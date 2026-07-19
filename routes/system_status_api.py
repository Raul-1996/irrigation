"""System Status API — status, health, scheduler, logs, water, server-time."""

import logging
import sqlite3
import threading
import time
from collections import OrderedDict
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from flask import Blueprint, current_app, jsonify, request, session

from config import TESTING
from database import db
from irrigation_scheduler import get_scheduler
from services import sse_hub as _sse_hub
from services.audit import audit_log
from services.helpers import api_error, parse_dt
from services.locks import snapshot_all_locks as _locks_snapshot
from services.monitors import env_monitor, rain_monitor, water_monitor
from services.next_watering import compute_next_watering, weather_skip_today
from services.security import admin_required
from utils import SecretDecryptionError

logger = logging.getLogger(__name__)

system_status_api_bp = Blueprint("system_status_api", __name__)


# ``/api/status`` is polled continuously. Broker health therefore comes only
# from already-running MQTT clients; opening a diagnostic connection here
# would pin a WSGI worker for every unreachable broker. A short, bounded cache
# bridges harmless runtime-map rebuild races without turning a stale success
# into permanent health.
MQTT_RUNTIME_HEALTH_CACHE_TTL_SEC = 30.0
MQTT_RUNTIME_HEALTH_CACHE_MAX = 64
_MQTT_RUNTIME_HEALTH_CACHE: "OrderedDict[tuple[Any, ...], tuple[bool, float]]" = OrderedDict()
_MQTT_RUNTIME_HEALTH_CACHE_LOCK = threading.Lock()


def _reset_runtime_mqtt_health_cache() -> None:
    """Clear process-local health state (also used for deterministic tests)."""
    with _MQTT_RUNTIME_HEALTH_CACHE_LOCK:
        _MQTT_RUNTIME_HEALTH_CACHE.clear()


def _server_health_cache_key(server: dict[str, Any]) -> tuple[Any, ...]:
    """Tie cached state to this DB instance and immutable connection identity."""
    try:
        port = int(server.get("port") or 1883)
    except (TypeError, ValueError):
        port = 1883
    return (
        id(db),
        int(server.get("id") or 0),
        str(server.get("host") or ""),
        port,
        str(server.get("username") or ""),
        str(server.get("client_id") or ""),
        int(server.get("tls_enabled") or 0),
        str(server.get("tls_ca_path") or ""),
        str(server.get("tls_cert_path") or ""),
        str(server.get("tls_key_path") or ""),
        int(server.get("tls_insecure") or 0),
        str(server.get("tls_version") or ""),
    )


def _copy_runtime_clients(lock: Any, clients: Any, server_ids: set[int]) -> dict[int, Any]:
    """Take a non-blocking snapshot of one live-client registry."""
    acquired = False
    try:
        acquired = bool(lock.acquire(blocking=False))
        if not acquired:
            return {}
        return {int(sid): client for sid, client in dict(clients).items() if int(sid) in server_ids}
    except (AttributeError, RuntimeError, TypeError, ValueError):
        return {}
    finally:
        if acquired:
            try:
                lock.release()
            except (AttributeError, RuntimeError):
                pass


def _snapshot_runtime_mqtt_health(server_ids: set[int]) -> dict[int, bool]:
    """Return non-blocking health observations from permanent MQTT clients.

    Paho's ``is_connected()`` becomes true only after an accepted CONNACK.
    Merely having a client object, a socket, or a successful ``connect()``
    return code is deliberately not treated as broker health.
    """
    if not server_ids:
        return {}
    candidates: dict[int, list[Any]] = {}
    try:
        hub_clients = _copy_runtime_clients(
            _sse_hub._SSE_HUB_LOCK,
            _sse_hub._SSE_HUB_MQTT,
            server_ids,
        )
    except AttributeError:
        hub_clients = {}
    try:
        from services import mqtt_pub

        publisher_clients = _copy_runtime_clients(
            mqtt_pub._MQTT_CLIENTS_LOCK,
            mqtt_pub._MQTT_CLIENTS,
            server_ids,
        )
    except (AttributeError, ImportError):
        publisher_clients = {}
    for source in (hub_clients, publisher_clients):
        for sid, client in source.items():
            candidates.setdefault(sid, []).append(client)

    observed: dict[int, bool] = {}
    for sid, clients in candidates.items():
        states: list[bool] = []
        for client in clients:
            try:
                is_connected = getattr(client, "is_connected", None)
                if callable(is_connected):
                    states.append(bool(is_connected()))
            except (AttributeError, RuntimeError, TypeError, ValueError):
                continue
        if states:
            observed[sid] = any(states)
    return observed


