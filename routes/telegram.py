"""
Минимальные маршруты Telegram-бота.

Требования:
- Главное меню с одной кнопкой «Группы»
- Меню «Группы»: список групп
- Экран группы: две кнопки — «Запустить» и «Остановить»
"""

import json
import logging
import sqlite3
from datetime import datetime, timedelta

from database import db

logger = logging.getLogger(__name__)

_notifier = None  # инжектируется из services/telegram_bot.py


def set_notifier(n) -> None:
    global _notifier
    _notifier = n


# ---------- helpers ----------


def _btn(text: str, data: str) -> dict:
    return {"text": text, "callback_data": data}


def _inline_markup(rows: list[list[dict]]) -> dict:
    return {"inline_keyboard": rows}


def _cb_decode(data: str) -> dict:
    # Плоский и предсказуемый формат: menu:*, group:<id>, group_start:<id>, group_stop:<id>
    if not isinstance(data, str):
        return {}
    if data.startswith("menu:"):
        return {"t": "menu", "a": data.split(":", 1)[1]}
    if data.startswith("group_start:"):
        try:
            return {"t": "group_start", "gid": int(data.split(":", 1)[1])}
        except (ValueError, TypeError, KeyError) as e:
            logger.debug("Exception in _cb_decode: %s", e)
            return {}
    if data.startswith("group_stop:"):
        try:
            return {"t": "group_stop", "gid": int(data.split(":", 1)[1])}
        except (ValueError, TypeError, KeyError) as e:
            logger.debug("Exception in _cb_decode: %s", e)
            return {}
    if data.startswith("group:"):
        try:
            return {"t": "group", "gid": int(data.split(":", 1)[1])}
        except (ValueError, TypeError, KeyError) as e:
            logger.debug("Exception in _cb_decode: %s", e)
            return {}
    # Совместимость со старым форматом
    if data.startswith("groupsel:"):
        try:
            return {"t": "group", "gid": int(data.split(":", 1)[1])}
        except (ValueError, TypeError, KeyError) as e:
            logger.debug("Exception in _cb_decode: %s", e)
            return {}
    if data.startswith("postpone:"):
        try:
            _, gid, days = data.split(":", 2)
            return {"t": "postpone", "gid": int(gid), "days": int(days)}
        except (ValueError, TypeError, KeyError) as e:
            logger.debug("Exception in line_69: %s", e)
            return {}
    # JSON совместимость (на будущее)
    try:
        jd = json.loads(data)
        return jd if isinstance(jd, dict) else {}
    except (json.JSONDecodeError, KeyError, TypeError, ValueError) as e:
        logger.debug("Exception in line_76: %s", e)
        return {}


# ---------- экраны ----------


def _screen_main_menu() -> tuple[str, dict]:
    rows = [[_btn("Группы", "menu:groups")]]
    return "Главное меню:", _inline_markup(rows)


def _screen_groups_list() -> tuple[str, dict]:
    groups = db.list_groups_min() or []
    if not groups:
        return "Группы не найдены.", _inline_markup([[_btn("⬅️ Назад", "menu:root")]])

    rows: list[list[dict]] = []
    row: list[dict] = []
    for g in groups:
        row.append(_btn(str(g.get("name") or f"#{g.get('id')}"), f"group:{int(g['id'])}"))
        if len(row) == 2:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    rows.append([_btn("⬅️ Назад", "menu:root")])
    return "Выберите группу:", _inline_markup(rows)


def _screen_group_actions(group_id: int) -> tuple[str, dict]:
    # Безопасно получаем название группы
    g = {}
    try:
        gl = db.list_groups_min() or []
        g = next((gg for gg in gl if int(gg.get("id")) == int(group_id)), {})
    except (sqlite3.Error, OSError) as e:
        logger.debug("Exception in _screen_group_actions: %s", e)
        g = {}
    name = g.get("name") or f"#{group_id}"
    rows = [
        [_btn("▶ Запустить", f"group_start:{int(group_id)}")],
        [_btn("⏹ Остановить", f"group_stop:{int(group_id)}")],
        [
            _btn("⏰ Отложить 1 день", f"postpone:{int(group_id)}:1"),
            _btn("2 дня", f"postpone:{int(group_id)}:2"),
            _btn("3 дня", f"postpone:{int(group_id)}:3"),
        ],
        [_btn("⬅️ К группам", "menu:groups")],
    ]
    return f"Группа {name} (id={int(group_id)})", _inline_markup(rows)


# ---------- действия ----------


def _emergency_stop_active() -> bool:
    """Флаг аварийной остановки из app.config.

    Обработчики Telegram работают в потоке поллинга, вне request context,
    поэтому current_app здесь недоступен — читаем app напрямую.
    """
    try:
        from app import app as flask_app

        return bool(flask_app.config.get("EMERGENCY_STOP"))
    except ImportError:
        return False


