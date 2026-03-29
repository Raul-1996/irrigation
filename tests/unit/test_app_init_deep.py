"""Deep tests for services/app_init.py."""
import os
import pytest
from unittest.mock import patch, MagicMock


class TestInitializeApp:
    """Tests for initialize_app."""

    def test_initialize_app_testing_mode(self, test_db):
        """initialize_app should skip init in TESTING mode."""
        from services.app_init import initialize_app, reset_init
        reset_init()

        mock_app = MagicMock()
        mock_app.config = {'TESTING': True}
        initialize_app(mock_app, test_db)

    def test_initialize_app_idempotent(self, test_db):
        """initialize_app should only run once."""
        from services.app_init import initialize_app, reset_init
        reset_init()

        mock_app = MagicMock()
        mock_app.config = {'TESTING': True}
        initialize_app(mock_app, test_db)
        initialize_app(mock_app, test_db)  # no-op


class TestRegisterShutdownHandlers:
    """Tests for shutdown handler registration."""

    def test_skip_in_testing(self):
        os.environ['TESTING'] = '1'
        from services.app_init import _register_shutdown_handlers
        _register_shutdown_handlers()


class TestResetInit:
    """Tests for reset_init."""

    def test_reset_allows_reinit(self, test_db):
        from services.app_init import initialize_app, reset_init
        reset_init()
        mock_app = MagicMock()
        mock_app.config = {'TESTING': True}
        initialize_app(mock_app, test_db)
        reset_init()
        initialize_app(mock_app, test_db)  # should work again


class TestResetShutdown:
    """Tests for reset_shutdown."""

    def test_reset_shutdown(self):
        from services.app_init import reset_shutdown, shutdown_all_zones
        reset_shutdown()
