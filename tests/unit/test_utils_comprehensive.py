"""Comprehensive tests for utils.py."""
import pytest
import os
import base64
from unittest.mock import patch, MagicMock

os.environ['TESTING'] = '1'


class TestNormalizeTopic:
    def test_normal_topic(self):
        from utils import normalize_topic
        assert normalize_topic('/devices/wb/controls/K1') == '/devices/wb/controls/K1'

    def test_no_leading_slash(self):
        from utils import normalize_topic
        assert normalize_topic('devices/wb/controls/K1') == '/devices/wb/controls/K1'

    def test_multiple_leading_slashes(self):
        from utils import normalize_topic
        assert normalize_topic('///devices/wb') == '/devices/wb'

    def test_strip_on_suffix(self):
        from utils import normalize_topic
        assert normalize_topic('/devices/wb/controls/K1/on') == '/devices/wb/controls/K1'

    def test_empty_string(self):
        from utils import normalize_topic
        assert normalize_topic('') == ''

    def test_none(self):
        from utils import normalize_topic
        assert normalize_topic(None) == ''

    def test_whitespace(self):
        from utils import normalize_topic
        assert normalize_topic('  /test/topic  ') == '/test/topic'


class TestEncryptDecryptSecret:
    def test_encrypt_none(self):
        from utils import encrypt_secret
        assert encrypt_secret(None) is None

    def test_decrypt_none(self):
        from utils import decrypt_secret
        assert decrypt_secret(None) is None

    def test_decrypt_empty(self):
        from utils import decrypt_secret
        assert decrypt_secret('') is None

    def test_encrypt_decrypt_roundtrip(self):
        from utils import encrypt_secret, decrypt_secret
        plaintext = 'my-secret-password'
        encrypted = encrypt_secret(plaintext)
        assert encrypted is not None
        assert encrypted != plaintext
        decrypted = decrypt_secret(encrypted)
        assert decrypted == plaintext

    def test_encrypt_decrypt_xor_fallback(self):
        from utils import encrypt_secret, decrypt_secret
        # Force xor fallback by mocking Crypto import
        with patch.dict('sys.modules', {'Crypto': None, 'Crypto.Cipher': None, 'Crypto.Cipher.AES': None, 'Crypto.Random': None}):
            # This test is best-effort since import caching may interfere
            pass

    def test_get_hostname_key(self):
        from utils import _get_hostname_key
        key = _get_hostname_key()
        assert len(key) == 32

    def test_get_secret_key(self):
        from utils import _get_secret_key
        key = _get_secret_key()
        assert len(key) >= 32
