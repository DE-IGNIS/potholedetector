"""Pothole detector backend.

POST /predict  photo -> detections, annotated image, EXIF location (if any)
POST /ticket   detection + location -> ticket for the responsible municipality
GET  /health   used by the website to show "server online"

Tickets are SIMULATED: saved to tickets.db, with a letter in outbox/ and the
evidence photo in evidence/. Nothing is emailed to a real municipality.
"""
import base64
import json
import math
import os
import secrets
import sqlite3
import threading
import time
import urllib.parse
import urllib.request
from collections import defaultdict, deque
from datetime import datetime, timedelta, timezone
from io import BytesIO

import cv2
import numpy as np
import onnxruntime as ort
from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from PIL import Image
from pydantic import BaseModel, Field

HERE = os.path.dirname(os.path.abspath(__file__))
MODEL_PATH = os.path.join(HERE, "best.onnx")
MUNI_PATH = os.path.join(HERE, "municipalities.json")
DB_PATH = os.path.join(HERE, "tickets.db")
EVIDENCE_DIR = os.path.join(HERE, "evidence")
OUTBOX_DIR = os.path.join(HERE, "outbox")

IMG_SIZE = 640
MAX_BYTES = 10 * 1024 * 1024        # upload size cap
NMS_IOU = 0.45
DEDUPE_METERS = 30                  # same spot within this distance...
DEDUPE_HOURS = 24                   # ...and this time counts as already reported
CACHE_TTL = 900                     # seconds a detection can be turned into a ticket
CACHE_MAX = 100

os.makedirs(EVIDENCE_DIR, exist_ok=True)
os.makedirs(OUTBOX_DIR, exist_ok=True)

session = ort.InferenceSession(MODEL_PATH, providers=["CPUExecutionProvider"])
input_name = session.get_inputs()[0].name

app = FastAPI(title="Pothole Detector")
app.add_middleware(CORSMiddleware, allow_origins=["*"],
                   allow_methods=["GET", "POST"], allow_headers=["Content-Type"])

db = sqlite3.connect(DB_PATH, check_same_thread=False)
db_lock = threading.Lock()
with db_lock:
    db.execute("""CREATE TABLE IF NOT EXISTS tickets (
        id TEXT PRIMARY KEY, created_at TEXT, lat REAL, lon REAL,
        place TEXT, address TEXT, municipality TEXT, email TEXT,
        pothole_count INTEGER, max_conf REAL, status TEXT)""")
    db.commit()


# ---------- abuse protection ----------
hits = defaultdict(deque)

def limit(request: Request, bucket: str, per_minute: int):
    ip = request.headers.get("cf-connecting-ip") or request.client.host
    q = hits[(ip, bucket)]
    now = time.time()
    while q and now - q[0] > 60:
        q.popleft()
    if len(q) >= per_minute:
        raise HTTPException(429, "Too many requests. Wait a minute and try again.")
    q.append(now)


# ---------- detection ----------
def decode(data: bytes):
    img = cv2.imdecode(np.frombuffer(data, np.uint8), cv2.IMREAD_COLOR)
    if img is None:
        raise HTTPException(400, "Could not read that image.")
    return img


def letterbox(img):
    h, w = img.shape[:2]
    r = min(IMG_SIZE / h, IMG_SIZE / w)
    nw, nh = round(w * r), round(h * r)
    resized = cv2.resize(img, (nw, nh), interpolation=cv2.INTER_LINEAR)
    top, left = (IMG_SIZE - nh) // 2, (IMG_SIZE - nw) // 2
    canvas = np.full((IMG_SIZE, IMG_SIZE, 3), 114, dtype=np.uint8)
    canvas[top:top + nh, left:left + nw] = resized
    return canvas, r, left, top


