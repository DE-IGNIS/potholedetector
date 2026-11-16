# Pothole Detector

A lightweight web app that lets you upload a road photo, detect potholes using an ONNX model, and file a simulated municipal ticket for the reported location.

## Overview

This project combines a Python FastAPI backend with a simple browser-based frontend. A user uploads an image, the server runs object detection on the road surface, and the app can either use the photo's GPS metadata or the user's current/manual location to generate a ticket record.

The project is designed as a learning/demo application. It does not send real reports to government agencies. Ticket data is stored locally and exported as simulated letters in the project folders.

## Features

- Upload a road image from the browser
- Detect potholes with a trained ONNX model
- Draw bounding boxes around detected potholes
- Read GPS location from image EXIF metadata
- Reverse-geocode coordinates to a nearby place and address
- Match a location with a municipality and contact email
- Save a simulated ticket record in SQLite
- Create a draft letter in the outbox folder
- Save evidence images locally for review

## Tech Stack

- Python
- FastAPI
- Uvicorn
- OpenCV
- ONNX Runtime
- NumPy
- Pillow
- SQLite
- Static HTML/CSS/JS frontend in the docs folder

## Project Structure

- server.py — FastAPI backend and detection logic
- run.py — starts the local model server and optional Cloudflare tunnel
- docs/ — frontend UI (HTML, CSS, JavaScript)
- best.onnx — trained detection model
- municipalities.json — municipality lookup data
- tickets.db — local SQLite ticket database
- evidence/ — saved evidence photos
- outbox/ — generated ticket letters
- requirements.txt — Python dependencies

## Getting Started

### Prerequisites

- Python 3.10+
- pip
- Optional: Cloudflared for public tunnel access

### Install dependencies

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

### Start the app

```bash
python run.py
```

This starts the backend and prints a local URL. If Cloudflared is installed, it may also print a public tunnel link so the app can be opened remotely.

## Usage

1. Open the local app URL in your browser.
2. Upload a road image.
3. Run pothole detection.
4. Confirm or provide a location.
5. File the ticket.
6. Review the local database, evidence image, and generated letter.

## Important Notes

- Tickets are simulated only.
- Nothing is emailed to a real municipality.
- Files in evidence/ and outbox/ are generated locally for demonstration purposes.
- The app is intended for local testing and educational use.

## License

This project is provided as-is for learning and demonstration purposes. Use it responsibly and only for local or non-production testing.