def _prune_runtime_mqtt_health_cache(now: float) -> None:
    for key, (_connected, observed_at) in list(_MQTT_RUNTIME_HEALTH_CACHE.items()):
        if now - observed_at > MQTT_RUNTIME_HEALTH_CACHE_TTL_SEC:
            _MQTT_RUNTIME_HEALTH_CACHE.pop(key, None)
    while len(_MQTT_RUNTIME_HEALTH_CACHE) > MQTT_RUNTIME_HEALTH_CACHE_MAX:
        _MQTT_RUNTIME_HEALTH_CACHE.popitem(last=False)


def _runtime_mqtt_health(servers: list[dict[str, Any]]) -> dict[str, Any]:
    """Aggregate live health without network or disk I/O in the request."""
    if not servers:
        return {
            "status": "not_configured",
            "source": "runtime_cache",
            "connected": False,
            "known_servers": 0,
        }

    server_ids = {int(server.get("id") or 0) for server in servers}
    observed = _snapshot_runtime_mqtt_health(server_ids)
    now = time.monotonic()
    states: dict[int, bool] = {}
    with _MQTT_RUNTIME_HEALTH_CACHE_LOCK:
        _prune_runtime_mqtt_health_cache(now)
        for server in servers:
            sid = int(server.get("id") or 0)
            key = _server_health_cache_key(server)
            if sid in observed:
                _MQTT_RUNTIME_HEALTH_CACHE[key] = (bool(observed[sid]), now)
                _MQTT_RUNTIME_HEALTH_CACHE.move_to_end(key)
            cached = _MQTT_RUNTIME_HEALTH_CACHE.get(key)
            if cached is not None:
                states[sid] = bool(cached[0])
        _prune_runtime_mqtt_health_cache(now)

    connected = any(states.values())
    if connected:
        status = "healthy"
    elif states:
        status = "degraded"
    else:
        status = "unknown"
    return {
        "status": status,
        "source": "runtime_cache",
        "connected": connected,
        "known_servers": len(states),
    }


def _last_known_runtime_mqtt_health() -> dict[str, Any]:
    """Preserve a recent signal when encrypted configuration is unavailable."""
    now = time.monotonic()
    with _MQTT_RUNTIME_HEALTH_CACHE_LOCK:
        _prune_runtime_mqtt_health_cache(now)
        states = [
            connected
            for key, (connected, _observed_at) in _MQTT_RUNTIME_HEALTH_CACHE.items()
            if key and key[0] == id(db)
        ]
    return {
        "status": "degraded",
        "source": "runtime_cache",
        "connected": any(states),
        "known_servers": len(states),
        "error_code": "MQTT_SECRET_UNAVAILABLE",
    }


def _database_is_healthy() -> bool:
    """Validate the existing application DB without creating a new empty file."""
    db_path = getattr(db, "db_path", None)
    if not db_path or str(db_path) == ":memory:":
        return False
    try:
        uri = Path(str(db_path)).resolve().as_uri() + "?mode=ro"
        with sqlite3.connect(uri, uri=True, timeout=0.25) as conn:
            row = conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'zones'"
            ).fetchone()
        return row is not None
    except (OSError, sqlite3.Error, ValueError):
        return False


# ===== Health / Scheduler =====


