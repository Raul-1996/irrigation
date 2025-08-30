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
import random
from typing import Dict, List, Tuple, Set

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
MSW_AUTO_ENABLED = (os.getenv("EMULATOR_MSW_AUTO_ENABLED", "1") == "1")
# Autopublish interval for MSW sensors (min 30s)
MSW_AUTO_INTERVAL_SECONDS = float(os.getenv("EMULATOR_MSW_AUTO_INTERVAL", "30.0"))


def build_relay_topics(device_ids: List[str], num_channels: int) -> List[str]:
    topics: List[str] = []
    for dev in device_ids:
        for ch in range(1, num_channels + 1):
            topics.append(f"/devices/wb-mr6cv3_{dev}/controls/K{ch}")
    return topics


# Inputs for wb-mr6cv3_101: Input 0..6 and Input N counter
INPUT_DEVICE_ID = os.getenv("EMULATOR_INPUT_DEVICE_ID", "101")
INPUT_RANGE_ENV = os.getenv("EMULATOR_INPUT_RANGE", "0-6")  # inclusive
try:
    _start, _end = INPUT_RANGE_ENV.split("-", 1)
    INPUT_INDEX_START = int(_start)
    INPUT_INDEX_END = int(_end)
except Exception:
    INPUT_INDEX_START, INPUT_INDEX_END = 0, 6


def build_input_topics(device_id: str, start_idx: int, end_idx: int) -> Tuple[List[str], List[str]]:
    switch_topics: List[str] = []
    counter_topics: List[str] = []
    for i in range(start_idx, end_idx + 1):
        switch_topics.append(f"/devices/wb-mr6cv3_{device_id}/controls/Input {i}")
        counter_topics.append(f"/devices/wb-mr6cv3_{device_id}/controls/Input {i} counter")
    return switch_topics, counter_topics


RELAY_TOPICS: List[str] = build_relay_topics(DEVICE_IDS, NUM_CHANNELS)
INPUT_SWITCH_TOPICS, INPUT_COUNTER_TOPICS = build_input_topics(INPUT_DEVICE_ID, INPUT_INDEX_START, INPUT_INDEX_END)

# wb-msw-v4_107 sensors with editable values in UI
MSW_DEVICE_ID = os.getenv("EMULATOR_MSW_DEVICE_ID", "107")
MSW_CONTROL_NAMES: List[str] = [
    "Temperature",
    "Humidity",
    "CO2",
    "Air Quality (VOC)",
    "Sound Level",
    "Illuminance",
    "Max Motion",
    "Current Motion",
]
MSW_TOPICS: List[str] = [f"/devices/wb-msw-v4_{MSW_DEVICE_ID}/controls/{name}" for name in MSW_CONTROL_NAMES]
MSW_TEMPERATURE_TOPIC = f"/devices/wb-msw-v4_{MSW_DEVICE_ID}/controls/Temperature"
MSW_HUMIDITY_TOPIC = f"/devices/wb-msw-v4_{MSW_DEVICE_ID}/controls/Humidity"

ALL_TOPICS: List[str] = RELAY_TOPICS + INPUT_SWITCH_TOPICS + INPUT_COUNTER_TOPICS + MSW_TOPICS
RELAY_TOPICS_SET: Set[str] = set(RELAY_TOPICS)
INPUT_SWITCH_TOPICS_SET: Set[str] = set(INPUT_SWITCH_TOPICS)
INPUT_COUNTER_TOPICS_SET: Set[str] = set(INPUT_COUNTER_TOPICS)
MSW_TOPICS_SET: Set[str] = set(MSW_TOPICS)

# ------------------------------
# Shared State
# ------------------------------

state_lock = threading.Lock()
# Initialize relays as "0", input switches as "false", counters as "0"
topic_to_state: Dict[str, str] = {}
for t in RELAY_TOPICS:
    topic_to_state[t] = "0"
for t in INPUT_SWITCH_TOPICS:
    topic_to_state[t] = "false"
for t in INPUT_COUNTER_TOPICS:
    topic_to_state[t] = "0"
