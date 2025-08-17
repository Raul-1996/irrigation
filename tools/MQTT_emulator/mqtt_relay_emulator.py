from pathlib import Path
import sys

# Allow running from tool folder or repo root
HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[2] if (len(HERE.parents) >= 2) else HERE
if str(ROOT) not in sys.path:
    sys.path.append(str(ROOT))

import os
import base64
from collections import deque
from datetime import datetime
import threading
import time
from typing import Dict, List, Tuple

from flask import Flask, jsonify, request, Response
import paho.mqtt.client as mqtt

# ------------------------------
# Configuration
# ------------------------------

BROKER_HOST = os.getenv("TEST_MQTT_HOST", "127.0.0.1")
BROKER_PORT = int(os.getenv("TEST_MQTT_PORT", "1883"))
BROKER_USER = os.getenv("TEST_MQTT_USER") or None
BROKER_PASS = os.getenv("TEST_MQTT_PASS") or None

# Device IDs and channels: 5 relays mrc6cv3 with 6 channels each by default
DEVICE_IDS_ENV = os.getenv("EMULATOR_DEVICE_IDS", "101,102,103,104,105")
DEVICE_IDS = [x.strip() for x in DEVICE_IDS_ENV.split(",") if x.strip()]
NUM_CHANNELS = int(os.getenv("EMULATOR_CHANNELS", "6"))

# Optional artificial delay before the device publishes its confirmation (seconds)
ECHO_DELAY_SECONDS = float(os.getenv("EMULATOR_ECHO_DELAY_SECONDS", "0.05"))
# Optional toggle to disable echoing back device confirmations entirely ("1" to enable, "0" to disable)
ECHO_ENABLED = (os.getenv("EMULATOR_ECHO_ENABLED", "1") == "1")
# Optional suppression window for re-echoing per-topic (seconds). If another change arrives within this window, skip echo.
ECHO_SUPPRESS_WINDOW = float(os.getenv("EMULATOR_ECHO_SUPPRESS_WINDOW", "0.10"))

# HTTP server settings
HTTP_HOST = os.getenv("EMULATOR_HTTP_HOST", "0.0.0.0")
HTTP_PORT = int(os.getenv("EMULATOR_HTTP_PORT", "5055"))


def build_topics(device_ids: List[str], num_channels: int) -> List[str]:
    topics: List[str] = []
    for dev in device_ids:
        for ch in range(1, num_channels + 1):
            topics.append(f"/devices/wb-mr6cv3_{dev}/controls/K{ch}")
    return topics


ALL_TOPICS: List[str] = build_topics(DEVICE_IDS, NUM_CHANNELS)

# ------------------------------
# Shared State
# ------------------------------

state_lock = threading.Lock()
topic_to_state: Dict[str, str] = {t: "0" for t in ALL_TOPICS}
last_echo_ts: Dict[str, float] = {}
log_lock = threading.Lock()
log_buffer: deque[str] = deque(maxlen=1000)


def log_event(message: str) -> None:
    timestamp = datetime.now().strftime("%H:%M:%S")
    line = f"[{timestamp}] {message}"
    with log_lock:
        log_buffer.append(line)
    # still print to stdout for terminal debugging
    print(line)


def apply_local_state(topic: str, value: str) -> None:
    t = normalize_topic(topic)
    want = "1" if value == "1" else "0"
    with state_lock:
        previous = topic_to_state.get(t, "0")
        changed = (want != previous)
        topic_to_state[t] = want
    if changed:
        log_event(f"offline: applied state topic={t} payload={want}")
    else:
        log_event(f"offline: state unchanged topic={t} payload={want}")


def normalize_topic(t: str) -> str:
    return t if t.startswith("/") else "/" + t


def normalize_payload(raw: bytes) -> str:
    try:
        payload = raw.decode("utf-8", "ignore").strip()
    except Exception:
        payload = "0"
    return "1" if payload in ("1", "true", "on", "ON", "True", "YES", "yes") else "0"

# ------------------------------
# MQTT Clients (device + controller)
# ------------------------------

# Controller client publishes commands (triggered by UI), does not subscribe
controller_client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id=os.getenv("EMULATOR_CONTROLLER_CLIENT_ID", "emulator-controller"))

if BROKER_USER:
    controller_client.username_pw_set(BROKER_USER, BROKER_PASS)


def controller_connect() -> None:
    def _connect_loop():
        while True:
            try:
                controller_client.connect(BROKER_HOST, BROKER_PORT, 10)
                controller_client.loop_start()
                log_event(f"controller: connected to MQTT {BROKER_HOST}:{BROKER_PORT}")
                return
            except Exception as exc:
                log_event(f"controller: connect failed: {exc}; retry in 2s")
                time.sleep(2)
    threading.Thread(target=_connect_loop, daemon=True).start()