@system_status_api_bp.route("/api/health-details")
@admin_required
def api_health_details():
    try:
        sched = get_scheduler()
        jobs = []
        if sched is not None and getattr(sched, "scheduler", None) is not None:
            try:
                for j in sched.scheduler.get_jobs():
                    try:
                        nrt = getattr(j, "next_run_time", None)
                        jid = str(j.id)
                        jstore = "default" if jid.startswith("program:") else "volatile"
                        trig = str(getattr(j, "trigger", ""))
                        jobs.append(
                            {
                                "id": jid,
                                "name": str(getattr(j, "name", "")),
                                "next_run_time": nrt.isoformat() if nrt else None,
                                "jobstore": jstore,
                                "trigger": trig,
                            }
                        )
                    except (ValueError, TypeError, KeyError) as e:
                        logger.debug("Exception in api_health_details: %s", e)
                        continue
            except (ValueError, TypeError, KeyError) as e:
                logger.debug("Handled exception in api_health_details: %s", e)
        zones = []
        try:
            for z in db.get_zones():
                try:
                    state = str(z.get("state") or "")
                    cstate = str(z.get("commanded_state") or "")
                    if state != "off" or cstate in ("starting", "on", "stopping"):
                        zones.append(
                            {
                                "id": int(z.get("id")),
                                "group_id": int(z.get("group_id") or 0),
                                "state": state,
                                "commanded_state": cstate,
                                "observed_state": str(z.get("observed_state") or ""),
                                "sequence_id": z.get("sequence_id"),
                                "command_id": z.get("command_id"),
                                "version": z.get("version"),
                                "planned_end_time": z.get("planned_end_time"),
                            }
                        )
                except (ValueError, TypeError, KeyError) as e:
                    logger.debug("Exception in line_84: %s", e)
                    continue
        except (sqlite3.Error, OSError) as e:
            logger.debug("Handled exception in line_87: %s", e)
        locks = _locks_snapshot()
        group_cancels = []
        try:
            if hasattr(sched, "group_cancel_events"):
                for gid, ev in (sched.group_cancel_events or {}).items():
                    try:
                        group_cancels.append({"group_id": int(gid), "set": bool(ev.is_set())})
                    except (ValueError, TypeError, KeyError) as e:
                        logger.debug("Exception in line_96: %s", e)
                        continue
        except (ValueError, TypeError, KeyError) as e:
            logger.debug("Handled exception in line_99: %s", e)
        try:
            meta_tail = _sse_hub.get_meta_buffer()
        except (OSError, ValueError, RuntimeError) as e:
            logger.debug("Exception in line_103: %s", e)
            meta_tail = []
        payload = {
            "now": datetime.now().isoformat(timespec="seconds"),
            "scheduler_running": bool(sched and sched.is_running),
            "jobs": jobs,
            "zones": zones,
            "locks": locks,
            "group_cancels": group_cancels,
            "meta_tail": meta_tail,
        }
        return jsonify(payload)
    except (sqlite3.Error, OSError) as e:
        logger.exception("health-details failed")
        return api_error("health_details_failed", f"health details error: {e}", 500)


@system_status_api_bp.route("/api/health/job/<path:job_id>/cancel", methods=["POST"])
@admin_required
@audit_log("scheduler_job_cancel", target_extractor=lambda *a, **kw: f"job:{kw.get('job_id', a[0] if a else '?')}")
def api_health_cancel_job(job_id):
    try:
        sched = get_scheduler()
        if not sched or not getattr(sched, "scheduler", None):
            return api_error("scheduler_unavailable", "scheduler unavailable", 503)
        try:
            sched.scheduler.remove_job(str(job_id))
            return jsonify({"success": True, "message": f"job {job_id} removed"})
        except (ValueError, TypeError, KeyError) as e:
            logger.debug("Exception in api_health_cancel_job: %s", e)
            return api_error("job_remove_failed", f"failed to remove job: {e}", 400)
    except (ValueError, TypeError, KeyError) as e:
        logger.exception("cancel job failed")
        return api_error("cancel_job_failed", f"error: {e}", 500)


@system_status_api_bp.route("/api/health/group/<int:group_id>/cancel", methods=["POST"])
@admin_required
@audit_log(
    "scheduler_group_cancel", target_extractor=lambda *a, **kw: f"group:{kw.get('group_id', a[0] if a else '?')}"
)
def api_health_cancel_group(group_id):
    try:
        sched = get_scheduler()
        if not sched:
            return api_error("scheduler_unavailable", "scheduler unavailable", 503)
        try:
            if hasattr(sched, "cancel_group_jobs"):
                sched.cancel_group_jobs(int(group_id))
        except (ValueError, TypeError, RuntimeError):
            logger.exception("group cancel failed")
            return api_error("group_cancel_failed", "failed to cancel group jobs", 400)
        return jsonify({"success": True, "message": f"group {group_id} cancelled"})
    except (ValueError, TypeError, KeyError) as e:
        logger.exception("cancel group failed")
        return api_error("cancel_group_failed", f"error: {e}", 500)


@system_status_api_bp.route("/api/scheduler/init", methods=["POST"])
@audit_log("scheduler_init", target_extractor=lambda *a, **kw: "scheduler")
def api_scheduler_init():
    """Explicit scheduler init for UI/tests."""
    try:
        from irrigation_scheduler import init_scheduler

        init_scheduler(db)
        return jsonify({"success": True})
    except (ValueError, KeyError, RuntimeError) as e:
        logger.error(f"Ошибка явной инициализации планировщика: {e}")
        return api_error("INTERNAL_ERROR", "internal error", 500)


