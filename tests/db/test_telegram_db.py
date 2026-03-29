"""Tests for Telegram DB: subscriptions, users."""
import pytest
import os

os.environ['TESTING'] = '1'


class TestBotUsers:
    def test_upsert_bot_user(self, test_db):
        test_db.upsert_bot_user(123456, 'testuser', 'Test')
        user = test_db.get_bot_user_by_chat(123456)
        assert user is not None
        assert user['username'] == 'testuser'

    def test_get_nonexistent_user(self, test_db):
        user = test_db.get_bot_user_by_chat(999999)
        assert user is None

    def test_authorize_user(self, test_db):
        test_db.upsert_bot_user(111, 'u1', 'U')
        test_db.set_bot_user_authorized(111, 'admin')
        user = test_db.get_bot_user_by_chat(111)
        assert user['is_authorized'] == 1
        assert user['role'] == 'admin'

    def test_increment_failed_attempts(self, test_db):
        test_db.upsert_bot_user(222, 'u2', 'U')
        test_db.inc_bot_user_failed(222)
        user = test_db.get_bot_user_by_chat(222)
        assert user['failed_attempts'] >= 1

    def test_lock_user(self, test_db):
        test_db.upsert_bot_user(333, 'u3', 'U')
        test_db.lock_bot_user_until(333, '2099-12-31 23:59:59')
        user = test_db.get_bot_user_by_chat(333)
        assert user['locked_until'] == '2099-12-31 23:59:59'


class TestBotFSM:
    def test_set_and_get_fsm(self, test_db):
        test_db.upsert_bot_user(444, 'u4', 'U')
        test_db.set_bot_fsm(444, 'waiting_password', '{"step": 1}')
        state, data = test_db.get_bot_fsm(444)
        assert state == 'waiting_password'

    def test_get_fsm_no_state(self, test_db):
        test_db.upsert_bot_user(555, 'u5', 'U')
        state, data = test_db.get_bot_fsm(555)
        assert state is None


class TestBotIdempotency:
    def test_new_token(self, test_db):
        result = test_db.is_new_idempotency_token('tok1', 100, 'start')
        assert result is True

    def test_duplicate_token(self, test_db):
        test_db.is_new_idempotency_token('tok2', 100, 'start')
        result = test_db.is_new_idempotency_token('tok2', 100, 'start')
        assert result is False


class TestBotNotifications:
    def test_get_notif_settings(self, test_db):
        test_db.upsert_bot_user(666, 'u6', 'U')
        settings = test_db.get_bot_user_notif_settings(666)
        assert isinstance(settings, dict)

    def test_toggle_notification(self, test_db):
        test_db.upsert_bot_user(777, 'u7', 'U')
        # The toggle API expects short key names like 'rain', not 'notif_rain'
        test_db.set_bot_user_notif_toggle(777, 'rain', True)
        settings = test_db.get_bot_user_notif_settings(777)
        assert settings.get('rain') == 1
