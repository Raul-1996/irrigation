"""Shared API helpers used across multiple route blueprints."""

import logging
import os
import re
from datetime import datetime

from flask import jsonify

logger = logging.getLogger(__name__)


# ── Path-traversal guard ───────────────────────────────────────────────────
class UnsafePathError(ValueError):
    """Raised when a user-controllable relative path escapes its base dir.

    See SEC-009: zone.photo_path is read from the DB and used to build
    filesystem paths in delete/get/rotate zone photo endpoints. Even
    though photo_path is populated by the upload handler (which controls
    the name), a future bulk-import or migration bug could persist a
    relative path with `..` segments. We normalize and check on every
    read, not just on write — defense in depth.
    """


# Whitelist: legal zone photo filename pattern.
# Matches what upload_zone_photo writes: "ZONE_<id>.<ext>" or "ZONE_<id>_thumb.<ext>".
# Issue #11 added the optional `_thumb` suffix; the parenthesised group is the
# only widening — anything else (e.g. ZONE_5_thumbb.webp, ZONE_5_thumb_evil.webp)
# still fails the anchored match.
_ZONE_PHOTO_FILENAME_RE = re.compile(
    r"^ZONE_\d+(_thumb)?\.(png|jpg|jpeg|gif|webp)$",
    re.IGNORECASE,
)


def safe_media_subpath(base_dir: str, relative_path: str) -> str:
    """Return absolute path inside *base_dir*, or raise UnsafePathError.

    Rejects:
      * absolute paths
      * empty / None
      * paths containing `..` components
      * paths that, after normalization, resolve outside *base_dir*

    The function does NOT check filesystem existence — callers must do
    that separately. It only validates the path STRING.
    """
    if not relative_path or not isinstance(relative_path, str):
        raise UnsafePathError("empty or non-string path")
    # Reject absolute paths immediately — never join an absolute path.
    if os.path.isabs(relative_path):
        raise UnsafePathError("absolute path not allowed")
    # Reject NUL byte (Python < 3.x protection; still prudent).
    if "\x00" in relative_path:
        raise UnsafePathError("NUL byte in path")
    # Normalize and ensure the real resolved path is within base_dir.
    base_abs = os.path.abspath(base_dir)
    joined = os.path.abspath(os.path.join(base_abs, relative_path))
    # `commonpath` raises on mixed drives (Windows) — here irrigation is
    # Linux-only; still, wrap defensively.
    try:
        common = os.path.commonpath([base_abs, joined])
    except ValueError as exc:
        raise UnsafePathError(f"path resolution failed: {exc}") from exc
    if common != base_abs:
        raise UnsafePathError(f"path escapes base_dir (base={base_abs!r}, resolved={joined!r})")
    return joined


def safe_zone_photo_path(photo_path: str) -> str:
    """Validate *photo_path* (DB column) and return absolute filesystem path.

    Expected structure: `media/zones/ZONE_<id>.<ext>`. The path must live
    under `static/` and the filename must match _ZONE_PHOTO_FILENAME_RE.
    Raises UnsafePathError on anything else.
    """
    if not photo_path:
        raise UnsafePathError("empty photo_path")
    # First layer: the DB value must live under static/ — reject if it
    # already has a leading slash or tries to escape media/zones/.
    filename = os.path.basename(photo_path)
    if not _ZONE_PHOTO_FILENAME_RE.match(filename):
        raise UnsafePathError(f"invalid zone photo filename: {filename!r}")
    # Second layer: normalise and check containment inside static/.
    return safe_media_subpath("static", photo_path)


# Unified API error helpers
def api_error(error_code: str, message: str, status: int = 400, extra: dict | None = None):
    payload = {"success": False, "error_code": str(error_code), "message": str(message)}
    if extra:
        try:
            payload.update(extra)
        except (TypeError, ValueError) as e:
            logger.debug("Handled exception in api_error: %s", e)
    return jsonify(payload), int(status)


def api_soft(error_code: str, message: str, extra: dict | None = None):
    """Soft 200 responses with explicit error_code for diagnostics."""
    return api_error(error_code, message, 200, extra)


def parse_dt(s: str):
    """Parse datetime string in common formats."""
    if not s:
        return None
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M"):
        try:
            return datetime.strptime(s, fmt)
        except (ValueError, TypeError, KeyError) as e:
            logger.debug("Exception in parse_dt: %s", e)
            continue
    return None


# Media / upload constants
MEDIA_ROOT = "static/media"
ZONE_MEDIA_SUBDIR = "zones"
MAP_MEDIA_SUBDIR = "maps"
UPLOAD_FOLDER = os.path.join(MEDIA_ROOT, ZONE_MEDIA_SUBDIR)
MAP_DIR = os.path.join(MEDIA_ROOT, MAP_MEDIA_SUBDIR)
ALLOWED_EXTENSIONS = {"png", "jpg", "jpeg", "gif", "webp"}
ALLOWED_MIME_TYPES = {"image/png", "image/jpeg", "image/gif", "image/webp"}
MAX_FILE_SIZE = 20 * 1024 * 1024  # 20MB (issue #11)

# Ensure directories exist
os.makedirs(UPLOAD_FOLDER, exist_ok=True)
os.makedirs(MAP_DIR, exist_ok=True)
