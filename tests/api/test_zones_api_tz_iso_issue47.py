"""API contract tests for issue #47: planned_end_time / watering_start_time
returned by /api/zones (and /api/zones/<id>) must be ISO-8601 with an explicit
TZ offset, so the browser's ``new Date(...)`` parses them as the controller's
wall-clock time — not as device-local time.

Old format (bug): "2026-05-28 00:45:52"   <- ambiguous, JS parses as device-local
New format (fix): "2026-05-28T00:45:52+05:00"

The DB representation is unchanged (the schema and ``db.get_zone()`` still
return the naive ``"YYYY-MM-DD HH:MM:SS"`` form); only the JSON serialisation
at the API boundary is normalised. That keeps the existing Python tests
(which call ``datetime.strptime(zone["planned_end_time"], "%Y-%m-%d %H:%M:%S")``
on the DB row) untouched.
"""

import json
import re
from datetime import datetime, timedelta

# Match ISO-8601 with explicit offset or trailing 'Z'.
ISO_WITH_TZ = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}([+-]\d{2}:\d{2}|Z)$")


def _set_watering(app, zone_id: int, dur_min: int = 5):
    """Put zone into a synthetic 'on' state with both timestamps set, the
    same way the production start path does (services.zone_control writes
    naive "YYYY-MM-DD HH:MM:SS")."""
    now = datetime.now()
    start = now.strftime("%Y-%m-%d %H:%M:%S")
    end = (now + timedelta(minutes=dur_min)).strftime("%Y-%m-%d %H:%M:%S")
    app.db.update_zone(
        zone_id,
        {
            "state": "on",
            "watering_start_time": start,
            "planned_end_time": end,
        },
    )
    return start, end


class TestZonesApiTzIso:
    def test_api_zones_list_emits_iso_with_tz(self, admin_client, app):
        """GET /api/zones — running zone's planned_end_time has TZ suffix."""
        zone = app.db.create_zone({"name": "TZ-List", "duration": 5, "group_id": 1})
        _set_watering(app, zone["id"], dur_min=5)

        resp = admin_client.get("/api/zones")
        assert resp.status_code == 200
        zones = resp.get_json()
        match = next((z for z in zones if z["id"] == zone["id"]), None)
        assert match is not None, "test zone missing from /api/zones response"

        assert match.get("planned_end_time"), "planned_end_time missing"
        assert ISO_WITH_TZ.match(match["planned_end_time"]), (
            f"planned_end_time {match['planned_end_time']!r} lacks TZ offset"
        )
        assert match.get("watering_start_time"), "watering_start_time missing"
        assert ISO_WITH_TZ.match(match["watering_start_time"]), (
            f"watering_start_time {match['watering_start_time']!r} lacks TZ offset"
        )

    def test_api_zones_single_emits_iso_with_tz(self, admin_client, app):
        """GET /api/zones/<id> — single zone payload follows the same rule."""
        zone = app.db.create_zone({"name": "TZ-Single", "duration": 5, "group_id": 1})
        _set_watering(app, zone["id"], dur_min=5)

        resp = admin_client.get(f"/api/zones/{zone['id']}")
        assert resp.status_code == 200
        body = resp.get_json()

        assert ISO_WITH_TZ.match(body["planned_end_time"]), (
            f"planned_end_time {body['planned_end_time']!r} lacks TZ offset"
        )
        assert ISO_WITH_TZ.match(body["watering_start_time"]), (
            f"watering_start_time {body['watering_start_time']!r} lacks TZ offset"
        )

    def test_db_storage_unchanged(self, admin_client, app):
        """Regression guard: the DB row keeps the naive "YYYY-MM-DD HH:MM:SS"
        form. Several existing tests parse it with
        ``strptime("%Y-%m-%d %H:%M:%S")`` against ``app.db.get_zone()``; if
        that format ever silently shifts to ISO, those tests would break."""
        zone = app.db.create_zone({"name": "TZ-DB", "duration": 5, "group_id": 1})
        _, end = _set_watering(app, zone["id"], dur_min=5)

        row = app.db.get_zone(zone["id"])
        assert row["planned_end_time"] == end
        # Format check — parsable via the canonical naive format.
        datetime.strptime(row["planned_end_time"], "%Y-%m-%d %H:%M:%S")

    def test_remaining_time_calc_matches_server_regardless_of_device_tz(self, admin_client, app):
        """End-to-end acceptance from issue #47:

        Even if the device parses the API string as if it were local time
        (i.e. ``datetime.fromisoformat(payload)`` — same semantics as
        ``new Date(...)`` in JS), the resulting wall-clock instant must
        equal the controller's ``planned_end_time`` instant. That means
        ``(end - now)`` on the device equals ``(end - now)`` on the server,
        i.e. ~5 minutes after starting a 5-minute zone.

        Pre-fix: the API emitted a TZ-less string and JS treated it as
        device-local, shifting the computed remaining by (controller_tz -
        device_tz) — 125-130 minutes for the Moscow/Yekaterinburg pair.
        """
        zone = app.db.create_zone({"name": "TZ-Calc", "duration": 5, "group_id": 1})
        _set_watering(app, zone["id"], dur_min=5)

        resp = admin_client.get("/api/zones")
        z = next(z for z in resp.get_json() if z["id"] == zone["id"])

        # Simulate the browser parse path: ISO with offset -> aware datetime.
        end_aware = datetime.fromisoformat(z["planned_end_time"])
        # Convert to local-naive for comparison against datetime.now() (which
        # is the same conversion the JS does when subtracting Date.now()).
        end_local_naive = end_aware.astimezone().replace(tzinfo=None)

        remaining_sec = (end_local_naive - datetime.now()).total_seconds()
        # 5-minute zone: remaining should be ~300 s (allow generous ±15 s for
        # CI clock skew between the two now() reads and any test slowness).
        assert 285 <= remaining_sec <= 315, f"remaining {remaining_sec}s outside [285, 315] — TZ handling drifted"

    def test_api_response_serialisable_back_to_json(self, admin_client, app):
        """Sanity: the response is plain JSON (no datetime objects leaked)."""
        zone = app.db.create_zone({"name": "TZ-JSON", "duration": 5, "group_id": 1})
        _set_watering(app, zone["id"], dur_min=5)
        resp = admin_client.get("/api/zones")
        json.dumps(resp.get_json())  # raises if non-serialisable
