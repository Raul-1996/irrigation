"""Groups API blueprint — all /api/groups* endpoints + master valve."""

import json
import logging
import sqlite3
import threading
import time
from functools import wraps

from flask import Blueprint, current_app, jsonify, request

from constants import GROUP_DEBOUNCE_SEC
from database import db
from irrigation_scheduler import get_scheduler, init_scheduler
from services import sse_hub as _sse_hub
from services.api_rate_limiter import rate_limit
from services.audit import audit_log
from services.locks import group_lock
from services.mqtt_pub import publish_mqtt_value as _publish_mqtt_value
from services.observed_state import verify_master_command as _verify_master_command
from utils import normalize_topic

logger = logging.getLogger(__name__)

try:
    import paho.mqtt.client as mqtt
except ImportError as e:
    logger.debug("Exception in line_18: %s", e)
    mqtt = None

groups_api_bp = Blueprint("groups_api", __name__)

# Anti-flapper guard
_GROUP_CHANGE_GUARD = {}
_GROUP_GUARD_LOCK = threading.Lock()
_MASTER_TOPOLOGY_FIELDS = {
    "use_master_valve",
    "master_mqtt_topic",
    "master_mode",
    "master_mqtt_server_id",
}
_WATER_TOPOLOGY_FIELDS = {
    "use_water_meter",
    "water_mqtt_topic",
    "water_mqtt_server_id",
    "water_pulse_size",
    "water_base_value_m3",
    "water_base_pulses",
}


def _group_change_is_throttled(group_id: int, window_sec: float = GROUP_DEBOUNCE_SEC) -> bool:
    now = time.time()
    with _GROUP_GUARD_LOCK:
        last = _GROUP_CHANGE_GUARD.get(group_id, 0)
        return now - last < window_sec


def _mark_group_change(group_id: int) -> None:
    with _GROUP_GUARD_LOCK:
        _GROUP_CHANGE_GUARD[int(group_id)] = time.time()


def _clear_group_change(group_id: int) -> None:
    with _GROUP_GUARD_LOCK:
        _GROUP_CHANGE_GUARD.pop(int(group_id), None)


def _serialize_group_hardware_mutation(*, always: bool = False):
    """Serialize topology/master actions with the core group state machine."""

    def decorate(fn):
        @wraps(fn)
        def wrapped(group_id, *args, **kwargs):
            body = request.get_json(silent=True)
            hardware_fields = _MASTER_TOPOLOGY_FIELDS | _WATER_TOPOLOGY_FIELDS
            needs_lock = always or (isinstance(body, dict) and bool(set(body) & hardware_fields))
            if needs_lock:
                with group_lock(int(group_id)):
                    return fn(group_id, *args, **kwargs)
            return fn(group_id, *args, **kwargs)

        return wrapped

    return decorate


def _serialize_rain_config_mutation(fn):
    """Serialize one group rain flag update with the global rain runtime."""

    @wraps(fn)
    def wrapped(group_id, *args, **kwargs):
        body = request.get_json(silent=True)
        if isinstance(body, dict) and "use_rain_sensor" in body:
            from services.monitors import rain_config_transaction_lock

            with rain_config_transaction_lock():
                return fn(group_id, *args, **kwargs)
        return fn(group_id, *args, **kwargs)

    return wrapped


def _get_group(group_id: int) -> dict | None:
    return next(
        (group for group in (db.get_groups() or []) if int(group.get("id") or 0) == int(group_id)),
        None,
    )


def _strict_groups() -> list[dict]:
    loader = getattr(db, "get_groups_strict", None)
    return loader() if callable(loader) else db.get_groups()


def _strict_zones() -> list[dict]:
    loader = getattr(db, "get_zones_strict", None)
    return loader() if callable(loader) else db.get_zones()


def _strict_group_zone_ids(group_id: int) -> list[int]:
    return sorted(
        int(zone["id"]) for zone in (_strict_zones() or []) if int(zone.get("group_id") or 0) == int(group_id)
    )


def _get_group_strict(group_id: int) -> dict | None:
    return next(
        (group for group in (_strict_groups() or []) if int(group.get("id") or 0) == int(group_id)),
        None,
    )


def _get_mqtt_server_strict(server_id: int) -> dict | None:
    loader = getattr(db, "get_mqtt_server_strict", None)
    return loader(int(server_id)) if callable(loader) else db.get_mqtt_server(int(server_id))


def _reconfigure_water_monitor() -> bool:
    """Apply committed group topology to the long-lived meter runtime."""
    try:
        from services.monitors import water_monitor

        reconfigure = getattr(water_monitor, "reconfigure", None)
        if not callable(reconfigure):
            logger.error("WaterMonitor does not expose reconfigure")
            return False
        if reconfigure() is not True:
            logger.error("WaterMonitor rejected committed group reconfiguration")
            return False
        return True
    except (ConnectionError, OSError, RuntimeError, sqlite3.Error, TypeError, ValueError):
        logger.exception("WaterMonitor reconfiguration failed after group mutation")
        return False


def _enforce_rain_group(group_id: int) -> bool:
    """Apply one newly opted-in group to the live fail-closed rain gate."""
    try:
        from services.monitors import rain_monitor

        enforce = getattr(rain_monitor, "enforce_group", None)
        if not callable(enforce):
            logger.error("RainMonitor does not expose enforce_group")
            return False
        return enforce(int(group_id)) is True
    except (ConnectionError, OSError, RuntimeError, sqlite3.Error, TypeError, ValueError):
        logger.exception("RainMonitor group enforcement failed for group %s", group_id)
        return False


def _activate_manual_master_open(
    group_id: int,
    server_id: int,
    topic: str,
    mode: str,
    publish_command,
) -> bool:
    """Delegate manual OPEN exclusively to the activation-bound core path."""
    try:
        from services.zone_control import activate_manual_master_open

        return (
            activate_manual_master_open(
                int(group_id),
                int(server_id),
                topic,
                mode,
                publish_command,
                hours=24,
            )
            is True
        )
    except (ConnectionError, ImportError, OSError, RuntimeError, TypeError, ValueError):
        logger.exception("activation-bound manual master OPEN failed group=%s", group_id)
        return False


def _close_master_valve_confirmed(
    server_id: int,
    topic: str,
    mode: str,
    publish_command,
) -> bool:
    """Require core fresh CLOSE echo and exact activation-cap cleanup."""
    try:
        from services.zone_control import close_master_valve_confirmed

        return close_master_valve_confirmed(int(server_id), topic, mode, publish_command) is True
    except (ConnectionError, ImportError, OSError, RuntimeError, TypeError, ValueError):
        logger.exception("confirmed master CLOSE failed identity=%s:%s", server_id, topic)
        return False


def _zone_is_physically_safe(zone: dict) -> bool:
    state = str(zone.get("state") or "").strip().lower()
    commanded = str(zone.get("commanded_state") or "").strip().lower()
    observed = str(zone.get("observed_state") or "").strip().lower()
    return state == "off" and commanded == "off" and observed == "off"


