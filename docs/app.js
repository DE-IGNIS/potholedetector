// ---------- where is the model server? ----------
// run.py prints a link like  https://your-site/?api=https://xxxx.trycloudflare.com
// Only localhost and Cloudflare tunnel addresses are accepted, so a crafted link
// can't make the page upload photos to some other server.
const DEFAULT_API = "http://localhost:8000";
const SAFE_API = /^(https:\/\/[a-z0-9-]+\.trycloudflare\.com|http:\/\/(localhost|127\.0\.0\.1):\d+)$/i;

function resolveApi() {
  let fromUrl = null, saved = null;
  try { fromUrl = new URLSearchParams(location.search).get("api"); } catch {}
  try { saved = localStorage.getItem("pothole_api"); } catch {}
  const candidate = (fromUrl || saved || DEFAULT_API).replace(/\/+$/, "");
  if (!SAFE_API.test(candidate)) return DEFAULT_API;
  if (fromUrl) {
    try { localStorage.setItem("pothole_api", candidate); } catch {}
    history.replaceState(null, "", location.pathname);
  }
  return candidate;
}
const API = resolveApi();

// ---------- helpers ----------
const $ = (id) => document.getElementById(id);
const MAX_BYTES = 10 * 1024 * 1024;

let file = null;
let previewUrl = null;
let detection = null;     // response from /predict
let coords = null;        // { lat, lon, source }

function setServer(state, text) {
  $("server-pill").dataset.state = state;
  $("server-text").textContent = text;
}

function setStep(n) {
  document.querySelectorAll(".steps li").forEach((li) => {
    const s = Number(li.dataset.step);
    li.dataset.state = s < n ? "done" : s === n ? "active" : "";
  });
}

function setStatus(msg, isError = false) {
  const el = $("status");
  el.textContent = msg;
  el.classList.toggle("error", isError);
}

async function api(path, options) {
  let res;
  try {
    res = await fetch(API + path, options);
  } catch {
    setServer("offline", "Model server offline");
    throw new Error("Can't reach the model server. Start it with “python run.py” and open the link it prints.");
  }
  const body = await res.json().catch(() => ({}));
  if (!res.ok) {
    throw new Error(typeof body.detail === "string" ? body.detail : "Please check the details and try again.");
  }
  return body;
}

async function checkServer() {
  setServer("checking", "Checking server…");
  try {
    const res = await fetch(API + "/health", { cache: "no-store" });
    if (!res.ok) throw new Error();
    setServer("online", "Model server online");
  } catch {
    setServer("offline", "Model server offline");
  }
}

// ---------- 1. choose a photo ----------
function chooseFile(f) {
  if (!f) return;
  if (!f.type.startsWith("image/")) return setStatus("Please choose an image file.", true);
  if (f.size > MAX_BYTES) return setStatus("That photo is larger than 10 MB.", true);

  resetResults();
  file = f;
  if (previewUrl) URL.revokeObjectURL(previewUrl);
  previewUrl = URL.createObjectURL(f);
  $("preview").src = previewUrl;
  $("fname").textContent = f.name;
  $("drop-empty").hidden = true;
  $("drop-preview").hidden = false;
  $("actions").hidden = false;
  setStatus("");
  setStep(1);
}

function clearFile() {
  file = null;
  $("file").value = "";
  $("drop-empty").hidden = false;
  $("drop-preview").hidden = true;
  $("actions").hidden = true;
  setStatus("");
  resetResults();
  setStep(1);
}

function resetResults() {
  detection = null;
  coords = null;
  $("result").hidden = true;
  $("ticket").hidden = true;
}

$("file").addEventListener("change", (e) => chooseFile(e.target.files[0]));
$("clear").addEventListener("click", clearFile);
$("again").addEventListener("click", () => { clearFile(); window.scrollTo({ top: 0, behavior: "smooth" }); });

const drop = $("drop");
["dragenter", "dragover"].forEach((ev) =>
  drop.addEventListener(ev, (e) => { e.preventDefault(); drop.classList.add("over"); }));
["dragleave", "drop"].forEach((ev) =>
  drop.addEventListener(ev, (e) => { e.preventDefault(); drop.classList.remove("over"); }));
drop.addEventListener("drop", (e) => chooseFile(e.dataTransfer.files[0]));

