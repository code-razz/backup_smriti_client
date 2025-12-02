# client.py (improved)
import socketio
import sounddevice as sd
import numpy as np
import threading
import queue
import time
import sys
import signal

# GPIO (pigpio backend)
from gpiozero import Button
from gpiozero.pins.pigpio import PiGPIOFactory

import requests
import cv2
import os
from io import BytesIO
from PIL import Image
from picamera2 import Picamera2

# -----------------------------
# Config
# -----------------------------
SERVER_URL = "http://192.168.31.16:5000"  # change to your server IP
PROCESS_FACE_ENDPOINT = f"{SERVER_URL}/process_face"
SAMPLE_RATE = 16000
CHANNELS = 1
BLOCKSIZE = 1024  # frames per callback

# BCM pins for push buttons
BUTTON1_PIN = 17   # existing press-and-hold (query-recording)
BUTTON2_PIN = 27   # new single-press toggle (context-recording)
BOUNCE_TIME = 0.05

# -----------------------------
# Socket.IO client
# -----------------------------
sio = socketio.Client()

@sio.event
def connect():
    print(f"✅ Connected to server. SID: {sio.sid}")

@sio.event
def reconnect():
    print(f"🔄 Reconnected to server. New SID: {sio.sid}")

@sio.event
def disconnect():
    print("❌ Disconnected from server")

# -----------------------------
# Playback control and queue
# -----------------------------
playback_queue = queue.Queue()
playback_enabled = threading.Event()     # when set, allow enqueueing & playing incoming server audio
playback_worker_running = threading.Event()

# Start playback enabled by default (so server replies will play unless disabled for a query)
playback_enabled.set()

# server -> client chunk handler
@sio.on("server_audio_chunk")
def on_server_audio_chunk(data):
    # Only accept and enqueue server chunks if playback is enabled
    if not playback_enabled.is_set():
        print(f"⚠️ Dropping audio chunk ({len(data)} bytes) because playback is disabled.")
        return
    
    # Debug: print every chunk size
    # print(f"🔊 Rx chunk: {len(data)} bytes") 
    arr = np.frombuffer(data, dtype=np.int16)
    playback_queue.put(arr)

@sio.on("server_audio_complete")
def on_server_audio_complete():
    # server signalled end of its stream
    print("ℹ️ Server finished streaming reply")

# -----------------------------
# Playback thread
# -----------------------------
def playback_worker():
    """
    Reusable playback worker that consumes numpy arrays from playback_queue and writes them to an OutputStream.
    The worker runs continuously; it only plays when playback_enabled is set and queue has items.
    """
    try:
        with sd.OutputStream(samplerate=SAMPLE_RATE, channels=CHANNELS, dtype='int16') as out_stream:
            print("🔊 Playback stream opened")
            playback_worker_running.set()
            while playback_worker_running.is_set():
                # If playback disabled, sleep briefly and clear queued frames
                if not playback_enabled.is_set():
                    time.sleep(0.05)
                    try:
                        while not playback_queue.empty():
                            playback_queue.get_nowait()
                    except queue.Empty:
                        pass
                    continue

                try:
                    arr = playback_queue.get(timeout=0.1)
                except queue.Empty:
                    continue

                if arr.ndim == 1:
                    arr = arr.reshape(-1, CHANNELS)
                try:
                    out_stream.write(arr)
                except Exception as e:
                    print(f"❌ Playback write error: {e}")
    except Exception as e:
        print(f"❌ Playback stream error: {e}")
    finally:
        playback_worker_running.clear()
        print("🔇 Playback worker stopped")

# start playback worker thread once
threading.Thread(target=playback_worker, daemon=True).start()
# wait for it to open
while not playback_worker_running.is_set():
    time.sleep(0.01)

# -----------------------------
# Recording (shared InputStream for both modes)
# -----------------------------
stream = None
stream_lock = threading.Lock()

# which mode is currently active
recording_query = False
recording_context = False
context_paused_by_query = False  # used to remember to resume context after query finishes

# protect reading/writing of recording flags
recording_lock = threading.Lock()