# Device client subscribes and echoes state back (simulates the relay device behavior)
device_client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id=os.getenv("EMULATOR_DEVICE_CLIENT_ID", "emulator-device"))

if BROKER_USER:
    device_client.username_pw_set(BROKER_USER, BROKER_PASS)


def device_on_connect(client: mqtt.Client, userdata, flags, reason_code, properties=None):
    # Subscribe to all topics; allow receiving our own echoes to detect changes once, but avoid infinite loops
    # We'll prevent re-echoing the same value by comparing with the previous state.
    log_event(f"device: connected to MQTT (rc={reason_code}) — subscribing to topics")
    for t in ALL_TOPICS:
        # Using MQTT v5 SubscribeOptions if available
        try:
            # Avoid receiving our own published echoes to further reduce feedback
            options = mqtt.SubscribeOptions(qos=0, noLocal=True)
            client.subscribe(t, options=options)
        except Exception:
            client.subscribe(t, qos=0)
    log_event(f"device: subscribed to {len(ALL_TOPICS)} topics")


def device_on_message(client: mqtt.Client, userdata, msg: mqtt.MQTTMessage):
    topic = normalize_topic(msg.topic)
    want = normalize_payload(msg.payload)
    try:
        raw = msg.payload.decode('utf-8','ignore')
    except Exception:
        raw = str(msg.payload)
    log_event(f"device: RX topic={topic} payload={raw!r} -> want={want}")

    with state_lock:
        previous = topic_to_state.get(topic, "0")
        changed = (want != previous)
        topic_to_state[topic] = want

    # Simulate device switching delay and publish confirmation only if state changed
    if changed and ECHO_ENABLED:
        # Suppress too-frequent echoes on the same topic
        now = time.time()
        with state_lock:
            last_ts = last_echo_ts.get(topic, 0.0)
            if (now - last_ts) < ECHO_SUPPRESS_WINDOW:
                log_event(f"device: SUPPRESS echo topic={topic} payload={want}")
                return
            last_echo_ts[topic] = now
        if ECHO_DELAY_SECONDS > 0:
            time.sleep(ECHO_DELAY_SECONDS)
        log_event(f"device: TX echo topic={topic} payload={want}")
        client.publish(topic, payload=want, qos=0, retain=False)


def device_connect() -> None:
    device_client.on_connect = device_on_connect
    device_client.on_message = device_on_message
    def _connect_loop():
        while True:
            try:
                device_client.connect(BROKER_HOST, BROKER_PORT, 10)
                device_client.loop_start()
                log_event(f"device: connected to MQTT {BROKER_HOST}:{BROKER_PORT}")
                return
            except Exception as exc:
                log_event(f"device: connect failed: {exc}; retry in 2s")
                time.sleep(2)
    threading.Thread(target=_connect_loop, daemon=True).start()


def publish_command(topic: str, value: str) -> None:
    t = normalize_topic(topic)
    v = "1" if value == "1" else "0"
    log_event(f"controller: CMD publish topic={t} payload={v}")
    info = None
    try:
        info = controller_client.publish(t, payload=v, qos=0, retain=False)
    except Exception as exc:
        log_event(f"controller: publish error: {exc}")
    # Fallback: if either client isn't connected, or publish failed, apply state locally
    try:
        dev_connected = bool(getattr(device_client, "is_connected")() if hasattr(device_client, "is_connected") else False)
    except Exception:
        dev_connected = False
    try:
        ctrl_connected = bool(getattr(controller_client, "is_connected")() if hasattr(controller_client, "is_connected") else False)
    except Exception:
        ctrl_connected = False
    publish_failed = (info is None) or (getattr(info, 'rc', 0) != 0)
    if (not dev_connected) or (not ctrl_connected) or publish_failed:
        apply_local_state(t, v)

# ------------------------------
# Flask app
# ------------------------------

app = Flask(__name__)


@app.after_request
def add_no_cache_headers(resp: Response):
    try:
        resp.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
        resp.headers["Pragma"] = "no-cache"
        resp.headers["Expires"] = "0"
    except Exception:
        pass
    return resp


def build_structure() -> List[Tuple[str, List[Tuple[str, str]]]]:
    # Returns list of (device_id, [ (topic, state), ...channels ]) preserving order
    structure: List[Tuple[str, List[Tuple[str, str]]]] = []
    with state_lock:
        for dev in DEVICE_IDS:
            channels: List[Tuple[str, str]] = []
            for ch in range(1, NUM_CHANNELS + 1):
                topic = f"/devices/wb-mr6cv3_{dev}/controls/K{ch}"
                channels.append((topic, topic_to_state.get(topic, "0")))
            structure.append((dev, channels))
    return structure


