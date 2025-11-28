from picamera2 import Picamera2
import time

# Initialize the camera
picam2 = Picamera2()

# Configure the camera (optional settings)
picam2.start_preview()

# Allow the camera to adjust
time.sleep(2)

# Capture and save the image
picam2.capture_file('image.jpg')

# Stop preview
picam2.stop_preview()

print("Image captured and saved as image.jpg")