def ensure_stream_running():
    global stream
    with stream_lock:
        if stream is None:
            try:
                stream = sd.InputStream(samplerate=SAMPLE_RATE, channels=CHANNELS, dtype='int16',
                                         blocksize=BLOCKSIZE, callback=audio_callback)
                stream.start()
                print("🎧 Input stream started")
            except Exception as e:
                stream = None
                print(f"❌ Failed to start input stream: {e}")

def stop_stream_if_unused():
    global stream
    with stream_lock:
        with recording_lock:
            unused = not recording_query and not recording_context
        if stream is not None and unused:
            try:
                stream.stop()
                stream.close()
            except Exception:
                pass
            stream = None
            print("🛑 Input stream stopped (no active recordings)")

def audio_callback(indata, frames, time_info, status):
    """
    Called continuously by sounddevice while the input stream is running.
    Decide which recording mode (if any) is active and emit chunks appropriately.
    """
    if status:
        print(f"⚠️ Input status: {status}", file=sys.stderr)
    # prefer query chunks if both flags somehow true
    with recording_lock:
        rq = recording_query
        rc = recording_context

    if rq:
        raw = indata.tobytes()
        try:
            sio.emit("audio_chunk", raw)
        except Exception as e:
            print(f"❌ Failed to emit audio_chunk: {e}")
    elif rc:
        raw = indata.tobytes()
        try:
            sio.emit("context_audio_chunk", raw)
        except Exception as e:
            print(f"❌ Failed to emit context_audio_chunk: {e}")
    else:
        # neither recording active -> do nothing
        pass

# -----------------------------
# Query recording functions (push-and-hold) — behavior preserved
# -----------------------------
def start_query_recording():
    global recording_query, recording_context, context_paused_by_query
    with recording_lock:
        if recording_query:
            return
        # If context recording is active, pause it and remember to resume after query completes
        if recording_context:
            context_paused_by_query = True
            recording_context = False
            try:
                sio.emit("context_pause")
                print("🟣 Context recording paused because query recording started")
            except Exception as e:
                print(f"❌ Failed to emit context_pause: {e}")

        # Before starting query: disable playback and inform server to stop streaming (if any)
        playback_enabled.clear()
        try:
            sio.emit("stop_server_stream")  # ask server to stop sending previous reply immediately
        except Exception as e:
            print(f"❌ Failed to emit stop_server_stream: {e}")

        recording_query = True

    ensure_stream_running()
    print("🎙️ Query recording started (press-hold). Playback stopped/disabled.")

def stop_query_recording():
    global recording_query, context_paused_by_query, recording_context
    with recording_lock:
        if not recording_query:
            return
        recording_query = False

    # Tell server we're done so it can begin streaming reply
    try:
        sio.emit("audio_complete")
        print("⏹️ Query recording stopped. Sent audio_complete to server. Playback will be accepted when server streams.")
    except Exception as e:
        print(f"❌ Failed to emit audio_complete: {e}")

    # Enable playback — we will accept and play server chunks now
    playback_enabled.set()

    # If context was paused due to query, resume it automatically
    with recording_lock:
        if context_paused_by_query:
            context_paused_by_query = False
            recording_context = True
            try:
                sio.emit("context_resume")
                print("🟣 Context recording resumed automatically after query finished")
            except Exception as e:
                print(f"❌ Failed to emit context_resume: {e}")

    # If no recording left, stop input stream
    stop_stream_if_unused()

# -----------------------------
# Context recording functions (push-button2 toggle)
# -----------------------------
def start_context_recording(new_session=True):
    """
    Start context recording. If new_session True, tell server to clear previous context buffer.
    """
    global recording_context
    with recording_lock:
        if recording_context:
            return
        recording_context = True

    # inform server of start (clear) or resume
    if new_session:
        try:
            sio.emit("context_start")
        except Exception as e:
            print(f"❌ Failed to emit context_start: {e}")
    else:
        try:
            sio.emit("context_resume")
        except Exception as e:
            print(f"❌ Failed to emit context_resume: {e}")

    ensure_stream_running()
    print("🟣 Context recording started (sending to server).")

