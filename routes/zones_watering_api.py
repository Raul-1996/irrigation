"""Zones Watering API — start/stop, watering time, SSE, MQTT control."""

import json
import logging
import sqlite3
from datetime import datetime

from flask import Blueprint, Response, current_app, jsonify, request, stream_with_context

from database import db
from irrigation_scheduler import get_scheduler
from services import sse_hub as _sse_hub
from services.api_rate_limiter import rate_limit
from services.audit import audit_log
from services.mqtt_pub import publish_mqtt_value as _publish_mqtt_value
from utils import normalize_topic

logger = logging.getLogger(__name__)

zones_watering_api_bp = Blueprint("zones_watering_api", __name__)


# ---- Zone start/stop ----


@zones_watering_api_bp.route("/api/zones/<int:zone_id>/start", methods=["POST"])
@audit_log("zone_start", target_extractor=lambda *a, **kw: f"zone:{kw.get('zone_id', a[0] if a else '?')}")
def start_zone(zone_id):
    """Start zone watering."""
    try:
        if current_app.config.get("EMERGENCY_STOP"):
            return jsonify(
                {"success": False, "message": "Аварийная остановка активна. Сначала отключите аварийный режим."}
            ), 400
        zone = db.get_zone(zone_id)
        if not zone:
            return jsonify({"success": False, "message": "Зона не найдена"}), 404

        try:
            from services.zone_control import start_zone_orchestrated

            status, _ctx = start_zone_orchestrated(int(zone_id))
        except (ValueError, TypeError, KeyError):
            logger.exception("start_zone: central start failed")
            return jsonify({"success": False, "message": "Не удалось запустить зону"}), 500
        if status in ("not_found", "failed"):
            return jsonify({"success": False, "message": "Не удалось запустить зону"}), 500

        group_id = int(zone.get("group_id") or 0)
        db.add_log(
            "zone_start",
            json.dumps({"zone": zone_id, "group": group_id, "source": "manual", "duration": int(zone["duration"])}),
        )
        return jsonify({"success": True, "message": f"Зона {zone_id} запущена", "zone_id": zone_id, "state": "on"})
    except (json.JSONDecodeError, KeyError, TypeError, ValueError) as e:
        logger.error(f"Ошибка запуска зоны {zone_id}: {e}")
        return jsonify({"success": False, "message": "Ошибка запуска зоны"}), 500


@zones_watering_api_bp.route("/api/zones/<int:zone_id>/stop", methods=["POST"])
@audit_log("zone_stop", target_extractor=lambda *a, **kw: f"zone:{kw.get('zone_id', a[0] if a else '?')}")
def stop_zone(zone_id):
    """Stop zone watering."""
    try:
        zone = db.get_zone(zone_id)
        if not zone:
            return jsonify({"success": False, "message": "Зона не найдена"}), 404

        # Issue #16: if this zone is part of an active group session, the
        # user-visible "stop" must abort the whole session — same as
        # api_zone_mqtt_stop above. See specs/issue-16-architecture.md §3.4.
        gid = int(zone.get("group_id") or 0)
        sched = get_scheduler()
        session_active = bool(sched and gid and sched.is_group_session_active(gid))
        if session_active:
            try:
                from services.audit import record_audit

                record_audit(
                    action_type="session_aborted_by_user",
                    source="zone_stop",
                    target=f"group:{gid}",
                    payload={"triggered_by_zone": int(zone_id), "endpoint": "api_zone_stop"},
                    actor="user",
                )
            except Exception:
                logger.exception("session_aborted_by_user audit failed")
            try:
                sched.cancel_group_jobs(int(gid))
                return jsonify(
                    {
                        "success": True,
                        "message": "Сессия группы остановлена",
                        "session_aborted": True,
                        "zone_id": zone_id,
                        "state": "off",
                    }
                )
            except (ValueError, TypeError, KeyError, RuntimeError):
                logger.exception("stop_zone: cancel_group_jobs failed, falling back to solo stop")

        try:
            from services.zone_control import stop_zone as _stop_central

            if not _stop_central(int(zone_id), reason="manual", force=False):
                return jsonify({"success": False, "message": "Не удалось остановить зону"}), 500
        except (ValueError, TypeError, KeyError):
            logger.exception("stop_zone: central stop failed")
            return jsonify({"success": False, "message": "Не удалось остановить зону"}), 500
        try:
            db.add_log(
                "zone_stop",
                json.dumps({"zone": int(zone_id), "group": int(zone.get("group_id") or 0), "source": "manual"}),
            )
        except (json.JSONDecodeError, KeyError, TypeError, ValueError) as e:
            logger.debug("Handled exception in stop_zone: %s", e)
        return jsonify({"success": True, "message": f"Зона {zone_id} остановлена", "zone_id": zone_id, "state": "off"})
    except (json.JSONDecodeError, KeyError, TypeError, ValueError) as e:
        logger.error(f"Ошибка остановки зоны {zone_id}: {e}")
        return jsonify({"success": False, "message": "Ошибка остановки зоны"}), 500


