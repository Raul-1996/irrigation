"""
Tests for utils.py — normalize_topic, encrypt/decrypt, edge cases.
"""
import os
import sys
import pytest

_HERE = os.path.abspath(os.path.dirname(__file__))
_ROOT = os.path.abspath(os.path.join(_HERE, os.pardir, os.pardir, os.pardir))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from utils import normalize_topic, encrypt_secret, decrypt_secret


class TestNormalizeTopic:
    def test_normal_topic(self):
        assert normalize_topic('/devices/wb-mr6cv3_101/controls/K1') == '/devices/wb-mr6cv3_101/controls/K1'

    def test_no_leading_slash(self):
        result = normalize_topic('devices/wb/controls/K1')
        assert result.startswith('/')

    def test_multiple_leading_slashes(self):
        result = normalize_topic('///devices/test')
        assert result == '/devices/test'

    def test_none_input(self):
        assert normalize_topic(None) == ''

    def test_empty_string(self):
        assert normalize_topic('') == ''

    def test_whitespace_only(self):
        assert normalize_topic('   ') == ''

    def test_trailing_on_stripped(self):
        result = normalize_topic('/devices/test/controls/K1/on')
        assert result == '/devices/test/controls/K1'

    def test_whitespace_stripped(self):
        result = normalize_topic('  /devices/test  ')
        assert result == '/devices/test'


class TestEncryptDecrypt:
    def test_roundtrip(self):
        plaintext = 'my_secret_password_123'
        encrypted = encrypt_secret(plaintext)
        assert encrypted is not None
        assert encrypted != plaintext
        decrypted = decrypt_secret(encrypted)
        assert decrypted == plaintext

    def test_encrypt_none(self):
        assert encrypt_secret(None) is None

    def test_decrypt_none(self):
        assert decrypt_secret(None) is None

    def test_decrypt_empty(self):
        assert decrypt_secret('') is None

    def test_different_plaintexts_different_ciphertexts(self):
        e1 = encrypt_secret('aaa')
        e2 = encrypt_secret('bbb')
        assert e1 != e2

    def test_unicode_roundtrip(self):
        text = 'Пароль_пользователя_🔑'
        encrypted = encrypt_secret(text)
        decrypted = decrypt_secret(encrypted)
        assert decrypted == text

    def test_long_string(self):
        text = 'x' * 10000
        encrypted = encrypt_secret(text)
        decrypted = decrypt_secret(encrypted)
        assert decrypted == text