// ---------- 2. detect ----------
$("detect").addEventListener("click", async () => {
  if (!file) return;
  $("detect").disabled = true;
  resetResults();
  setStatus("Detecting potholes…");
  try {
    const form = new FormData();
    form.append("file", file);
    detection = await api("/predict", { method: "POST", body: form });
    setServer("online", "Model server online");
    coords = detection.exif_location ? { ...detection.exif_location, source: "from the photo" } : null;
    showResult();
    setStatus("");
  } catch (err) {
    setStatus(err.message, true);
  } finally {
    $("detect").disabled = false;
  }
});

function showResult() {
  const n = detection.count;
  $("result-img").src = detection.image;
  $("count").textContent = n;
  $("count-label").textContent = n === 1 ? "pothole detected" : "potholes detected";
  $("none-msg").hidden = n > 0;
  $("locate").hidden = n === 0;
  $("result").hidden = false;
  if (n > 0) renderLocation();
  setStep(2);
  $("result").scrollIntoView({ behavior: "smooth", block: "start" });
}

// ---------- location ----------
function renderLocation() {
  const text = $("loc-text");
  text.textContent = "";
  if (coords) {
    const strong = document.createElement("strong");
    strong.textContent = `${coords.lat.toFixed(5)}, ${coords.lon.toFixed(5)}`;
    text.append("Location ", strong, ` (${coords.source}). Not right? Set another below.`);
  } else {
    text.textContent = "This photo has no location data. Share your current location or paste coordinates (for example, copied from a map).";
  }
  $("file-ticket").disabled = !coords;
}

$("use-loc").addEventListener("click", () => {
  if (!navigator.geolocation) return setStatus("This browser can't share a location. Paste coordinates instead.", true);
  setStatus("Getting your location…");
  navigator.geolocation.getCurrentPosition(
    (pos) => {
      coords = { lat: pos.coords.latitude, lon: pos.coords.longitude, source: "from your device" };
      renderLocation();
      setStatus("");
    },
    () => setStatus("Couldn't get your location. Allow location access, or paste coordinates instead.", true),
    { enableHighAccuracy: true, timeout: 10000 }
  );
});

$("manual").addEventListener("submit", (e) => {
  e.preventDefault();
  const m = $("manual-input").value.match(/^\s*(-?\d+(?:\.\d+)?)\s*[, ]\s*(-?\d+(?:\.\d+)?)\s*$/);
  const lat = m && Number(m[1]), lon = m && Number(m[2]);
  if (!m || Math.abs(lat) > 90 || Math.abs(lon) > 180) {
    return setStatus("Enter coordinates like 22.7196, 75.8577.", true);
  }
  coords = { lat, lon, source: "entered manually" };
  renderLocation();
  setStatus("");
});

// ---------- 3. file the ticket ----------
$("file-ticket").addEventListener("click", async () => {
  if (!detection || !coords) return;
  const btn = $("file-ticket");
  btn.disabled = true;
  setStatus("Filing your ticket…");
  try {
    const ticket = await api("/ticket", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ detection_id: detection.detection_id, lat: coords.lat, lon: coords.lon }),
    });
    showTicket(ticket);
    setStatus("");
  } catch (err) {
    setStatus(err.message, true);
    btn.disabled = false;
  }
});

function showTicket(t) {
  // textContent only: address and place names come from a third-party lookup
  $("t-kicker").textContent = t.duplicate ? "Already reported" : "Ticket filed";
  $("t-id").textContent = t.ticket_id;
  $("t-msg").textContent = t.duplicate
    ? "This spot was already reported in the last 24 hours, so no new ticket was needed."
    : "Success! Your report has been filed with the municipality for this location.";
  $("t-muni").textContent = t.municipality;
  $("t-loc").textContent = t.address || `${t.lat.toFixed(5)}, ${t.lon.toFixed(5)}`;
  $("t-count").textContent = String(t.pothole_count);
  $("t-time").textContent = new Date(t.created_at).toLocaleString();
  $("ticket").hidden = false;
  setStep(3);
  $("ticket").scrollIntoView({ behavior: "smooth", block: "start" });
}

// ---------- start ----------
checkServer();
setInterval(checkServer, 20000);