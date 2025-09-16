import typing as _t
import os
import base64


def normalize_topic(topic: _t.Optional[str]) -> str:
    """Ensure MQTT topic starts with a single leading slash.

    - Trims whitespace
    - Converts None to empty string
    - Collapses multiple leading slashes to one
    """
    s = str(topic or "").strip()
    if not s:
        return ""
    if s.startswith('/'):
        # collapse multiple leading slashes
        i = 0
        n = len(s)
        while i < n and s[i] == '/':
            i += 1
        s = '/' + s[i:]
    else:
        s = '/' + s
    # Стрижём управляющий суффикс '/on' — используем только базовый топик
    if s.endswith('/on'):
        s = s[:-3]
    return s




# --- Simple symmetric encryption helpers for secrets ---
def _get_secret_key() -> bytes:
    key = os.getenv('IRRIG_SECRET_KEY')
    if key:
        try:
            return base64.urlsafe_b64decode(key + '===')
        except Exception:
            pass
    # fallback from hostname (weak, but better than plain)
    try:
        host = os.uname().nodename
    except Exception:
        host = 'irrigation'
    b = (host or 'irrigation').encode('utf-8')
    return (b * 4)[:32]

def encrypt_secret(plaintext: _t.Optional[str]) -> _t.Optional[str]:
    if plaintext is None:
        return None
    try:
        from Crypto.Cipher import AES  # pycryptodome
        from Crypto.Random import get_random_bytes
        key = _get_secret_key()
        iv = get_random_bytes(12)
        cipher = AES.new(key[:32], AES.MODE_GCM, nonce=iv)
        ct, tag = cipher.encrypt_and_digest(plaintext.encode('utf-8'))
        blob = b'aes:' + iv + tag + ct
        return base64.urlsafe_b64encode(blob).decode('utf-8')
    except Exception:
        # xor fallback
        b = plaintext.encode('utf-8')
        k = _get_secret_key()
        x = bytes([b[i] ^ k[i % len(k)] for i in range(len(b))])
        return 'xor:' + base64.urlsafe_b64encode(x).decode('utf-8')

def decrypt_secret(ciphertext: _t.Optional[str]) -> _t.Optional[str]:
    if not ciphertext:
        return None
    # AES-GCM preferred
    try:
        raw = base64.urlsafe_b64decode(ciphertext)
        if raw.startswith(b'aes:'):
            from Crypto.Cipher import AES
            raw = raw[4:]
            iv, tag, ct = raw[:12], raw[12:28], raw[28:]
            key = _get_secret_key()
            cipher = AES.new(key[:32], AES.MODE_GCM, nonce=iv)
            pt = cipher.decrypt_and_verify(ct, tag)
            return pt.decode('utf-8')
    except Exception:
        pass
    # xor fallback
    try:
        if ciphertext.startswith('xor:'):
            x = base64.urlsafe_b64decode(ciphertext[4:])
            k = _get_secret_key()
            b = bytes([x[i] ^ k[i % len(k)] for i in range(len(x))])
            return b.decode('utf-8')
    except Exception:
        return None
    return None