def _validated_group_stop_result(result, group_id: int, expected_ids: list[int]) -> dict | None:
    """Accept only the scheduler's exact physical-and-job stop contract."""
    required_keys = {
        "success",
        "group_id",
        "aggregate_valid",
        "stopped",
        "unresolved",
        "unverified_zone_ids",
        "retry_scheduled",
    }
    if not isinstance(result, dict) or set(result) != required_keys:
        return None
    if type(result["group_id"]) is not int or result["group_id"] != int(group_id):
        return None
    if any(type(result[key]) is not bool for key in ("success", "aggregate_valid", "retry_scheduled")):
        return None

    normalized: dict[str, list[int]] = {}
    for key in ("stopped", "unresolved", "unverified_zone_ids"):
        values = result[key]
        if not isinstance(values, list):
            return None
        if any(type(zone_id) is not int or zone_id <= 0 for zone_id in values):
            return None
        if values != sorted(set(values)):
            return None
        normalized[key] = list(values)

    stopped = set(normalized["stopped"])
    unresolved = set(normalized["unresolved"])
    unverified = set(normalized["unverified_zone_ids"])
    expected = set(expected_ids)
    if result["aggregate_valid"]:
        if unverified or stopped & unresolved or stopped | unresolved != expected:
            return None
        if result["success"] is not (not unresolved):
            return None
        if result["retry_scheduled"] and not unresolved:
            return None
    else:
        if result["success"] or stopped or unresolved or unverified != expected or result["retry_scheduled"]:
            return None

    return {
        "success": result["success"],
        "aggregate_valid": result["aggregate_valid"],
        "stopped": normalized["stopped"],
        "unresolved": normalized["unresolved"],
        "unverified_zone_ids": normalized["unverified_zone_ids"],
        "retry_scheduled": result["retry_scheduled"],
    }


def _canonical_master_topic(value, *, allow_empty: bool) -> tuple[str | None, str | None]:
    """Validate one master-valve report/base topic from public configuration."""
    if value is not None and not isinstance(value, str):
        return None, "MQTT-топик мастер-клапана должен быть строкой"
    raw = str(value or "").strip()
    if not raw:
        return ("", None) if allow_empty else (None, "Нужен MQTT-топик мастер-клапана")
    if "\x00" in raw or "+" in raw or "#" in raw:
        return None, "MQTT-топик мастер-клапана не должен содержать NUL или wildcard"
    collapsed = "/" + raw.lstrip("/")
    if collapsed == "/" or collapsed.endswith("/on"):
        return None, "Нужен report-топик мастер-клапана, не root и не /on command-топик"
    try:
        if len(collapsed.encode("utf-8")) > 65_535:
            return None, "MQTT-топик мастер-клапана слишком длинный"
    except UnicodeEncodeError:
        return None, "MQTT-топик мастер-клапана должен быть корректным UTF-8"
    canonical = normalize_topic(collapsed)
    if not canonical:
        return None, "Некорректный MQTT-топик мастер-клапана"
    return canonical, None


def _master_identity(group: dict) -> tuple[int, str] | None:
    topic, topic_error = _canonical_master_topic(group.get("master_mqtt_topic"), allow_empty=False)
    server_id = group.get("master_mqtt_server_id")
    if topic_error or not topic or not server_id:
        return None
    return int(server_id), topic


def _groups_sharing_master(group: dict) -> list[dict]:
    identity = _master_identity(group)
    if identity is None:
        return []
    shared = []
    for candidate in _strict_groups() or []:
        if int(candidate.get("use_master_valve") or 0) != 1:
            continue
        if _master_identity(candidate) == identity:
            shared.append(candidate)
    return shared


def _master_has_unsafe_zone(group: dict) -> bool:
    all_zones = _strict_zones() or []
    for shared_group in _groups_sharing_master(group):
        zones = [zone for zone in all_zones if int(zone.get("group_id") or 0) == int(shared_group["id"])]
        if any(not _zone_is_physically_safe(zone) for zone in zones):
            return True
    return False


def _master_command_locked(
    server_id: int,
    topic: str,
    server: dict,
    value: str,
    *,
    mode: str,
    close_guard: dict | None = None,
) -> tuple[bool, bool]:
    """Fresh-close through core, rechecking shared-zone safety under its lock."""
    blocked = False

    def _publish_if_still_safe() -> bool:
        nonlocal blocked
        # Core calls this only after taking the physical-identity lock and
        # preparing the fresh report subscription. Recheck at the last safe
        # point before the CLOSE publish.
        if close_guard is not None and _master_has_unsafe_zone(close_guard):
            blocked = True
            return False
        return _publish_mqtt_value(
            server,
            topic,
            value,
            min_interval_sec=0.0,
            qos=2,
            retain=True,
        )

    confirmed = _close_master_valve_confirmed(
        int(server_id),
        topic,
        mode,
        _publish_if_still_safe,
    )
    return confirmed, blocked


def _confirm_master_closed(group: dict) -> tuple[bool, str | None]:
    """Require a fresh physical close echo before a mapping can change."""
    if int(group.get("use_master_valve") or 0) != 1:
        return True, None
    if _master_has_unsafe_zone(group):
        return False, "Нельзя изменить мастер-клапан: связанная зона физически не подтверждена как OFF"
    identity = _master_identity(group)
    if identity is None:
        return False, "Нельзя закрыть старый мастер-клапан: неполная MQTT-конфигурация"
    server_id, topic = identity
    server = _get_mqtt_server_strict(server_id)
    if not server:
        return False, "Нельзя закрыть старый мастер-клапан: MQTT-сервер не найден"
    mode = str(group.get("master_mode") or "NC").strip().upper()
    close_value = "1" if mode == "NO" else "0"
    blocked = False
    try:
        closed, blocked = _master_command_locked(
            server_id,
            topic,
            server,
            close_value,
            mode=mode,
            close_guard=group,
        )
    except (ConnectionError, ImportError, OSError, RuntimeError, TimeoutError, TypeError, ValueError, sqlite3.Error):
        logger.exception("old master close failed for group %s", group.get("id"))
        closed = False
    if blocked:
        return False, "Нельзя изменить мастер-клапан: связанная зона запускается или физически не OFF"
    if not closed:
        return False, "Старый мастер-клапан не подтвердил закрытие"
    return True, None


@groups_api_bp.route("/api/groups")
def api_groups():
    groups = db.get_groups()
    return jsonify(groups)