def _do_group_start(group_id: int) -> str:
    try:
        if _emergency_stop_active():
            return "Аварийная остановка активна. Сначала отключите аварийный режим."
        from irrigation_scheduler import get_scheduler

        s = get_scheduler()
        if not s:
            return "Планировщик недоступен"
        # Issue #31: manual=True — Telegram-initiated run is user-initiated.
        ok = s.start_group_sequence(int(group_id), manual=True)
        return "▶ Запущен полив группы" if ok else "Не удалось запустить"
    except (ValueError, TypeError, RuntimeError) as e:
        logger.debug("Exception in _do_group_start: %s", e)
        return "Ошибка запуска группы"


def _zone_off_status(zone: dict, stop_result: bool | None) -> str:
    """Classify one stop as ``confirmed``, ``pending`` or ``failed``.

    ``observed_state`` is a stored snapshot and may predate this Telegram
    command.  Therefore an MQTT-backed zone cannot be called confirmed here;
    only the command-scoped verifier can establish a fresh physical echo.
    """
    state = str(zone.get("state") or "").strip().lower()
    commanded = str(zone.get("commanded_state") or "").strip().lower()
    if state == "stopping" and commanded == "off":
        return "pending"
    if stop_result is False or state != "off" or (commanded and commanded != "off"):
        return "failed"
    has_mqtt_channel = bool(zone.get("mqtt_server_id") and str(zone.get("topic") or "").strip())
    if not has_mqtt_channel:
        return "confirmed"
    observed = str(zone.get("observed_state") or "").strip().lower()
    if observed in ("on", "1", "true"):
        return "failed"
    return "pending"


def _read_group_zones_strict(group_id: int) -> list[dict]:
    """Read a complete group snapshot without repository fail-soft semantics."""
    with sqlite3.connect(db.db_path, timeout=5) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT * FROM zones WHERE group_id = ? ORDER BY id",
            (int(group_id),),
        ).fetchall()
    return [dict(row) for row in rows]


def _strict_group_zone_ids(group_id: int, zones: list[dict]) -> set[int]:
    """Validate that a DB snapshot is an exact, duplicate-free group partition."""
    gid = int(group_id)
    if type(zones) is not list:
        raise TypeError("group zone snapshot must be a list")
    zone_ids: set[int] = set()
    for zone in zones:
        if type(zone) is not dict:
            raise TypeError("group zone snapshot rows must be dictionaries")
        zone_id = zone.get("id")
        row_group_id = zone.get("group_id")
        if type(zone_id) is not int or zone_id <= 0 or type(row_group_id) is not int or row_group_id != gid:
            raise ValueError("invalid group zone snapshot row")
        if zone_id in zone_ids:
            raise ValueError("duplicate zone in group snapshot")
        zone_ids.add(zone_id)
    return zone_ids


def _parse_scheduler_group_stop(
    group_id: int,
    expected_zone_ids: set[int],
    summary: object,
) -> tuple[bool, set[int], set[int]]:
    """Validate the scheduler's exact seven-field physical OFF contract.

    Only an aggregate produced from the current complete group snapshot is
    trusted.  A malformed or unverified result is intentionally collapsed to
    ``valid=False`` so the caller can fail closed for every expected zone.
    """
    required_keys = {
        "success",
        "group_id",
        "aggregate_valid",
        "stopped",
        "unresolved",
        "unverified_zone_ids",
        "retry_scheduled",
    }
    if type(summary) is not dict or set(summary) != required_keys:
        return False, set(), set()
    if (
        type(summary["success"]) is not bool
        or type(summary["group_id"]) is not int
        or summary["group_id"] != int(group_id)
        or summary["aggregate_valid"] is not True
        or type(summary["stopped"]) is not list
        or type(summary["unresolved"]) is not list
        or type(summary["unverified_zone_ids"]) is not list
        or type(summary["retry_scheduled"]) is not bool
    ):
        return False, set(), set()

    raw_stopped = summary["stopped"]
    raw_unresolved = summary["unresolved"]
    raw_unverified = summary["unverified_zone_ids"]
    all_ids = raw_stopped + raw_unresolved + raw_unverified
    if any(type(zone_id) is not int or zone_id <= 0 for zone_id in all_ids):
        return False, set(), set()

    stopped = set(raw_stopped)
    unresolved = set(raw_unresolved)
    unverified = set(raw_unverified)
    if (
        len(stopped) != len(raw_stopped)
        or len(unresolved) != len(raw_unresolved)
        or len(unverified) != len(raw_unverified)
        or bool(stopped & unresolved)
        or bool(stopped & unverified)
        or bool(unresolved & unverified)
        or stopped | unresolved | unverified != set(expected_zone_ids)
        or bool(unverified)
    ):
        return False, set(), set()

    confirmed = not unresolved and stopped == set(expected_zone_ids)
    if summary["success"] is not confirmed:
        return False, set(), set()
    if confirmed and summary["retry_scheduled"] is not False:
        return False, set(), set()
    if summary["retry_scheduled"] and not unresolved:
        return False, set(), set()
    return True, stopped, unresolved


