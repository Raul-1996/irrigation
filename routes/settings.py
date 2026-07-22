import logging
import sqlite3

from flask import Blueprint, current_app, jsonify, render_template, request

from database import db
from services.audit import audit_log
from services.security import admin_required
from utils import decrypt_secret, encrypt_secret

# CQ-001..004 (MASTER-C2 extension): previously this module used `logger.debug(...)`
# but never imported logging, so those calls would raise NameError if any of the
# exception branches ever executed. Fix: add the canonical logger binding.
logger = logging.getLogger(__name__)


logger = logging.getLogger(__name__)

settings_bp = Blueprint("settings_bp", __name__)


@settings_bp.route("/settings")
@admin_required
def settings_page():
    # Передаём текущее название системы в шаблон
    name = db.get_setting_value("system_name") or ""
    return render_template("settings.html", system_name=name)


@settings_bp.route("/api/settings/telegram", methods=["GET"])
@admin_required
def api_get_telegram_settings():
    try:
        tok_enc = db.get_setting_value("telegram_bot_token_encrypted")
        tok_plain = decrypt_secret(tok_enc) if tok_enc else ""
        masked = (("*" * max(0, len(tok_plain) - 4)) + tok_plain[-4:]) if tok_plain else ""
        return jsonify(
            {
                "telegram_bot_token_masked": masked,
                "telegram_webhook_secret_path": db.get_setting_value("telegram_webhook_secret_path") or "",
                "telegram_admin_chat_id": db.get_setting_value("telegram_admin_chat_id") or "",
            }
        )
    except (sqlite3.Error, OSError) as e:
        logger.debug("Exception in api_get_telegram_settings: %s", e)
        return jsonify({"error": str(e)}), 500


@settings_bp.route("/api/settings/telegram", methods=["PUT"])
@admin_required
@audit_log("telegram_settings_save", target_extractor=lambda *a, **kw: "settings:telegram")
def api_put_telegram_settings():
    try:
        data = request.get_json() or {}
        # This application supports long polling only.  Reject the unsupported
        # mode before touching the token: the former implementation persisted
        # it and only then called a nonexistent notifier.set_webhook().
        if bool(data.get("set_webhook")):
            return jsonify(
                {
                    "success": False,
                    "error": "Telegram webhook mode is not supported; use long polling",
                }
            ), 400

        ok = True
        if "telegram_bot_token" in data:
            tok = data.get("telegram_bot_token") or ""
            val = encrypt_secret(tok) if tok else None
            from services.telegram_bot import reconfigure_bot_token

            if not reconfigure_bot_token(val):
                return jsonify(
                    {
                        "success": False,
                        "error": "Не удалось безопасно перезапустить Telegram polling runtime",
                    }
                ), 503
        if "telegram_access_password" in data:
            from werkzeug.security import generate_password_hash

            pwd = data.get("telegram_access_password") or ""
            # Явно указываем PBKDF2, чтобы избежать scrypt на системах без поддержки
            ok &= db.set_setting_value(
                "telegram_access_password_hash", generate_password_hash(pwd, method="pbkdf2:sha256:260000")
            )
        if "telegram_webhook_secret_path" in data:
            ok &= db.set_setting_value("telegram_webhook_secret_path", data.get("telegram_webhook_secret_path") or "")
        if "telegram_admin_chat_id" in data:
            ok &= db.set_setting_value("telegram_admin_chat_id", str(data.get("telegram_admin_chat_id") or ""))
        return jsonify({"success": bool(ok)})
    except (sqlite3.Error, OSError) as e:
        logger.debug("Exception in line_73: %s", e)
        return jsonify({"success": False, "error": str(e)}), 500


@settings_bp.route("/api/settings/telegram/test", methods=["POST"])
@admin_required
@audit_log("telegram_test_send", target_extractor=lambda *a, **kw: "settings:telegram_test")
def api_test_telegram():
    try:
        # Skip actual telegram operations in TESTING mode
        if current_app.config.get("TESTING"):
            return jsonify({"success": True, "message": "TESTING mode: telegram test skipped"})

        tok_enc = db.get_setting_value("telegram_bot_token_encrypted")
        if not tok_enc:
            return jsonify({"success": False, "message": "Токен бота не задан"}), 400
        # Попробуем отправить тестовое сообщение
        try:
            from services.telegram_bot import notifier

            chat_id = db.get_setting_value("telegram_admin_chat_id")
            if not chat_id:
                # fallback: последний активный чат
                with db.telegram._connect() as conn:
                    cur = conn.execute("SELECT chat_id FROM bot_users ORDER BY last_seen_at DESC LIMIT 1")
                    row = cur.fetchone()
                    chat_id = str(row["chat_id"]) if row else None
            if chat_id:
                ok = notifier.send_text(int(chat_id), "Тестовое сообщение: WB-Irrigation Bot активен")
                if ok:
                    return jsonify({"success": True, "message": f"Отправлено в чат {chat_id}"})
                else:
                    return jsonify(
                        {"success": False, "message": "Не удалось отправить сообщение — проверьте токен"}
                    ), 500
            else:
                return jsonify(
                    {"success": True, "message": "Токен сохранён. Откройте чат с ботом (/start), затем повторите тест."}
                )
        except (sqlite3.Error, OSError):
            # Transport exceptions may embed the full /bot<TOKEN>/ URL.  Keep
            # both the operator response and logs stable and credential-free.
            logger.warning("Telegram test message transport or storage failure")
            return jsonify({"success": False, "message": "Не удалось отправить тестовое сообщение Telegram"}), 500
    except (sqlite3.Error, OSError):
        logger.warning("Telegram test message setup failure")
        return jsonify({"success": False, "message": "Не удалось отправить тестовое сообщение Telegram"}), 500
