from picamera2 import Picamera2
import time

picam2 = Picamera2()
picam2.start()
time.sleep(2)
picam2.capture_file("test.jpg")
picam2.stop()
print("Image captured as test.jpg")
