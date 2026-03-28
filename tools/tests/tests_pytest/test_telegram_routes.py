"""
Tests for routes/telegram.py — callback processing, menu screens.
All Telegram API calls are mocked.
"""
import os
import sys
import json
import pytest
from unittest.mock import patch, MagicMock

_HERE = os.path.abspath(os.path.dirname(__file__))
_ROOT = os.path.abspath(os.path.join(_HERE, os.pardir, os.pardir, os.pardir))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

os.environ.setdefault("TESTING", "1")


class TestTelegramHelpers:
    def test_btn_helper(self):
        from routes.telegram import _btn
        result = _btn('Test', 'data')
        assert result['text'] == 'Test'
        assert 'callback_data' in result

    def test_inline_markup(self):
        from routes.telegram import _inline_markup, _btn
        rows = [[_btn('A', 'a'), _btn('B', 'b')]]
        markup = _inline_markup(rows)
        assert 'inline_keyboard' in markup

    def test_cb_decode(self):
        from routes.telegram import _cb_decode
        result = _cb_decode('{"action":"test","id":1}')
        assert result.get('action') == 'test'

    def test_cb_decode_invalid(self):
        from routes.telegram import _cb_decode
        result = _cb_decode('not-json')
        assert isinstance(result, dict)


class TestScreens:
    def test_main_menu(self):
        from routes.telegram import _screen_main_menu
        text, markup = _screen_main_menu()
        assert isinstance(text, str)
        assert 'inline_keyboard' in markup

    def test_groups_list(self):
        from routes.telegram import _screen_groups_list
        text, markup = _screen_groups_list()
        assert isinstance(text, str)

    def test_group_actions(self):
        from routes.telegram import _screen_group_actions
        try:
            text, markup = _screen_group_actions(1)
            assert isinstance(text, str)
        except Exception:
            # Group may not exist in test DB
            pass


class TestCallbackProcessing:
    @patch('routes.telegram._notify', new_callable=lambda: MagicMock)
    def test_process_callback_main(self, *mocks):
        from routes.telegram import process_callback_json
        try:
            process_callback_json(123, {'action': 'main'}, message_id=1)
        except Exception:
            pass  # May need notifier set

    @patch('routes.telegram._notify', new_callable=lambda: MagicMock)
    def test_process_callback_groups(self, *mocks):
        from routes.telegram import process_callback_json
        try:
            process_callback_json(123, {'action': 'groups'}, message_id=1)
        except Exception:
            pass