@groups_api_bp.route("/api/groups/<int:group_id>", methods=["PUT"])
@audit_log("group_save", target_extractor=lambda *a, **kw: f"group:{kw.get('group_id', a[0] if a else '?')}")
@_serialize_rain_config_mutation
@_serialize_group_hardware_mutation(always=True)
def api_update_group(group_id):
    data = request.get_json() or {}
    if not isinstance(data, dict):
        return jsonify({"success": False, "message": "Некорректные данные группы"}), 400
    try:
        all_groups = _strict_groups() or []
        current = next(
            (group for group in all_groups if int(group.get("id") or 0) == int(group_id)),
            None,
        )
    except (ConnectionError, OSError, RuntimeError, sqlite3.Error, TypeError, ValueError):
        logger.exception("group topology preflight unavailable for group %s", group_id)
        return jsonify({"success": False, "message": "Проверка топологии недоступна"}), 503
    if not current:
        return jsonify({"success": False, "message": "Группа не найдена"}), 404
    before_snapshot = db.get_group_storage_snapshot(int(group_id))
    if before_snapshot is None:
        return jsonify({"success": False, "message": "Не удалось зафиксировать состояние группы"}), 503
    # This exact at-rest row also guards the eventual write.  Using the
    # separately loaded aggregate row here could close or rewrite stale
    # master wiring if another configuration writer won the race.
    current = dict(before_snapshot)

    if "master_mqtt_topic" in data:
        canonical_master_topic, topic_error = _canonical_master_topic(
            data.get("master_mqtt_topic"),
            allow_empty=True,
        )
        if topic_error:
            return jsonify({"success": False, "message": topic_error}), 400
        data = dict(data)
        data["master_mqtt_topic"] = canonical_master_topic

    fields_map = {
        "use_master_valve": lambda v: 1 if v else 0,
        "master_mqtt_topic": lambda v: (v or "").strip(),
        "master_mode": lambda v: str(v or "NC").strip().upper(),
        "master_mqtt_server_id": lambda v: int(v) if v not in (None, "") else None,
        "use_pressure_sensor": lambda v: 1 if v else 0,
        "pressure_mqtt_topic": lambda v: (v or "").strip(),
        "pressure_unit": lambda v: str(v or "bar").strip(),
        "pressure_mqtt_server_id": lambda v: int(v) if v not in (None, "") else None,
        "use_water_meter": lambda v: 1 if v else 0,
        "water_mqtt_topic": lambda v: (v or "").strip(),
        "water_mqtt_server_id": lambda v: int(v) if v not in (None, "") else None,
        "water_pulse_size": lambda v: str(v or "1l") if str(v or "1l") in ("1l", "10l", "100l") else "1l",
        "water_base_value_m3": lambda v: float(v) if v not in (None, "") else 0.0,
        "water_base_pulses": lambda v: int(v) if v not in (None, "") else 0,
        "master_close_delay_sec": lambda v: max(1, min(3600, int(v or 60))),
    }
    updates = {}
    if "name" in data:
        name = str(data.get("name") or "").strip()
        if not name:
            return jsonify({"success": False, "message": "Название группы не должно быть пустым"}), 400
        if any(
            int(group.get("id") or 0) != int(group_id) and str(group.get("name") or "") == name for group in all_groups
        ):
            return jsonify({"success": False, "message": "Группа с таким названием уже существует"}), 400
        updates["name"] = name
    if "use_rain_sensor" in data:
        updates["use_rain_sensor"] = 1 if data.get("use_rain_sensor") else 0
    for field, normalise in fields_map.items():
        if field not in data:
            continue
        try:
            updates[field] = normalise(data.get(field))
        except (AttributeError, TypeError, ValueError):
            return jsonify({"success": False, "message": f"Некорректное поле {field}"}), 400
    if not updates:
        return jsonify({"success": False, "message": "Нет изменений"}), 400

    if updates.get("master_mode", current.get("master_mode")) not in ("NC", "NO"):
        return jsonify({"success": False, "message": "master_mode должен быть NC или NO"}), 400
    if str(updates.get("pressure_unit", current.get("pressure_unit") or "bar")).lower() not in (
        "bar",
        "kpa",
        "psi",
    ):
        return jsonify({"success": False, "message": "pressure_unit должен быть bar|kPa|psi"}), 400

    for server_field in (
        "master_mqtt_server_id",
        "pressure_mqtt_server_id",
        "water_mqtt_server_id",
    ):
        server_id = updates.get(server_field) if server_field in updates else current.get(server_field)
        if server_field in updates and server_id is not None:
            try:
                server_exists = _get_mqtt_server_strict(int(server_id)) is not None
            except (ConnectionError, OSError, RuntimeError, sqlite3.Error, TypeError, ValueError):
                logger.exception("MQTT reference validation unavailable for group %s", group_id)
                return jsonify({"success": False, "message": "Проверка MQTT-ссылок недоступна"}), 503
            if not server_exists:
                return jsonify({"success": False, "message": f"{server_field}: MQTT-сервер не найден"}), 400

    final_use_master = int(updates.get("use_master_valve", current.get("use_master_valve") or 0)) == 1
    if final_use_master:
        final_topic = updates.get("master_mqtt_topic", current.get("master_mqtt_topic"))
        final_server_id = updates.get("master_mqtt_server_id", current.get("master_mqtt_server_id"))
        _canonical_final_topic, topic_error = _canonical_master_topic(final_topic, allow_empty=False)
        if topic_error:
            return jsonify({"success": False, "message": topic_error}), 400
        try:
            final_server_exists = bool(final_server_id) and _get_mqtt_server_strict(int(final_server_id)) is not None
        except (ConnectionError, OSError, RuntimeError, sqlite3.Error, TypeError, ValueError):
            logger.exception("master MQTT reference validation unavailable for group %s", group_id)
            return jsonify({"success": False, "message": "Проверка MQTT-ссылок недоступна"}), 503
        if not final_server_exists:
            return jsonify({"success": False, "message": "Нужен корректный MQTT-сервер для мастер-клапана"}), 400

    master_topology_changed = any(
        field in updates and updates[field] != current.get(field) for field in _MASTER_TOPOLOGY_FIELDS
    )
    water_topology_changed = any(
        field in updates and updates[field] != current.get(field) for field in _WATER_TOPOLOGY_FIELDS
    )
    rain_opt_in = (
        "use_rain_sensor" in updates
        and int(current.get("use_rain_sensor") or 0) != 1
        and int(updates["use_rain_sensor"] or 0) == 1
    )
    if master_topology_changed:
        try:
            group_zones = [zone for zone in (_strict_zones() or []) if int(zone.get("group_id") or 0) == int(group_id)]
        except (ConnectionError, OSError, RuntimeError, sqlite3.Error, TypeError, ValueError):
            logger.exception("group zone safety scan unavailable for group %s", group_id)
            return jsonify({"success": False, "message": "Проверка топологии недоступна"}), 503
        if any(not _zone_is_physically_safe(zone) for zone in group_zones):
            return jsonify(
                {"success": False, "message": "Топологию master valve нельзя менять до подтверждённого OFF"}
            ), 409
        try:
            closed, close_error = _confirm_master_closed(current)
        except (ConnectionError, OSError, RuntimeError, sqlite3.Error, TypeError, ValueError):
            logger.exception("old master confirmation unavailable for group %s", group_id)
            return jsonify({"success": False, "message": "Проверка мастер-клапана недоступна"}), 503
        if not closed:
            return jsonify({"success": False, "message": close_error, "error_code": "MASTER_CONFIRMATION_TIMEOUT"}), 503
        final_group = {**current, **updates}
        try:
            confirmed, confirmation_error = _confirm_master_closed(final_group)
        except (ConnectionError, OSError, RuntimeError, sqlite3.Error, TypeError, ValueError):
            logger.exception("new master confirmation unavailable for group %s", group_id)
            return jsonify({"success": False, "message": "Проверка мастер-клапана недоступна"}), 503
        if not confirmed:
            return jsonify(
                {
                    "success": False,
                    "message": confirmation_error,
                    "error_code": "MASTER_CONFIRMATION_TIMEOUT",
                }
            ), 503
        # The topology change invalidates the old observation.  Only SSE hub's
        # fresh base-topic echo may set a new physical state afterwards.
        updates["master_valve_observed"] = None

    committed_snapshot = db.update_group_config_with_snapshot(
        int(group_id),
        updates,
        expected_current=before_snapshot,
        allow_observed_drift=True,
    )
    if committed_snapshot is None:
        return jsonify(
            {
                "success": False,
                "message": "Конфигурация группы изменилась конкурентно; повторите запрос",
                "error_code": "GROUP_UPDATE_CONFLICT",
            }
        ), 409

    if water_topology_changed and not _reconfigure_water_monitor():
        rollback_snapshot = dict(before_snapshot)
        if master_topology_changed:
            rollback_snapshot["master_valve_observed"] = None
        restored = db.restore_group_snapshot(
            rollback_snapshot,
            expected_current=committed_snapshot,
            allow_observed_drift=master_topology_changed,
        )
        if not restored:
            logger.critical("WaterMonitor rollback CAS failed for group %s", group_id)
            return jsonify(
                {
                    "success": False,
                    "message": "Runtime не применён, а конфигурация была изменена конкурентно",
                    "error_code": "WATER_MONITOR_ROLLBACK_CONFLICT",
                }
            ), 500
        return jsonify(
            {
                "success": False,
                "message": "WaterMonitor не принял конфигурацию; изменения отменены",
                "error_code": "WATER_MONITOR_RECONFIGURE_FAILED",
            }
        ), 409
    if rain_opt_in and not _enforce_rain_group(int(group_id)):
        rollback_snapshot = dict(before_snapshot)
        if master_topology_changed:
            rollback_snapshot["master_valve_observed"] = None
        restored = db.restore_group_snapshot(
            rollback_snapshot,
            expected_current=committed_snapshot,
            allow_observed_drift=master_topology_changed,
        )
        runtime_restored = True
        if restored and water_topology_changed:
            # WaterMonitor already accepted the committed combined update.
            # Rebind it to the exact restored DB snapshot before responding.
            runtime_restored = _reconfigure_water_monitor()
        if not restored or not runtime_restored:
            logger.critical(
                "Rain group enforcement rollback failed group=%s db=%s water_runtime=%s",
                group_id,
                restored,
                runtime_restored,
            )
            return jsonify(
                {
                    "success": False,
                    "message": "Rain safety enforcement не завершён, безопасный rollback не выполнен",
                    "error_code": "RAIN_GROUP_ENFORCEMENT_ROLLBACK_CONFLICT",
                }
            ), 500
        return jsonify(
            {
                "success": False,
                "message": "Rain safety enforcement не завершён; изменение отменено",
                "error_code": "RAIN_GROUP_ENFORCEMENT_FAILED",
            }
        ), 409
    if master_topology_changed:
        _sse_hub.reload_hub()
    try:
        db.add_log("group_edit", json.dumps({"group": group_id, **updates}))
    except (json.JSONDecodeError, KeyError, TypeError, ValueError) as e:
        logger.debug("group edit log failed: %s", e)
    return jsonify({"success": True})