@system_status_api_bp.route("/api/scheduler/status")
@admin_required
def api_scheduler_status():
    """Get scheduler status.

    In TESTING mode the APScheduler may not be initialised (the test fixtures
    use an in-memory DB and don't spin up the real scheduler).  Returning 500
    in that case forces every test that hits an unrelated endpoint to either
    bootstrap APScheduler or assert ``status_code in (200, 500)``.  Instead
    we degrade gracefully: HTTP 200 with ``running=false`` +
    ``reason=scheduler_not_initialized`` so callers still see structured JSON.

    In production a missing scheduler is an actual incident — keep the 500.
    """
    try:
        scheduler = get_scheduler()
        if not scheduler:
            if TESTING:
                return jsonify(
                    {
                        "running": False,
                        "is_running": False,
                        "active_programs": [],
                        "active_zones": {},
                        "reason": "scheduler_not_initialized",
                    }
                ), 200
            return jsonify({"error": "Планировщик не инициализирован"}), 500
        active_programs = scheduler.get_active_programs()
        active_zones = scheduler.get_active_zones()
        return jsonify(
            {
                "active_programs": active_programs,
                "active_zones": {str(k): v.isoformat() for k, v in active_zones.items()},
                "is_running": scheduler.is_running,
            }
        )
    except (ValueError, TypeError, KeyError) as e:
        logger.error(f"Ошибка получения статуса планировщика: {e}")
        return jsonify({"error": "Ошибка получения статуса"}), 500


@system_status_api_bp.route("/api/scheduler/jobs")
@admin_required
def api_scheduler_jobs():
    try:
        sched = get_scheduler()
        if not sched:
            return jsonify({"success": False, "message": "scheduler not running", "jobs": []}), 200
        jobs = []
        for j in sched.scheduler.get_jobs():
            try:
                jobs.append(
                    {
                        "id": j.id,
                        "next_run_time": None
                        if j.next_run_time is None
                        else j.next_run_time.strftime("%Y-%m-%d %H:%M:%S"),
                        "name": getattr(j, "name", ""),
                    }
                )
            except (ValueError, TypeError, KeyError) as e:
                logger.debug("Exception in api_scheduler_jobs: %s", e)
                continue
        return jsonify({"success": True, "jobs": jobs})
    except (ValueError, TypeError, KeyError) as e:
        logger.error(f"scheduler jobs list failed: {e}")
        return jsonify({"success": False, "jobs": []}), 200


# ===== Health check =====


@system_status_api_bp.route("/health")
def health_check():
    try:
        db_ok = _database_is_healthy()
        try:
            sched = get_scheduler()
            sched_ok = bool(sched is not None)
        except (ValueError, KeyError, RuntimeError) as e:
            logger.debug("Exception in health_check: %s", e)
            sched_ok = False
        mqtt_configured = False
        try:
            servers = db.get_mqtt_servers() or []
            mqtt_configured = bool(servers)
            enabled_servers = [server for server in servers if int(server.get("enabled") or 0) == 1]
            if servers and not enabled_servers:
                mqtt_health = {
                    "status": "disabled",
                    "source": "runtime_cache",
                    "connected": False,
                    "known_servers": 0,
                }
            else:
                mqtt_health = _runtime_mqtt_health(enabled_servers)
        except SecretDecryptionError:
            mqtt_health = _last_known_runtime_mqtt_health()
        except (ConnectionError, TimeoutError, OSError, sqlite3.Error, TypeError, ValueError):
            mqtt_health = {
                "status": "degraded",
                "source": "runtime_cache",
                "connected": False,
                "known_servers": 0,
                "error_code": "MQTT_CONFIG_UNAVAILABLE",
            }
        mqtt_ready = mqtt_health.get("status") in {"healthy", "not_configured", "disabled"}
        payload = {
            "ok": db_ok and (sched_ok or TESTING) and mqtt_ready,
            "db": db_ok,
            "scheduler": sched_ok if sched_ok else ("not_initialized" if TESTING else False),
            "mqtt_configured": mqtt_configured,
            "mqtt_health": mqtt_health,
        }
        # In TESTING mode the scheduler is optional — most tests run without
        # bootstrapping APScheduler.  Treat its absence as a soft "not_initialized"
        # signal (HTTP 200) instead of failing health-check.  In production a
        # missing scheduler is a real incident — keep the 503.
        if TESTING and not sched_ok:
            return jsonify(payload), 200 if payload["ok"] else 503
        overall = db_ok and sched_ok and mqtt_ready
        code = 200 if overall else 503
        payload["ok"] = overall
        return jsonify(payload), code
    except (ConnectionError, TimeoutError, OSError, sqlite3.Error) as e:
        logger.exception("health check failed")
        return jsonify({"ok": False, "error_code": "HEALTH_CHECK_FAILED"}), 500


