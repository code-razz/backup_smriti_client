import socketio
import numpy as np
import sounddevice as sd
import time

sio = socketio.Client()

@sio.on("server_reply")
def on_reply(data):
    print("Received reply audio, playing...")
    audio = np.frombuffer(data, dtype=np.int16)
    sd.play(audio, samplerate=16000)
    sd.wait()

def generate_sine_wave(duration=3, freq=440, sample_rate=16000):
    t = np.linspace(0, duration, int(sample_rate * duration), False)
    tone = np.sin(freq * t * 2 * np.pi)
    audio = (tone * 32767).astype(np.int16)
    return audio.tobytes()

def run_client():
    sio.connect("http://192.168.31.16:5000")
    data = generate_sine_wave()
    chunk_size = 4096
    for i in range(0, len(data), chunk_size):
        sio.emit("audio_chunk", data[i:i+chunk_size])
        time.sleep(0.05)
    sio.emit("audio_complete")
    sio.wait()

if __name__ == "__main__":
    run_client()