def _do_group_stop(group_id: int) -> str:
    try:
        from irrigation_scheduler import get_scheduler

        gid = int(group_id)
        scheduler = get_scheduler()
        stop_results: dict[int, bool] = {}
        scheduler_unresolved: set[int] = set()
        scheduler_failed = False
        zones: list[dict]
        if scheduler:
            summary = scheduler.cancel_group_jobs(gid)
            zones = _read_group_zones_strict(gid)
            expected_zone_ids = _strict_group_zone_ids(gid, zones)
            aggregate_valid, _stopped, scheduler_unresolved = _parse_scheduler_group_stop(
                gid,
                expected_zone_ids,
                summary,
            )
            scheduler_failed = not aggregate_valid or bool(scheduler_unresolved)
            if not aggregate_valid:
                scheduler_unresolved = set(expected_zone_ids)
        else:
            from services.zone_control import stop_zone

            # The group aggregate is deliberately best-effort and has no result.
            # Call the central per-zone primitive so a failed publish remains
            # visible to this operator-facing command.
            zones = _read_group_zones_strict(gid)
            _strict_group_zone_ids(gid, zones)
            for zone in zones:
                zone_id = int(zone["id"])
                try:
                    stop_results[zone_id] = bool(stop_zone(zone_id, reason="telegram", force=True))
                except (sqlite3.Error, OSError, RuntimeError, ValueError, TypeError):
                    logger.exception("Telegram stop failed for zone %s", zone_id)
                    stop_results[zone_id] = False

        # Both stop primitives are best-effort and historically returned no
        # aggregate result.  Re-read the authoritative state before telling an
        # operator that physical OFF succeeded.
        if scheduler is None:
            zones = _read_group_zones_strict(gid)
            _strict_group_zone_ids(gid, zones)
            statuses = {int(zone["id"]): _zone_off_status(zone, stop_results.get(int(zone["id"]))) for zone in zones}
            failed = [zone_id for zone_id, status in statuses.items() if status == "failed"]
            pending = [zone_id for zone_id, status in statuses.items() if status == "pending"]
        else:
            zone_by_id = {int(zone["id"]): zone for zone in zones}
            pending = sorted(
                zone_id for zone_id in scheduler_unresolved if _zone_off_status(zone_by_id[zone_id], None) == "pending"
            )
            failed = sorted(scheduler_unresolved - set(pending))

        if scheduler_failed and not failed and not pending:
            # A structured failure without IDs is still authoritative.  Attach
            # known group zones for retries; if the DB also has none, the generic
            # group-level error below remains visible to the operator.
            failed = [int(zone["id"]) for zone in zones]
        unresolved = failed + pending
        if not unresolved and not scheduler_failed:
            return "⏹ Полив группы остановлен"

        retry_at = datetime.now() + timedelta(seconds=15)
        scheduled_retry_count = 0
        retry_not_scheduled: list[int] = []
        if scheduler is not None and hasattr(scheduler, "schedule_zone_hard_stop"):
            for zone_id in unresolved:
                # Scheduler owns the activation-bound force=True callback.  Its
                # executable contract is pinned by
                # test_safety_stop_forces_mqtt_off_and_rearms_after_publish_failure.
                try:
                    if scheduler.schedule_zone_hard_stop(zone_id, retry_at) is True:
                        scheduled_retry_count += 1
                    else:
                        retry_not_scheduled.append(zone_id)
                except (sqlite3.Error, OSError, RuntimeError, ValueError, TypeError):
                    logger.exception("Telegram hard-stop retry scheduling failed for zone %s", zone_id)
                    retry_not_scheduled.append(zone_id)
        else:
            # Without APScheduler, retain observed-state retry publishes rather
            # than converting an unresolved command into a false success.
            from services.observed_state import state_verifier

            for zone_id in unresolved:
                state_verifier.verify_async(zone_id, "off")
                scheduled_retry_count += 1

        messages = []
        if failed:
            ids = ", ".join(str(zone_id) for zone_id in failed)
            messages.append(f"⚠️ OFF не подтверждён для зон: {ids}.")
        elif scheduler_failed:
            messages.append(f"⚠️ OFF не подтверждён для группы {int(group_id)}.")
        if pending:
            ids = ", ".join(str(zone_id) for zone_id in pending)
            messages.append(f"⏳ Команда OFF принята для зон: {ids}; ожидается свежее подтверждение реле.")
        if scheduled_retry_count == len(unresolved) and unresolved:
            messages.append("Повторная остановка запланирована.")
        elif retry_not_scheduled:
            ids = ", ".join(str(zone_id) for zone_id in retry_not_scheduled)
            messages.append(f"Повторная остановка не запланирована для зон: {ids}.")
        else:
            messages.append("Повторная остановка не запланирована: нет доступных целей.")
        return " ".join(messages)
    except (ImportError, sqlite3.Error, OSError, RuntimeError, ValueError, TypeError) as e:
        logger.debug("Exception in _do_group_stop: %s", e)
        return "Ошибка остановки группы"