# ===== Server time =====


@system_status_api_bp.route("/api/server-time")
def api_server_time():
    try:
        now = datetime.now()
        try:
            tzname = time.tzname[0] if time.tzname else ""
        except (KeyError, TypeError, ValueError) as e:
            logger.debug("Exception in api_server_time: %s", e)
            tzname = ""
        payload = {"now_iso": now.strftime("%Y-%m-%d %H:%M:%S"), "epoch_ms": int(time.time() * 1000), "tz": tzname}
        resp = jsonify(payload)
        resp.headers["Cache-Control"] = "no-store"
        return resp
    except (ValueError, TypeError, KeyError) as e:
        logger.error(f"server-time failed: {e}")
        return jsonify({"now_iso": None, "epoch_ms": int(time.time() * 1000)}), 200


# ===== Status (big endpoint) =====


@system_status_api_bp.route("/api/status")
def api_status():
    rain_cfg = db.get_rain_config()
    zones = db.get_zones()
    groups = db.get_groups()
    programs = db.get_programs()
    weather_skip = weather_skip_today()

    # Compute the request-wide snapshot once and slice it per group below.
    # Besides avoiding repeated schedule work, this lets next_watering's
    # request-local cancellation cache deduplicate (program, date, group)
    # lookups across every zone rendered by this status response.
    watering_zone_ids = [int(z["id"]) for z in zones if int(z.get("group_id") or 0) != 999]
    next_watering_by_zone = compute_next_watering(
        watering_zone_ids,
        all_zones=zones,
        programs=programs,
        skip_today=weather_skip,
    )

    zones_by_group = {}
    for zone in zones:
        group_id = zone["group_id"]
        if group_id == 999:
            continue
        if group_id not in zones_by_group:
            zones_by_group[group_id] = []
        zones_by_group[group_id].append(zone)

    groups_status = []
    for group in groups:
        group_id = group["id"]
        if group_id == 999:
            continue
        group_zones = zones_by_group.get(group_id, [])
        if not group_zones:
            continue

        active_zones = [z for z in group_zones if z["state"] == "on"]
        postponed_zones = []
        for z in group_zones:
            pu = z.get("postpone_until")
            if not pu:
                continue
            # Canonical postpone values are written as YYYY-MM-DD HH:MM:SS
            # (system_config_api uses '%Y-%m-%d 23:59:59'); the previous
            # strptime('%Y-%m-%d %H:%M') silently failed and short-circuited
            # the "is postponed" check.  parse_dt accepts both formats.
            pu_dt = parse_dt(pu)
            if pu_dt is None:
                # Unparseable — treat as postponed (defensive: matches the
                # prior except-branch behaviour).
                postponed_zones.append(z)
            elif pu_dt > datetime.now():
                postponed_zones.append(z)

        if current_app.config.get("EMERGENCY_STOP"):
            status = "postponed"
            current_zone = None
        elif active_zones:
            status = "watering"
            current_zone = active_zones[0]["id"]
        elif postponed_zones:
            status = "postponed"
            current_zone = None
        else:
            status = "waiting"
            current_zone = None

        next_start = None
        if group_zones:
            group_zone_ids = {int(z["id"]) for z in group_zones}
            candidates = [
                entry["next_dt"]
                for zone_id, entry in next_watering_by_zone.items()
                if zone_id in group_zone_ids and entry.get("next_dt")
            ]
            if candidates:
                next_start = min(candidates).strftime("%H:%M")

        postpone_until = None
        group_postpone_reason = None
        if current_app.config.get("EMERGENCY_STOP"):
            postpone_until = "До отмены аварийной остановки"
            group_postpone_reason = "emergency"
        elif postponed_zones:
            postpone_until = postponed_zones[0].get("postpone_until")
            try:
                reasons = [z.get("postpone_reason") for z in postponed_zones if z.get("postpone_reason")]
                if "manual" in reasons:
                    group_postpone_reason = "manual"
                elif reasons:
                    group_postpone_reason = reasons[0]
            except (KeyError, TypeError, ValueError) as e:
                logger.debug("Handled exception in line_754: %s", e)

        current_zone_source = None
        try:
            if status == "watering" and current_zone:
                cz = next((z for z in group_zones if int(z["id"]) == int(current_zone)), None)
                if cz:
                    src = (cz.get("watering_start_source") or "").strip().lower()
                    if src in ("manual", "schedule", "remote"):
                        current_zone_source = src
                    else:
                        current_zone_source = "remote"
        except (ValueError, TypeError, KeyError) as e:
            logger.debug("Handled exception in line_767: %s", e)

        try:
            use_master_valve = bool(int(group.get("use_master_valve") or 0))
        except (ValueError, TypeError, KeyError) as e:
            logger.debug("Exception in line_772: %s", e)
            use_master_valve = False
        try:
            mvo = (group.get("master_valve_observed") or "").strip()
            master_valve_state = mvo if mvo in ("open", "closed") else "unknown"
        except (ValueError, TypeError, KeyError) as e:
            logger.debug("Exception in line_778: %s", e)
            master_valve_state = "unknown"
        try:
            use_pressure_sensor = bool(int(group.get("use_pressure_sensor") or 0))
        except (ValueError, TypeError, KeyError) as e:
            logger.debug("Exception in line_783: %s", e)
            use_pressure_sensor = False
        try:
            use_water_meter = bool(int(group.get("use_water_meter") or 0))
        except (ValueError, TypeError, KeyError) as e:
            logger.debug("Exception in line_788: %s", e)
            use_water_meter = False
        pressure_unit = (group.get("pressure_unit") or "bar") if use_pressure_sensor else None
        pressure_value = None
        meter_value_m3 = None
        flow_value = None
        if use_water_meter:
            try:
                meter_value_m3 = water_monitor.get_current_reading_m3(int(group_id))
                start_iso = None
                if status == "watering" and current_zone:
                    try:
                        cz = next((z for z in group_zones if int(z["id"]) == int(current_zone)), None)
                        start_iso = cz.get("watering_start_time") if cz else None
                    except (ValueError, TypeError, KeyError) as e:
                        logger.debug("Exception in line_803: %s", e)
                        start_iso = None
                flow_value = water_monitor.get_flow_lpm(int(group_id), start_iso)
            except (ValueError, TypeError, KeyError) as e:
                logger.debug("Exception in line_807: %s", e)
                meter_value_m3 = None
                flow_value = None

        # Queue remaining: how many zones in this group still queued AFTER the
        # currently active zone. Drives the Skip button visibility (issue #14).
        queue_remaining = 0
        try:
            if status == "watering" and current_zone:
                cur_start = None
                for z in group_zones:
                    if int(z["id"]) == int(current_zone):
                        cur_start = z.get("scheduled_start_time")
                        break
                if cur_start:
                    queue_remaining = sum(
                        1
                        for z in group_zones
                        if z.get("scheduled_start_time")
                        and z["scheduled_start_time"] > cur_start
                        and int(z["id"]) != int(current_zone)
                    )
        except (sqlite3.Error, OSError, ValueError, TypeError, KeyError) as e:
            logger.debug("queue_remaining calc failed for group %s: %s", group_id, e)
            queue_remaining = 0

        groups_status.append(
            {
                "id": group_id,
                "name": group["name"],
                "status": status,
                "current_zone": current_zone,
                "postpone_until": postpone_until,
                "next_start": next_start,
                "postpone_reason": group_postpone_reason,
                "was_postponed": bool(postponed_zones),
                "current_zone_source": current_zone_source,
                "use_master_valve": use_master_valve,
                "master_valve_state": master_valve_state,
                "use_pressure_sensor": use_pressure_sensor,
                "pressure_value": pressure_value,
                "pressure_unit": pressure_unit,
                "use_water_meter": use_water_meter,
                "flow_value": flow_value,
                "meter_value_m3": meter_value_m3,
                "queue_remaining": queue_remaining,
            }
        )

    if not rain_cfg.get("enabled"):
        rain_sensor_status = "выключен"
    else:
        try:
            if hasattr(rain_monitor, "is_rain") and rain_monitor.is_rain is not None:
                rain_sensor_status = "идёт дождь" if rain_monitor.is_rain else "дождя нет"
            else:
                rain_sensor_status = "дождя нет"
        except (ValueError, TypeError, RuntimeError) as e:
            logger.debug("Exception in line_831: %s", e)
            rain_sensor_status = "дождя нет"

    env_cfg = db.get_env_config()
    temp_enabled = bool(env_cfg.get("temp", {}).get("enabled"))
    hum_enabled = bool(env_cfg.get("hum", {}).get("enabled"))
    temperature = (
        None if not temp_enabled else (env_monitor.temp_value if env_monitor.temp_value is not None else "нет данных")
    )
    humidity = (
        None if not hum_enabled else (env_monitor.hum_value if env_monitor.hum_value is not None else "нет данных")
    )

    mqtt_secret_unavailable = False
    try:
        servers = db.get_mqtt_servers() or []
    except SecretDecryptionError:
        # Keep the rest of the public status usable and preserve a recent live
        # signal, but expose the recovery problem as structured degraded state.
        servers = []
        mqtt_secret_unavailable = True
    except (ConnectionError, TimeoutError, OSError, sqlite3.Error, TypeError, ValueError):
        servers = []
    mqtt_servers_count = len(servers)
    enabled_servers = [s for s in servers if int(s.get("enabled") or 0) == 1]
    mqtt_enabled_count = len(enabled_servers)
    if mqtt_secret_unavailable:
        mqtt_health = _last_known_runtime_mqtt_health()
    elif mqtt_servers_count and not mqtt_enabled_count:
        mqtt_health = {
            "status": "disabled",
            "source": "runtime_cache",
            "connected": False,
            "known_servers": 0,
        }
    else:
        mqtt_health = _runtime_mqtt_health(enabled_servers)
    mqtt_connected = bool(mqtt_health.get("connected"))
    try:
        role = session.get("role")
        is_admin = role == "admin"
    except (KeyError, TypeError, ValueError) as e:
        logger.debug("Exception in line_893: %s", e)
        is_admin = False

    # Feature A: aggregate system health so the UI can show a prominent alert.
    # A zone in state='fault' means its relay did not confirm switching — the
    # zone is excluded from the schedule and is NOT being watered.
    # TODO: master-valve-stuck faults are not aggregated yet.
    faults = []
    try:
        for z in zones:
            if str(z.get("state") or "").lower() == "fault":
                zid = z.get("id")
                faults.append(
                    {
                        "type": "zone_fault",
                        "severity": "critical",
                        "zone_id": int(zid) if zid is not None else None,
                        "zone_name": z.get("name") or (f"#{zid}" if zid is not None else "?"),
                        "reason": "Реле не подтвердило включение зоны",
                        "since": z.get("last_fault"),
                    }
                )
    except (KeyError, TypeError, ValueError) as e:
        logger.debug("api_status: fault aggregation failed: %s", e)

    if mqtt_secret_unavailable:
        faults.append(
            {
                "type": "mqtt_secret_unavailable",
                "severity": "critical",
                "zone_id": None,
                "zone_name": None,
                "reason": "MQTT credentials cannot be decrypted; restore the configured secret key",
                "since": None,
            }
        )
    # Report a hard disconnect only when a live runtime client explicitly says
    # it is disconnected. With no current signal, remain degraded/unknown
    # rather than turning absence of a request-time probe into a false claim.
    try:
        if mqtt_enabled_count and mqtt_health.get("status") == "degraded":
            faults.append(
                {
                    "type": "mqtt_disconnect",
                    "severity": "critical",
                    "zone_id": None,
                    "zone_name": None,
                    "reason": "Нет связи с MQTT-брокером — команды не доходят до реле",
                    "since": None,
                }
            )
        elif mqtt_enabled_count and mqtt_health.get("status") == "unknown":
            faults.append(
                {
                    "type": "mqtt_health_unknown",
                    "severity": "warning",
                    "zone_id": None,
                    "zone_name": None,
                    "reason": "Нет актуального сигнала от MQTT runtime",
                    "since": None,
                }
            )
    except (NameError, TypeError) as e:
        logger.debug("api_status: mqtt health check failed: %s", e)

    # sensor_mismatch (Горизонт 1): local temp sensor disagrees with Open-Meteo
    # beyond the hard threshold → the coefficient fell back to the forecast.
    # This is a *warning* (watering continues on API data), not a relay fault.
    try:
        from services.weather.singletons import get_weather_adjustment

        mm = get_weather_adjustment().get_sensor_mismatch()
        if mm and mm.get("level") == "hard":
            faults.append(
                {
                    "type": "sensor_mismatch",
                    "severity": "warning",
                    "zone_id": None,
                    "zone_name": None,
                    "reason": (
                        f"Датчик температуры {mm['local']:.0f}°C расходится с прогнозом Open-Meteo "
                        f"{mm['api']:.0f}°C — полив считается по прогнозу"
                    ),
                    "since": None,
                }
            )
    except (ImportError, OSError, ValueError, TypeError, KeyError, AttributeError) as e:
        logger.debug("api_status: sensor_mismatch check failed: %s", e)

    return jsonify(
        {
            "datetime": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "temperature": temperature,
            "humidity": humidity,
            "rain_enabled": bool(rain_cfg.get("enabled")),
            "rain_sensor": rain_sensor_status,
            "groups": groups_status,
            "emergency_stop": current_app.config.get("EMERGENCY_STOP", False),
            "is_admin": is_admin,
            "mqtt_servers_count": mqtt_servers_count,
            "mqtt_enabled_count": mqtt_enabled_count,
            "mqtt_connected": mqtt_connected,
            "mqtt_health": mqtt_health,
            "system_health": {
                "ok": not any(f.get("severity", "critical") == "critical" for f in faults),
                "faults": faults,
            },
        }
    )