@app.get("/")
def index():
    cards_html = "\n".join([
        (
            "  <div class=\"card\">\n"
            f"    <div class=\"device-title\">wb-mr6cv3_{dev}</div>\n"
            "    <table>\n      <thead><tr><th>Канал</th><th>Статус</th><th>Топик</th><th>Действие</th></tr></thead>\n      <tbody>\n"
            + "\n".join([
                (
                    "        <tr>\n          <td>K" + str(ch) + "</td>\n          <td><span class=\"lamp off\" id=\"lamp-"
                    + base64.b64encode(f"/devices/wb-mr6cv3_{dev}/controls/K{ch}".encode()).decode()
                    + "\"></span> <span id=\"status-"
                    + base64.b64encode(f"/devices/wb-mr6cv3_{dev}/controls/K{ch}".encode()).decode()
                    + "\">0</span></td>\n          <td class=\"topic\">/devices/wb-mr6cv3_" + dev + "/controls/K" + str(ch) + "</td>\n          <td class=\"controls\">\n            <button class=\"btn\" data-topic=\"/devices/wb-mr6cv3_"
                    + dev + "/controls/K" + str(ch) + "\" data-action=\"on\">Вкл (1)</button>\n            <button class=\"btn\" data-topic=\"/devices/wb-mr6cv3_"
                    + dev + "/controls/K" + str(ch) + "\" data-action=\"off\">Выкл (0)</button>\n          </td>\n        </tr>"
                )
                for ch in range(1, NUM_CHANNELS + 1)
            ])
            + "\n      </tbody>\n    </table>\n  </div>"
        )
        for dev in DEVICE_IDS
    ])

    page = """
<!DOCTYPE html>
<html lang=\"ru\">
<head>
  <meta charset=\"utf-8\" />
  <meta name=\"viewport\" content=\"width=device-width, initial-scale=1\" />
  <title>MQTT Эмулятор wb-mr6cv3 (101–105 x K1..K6)</title>
  <style>
    body { font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, 'Helvetica Neue', Arial, 'Noto Sans', 'Apple Color Emoji', 'Segoe UI Emoji'; margin: 16px; }
    h1 { font-size: 20px; margin: 0 0 12px; }
    /* Stack all device cards vertically to avoid overlap */
    .grid { display: flex; flex-direction: column; gap: 12px; }
    .card { border: 1px solid #ddd; border-radius: 8px; padding: 12px; width: 100%; max-width: 980px; margin: 0 auto; background: #fff; }
    .device-title { font-weight: 600; margin-bottom: 8px; }
    table { border-collapse: collapse; width: 100%; table-layout: fixed; }
    th, td { border-bottom: 1px solid #eee; padding: 6px 4px; text-align: left; vertical-align: middle; }
    th:nth-child(1){ width: 60px; }
    th:nth-child(2){ width: 70px; }
    th:nth-child(4){ width: 170px; }
    .row { display: flex; align-items: center; justify-content: flex-start; gap: 8px; }
    .lamp { width: 14px; height: 14px; border-radius: 50%; display: inline-block; border: 1px solid rgba(0,0,0,0.2); vertical-align: middle; margin-right: 8px; }
    .on { background: #14a44d; box-shadow: 0 0 6px rgba(20,164,77,0.6); }
    .off { background: #dc3545; box-shadow: 0 0 6px rgba(220,53,69,0.5); }
    .topic { font-family: ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, 'Liberation Mono', 'Courier New', monospace; font-size: 12px; color: #333; overflow-wrap: anywhere; }
    .btn { padding: 4px 8px; font-size: 12px; border: 1px solid #ccc; border-radius: 6px; background: #f8f8f8; cursor: pointer; }
    .btn:hover { background: #f0f0f0; }
    .controls { display: flex; gap: 6px; flex-wrap: wrap; }
    .global-controls { display: flex; gap: 8px; margin: 8px 0 14px; }
    .btn.primary { background: #e8f5e9; border-color: #b7e1c1; }
    .btn.danger { background: #fdecea; border-color: #f5c2c7; }
    .legend { margin: 10px 0 16px; font-size: 13px; color: #555; }
    .muted { color: #777; font-size: 12px; }
    .logs { max-width: 980px; margin: 16px auto; border: 1px solid #ddd; border-radius: 8px; background: #fafafa; }
    .logs-header { padding: 8px 12px; border-bottom: 1px solid #eee; font-weight: 600; display: flex; align-items: center; justify-content: space-between; }
    .logs-body { padding: 8px 12px; height: 240px; overflow: auto; font-family: ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, 'Liberation Mono', 'Courier New', monospace; font-size: 12px; line-height: 1.4; white-space: pre-wrap; }
    .logs-actions { display: flex; gap: 8px; }
  </style>
  <script>
    async function apiState() {
      const res = await fetch('/api/state');
      return await res.json();
    }
    async function toggle(topic, value) {
      await fetch('/api/toggle', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ topic, value })
      });
    }
    async function toggleAll(value) {
      await fetch('/api/toggle-all', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ value })
      });
    }
    function applyState(data) {
      for (const [topic, val] of Object.entries(data.states || {})) {
        const id = btoa(topic);
        const lamp = document.getElementById('lamp-' + id);
        const status = document.getElementById('status-' + id);
        if (!lamp || !status) continue;
        if (String(val) === '1') {
          lamp.classList.add('on'); lamp.classList.remove('off'); status.textContent = '1';
        } else {
          lamp.classList.add('off'); lamp.classList.remove('on'); status.textContent = '0';
        }
      }
    }
    async function refresh() {
      try { const data = await apiState(); applyState(data); } catch (e) {}
    }
    function setup() {
      refresh();
      setInterval(refresh, 1000);
      document.body.addEventListener('click', function(e) {
        const el = e.target.closest('[data-topic][data-action]');
        if (!el) return;
        const topic = el.getAttribute('data-topic');
        const action = el.getAttribute('data-action');
        const value = action === 'on' ? '1' : '0';
        toggle(topic, value).then(() => setTimeout(refresh, 150));
      });
      // Global controls
      const allOn = document.getElementById('all-on');
      const allOff = document.getElementById('all-off');
      if (allOn) allOn.addEventListener('click', ()=> { toggleAll('1').then(()=> setTimeout(refresh, 150)); });
      if (allOff) allOff.addEventListener('click', ()=> { toggleAll('0').then(()=> setTimeout(refresh, 150)); });

      // Logs polling
      const poll = () => fetch('/api/logs').then(r=>r.json()).then(data=>{
        const box = document.getElementById('logs-box');
        if (box) {
          const safe = (data.logs || []).map(l => l.replace(/&/g,'&amp;').replace(/</g,'&lt;'));
          box.innerHTML = safe.join('<br/>');
          box.scrollTop = box.scrollHeight;
        }
      }).catch(()=>{});
      poll(); setInterval(poll, 1000);
      const clearBtn = document.getElementById('logs-clear');
      if (clearBtn) clearBtn.addEventListener('click', async function(){ await fetch('/api/logs/clear', {method:'POST'}); setTimeout(poll, 100); });
    }
    document.addEventListener('DOMContentLoaded', setup);
  </script>
</head>
<body>
  <h1>MQTT Эмулятор wb-mr6cv3</h1>
  <div class=\"legend\">Пять устройств: <strong>101–105</strong>, по шесть каналов <strong>K1–K6</strong>. Зелёная лампа — «включено» (1), красная — «выключено» (0).<div class=\"muted\">Брокер: \""" + BROKER_HOST + ":" + str(BROKER_PORT) + "\"</div></div>
  <div class=\"global-controls\">
    <button class=\"btn primary\" id=\"all-on\">Включить все (1)</button>
    <button class=\"btn danger\" id=\"all-off\">Выключить все (0)</button>
  </div>
  <div class=\"grid\">""" + cards_html + """\n  </div>
  <div class=\"logs\">
    <div class=\"logs-header\">Логи MQTT
      <div class=\"logs-actions\"><button class=\"btn\" id=\"logs-clear\">Очистить</button></div>
    </div>
    <div class=\"logs-body\" id=\"logs-box\"></div>
  </div>
</body>
</html>
"""
    resp = Response(page, mimetype="text/html")
    resp.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
    return resp


