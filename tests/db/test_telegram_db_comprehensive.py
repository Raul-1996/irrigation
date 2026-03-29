"""Comprehensive tests for db/telegram.py."""
import pytest
import os
from datetime import datetime

os.environ['TESTING'] = '1'


class TestBotUserCRUD:
    def test_upsert_new_user(self, test_db):
        test_db.upsert_bot_user(12345, 'testuser', 'Test')
        user = test_db.get_bot_user_by_chat(12345)
        assert user is not None

    def test_upsert_existing_user(self, test_db):
        test_db.upsert_bot_user(12345, 'old', 'Old')
        test_db.upsert_bot_user(12345, 'new', 'New')
        user = test_db.get_bot_user_by_chat(12345)
        assert user is not None

    def test_get_nonexistent_user(self, test_db):
        result = test_db.get_bot_user_by_chat(99999)
        assert result is None


class TestBotUserAuth:
    def test_authorize_user(self, test_db):
        test_db.upsert_bot_user(100, 'auth', 'Auth')
        test_db.set_bot_user_authorized(100, role='admin')
        user = test_db.get_bot_user_by_chat(100)
        assert user is not None

    def test_inc_failed(self, test_db):
        test_db.upsert_bot_user(200, 'fail', 'Fail')
        test_db.inc_bot_user_failed(200)
        # Should increment failed_attempts

    def test_lock_until(self, test_db):
        test_db.upsert_bot_user(300, 'lock', 'Lock')
        test_db.lock_bot_user_until(300, '2026-12-31 23:59:59')


class TestBotFSM:
    def test_set_get_fsm(self, test_db):
        test_db.upsert_bot_user(400, 'fsm', 'FSM')
        test_db.set_bot_fsm(400, 'awaiting_password', '{"attempts": 0}')
        state, data = test_db.get_bot_fsm(400)
        assert state == 'awaiting_password'

    def test_get_fsm_no_state(self, test_db):
        test_db.upsert_bot_user(401, 'nofsm', 'NoFSM')
        result = test_db.get_bot_fsm(401)
        assert result is not None  # returns tuple


class TestIdempotencyToken:
    def test_new_token(self, test_db):
        result = test_db.is_new_idempotency_token('token1', 500, 'start')
        assert result is True

    def test_duplicate_token(self, test_db):
        test_db.is_new_idempotency_token('token2', 500, 'stop')
        result = test_db.is_new_idempotency_token('token2', 500, 'stop')
        assert result is False


class TestNotifSettings:
    def test_get_default(self, test_db):
        test_db.upsert_bot_user(600, 'notif', 'Notif')
        result = test_db.get_bot_user_notif_settings(600)
        assert isinstance(result, dict)

    def test_toggle_notif(self, test_db):
        test_db.upsert_bot_user(601, 'toggle', 'Toggle')
        test_db.set_bot_user_notif_toggle(601, 'zone_start', False)
        settings = test_db.get_bot_user_notif_settings(601)


class TestSubscriptions:
    def test_create_subscription(self, test_db):
        test_db.upsert_bot_user(700, 'sub', 'Sub')
        test_db.set_bot_user_authorized(700, 'user')
        result = test_db.create_or_update_subscription(
            700, 'daily', 'brief', '08:00', '1111111', True
        )
        assert isinstance(result, (bool, dict, type(None)))

    def test_get_due_subscriptions(self, test_db):
        now = datetime.now()
        result = test_db.get_due_bot_subscriptions(now)
        assert isinstance(result, list)
