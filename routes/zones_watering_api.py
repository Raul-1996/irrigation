"""Zones Watering API — start/stop, watering time, SSE, MQTT control."""

import json
import logging
import sqlite3
import threading
from datetime import datetime

from flask import Blueprint, Response, current_app, jsonify, request

from database import db
from irrigation_scheduler import get_scheduler
from services import sse_hub as _sse_hub
from services.api_rate_limiter import rate_limit
from services.audit import audit_log

logger = logging.getLogger(__name__)

zones_watering_api_bp = Blueprint("zones_watering_api", __name__)

_SSE_HTTP_LOCK = threading.Lock()
_SSE_HTTP_ACTIVE = 0


def _sse_http_limit() -> int:
    """Compute a cap that always leaves the configured control reserve."""
    try:
        workers = max(2, int(current_app.config.get("HTTP_EXECUTOR_WORKERS", 8)))
        reserve = min(workers - 1, max(1, int(current_app.config.get("HTTP_CONTROL_WORKER_RESERVE", 2))))
        configured = max(1, int(current_app.config.get("SSE_HTTP_MAX_CLIENTS", 4)))
        return min(configured, workers - reserve)
    except (AttributeError, TypeError, ValueError):
        return 1


def _acquire_sse_http_slot(limit: int):
    """Acquire a global stream lease and return an idempotent releaser."""
    global _SSE_HTTP_ACTIVE
    with _SSE_HTTP_LOCK:
        if max(1, int(limit)) <= _SSE_HTTP_ACTIVE:
            return None
        _SSE_HTTP_ACTIVE += 1

    released = False

    def _release() -> None:
        nonlocal released
        global _SSE_HTTP_ACTIVE
        with _SSE_HTTP_LOCK:
            if released:
                return
            released = True
            _SSE_HTTP_ACTIVE = max(0, _SSE_HTTP_ACTIVE - 1)

    return _release


def _sse_event_stream(msg_queue, close_stream):
    """Yield one SSE stream and always transfer termination to cleanup."""
    import queue as _q

    try:
        yield ": connected\n\n"
        while True:
            try:
                data = msg_queue.get(timeout=15.0)
            except _q.Empty:
                yield ": ping\n\n"
                continue
            if data is None:
                break
            yield f"data: {data}\n\n"
    finally:
        close_stream()


def _signal_group_session_cancel(scheduler, group_id: int) -> None:
    """Stop the sequencer advancing while physical OFF is being confirmed."""
    try:
        event = scheduler.group_cancel_events.get(int(group_id))
        if event is not None:
            event.set()
    except (AttributeError, KeyError, TypeError, ValueError):
        logger.exception("group session cancel signal failed group=%s", group_id)


_SCHEDULER_STOP_AGGREGATE_KEYS = {
    "success",
    "aggregate_valid",
    "stopped",
    "unresolved",
    "unverified_zone_ids",
    "retry_scheduled",
    "group_id",
}


def _strict_zone_id_bucket(value) -> list[int] | None:
    if type(value) is not list:
        return None
    if any(type(zone_id) is not int or zone_id <= 0 for zone_id in value):
        return None
    if len(value) != len(set(value)):
        return None
    return sorted(value)


def _completed_session_stop_aggregate(result, group_id: int, expected_zone_ids: list[int]) -> dict | None:
    """Accept only the scheduler's exact proof of one completed group OFF."""
    gid = int(group_id)
    if type(result) is not dict or set(result) != _SCHEDULER_STOP_AGGREGATE_KEYS:
        return None
    if (
        type(result.get("success")) is not bool
        or result.get("success") is not True
        or type(result.get("aggregate_valid")) is not bool
        or result.get("aggregate_valid") is not True
        or type(result.get("retry_scheduled")) is not bool
        or result.get("retry_scheduled") is not False
        or type(result.get("group_id")) is not int
        or result.get("group_id") != gid
    ):
        return None

    stopped = _strict_zone_id_bucket(result.get("stopped"))
    unresolved = _strict_zone_id_bucket(result.get("unresolved"))
    unverified = _strict_zone_id_bucket(result.get("unverified_zone_ids"))
    expected = set(expected_zone_ids)
    if stopped is None or unresolved is None or unverified is None:
        return None
    stopped_set = set(stopped)
    unresolved_set = set(unresolved)
    unverified_set = set(unverified)
    if stopped_set & unresolved_set or stopped_set & unverified_set or unresolved_set & unverified_set:
        return None
    if stopped_set | unresolved_set | unverified_set != expected:
        return None
    if stopped_set != expected or unresolved or unverified:
        return None
    return {
        "success": True,
        "stopped": stopped,
        "unresolved": [],
        "unverified_zone_ids": [],
        "group_id": gid,
    }