def detect(img, conf):
    h, w = img.shape[:2]
    canvas, r, left, top = letterbox(img)
    blob = np.ascontiguousarray(
        canvas[:, :, ::-1].transpose(2, 0, 1)[None].astype(np.float32) / 255.0)
    out = session.run(None, {input_name: blob})[0][0].T      # (8400, 5) for 1 class
    if out.shape[1] != 5:
        raise HTTPException(500, "Unexpected model output shape.")
    scores = out[:, 4]
    keep = scores >= conf
    out, scores = out[keep], scores[keep]
    if len(out) == 0:
        return []
    cx, cy, bw, bh = out[:, 0], out[:, 1], out[:, 2], out[:, 3]
    x1 = np.clip((cx - bw / 2 - left) / r, 0, w)
    y1 = np.clip((cy - bh / 2 - top) / r, 0, h)
    x2 = np.clip((cx + bw / 2 - left) / r, 0, w)
    y2 = np.clip((cy + bh / 2 - top) / r, 0, h)
    xywh = np.stack([x1, y1, x2 - x1, y2 - y1], axis=1)
    idx = np.array(cv2.dnn.NMSBoxes(xywh.tolist(), scores.tolist(), float(conf), NMS_IOU)).flatten()
    return [{"box": [float(x1[i]), float(y1[i]), float(x2[i]), float(y2[i])],
             "conf": round(float(scores[i]), 3)} for i in idx]


def annotate(img, dets):
    vis = img.copy()
    t = max(2, round(max(img.shape[:2]) / 400))
    for d in dets:
        x1, y1, x2, y2 = (int(v) for v in d["box"])
        cv2.rectangle(vis, (x1, y1), (x2, y2), (41, 198, 255), t)          # BGR yellow
        cv2.putText(vis, f'{d["conf"]:.2f}', (x1, max(y1 - 6, 14)),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.3 * t + 0.3, (41, 198, 255), max(1, t - 1))
    return vis


def exif_location(data: bytes):
    """GPS position stored in the photo, or None."""
    try:
        gps = Image.open(BytesIO(data)).getexif().get_ifd(0x8825)
        if not gps:
            return None

        def dms(value, ref):
            d, m, s = (float(x) for x in value)
            deg = d + m / 60 + s / 3600
            return -deg if ref in ("S", "W") else deg

        lat, lon = dms(gps[2], gps[1]), dms(gps[4], gps[3])
        if abs(lat) > 90 or abs(lon) > 180 or (lat == 0 and lon == 0):
            return None
        return {"lat": round(lat, 6), "lon": round(lon, 6)}
    except Exception:
        return None


# ---------- short-lived cache so tickets can only come from real detections ----------
cache = {}
cache_lock = threading.Lock()

def cache_put(entry: dict) -> str:
    now = time.time()
    with cache_lock:
        for k in [k for k, v in cache.items() if now - v["t"] > CACHE_TTL]:
            del cache[k]
        while len(cache) >= CACHE_MAX:
            del cache[min(cache, key=lambda k: cache[k]["t"])]
        key = secrets.token_urlsafe(12)
        cache[key] = {**entry, "t": now}
        return key

def cache_get(key: str):
    with cache_lock:
        v = cache.get(key)
        return v if v and time.time() - v["t"] <= CACHE_TTL else None


# ---------- municipality + ticket helpers ----------
def haversine_m(lat1, lon1, lat2, lon2):
    p1, p2 = math.radians(lat1), math.radians(lat2)
    a = (math.sin((p2 - p1) / 2) ** 2
         + math.cos(p1) * math.cos(p2) * math.sin(math.radians(lon2 - lon1) / 2) ** 2)
    return 2 * 6371000 * math.asin(math.sqrt(a))


def reverse_geocode(lat, lon):
    """OpenStreetMap Nominatim: coordinates -> place name and address."""
    url = "https://nominatim.openstreetmap.org/reverse?" + urllib.parse.urlencode(
        {"format": "jsonv2", "lat": lat, "lon": lon, "zoom": 14, "addressdetails": 1})
    req = urllib.request.Request(url, headers={"User-Agent": "pothole-detector-learning-project/1.0"})
    try:
        with urllib.request.urlopen(req, timeout=8) as r:
            j = json.load(r)
    except Exception:
        return None
    a = j.get("address", {})
    place = next((a[k] for k in ("city", "town", "village", "municipality", "suburb", "county")
                  if a.get(k)), None)
    return {"place": place, "address": j.get("display_name")}


def resolve_municipality(place):
    if not place:
        return "Unassigned (location lookup failed)", None
    try:
        with open(MUNI_PATH, encoding="utf-8") as f:
            table = {k.lower(): v for k, v in json.load(f).items()}
    except Exception:
        table = {}
    entry = table.get(place.lower())
    if entry:
        return entry["name"], entry.get("email")
    return f"{place} Municipal Authority", None


