# SSE Hub Hardening Spec

## Problem
Dead SSE clients (phone sleep, network change) accumulate in `_SSE_HUB_CLIENTS`.
Hypercorn event loop spins on socket.send() errors → service unavailable.

## Solution

### 1. `services/sse_hub.py`
- **MAX_SSE_CLIENTS = 20** — evict oldest when limit reached (send None sentinel)
- **Queue maxsize 10000 → 100** — smaller buffer, faster dead detection
- **broadcast()** — remove clients with full queues (dead detection)
- **Cleaner thread** — logs client count every 60s (daemon, started once)

### 2. `routes/zones_watering_api.py`
- **Generator timeout** — 30 min max, then yield reconnect event and break
- **Sentinel handling** — None in queue → break generator loop
- **Queue poll timeout** — 0.5s → 1.0s (less CPU)

### 3. `static/js/status.js`
- **Client-side reconnect** — close + reconnect every 25 min
- **Handle `reconnect` event** — server-initiated reconnect

## Safety
- No changes outside SSE flow
- Backward-compatible SSE protocol (new events are additive)
- Tests cover: client limit, dead detection, sentinel, timeout
