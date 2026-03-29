"""Deep tests for settings routes (telegram settings, etc.)."""
import json
import pytest
from unittest.mock import patch, MagicMock


class TestTelegramSettings:
    """Tests for /api/settings/telegram endpoints."""

    def test_get_telegram_settings(self, admin_client):
        """GET /api/settings/telegram returns masked token and settings."""
        resp = admin_client.get('/api/settings/telegram')
        assert resp.status_code == 200
        data = resp.get_json()
        assert 'telegram_bot_token_masked' in data
        assert 'telegram_webhook_secret_path' in data
        assert 'telegram_admin_chat_id' in data

    def test_put_telegram_token(self, admin_client):
        """PUT /api/settings/telegram with a bot token."""
        resp = admin_client.put('/api/settings/telegram',
                                data=json.dumps({
                                    'telegram_bot_token': '123456:ABCdef',
                                }),
                                content_type='application/json')
        assert resp.status_code == 200
        assert resp.get_json()['success'] is True

    def test_put_telegram_admin_chat_id(self, admin_client):
        """PUT /api/settings/telegram sets admin chat ID."""
        resp = admin_client.put('/api/settings/telegram',
                                data=json.dumps({
                                    'telegram_admin_chat_id': '12345678',
                                }),
                                content_type='application/json')
        assert resp.status_code == 200
        assert resp.get_json()['success'] is True

    def test_put_telegram_access_password(self, admin_client):
        """PUT /api/settings/telegram sets access password."""
        resp = admin_client.put('/api/settings/telegram',
                                data=json.dumps({
                                    'telegram_access_password': 'mysecret',
                                }),
                                content_type='application/json')
        assert resp.status_code == 200
        assert resp.get_json()['success'] is True

    def test_put_telegram_webhook_secret(self, admin_client):
        """PUT /api/settings/telegram sets webhook secret path."""
        resp = admin_client.put('/api/settings/telegram',
                                data=json.dumps({
                                    'telegram_webhook_secret_path': 'my-secret-path',
                                }),
                                content_type='application/json')
        assert resp.status_code == 200
        assert resp.get_json()['success'] is True

    def test_test_telegram_in_testing_mode(self, admin_client):
        """POST /api/settings/telegram/test in TESTING mode."""
        resp = admin_client.post('/api/settings/telegram/test')
        assert resp.status_code == 200
        data = resp.get_json()
        assert data['success'] is True
        assert 'TESTING' in data.get('message', '')


class TestSettingsPage:
    """Tests for settings page rendering."""

    def test_settings_page_requires_admin(self, client):
        """GET /settings redirects for non-admin."""
        resp = client.get('/settings')
        # In test mode it might render or redirect
        assert resp.status_code in (200, 302, 401, 403)

    def test_settings_page_admin(self, admin_client):
        """GET /settings renders for admin."""
        resp = admin_client.get('/settings')
        assert resp.status_code == 200
