"""Zones Watering API — start/stop, watering time, SSE, MQTT control."""

import json
import logging
import sqlite3
import time
from datetime import datetime, timedelta

from flask import Blueprint, Response, current_app, jsonify, request, stream_with_context

from database import db
from irrigation_scheduler import get_scheduler
from services import sse_hub as _sse_hub
from services.api_rate_limiter import rate_limit
from services.audit import audit_log
from services.mqtt_pub import publish_mqtt_value as _publish_mqtt_value
from utils import normalize_topic

try:
    import paho.mqtt.client as mqtt
except ImportError:
    mqtt = None

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
            scheduler = get_scheduler()
            if scheduler:
                scheduler.cancel_group_jobs(int(zone["group_id"]))
        except (ValueError, TypeError, KeyError) as e:
            logger.debug("Handled exception in start_zone: %s", e)

        # Turn off all other zones in group
        try:
            zones = db.get_zones()
            group_id = int(zone.get("group_id") or 0)
            if group_id:
                group_zones = [z for z in zones if z["group_id"] == group_id and int(z["id"]) != int(zone_id)]
                for gz in group_zones:
                    try:
                        sid = gz.get("mqtt_server_id")
                        topic = (gz.get("topic") or "").strip()
                        if mqtt and sid and topic:
                            t = topic if str(topic).startswith("/") else "/" + str(topic)
                            server = db.get_mqtt_server(int(sid))
                            if server:
                                _publish_mqtt_value(server, t, "0", min_interval_sec=0.0, qos=2, retain=True)
                    except (ConnectionError, TimeoutError, OSError):
                        logger.exception("Ошибка публикации MQTT '0' при ручном запуске: выключение соседей")
                    try:
                        # Manual start of one zone in a group force-stops
                        # peers — audited so each peer's transition is visible.
                        from services.zones_state import update_zone_state as _uzs

                        _uzs(
                            int(gz["id"]),
                            {"state": "off", "watering_start_time": None},
                            audit_reason="peer_off_manual_start",
                        )
                    except (sqlite3.Error, OSError, ImportError) as e:
                        logger.debug("Handled exception in line_796: %s", e)
        except (ConnectionError, TimeoutError, OSError) as e:
            logger.debug("Handled exception in line_798: %s", e)

        try:
            from services.zone_control import exclusive_start_zone as _start_central

            ok = _start_central(int(zone_id))
            if not ok:
                return jsonify({"success": False, "message": "Не удалось запустить зону"}), 500
        except (ValueError, TypeError, KeyError):
            logger.exception("start_zone: central start failed")
            return jsonify({"success": False, "message": "Не удалось запустить зону"}), 500

        try:
            scheduler = get_scheduler()
            if scheduler:
                scheduler.schedule_zone_stop(zone_id, int(zone["duration"]), command_id=str(int(time.time())))
        except (ValueError, TypeError, KeyError) as e:
            logger.error(f"Ошибка планирования остановки зоны {zone_id}: {e}")

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
    """Manual MQTT start of a zone — thin shim around services.zone_control.exclusive_start_zone.

    History note: prior to fix/mqtt-start-unify this endpoint duplicated ~258
    lines of start logic (peer-off threadpool, master-valve open, MQTT publish,
    DB writes) and — critically — never called db.create_zone_run. Result:
    UI-initiated starts left zone_runs empty, breaking get_last_watering_time.
    The sibling endpoint /api/zones/<id>/start (start_zone) was already
    delegating to exclusive_start_zone, so this is alignment, not invention.
    """
    try:
        z = db.get_zone(zone_id)
        if not z:
            return jsonify({"success": False, "message": "Зона не найдена"}), 404
        if current_app.config.get("EMERGENCY_STOP"):
            return jsonify({"success": False, "message": "Аварийная остановка активна"}), 400

        # ---- Optional one-time duration override (1..120 min) ----
        # Carry as local only — exclusive_start_zone does NOT consult
        # z['duration'] for any scheduling, so in-memory mutation would be
        # invisible. We use override_dur explicitly for schedule_zone_stop /
        # planned_end_time below; base duration in DB stays untouched.
        override_dur = None
        warnings: list = []  # Issue #12 — populated by % branch when applicable
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
            override_dur = req_d
            logger.info(
                "mqtt_start: zone %s using override duration %s min (base unchanged)",
                zone_id,
                req_d,
            )

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
                    from services.zone_control import per_zone_dur as _per_zone_dur

                    p = int(req_pct)
                    if p in PERCENT_PRESETS:
                        computed, warns = _per_zone_dur(z, None, p)
                        override_dur = computed
                        warnings = warns
                        logger.info(
                            "mqtt_start: zone %s using override percent %s%% -> %s min (warnings=%s)",
                            zone_id,
                            p,
                            override_dur,
                            warnings,
                        )
            except (ValueError, TypeError) as e:
                logger.debug("mqtt_start percent parse: %s", e)

        # ---- Already-ON branch — reschedule stop, do NOT delegate ----
        # Delegating would peer-off siblings (none changed) and re-publish
        # ON (no-op with retain). The only useful effect of a re-POST while
        # ON is updating the auto-stop time, so handle it here without a
        # new zone_run row.
        if str(z.get("state") or "") == "on":
            if override_dur is not None:
                now_dt = datetime.now()
                new_end = (now_dt + timedelta(minutes=override_dur)).strftime("%Y-%m-%d %H:%M:%S")
                db.update_zone(
                    zone_id,
                    {
                        "planned_end_time": new_end,
                        "watering_start_time": now_dt.strftime("%Y-%m-%d %H:%M:%S"),
                        "watering_start_source": "manual",
                    },
                )
                try:
                    sched = get_scheduler()
                    if sched:
                        # Remove existing stop jobs for THIS zone only —
                        # cancel_group_jobs would stop running peers.
                        try:
                            for job in sched.scheduler.get_jobs():
                                if f"zone_stop:{zone_id}:" in str(job.id) or f"zone_hard_stop:{zone_id}" in str(job.id):
                                    job.remove()
                        except (RuntimeError, AttributeError, ValueError) as e:
                            logger.debug("remove old stop jobs: %s", e)
                        if not current_app.config.get("TESTING", False):
                            sched.schedule_zone_stop(int(zone_id), override_dur, command_id=str(int(time.time())))
                            sched.schedule_zone_hard_stop(int(zone_id), now_dt + timedelta(minutes=override_dur))
                except (ValueError, TypeError, ImportError) as e:
                    logger.debug("reschedule on override: %s", e)
                logger.info(
                    "mqtt_start: zone %s already ON, rescheduled to %s min (end=%s)",
                    zone_id,
                    override_dur,
                    new_end,
                )
                return jsonify(
                    {
                        "success": True,
                        "message": f"Зона {zone_id} перезапущена на {override_dur} мин",
                        "warnings": warnings,
                    }
                )
            return jsonify({"success": True, "message": "Зона уже запущена", "warnings": warnings})

        # ---- Pre-delegate housekeeping: cancel scheduled program/stop jobs ----
        # cancel_group_jobs sets the cancel-event flag and removes APScheduler
        # jobs for the group (programs, peer zone_stops). It also calls
        # stop_all_in_group(force=True) — which the delegate's parallel
        # peer-off would do anyway, but we still need cancel_group_jobs for
        # the program/job-removal side effects. Sibling start_zone calls
        # cancel_group_jobs the same way.
        gid = int(z.get("group_id") or 0)
        if gid:
            try:
                sched = get_scheduler()
                if sched:
                    sched.cancel_group_jobs(int(gid))
                try:
                    programs = db.get_programs() or []
                    now = datetime.now()
                    today = now.strftime("%Y-%m-%d")
                    for p in programs:
                        try:
                            hh, mm = map(int, str(p.get("time") or "00:00").split(":", 1))
                        except (ValueError, TypeError, KeyError):
                            hh, mm = 0, 0
                        if now.replace(hour=hh, minute=mm, second=0, microsecond=0) <= now:
                            db.cancel_program_run_for_group(int(p.get("id")), today, int(gid))
                except (ConnectionError, TimeoutError, OSError, sqlite3.Error) as e:
                    logger.debug("mqtt_start: cancel programs failed: %s", e)
                try:
                    db.reschedule_group_to_next_program(int(gid))
                except (sqlite3.Error, OSError) as e:
                    logger.debug("mqtt_start: reschedule_group_to_next_program failed: %s", e)
            except (ConnectionError, TimeoutError, OSError) as e:
                logger.debug("mqtt_start pre-delegate housekeeping: %s", e)

        # ---- Delegate to single source of truth ----
        # exclusive_start_zone does (in order, all under group/zone locks):
        #   1) state -> 'starting'
        #   2) db.create_zone_run(...)            <-- the bug fix
        #   3) master-valve open (if group uses MV)
        #   4) MQTT publish '1' on zone topic
        #   5) state -> 'on'
        #   6) parallel peer-off (publish '0' + finish their zone_runs)
        # On MQTT failure step 4, the zone_run row stays open and is
        # aborted by _boot_sync on next boot — that's the documented
        # invariant, do not reorder.
        try:
            from services.zone_control import exclusive_start_zone as _start_central

            ok = _start_central(int(zone_id))
            if not ok:
                return jsonify({"success": False, "message": "Не удалось запустить зону"}), 500
        except (ValueError, TypeError, KeyError):
            logger.exception("api_zone_mqtt_start: central start failed")
            return jsonify({"success": False, "message": "Не удалось запустить зону"}), 500

        # ---- Post-delegate: planned_end_time + source, schedule auto-stop ----
        # exclusive_start_zone writes state/commanded_state/watering_start_time
        # but NOT planned_end_time or watering_start_source. Add them here so
        # the UI auto-stop timer / "manual" badge work the same as before.
        dur_min = override_dur if override_dur is not None else int(z.get("duration") or 10)
        now_dt = datetime.now()
        planned_end = (now_dt + timedelta(minutes=dur_min)).strftime("%Y-%m-%d %H:%M:%S")
        try:
            from services.zones_state import update_zone_state as _uzs

            _uzs(
                int(zone_id),
                {
                    "planned_end_time": planned_end,
                    "watering_start_source": "manual",
                },
                audit_reason="manual_start_planned_end",
            )
        except (sqlite3.Error, OSError, ImportError):
            logger.exception(
                "api_zone_mqtt_start: audited planned_end_time update failed zone=%s — falling back to raw update_zone",
                zone_id,
            )
            try:
                db.update_zone(
                    int(zone_id),
                    {
                        "planned_end_time": planned_end,
                        "watering_start_source": "manual",
                    },
                )
            except (sqlite3.Error, OSError) as e:
                logger.debug("mqtt_start: planned_end_time fallback update failed: %s", e)

        # Schedule auto-stop synchronously (sched calls are non-blocking).
        # Sibling start_zone uses the same pattern at line 85-89.
        try:
            sched = get_scheduler()
            if sched and not current_app.config.get("TESTING", False):
                sched.schedule_zone_stop(int(zone_id), dur_min, command_id=str(int(time.time())))
                sched.schedule_zone_hard_stop(int(zone_id), now_dt + timedelta(minutes=dur_min))
        except (ValueError, TypeError, ImportError) as e:
            logger.debug("mqtt_start schedule stops: %s", e)

        try:
            db.add_log("zone_start_manual", json.dumps({"zone": int(zone_id), "group": gid}))
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