@groups_api_bp.route("/api/groups", methods=["POST"])
@audit_log("group_create", target_extractor=lambda *a, **kw: "group:new")
def api_create_group():
    data = request.get_json() or {}
    name = data.get("name") or "Новая группа"
    group = db.create_group(name)
    if group:
        db.add_log("group_create", json.dumps({"group": group["id"], "name": name}))
        return jsonify(group), 201
    return jsonify({"success": False, "message": "Не удалось создать группу"}), 400


@groups_api_bp.route("/api/groups/<int:group_id>", methods=["DELETE"])
@audit_log("group_delete", target_extractor=lambda *a, **kw: f"group:{kw.get('group_id', a[0] if a else '?')}")
@_serialize_group_hardware_mutation(always=True)
def api_delete_group(group_id):
    try:
        group = _get_group_strict(group_id)
        before_snapshot = db.get_group_storage_snapshot(int(group_id))
        group_zones = [zone for zone in (_strict_zones() or []) if int(zone.get("group_id") or 0) == int(group_id)]
    except (ConnectionError, OSError, RuntimeError, sqlite3.Error, TypeError, ValueError):
        logger.exception("group delete topology preflight unavailable for group %s", group_id)
        return jsonify({"success": False, "message": "Проверка топологии недоступна"}), 503
    if not group:
        return jsonify({"success": False, "message": "Группа не найдена"}), 404
    if before_snapshot is None:
        return jsonify({"success": False, "message": "Не удалось зафиксировать состояние группы"}), 503
    group = dict(before_snapshot)
    if int(group_id) == 999:
        # Preserve the existing group-999 API contract while performing the
        # new topology checks for ordinary groups.
        return jsonify(
            {"success": False, "message": "Нельзя удалить группу: переместите или удалите зоны этой группы"}
        ), 400
    if group_zones:
        return jsonify(
            {"success": False, "message": "Нельзя удалить группу: переместите или удалите зоны этой группы"}
        ), 400
    try:
        closed, close_error = _confirm_master_closed(group)
    except (ConnectionError, OSError, RuntimeError, sqlite3.Error, TypeError, ValueError):
        logger.exception("master delete confirmation unavailable for group %s", group_id)
        return jsonify({"success": False, "message": "Проверка мастер-клапана недоступна"}), 503
    if not closed:
        return jsonify({"success": False, "message": close_error, "error_code": "MASTER_CONFIRMATION_TIMEOUT"}), 503
    if db.delete_group_if_unchanged(before_snapshot, allow_observed_drift=True):
        if int(group.get("use_water_meter") or 0) == 1 and not _reconfigure_water_monitor():
            rollback_snapshot = dict(before_snapshot)
            if int(group.get("use_master_valve") or 0) == 1:
                rollback_snapshot["master_valve_observed"] = None
            if not db.restore_group_snapshot(rollback_snapshot, expected_current=None):
                logger.critical("WaterMonitor delete rollback failed for group %s", group_id)
                return jsonify(
                    {
                        "success": False,
                        "message": "Runtime не применён, восстановление группы не удалось",
                        "error_code": "WATER_MONITOR_ROLLBACK_FAILED",
                    }
                ), 500
            return jsonify(
                {
                    "success": False,
                    "message": "WaterMonitor не принял удаление; группа восстановлена",
                    "error_code": "WATER_MONITOR_RECONFIGURE_FAILED",
                }
            ), 409
        db.add_log("group_delete", json.dumps({"group": group_id}))
        if _master_identity(group) is not None:
            _sse_hub.reload_hub()
        return ("", 204)
    return jsonify(
        {
            "success": False,
            "message": "Группа изменилась конкурентно или получила зоны; повторите запрос",
            "error_code": "GROUP_DELETE_CONFLICT",
        }
    ), 409


