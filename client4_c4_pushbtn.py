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

# BCM pin for push button (change if needed)
BUTTON_PIN = 17
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
# Recording (streaming while recording)
# -----------------------------
recording = False
stream = None

def audio_callback(indata, frames, time_info, status):
    """
    Called continuously by sounddevice while recording.
    Immediately emit each chunk to the server so streaming is real-time.
    """
    if status:
        print(f"⚠️ Input status: {status}", file=sys.stderr)
    raw = indata.tobytes()
    try:
        sio.emit("audio_chunk", raw)
    except Exception as e:
        print(f"❌ Failed to emit audio_chunk: {e}")

def start_recording():
    global stream, recording
    if recording:
        return
    # Before starting recording: disable playback and inform server to stop streaming (if any)
    playback_enabled.clear()
    try:
        sio.emit("stop_server_stream")  # ask server to stop sending previous reply immediately
    except Exception as e:
        print(f"❌ Failed to emit stop_server_stream: {e}")

    # start input
    stream = sd.InputStream(samplerate=SAMPLE_RATE, channels=CHANNELS, dtype='int16',
                            blocksize=BLOCKSIZE, callback=audio_callback)
    stream.start()
    recording = True
    print("🎙️ Recording started (streaming to server). Playback stopped/disabled.")

def stop_recording():
    global stream, recording
    if not recording:
        return
    recording = False
    if stream:
        stream.stop()
        stream.close()
        stream = None
    # Tell server we're done so it can begin streaming reply
    try:
        sio.emit("audio_complete")
        print("⏹️ Recording stopped. Sent audio_complete to server. Playback will be accepted when server streams.")
    except Exception as e:
        print(f"❌ Failed to emit audio_complete: {e}")
    # Enable playback — we will accept and play server chunks now
    playback_enabled.set()

# -----------------------------
# GPIO: configure button (press-and-hold)
# -----------------------------
def setup_button():
    """
    Configure gpiozero Button with pigpio backend.
    Press-and-hold behavior: when_pressed -> start_recording, when_released -> stop_recording.
    Both calls are dispatched to worker threads to avoid blocking gpio callbacks.
    """
    factory = PiGPIOFactory()
    btn = Button(BUTTON_PIN, pin_factory=factory, pull_up=True, bounce_time=BOUNCE_TIME)

    def on_press():
        # run in thread so gpio callback returns quickly
        threading.Thread(target=start_recording, daemon=True).start()

    def on_release():
        # run stop in a thread to avoid blocking gpio callback
        threading.Thread(target=stop_recording, daemon=True).start()

    btn.when_pressed = on_press
    btn.when_released = on_release
    print(f"🔘 Button configured on BCM pin {BUTTON_PIN} (press & hold to record; release to stop).")
    return btn

# -----------------------------
# Main loop (only 'q' to quit; button controls recording)
# -----------------------------
def run_client():
    sio.connect(SERVER_URL)
    print("Push the physical button to record (press-and-hold). Type 'q' + Enter to quit.")
    # Setup button handlers (pigpio)
    try:
        btn = setup_button()
    except Exception as e:
        print("❌ Failed to set up button:", e)
        btn = None

    try:
        while True:
            cmd = input(">> ").strip().lower()
            if cmd == "q":
                print("Exiting...")
                break
            # Ignore other input; recording handled by button
    finally:
        # cleanup
        if recording:
            stop_recording()
        playback_worker_running.clear()
        playback_enabled.clear()
        time.sleep(0.05)
        # close button if exists
        try:
            if btn:
                btn.close()
        except Exception:
            pass
        sio.disconnect()

if __name__ == "__main__":
    run_client()
