#!/usr/bin/env python3
"""
client_pigpio_toggle.py

- Toggle recording with a single physical button (press to start, press again to stop & send).
- If playback is active when you start a new recording, playback is stopped immediately.
- Uses pigpiod backend (PiGPIOFactory).
"""

import socketio
import sounddevice as sd
import numpy as np
import queue
import threading
import time
import sys
from gpiozero import Button
from gpiozero.pins.pigpio import PiGPIOFactory

# -----------------------------
# Config
# -----------------------------
SERVER_URL = "http://192.168.31.16:5000"  # change if needed
SAMPLE_RATE = 16000
CHANNELS = 1
BLOCKSIZE = 1024
BUTTON_PIN = 17           # BCM pin for button
BOUNCE_TIME = 0.05        # debounce seconds
CHUNK_SIZE = 4096         # bytes per socket emit chunk

# -----------------------------
# Socket.IO client
# -----------------------------
sio = socketio.Client()

@sio.event
def connect():
    print("✅ Connected to server.")

@sio.event
def disconnect():
    print("⛔ Disconnected from server.")

# Playback control shared state
_playback_lock = threading.Lock()
_playback_thread = None
_playing = False

def stop_playback():
    """Stop any ongoing playback (safe to call from any thread)."""
    global _playing, _playback_thread
    with _playback_lock:
        if not _playing:
            return
        # sounddevice global stop
        try:
            sd.stop()
        except Exception:
            pass
        _playing = False

    # join the playback thread briefly (non-blocking if it's already ended)
    if _playback_thread is not None:
        _playback_thread.join(timeout=0.5)
        _playback_thread = None
    print("🔇 Playback stopped.")

@sio.on("server_audio")
def on_server_audio(data):
    """Play server audio in background; set playing flag; can be stopped by stop_playback()."""
    def play_audio(pbytes):
        global _playing, _playback_thread
        with _playback_lock:
            # if already playing, stop previous then continue with new
            if _playing:
                try:
                    sd.stop()
                except Exception:
                    pass
            _playing = True
            _playback_thread = threading.current_thread()

        try:
            audio_array = np.frombuffer(pbytes, dtype=np.int16)
            # play (non-blocking) and wait so thread remains alive while playing
            sd.play(audio_array, samplerate=SAMPLE_RATE)
            sd.wait()  # returns early if sd.stop() called elsewhere
        except Exception as e:
            print("Error during playback:", e)
        finally:
            with _playback_lock:
                _playing = False
                _playback_thread = None
            print("🔊 Playback finished.")

    # start playback thread (daemon so it doesn't prevent exit)
    threading.Thread(target=play_audio, args=(data,), daemon=True).start()

# -----------------------------
# Recording
# -----------------------------
audio_queue = queue.Queue()
_recording_lock = threading.Lock()
recording = False
stream = None

def audio_callback(indata, frames, time_info, status):
    if status:
        # optionally log or pass
        pass
    with _recording_lock:
        if recording:
            # convert to bytes for transport
            audio_queue.put(indata.tobytes())

def _drain_audio_queue():
    try:
        while True:
            audio_queue.get_nowait()
    except Exception:
        pass

def start_recording():
    """Start audio capture. If playback is active, stop it first."""
    global stream, recording
    # if playback playing, stop immediately
    stop_playback()

    with _recording_lock:
        if recording:
            print("⚠️ Already recording.")
            return
        recording = True

    # clear previous queued audio
    _drain_audio_queue()
    try:
        stream = sd.InputStream(
            samplerate=SAMPLE_RATE,
            channels=CHANNELS,
            dtype='int16',
            blocksize=BLOCKSIZE,
            callback=audio_callback
        )
        stream.start()
        print("⏺️ Recording started.")
    except Exception as e:
        with _recording_lock:
            recording = False
        print("Failed to start recording:", e)

def stop_recording():
    """Stop audio capture."""
    global stream, recording
    with _recording_lock:
        if not recording:
            return
        recording = False

    if stream:
        try:
            stream.stop()
            stream.close()
        except Exception:
            pass
        stream = None

    # delay to avoid audio device conflicts
    time.sleep(0.15)
    print("⏹️ Recording stopped.")

def send_audio_to_server():
    """Send audio queue contents to server in chunks."""
    chunks = []
    while True:
        try:
            chunks.append(audio_queue.get_nowait())
        except Exception:
            break

    if not chunks:
        print("ℹ️ No audio to send.")
        return

    data = b''.join(chunks)
    try:
        for i in range(0, len(data), CHUNK_SIZE):
            sio.emit("audio_chunk", data[i:i+CHUNK_SIZE])
            time.sleep(0.05)
        sio.emit("audio_complete")
        print(f"📤 Sent {len(data)} bytes to server.")
    except Exception as e:
        print("Error sending audio:", e)

# worker used when toggling off recording
def stop_and_send():
    stop_recording()
    send_audio_to_server()

# -----------------------------
# Button toggle behavior
# -----------------------------
def setup_button():
    """Set up button with PiGPIOFactory and a toggle on press."""
    try:
        factory = PiGPIOFactory()
    except Exception as e:
        print("Failed to create PiGPIOFactory:", e)
        raise

    btn = Button(BUTTON_PIN, pin_factory=factory, pull_up=True, bounce_time=BOUNCE_TIME)

    # Toggle behavior on press: start if not recording, otherwise stop & send.
    def on_press_toggle():
        global recording
        # Acquire lock to inspect state atomically
        with _recording_lock:
            currently_recording = recording

        if not currently_recording:
            # start recording, but if playback was active, stop_playback() is called in start_recording()
            start_recording()
        else:
            # stop + send in background so this callback returns quickly
            threading.Thread(target=stop_and_send, daemon=True).start()

    btn.when_pressed = on_press_toggle
    print(f"🔘 Toggle button configured on BCM pin {BUTTON_PIN}. Press once to start, press again to stop & send.")
    return btn

# -----------------------------
# Main loop
# -----------------------------
def run_client():
    print(f"Connecting to {SERVER_URL} ...")
    try:
        sio.connect(SERVER_URL)
    except Exception as e:
        print("Could not connect to server:", e)
    else:
        print("Connected. Button is active.")

    try:
        btn = setup_button()
    except Exception as e:
        print("Button setup failed:", e)
        try:
            sio.disconnect()
        except Exception:
            pass
        sys.exit(1)

    try:
        while True:
            time.sleep(0.5)
    except KeyboardInterrupt:
        print("\nExiting (Ctrl+C).")
    finally:
        try:
            btn.close()
        except Exception:
            pass
        # make sure to stop recording and send any buffered audio
        stop_recording()
        try:
            send_audio_to_server()
        except Exception:
            pass
        try:
            sio.disconnect()
        except Exception:
            pass
        print("Clean exit.")
        sys.exit(0)

if __name__ == "__main__":
    run_client()
