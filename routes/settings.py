from flask import Blueprint, render_template
from services.security import admin_required
from database import db
from flask import jsonify, request
from utils import encrypt_secret, decrypt_secret


settings_bp = Blueprint('settings_bp', __name__)


@settings_bp.route('/settings')
@admin_required
def settings_page():
    # Передаём текущее название системы в шаблон
    name = db.get_setting_value('system_name') or ''
    return render_template('settings.html', system_name=name)


@settings_bp.route('/api/settings/telegram', methods=['GET'])
@admin_required
def api_get_telegram_settings():
    try:
        tok_enc = db.get_setting_value('telegram_bot_token_encrypted')
        tok_plain = decrypt_secret(tok_enc) if tok_enc else ''
        masked = (('*' * max(0, len(tok_plain) - 4)) + tok_plain[-4:]) if tok_plain else ''
        return jsonify({
            'telegram_bot_token_masked': masked,
            'telegram_webhook_secret_path': db.get_setting_value('telegram_webhook_secret_path') or '',
            'telegram_admin_chat_id': db.get_setting_value('telegram_admin_chat_id') or ''
        })
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@settings_bp.route('/api/settings/telegram', methods=['PUT'])
@admin_required
def api_put_telegram_settings():
    try:
        data = request.get_json() or {}
        ok = True
        if 'telegram_bot_token' in data:
            tok = data.get('telegram_bot_token') or ''
            val = encrypt_secret(tok) if tok else None
            ok &= db.set_setting_value('telegram_bot_token_encrypted', val)
            # Автогенерация секрета вебхука, если не задан — для защиты, но не обязательна
            cur = db.get_setting_value('telegram_webhook_secret_path') or ''
            if not cur:
                try:
                    import secrets
                    db.set_setting_value('telegram_webhook_secret_path', secrets.token_urlsafe(16))
                except Exception:
                    pass
            # Настройка вебхука — только по явному запросу
            if bool(data.get('set_webhook')):
                try:
                    base_url = (request.host_url or '').rstrip('/')
                    wh_secret = db.get_setting_value('telegram_webhook_secret_path') or 'any'
                    from services.telegram_bot import notifier
                    notifier.set_webhook(f"{base_url}/telegram/webhook/{wh_secret}")
                except Exception:
                    pass
        if 'telegram_access_password' in data:
            from werkzeug.security import generate_password_hash
            pwd = data.get('telegram_access_password') or ''
            # Явно указываем PBKDF2, чтобы избежать scrypt на системах без поддержки
            ok &= db.set_setting_value('telegram_access_password_hash', generate_password_hash(pwd, method='pbkdf2:sha256:260000'))
        if 'telegram_webhook_secret_path' in data:
            ok &= db.set_setting_value('telegram_webhook_secret_path', data.get('telegram_webhook_secret_path') or '')
        if 'telegram_admin_chat_id' in data:
            ok &= db.set_setting_value('telegram_admin_chat_id', str(data.get('telegram_admin_chat_id') or ''))
        return jsonify({'success': bool(ok)})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


@settings_bp.route('/api/settings/telegram/test', methods=['POST'])
@admin_required
def api_test_telegram():
    try:
        tok_enc = db.get_setting_value('telegram_bot_token_encrypted')
        if not tok_enc:
            return jsonify({'success': False, 'message': 'Токен бота не задан'}), 400
        # Попробуем отправить тестовое сообщение
        try:
            from services.telegram_bot import notifier
            chat_id = db.get_setting_value('telegram_admin_chat_id')
            if not chat_id:
                # fallback: последний активный чат
                import sqlite3
                with sqlite3.connect(db.db_path, timeout=5) as conn:
                    conn.row_factory = sqlite3.Row
                    cur = conn.execute('SELECT chat_id FROM bot_users ORDER BY last_seen_at DESC LIMIT 1')
                    row = cur.fetchone()
                    chat_id = str(row['chat_id']) if row else None
            if chat_id:
                ok = notifier.send_text(int(chat_id), 'Тестовое сообщение: WB-Irrigation Bot активен')
                if ok:
                    return jsonify({'success': True, 'message': f'Отправлено в чат {chat_id}'} )
                else:
                    return jsonify({'success': False, 'message': 'Не удалось отправить сообщение — проверьте токен'}), 500
            else:
                return jsonify({'success': True, 'message': 'Токен сохранён. Откройте чат с ботом (/start), затем повторите тест.'})
        except Exception as e:
            return jsonify({'success': False, 'message': f'Ошибка отправки: {e}'}), 500
    except Exception as e:
        return jsonify({'success': False, 'message': str(e)}), 500