def stop_context_recording():
    """
    Stop context recording and tell server to finalize the context file.
    """
    global recording_context
    with recording_lock:
        if not recording_context:
            return
        recording_context = False

    try:
        sio.emit("context_audio_complete")
        print("🟣 Context recording stopped. Sent context_audio_complete to server.")
    except Exception as e:
        print(f"❌ Failed to emit context_audio_complete: {e}")

    stop_stream_if_unused()

# -----------------------------
# Face Recognition Functions
# -----------------------------
def get_face_image_bytes():
    """
    Captures an image from the camera using picamera2.
    Captures 5 frames (Smart Burst) and selects the sharpest one using Laplacian variance.
    """
    print("📷 Opening camera...")
    picam2 = Picamera2()
    
    try:
        # Configure camera for still capture at 1280x720 (720p for better face recognition)
        config = picam2.create_still_configuration(main={"size": (1280, 720)})
        picam2.configure(config)
        picam2.start()
        
        print(f"   - Camera initialized at: 1280x720")
        print("   - Warming up (2s)...")
        time.sleep(2.0)

        captured_bytes = None
        best_frame = None
        best_score = -1.0
        
        # Capture 5 frames in quick succession
        for i in range(5):
            try:
                # Capture frame as numpy array (RGB format)
                burst_frame = picam2.capture_array() 
                
                # Convert RGB to BGR for cv2 compatibility
                burst_frame_bgr = cv2.cvtColor(burst_frame, cv2.COLOR_RGB2BGR)
                
                # Calculate sharpness using Laplacian Variance
                gray = cv2.cvtColor(burst_frame_bgr, cv2.COLOR_BGR2GRAY)
                score = cv2.Laplacian(gray, cv2.CV_64F).var()
                
                if score > best_score:
                    best_score = score
                    best_frame = burst_frame_bgr
                
                time.sleep(0.05)
                
            except Exception as e:
                print(f"   ⚠️ Error capturing frame {i+1}: {e}")
                continue
        
        if best_frame is not None:
            print(f"✨ Selected best frame (Score: {best_score:.2f})")
            # Convert to JPEG bytes
            is_success, buffer = cv2.imencode(".jpg", best_frame)
            if is_success:
                captured_bytes = buffer.tobytes()
            else:
                print("❌ Failed to encode image.")
        else:
            print("❌ Failed to capture any valid frames.")
                
    except Exception as e:
        print(f"❌ Camera error: {e}")
        captured_bytes = None
    finally:
        try:
            picam2.stop()
            picam2.close()
        except:
            pass
    
    return captured_bytes

def perform_face_recognition():
    """
    Captures face and sends to server for recognition.
    """
    print("🤖 Starting Face Recognition...")
    image_bytes = get_face_image_bytes()
    
    if not image_bytes:
        print("❌ No image captured.")
        return

    print("🚀 Sending to server for recognition...")
    try:
        print(f"   (DEBUG) Using SocketIO SID: {sio.sid}")
        files = {'image': ('face.jpg', image_bytes, 'image/jpeg')}
        data = {'sid': sio.sid} if sio.sid else {}
        # add a short timeout so the client doesn't hang if server is unreachable
        response = requests.post(PROCESS_FACE_ENDPOINT, files=files, data=data, timeout=10.0)
        
        # Accept both 200 (recognized) and 201 (enrolled) as success
        if response.status_code not in [200, 201]:
            print(f"❌ Server Error: {response.status_code} - {response.text}")
            return
            
        result = response.json()
        status = result.get("status")
        
        if status == "recognized" or status == "enrolled":
            name = result.get("name")
            if status == "enrolled":
                print(f"\n🆕 NEW PERSON ENROLLED: {name} 🆕")
            else:
                print(f"\n✨ RECOGNIZED: {name} ✨")
            
        elif status == "no_face":
            print("\n⚠️ No face detected.")

        elif status == "unknown":
            print("\n❓ Face Unknown.")
            
        elif status == "error":
            print(f"❌ Error: {result.get('message')}")
            
    except requests.Timeout:
        print("❌ Face recognition request timed out.")
    except Exception as e:
        print(f"❌ Connection Error: {e}")