def _abort_group_session_after_off(scheduler, group_id: int) -> dict:
    """Abort a session while preserving safety retries for unresolved OFFs."""
    gid = int(group_id)
    result = {
        "success": False,
        "stopped": [],
        "unresolved": [],
        "unverified_zone_ids": [],
        "group_id": gid,
    }
    _signal_group_session_cancel(scheduler, gid)
    try:
        from services import zone_control

        strict_group_zone_ids = getattr(zone_control, "_strict_group_zone_ids", None)
        expected_zone_ids = strict_group_zone_ids(gid) if callable(strict_group_zone_ids) else None
    except (sqlite3.Error, OSError, AttributeError, TypeError, ValueError):
        logger.exception("session abort: unable to load strict group zones group=%s", gid)
        expected_zone_ids = None
    if expected_zone_ids is None:
        result["error_code"] = "SESSION_INVENTORY_UNAVAILABLE"
        return result

    _stop_central = zone_control.stop_zone

    stopped: set[int] = set()
    unresolved: set[int] = set()
    for zone_id in expected_zone_ids:
        try:
            physically_stopped = _stop_central(
                zone_id,
                reason="manual_session_abort",
                force=True,
                require_observed_confirmation=True,
            )
        except (ConnectionError, TimeoutError, OSError, sqlite3.Error, ValueError, TypeError, KeyError, RuntimeError):
            logger.exception("session abort OFF failed zone=%s", zone_id)
            physically_stopped = False
        (stopped if physically_stopped is True else unresolved).add(zone_id)

    if unresolved:
        # Keep zone_stop / hard_stop jobs planted.  They are the remaining
        # recovery path when the immediate physical OFF command failed.
        result.update({"stopped": sorted(stopped), "unresolved": sorted(unresolved)})
        return result

    try:
        cancel_result = scheduler.cancel_group_jobs(gid)
    except (ConnectionError, TimeoutError, OSError, sqlite3.Error, ValueError, TypeError, KeyError, RuntimeError):
        logger.exception("session abort cleanup failed group=%s", gid)
        result.update(
            {
                "stopped": sorted(stopped),
                "unresolved": [],
                "error_code": "SESSION_CLEANUP_FAILED",
            }
        )
        return result

    completed = _completed_session_stop_aggregate(cancel_result, gid, expected_zone_ids)
    if completed is None:
        result.update(
            {
                "stopped": [],
                "unresolved": [],
                "unverified_zone_ids": sorted(expected_zone_ids),
                "error_code": "SESSION_AGGREGATE_INVALID",
            }
        )
        return result
    return completed


