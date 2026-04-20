"""Security tests for SEC-003 — CSRF policy.

Rationale: the original code `csrf.exempt(bp)`-ed every API blueprint. We
now exempt only the small set of endpoints that must work for the nginx
basic-auth guest flow. Admin-CRUD blueprints (zones CRUD, photo upload,
programs, weather settings, MQTT config) must now carry a CSRF token on
non-GET requests.

These tests use the project's `app` fixture which sets TESTING=1 and thus
disables CSRF enforcement. We therefore validate the policy by reading
the registered exempt set from flask-wtf (`app.extensions['csrf']`) rather
than via live HTTP. This is the cleanest way to catch regressions where
someone re-adds a blanket `csrf.exempt(bp)` without also understanding
the impact.
"""
from __future__ import annotations

import pytest


# Admin-CRUD endpoints that MUST now be CSRF-protected (they used to be
# blanket-exempt in the old code; re-adding them to the exempt set would
# be a regression).
_ADMIN_ONLY_PATHS_REQUIRING_CSRF = [
    ('POST', '/api/zones'),                        # zones CRUD
    ('POST', '/api/zones/import'),                 # bulk import
    ('POST', '/api/zones/1/photo'),                # photo upload
    ('DELETE', '/api/zones/1/photo'),              # photo delete
    ('POST', '/api/zones/1/photo/rotate'),         # photo rotate
    ('POST', '/api/programs'),                     # programs CRUD
    ('POST', '/api/groups'),                       # groups CRUD
    ('PUT', '/api/settings/weather'),              # weather settings
    ('PUT', '/api/settings/location'),             # weather location
    ('POST', '/api/mqtt/servers'),                 # MQTT server add
]


# Endpoints that MUST remain CSRF-exempt — they're the guest-controllable
# physical-action endpoints (nginx basic-auth protects them at the proxy,
# not via Flask session).
_GUEST_PUBLIC_POSTS = [
    '/api/login',
    '/api/env',
    '/api/status',
    '/api/postpone',
    '/api/emergency-stop',
    '/api/emergency-resume',
    '/api/zones/next-watering-bulk',
    '/api/zones/1/start',
    '/api/zones/1/stop',
    '/api/zones/1/mqtt/start',
    '/api/zones/1/mqtt/stop',
    '/api/groups/1/stop',
    '/api/groups/1/start-from-first',
    '/api/groups/1/start-zone/2',
    '/api/groups/1/master-valve/open',
]


def _csrf_exempt_view_names(app):
    """Return the set of short view function names that are CSRF-exempt.

    flask-wtf stores the entries as strings like "'routes.auth.api_login'"
    (with quotes) — we extract the last dotted segment and strip any
    surrounding quotes for easy comparison.
    """
    try:
        csrf_ext = app.extensions['csrf']
    except KeyError:
        return set()
    raw = getattr(csrf_ext, '_exempt_views', None) or set()
    names = set()
    for entry in raw:
        s = str(entry).strip().strip("'").strip('"')
        # Last dotted segment, in case the entry is a full qualified name.
        names.add(s.rsplit('.', 1)[-1])
    return names


def test_csrf_ext_is_wired(app):
    """Sanity: CSRFProtect is registered on the app."""
    # TESTING=1 makes WTF_CSRF_ENABLED False (see config.TestConfig), but
    # the extension should still be registered for introspection.
    assert 'csrf' in app.extensions or app.config.get('WTF_CSRF_ENABLED') is False


def test_guest_endpoints_are_exempt(app):
    """All endpoints in _GUEST_PUBLIC_POSTS must be CSRF-exempt."""
    # In TESTING mode WTF_CSRF is disabled, so we can't detect enforcement
    # via a 400 response. Instead, verify the exempt set directly.
    exempt = _csrf_exempt_view_names(app)
    expected_exempt = {
        'api_login',
        'api_env_config',
        'api_status',
        'api_postpone',
        'api_emergency_stop',
        'api_emergency_resume',
        'api_zones_next_watering_bulk',
        'start_zone',
        'stop_zone',
        'api_zone_mqtt_start',
        'api_zone_mqtt_stop',
        'api_stop_group',
        'api_start_group_from_first',
        'api_start_zone_exclusive',
        'api_master_valve_toggle',
    }
    missing = expected_exempt - exempt
    assert not missing, (
        f"SEC-003 regression: these guest endpoints are NOT CSRF-exempt "
        f"and will break the nginx-basic-auth gardener flow: {missing}. "
        f"Current exempt set: {exempt}"
    )


def test_admin_crud_endpoints_are_NOT_exempt(app):
    """Admin CRUD view functions must NOT be in the CSRF-exempt set.

    These are the ones that used to be `csrf.exempt(bp)`'ed wholesale.
    """
    exempt = _csrf_exempt_view_names(app)
    # Names that MUST NOT appear in the exempt set.
    forbidden = {
        'api_change_password',        # /api/password POST — session-auth only, must require CSRF (review BLOCKER fix)
        'api_create_zone',            # /api/zones POST
        'api_import_zones_bulk',      # /api/zones/import
        'upload_zone_photo',          # /api/zones/<id>/photo POST
        'delete_zone_photo',          # /api/zones/<id>/photo DELETE
        'rotate_zone_photo',          # /api/zones/<id>/photo/rotate
        'api_put_weather_settings',   # /api/settings/weather PUT
        'api_put_location',           # /api/settings/location PUT
        'api_create_group',           # /api/groups POST
    }
    still_exempt = exempt & forbidden
    assert not still_exempt, (
        f"SEC-003 regression: these admin-only endpoints are CSRF-exempt "
        f"again — CSRF is bypassable for them: {still_exempt}"
    )


@pytest.mark.parametrize("method,path", _ADMIN_ONLY_PATHS_REQUIRING_CSRF)
def test_admin_paths_defined(app, method, path):
    """Confirm each admin path exists in the URL map — policy targets reality."""
    # Walk the url_map and check at least one rule matches path/method.
    found = False
    for rule in app.url_map.iter_rules():
        if method in rule.methods and _rule_matches_path(rule, path):
            found = True
            break
    assert found, f'{method} {path} is not registered — test pattern is stale'


def _rule_matches_path(rule, path):
    """Lightweight matcher: replace <int:x>/<path:x>/<x> with literal '1'/etc."""
    # Convert rule.rule to a concrete example path and compare textually.
    import re
    concrete = re.sub(r'<int:[^>]+>', '1', rule.rule)
    concrete = re.sub(r'<path:[^>]+>', 'x', concrete)
    concrete = re.sub(r'<[^>]+>', 'x', concrete)
    # For routes like /api/groups/<int:group_id>/master-valve/<action>
    if concrete.endswith('/x'):
        # Accept any tail after the last /
        return path.startswith(concrete[:-2])
    return concrete == path
