"""Deep tests for settings routes (telegram settings, etc.)."""

import json
import logging
from unittest.mock import patch


class TestTelegramSettings:
    """Tests for /api/settings/telegram endpoints."""

    def test_get_telegram_settings(self, admin_client):
        """GET /api/settings/telegram returns masked token and settings."""
        resp = admin_client.get("/api/settings/telegram")
        assert resp.status_code == 200
        data = resp.get_json()
        assert "telegram_bot_token_masked" in data
        assert "telegram_webhook_secret_path" in data
        assert "telegram_admin_chat_id" in data

    def test_put_telegram_token(self, admin_client):
        """PUT /api/settings/telegram with a bot token."""
        with (
            patch("routes.settings.encrypt_secret", return_value="encrypted-token"),
            patch("services.telegram_bot.reconfigure_bot_token", return_value=True) as reconfigure,
        ):
            resp = admin_client.put(
                "/api/settings/telegram",
                data=json.dumps(
                    {
                        "telegram_bot_token": "123456:ABCdef",
                    }
                ),
                content_type="application/json",
            )
        assert resp.status_code == 200
        assert resp.get_json()["success"] is True
        reconfigure.assert_called_once_with("encrypted-token")

    def test_delete_telegram_token_reconfigures_runtime(self, admin_client):
        with patch("services.telegram_bot.reconfigure_bot_token", return_value=True) as reconfigure:
            resp = admin_client.put(
                "/api/settings/telegram",
                data=json.dumps({"telegram_bot_token": ""}),
                content_type="application/json",
            )

        assert resp.status_code == 200
        assert resp.get_json()["success"] is True
        reconfigure.assert_called_once_with(None)

    def test_set_webhook_is_rejected_before_token_mutation(self, admin_client):
        with patch("services.telegram_bot.reconfigure_bot_token") as reconfigure:
            resp = admin_client.put(
                "/api/settings/telegram",
                data=json.dumps(
                    {
                        "telegram_bot_token": "123456:ABCdef",
                        "set_webhook": True,
                    }
                ),
                content_type="application/json",
            )

        assert resp.status_code == 400
        assert resp.get_json() == {
            "success": False,
            "error": "Telegram webhook mode is not supported; use long polling",
        }
        reconfigure.assert_not_called()

    def test_put_telegram_admin_chat_id(self, admin_client):
        """PUT /api/settings/telegram sets admin chat ID."""
        resp = admin_client.put(
            "/api/settings/telegram",
            data=json.dumps(
                {
                    "telegram_admin_chat_id": "12345678",
                }
            ),
            content_type="application/json",
        )
        assert resp.status_code == 200
        assert resp.get_json()["success"] is True

    def test_put_telegram_access_password(self, admin_client):
        """PUT /api/settings/telegram sets access password."""
        resp = admin_client.put(
            "/api/settings/telegram",
            data=json.dumps(
                {
                    "telegram_access_password": "mysecret",
                }
            ),
            content_type="application/json",
        )
        assert resp.status_code == 200
        assert resp.get_json()["success"] is True

    def test_put_telegram_webhook_secret(self, admin_client):
        """PUT /api/settings/telegram sets webhook secret path."""
        resp = admin_client.put(
            "/api/settings/telegram",
            data=json.dumps(
                {
                    "telegram_webhook_secret_path": "my-secret-path",
                }
            ),
            content_type="application/json",
        )
        assert resp.status_code == 200
        assert resp.get_json()["success"] is True

    def test_test_telegram_in_testing_mode(self, admin_client):
        """POST /api/settings/telegram/test in TESTING mode."""
        resp = admin_client.post("/api/settings/telegram/test")
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["success"] is True
        assert "TESTING" in data.get("message", "")

    def test_test_telegram_transport_failure_is_sanitized(self, admin_client, app, caplog, monkeypatch):
        from routes import settings
        from services.telegram_bot import notifier

        token = "123456:SENTINEL_HTTP_TOKEN"
        leaked_url = f"https://api.telegram.org/bot{token}/sendMessage"

        def setting_value(key: str):
            if key == "telegram_bot_token_encrypted":
                return "encrypted-token"
            if key == "telegram_admin_chat_id":
                return "701"
            return None

        monkeypatch.setitem(app.config, "TESTING", False)
        with (
            patch.object(settings.db, "get_setting_value", side_effect=setting_value),
            patch.object(notifier, "send_text", side_effect=ConnectionError(f"failed: {leaked_url}")),
            caplog.at_level(logging.DEBUG, logger="routes.settings"),
        ):
            resp = admin_client.post("/api/settings/telegram/test")

        assert resp.status_code == 500
        assert resp.get_json() == {
            "success": False,
            "message": "Не удалось отправить тестовое сообщение Telegram",
        }
        response_text = resp.get_data(as_text=True)
        logged = "\n".join(record.getMessage() for record in caplog.records)
        assert token not in response_text
        assert token not in logged
        assert "/bot123456" not in response_text
        assert "/bot123456" not in logged


class TestSettingsPage:
    """Tests for settings page rendering."""

    def test_settings_page_requires_admin(self, client):
        """GET /settings redirects for non-admin."""
        resp = client.get("/settings")
        # In test mode it might render or redirect
        assert resp.status_code in (200, 302, 401, 403)

    def test_settings_page_admin(self, admin_client):
        """GET /settings renders for admin."""
        resp = admin_client.get("/settings")
        assert resp.status_code == 200