def _accepted_stop_response(zone_id: int):
    """Serialize the persisted result of an accepted central OFF command."""
    current = db.get_zone(int(zone_id))
    state = str((current or {}).get("state") or "").strip().lower()
    if state not in {"off", "stopping", "fault"}:
        logger.error("central stop returned success without a persisted stop state zone=%s state=%s", zone_id, state)
        return (
            jsonify(
                {
                    "success": False,
                    "message": "Команда OFF не зафиксирована",
                    "error_code": "ZONE_STOP_STATE_INVALID",
                    "zone_id": int(zone_id),
                    "state": state or None,
                }
            ),
            409,
        )

    pending = state != "off"
    try:
        db.add_log(
            "zone_stop_command",
            json.dumps(
                {
                    "zone": int(zone_id),
                    "group": int((current or {}).get("group_id") or 0),
                    "source": "manual",
                    "state": state,
                    "pending_confirmation": pending,
                }
            ),
        )
    except (sqlite3.Error, OSError, json.JSONDecodeError, KeyError, TypeError, ValueError):
        logger.debug("zone stop command log failed zone=%s", zone_id, exc_info=True)

    return jsonify(
        {
            "success": True,
            "message": "Команда OFF отправлена" if pending else f"Зона {int(zone_id)} остановлена",
            "zone_id": int(zone_id),
            "state": state,
            "pending_confirmation": pending,
        }
    )


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

            status, _ctx = start_zone_orchestrated(int(zone_id), restart_if_on=True)
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
                session_result = _abort_group_session_after_off(sched, int(gid))
                if session_result.get("success") is not True:
                    return (
                        jsonify(
                            {
                                "success": False,
                                "message": "Не все зоны подтвердили состояние OFF",
                                "session_aborted": False,
                                "stopped": session_result.get("stopped") or [],
                                "unresolved": session_result.get("unresolved") or [],
                                "unverified_zone_ids": session_result.get("unverified_zone_ids") or [],
                                "error_code": session_result.get("error_code") or "SESSION_OFF_UNRESOLVED",
                            }
                        ),
                        503,
                    )
                return jsonify(
                    {
                        "success": True,
                        "message": "Сессия группы остановлена",
                        "session_aborted": True,
                        "zone_id": zone_id,
                        "state": "off",
                        "stopped": session_result.get("stopped") or [],
                        "unresolved": [],
                    }
                )
            except (ValueError, TypeError, KeyError, RuntimeError):
                logger.exception("stop_zone: session abort failed")
                return (
                    jsonify(
                        {
                            "success": False,
                            "message": "Не удалось подтвердить остановку сессии группы",
                            "session_aborted": False,
                            "stopped": [],
                            "unresolved": [int(zone_id)],
                            "unverified_zone_ids": [],
                            "error_code": "SESSION_ABORT_FAILED",
                        }
                    ),
                    503,
                )

        try:
            from services.zone_control import stop_zone as _stop_central

            if not _stop_central(int(zone_id), reason="manual", force=True):
                return jsonify({"success": False, "message": "Не удалось остановить зону"}), 500
        except (ValueError, TypeError, KeyError):
            logger.exception("stop_zone: central stop failed")
            return jsonify({"success": False, "message": "Не удалось остановить зону"}), 500
        return _accepted_stop_response(int(zone_id))
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
    release_slot = _acquire_sse_http_slot(_sse_http_limit())
    if release_slot is None:
        response = jsonify(
            {
                "success": False,
                "message": "Too many live-update connections",
                "error_code": "SSE_CAPACITY",
            }
        )
        response.status_code = 503
        response.headers["Retry-After"] = "5"
        return response

    close_lock = threading.Lock()
    stream_closed = False
    msg_queue = None

    def _close_stream() -> None:
        nonlocal stream_closed
        with close_lock:
            if stream_closed:
                return
            stream_closed = True
        try:
            if msg_queue is not None:
                _sse_hub.unregister_client(msg_queue)
        except Exception:
            logger.exception("SSE client unregister failed")
        finally:
            release_slot()

    # Until the Response is returned, this scope owns both the capacity lease
    # and any registered queue.  Catch BaseException only across that narrow
    # ownership-transfer window, clean up, then re-raise without swallowing.
    try:
        try:
            _sse_hub.ensure_hub_started()
        except (OSError, RuntimeError) as e:
            logger.debug("SSE hub start (background): %s", e)

        msg_queue = _sse_hub.register_client()
        stream = _sse_event_stream(msg_queue, _close_stream)

        # The generator uses no request-local state, so wrapping it in
        # stream_with_context is unnecessary and makes cross-context close unsafe.
        resp = Response(stream, mimetype="text/event-stream")
        resp.headers["Cache-Control"] = "no-cache"
        resp.headers["X-Accel-Buffering"] = "no"
        resp.headers["Connection"] = "keep-alive"
        resp.call_on_close(_close_stream)
        return resp
    except BaseException:
        _close_stream()
        raise


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
@audit_log("zone_mqtt_stop", target_extractor=lambda *a, **kw: f"zone:{kw.get('zone_id', a[0] if a else '?')}")
def api_zone_mqtt_stop(zone_id: int):
    # Explicit OFF is a safety action and must remain available even when the
    # per-IP start bucket is saturated.  Authentication/guest-control policy
    # is still enforced centrally by app._auth_before_request before this view.
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
            session_result = _abort_group_session_after_off(sched, int(gid))
            if session_result.get("success") is not True:
                return (
                    jsonify(
                        {
                            "success": False,
                            "message": "Не все зоны подтвердили состояние OFF",
                            "session_aborted": False,
                            "stopped": session_result.get("stopped") or [],
                            "unresolved": session_result.get("unresolved") or [],
                            "unverified_zone_ids": session_result.get("unverified_zone_ids") or [],
                            "error_code": session_result.get("error_code") or "SESSION_OFF_UNRESOLVED",
                        }
                    ),
                    503,
                )
            return jsonify(
                {
                    "success": True,
                    "message": "Сессия группы остановлена",
                    "session_aborted": True,
                    "stopped": session_result.get("stopped") or [],
                    "unresolved": [],
                }
            )
        except (ValueError, TypeError, KeyError, RuntimeError):
            logger.exception("api_zone_mqtt_stop: session abort failed")
            return (
                jsonify(
                    {
                        "success": False,
                        "message": "Не удалось подтвердить остановку сессии группы",
                        "session_aborted": False,
                        "stopped": [],
                        "unresolved": [int(zone_id)],
                        "unverified_zone_ids": [],
                        "error_code": "SESSION_ABORT_FAILED",
                    }
                ),
                503,
            )

    try:
        from services.zone_control import stop_zone as _stop_central

        stopped = _stop_central(int(zone_id), reason="manual", force=True)
    except (ConnectionError, TimeoutError, OSError, sqlite3.Error, ValueError, TypeError, KeyError, RuntimeError):
        logger.exception("api_zone_mqtt_stop: central stop failed")
        stopped = False
    if stopped:
        return _accepted_stop_response(int(zone_id))

    # The central path owns the topology lock, command generation, broker
    # publish and durable transition.  Retrying here from the pre-call snapshot
    # can target a channel that moved while the central call was running, and
    # can publish after its CAS already rejected.  Surface the unresolved stop
    # and let the retained safety retry/current owner decide the next command.
    current = db.get_zone(int(zone_id)) or {}
    logger.error(
        "api_zone_mqtt_stop: central stop unresolved zone=%s state=%s version=%s",
        zone_id,
        current.get("state"),
        current.get("version"),
    )
    return (
        jsonify(
            {
                "success": False,
                "message": "Не удалось подтвердить команду остановки зоны",
                "error_code": "ZONE_STOP_UNRESOLVED",
                "state": current.get("state"),
            }
        ),
        500,
    )
