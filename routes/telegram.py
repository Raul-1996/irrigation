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


def _do_group_stop(group_id: int) -> str:
    try:
        from irrigation_scheduler import get_scheduler

        scheduler = get_scheduler()
        if scheduler:
            scheduler.cancel_group_jobs(int(group_id))
        else:
            from services.zone_control import stop_all_in_group

            stop_all_in_group(int(group_id), reason="telegram", force=True)
        return "⏹ Полив группы остановлен"
    except ImportError as e:
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
