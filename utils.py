import base64
import binascii
import logging
import os
import secrets
import stat
from datetime import datetime

logger = logging.getLogger(__name__)

_PRIVATE_FILE_MODE = stat.S_IRUSR | stat.S_IWUSR
_PRIVATE_DIR_MODE = stat.S_IRWXU
_SAFE_OPEN_FLAGS = getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
_DIRECTORY_OPEN_FLAGS = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | _SAFE_OPEN_FLAGS


class SecretKeyConfigurationError(RuntimeError):
    """A configured key is missing or damaged and must be restored.

    Silently replacing such a key would invalidate Flask sessions or make
    encrypted MQTT/Telegram credentials unrecoverable.  Messages raised by
    this class deliberately identify only the key source, never its material.
    """


def _open_private_directory(directory: str | os.PathLike[str]) -> int:
    """Open a real directory without following its final path component."""

    path = os.fspath(directory)
    descriptor = os.open(path, _DIRECTORY_OPEN_FLAGS)
    try:
        if not stat.S_ISDIR(os.fstat(descriptor).st_mode):
            raise NotADirectoryError(f"Private storage path is not a directory: {path}")
    except OSError:
        os.close(descriptor)
        raise
    return descriptor


def _harden_private_directory(directory: str | os.PathLike[str]) -> None:
    descriptor = _open_private_directory(directory)
    try:
        os.fchmod(descriptor, _PRIVATE_DIR_MODE)
    finally:
        os.close(descriptor)


def ensure_private_directory(directory: str | os.PathLike[str]) -> None:
    """Create *directory* as 0700 and harden an existing leaf directory.

    ``mode=`` passed to :func:`os.makedirs` is still filtered by the process
    umask and does not reliably cover intermediate directories.  Create every
    missing component explicitly and chmod it before it can hold a secret.
    Existing ancestors are left alone; only the requested leaf is hardened.
    """

    directory_path = os.path.abspath(os.fspath(directory))
    missing: list[str] = []
    cursor = directory_path
    while not os.path.lexists(cursor):
        missing.append(cursor)
        parent = os.path.dirname(cursor)
        if parent == cursor:
            break
        cursor = parent

    for path in reversed(missing):
        created = False
        try:
            os.mkdir(path, _PRIVATE_DIR_MODE)
            created = True
        except FileExistsError:
            pass
        try:
            _harden_private_directory(path)
        except PermissionError:
            if not created:
                raise
            # A maximally restrictive umask can remove owner permissions from
            # the directory we just created, preventing O_DIRECTORY open.  It
            # is safe to grant mode on this just-created path, then immediately
            # verify it through O_NOFOLLOW and constrain it via fchmod.
            os.chmod(path, _PRIVATE_DIR_MODE)
            _harden_private_directory(path)

    _harden_private_directory(directory_path)


def _ensure_private_parent(file_path: str | os.PathLike[str]) -> None:
    parent = os.path.dirname(os.path.abspath(os.fspath(file_path)))
    if not os.path.lexists(parent):
        ensure_private_directory(parent)
        return
    descriptor = _open_private_directory(parent)
    os.close(descriptor)


def ensure_private_file(file_path: str | os.PathLike[str], *, create: bool = False) -> None:
    """Harden a regular file to 0600, optionally creating it without a race."""

    path = os.fspath(file_path)
    if create:
        _ensure_private_parent(path)
    flags = os.O_RDWR | _SAFE_OPEN_FLAGS
    if create:
        flags |= os.O_CREAT
    descriptor = os.open(path, flags, _PRIVATE_FILE_MODE)
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode):
            raise OSError(f"Private storage path is not a regular file: {path}")
        os.fchmod(descriptor, _PRIVATE_FILE_MODE)
    finally:
        os.close(descriptor)


def read_private_file(file_path: str | os.PathLike[str]) -> bytes:
    """Read a regular file only after constraining it to owner read/write."""

    path = os.fspath(file_path)
    descriptor = os.open(path, os.O_RDONLY | _SAFE_OPEN_FLAGS)
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode):
            raise OSError(f"Private storage path is not a regular file: {path}")
        os.fchmod(descriptor, _PRIVATE_FILE_MODE)
        chunks: list[bytes] = []
        while True:
            chunk = os.read(descriptor, 64 * 1024)
            if not chunk:
                return b"".join(chunks)
            chunks.append(chunk)
    finally:
        os.close(descriptor)


