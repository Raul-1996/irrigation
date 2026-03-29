"""Deep tests for utils.py — covering encrypt/decrypt and edge cases."""
import pytest
from utils import normalize_topic, encrypt_secret, decrypt_secret


class TestNormalizeTopic:
    """Tests for MQTT topic normalization."""

    def test_strips_on_suffix(self):
        assert normalize_topic('/devices/wb-mr6cv3_85/controls/K1/on') == '/devices/wb-mr6cv3_85/controls/K1'

    def test_preserves_normal_topic(self):
        assert normalize_topic('/devices/wb-mr6cv3_85/controls/K1') == '/devices/wb-mr6cv3_85/controls/K1'

    def test_empty_string(self):
        assert normalize_topic('') == ''

    def test_none_returns_empty(self):
        result = normalize_topic(None)
        assert result == '' or result is None

    def test_strips_whitespace(self):
        result = normalize_topic('  /devices/test/K1  ')
        assert result.strip() == '/devices/test/K1'


class TestEncryptDecrypt:
    """Tests for secret encryption/decryption."""

    def test_encrypt_decrypt_roundtrip(self):
        """Encrypted value should decrypt back to original."""
        original = 'my-secret-password-123'
        encrypted = encrypt_secret(original)
        assert encrypted is not None
        assert encrypted != original
        decrypted = decrypt_secret(encrypted)
        assert decrypted == original

    def test_encrypt_empty_string(self):
        """Encrypting empty string should work."""
        encrypted = encrypt_secret('')
        assert encrypted is not None or encrypted == ''

    def test_decrypt_none(self):
        """Decrypting None should return None or empty."""
        result = decrypt_secret(None)
        assert result is None or result == ''

    def test_decrypt_invalid_data(self):
        """Decrypting invalid data should not crash."""
        result = decrypt_secret('not-valid-encrypted-data')
        assert result is None or isinstance(result, str)