# ===== Logs =====


@system_status_api_bp.route("/api/logs")
@admin_required
def api_logs():
    try:
        from_date = request.args.get("from")
        to_date = request.args.get("to")
        event_type = request.args.get("type")
        # The repository applies these predicates before its LIMIT 1000.
        # Filtering the already-limited result in Python hid older matches.
        logs = db.get_logs(event_type, from_date, to_date)
        return jsonify(logs)
    except (sqlite3.Error, OSError) as e:
        logger.error(f"Ошибка получения логов: {e}")
        return jsonify({"error": "Ошибка получения логов"}), 500


# ===== Water usage =====


@system_status_api_bp.route("/api/water")
def api_water():
    """Water usage data — real data from DB or empty arrays."""
    try:
        try:
            days = max(1, min(365, int(request.args.get("days", 7))))
        except (TypeError, ValueError):
            days = 7
        # get_water_usage(days) returns flat rows (zone_id, liters, timestamp,
        # zone_name) for ALL zones over the window — fetch once, split per group.
        try:
            usage_rows = db.get_water_usage(days) if hasattr(db, "get_water_usage") else []
        except (sqlite3.Error, OSError) as e:
            logger.debug("Exception in api_water: %s", e)
            usage_rows = []
        groups = db.get_groups()
        water_data = {}
        for group in groups:
            # Исключаем только служебную группу 999 («БЕЗ ПОЛИВА»); группы,
            # созданные после сида, получают id 1000+ и должны попадать в отчёт
            if group["id"] == 999:
                continue
            group_id = str(group["id"])
            try:
                zones = db.get_zones_by_group(group["id"])
                zone_ids = {int(zone["id"]) for zone in zones}
                zone_usage = {str(zone["id"]): {"name": zone["name"], "liters": 0, "last_used": None} for zone in zones}
                total_liters = 0
                liters_by_date: dict[str, float] = {}
                for row in usage_rows:
                    if row.get("zone_id") not in zone_ids:
                        continue
                    liters = float(row.get("liters") or 0)
                    ts = str(row.get("timestamp") or "")
                    total_liters += liters
                    if ts:
                        liters_by_date[ts[:10]] = liters_by_date.get(ts[:10], 0.0) + liters
                    zu = zone_usage.get(str(row.get("zone_id")))
                    if zu:
                        zu["liters"] = round(zu["liters"] + liters, 2)
                        # Rows are ordered by timestamp DESC — first hit is the freshest.
                        if zu["last_used"] is None:
                            zu["last_used"] = ts or None
                daily_usage = []
                for i in range(days):
                    date = (datetime.now() - timedelta(days=i)).strftime("%Y-%m-%d")
                    daily_usage.append({"date": date, "liters": round(liters_by_date.get(date, 0.0), 2)})
                water_data[group_id] = {
                    "group_name": group["name"],
                    "data": {
                        "daily_usage": daily_usage,
                        "total_liters": round(total_liters, 2),
                        "zone_usage": zone_usage,
                    },
                }
            except (sqlite3.Error, OSError) as e:
                logger.error(f"Ошибка обработки группы {group['id']}: {e}")
                continue
        return jsonify(water_data)
    except (sqlite3.Error, OSError) as e:
        logger.error(f"Ошибка получения данных о воде: {e}")
        return jsonify({"error": "Ошибка получения данных о воде"}), 500
