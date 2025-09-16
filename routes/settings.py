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
        if 'telegram_access_password' in data:
            from werkzeug.security import generate_password_hash
            pwd = data.get('telegram_access_password') or ''
            ok &= db.set_setting_value('telegram_access_password_hash', generate_password_hash(pwd))
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
        return jsonify({'success': True, 'message': 'Токен сохранён. Тестовая отправка появится после внедрения бота.'})
    except Exception as e:
        return jsonify({'success': False, 'message': str(e)}), 500


