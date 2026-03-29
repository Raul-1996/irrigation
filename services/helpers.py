"""Shared API helpers used across multiple route blueprints."""
from flask import jsonify
from datetime import datetime
import os
import logging

logger = logging.getLogger(__name__)

# Unified API error helpers
def api_error(error_code: str, message: str, status: int = 400, extra: dict = None):
    payload = {'success': False, 'error_code': str(error_code), 'message': str(message)}
    if extra:
        try:
            payload.update(extra)
        except (TypeError, ValueError) as e:
            logger.debug("Handled exception in api_error: %s", e)
    return jsonify(payload), int(status)


def api_soft(error_code: str, message: str, extra: dict = None):
    """Soft 200 responses with explicit error_code for diagnostics."""
    return api_error(error_code, message, 200, extra)


def parse_dt(s: str):
    """Parse datetime string in common formats."""
    if not s:
        return None
    for fmt in ('%Y-%m-%d %H:%M:%S', '%Y-%m-%d %H:%M'):
        try:
            return datetime.strptime(s, fmt)
        except (ValueError, TypeError, KeyError) as e:
            logger.debug("Exception in parse_dt: %s", e)
            continue
    return None


# Media / upload constants
MEDIA_ROOT = 'static/media'
ZONE_MEDIA_SUBDIR = 'zones'
MAP_MEDIA_SUBDIR = 'maps'
UPLOAD_FOLDER = os.path.join(MEDIA_ROOT, ZONE_MEDIA_SUBDIR)
MAP_DIR = os.path.join(MEDIA_ROOT, MAP_MEDIA_SUBDIR)
ALLOWED_EXTENSIONS = {'png', 'jpg', 'jpeg', 'gif', 'webp'}
ALLOWED_MIME_TYPES = {'image/png', 'image/jpeg', 'image/gif', 'image/webp'}
MAX_FILE_SIZE = 5 * 1024 * 1024  # 5MB

# Ensure directories exist
os.makedirs(UPLOAD_FOLDER, exist_ok=True)
os.makedirs(MAP_DIR, exist_ok=True)