def _do_group_postpone(group_id: int, days: int) -> str:
    try:
        from services.postpone import postpone_group

        res = postpone_group(int(group_id), int(days), source="telegram")
        if res.get("success") is not True:
            unresolved = ", ".join(str(zone_id) for zone_id in res.get("unresolved", []))
            unverified = ", ".join(str(zone_id) for zone_id in res.get("unverified_zone_ids", []))
            error_code = res.get("error_code")
            if error_code == "POSTPONE_STOP_UNAVAILABLE":
                detail = "результат физического подтверждения недоступен."
                if unverified:
                    detail += f" Зоны без проверенного результата: {unverified}."
            elif error_code == "POSTPONE_SESSION_NOT_QUIESCED":
                detail = "сессия полива не остановлена гарантированно."
                if unresolved:
                    detail += f" Зоны без подтверждения OFF: {unresolved}."
                if unverified:
                    detail += f" Зоны без проверенного результата: {unverified}."
            else:
                detail = "физическое выключение пока не подтверждено."
                if unresolved:
                    detail += f" Зоны без подтверждения: {unresolved}."
            retry_detail = (
                "Защитные повторы продолжаются."
                if res.get("retry_scheduled") is True
                else "Владелец защитного повтора не подтверждён; требуется немедленная проверка."
            )
            return f"⏳ Отсрочка установлена до {res['postpone_until']}, но {detail} {retry_detail}"
        return f"⏰ Полив отложен на {int(days)} дн. до {res['postpone_until']}"
    except (ValueError, TypeError, RuntimeError) as e:
        logger.debug("Exception in _do_group_postpone: %s", e)
        return "Ошибка отложки группы"


# ---------- роутер ----------


def process_callback_json(chat_id: int, jd: dict, message_id: int | None = None) -> None:
    if _notifier is None:
        return

    t = jd.get("t")

    if t == "menu":
        a = jd.get("a")
        if a in (None, "root"):
            text, markup = _screen_main_menu()
        elif a == "groups":
            text, markup = _screen_groups_list()
        else:
            text, markup = _screen_main_menu()
        if message_id:
            _notifier.edit_message_text(chat_id, message_id, text, reply_markup=markup)
        else:
            _notifier.send_message(chat_id, text, reply_markup=markup)
        return

    if t == "group":
        gid = int(jd.get("gid"))
        text, markup = _screen_group_actions(gid)
        if message_id:
            _notifier.edit_message_text(chat_id, message_id, text, reply_markup=markup)
        else:
            _notifier.send_message(chat_id, text, reply_markup=markup)
        return

    if t == "group_start":
        gid = int(jd.get("gid"))
        notice = _do_group_start(gid)
        text, markup = _screen_group_actions(gid)
        if message_id:
            _notifier.edit_message_text(chat_id, message_id, text, reply_markup=markup)
        else:
            _notifier.send_message(chat_id, text, reply_markup=markup)
        _notifier.send_text(chat_id, notice)
        return

    if t == "group_stop":
        gid = int(jd.get("gid"))
        notice = _do_group_stop(gid)
        text, markup = _screen_group_actions(gid)
        if message_id:
            _notifier.edit_message_text(chat_id, message_id, text, reply_markup=markup)
        else:
            _notifier.send_message(chat_id, text, reply_markup=markup)
        _notifier.send_text(chat_id, notice)
        return

    if t == "postpone":
        gid = int(jd.get("gid"))
        days = int(jd.get("days") or 1)
        notice = _do_group_postpone(gid, days)
        text, markup = _screen_group_actions(gid)
        if message_id:
            _notifier.edit_message_text(chat_id, message_id, text, reply_markup=markup)
        else:
            _notifier.send_message(chat_id, text, reply_markup=markup)
        _notifier.send_text(chat_id, notice)
        return

    # fallback -> главное меню
    text, markup = _screen_main_menu()
    if message_id:
        _notifier.edit_message_text(chat_id, message_id, text, reply_markup=markup)
    else:
        _notifier.send_message(chat_id, text, reply_markup=markup)