# -----------------------------
# GPIO: configure both buttons
# -----------------------------
def setup_buttons():
    """
    Configure two gpiozero Buttons with pigpio backend.
    - Button1: press-and-hold behaviour for query (when_held -> start_query_recording, when_released -> stop_query_recording)
      AND single-press for face recognition (when_released without hold).
    - Button2: single-press toggle for context: when_pressed -> toggle context on/off
    """
    factory = PiGPIOFactory()
    # Button1: hold_time=0.5s to distinguish click vs hold
    btn1 = Button(BUTTON1_PIN, pin_factory=factory, pull_up=True, bounce_time=BOUNCE_TIME, hold_time=0.5)
    btn2 = Button(BUTTON2_PIN, pin_factory=factory, pull_up=True, bounce_time=BOUNCE_TIME)

    # Button1 Logic
    def on_btn1_hold():
        # Triggered after hold_time
        threading.Thread(target=start_query_recording, daemon=True).start()

    def on_btn1_release():
        # If we were recording query, stop it.
        # If we were NOT recording query, it means it was a short press -> Face Rec
        with recording_lock:
            rq = recording_query
        if rq:
            threading.Thread(target=stop_query_recording, daemon=True).start()
        else:
            # Short press
            threading.Thread(target=perform_face_recognition, daemon=True).start()

    btn1.when_held = on_btn1_hold
    btn1.when_released = on_btn1_release

    # Button2 single-press toggle (context)
    def on_btn2_press():
        # toggle context: start new session if currently not active; else stop it
        def worker():
            with recording_lock:
                rc = recording_context
            if rc:
                stop_context_recording()
            else:
                # Start a NEW context session when user toggles button (not a resume)
                start_context_recording(new_session=True)
        threading.Thread(target=worker, daemon=True).start()

    btn2.when_pressed = on_btn2_press

    print(f"🔘 Button1 (query) on BCM pin {BUTTON1_PIN} configured (press & hold / click).")
    print(f"🔘 Button2 (context) on BCM pin {BUTTON2_PIN} configured (single press toggles).")
    return btn1, btn2

# -----------------------------
# Graceful shutdown handling
# -----------------------------
def _graceful_shutdown(signum=None, frame=None):
    print("\nShutting down (signal received). Cleaning up...")
    try:
        with recording_lock:
            if recording_query:
                stop_query_recording()
            if recording_context:
                stop_context_recording()
    except Exception:
        pass

    # stop playback worker
    playback_worker_running.clear()
    playback_enabled.clear()
    time.sleep(0.05)
    try:
        sio.disconnect()
    except Exception:
        pass
    # allow process to exit
    sys.exit(0)

signal.signal(signal.SIGINT, _graceful_shutdown)
signal.signal(signal.SIGTERM, _graceful_shutdown)

# -----------------------------
# Main loop (only 'q' to quit; button controls recording)
# -----------------------------
def run_client():
    try:
        sio.connect(SERVER_URL)
    except Exception as e:
        print(f"❌ Could not connect to server: {e}")
        # still allow local ops like face capture, but most features require server
    print("Push the physical button(s) to record. Type 'q' + Enter to quit.")
    # Setup button handlers (pigpio)
    try:
        btn1, btn2 = setup_buttons()
    except Exception as e:
        print("❌ Failed to set up buttons:", e)
        btn1 = btn2 = None

    try:
        while True:
            try:
                cmd = input(">> ").strip().lower()
            except EOFError:
                # e.g. ran without tty; just sleep
                time.sleep(0.2)
                continue
            if cmd == "q":
                print("Exiting...")
                break
            # Ignore other input; recording handled by buttons
    finally:
        # cleanup: if any recording is active, stop them and notify server
        with recording_lock:
            rq = recording_query
            rc = recording_context

        if rq:
            stop_query_recording()
        if rc:
            stop_context_recording()

        playback_worker_running.clear()
        playback_enabled.clear()
        time.sleep(0.05)
        # close buttons if exist
        try:
            if btn1:
                btn1.close()
            if btn2:
                btn2.close()
        except Exception:
            pass
        try:
            sio.disconnect()
        except Exception:
            pass

if __name__ == "__main__":
    run_client()
