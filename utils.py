import base64
import logging
import os
import secrets
import stat
from datetime import datetime

logger = logging.getLogger(__name__)


def to_iso_with_tz(s: str | None) -> str | None:
    """Convert a naive controller-local timestamp string to ISO-8601 with TZ offset.

    Input: "YYYY-MM-DD HH:MM:SS" (as stored in the zones table — naive,
    implicitly in the controller's local TZ, written by ``datetime.now()``).
    Output: e.g. "2026-05-28T00:45:52+05:00".

    Why this exists (issue #47): the browser parses
    ``new Date("2026-05-28 00:45:52")`` as device-local time. When the device
    TZ differs from the controller TZ (Moscow phone, Yekaterinburg controller),
    the displayed timer was shifted by the offset. Emitting an explicit TZ
    suffix removes the ambiguity for every JS consumer at once, without
    changing the DB storage format (which Python ``strptime`` callers rely on).

    Returns the input unchanged if it is None, empty, or already TZ-aware
    (contains a '+'/'-' offset after the time part or a trailing 'Z').
    """
    if not s:
        return s
    txt = str(s)
    # Already TZ-aware? Check for 'Z' suffix or offset after position 10
    # (skip the date part where '-' is the separator).
    if txt.endswith("Z") or "+" in txt[10:] or "-" in txt[10:]:
        return txt
    try:
        naive = datetime.strptime(txt, "%Y-%m-%d %H:%M:%S")
    except (ValueError, TypeError):
        return txt
    # astimezone() with no arg uses the system local TZ, matching the
    # convention of ``datetime.now()`` used everywhere in the server.
    aware = naive.astimezone()
    return aware.isoformat(timespec="seconds")


def normalize_topic(topic: str | None) -> str:
    """Ensure MQTT topic starts with a single leading slash.

    - Trims whitespace
    - Converts None to empty string
    - Collapses multiple leading slashes to one
    """
    s = str(topic or "").strip()
    if not s:
        return ""
    if s.startswith("/"):
        # collapse multiple leading slashes
        i = 0
        n = len(s)
        while i < n and s[i] == "/":
            i += 1
        s = "/" + s[i:]
    else:
        s = "/" + s
    # Стрижём управляющий суффикс '/on' — используем только базовый топик
    if s.endswith("/on"):
        s = s[:-3]
    return s


# --- Simple symmetric encryption helpers for secrets ---


def _get_hostname_key() -> bytes:
    """Compute the old hostname-based key (for migration only)."""
    try:
        host = os.uname().nodename
    except (ValueError, TypeError) as e:
        logger.debug("Exception in _get_hostname_key: %s", e)
        host = "irrigation"
    b = (host or "irrigation").encode("utf-8")
    return (b * 4)[:32]


def _get_secret_key() -> bytes:
    """Load or generate IRRIG_SECRET_KEY.

    Priority:
    1. Environment variable IRRIG_SECRET_KEY (base64-encoded)
    2. File .irrig_secret_key (raw 32 bytes)
    3. Generate new random 32 bytes, persist to file
    """
    _SECRET_KEY_FILE = ".irrig_secret_key"

    # 1. Check environment variable
    key = os.getenv("IRRIG_SECRET_KEY")
    if key:
        try:
            return base64.urlsafe_b64decode(key + "===")
        except (ValueError, TypeError) as e:
            logger.debug("Handled exception in _get_secret_key: %s", e)

    # 2. Try reading from file
    try:
        with open(_SECRET_KEY_FILE, "rb") as f:
            data = f.read()
        if len(data) >= 32:
            return data[:32]
    except FileNotFoundError:
        logging.getLogger(__name__).debug("Secret key file not found, will generate new one")

    # 3. Generate new key and persist
    new_key = secrets.token_bytes(32)
    with open(_SECRET_KEY_FILE, "wb") as f:
        f.write(new_key)
    os.chmod(_SECRET_KEY_FILE, stat.S_IRUSR | stat.S_IWUSR)  # 0o600
    return new_key


def encrypt_secret(plaintext: str | None) -> str | None:
    if plaintext is None:
        return None
    try:
        from Crypto.Cipher import AES  # pycryptodome
        from Crypto.Random import get_random_bytes

        key = _get_secret_key()
        iv = get_random_bytes(12)
        cipher = AES.new(key[:32], AES.MODE_GCM, nonce=iv)
        ct, tag = cipher.encrypt_and_digest(plaintext.encode("utf-8"))
        blob = b"aes:" + iv + tag + ct
        return base64.urlsafe_b64encode(blob).decode("utf-8")
    except ImportError as e:
        logger.debug("Exception in encrypt_secret: %s", e)
        # xor fallback
        b = plaintext.encode("utf-8")
        k = _get_secret_key()
        x = bytes([b[i] ^ k[i % len(k)] for i in range(len(b))])
        return "xor:" + base64.urlsafe_b64encode(x).decode("utf-8")


def decrypt_secret(ciphertext: str | None) -> str | None:
    if not ciphertext:
        return None
    # AES-GCM preferred
    try:
        raw = base64.urlsafe_b64decode(ciphertext)
        if raw.startswith(b"aes:"):
            from Crypto.Cipher import AES

            raw = raw[4:]
            iv, tag, ct = raw[:12], raw[12:28], raw[28:]
            key = _get_secret_key()
            cipher = AES.new(key[:32], AES.MODE_GCM, nonce=iv)
            pt = cipher.decrypt_and_verify(ct, tag)
            return pt.decode("utf-8")
    except ImportError as e:
        logger.debug("Handled exception in decrypt_secret: %s", e)
    # xor fallback
    try:
        if ciphertext.startswith("xor:"):
            x = base64.urlsafe_b64decode(ciphertext[4:])
            k = _get_secret_key()
            b = bytes([x[i] ^ k[i % len(k)] for i in range(len(x))])
            return b.decode("utf-8")
    except (KeyError, TypeError, ValueError) as e:
        logger.debug("Exception in decrypt_secret: %s", e)
        return None
    return None
