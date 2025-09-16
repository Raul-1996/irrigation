from flask import Blueprint, request, jsonify
from database import db
from werkzeug.security import check_password_hash
from datetime import datetime, timedelta
from services.telegram_bot import notifier
from services.reports import build_report_text
from services import events as evt
import time
InlineKeyboardButton = None
InlineKeyboardMarkup = None

_RL_CACHE = {}
_RL_LIMIT = 10  # cmds/min per chat
_CB_DEDUP = {}
_CB_TTL = 300.0  # 5 min

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

def _inline_markup(rows):
    try:
        return {'inline_keyboard': rows}
    except Exception:
        return None

def _get_role(chat_id: int) -> str:
    try:
        u = db.get_bot_user_by_chat(int(chat_id)) or {}
        r = str(u.get('role') or 'viewer')
        return r
    except Exception:
        return 'viewer'

def _role_allowed(current: str, required: str) -> bool:
    order = {'viewer': 0, 'user': 1, 'admin': 2}
    return order.get(current, 0) >= order.get(required, 0)

def _dedup_cb(chat_id: int, data: str) -> bool:
    # returns True if duplicated (should skip)
    now = time.time()
    key = f"{chat_id}:{data}"
    # cleanup
    try:
        if len(_CB_DEDUP) > 2048:
            dead = [k for k, ts in _CB_DEDUP.items() if now - ts > _CB_TTL]
            for k in dead:
                _CB_DEDUP.pop(k, None)
    except Exception:
        pass
    ts = _CB_DEDUP.get(key)
    if ts and (now - ts < 2.0):
        return True
    _CB_DEDUP[key] = now
    return False

telegram_bp = Blueprint('telegram_bp', __name__)