@groups_api_bp.route("/api/groups/<int:group_id>/stop", methods=["POST"])
@audit_log("group_stop", target_extractor=lambda *a, **kw: f"group:{kw.get('group_id', a[0] if a else '?')}")
def api_stop_group(group_id):
    """Stop all zones in group."""
    try:
        try:
            scheduler = get_scheduler()
            cancel_group_jobs = getattr(scheduler, "cancel_group_jobs", None)
            if not callable(cancel_group_jobs):
                raise RuntimeError("scheduler group cancellation is unavailable")
            stop_result = cancel_group_jobs(int(group_id))
        except Exception:
            logger.exception("group stop: scheduler cancellation failed")
            try:
                expected_zone_ids = _strict_group_zone_ids(int(group_id))
            except (ConnectionError, OSError, RuntimeError, sqlite3.Error, KeyError, TypeError, ValueError):
                logger.exception("group stop inventory unavailable after cancellation failure group=%s", group_id)
                expected_zone_ids = []
            return jsonify(
                {
                    "success": False,
                    "message": "Не удалось безопасно остановить группу и снять задания",
                    "stopped": [],
                    "unresolved": expected_zone_ids,
                    "unverified_zone_ids": expected_zone_ids,
                }
            ), 503

        try:
            expected_zone_ids = _strict_group_zone_ids(int(group_id))
        except (ConnectionError, OSError, RuntimeError, sqlite3.Error, KeyError, TypeError, ValueError):
            logger.exception("group stop inventory unavailable group=%s", group_id)
            return jsonify(
                {
                    "success": False,
                    "message": "Не удалось проверить состав группы",
                    "stopped": [],
                    "unresolved": [],
                    "unverified_zone_ids": [],
                }
            ), 503

        validated_stop = _validated_group_stop_result(stop_result, int(group_id), expected_zone_ids)
        if validated_stop is None:
            logger.error("group stop returned malformed partition group_id=%s result=%r", group_id, stop_result)
            return jsonify(
                {
                    "success": False,
                    "message": "Не удалось подтвердить остановку всех зон группы",
                    "stopped": [],
                    "unresolved": expected_zone_ids,
                    "unverified_zone_ids": expected_zone_ids,
                }
            ), 503
        if validated_stop["aggregate_valid"] is not True or validated_stop["success"] is not True:
            logger.error(
                "group stop remains unresolved: group_id=%s unresolved=%s unverified=%s",
                group_id,
                validated_stop["unresolved"],
                validated_stop["unverified_zone_ids"],
            )
            return jsonify(
                {
                    "success": False,
                    "message": "Не все зоны и задания группы подтверждены безопасными",
                    "stopped": validated_stop["stopped"],
                    "unresolved": validated_stop["unresolved"],
                    "unverified_zone_ids": validated_stop["unverified_zone_ids"],
                    "retry_scheduled": validated_stop["retry_scheduled"],
                }
            ), 503

        # Issue #16 §3.5: emit a session_aborted_by_user audit row so
        # a single query on action_type='session_aborted_by_user'
        # lists all user-driven aborts regardless of which button
        # was pressed (zone-card stop, group-card stop).
        try:
            from services.audit import record_audit

            record_audit(
                action_type="session_aborted_by_user",
                source="group_stop",
                target=f"group:{int(group_id)}",
                payload={"endpoint": "api_stop_group"},
                actor="user",
            )
        except Exception:
            logger.exception("session_aborted_by_user audit failed")
        try:
            db.clear_group_scheduled_starts(group_id)
        except (sqlite3.Error, OSError) as e:
            logger.debug("Handled exception in api_stop_group: %s", e)

        # The scheduler cancellation above terminates only the active group
        # session. Do not write a day-wide program cancellation here: it would
        # also suppress later ``extra_times`` slots that the user did not stop.
        try:
            db.reschedule_group_to_next_program(group_id)
        except (sqlite3.Error, OSError) as e:
            logger.debug("Handled exception in line_177: %s", e)

        db.add_log("group_stop", json.dumps({"group": group_id}))
        return jsonify({"success": True, "message": f"Группа {group_id} остановлена"})
    except (json.JSONDecodeError, KeyError, TypeError, ValueError) as e:
        logger.error(f"Ошибка остановки группы {group_id}: {e}")
        return jsonify({"success": False, "message": "Ошибка остановки группы"}), 500


def _compute_next_zone_id(group_id: int, current_zone_id: int):
    """Best-effort: read zones.scheduled_start_time for this group, return the
    next zone after current_zone_id by plan order. None if current is last or
    no plan exists."""
    try:
        zones = db.get_zones() or []
        gz = [z for z in zones if int(z.get("group_id") or 0) == int(group_id) and z.get("scheduled_start_time")]
        if not gz:
            return None
        gz.sort(key=lambda z: str(z.get("scheduled_start_time") or ""))
        zids = [int(z["id"]) for z in gz]
        if int(current_zone_id) not in zids:
            return None
        idx = zids.index(int(current_zone_id))
        if idx + 1 < len(zids):
            return zids[idx + 1]
        return None
    except (sqlite3.Error, OSError, ValueError, TypeError, KeyError) as e:
        logger.debug("compute_next_zone_id error: %s", e)
        return None


