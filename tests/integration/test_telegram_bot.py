"""Integration test: Telegram bot (mock aiogram)."""
import pytest
import os

os.environ['TESTING'] = '1'


class TestTelegramBotDB:
    """Test Telegram bot DB interactions without actual bot."""

    def test_user_lifecycle(self, test_db):
        """Create, authorize, lock user."""
        test_db.upsert_bot_user(100, 'bot_user', 'Bot')
        user = test_db.get_bot_user_by_chat(100)
        assert user is not None
        assert user['is_authorized'] == 0

        test_db.set_bot_user_authorized(100, 'user')
        user = test_db.get_bot_user_by_chat(100)
        assert user['is_authorized'] == 1

    def test_fsm_state_machine(self, test_db):
        """FSM state transitions."""
        test_db.upsert_bot_user(200, 'fsm_user', 'FSM')
        
        test_db.set_bot_fsm(200, 'waiting_password', '{}')
        state, data = test_db.get_bot_fsm(200)
        assert state == 'waiting_password'
        
        test_db.set_bot_fsm(200, 'authorized', '{"role": "admin"}')
        state, data = test_db.get_bot_fsm(200)
        assert state == 'authorized'

    def test_idempotency_prevents_duplicate(self, test_db):
        """Same token should not process twice."""
        first = test_db.is_new_idempotency_token('tok_dup', 300, 'start_zone')
        assert first is True
        
        second = test_db.is_new_idempotency_token('tok_dup', 300, 'start_zone')
        assert second is False
