# Stage B Deploy Report — WB-8 Controller (.244)

**Date:** 2026-03-29  
**Version:** v2.0.0 (refactor/v2)  
**Target:** WB-8 controller `10.2.5.244` (hostname: wirenboard-AHQBDF7N)

## Pre-deployment State

- **Service:** `wb-irrigation.service` (systemd, enabled)
- **Process:** `/opt/wb-irrigation/irrigation/venv/bin/python run.py`
- **Working dir:** `/opt/wb-irrigation/irrigation`
- **Python:** 3.9.2 (venv)
- **Old version:** monolithic `app.py` (208KB), `database.py` (122KB), `irrigation_scheduler.py` (56KB)

## Steps Completed

### 1. Reconnaissance ✅
- Service: `wb-irrigation.service` — loaded, active, running
- ExecStart: `/opt/wb-irrigation/irrigation/venv/bin/python run.py`
- Environment: `TESTING=0`, `UI_THEME=auto`

### 2. Backup ✅
- Backup created: `/root/irrigation-backup-20260329.tar.gz` (44MB)
- Note: `tar` warning "file changed as we read it" (active DB) — non-critical

### 3. Stop Old Version ✅
- `systemctl stop wb-irrigation`
- Process confirmed stopped (no irrigation processes in `ps aux`)

### 4. Rename Old Version ✅
- `mv /opt/wb-irrigation/irrigation → /opt/wb-irrigation/irrigation-old`

### 5. Archive New Version ✅
- Branch: `refactor/v2` (up to date)
- Archive: `/tmp/wb-irrigation-v2.tar.gz` (21MB)
- Excluded: `.git`, `__pycache__`, `*.pyc`, `irrigation.db*`, `backups/*`, `mosquitto_data`, `mosquitto_log`, `reports`, `specs`, `tools/tests`, `.secret_key`, `.irrig_secret_key`

### 6. Transfer Archive ✅
- Local → Docker host (172.30.0.1) via scp
- Docker host → Controller (.244) via sshpass+scp

### 7. Unpack ✅
- `mkdir -p /opt/wb-irrigation/irrigation`
- `tar xzf /tmp/wb-irrigation-v2.tar.gz`
- V2 structure confirmed: modular `app.py` (18KB), `routes/`, `services/`, `db/`, `constants.py`, `VERSION`

### 8. Copy DB ✅
- `cp /opt/wb-irrigation/irrigation-old/irrigation.db → irrigation/`
- Size: 167936 bytes

### 9. MQTT ✅
- `wb-mqtt-serial` — active (running), port 1883
- No custom mosquitto needed (WB-8 has built-in broker)

### 10. Dependencies ✅
- Copied old `venv/` from `irrigation-old` (compatible with systemd unit path)
- Installed updates via pip:
  - Flask 2.3.3 → 3.1.3
  - Pillow 10.0.1 → 11.3.0
  - aiogram 3.4.1 → 3.22.0
- `requests>=2.33.0` incompatible with Python 3.9; used existing `requests==2.32.3` (sufficient)
- All other deps (APScheduler 3.10.4, paho-mqtt 2.1.0, hypercorn 0.14.4, Flask-WTF 1.2.1, Flask-Sock 0.7.0, pycryptodome 3.21.0) already satisfied

### 11. Start New Version ✅
- **Bug found:** `run.py` used undefined `logger` variable — crash on startup
- **Fix:** Added `import logging` and `logger = logging.getLogger(__name__)` to `run.py`
- `systemctl restart wb-irrigation`
- Service running: PID 556736, Memory ~111MB

### 12. Verification ✅
- **HTTP:** `curl http://localhost:8080/` → **200 OK** (after ~20s startup)
- **API zones:** `GET /api/zones` → JSON array with zones (Зона 1, group "Насос-1", topic `/devices/wb-mr6cv3_85/controls/K1`, state=off)
- **DB migration:** Automatic — added fields `last_fault`, `fault_count` to zones table
- **VERSION:** `2.0.0`

## Known Issues (non-blocking)

1. **Telegram token not configured:**
   - `[ERROR] TelegramNotifier: no encrypted token in DB` — needs configuration via web UI settings
   
2. **Circular import warning:**
   - `ImportError: cannot import name '_start_single_zone_watchdog' from partially initialized module 'app'`
   - Single-zone watchdog didn't start, but the main `ZoneWatchdog` service started successfully
   - Likely needs code fix in `services/app_init.py` import order

3. **Python 3.9 compatibility:**
   - `requests>=2.33.0` requires Python 3.10+; using `requests==2.32.3` which works
   - `requirements.txt` should be updated for WB-8 compatibility

## Rollback Plan

If issues arise:
```bash
systemctl stop wb-irrigation
rm -rf /opt/wb-irrigation/irrigation
mv /opt/wb-irrigation/irrigation-old /opt/wb-irrigation/irrigation
systemctl start wb-irrigation
```

Backup also available: `/root/irrigation-backup-20260329.tar.gz`