@groups_api_bp.route("/api/groups/<int:group_id>/skip-current", methods=["POST"])
@audit_log("zone_skip", target_extractor=lambda *a, **kw: f"group:{kw.get('group_id', a[0] if a else '?')}")
def api_skip_current_zone(group_id):
    """Skip the currently running zone in the group's sequence; next zone starts now."""
    try:
        group = next((g for g in (db.get_groups() or []) if int(g["id"]) == int(group_id)), None)
        if not group:
            return jsonify({"success": False, "message": "Группа не найдена"}), 404

        scheduler = get_scheduler()
        if not scheduler:
            return jsonify({"success": False, "message": "Планировщик недоступен"}), 500
        if not scheduler.is_group_session_active(int(group_id)):
            return jsonify({"success": False, "message": "Нет активного полива в группе"}), 400

        # Capture "current" + "next" from authoritative state BEFORE setting the event.
        zones = db.get_zones() or []
        active = [z for z in zones if int(z.get("group_id") or 0) == int(group_id) and z.get("state") == "on"]
        if not active:
            return jsonify({"success": False, "message": "Нет активной зоны для пропуска"}), 400
        current_zone_id = int(active[0]["id"])
        next_zone_id = _compute_next_zone_id(int(group_id), current_zone_id)

        scheduled = scheduler.request_skip_current_zone(int(group_id))
        if scheduled == "debounced":
            # Issue #14 C2: server-side debounce — second skip request for
            # the same group arrived within 1.0s of the previous successful
            # one. Frontend 1500ms guard is bypassable (multi-tab, scripted
            # callers); this is the authoritative throttle.
            return jsonify(
                {
                    "success": False,
                    "message": "Слишком частые запросы — подождите секунду",
                }
            ), 429
        if scheduled != "ok":
            return jsonify({"success": False, "message": "Нет активного полива в группе"}), 400

        try:
            db.add_log(
                "zone_skip",
                json.dumps(
                    {
                        "group_id": int(group_id),
                        "zone_id": current_zone_id,
                        "next_zone_id": next_zone_id,
                        "source": "manual",
                    }
                ),
            )
        except (json.JSONDecodeError, KeyError, TypeError, ValueError) as e:
            logger.debug("zone_skip api log: %s", e)
        return jsonify(
            {
                "success": True,
                "skipped_zone_id": current_zone_id,
                "next_zone_id": next_zone_id,
            }
        )
    except (json.JSONDecodeError, KeyError, TypeError, ValueError) as e:
        logger.error(f"Ошибка пропуска зоны в группе {group_id}: {e}")
        return jsonify({"success": False, "message": "Ошибка пропуска зоны"}), 500


@groups_api_bp.route("/api/groups/<int:group_id>/start-from-first", methods=["POST"])
@audit_log(
    "group_start_from_first", target_extractor=lambda *a, **kw: f"group:{kw.get('group_id', a[0] if a else '?')}"
)
def api_start_group_from_first(group_id):
    """Start sequential watering of the group from the first zone."""
    try:
        if current_app.config.get("EMERGENCY_STOP"):
            return jsonify(
                {"success": False, "message": "Аварийная остановка активна. Сначала отключите аварийный режим."}
            ), 400
        scheduler = get_scheduler()
        if not scheduler:
            try:
                scheduler = init_scheduler(db)
            except (ValueError, KeyError, RuntimeError) as e:
                logger.debug("Exception in api_start_group_from_first: %s", e)
                scheduler = None
        if not scheduler:
            return jsonify({"success": False, "message": "Планировщик недоступен"}), 500
        body = request.get_json(silent=True) or {}
        # Issue #12 C2: "minutes wins if both sent" — strict. If
        # override_duration is present in the body AT ALL, that is the
        # user's intent. Accept (1..120) or reject the whole request (400).
        # Never silently fall through to percent.
        override_dur_raw = body.get("override_duration")
        minutes_sent = override_dur_raw is not None
        override_dur = None
        if minutes_sent:
            try:
                override_dur = int(override_dur_raw)
            except (ValueError, TypeError):
                return jsonify({"success": False, "message": "override_duration должен быть целым числом 1..120"}), 400
            if not (1 <= override_dur <= 120):
                return jsonify(
                    {"success": False, "message": "override_duration должен быть в диапазоне 1..120 мин"}
                ), 400
        # Issue #12: optional duration_percent (one of PERCENT_PRESETS).
        # Only honoured when minutes mode is absent. Anything outside the
        # whitelist is silently ignored — defensive, mirrors 1..120 minutes
        # validation behaviour for non-whitelist values.
        override_pct = None
        if not minutes_sent:
            req_pct = body.get("duration_percent")
            if req_pct is not None:
                try:
                    from services.zone_control import PERCENT_PRESETS

                    p = int(req_pct)
                    if p in PERCENT_PRESETS:
                        override_pct = p
                except (ValueError, TypeError):
                    override_pct = None
        # Issue #12 C1: pre-compute warnings (deduped) so the response
        # mirrors the single-zone endpoint contract. Cheap: same helper
        # the scheduler uses, just called once per group zone here for
        # surface-able tags. Order is sorted for deterministic output.
        warnings: list = []
        if override_pct is not None:
            try:
                from services.zone_control import per_zone_dur as _per_zone_dur

                zones = db.get_zones() or []
                group_zones = [z for z in zones if z.get("group_id") == group_id]
                wset: set = set()
                for z in group_zones:
                    _d, _w = _per_zone_dur(z, override_dur, override_pct)
                    for tag in _w:
                        wset.add(tag)
                warnings = sorted(wset)
            except (ValueError, TypeError, KeyError, AttributeError) as e:
                logger.debug("group warnings preflight failed: %s", e)
                warnings = []
        # Issue #31: manual=True — bypass weather skip for user-initiated runs.
        ok = scheduler.start_group_sequence(
            group_id, override_duration=override_dur, override_percent=override_pct, manual=True
        )
        if not ok:
            return jsonify({"success": False, "message": "Не удалось запустить последовательный полив группы"}), 400
        try:
            db.add_log("group_start_from_first", json.dumps({"group": group_id}))
        except (json.JSONDecodeError, KeyError, TypeError, ValueError) as e:
            logger.debug("Handled exception in api_start_group_from_first: %s", e)
        return jsonify(
            {"success": True, "message": f"Группа {group_id}: запущен последовательный полив", "warnings": warnings}
        )
    except (json.JSONDecodeError, KeyError, TypeError, ValueError) as e:
        logger.error(f"Ошибка запуска группы {group_id} с первой зоны: {e}")
        return jsonify({"success": False, "message": "Ошибка запуска группы"}), 500