@app.get("/api/state")
def api_state():
    with state_lock:
        return jsonify({
            "states": {t: topic_to_state.get(t, "0") for t in ALL_TOPICS}
        })


@app.get("/api/logs")
def api_logs():
    with log_lock:
        return jsonify({"logs": list(log_buffer)})


@app.post("/api/logs/clear")
def api_logs_clear():
    with log_lock:
        log_buffer.clear()
    return jsonify({"ok": True})


@app.post("/api/toggle")
def api_toggle():
    data = request.get_json(silent=True) or {}
    topic = data.get("topic")
    value = data.get("value")
    if not topic or value not in ("0", "1"):
        return jsonify({"ok": False, "error": "Invalid topic or value"}), 400
    publish_command(topic, value)
    return jsonify({"ok": True})


@app.post("/api/toggle-all")
def api_toggle_all():
    data = request.get_json(silent=True) or {}
    value = data.get("value")
    if value not in ("0", "1"):
        return jsonify({"ok": False, "error": "Invalid value"}), 400
    for t in ALL_TOPICS:
        publish_command(t, value)
    return jsonify({"ok": True, "count": len(ALL_TOPICS)})


def start_mqtt_clients():
    controller_connect()
    device_connect()


def main():
    start_mqtt_clients()
    app.run(host=HTTP_HOST, port=HTTP_PORT, debug=False, use_reloader=False)


if __name__ == "__main__":
    main()