def create_private_file(file_path: str | os.PathLike[str], data: bytes) -> None:
    """Atomically create a new 0600 file without exposing partial material."""

    path = os.path.abspath(os.fspath(file_path))
    _ensure_private_parent(path)
    parent = os.path.dirname(path)
    target_name = os.path.basename(path)
    temporary_name = f".{target_name}.{secrets.token_hex(8)}.tmp"
    directory_descriptor = _open_private_directory(parent)
    cleanup_temporary = True
    try:
        descriptor = os.open(
            temporary_name,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | _SAFE_OPEN_FLAGS,
            _PRIVATE_FILE_MODE,
            dir_fd=directory_descriptor,
        )
        try:
            try:
                os.fchmod(descriptor, _PRIVATE_FILE_MODE)
                view = memoryview(data)
                while view:
                    written = os.write(descriptor, view)
                    if written <= 0:
                        raise OSError(f"Unable to persist private file: {path}")
                    view = view[written:]
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
            # A hard link publishes the fully-written inode only if the target
            # is still absent.  Both names are resolved against the verified
            # directory fd, so a parent-path swap cannot redirect publication.
            os.link(
                temporary_name,
                target_name,
                src_dir_fd=directory_descriptor,
                dst_dir_fd=directory_descriptor,
                follow_symlinks=False,
            )
            try:
                # Persist the final directory entry before dropping the only
                # fallback name.  If this fsync fails, retain the 0600 temp hard
                # link so an operator can recover the generated material.
                os.fsync(directory_descriptor)
            except OSError:
                cleanup_temporary = False
                raise
            os.unlink(temporary_name, dir_fd=directory_descriptor)
            cleanup_temporary = False
        finally:
            if cleanup_temporary:
                try:
                    os.unlink(temporary_name, dir_fd=directory_descriptor)
                except FileNotFoundError:
                    pass
    finally:
        os.close(directory_descriptor)


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
    """Return one canonical MQTT *report/base* topic or ``""`` if unsafe.

    - Trims whitespace
    - Converts None to empty string
    - Collapses multiple leading slashes to one
    - Rejects the root topic and every actuator command topic ending ``/on``

    Callers append exactly one ``/on`` when publishing desired state.  Treating
    an already-suffixed value as a base topic is ambiguous and can make the
    application subscribe to or publish its own command channel.
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
    # MQTT wildcards are legal only for subscription filters, never for an
    # actuator's concrete report/command topic. NUL is invalid in MQTT UTF-8.
    if s == "/" or s.endswith("/on") or "+" in s or "#" in s or "\x00" in s:
        return ""
    return s


# --- Simple symmetric encryption helpers for secrets ---


class SecretDecryptionError(RuntimeError):
    """A stored secret cannot be decrypted with the configured key.

    Callers must treat this as a configuration/recovery error, never as an
    empty password.  The message is deliberately stable and does not expose
    cryptographic implementation details.
    """


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
    secret_key_file = ".irrig_secret_key"

    # 1. Check environment variable
    configured_key = os.getenv("IRRIG_SECRET_KEY")
    if configured_key:
        try:
            encoded = configured_key.strip().encode("ascii")
            padding = b"=" * (-len(encoded) % 4)
            decoded = base64.b64decode(encoded + padding, altchars=b"-_", validate=True)
        except (binascii.Error, UnicodeEncodeError, ValueError) as error:
            raise SecretKeyConfigurationError(
                "IRRIG_SECRET_KEY is invalid; restore a valid base64-encoded 32-byte key"
            ) from error
        if len(decoded) != 32:
            raise SecretKeyConfigurationError("IRRIG_SECRET_KEY is invalid; restore a valid base64-encoded 32-byte key")
        return decoded

    # 2. Try reading from file
    try:
        data = read_private_file(secret_key_file)
    except FileNotFoundError:
        data = None

    if data is not None:
        if len(data) != 32:
            raise SecretKeyConfigurationError("Encryption secret key file is invalid; restore the original 32-byte key")
        return data

    # 3. Generate new key and persist
    new_key = secrets.token_bytes(32)
    try:
        create_private_file(secret_key_file, new_key)
        return new_key
    except FileExistsError:
        # A second worker won the first-start race.  Never overwrite its key:
        # all workers must converge on the same persisted material.
        persisted = read_private_file(secret_key_file)
        if len(persisted) != 32:
            raise SecretKeyConfigurationError(
                "Encryption secret key file is invalid; restore the original 32-byte key"
            ) from None
        return persisted


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
            try:
                cipher = AES.new(key[:32], AES.MODE_GCM, nonce=iv)
                pt = cipher.decrypt_and_verify(ct, tag)
                return pt.decode("utf-8")
            except (TypeError, UnicodeError, ValueError) as e:
                raise SecretDecryptionError(
                    "Stored secret cannot be decrypted; restore the configured secret key"
                ) from e
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
