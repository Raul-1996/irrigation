"""
Tests for services/auth_service.py — password verification logic.
"""
import os
import sys
import pytest
from unittest.mock import patch, MagicMock
from werkzeug.security import generate_password_hash

_HERE = os.path.abspath(os.path.dirname(__file__))
_ROOT = os.path.abspath(os.path.join(_HERE, os.pardir, os.pardir, os.pardir))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

os.environ.setdefault("TESTING", "1")


class TestVerifyPassword:
    def test_correct_password(self):
        from services.auth_service import verify_password
        # Seed password hash in DB
        import database
        pw_hash = generate_password_hash('test123', method='pbkdf2:sha256')
        database.db.set_setting_value('password_hash', pw_hash)
        
        success, role = verify_password('test123')
        assert success is True
        assert role == 'admin'

    def test_wrong_password(self):
        from services.auth_service import verify_password
        import database
        pw_hash = generate_password_hash('correct', method='pbkdf2:sha256')
        database.db.set_setting_value('password_hash', pw_hash)
        
        success, role = verify_password('wrong')
        assert success is False

    def test_empty_password(self):
        from services.auth_service import verify_password
        success, role = verify_password('')
        assert success is False

    def test_verify_admin_deprecated(self):
        from services.auth_service import verify_admin
        assert verify_admin('anything') is False

    def test_verify_user_deprecated(self):
        from services.auth_service import verify_user
        assert verify_user('anything') is False