for t, default in zip(
    MSW_TOPICS,
    [
        "22.09",  # Temperature
        "42.63",  # Humidity
        "508",    # CO2
        "725",    # Air Quality (VOC)
        "42.49",  # Sound Level
        "215.57", # Illuminance
        "20",     # Max Motion
        "15",     # Current Motion
    ],
):
    topic_to_state[t] = default
last_echo_ts: Dict[str, float] = {}
log_lock = threading.Lock()
log_buffer: deque[str] = deque(maxlen=1000)
msw_temp_direction: int = 1
msw_hum_direction: int = 1


def log_event(message: str) -> None:
    timestamp = datetime.now().strftime("%H:%M:%S")
    line = f"[{timestamp}] {message}"
    with log_lock:
        log_buffer.append(line)
    # still print to stdout for terminal debugging
    print(line)


def apply_local_state_raw(topic: str, value: str) -> None:
    t = normalize_topic(topic)
    want = value
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


def normalize_bool_10(raw: str) -> str:
    return "1" if raw in ("1", "true", "on", "ON", "True", "YES", "yes") else "0"


def normalize_bool_true_false(raw: str) -> str:
    return "true" if raw in ("1", "true", "on", "ON", "True", "YES", "yes") else "false"

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
    try:
        raw = msg.payload.decode('utf-8', 'ignore').strip()
    except Exception:
        raw = str(msg.payload)

    # Determine desired state based on topic type
    if topic in RELAY_TOPICS_SET:
        want = normalize_bool_10(raw)  # '1'/'0'
    elif topic in INPUT_SWITCH_TOPICS_SET:
        want = normalize_bool_true_false(raw)  # 'true'/'false'
    elif topic in INPUT_COUNTER_TOPICS_SET:
        try:
            # Allow integer values only; fall back to previous if invalid
            want = str(int(raw))
        except Exception:
            with state_lock:
                want = topic_to_state.get(topic, "0")
    elif topic in MSW_TOPICS_SET:
        # Accept any raw numeric/string; keep as-is
        want = raw
    else:
        # Unknown topic: store raw
        want = raw

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
    raw = (value or "").strip()

    # Normalize value per topic kind
    if t in RELAY_TOPICS_SET:
        v = normalize_bool_10(raw)
    elif t in INPUT_SWITCH_TOPICS_SET:
        v = normalize_bool_true_false(raw)
    elif t in INPUT_COUNTER_TOPICS_SET:
        try:
            v = str(int(raw))
        except Exception:
            with state_lock:
                v = topic_to_state.get(t, "0")
    elif t in MSW_TOPICS_SET:
        # Keep raw user-entered value
        v = raw
    else:
        v = raw

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
        apply_local_state_raw(t, v)

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


def build_structure() -> Tuple[List[Tuple[str, List[Tuple[str, str]]]], List[Tuple[str, str]], List[Tuple[str, str]]]:
    # Returns:
    #  - list of (device_id, [ (topic, state) ]) for relay channels
    #  - list of (topic, state) for input switches (wb-mr6cv3_101)
    #  - list of (topic, state) for input counters (wb-mr6cv3_101)
    relay_structure: List[Tuple[str, List[Tuple[str, str]]]] = []
    switch_rows: List[Tuple[str, str]] = []
    counter_rows: List[Tuple[str, str]] = []
    with state_lock:
        for dev in DEVICE_IDS:
            channels: List[Tuple[str, str]] = []
            for ch in range(1, NUM_CHANNELS + 1):
                topic = f"/devices/wb-mr6cv3_{dev}/controls/K{ch}"
                channels.append((topic, topic_to_state.get(topic, "0")))
            relay_structure.append((dev, channels))

        for t in INPUT_SWITCH_TOPICS:
            switch_rows.append((t, topic_to_state.get(t, "false")))
        for t in INPUT_COUNTER_TOPICS:
            counter_rows.append((t, topic_to_state.get(t, "0")))
    return relay_structure, switch_rows, counter_rows


