"""
Минимальные маршруты Telegram-бота.

Требования:
- Главное меню с одной кнопкой «Группы»
- Меню «Группы»: список групп
- Экран группы: две кнопки — «Запустить» и «Остановить»
"""

from typing import Optional, Tuple, List, Dict
import json
from datetime import datetime, timedelta

from database import db

_notifier = None  # инжектируется из services/telegram_bot.py


def set_notifier(n) -> None:
    global _notifier
    _notifier = n


# ---------- helpers ----------

def _btn(text: str, data: str) -> Dict:
    return {"text": text, "callback_data": data}


def _inline_markup(rows: List[List[Dict]]) -> dict:
    return {"inline_keyboard": rows}


def _cb_decode(data: str) -> Dict:
    # Плоский и предсказуемый формат: menu:*, group:<id>, group_start:<id>, group_stop:<id>
    if not isinstance(data, str):
        return {}
    if data.startswith('menu:'):
        return {"t": "menu", "a": data.split(':', 1)[1]}
    if data.startswith('group_start:'):
        try:
            return {"t": "group_start", "gid": int(data.split(':', 1)[1])}
        except Exception:
            return {}
    if data.startswith('group_stop:'):
        try:
            return {"t": "group_stop", "gid": int(data.split(':', 1)[1])}
        except Exception:
            return {}
    if data.startswith('group:'):
        try:
            return {"t": "group", "gid": int(data.split(':', 1)[1])}
        except Exception:
            return {}
    # Совместимость со старым форматом
    if data.startswith('groupsel:'):
        try:
            return {"t": "group", "gid": int(data.split(':', 1)[1])}
        except Exception:
            return {}
    if data.startswith('postpone:'):
        try:
            _, gid, days = data.split(':', 2)
            return {"t": "postpone", "gid": int(gid), "days": int(days)}
        except Exception:
            return {}
    # JSON совместимость (на будущее)
    try:
        jd = json.loads(data)
        return jd if isinstance(jd, dict) else {}
    except Exception:
        return {}


# ---------- экраны ----------

def _screen_main_menu() -> Tuple[str, dict]:
    rows = [[_btn('Группы', 'menu:groups')]]
    return 'Главное меню:', _inline_markup(rows)


def _screen_groups_list() -> Tuple[str, dict]:
    groups = db.list_groups_min() or []
    if not groups:
        return 'Группы не найдены.', _inline_markup([[ _btn('⬅️ Назад', 'menu:root') ]])

    rows: List[List[Dict]] = []
    row: List[Dict] = []
    for g in groups:
        row.append(_btn(str(g.get('name') or f"#{g.get('id')}") , f"group:{int(g['id'])}"))
        if len(row) == 2:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    rows.append([_btn('⬅️ Назад', 'menu:root')])
    return 'Выберите группу:', _inline_markup(rows)


def _screen_group_actions(group_id: int) -> Tuple[str, dict]:
    # Безопасно получаем название группы
    g = {}
    try:
        gl = db.list_groups_min() or []
        g = next((gg for gg in gl if int(gg.get('id')) == int(group_id)), {})
    except Exception:
        g = {}
    name = g.get('name') or f"#{group_id}"
    rows = [
        [_btn('▶ Запустить', f'group_start:{int(group_id)}')],
        [_btn('⏹ Остановить', f'group_stop:{int(group_id)}')],
        [
            _btn('⏰ Отложить 1 день', f'postpone:{int(group_id)}:1'),
            _btn('2 дня', f'postpone:{int(group_id)}:2'),
            _btn('3 дня', f'postpone:{int(group_id)}:3'),
        ],
        [_btn('⬅️ К группам', 'menu:groups')],
    ]
    return f"Группа {name} (id={int(group_id)})", _inline_markup(rows)


# ---------- действия ----------

def _do_group_start(group_id: int) -> str:
    try:
        from irrigation_scheduler import get_scheduler
        s = get_scheduler()
        if not s:
            return 'Планировщик недоступен'
        ok = s.start_group_sequence(int(group_id))
        return '▶ Запущен полив группы' if ok else 'Не удалось запустить'
    except Exception:
        return 'Ошибка запуска группы'


def _do_group_stop(group_id: int) -> str:
    try:
        from services.zone_control import stop_all_in_group
        stop_all_in_group(int(group_id), reason='telegram', force=True)
        return '⏹ Полив группы остановлен'
    except Exception:
        return 'Ошибка остановки группы'


def _do_group_postpone(group_id: int, days: int) -> str:
    try:
        days = int(days)
        until_date = datetime.now() + timedelta(days=days)
        postpone_until = until_date.strftime('%Y-%m-%d 23:59:59')
        zones = db.get_zones() or []
        for z in zones:
            try:
                if int(z.get('group_id') or 0) == int(group_id):
                    db.update_zone_postpone(int(z['id']), postpone_until, 'manual')
            except Exception:
                pass
        try:
            from services.zone_control import stop_all_in_group
            stop_all_in_group(int(group_id), reason='manual_postpone', force=True)
        except Exception:
            pass
        return f'⏰ Полив отложен на {days} дн. до {postpone_until}'
    except Exception:
        return 'Ошибка отложки группы'


# ---------- роутер ----------

def process_callback_json(chat_id: int, jd: Dict, message_id: Optional[int] = None) -> None:
    if _notifier is None:
        return

    t = jd.get('t')

    if t == 'menu':
        a = jd.get('a')
        if a in (None, 'root'):
            text, markup = _screen_main_menu()
        elif a == 'groups':
            text, markup = _screen_groups_list()
        else:
            text, markup = _screen_main_menu()
        if message_id:
            _notifier.edit_message_text(chat_id, message_id, text, reply_markup=markup)
        else:
            _notifier.send_message(chat_id, text, reply_markup=markup)
        return

    if t == 'group':
        gid = int(jd.get('gid'))
        text, markup = _screen_group_actions(gid)
        if message_id:
            _notifier.edit_message_text(chat_id, message_id, text, reply_markup=markup)
        else:
            _notifier.send_message(chat_id, text, reply_markup=markup)
        return

    if t == 'group_start':
        gid = int(jd.get('gid'))
        notice = _do_group_start(gid)
        text, markup = _screen_group_actions(gid)
        if message_id:
            _notifier.edit_message_text(chat_id, message_id, text, reply_markup=markup)
        else:
            _notifier.send_message(chat_id, text, reply_markup=markup)
        _notifier.send_text(chat_id, notice)
        return

    if t == 'group_stop':
        gid = int(jd.get('gid'))
        notice = _do_group_stop(gid)
        text, markup = _screen_group_actions(gid)
        if message_id:
            _notifier.edit_message_text(chat_id, message_id, text, reply_markup=markup)
        else:
            _notifier.send_message(chat_id, text, reply_markup=markup)
        _notifier.send_text(chat_id, notice)
        return

    if t == 'postpone':
        gid = int(jd.get('gid'))
        days = int(jd.get('days') or 1)
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