def row_to_ticket(row, duplicate=False):
    (tid, created, lat, lon, place, address, muni, email, count, max_conf, status) = row
    return {"ticket_id": tid, "created_at": created, "lat": lat, "lon": lon,
            "place": place, "address": address, "municipality": muni,
            "pothole_count": count, "status": status, "duplicate": duplicate}


def find_recent_nearby(lat, lon):
    since = (datetime.now(timezone.utc) - timedelta(hours=DEDUPE_HOURS)).isoformat()
    with db_lock:
        rows = db.execute("SELECT * FROM tickets WHERE created_at >= ?", (since,)).fetchall()
    for row in rows:
        if haversine_m(lat, lon, row[2], row[3]) <= DEDUPE_METERS:
            return row
    return None


def write_letter(t, email, max_conf):
    lat, lon = t["lat"], t["lon"]
    text = f"""To: {email or '(no email configured)'}
Subject: Pothole report {t['ticket_id']} - {t['place'] or 'unknown location'}

To the {t['municipality']},

A road photo submitted through the Pothole Detector learning project shows
{t['pothole_count']} pothole(s) at:

  {t['address'] or 'Address unavailable'}
  Coordinates: {lat}, {lon}
  Map: https://www.openstreetmap.org/?mlat={lat}&mlon={lon}#map=18/{lat}/{lon}

Detected by an automated model (highest confidence {max_conf:.0%}).
Evidence photo: evidence/{t['ticket_id']}.jpg
Filed: {t['created_at']}

Please inspect and repair at your earliest convenience.

(Generated by a learning project. This message has NOT been sent.)
"""
    with open(os.path.join(OUTBOX_DIR, f"{t['ticket_id']}.txt"), "w", encoding="utf-8") as f:
        f.write(text)


# ---------- routes ----------
@app.get("/health")
def health():
    return {"status": "ok"}


@app.post("/predict")
def predict(request: Request, file: UploadFile = File(...), conf: float = Form(0.25)):
    limit(request, "predict", 20)
    data = file.file.read(MAX_BYTES + 1)
    if len(data) > MAX_BYTES:
        raise HTTPException(413, "Image is larger than 10 MB.")
    img = decode(data)
    conf = min(max(conf, 0.05), 0.95)

    dets = detect(img, conf)
    vis = annotate(img, dets)
    h, w = vis.shape[:2]
    s = 1280 / max(h, w)
    if s < 1:
        vis = cv2.resize(vis, (int(w * s), int(h * s)), interpolation=cv2.INTER_AREA)
    jpg = cv2.imencode(".jpg", vis, [cv2.IMWRITE_JPEG_QUALITY, 85])[1].tobytes()

    det_id = cache_put({"count": len(dets), "jpg": jpg,
                        "max_conf": max((d["conf"] for d in dets), default=0.0)})
    return {"detection_id": det_id, "count": len(dets), "detections": dets,
            "exif_location": exif_location(data),
            "image": "data:image/jpeg;base64," + base64.b64encode(jpg).decode()}


class TicketIn(BaseModel):
    detection_id: str
    lat: float = Field(ge=-90, le=90)
    lon: float = Field(ge=-180, le=180)


@app.post("/ticket")
def create_ticket(request: Request, body: TicketIn):
    limit(request, "ticket", 10)
    det = cache_get(body.detection_id)
    if not det:
        raise HTTPException(404, "This detection expired. Please upload the photo again.")
    if det["count"] < 1:
        raise HTTPException(400, "No potholes were detected in this photo.")

    existing = find_recent_nearby(body.lat, body.lon)
    if existing:
        return row_to_ticket(existing, duplicate=True)

    geo = reverse_geocode(body.lat, body.lon) or {}
    municipality, email = resolve_municipality(geo.get("place"))
    tid = f"PH-{datetime.now(timezone.utc):%Y%m%d}-{secrets.token_hex(3).upper()}"
    created = datetime.now(timezone.utc).isoformat(timespec="seconds")
    row = (tid, created, body.lat, body.lon, geo.get("place"), geo.get("address"),
           municipality, email, det["count"], det["max_conf"], "Filed")

    with open(os.path.join(EVIDENCE_DIR, f"{tid}.jpg"), "wb") as f:
        f.write(det["jpg"])
    with db_lock:
        db.execute("INSERT INTO tickets VALUES (?,?,?,?,?,?,?,?,?,?,?)", row)
        db.commit()
    ticket = row_to_ticket(row)
    write_letter(ticket, email, det["max_conf"])
    return ticket