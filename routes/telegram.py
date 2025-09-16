from flask import Blueprint, request, jsonify
from database import db
from werkzeug.security import check_password_hash
from datetime import datetime, timedelta

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
        return jsonify({'ok': True})
    if text.startswith('/auth'):
        parts = text.split(maxsplit=1)
        if len(parts) == 2:
            pwd = parts[1].strip()
            h = db.get_setting_value('telegram_access_password_hash')
            if h and check_password_hash(h, pwd):
                db.set_bot_user_authorized(int(chat_id), role='user')
                return jsonify({'ok': True})
            else:
                failed = db.inc_bot_user_failed(int(chat_id))
                if failed >= 5:
                    until = (datetime.now() + timedelta(hours=1)).strftime('%Y-%m-%d %H:%M:%S')
                    db.lock_bot_user_until(int(chat_id), until)
                return jsonify({'ok': True})
    return jsonify({'ok': True})

