import socketio
import sounddevice as sd
import queue
import threading
import numpy as np

# Server URL
SERVER_URL = "http://192.168.31.16:5000"  # change if server runs elsewhere

# Socket.IO client
sio = socketio.Client()

# Audio parameters
SAMPLE_RATE = 16000
CHANNELS = 1
BLOCKSIZE = 1024

# Queue for audio data
audio_queue = queue.Queue()
recording = False
stream = None

def audio_callback(indata, frames, time, status):
    """Callback to capture audio from microphone."""
    if recording:
        audio_queue.put(bytes(indata))



@sio.on("server_audio")
def on_server_audio(data):
    """Play audio received from server."""
    # Convert raw bytes back to numpy array
    audio_array = np.frombuffer(data, dtype=np.int16)

    sd.play(audio_array, SAMPLE_RATE)
    sd.wait()
    print("?? Played response audio from server")



def start_recording():
    global stream, recording
    if not recording:
        recording = True
        stream = sd.InputStream(
            samplerate=SAMPLE_RATE,
            channels=CHANNELS,
            blocksize=BLOCKSIZE,
            dtype="int16",
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

def send_audio():
    """Background thread: send audio to server."""
    while True:
        data = audio_queue.get()
        if data is None:
            break
        sio.emit("audio_chunk", data)

@sio.event
def connect():
    print("? Connected to server.")

@sio.event
def disconnect():
    print("? Disconnected from server.")

def run_client():
    global recording
    # connect to server
    sio.connect(SERVER_URL)

    # start background audio sender
    threading.Thread(target=send_audio, daemon=True).start()

    print("Type 'a' + Enter to toggle recording, 'q' + Enter to quit.")
    while True:
        cmd = input(">> ").strip().lower()
        if cmd == "a":
            if recording:
                stop_recording()
            else:
                start_recording()
        elif cmd == "q":
            print("?? Exiting client...")
            break

    # cleanup
    stop_recording()
    audio_queue.put(None)
    sio.disconnect()

if __name__ == "__main__":
    run_client()
