# client.py
import socketio
import sounddevice as sd
import numpy as np
import threading
import queue
import time
import sys

# GPIO (pigpio backend)
from gpiozero import Button
from gpiozero.pins.pigpio import PiGPIOFactory

# -----------------------------
# Config
# -----------------------------
SERVER_URL = "http://192.168.31.16:5000"  # change to your server IP
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
    print("✅ Connected to server")

@sio.event
def disconnect():
    print("❌ Disconnected from server")

# -----------------------------
# Playback control and queue
# -----------------------------
playback_queue = queue.Queue()
playback_enabled = threading.Event()     # when set, allow enqueueing & playing incoming server audio
playback_worker_running = threading.Event()

# server -> client chunk handler
@sio.on("server_audio_chunk")
def on_server_audio_chunk(data):
    # Only accept and enqueue server chunks if playback is enabled
    if not playback_enabled.is_set():
        # drop chunk if playback disabled
        return
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
                # If playback disabled, sleep briefly
                if not playback_enabled.is_set():
                    time.sleep(0.05)
                    # clear any queued frames to avoid playing stale audio once re-enabled
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

def ensure_stream_running():
    global stream
    with stream_lock:
        if stream is None:
            stream = sd.InputStream(samplerate=SAMPLE_RATE, channels=CHANNELS, dtype='int16',
                                     blocksize=BLOCKSIZE, callback=audio_callback)
            stream.start()
            print("🎧 Input stream started")

def stop_stream_if_unused():
    global stream
    with stream_lock:
        if stream is not None and not recording_query and not recording_context:
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
    if recording_query:
        raw = indata.tobytes()
        try:
            sio.emit("audio_chunk", raw)
        except Exception as e:
            print(f"❌ Failed to emit audio_chunk: {e}")
    elif recording_context:
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
    if recording_context:
        return
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

    recording_context = True
    ensure_stream_running()
    print("🟣 Context recording started (sending to server).")

def stop_context_recording():
    """
    Stop context recording and tell server to finalize the context file.
    """
    global recording_context
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
# GPIO: configure both buttons
# -----------------------------
def setup_buttons():
    """
    Configure two gpiozero Buttons with pigpio backend.
    - Button1: press-and-hold behaviour for query (when_pressed -> start_query_recording, when_released -> stop_query_recording)
    - Button2: single-press toggle for context: when_pressed -> toggle context on/off
    """
    factory = PiGPIOFactory()
    btn1 = Button(BUTTON1_PIN, pin_factory=factory, pull_up=True, bounce_time=BOUNCE_TIME)
    btn2 = Button(BUTTON2_PIN, pin_factory=factory, pull_up=True, bounce_time=BOUNCE_TIME)

    # Button1 press-and-hold (query)
    def on_btn1_press():
        threading.Thread(target=start_query_recording, daemon=True).start()

    def on_btn1_release():
        threading.Thread(target=stop_query_recording, daemon=True).start()

    btn1.when_pressed = on_btn1_press
    btn1.when_released = on_btn1_release

    # Button2 single-press toggle (context)
    def on_btn2_press():
        # toggle context: start new session if currently not active; else stop it
        def worker():
            # If currently recording context, stop it
            if recording_context:
                stop_context_recording()
            else:
                # Start a NEW context session when user toggles button (not a resume)
                start_context_recording(new_session=True)
        threading.Thread(target=worker, daemon=True).start()

    btn2.when_pressed = on_btn2_press

    print(f"🔘 Button1 (query) on BCM pin {BUTTON1_PIN} configured (press & hold).")
    print(f"🔘 Button2 (context) on BCM pin {BUTTON2_PIN} configured (single press toggles).")
    return btn1, btn2

# -----------------------------
# Main loop (only 'q' to quit; button controls recording)
# -----------------------------
def run_client():
    sio.connect(SERVER_URL)
    print("Push the physical button(s) to record. Type 'q' + Enter to quit.")
    # Setup button handlers (pigpio)
    try:
        btn1, btn2 = setup_buttons()
    except Exception as e:
        print("❌ Failed to set up buttons:", e)
        btn1 = btn2 = None

    try:
        while True:
            cmd = input(">> ").strip().lower()
            if cmd == "q":
                print("Exiting...")
                break
            # Ignore other input; recording handled by buttons
    finally:
        # cleanup: if any recording is active, stop them and notify server
        if recording_query:
            stop_query_recording()
        if recording_context:
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
        sio.disconnect()

if __name__ == "__main__":
    run_client()