@telegram_bp.route('/telegram/webhook/<secret>', methods=['POST'])
def telegram_webhook(secret):
    # Basic secret check
    expected = db.get_setting_value('telegram_webhook_secret_path') or ''
    # Если секрет не задан — допускаем работу вебхука (упрощённый режим)
    if expected:
        if str(secret) != str(expected):
            return jsonify({'ok': False}), 403
    update = request.get_json(silent=True) or {}
    msg = update.get('message') or {}
    callback = update.get('callback_query') or {}
    chat = msg.get('chat') or {}
    text = msg.get('text') or ''
    chat_id = chat.get('id')
    username = chat.get('username')
    first_name = chat.get('first_name')
    if not chat_id:
        return jsonify({'ok': True})
    db.upsert_bot_user(int(chat_id), username, first_name)
    # lock check
    try:
        ulock = db.get_bot_user_by_chat(int(chat_id)) or {}
        locked_until = ulock.get('locked_until')
        if locked_until:
            try:
                lu = datetime.strptime(str(locked_until), '%Y-%m-%d %H:%M:%S')
                if datetime.now() < lu and not text.startswith('/start'):
                    _send(chat_id, 'Ваш аккаунт временно заблокирован. Попробуйте позже.')
                    return jsonify({'ok': True})
            except Exception:
                pass
    except Exception:
        pass
    # Simple commands: /start, /auth <pwd>
    if text.startswith('/start'):
        welcome = (
            'Привет! Это WB‑Irrigation Bot.\n\n'
            'Доступные команды:\n'
            '/auth <пароль> — авторизация\n'
            '/menu — главное меню\n'
            '/help — краткая справка\n'
            '/report — быстрый отчёт\n'
            '/subscribe, /unsubscribe — подписки\n\n'
            'Сначала пройдите авторизацию: /auth <пароль>'
        )
        _send(chat_id, welcome)
        return jsonify({'ok': True})
    if text.startswith('/auth'):
        parts = text.split(maxsplit=1)
        if len(parts) == 2:
            pwd = parts[1].strip()
            h = db.get_setting_value('telegram_access_password_hash')
            if h and check_password_hash(h, pwd):
                db.set_bot_user_authorized(int(chat_id), role='user')
                _send(chat_id, 'Готово. Доступ предоставлен.')
                try:
                    _send_menu(chat_id)
                except Exception:
                    _send(chat_id, 'Введите /menu для открытия клавиатуры.')
                return jsonify({'ok': True})
            else:
                failed = db.inc_bot_user_failed(int(chat_id))
                if failed >= 5:
                    until = (datetime.now() + timedelta(hours=1)).strftime('%Y-%m-%d %H:%M:%S')
                    db.lock_bot_user_until(int(chat_id), until)
                _send(chat_id, f'Пароль неверный. Осталось попыток: {max(0, 5-failed)}')
                return jsonify({'ok': True})
    # rate limit (skip for callback pings w/o data)
    if _rate_limited(int(chat_id)):
        _send(chat_id, 'Слишком часто. Повторите позже.')
        return jsonify({'ok': True})
    user = db.get_bot_user_by_chat(int(chat_id)) or {}
    if not user or not int(user.get('is_authorized') or 0):
        _send(chat_id, 'Нет доступа. Авторизуйтесь: /auth <пароль>')
        return jsonify({'ok': True})
    # Basic commands
    # --- Inline menu ---
    def _send_menu(cid: int):
        kb = _inline_markup([
            [{'text': 'Группы', 'callback_data': 'menu:groups'}, {'text': 'Зоны', 'callback_data': 'menu:zones'}],
            [{'text': 'Отложить полив', 'callback_data': 'menu:postpone'}, {'text': 'Отчёты', 'callback_data': 'menu:report'}],
            [{'text': 'Подписки', 'callback_data': 'menu:subs'}, {'text': 'Уведомления', 'callback_data': 'menu:notif'}]
        ])
        try:
            # админ-кнопка
            if _role_allowed(_get_role(cid), 'admin'):
                kb['inline_keyboard'].append([{'text':'Аварийная остановка','callback_data':'menu:emergency'}])
        except Exception:
            pass
        try:
            notifier.send_message(cid, 'Главное меню:', kb)
            return
        except Exception:
            pass
        _send(cid, 'Меню: /groups, /zones <group>, /report today|7|30, /subscribe, /unsubscribe')

    if text.startswith('/help'):
        _send(chat_id, '/menu, /groups, /zones <group>, /group_start <id>, /group_stop <id>, /zone_start <id>, /zone_stop <id>, /report today')
        return jsonify({'ok': True})
    if text.startswith('/menu'):
        _send_menu(chat_id)
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
    if text.startswith('/postpone_cancel'):
        parts = text.split()
        if len(parts) > 1 and parts[1].isdigit():
            gid = int(parts[1])
            try:
                from app import app as _app
                with _app.test_request_context(json={'group_id': gid, 'action': 'cancel'}):
                    from app import api_postpone as _pp
                    _pp()
                _send(chat_id, f'Отложенный полив для группы {gid} отменен')
            except Exception:
                _send(chat_id, 'Ошибка отмены отложенного полива')
        else:
            _send(chat_id, 'Используйте: /postpone_cancel <group_id>')
        return jsonify({'ok': True})
    if text.startswith('/postpone'):
        parts = text.split()
        if len(parts) > 1 and parts[1].isdigit():
            gid = int(parts[1])
            days = 1
            try:
                if len(parts) > 2:
                    days = max(1, min(7, int(parts[2])))
            except Exception:
                days = 1
            try:
                from app import app as _app
                with _app.test_request_context(json={'group_id': gid, 'action': 'postpone', 'days': days}):
                    from app import api_postpone as _pp
                    _pp()
                _send(chat_id, f'Полив группы {gid} отложен на {days} дн.')
            except Exception:
                _send(chat_id, 'Ошибка установки отложенного полива')
        else:
            _send(chat_id, 'Используйте: /postpone <group_id> <days>')
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
    # --- Callback handling ---
    if callback:
        try:
            data = str(callback.get('data') or '')
            cqid = callback.get('id')
            from_chat = ((callback.get('message') or {}).get('chat') or {}).get('id') or chat_id
            if _dedup_cb(int(from_chat), data):
                try:
                    notifier.answer_callback(cqid)
                except Exception:
                    pass
                return jsonify({'ok': True})
            # Acknowledge
            try:
                notifier.answer_callback(cqid)
            except Exception:
                pass
            if data.startswith('menu:'):
                action = data.split(':',1)[1]
                if action == 'groups':
                    gl = db.list_groups_min()
                    txt = 'Группы:\n' + '\n'.join([f"{g['id']}: {g['name']}" for g in gl])
                    _send(from_chat, txt)
                elif action == 'zones':
                    gl = db.list_groups_min()
                    if gl:
                        rows = [[{'text': f"{g['id']}: {g['name']}", 'callback_data': f"zones:{g['id']}"}] for g in gl]
                        notifier.send_message(from_chat, 'Выберите группу:', _inline_markup(rows))
                    else:
                        _send(from_chat, 'Нет групп')
                elif action == 'postpone':
                    gl = db.list_groups_min()
                    if gl:
                        rows = []
                        for g in gl:
                            gid = int(g['id']); name = g['name']
                            rows.append([
                                {'text': f"{name}: +1д", 'callback_data': f"grp_postpone:{gid}:1"},
                                {'text': "+2д", 'callback_data': f"grp_postpone:{gid}:2"},
                                {'text': "+3д", 'callback_data': f"grp_postpone:{gid}:3"}
                            ])
                        notifier.send_message(from_chat, 'Отложить полив по группам:', _inline_markup(rows))
                    else:
                        _send(from_chat, 'Нет групп')
                elif action == 'report':
                    txt = build_report_text(period='today', fmt='brief')
                    _send(from_chat, txt)
                elif action == 'subs':
                    _send(from_chat, 'Подписки: используйте команды /subscribe и /unsubscribe')
                elif action == 'notif':
                    _send(from_chat, 'Настройки уведомлений пока задаются админом на сервере')
                elif action == 'emergency':
                    role = _get_role(from_chat)
                    if not _role_allowed(role, 'admin'):
                        _send(from_chat, 'Нет прав')
                    else:
                        rows = [[{'text':'🚨 Подтвердить остановку','callback_data':'confirm:emergency:on'}],
                                [{'text':'Отмена','callback_data':'confirm:cancel'}]]
                        notifier.send_message(from_chat, 'Подтвердите аварийную остановку', _inline_markup(rows))
                return jsonify({'ok': True})
            if data.startswith('zones:'):
                try:
                    gid = int(data.split(':',1)[1])
                except Exception:
                    gid = 0
                if gid:
                    zl = db.list_zones_by_group_min(gid)
                    if zl:
                        # Покажем клавиатуру для быстрого старта/стопа зон
                        rows = []
                        for z in zl:
                            zid = int(z['id']); name = z['name']
                            rows.append([
                                {'text': f"▶ {name}", 'callback_data': f"zone_start:{zid}"},
                                {'text': f"⏹ {name}", 'callback_data': f"zone_stop:{zid}"}
                            ])
                        notifier.send_message(from_chat, f'Группа {gid}: выберите действие по зонам', _inline_markup(rows))
                    else:
                        _send(from_chat, f'В группе {gid} нет зон')
                return jsonify({'ok': True})
            if data.startswith('zone_start:'):
                try:
                    zid = int(data.split(':',1)[1])
                except Exception:
                    zid = 0
                if zid:
                    role = _get_role(from_chat)
                    if not _role_allowed(role, 'user'):
                        _send(from_chat, 'Нет прав')
                        return jsonify({'ok': True})
                    try:
                        from services.zone_control import exclusive_start_zone
                        exclusive_start_zone(zid)
                        _send(from_chat, f'▶ Зона {zid} запущена')
                    except Exception:
                        _send(from_chat, 'Ошибка запуска зоны')
                return jsonify({'ok': True})
            if data.startswith('zone_stop:'):
                try:
                    zid = int(data.split(':',1)[1])
                except Exception:
                    zid = 0
                if zid:
                    role = _get_role(from_chat)
                    if not _role_allowed(role, 'user'):
                        _send(from_chat, 'Нет прав')
                        return jsonify({'ok': True})
                    try:
                        from services.zone_control import stop_zone
                        stop_zone(zid, reason='telegram')
                        _send(from_chat, f'⏹ Зона {zid} остановлена')
                    except Exception:
                        _send(from_chat, 'Ошибка остановки зоны')
                return jsonify({'ok': True})
            if data.startswith('grp_start:'):
                try:
                    gid = int(data.split(':',1)[1])
                except Exception:
                    gid = 0
                if gid:
                    role = _get_role(from_chat)
                    if not _role_allowed(role, 'user'):
                        _send(from_chat, 'Нет прав')
                        return jsonify({'ok': True})
                    try:
                        from irrigation_scheduler import get_scheduler
                        s = get_scheduler()
                        if s:
                            s.start_group_sequence(gid)
                        _send(from_chat, f'▶ Группа {gid} запущена')
                    except Exception:
                        _send(from_chat, 'Ошибка запуска группы')
                return jsonify({'ok': True})
            if data.startswith('grp_stop:'):
                try:
                    gid = int(data.split(':',1)[1])
                except Exception:
                    gid = 0
                if gid:
                    role = _get_role(from_chat)
                    if not _role_allowed(role, 'user'):
                        _send(from_chat, 'Нет прав')
                        return jsonify({'ok': True})
                    try:
                        from services.zone_control import stop_all_in_group
                        stop_all_in_group(gid, reason='telegram')
                        _send(from_chat, f'⏹ Группа {gid} остановлена')
                    except Exception:
                        _send(from_chat, 'Ошибка остановки группы')
                return jsonify({'ok': True})
            if data.startswith('grp_postpone:'):
                parts = data.split(':')
                gid = int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else 0
                days = int(parts[2]) if len(parts) > 2 and parts[2].isdigit() else 1
                if gid:
                    role = _get_role(from_chat)
                    if not _role_allowed(role, 'user'):
                        _send(from_chat, 'Нет прав')
                        return jsonify({'ok': True})
                    try:
                        from app import app as _app
                        with _app.test_request_context(json={'group_id': gid, 'action': 'postpone', 'days': days}):
                            from app import api_postpone as _pp
                            _pp()
                        _send(from_chat, f'Группа {gid}: отложен полив на {days} дн.')
                    except Exception:
                        _send(from_chat, 'Ошибка отложенного полива')
                return jsonify({'ok': True})
            if data == 'confirm:cancel':
                _send(from_chat, 'Отменено')
                return jsonify({'ok': True})
            if data.startswith('confirm:emergency:'):
                what = data.split(':',2)[2]
                role = _get_role(from_chat)
                if not _role_allowed(role, 'admin'):
                    _send(from_chat, 'Нет прав')
                    return jsonify({'ok': True})
                try:
                    from app import app as _app
                    with _app.test_request_context():
                        if what == 'on':
                            from app import api_emergency_stop as _es
                            _es()
                            _send(from_chat, '🚨 Аварийная остановка активирована')
                        else:
                            from app import api_emergency_resume as _er
                            _er()
                            _send(from_chat, '✅ Аварийная остановка снята')
                except Exception:
                    _send(from_chat, 'Ошибка выполнения аварийной операции')
                return jsonify({'ok': True})
        except Exception:
            pass
    return jsonify({'ok': True})