@app.get("/")
def index():
    relay_structure, input_switch_rows, input_counter_rows = build_structure()

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
        for dev, _channels in relay_structure
    ])

    # Inputs (only wb-mr6cv3_101 by default)
    inputs_html = (
        "  <div class=\"card\">\n"
        f"    <div class=\"device-title\">wb-mr6cv3_{INPUT_DEVICE_ID} — Inputs</div>\n"
        "    <table>\n      <thead><tr><th>Контрол</th><th>Статус</th><th>Топик</th><th>Действие</th></tr></thead>\n      <tbody>\n"
        + "\n".join([
            (
                "        <tr>\n          <td>" + t.split("/")[-1] + "</td>\n          <td><span class=\"lamp off\" id=\"lamp-"
                + base64.b64encode(t.encode()).decode()
                + "\"></span> <span id=\"status-"
                + base64.b64encode(t.encode()).decode()
                + "\">false</span></td>\n          <td class=\"topic\">" + t + "</td>\n          <td class=\"controls\">\n            <button class=\"btn\" data-topic=\"" + t + "\" data-action=\"true\">true</button>\n            <button class=\"btn\" data-topic=\"" + t + "\" data-action=\"false\">false</button>\n          </td>\n        </tr>"
            ) for (t, _s) in input_switch_rows
        ])
        + "\n".join([
            (
                "        <tr>\n          <td>" + t.split("/")[-1] + "</td>\n          <td><span id=\"status-"
                + base64.b64encode(t.encode()).decode()
                + "\">0</span></td>\n          <td class=\"topic\">" + t + "</td>\n          <td class=\"controls\">\n            <button class=\"btn\" data-counter-topic=\"" + t + "\">+1</button>\n          </td>\n        </tr>"
            ) for (t, _s) in input_counter_rows
        ])
        + "\n      </tbody>\n    </table>\n  </div>"
    )

    # Build MSW sensors table
    msw_rows = []
    for t in MSW_TOPICS:
        control_name = t.split("/")[-1]
        tid = base64.b64encode(t.encode()).decode()
        msw_rows.append(
            "        <tr>\n          <td>" + control_name + "</td>\n          <td><span id=\"status-" + tid + "\">" + topic_to_state.get(t, "") + "</span></td>\n          <td class=\"topic\">" + t + "</td>\n          <td class=\"controls\">\n            <input type=\"text\" class=\"in\" data-input-for=\"" + tid + "\" style=\"width:100px\" />\n            <button class=\"btn\" data-input-topic=\"" + t + "\">Установить</button>\n          </td>\n        </tr>"
        )
    msw_html = (
        "  <div class=\"card\">\n"
        f"    <div class=\"device-title\">wb-msw-v4_{MSW_DEVICE_ID} — Sensors</div>\n"
        "    <table>\n      <thead><tr><th>Контрол</th><th>Значение</th><th>Топик</th><th>Действие</th></tr></thead>\n      <tbody>\n"
        + "\n".join(msw_rows)
        + "\n      </tbody>\n    </table>\n  </div>"
    )

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
    /* Wider columns to prevent overlapping in sensors table */
    th:nth-child(1){ width: 160px; }
    th:nth-child(2){ width: 110px; }
    th:nth-child(4){ width: 220px; }
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
    async function counterIncr(topic) {
      await fetch('/api/counter-incr', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ topic })
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
        if (!status) continue;
        const sval = String(val);
        const isOn = (sval === '1') || (sval.toLowerCase && sval.toLowerCase() === 'true');
        if (lamp) {
          if (isOn) { lamp.classList.add('on'); lamp.classList.remove('off'); }
          else { lamp.classList.add('off'); lamp.classList.remove('on'); }
        }
        status.textContent = sval;
        const inEl = document.querySelector('[data-input-for="' + id + '"]');
        if (inEl) {
          // Do not override user's typing or pending manual edit
          if (document.activeElement !== inEl && !inEl.dataset.pending) {
            inEl.value = sval;
          }
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
        const btn = e.target.closest('[data-topic][data-action]');
        if (btn) {
          const topic = btn.getAttribute('data-topic');
          const action = btn.getAttribute('data-action');
          // action can be 'on'/'off' for K-channels or 'true'/'false' for inputs
          const value = (action === 'on') ? '1' : (action === 'off') ? '0' : action;
          toggle(topic, value).then(() => setTimeout(refresh, 150));
          return;
        }
        const cnt = e.target.closest('[data-counter-topic]');
        if (cnt) {
          const topic = cnt.getAttribute('data-counter-topic');
          counterIncr(topic).then(() => setTimeout(refresh, 150));
          return;
        }
        const setBtn = e.target.closest('[data-input-topic]');
        if (setBtn) {
          const topic = setBtn.getAttribute('data-input-topic');
          const input = setBtn.parentElement.querySelector('input');
          const value = input ? input.value : '';
          toggle(topic, value).then(() => { if (input) { delete input.dataset.pending; } setTimeout(refresh, 150); });
          return;
        }
      });
      // Mark fields as pending when user types to prevent auto-override
      document.body.addEventListener('input', function(e){
        const inp = e.target.closest('input[data-input-for]');
        if (inp) { inp.dataset.pending = '1'; }
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
  <div class=\"grid\">""" + cards_html + "\n" + inputs_html + "\n" + msw_html + """\n  </div>
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
    if not topic or value is None:
        return jsonify({"ok": False, "error": "Invalid topic or value"}), 400
    publish_command(topic, value)
    return jsonify({"ok": True})


@app.post("/api/counter-incr")
def api_counter_incr():
    data = request.get_json(silent=True) or {}
    topic = data.get("topic")
    if not topic:
        return jsonify({"ok": False, "error": "Invalid topic"}), 400
    t = normalize_topic(topic)
    if t not in INPUT_COUNTER_TOPICS_SET:
        return jsonify({"ok": False, "error": "Not a counter topic"}), 400
    with state_lock:
        try:
            cur = int(topic_to_state.get(t, "0"))
        except Exception:
            cur = 0
        new_val = cur + 1
        topic_to_state[t] = str(new_val)
    publish_command(t, str(new_val))
    return jsonify({"ok": True, "value": new_val})


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


def _bounded_oscillate(cur: float, direction: int, min_v: float, max_v: float) -> Tuple[float, int]:
    # step is 1.x where x in [0,9]
    frac = random.randint(0, 9) / 10.0
    step = 1.0 + frac
    nxt = cur + (step * (1 if direction >= 0 else -1))
    new_dir = direction
    if nxt > max_v:
        nxt = max_v
        new_dir = -1
    elif nxt < min_v:
        nxt = min_v
        new_dir = 1
    return nxt, new_dir


def start_msw_autopublish_thread():
    if not MSW_AUTO_ENABLED:
        return
    def _loop():
        global msw_temp_direction, msw_hum_direction
        # Initialize from current state
        with state_lock:
            try:
                cur_t = float(topic_to_state.get(MSW_TEMPERATURE_TOPIC, "22.0"))
            except Exception:
                cur_t = 22.0
            try:
                cur_h = float(topic_to_state.get(MSW_HUMIDITY_TOPIC, "42.0"))
            except Exception:
                cur_h = 42.0
        while True:
            try:
                # Compute next values
                cur_t, msw_temp_direction = _bounded_oscillate(cur_t, msw_temp_direction, 10.0, 40.0)
                cur_h, msw_hum_direction = _bounded_oscillate(cur_h, msw_hum_direction, 10.0, 40.0)

                # Format with one or two decimals
                t_str = f"{cur_t:.2f}"
                h_str = f"{cur_h:.2f}"

                # Publish directly from the device client to avoid duplicate echo messages
                try:
                    device_client.publish(MSW_TEMPERATURE_TOPIC, payload=t_str, qos=0, retain=False)
                    apply_local_state_raw(MSW_TEMPERATURE_TOPIC, t_str)
                except Exception as exc:
                    log_event(f"msw-auto: publish temp error {exc}")
                try:
                    device_client.publish(MSW_HUMIDITY_TOPIC, payload=h_str, qos=0, retain=False)
                    apply_local_state_raw(MSW_HUMIDITY_TOPIC, h_str)
                except Exception as exc:
                    log_event(f"msw-auto: publish hum error {exc}")
            except Exception as exc:
                log_event(f"msw-auto: error {exc}")
            # Enforce a minimum interval of 30 seconds to reduce message noise
            time.sleep(max(30.0, MSW_AUTO_INTERVAL_SECONDS))
    threading.Thread(target=_loop, daemon=True).start()


def main():
    start_mqtt_clients()
    start_msw_autopublish_thread()
    app.run(host=HTTP_HOST, port=HTTP_PORT, debug=False, use_reloader=False)


if __name__ == "__main__":
    main()


