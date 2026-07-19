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
    if stop_result is False or state != "off" or (commanded and commanded != "off"):
        return "failed"
    has_mqtt_channel = bool(zone.get("mqtt_server_id") and str(zone.get("topic") or "").strip())
    if not has_mqtt_channel:
        return "confirmed"
    observed = str(zone.get("observed_state") or "").strip().lower()
    if observed in ("on", "1", "true"):
        return "failed"
    return "pending"


def _do_group_stop(group_id: int) -> str:
    try:
        from irrigation_scheduler import get_scheduler

        scheduler = get_scheduler()
        stop_results: dict[int, bool] = {}
        scheduler_unresolved: list[int] = []
        scheduler_failed = False
        if scheduler:
            summary = scheduler.cancel_group_jobs(int(group_id))
            if isinstance(summary, dict):
                raw_unresolved = summary.get("unresolved") or []
                for item in raw_unresolved:
                    try:
                        value = item.get("zone_id", item.get("id")) if isinstance(item, dict) else item
                        scheduler_unresolved.append(int(value))
                    except (TypeError, ValueError):
                        logger.warning("Invalid unresolved zone in scheduler stop result: %r", item)
                scheduler_failed = summary.get("success") is False or bool(scheduler_unresolved)
        else:
            from services.zone_control import stop_zone

            # The group aggregate is deliberately best-effort and has no result.
            # Call the central per-zone primitive so a failed publish remains
            # visible to this operator-facing command.
            for zone in db.get_zones_by_group(int(group_id)) or []:
                zone_id = int(zone["id"])
                try:
                    stop_results[zone_id] = bool(stop_zone(zone_id, reason="telegram", force=True))
                except (sqlite3.Error, OSError, RuntimeError, ValueError, TypeError):
                    logger.exception("Telegram stop failed for zone %s", zone_id)
                    stop_results[zone_id] = False

        # Both stop primitives are best-effort and historically returned no
        # aggregate result.  Re-read the authoritative state before telling an
        # operator that physical OFF succeeded.
        zones = db.get_zones_by_group(int(group_id)) or []
        statuses = {
            int(zone["id"]): _zone_off_status(zone, stop_results.get(int(zone["id"])))
            for zone in zones
        }
        failed = list(
            dict.fromkeys(
                scheduler_unresolved
                + [zone_id for zone_id, status in statuses.items() if status == "failed"]
            )
        )
        if scheduler_failed and not failed:
            # A structured failure without IDs is still authoritative.  Attach
            # known group zones for retries; if the DB also has none, the generic
            # group-level error below remains visible to the operator.
            failed = list(statuses)
        pending = [
            zone_id
            for zone_id, status in statuses.items()
            if status == "pending" and zone_id not in failed
        ]
        unresolved = failed + pending
        if not unresolved and not scheduler_failed:
            return "⏹ Полив группы остановлен"

        retry_at = datetime.now() + timedelta(seconds=15)
        if scheduler is not None and hasattr(scheduler, "schedule_zone_hard_stop"):
            for zone_id in unresolved:
                # Scheduler owns the activation-bound force=True callback.  Its
                # executable contract is pinned by
                # test_safety_stop_forces_mqtt_off_and_rearms_after_publish_failure.
                scheduler.schedule_zone_hard_stop(zone_id, retry_at)
        else:
            # Without APScheduler, retain observed-state retry publishes rather
            # than converting an unresolved command into a false success.
            from services.observed_state import state_verifier

            for zone_id in unresolved:
                state_verifier.verify_async(zone_id, "off")

        messages = []
        if failed:
            ids = ", ".join(str(zone_id) for zone_id in failed)
            messages.append(f"⚠️ OFF не подтверждён для зон: {ids}.")
        elif scheduler_failed:
            messages.append(f"⚠️ OFF не подтверждён для группы {int(group_id)}.")
        if pending:
            ids = ", ".join(str(zone_id) for zone_id in pending)
            messages.append(f"⏳ Команда OFF принята для зон: {ids}; ожидается свежее подтверждение реле.")
        messages.append("Повторная остановка запланирована.")
        return " ".join(messages)
    except (ImportError, sqlite3.Error, OSError, RuntimeError, ValueError, TypeError) as e:
        logger.debug("Exception in _do_group_stop: %s", e)
        return "Ошибка остановки группы"


def _do_group_postpone(group_id: int, days: int) -> str:
    try:
        from services.postpone import postpone_group

        res = postpone_group(int(group_id), int(days), source="telegram")
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
