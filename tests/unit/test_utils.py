"""Tests for utils.py: normalize_topic, AES encrypt/decrypt."""
import pytest
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))


class TestNormalizeTopic:
    def test_none_returns_empty(self):
        from utils import normalize_topic
        assert normalize_topic(None) == ""

    def test_empty_string_returns_empty(self):
        from utils import normalize_topic
        assert normalize_topic("") == ""

    def test_whitespace_returns_empty(self):
        from utils import normalize_topic
        assert normalize_topic("   ") == ""

    def test_adds_leading_slash(self):
        from utils import normalize_topic
        assert normalize_topic("devices/wb-mr6cv3_1/controls/K1") == "/devices/wb-mr6cv3_1/controls/K1"

    def test_preserves_single_leading_slash(self):
        from utils import normalize_topic
        assert normalize_topic("/devices/wb-mr6cv3_1/controls/K1") == "/devices/wb-mr6cv3_1/controls/K1"

    def test_collapses_multiple_leading_slashes(self):
        from utils import normalize_topic
        result = normalize_topic("///devices/wb-mr6cv3_1/controls/K1")
        assert result == "/devices/wb-mr6cv3_1/controls/K1"

    def test_strips_whitespace(self):
        from utils import normalize_topic
        result = normalize_topic("  /devices/test  ")
        assert result == "/devices/test"

    def test_strips_on_suffix(self):
        from utils import normalize_topic
        result = normalize_topic("/devices/wb-mr6cv3_1/controls/K1/on")
        assert result == "/devices/wb-mr6cv3_1/controls/K1"

    def test_does_not_strip_non_on_suffix(self):
        from utils import normalize_topic
        result = normalize_topic("/devices/wb-mr6cv3_1/controls/K1/off")
        assert result == "/devices/wb-mr6cv3_1/controls/K1/off"


class TestEncryptDecrypt:
    def test_round_trip(self):
        from utils import encrypt_secret, decrypt_secret
        plaintext = "my-secret-password-123"
        encrypted = encrypt_secret(plaintext)
        assert encrypted is not None
        assert encrypted != plaintext
        decrypted = decrypt_secret(encrypted)
        assert decrypted == plaintext

    def test_none_encrypt(self):
        from utils import encrypt_secret
        assert encrypt_secret(None) is None

    def test_none_decrypt(self):
        from utils import decrypt_secret
        assert decrypt_secret(None) is None

    def test_empty_decrypt(self):
        from utils import decrypt_secret
        assert decrypt_secret("") is None

    def test_unicode_round_trip(self):
        from utils import encrypt_secret, decrypt_secret
        plaintext = "пароль_unicode_密码"
        encrypted = encrypt_secret(plaintext)
        decrypted = decrypt_secret(encrypted)
        assert decrypted == plaintext

    def test_different_encryptions_differ(self):
        from utils import encrypt_secret
        e1 = encrypt_secret("test")
        e2 = encrypt_secret("test")
        # AES-GCM uses random IV, so encryptions should differ
        assert e1 != e2