@groups_api_bp.route("/api/groups/<int:group_id>/start-zone/<int:zone_id>", methods=["POST"])
@audit_log(
    "zone_start_exclusive", target_extractor=lambda *a, **kw: f"zone:{kw.get('zone_id', a[1] if len(a) > 1 else '?')}"
)
def api_start_zone_exclusive(group_id, zone_id):
    """Start a zone, stopping all others in the group."""
    try:
        if current_app.config.get("EMERGENCY_STOP"):
            return jsonify(
                {"success": False, "message": "Аварийная остановка активна. Сначала отключите аварийный режим."}
            ), 400
        if not _get_group(int(group_id)):
            return jsonify({"success": False, "message": "Группа не найдена"}), 404
        zone = db.get_zone(int(zone_id))
        if not zone:
            return jsonify({"success": False, "message": "Зона не найдена"}), 404
        if int(zone.get("group_id") or 0) != int(group_id):
            return jsonify({"success": False, "message": f"Зона {zone_id} не принадлежит группе {group_id}"}), 409
        debounce_enabled = not current_app.config.get("TESTING") or current_app.config.get("GROUP_DEBOUNCE_IN_TESTS")
        with group_lock(int(group_id)):
            # The unlocked preflight is only a fast rejection. Topology edits
            # serialize on this group lock, so re-read membership while it is
            # held before the controller can consume the mutable zone row.
            zone = db.get_zone(int(zone_id))
            if not zone:
                return jsonify({"success": False, "message": "Зона не найдена"}), 404
            if int(zone.get("group_id") or 0) != int(group_id):
                return jsonify({"success": False, "message": f"Зона {zone_id} не принадлежит группе {group_id}"}), 409
            if debounce_enabled and _group_change_is_throttled(int(group_id)):
                latest = db.get_zone(int(zone_id)) or {}
                state = str(latest.get("state") or "").strip().lower()
                commanded = str(latest.get("commanded_state") or "").strip().lower()
                if state in {"starting", "on"} or commanded == "on":
                    return jsonify(
                        {"success": False, "message": "Группа уже обрабатывается", "error_code": "GROUP_BUSY"}
                    ), 429
                # A prior accepted marker without any active state is stale;
                # never turn it into a false 200 success.
                _clear_group_change(int(group_id))
            try:
                from services.zone_control import start_zone_orchestrated

                status, _ctx = start_zone_orchestrated(int(zone_id), restart_if_on=True)
            except (ValueError, TypeError, KeyError):
                logger.exception("exclusive_start failed")
                return jsonify({"success": False, "message": "Ошибка запуска зоны"}), 500
            if status in ("not_found", "failed"):
                return jsonify({"success": False, "message": "Не удалось запустить зону"}), 400
            if debounce_enabled:
                _mark_group_change(int(group_id))
        try:
            db.clear_scheduled_for_zone_group_peers(int(zone_id), int(group_id))
        except (sqlite3.Error, OSError) as e:
            logger.debug("Handled exception in api_start_zone_exclusive: %s", e)
        db.add_log("zone_start_exclusive", json.dumps({"group": group_id, "zone": zone_id}))
        return jsonify({"success": True, "message": f"Зона {zone_id} запущена, остальные остановлены"})
    except (json.JSONDecodeError, KeyError, TypeError, ValueError) as e:
        logger.error(f"Ошибка эксклюзивного запуска зоны {zone_id} в группе {group_id}: {e}")
        return jsonify({"success": False, "message": "Ошибка запуска зоны"}), 500


@groups_api_bp.route("/api/groups/<int:group_id>/master-valve/<action>", methods=["POST"])
@audit_log(
    "master_valve_toggle",
    target_extractor=lambda *a, **kw: (
        f"group:{kw.get('group_id', a[0] if a else '?')}:{kw.get('action', a[1] if len(a) > 1 else '?')}"
    ),
)
@_serialize_group_hardware_mutation(always=True)
def api_master_valve_toggle(group_id, action):
    try:
        action = str(action).strip().lower()
        if action not in {"open", "close"}:
            return jsonify({"success": False, "message": "action должен быть open или close"}), 400
        if current_app.config.get("EMERGENCY_STOP") and action == "open":
            return jsonify({"success": False, "message": "Аварийная остановка активна"}), 400
        try:
            g = _get_group_strict(group_id)
        except (ConnectionError, OSError, RuntimeError, sqlite3.Error, TypeError, ValueError):
            logger.exception("master valve topology preflight unavailable for group %s", group_id)
            return jsonify({"success": False, "message": "Проверка топологии недоступна"}), 503
        if not g:
            return jsonify({"success": False, "message": "Группа не найдена"}), 404
        try:
            if not bool(int(g.get("use_master_valve") or 0)):
                return jsonify({"success": False, "message": "Мастер-клапан не включён для группы"}), 400
        except (ValueError, TypeError, KeyError) as e:
            logger.debug("Exception in api_master_valve_toggle: %s", e)
            return jsonify({"success": False, "message": "Мастер-клапан не включён для группы"}), 400
        topic, topic_error = _canonical_master_topic(g.get("master_mqtt_topic"), allow_empty=False)
        server_id = g.get("master_mqtt_server_id")
        if topic_error or not topic or not server_id:
            return jsonify(
                {"success": False, "message": topic_error or "Не задан MQTT сервер или топик для мастер-клапана"}
            ), 400
        try:
            server = _get_mqtt_server_strict(int(server_id))
        except (ConnectionError, OSError, RuntimeError, sqlite3.Error, TypeError, ValueError):
            logger.exception("master MQTT lookup unavailable for group %s", group_id)
            return jsonify({"success": False, "message": "Проверка MQTT-сервера недоступна"}), 503
        if not server:
            return jsonify({"success": False, "message": "MQTT сервер не найден"}), 400
        mode = (g.get("master_mode") or "NC").upper().strip()
        want_open = action == "open"
        if not want_open:
            try:
                unsafe_zone = _master_has_unsafe_zone(g)
            except (ConnectionError, OSError, RuntimeError, sqlite3.Error, TypeError, ValueError):
                logger.exception("master close safety scan unavailable for group %s", group_id)
                return jsonify({"success": False, "message": "Проверка топологии недоступна"}), 503
            if unsafe_zone:
                return jsonify(
                    {
                        "success": False,
                        "message": "Нельзя закрыть мастер-клапан: связанная зона не подтверждена как OFF",
                    }
                ), 409
        val = ("0" if want_open else "1") if mode == "NO" else ("1" if want_open else "0")
        try:
            normalized_topic = topic
            if want_open:
                blocked = False
                ok = _activate_manual_master_open(
                    int(group_id),
                    int(server_id),
                    normalized_topic,
                    mode,
                    lambda: _verify_master_command(
                        int(server_id),
                        normalized_topic,
                        val,
                        lambda: _publish_mqtt_value(
                            server,
                            normalized_topic,
                            val,
                            min_interval_sec=0.0,
                            qos=2,
                            retain=True,
                        ),
                    ),
                )
            else:
                ok, blocked = _master_command_locked(
                    int(server_id),
                    normalized_topic,
                    server,
                    val,
                    mode=mode,
                    close_guard=g,
                )
        except sqlite3.Error:
            logger.exception("master valve safety scan failed")
            return jsonify({"success": False, "message": "Проверка топологии недоступна"}), 503
        except (ConnectionError, ImportError, RuntimeError, TimeoutError, OSError, TypeError, ValueError):
            logger.exception("master valve publish failed")
            return jsonify({"success": False, "message": "Не удалось отправить команду"}), 500
        if blocked:
            return jsonify(
                {
                    "success": False,
                    "message": "Нельзя закрыть мастер-клапан: связанная зона запускается или физически не OFF",
                }
            ), 409
        # Issue #38: publish_mqtt_value returns False if the base topic or
        # '/on' companion delivery is not confirmed. Don't lie to the UI/DB.
        if not ok:
            logger.warning("master valve physical echo timed out — gid=%s action=%s", group_id, action)
            return jsonify(
                {
                    "success": False,
                    "message": "Мастер-клапан не подтвердил состояние",
                    "error_code": "MASTER_CONFIRMATION_TIMEOUT",
                }
            ), 503
        return jsonify({"success": True, "confirmed": True})
    except (json.JSONDecodeError, KeyError, TypeError, ValueError) as e:
        logger.error(f"api_master_valve_toggle failed: {e}")
        return jsonify({"success": False, "message": "Ошибка"}), 500