@zones_watering_api_bp.route("/api/zones/<int:zone_id>/watering-time")
def api_zone_watering_time(zone_id):
    """Returns remaining and elapsed watering time for a zone."""
    try:
        zone = db.get_zone(zone_id)
        if not zone:
            resp = jsonify({"success": False, "message": "Зона не найдена"})
            resp.headers["Cache-Control"] = "no-store"
            return resp, 404

        # Use planned_end_time if available (for override duration), else base duration
        planned_end_str = zone.get("planned_end_time")
        if planned_end_str and zone.get("watering_start_time"):
            try:
                planned_end_dt = datetime.strptime(planned_end_str, "%Y-%m-%d %H:%M:%S")
                start_dt_for_calc = datetime.strptime(zone.get("watering_start_time"), "%Y-%m-%d %H:%M:%S")
                total_duration = max(1, int((planned_end_dt - start_dt_for_calc).total_seconds() / 60))
                logger.debug("watering_time: zone %s using planned_end_time dur=%s", zone_id, total_duration)
            except (ValueError, TypeError) as e:
                logger.debug("planned_end_time parse failed: %s", e)
                total_duration = int(zone.get("duration") or 0)
        else:
            total_duration = int(zone.get("duration") or 0)
        start_str = zone.get("watering_start_time")
        if zone.get("state") != "on" or not start_str:
            resp = jsonify(
                {
                    "success": True,
                    "zone_id": zone_id,
                    "is_watering": False,
                    "elapsed_time": 0,
                    "remaining_time": 0,
                    "total_duration": total_duration,
                    "elapsed_seconds": 0,
                    "remaining_seconds": 0,
                    "total_seconds": total_duration * 60,
                }
            )
            resp.headers["Cache-Control"] = "no-store"
            return resp

        try:
            start_dt = datetime.strptime(start_str, "%Y-%m-%d %H:%M:%S")
        except (ValueError, TypeError, KeyError) as e:
            logger.debug("Exception in api_zone_watering_time: %s", e)
            db.update_zone(zone_id, {"watering_start_time": None})
            resp = jsonify(
                {
                    "success": True,
                    "zone_id": zone_id,
                    "is_watering": False,
                    "elapsed_time": 0,
                    "remaining_time": 0,
                    "total_duration": total_duration,
                    "elapsed_seconds": 0,
                    "remaining_seconds": 0,
                    "total_seconds": total_duration * 60,
                }
            )
            resp.headers["Cache-Control"] = "no-store"
            return resp

        now = datetime.now()
        elapsed_seconds = max(0, int((now - start_dt).total_seconds()))
        total_seconds = int(total_duration * 60)
        if elapsed_seconds >= total_seconds:
            try:
                from services.zone_control import stop_zone as _stop_central

                _stop_central(int(zone_id), reason="time_expired_poll", force=True)
            except (ValueError, TypeError, KeyError) as e:
                logger.debug("stop_zone from polling failed: %s", e)
            resp = jsonify(
                {
                    "success": True,
                    "zone_id": zone_id,
                    "is_watering": False,
                    "elapsed_time": total_duration,
                    "remaining_time": 0,
                    "total_duration": total_duration,
                    "elapsed_seconds": total_seconds,
                    "remaining_seconds": 0,
                    "total_seconds": total_seconds,
                }
            )
            resp.headers["Cache-Control"] = "no-store"
            return resp
        remaining_seconds = max(0, total_seconds - elapsed_seconds)
        elapsed_min = int(elapsed_seconds // 60)
        remaining_min = int(remaining_seconds // 60)
        resp = jsonify(
            {
                "success": True,
                "zone_id": zone_id,
                "is_watering": True,
                "elapsed_time": elapsed_min,
                "remaining_time": remaining_min,
                "total_duration": total_duration,
                "elapsed_seconds": elapsed_seconds,
                "remaining_seconds": remaining_seconds,
                "total_seconds": total_seconds,
            }
        )
        resp.headers["Cache-Control"] = "no-store"
        return resp
    except (sqlite3.Error, OSError) as e:
        logger.error(f"Ошибка получения времени полива зоны {zone_id}: {e}")
        resp = jsonify({"success": False, "message": "Ошибка получения времени полива"})
        resp.headers["Cache-Control"] = "no-store"
        return resp, 500


# ---- MQTT zones SSE ----


@zones_watering_api_bp.route("/api/mqtt/zones-sse")
def api_mqtt_zones_sse():
    """SSE endpoint: real-time zone state push.

    Design:
    - Client registers via sse_hub.register_client() -> a queue.Queue
      with maxsize=100 (bumped from 20 in Wave 5).
    - Generator pulls messages with a 15 s timeout; on timeout emits
      a keepalive comment `: ping\\n\\n` so proxies (nginx) don't close
      idle connections at 60 s.
    - MAX_SSE_CLIENTS=20 (also bumped in Wave 5) evicts oldest via
      sentinel `None` in the queue; generator treats `None` as shutdown.
    - Headers: Cache-Control: no-cache, X-Accel-Buffering: no — prevent
      nginx response buffering.
    - ARM/Hypercorn concern (the reason the old stub returned 204) is
      mitigated by: (1) per-client maxsize=100 instead of 20, so slow
      clients don't get mass-evicted during burst; (2) MAX_SSE_CLIENTS
      hard cap so hub thread doesn't accumulate unbounded queues.
    """
    try:
        _sse_hub.ensure_hub_started()
    except (OSError, RuntimeError) as e:
        logger.debug("SSE hub start (background): %s", e)

    msg_queue = _sse_hub.register_client()

    def _generate():
        import queue as _q

        # Initial handshake + snapshot trigger
        yield ": connected\n\n"
        try:
            while True:
                try:
                    data = msg_queue.get(timeout=15.0)
                except _q.Empty:
                    yield ": ping\n\n"  # keepalive
                    continue
                if data is None:
                    break  # eviction sentinel
                yield f"data: {data}\n\n"
        finally:
            _sse_hub.unregister_client(msg_queue)

    resp = Response(stream_with_context(_generate()), mimetype="text/event-stream")
    resp.headers["Cache-Control"] = "no-cache"
    resp.headers["X-Accel-Buffering"] = "no"
    resp.headers["Connection"] = "keep-alive"
    return resp


# ---- Zone MQTT start/stop ----


@zones_watering_api_bp.route("/api/zones/<int:zone_id>/mqtt/start", methods=["POST"])
@rate_limit("mqtt_control", max_requests=10, window_sec=60)
@audit_log("zone_mqtt_start", target_extractor=lambda *a, **kw: f"zone:{kw.get('zone_id', a[0] if a else '?')}")
def api_zone_mqtt_start(zone_id: int):
    """Manual MQTT start of a zone — thin shim around services.zone_control.start_zone_orchestrated.

    History note: prior to fix/mqtt-start-unify this endpoint duplicated ~258
    lines of start logic (peer-off threadpool, master-valve open, MQTT publish,
    DB writes) and — critically — never called db.create_zone_run. Result:
    UI-initiated starts left zone_runs empty, breaking get_last_watering_time.
    The endpoint keeps only HTTP validation (override duration/percent) and
    response shaping; the orchestration lives in services.zone_control.
    """
    try:
        z = db.get_zone(zone_id)
        if not z:
            return jsonify({"success": False, "message": "Зона не найдена"}), 404
        if current_app.config.get("EMERGENCY_STOP"):
            return jsonify({"success": False, "message": "Аварийная остановка активна"}), 400

        # ---- Optional one-time duration override (1..120 min) ----
        # Base duration in DB stays untouched — the override is applied by
        # start_zone_orchestrated for schedule_zone_stop / planned_end_time.
        override_duration = None
        override_percent = None
        body = request.get_json(silent=True) or {}
        # Issue #12 C2: "minutes wins if both sent" must be strict.
        # If `duration` is present in the body AT ALL, that is the user's
        # intent — accept it (1..120) or reject the whole request (400).
        # Never silently fall through to percent. See specs/issue-12-review.md.
        req_d_raw = body.get("duration")
        minutes_sent = req_d_raw is not None
        if minutes_sent:
            try:
                req_d = int(req_d_raw)
            except (ValueError, TypeError):
                return jsonify({"success": False, "message": "duration должен быть целым числом 1..120"}), 400
            if not (1 <= req_d <= 120):
                return jsonify({"success": False, "message": "duration должен быть в диапазоне 1..120 мин"}), 400
            override_duration = req_d

        # ---- Issue #12: %-of-norm override (alternative to `duration`) ----
        # Minutes mode (duration) wins if SENT (validated above). Percent is
        # only honoured when `duration` is absent/null in the body. We don't
        # relax the 120-min cap on minutes mode — only percent mode may
        # produce 121..240.
        if not minutes_sent:
            try:
                req_pct = body.get("duration_percent")
                if req_pct is not None:
                    from services.zone_control import PERCENT_PRESETS

                    p = int(req_pct)
                    if p in PERCENT_PRESETS:
                        override_percent = p
            except (ValueError, TypeError) as e:
                logger.debug("mqtt_start percent parse: %s", e)

        try:
            from services.zone_control import start_zone_orchestrated

            status, ctx = start_zone_orchestrated(
                int(zone_id), override_duration=override_duration, override_percent=override_percent
            )
        except (ValueError, TypeError, KeyError):
            logger.exception("api_zone_mqtt_start: central start failed")
            return jsonify({"success": False, "message": "Не удалось запустить зону"}), 500
        warnings = ctx.get("warnings") or []
        if status == "not_found":
            return jsonify({"success": False, "message": "Зона не найдена"}), 404
        if status == "failed":
            return jsonify({"success": False, "message": "Не удалось запустить зону"}), 500
        if status == "rescheduled":
            return jsonify(
                {
                    "success": True,
                    "message": f"Зона {zone_id} перезапущена на {ctx.get('duration')} мин",
                    "warnings": warnings,
                }
            )
        if status == "already_on":
            return jsonify({"success": True, "message": "Зона уже запущена", "warnings": warnings})

        try:
            db.add_log("zone_start_manual", json.dumps({"zone": int(zone_id), "group": int(z.get("group_id") or 0)}))
        except (json.JSONDecodeError, KeyError, TypeError, ValueError) as e:
            logger.debug("mqtt_start add_log failed: %s", e)

        return jsonify({"success": True, "message": f"Зона {int(zone_id)} запущена", "warnings": warnings})
    except (json.JSONDecodeError, KeyError, TypeError, ValueError):
        logger.exception("api_zone_mqtt_start failed")
        return jsonify({"success": False, "message": "Ошибка запуска зоны"}), 500


@zones_watering_api_bp.route("/api/zones/<int:zone_id>/mqtt/stop", methods=["POST"])
@rate_limit("mqtt_control", max_requests=10, window_sec=60)
@audit_log("zone_mqtt_stop", target_extractor=lambda *a, **kw: f"zone:{kw.get('zone_id', a[0] if a else '?')}")
def api_zone_mqtt_stop(zone_id: int):
    z = db.get_zone(zone_id)
    if not z:
        return jsonify({"success": False}), 404

    # Issue #16: if this zone belongs to a group with an active session
    # (manual group sequence or scheduled program currently running), the
    # user pressing "stop" on the zone card must abort the WHOLE session,
    # not just close one valve and let the sequencer advance to the next
    # zone. Detection is via is_group_session_active(); the abort itself
    # reuses cancel_group_jobs (the same primitive /api/groups/<id>/stop
    # already calls). See specs/issue-16-architecture.md §3.3.
    gid = int(z.get("group_id") or 0)
    sched = get_scheduler()
    session_active = bool(sched and gid and sched.is_group_session_active(gid))
    if session_active:
        try:
            from services.audit import record_audit

            record_audit(
                action_type="session_aborted_by_user",
                source="zone_stop",
                target=f"group:{gid}",
                payload={"triggered_by_zone": int(zone_id), "endpoint": "api_zone_mqtt_stop"},
                actor="user",
            )
        except Exception:
            logger.exception("session_aborted_by_user audit failed")
        try:
            sched.cancel_group_jobs(int(gid))
            # cancel_group_jobs already invokes stop_all_in_group(force=True)
            # which stops THIS zone too, so we don't need an extra stop_zone.
            return jsonify({"success": True, "message": "Сессия группы остановлена", "session_aborted": True})
        except (ValueError, TypeError, KeyError, RuntimeError):
            # Best-effort safety net: fall through to the legacy single-zone
            # stop path so the valve definitely goes off even if the abort
            # plumbing fails.
            logger.exception("api_zone_mqtt_stop: cancel_group_jobs failed, falling back to solo stop")

    try:
        from services.zone_control import stop_zone as _stop_central

        if _stop_central(int(zone_id), reason="manual", force=False):
            return jsonify({"success": True, "message": "Зона остановлена"})
    except (ValueError, TypeError, KeyError):
        logger.exception("api_zone_mqtt_stop: central stop failed, fallback to direct publish")
    sid = z.get("mqtt_server_id")
    topic = (z.get("topic") or "").strip()
    if not sid or not topic:
        return jsonify({"success": False, "message": "No MQTT config for zone"}), 400
    t = normalize_topic(topic)
    try:
        server = db.get_mqtt_server(int(sid))
        if not server:
            return jsonify({"success": False, "message": "MQTT server not found"}), 400
        logger.info(f"HTTP publish OFF zone={zone_id} topic={t}")
        _publish_mqtt_value(server, t, "0", min_interval_sec=0.0, qos=2, retain=True)
        try:
            # Manual MQTT stop — operator action, audited.
            from services.zones_state import update_zone_state as _uzs

            _uzs(zone_id, {"state": "off", "watering_start_time": None}, audit_reason="mqtt_stop")
        except (sqlite3.Error, OSError, ImportError) as e:
            logger.debug("Handled exception in api_zone_mqtt_stop: %s", e)
        return jsonify({"success": True, "message": "Зона остановлена"})
    except (ConnectionError, TimeoutError, OSError) as e:
        logger.error(f"MQTT publish stop failed: {e}")
        return jsonify({"success": False, "message": "MQTT publish failed"}), 500
