"""Zones API — backward-compatible shim.

All zone API endpoints have been split into focused modules:
  - zones_crud_api.py    — CRUD, import, next-watering, duration conflicts
  - zones_photo_api.py   — photo upload/delete/rotate/get
  - zones_watering_api.py — start/stop, watering time, SSE, MQTT control

This file re-exports the blueprints and key symbols for backward compatibility.
"""
from routes.zones_crud_api import zones_crud_api_bp
from routes.zones_photo_api import zones_photo_api_bp, allowed_file, normalize_image
from routes.zones_watering_api import zones_watering_api_bp

# Legacy blueprint name — kept for any code that imports zones_api_bp directly.
# In app.py we now register the three focused blueprints instead.
zones_api_bp = zones_crud_api_bp

__all__ = [
    'zones_crud_api_bp',
    'zones_photo_api_bp',
    'zones_watering_api_bp',
    'zones_api_bp',
    'allowed_file',
    'normalize_image',
]