# ---------------------------------------------------------------------------
# Issue #15 — POST /api/groups/<gid>/run-selected
#
# Ad-hoc multi-zone run inside a single group. Goes through the same
# IrrigationScheduler.start_group_sequence path as ``start-from-first`` —
# the only thing that's "ad-hoc-specific" is the negative ``program_id``
# sentinel and the explicit ``zone_ids`` subset.
#
# Body:
#   {
#     "zones": [int, int, ...],         # required, non-empty
#     "duration": 1..120,                # optional, minutes mode
#     "duration_percent": 10..200,       # optional, percent mode (minutes wins)
#   }
#
# NOTE: zones table currently has no ``enabled`` column on this branch
# (db/migrations.py — id/state/name/icon/duration/group_id/topic/...).
# Spec §6.1 → drop the "is enabled" check; only validate exists + group.
# ---------------------------------------------------------------------------
def _parse_run_overrides(body: dict):
    """Return ``(override_duration, override_percent, error_message_or_None)``.

    Contract (mirrors PR #21 / issue #12):
      - ``duration`` int in [1, 120] — "minutes mode"; if BOTH given, minutes win.
      - ``duration_percent`` int in [10, 200] — "percent mode".
      - Invalid values → 400-style error message; do NOT silently fall back.
    """
    raw_dur = body.get("duration")
    raw_pct = body.get("duration_percent")

    parsed_dur = None
    if raw_dur is not None:
        try:
            d = int(raw_dur)
        except (ValueError, TypeError):
            return None, None, "duration должна быть целым числом 1..120"
        if not (1 <= d <= 120):
            return None, None, "duration вне диапазона 1..120"
        parsed_dur = d

    parsed_pct = None
    if raw_pct is not None:
        try:
            p = int(raw_pct)
        except (ValueError, TypeError):
            return None, None, "duration_percent должна быть целым числом 10..200"
        if not (10 <= p <= 200):
            return None, None, "duration_percent вне диапазона 10..200"
        parsed_pct = p

    # "Minutes wins" — if both given, drop percent.
    if parsed_dur is not None and parsed_pct is not None:
        parsed_pct = None
    return parsed_dur, parsed_pct, None


def _build_ad_hoc_name(zone_ids, override_dur, override_pct) -> str:
    """Compact human label for audit / history."""
    z_part = ", ".join(f"Z{int(z)}" for z in zone_ids[:6])
    if len(zone_ids) > 6:
        z_part += f", …+{len(zone_ids) - 6}"
    if override_dur is not None:
        suffix = f"{int(override_dur)} мин"
    elif override_pct is not None:
        suffix = f"{int(override_pct)}% от нормы"
    else:
        suffix = "нормы"
    return f"Ad-hoc: {z_part} ({suffix})"


@groups_api_bp.route("/api/groups/<int:gid>/run-selected", methods=["POST"])
@rate_limit("programs", max_requests=10, window_sec=60)
@audit_log("prog_manual_run_selected", target_extractor=lambda *a, **kw: f"group:{kw.get('gid', a[0] if a else '?')}")
def api_run_selected(gid):
    """Ad-hoc run of a selected subset of zones in one group (issue #15)."""
    try:
        if current_app.config.get("EMERGENCY_STOP"):
            return jsonify(
                {"success": False, "message": "Аварийная остановка активна. Сначала отключите аварийный режим."}
            ), 400
        group = next((g for g in (db.get_groups() or []) if int(g["id"]) == int(gid)), None)
        if not group:
            return jsonify({"success": False, "message": "Группа не найдена"}), 404

        body = request.get_json(silent=True) or {}
        raw_zones = body.get("zones")
        if not isinstance(raw_zones, list) or not raw_zones:
            return jsonify({"success": False, "message": "zones обязательны"}), 400
        try:
            zone_ids = [int(z) for z in raw_zones]
        except (ValueError, TypeError):
            return jsonify({"success": False, "message": "zones должны быть int[]"}), 400

        # Per-zone validation: exists + belongs to gid.
        # NB: spec §6.1 — no `enabled` column in zones schema, skip that check.
        all_zones = {int(z["id"]): z for z in (db.get_zones() or [])}
        for zid in zone_ids:
            z = all_zones.get(int(zid))
            if z is None:
                return jsonify({"success": False, "message": f"Зона {zid} не найдена"}), 400
            if int(z.get("group_id") or 0) != int(gid):
                return jsonify({"success": False, "message": f"Зона {zid} не принадлежит группе {gid}"}), 400

        override_dur, override_pct, parse_err = _parse_run_overrides(body)
        if parse_err is not None:
            return jsonify({"success": False, "message": parse_err}), 400

        scheduler = get_scheduler()
        if not scheduler:
            try:
                scheduler = init_scheduler(db)
            except (ValueError, KeyError, RuntimeError) as e:
                logger.debug("api_run_selected: init_scheduler failed: %s", e)
                scheduler = None
        if not scheduler:
            return jsonify({"success": False, "message": "Планировщик недоступен"}), 500

        # Negative sentinel — distinguishes ad-hoc runs in audit/history.
        # See spec §1.4. timestamp() is second-resolution; collisions are
        # benign (audit row PK still distinguishes them).
        ad_hoc_id = -int(time.time())
        ad_hoc_name = _build_ad_hoc_name(zone_ids, override_dur, override_pct)

        # Issue #31: manual=True — bypass weather skip for user-initiated runs.
        ok = scheduler.start_group_sequence(
            int(gid),
            override_duration=override_dur,
            override_percent=override_pct,
            zone_ids=zone_ids,
            ad_hoc_program_id=ad_hoc_id,
            ad_hoc_program_name=ad_hoc_name,
            manual=True,
        )
        if not ok:
            return jsonify({"success": False, "message": "Не удалось запустить"}), 400

        try:
            db.add_log(
                "prog_manual_run_selected",
                json.dumps(
                    {
                        "group_id": int(gid),
                        "zones": zone_ids,
                        "ad_hoc_program_id": ad_hoc_id,
                        "override_duration": override_dur,
                        "override_percent": override_pct,
                    }
                ),
            )
        except (json.JSONDecodeError, KeyError, TypeError, ValueError) as e:
            logger.debug("api_run_selected: add_log failed: %s", e)

        return jsonify(
            {
                "success": True,
                "message": f"Группа {group.get('name')}: запущены {len(zone_ids)} зон(ы)",
                "ad_hoc_program_id": ad_hoc_id,
            }
        )
    except (json.JSONDecodeError, KeyError, TypeError, ValueError) as e:
        logger.error(f"api_run_selected failed for group {gid}: {e}")
        return jsonify({"success": False, "message": "Ошибка запуска выбранных зон"}), 500
