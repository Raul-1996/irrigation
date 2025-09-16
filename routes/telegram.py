from flask import Blueprint, request, jsonify
from database import db
from werkzeug.security import check_password_hash
from datetime import datetime, timedelta
from services.telegram_bot import notifier
from services.reports import build_report_text
from services import events as evt
import time

_RL_CACHE = {}
_RL_LIMIT = 10  # cmds/min per chat

def _rate_limited(chat_id: int) -> bool:
    now = time.time()
    win = 60.0
    t0, n = _RL_CACHE.get(chat_id, (now, 0))
    if now - t0 > win:
        _RL_CACHE[chat_id] = (now, 1)
        return False
    if n >= _RL_LIMIT:
        return True
    _RL_CACHE[chat_id] = (t0, n+1)
    return False

def _send(chat_id: int, text: str):
    try:
        notifier.send_text(int(chat_id), text)
    except Exception:
        pass

telegram_bp = Blueprint('telegram_bp', __name__)

@telegram_bp.route('/telegram/webhook/<secret>', methods=['POST'])
def telegram_webhook(secret):
    # Basic secret check
    expected = db.get_setting_value('telegram_webhook_secret_path') or ''
    if str(secret) != str(expected):
        return jsonify({'ok': False}), 403
    update = request.get_json(silent=True) or {}
    msg = update.get('message') or {}
    chat = msg.get('chat') or {}
    text = msg.get('text') or ''
    chat_id = chat.get('id')
    username = chat.get('username')
    first_name = chat.get('first_name')
    if not chat_id:
        return jsonify({'ok': True})
    db.upsert_bot_user(int(chat_id), username, first_name)
    # Simple commands: /start, /auth <pwd>
    if text.startswith('/start'):
        _send(chat_id, 'Привет! Это WB-Irrigation. Для доступа отправьте команду /auth <пароль>.')
        return jsonify({'ok': True})
    if text.startswith('/auth'):
        parts = text.split(maxsplit=1)
        if len(parts) == 2:
            pwd = parts[1].strip()
            h = db.get_setting_value('telegram_access_password_hash')
            if h and check_password_hash(h, pwd):
                db.set_bot_user_authorized(int(chat_id), role='user')
                _send(chat_id, 'Готово. Доступ предоставлен. Введите /menu.')
                return jsonify({'ok': True})
            else:
                failed = db.inc_bot_user_failed(int(chat_id))
                if failed >= 5:
                    until = (datetime.now() + timedelta(hours=1)).strftime('%Y-%m-%d %H:%M:%S')
                    db.lock_bot_user_until(int(chat_id), until)
                _send(chat_id, f'Пароль неверный. Осталось попыток: {max(0, 5-failed)}')
                return jsonify({'ok': True})
    # rate limit
    if _rate_limited(int(chat_id)):
        _send(chat_id, 'Слишком часто. Повторите позже.')
        return jsonify({'ok': True})
    user = db.get_bot_user_by_chat(int(chat_id)) or {}
    if not user or not int(user.get('is_authorized') or 0):
        _send(chat_id, 'Нет доступа. Авторизуйтесь: /auth <пароль>')
        return jsonify({'ok': True})
    # Basic commands
    if text.startswith('/help'):
        _send(chat_id, '/menu, /groups, /zones <group>, /group_start <id>, /group_stop <id>, /zone_start <id>, /zone_stop <id>, /report today')
        return jsonify({'ok': True})
    if text.startswith('/menu'):
        _send(chat_id, 'Меню: /groups, /zones <group>, /report today|7|30, /subscribe, /unsubscribe')
        return jsonify({'ok': True})
    if text.startswith('/groups'):
        gl = db.list_groups_min()
        txt = 'Группы:\n' + '\n'.join([f"{g['id']}: {g['name']}" for g in gl])
        _send(chat_id, txt)
        return jsonify({'ok': True})
    if text.startswith('/zones'):
        parts = text.split()
        gid = int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else None
        if not gid:
            _send(chat_id, 'Используйте: /zones <group_id>')
            return jsonify({'ok': True})
        zl = db.list_zones_by_group_min(gid)
        txt = f'Зоны группы {gid}:\n' + '\n'.join([f"{z['id']}: {z['name']} ({z['state']})" for z in zl])
        _send(chat_id, txt)
        return jsonify({'ok': True})
    if text.startswith('/group_start'):
        parts = text.split()
        if len(parts) > 1 and parts[1].isdigit():
            gid = int(parts[1])
            try:
                from irrigation_scheduler import get_scheduler
                s = get_scheduler()
                if s:
                    s.start_group_sequence(gid)
                _send(chat_id, f'▶ Группа {gid} запущена')
                evt.publish({'type':'group_start','id':gid,'by':'telegram'})
            except Exception:
                _send(chat_id, 'Ошибка запуска группы')
        return jsonify({'ok': True})
    if text.startswith('/group_stop'):
        parts = text.split()
        if len(parts) > 1 and parts[1].isdigit():
            gid = int(parts[1])
            try:
                from services.zone_control import stop_all_in_group
                stop_all_in_group(gid, reason='telegram')
                _send(chat_id, f'⏹ Группа {gid} остановлена')
                evt.publish({'type':'group_stop','id':gid,'by':'telegram'})
            except Exception:
                _send(chat_id, 'Ошибка остановки группы')
        return jsonify({'ok': True})
    if text.startswith('/zone_start'):
        parts = text.split()
        if len(parts) > 1 and parts[1].isdigit():
            zid = int(parts[1])
            try:
                from services.zone_control import exclusive_start_zone
                exclusive_start_zone(zid)
                _send(chat_id, f'▶ Зона {zid} запущена')
                evt.publish({'type':'zone_start','id':zid,'by':'telegram'})
            except Exception:
                _send(chat_id, 'Ошибка запуска зоны')
        return jsonify({'ok': True})
    if text.startswith('/zone_stop'):
        parts = text.split()
        if len(parts) > 1 and parts[1].isdigit():
            zid = int(parts[1])
            try:
                from services.zone_control import stop_zone
                stop_zone(zid, reason='telegram')
                _send(chat_id, f'⏹ Зона {zid} остановлена')
                evt.publish({'type':'zone_stop','id':zid,'by':'telegram'})
            except Exception:
                _send(chat_id, 'Ошибка остановки зоны')
        return jsonify({'ok': True})
    if text.startswith('/report'):
        period = 'today'
        parts = text.split()
        if len(parts) > 1:
            period = parts[1]
        txt = build_report_text(period=period, fmt='brief')
        _send(chat_id, txt)
        return jsonify({'ok': True})
    if text.startswith('/whoami'):
        _send(chat_id, f"chat_id={chat_id}, role={user.get('role','user')}")
        return jsonify({'ok': True})
    if text.startswith('/emergency_stop'):
        if str(user.get('role','user')) != 'admin':
            _send(chat_id, 'Нет прав')
            return jsonify({'ok': True})
        try:
            from app import app as _app
            with _app.test_request_context():
                from app import api_emergency_stop as _es
                _es()
            _send(chat_id, '🚨 Аварийная остановка активирована')
        except Exception:
            _send(chat_id, 'Ошибка аварийной остановки')
        return jsonify({'ok': True})
    if text.startswith('/emergency_resume'):
        if str(user.get('role','user')) != 'admin':
            _send(chat_id, 'Нет прав')
            return jsonify({'ok': True})
        try:
            from app import app as _app
            with _app.test_request_context():
                from app import api_emergency_resume as _er
                _er()
            _send(chat_id, '✅ Аварийная остановка снята')
        except Exception:
            _send(chat_id, 'Ошибка снятия аварийной остановки')
        return jsonify({'ok': True})
    if text.startswith('/broadcast'):
        if str(user.get('role','user')) != 'admin':
            _send(chat_id, 'Нет прав')
            return jsonify({'ok': True})
        msg = text[len('/broadcast'):].strip()
        if not msg:
            _send(chat_id, 'Текст пуст')
            return jsonify({'ok': True})
        try:
            # naive: broadcast to all authorized users
            import sqlite3
            with sqlite3.connect(db.db_path, timeout=5) as conn:
                conn.row_factory = sqlite3.Row
                cur = conn.execute('SELECT chat_id FROM bot_users WHERE is_authorized=1')
                for r in cur.fetchall():
                    try:
                        notifier.send_text(int(r['chat_id']), msg)
                    except Exception:
                        pass
            _send(chat_id, 'Рассылка выполнена')
        except Exception:
            _send(chat_id, 'Ошибка рассылки')
        return jsonify({'ok': True})
    if text.startswith('/subscribe'):
        # /subscribe daily brief 08:00   or weekly full 09:00 1111100
        try:
            parts = text.split()
            stype = parts[1] if len(parts)>1 else 'daily'
            sformat = parts[2] if len(parts)>2 else 'brief'
            time_local = parts[3] if len(parts)>3 else '08:00'
            dow = parts[4] if (len(parts)>4 and stype=='weekly') else None
        except Exception:
            stype, sformat, time_local, dow = 'daily','brief','08:00',None
        # map chat->user
        u = db.get_bot_user_by_chat(int(chat_id))
        if u:
            db.create_or_update_subscription(int(u.get('id')), stype, sformat, time_local, dow, True)
            _send(chat_id, 'Подписка сохранена')
        return jsonify({'ok': True})
    if text.startswith('/unsubscribe'):
        u = db.get_bot_user_by_chat(int(chat_id))
        if u:
            # disable all
            try:
                db.create_or_update_subscription(int(u.get('id')), 'daily', 'brief', '08:00', None, False)
                db.create_or_update_subscription(int(u.get('id')), 'weekly', 'brief', '08:00', '1111111', False)
            except Exception:
                pass
        _send(chat_id, 'Подписки отключены')
        return jsonify({'ok': True})
    return jsonify({'ok': True})

