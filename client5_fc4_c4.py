import socketio
import sounddevice as sd
import numpy as np
import threading
import queue
import time
import sys
from gpiozero import Button
from gpiozero.pins.pigpio import PiGPIOFactory

# -----------------------------
# Config
# -----------------------------
SERVER_URL = "http://192.168.31.16:5000"  # change to your server IP
SERVER_URL = "http://10.206.120.206:5000"  # change to your server IP
SAMPLE_RATE = 16000
CHANNELS = 1
BLOCKSIZE = 1024
BUTTON1_PIN = 17   # press-hold for query
BUTTON2_PIN = 27   # toggle for context
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
# Playback
# -----------------------------
playback_queue = queue.Queue()
playback_enabled = threading.Event()
playback_worker_running = threading.Event()

@sio.on("server_audio_chunk")
def on_server_audio_chunk(data):
    if not playback_enabled.is_set():
        return
    arr = np.frombuffer(data, dtype=np.int16)
    playback_queue.put(arr)

@sio.on("server_audio_complete")
def on_server_audio_complete():
    print("ℹ️ Server finished streaming reply.")
    global context_paused
    if context_active and not recording:
        context_paused = False
        print("▶️ Resuming context recording after query/playback complete.")

def playback_worker():
    try:
        with sd.OutputStream(samplerate=SAMPLE_RATE, channels=CHANNELS, dtype='int16') as out_stream:
            print("🔊 Playback stream opened")
            playback_worker_running.set()
            while playback_worker_running.is_set():
                if not playback_enabled.is_set():
                    time.sleep(0.05)
                    while not playback_queue.empty():
                        playback_queue.get_nowait()
                    continue
                try:
                    arr = playback_queue.get(timeout=0.1)
                except queue.Empty:
                    continue
                if arr.ndim == 1:
                    arr = arr.reshape(-1, CHANNELS)
                out_stream.write(arr)
    except Exception as e:
        print(f"❌ Playback stream error: {e}")
    finally:
        playback_worker_running.clear()
        print("🔇 Playback worker stopped")

threading.Thread(target=playback_worker, daemon=True).start()
while not playback_worker_running.is_set():
    time.sleep(0.01)

# -----------------------------
# Recording states
# -----------------------------
recording = False
stream = None
context_active = False
context_paused = False

# -----------------------------
# Audio callback
# -----------------------------
def audio_callback(indata, frames, time_info, status):
    if status:
        print(f"⚠️ Input status: {status}", file=sys.stderr)
    raw = indata.tobytes()
    try:
        if recording:
            sio.emit("audio_chunk_query", raw)
        elif context_active and not context_paused:
            sio.emit("audio_chunk_context", raw)
    except Exception as e:
        print(f"❌ Failed to emit chunk: {e}")

# -----------------------------
# Query recording logic
# -----------------------------
def start_recording():
    global stream, recording, context_paused
    if recording:
        return
    playback_enabled.clear()
    try:
        sio.emit("stop_server_stream")
    except:
        pass
    if context_active:
        context_paused = True
        print("⏸️ Context recording paused during query.")
    stream = sd.InputStream(samplerate=SAMPLE_RATE, channels=CHANNELS,
                            dtype='int16', blocksize=BLOCKSIZE, callback=audio_callback)
    stream.start()
    recording = True
    print("🎙️ Query recording started.")

def stop_recording():
    global stream, recording
    if not recording:
        return
    recording = False
    if stream:
        stream.stop()
        stream.close()
        stream = None
    try:
        sio.emit("audio_complete_query")
    except:
        pass
    print("⏹️ Query recording stopped. Sent to server.")
    playback_enabled.set()

# -----------------------------
# Context recording logic
# -----------------------------
def start_context_recording():
    global stream, context_active, context_paused
    if context_active:
        return
    context_active = True
    context_paused = recording
    if not recording and not (stream and stream.active):
        stream = sd.InputStream(samplerate=SAMPLE_RATE, channels=CHANNELS,
                                dtype='int16', blocksize=BLOCKSIZE, callback=audio_callback)
        stream.start()
    print(f"🟢 Context recording started (paused={context_paused}).")

def stop_context_recording():
    global stream, context_active
    if not context_active:
        return
    try:
        sio.emit("audio_complete_context")
    except:
        pass
    print("⏹️ Context recording stopped. Sent to server.")
    context_active = False
    if not recording and stream:
        stream.stop()
        stream.close()
        stream = None

def toggle_context():
    if context_active:
        stop_context_recording()
    else:
        start_context_recording()

# -----------------------------
# Button setup
# -----------------------------
def setup_button():
    factory = PiGPIOFactory()
    btn1 = Button(BUTTON1_PIN, pin_factory=factory, pull_up=True, bounce_time=BOUNCE_TIME)
    btn2 = Button(BUTTON2_PIN, pin_factory=factory, pull_up=True, bounce_time=BOUNCE_TIME)

    btn1.when_pressed = lambda: threading.Thread(target=start_recording, daemon=True).start()
    btn1.when_released = lambda: threading.Thread(target=stop_recording, daemon=True).start()
    btn2.when_pressed = lambda: threading.Thread(target=toggle_context, daemon=True).start()

    print(f"🔘 Button1 (query, press-hold) = BCM {BUTTON1_PIN}")
    print(f"🔘 Button2 (context, toggle)    = BCM {BUTTON2_PIN}")
    return btn1, btn2

# -----------------------------
# Main
# -----------------------------
def run_client():
    sio.connect(SERVER_URL)
    print("Push Button1 (hold) for QUERY recording.")
    print("Press Button2 (toggle) for CONTEXT recording.")
    print("Type 'q' to quit.")
    try:
        btn1, btn2 = setup_button()
    except Exception as e:
        print("❌ Failed to setup buttons:", e)
        btn1 = btn2 = None

    try:
        while True:
            cmd = input(">> ").strip().lower()
            if cmd == "q":
                break
    finally:
        if recording:
            stop_recording()
        if context_active:
            stop_context_recording()
        playback_worker_running.clear()
        playback_enabled.clear()
        time.sleep(0.05)
        if btn1: btn1.close()
        if btn2: btn2.close()
        sio.disconnect()

if __name__ == "__main__":
    run_client()
