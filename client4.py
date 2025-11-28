import socketio
import sounddevice as sd
import numpy as np
import queue
import threading
import time

# -----------------------------
# Config
# -----------------------------
SERVER_URL = "http://192.168.31.16:5000"  # Change to your server IP
SAMPLE_RATE = 16000
CHANNELS = 1
BLOCKSIZE = 1024

# -----------------------------
# Socket.IO client
# -----------------------------
sio = socketio.Client()

@sio.event
def connect():
    print("? Connected to server.")

@sio.event
def disconnect():
    print("? Disconnected from server.")

@sio.on("server_audio")
def on_server_audio(data):
    """Play server audio safely in a separate thread."""
    def play_audio():
        audio_array = np.frombuffer(data, dtype=np.int16)
        sd.play(audio_array, samplerate=SAMPLE_RATE)
        sd.wait()
        print("?? Played server audio.")

    threading.Thread(target=play_audio, daemon=True).start()

# -----------------------------
# Recording
# -----------------------------
audio_queue = queue.Queue()
recording = False
stream = None

def audio_callback(indata, frames, time_info, status):
    if recording:
        audio_queue.put(bytes(indata))

def start_recording():
    global stream, recording
    if not recording:
        recording = True
        audio_queue.queue.clear()  # clear old data
        stream = sd.InputStream(
            samplerate=SAMPLE_RATE,
            channels=CHANNELS,
            dtype='int16',
            blocksize=BLOCKSIZE,
            callback=audio_callback
        )
        stream.start()
        print("?? Recording started...")

def stop_recording():
    global stream, recording
    if recording:
        recording = False
        if stream:
            stream.stop()
            stream.close()
            stream = None
        print("?? Recording stopped.")
        # Wait a bit to avoid Bluetooth device conflicts
        time.sleep(0.2)

def send_audio_to_server():
    """Send all queued audio to server in chunks."""
    chunks = []
    while not audio_queue.empty():
        chunks.append(audio_queue.get())
    if not chunks:
        print("?? No audio captured to send.")
        return
    data = b''.join(chunks)
    chunk_size = 4096
    for i in range(0, len(data), chunk_size):
        sio.emit("audio_chunk", data[i:i+chunk_size])
        time.sleep(0.05)
    sio.emit("audio_complete")
    print(f"? Sent {len(data)} bytes of audio to server.")

# -----------------------------
# Main loop
# -----------------------------
def run_client():
    sio.connect(SERVER_URL)
    print("Type 'a' + Enter to toggle recording, 'q' + Enter to quit.")

    try:
        while True:
            cmd = input(">> ").strip().lower()
            if cmd == "a":
                if recording:
                    stop_recording()
                    send_audio_to_server()
                else:
                    start_recording()
            elif cmd == "q":
                print("?? Exiting client...")
                break
    finally:
        stop_recording()
        sio.disconnect()

if __name__ == "__main__":
    run_client()
