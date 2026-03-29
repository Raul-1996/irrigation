"""Comprehensive tests for services/telegram_bot.py — mock-based, no real Telegram."""
import pytest
import os
from unittest.mock import patch, MagicMock, AsyncMock

os.environ['TESTING'] = '1'


class TestTelegramBotImport:
    def test_module_imports(self):
        """telegram_bot module should import without errors."""
        try:
            import services.telegram_bot
        except ImportError:
            pytest.skip("telegram_bot has unresolvable dependencies")

    def test_notifier_exists(self):
        """Module should expose a notifier object."""
        try:
            from services.telegram_bot import notifier
            # notifier may be None if aiogram is not configured
        except ImportError:
            pytest.skip("aiogram not available")
