"""System API — backward-compatible shim.

All system API endpoints have been split into focused modules:
  - system_status_api.py    — status, health, scheduler, logs, water, server-time
  - system_config_api.py    — auth, password, rain, env, map, postpone, settings
  - system_emergency_api.py — emergency stop/resume, backup

This file re-exports the blueprints for backward compatibility.
"""
from routes.system_status_api import system_status_api_bp
from routes.system_config_api import system_config_api_bp
from routes.system_emergency_api import system_emergency_api_bp

# Legacy blueprint name
system_api_bp = system_status_api_bp

__all__ = [
    'system_status_api_bp',
    'system_config_api_bp',
    'system_emergency_api_bp',
    'system_api_bp',
]
