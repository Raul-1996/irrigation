"""Integration test: /api/zones/<id>/mqtt/start writes a zone_runs row.

Regression for the bug fixed in fix/mqtt-start-unify: prior to that commit
the UI start endpoint duplicated start logic instead of delegating to
services.zone_control.exclusive_start_zone, so db.create_zone_run was
never called from UI starts. After this fix, every successful UI start
opens a zone_run row, every stop closes it, and last_watering_time is
derived from zone_runs.end_utc as the single source of truth.

Mirrors the test_client / patch pattern from tests/api/test_mqtt_api_deep.py.
"""

import json
import os
from unittest.mock import patch

import pytest

from tests.safety_contracts import confirmed_group_stop

os.environ["TESTING"] = "1"


def _make_zone(app, mqtt_id=None):
    """Create one zone in group 1 with a routable MQTT topic."""
    return app.db.create_zone(
        {
            "name": "MQTT Start Zone",
            "duration": 10,
            "group_id": 1,
            "topic": "/devices/test/K1",
            "mqtt_server_id": mqtt_id,
        }
    )


def _patch_publish():
    """Patch every publish call site exclusive_start_zone reaches."""
    return patch("services.zone_control.publish_mqtt_value", return_value=True)


def _water_monitor_patch():
    return patch("services.zone_control.water_monitor", **{"summarize_run.return_value": (None, None)})


class TestApiMqttStartWritesZoneRun:
    """The bug: UI start used to skip db.create_zone_run. Fixed by delegating."""

    @pytest.fixture(autouse=True)
    def _confirmed_group_stop(self, app):
        with confirmed_group_stop(app.db):
            yield

    def test_mqtt_start_creates_open_zone_run(self, admin_client, app):
        """POST /mqtt/start opens exactly one zone_run row with start_utc set, end_utc NULL."""
        srv = app.db.create_mqtt_server(
            {
                "name": "S1",
                "host": "127.0.0.1",
                "port": 1883,
                "enabled": 1,
            }
        )
        zone = _make_zone(app, mqtt_id=srv["id"])

        with (
            _patch_publish(),
            _water_monitor_patch(),
            patch("services.zone_control.state_verifier"),
        ):
            resp = admin_client.post(f"/api/zones/{zone['id']}/mqtt/start")

        assert resp.status_code == 200, resp.get_data(as_text=True)
        assert resp.get_json()["success"] is True

        run = app.db.get_open_zone_run(int(zone["id"]))
        assert run is not None, "expected an open zone_run row after mqtt/start"
        assert run.get("start_utc") is not None
        assert run.get("end_utc") is None

    def test_mqtt_start_then_stop_closes_zone_run(self, admin_client, app):
        """Start → stop keeps history pending until a fresh physical OFF, then closes it."""
        srv = app.db.create_mqtt_server(
            {
                "name": "S1",
                "host": "127.0.0.1",
                "port": 1883,
                "enabled": 1,
            }
        )
        zone = _make_zone(app, mqtt_id=srv["id"])

        with (
            _patch_publish(),
            _water_monitor_patch(),
            patch("services.zone_control.state_verifier"),
        ):
            r1 = admin_client.post(f"/api/zones/{zone['id']}/mqtt/start")
            assert r1.status_code == 200, r1.get_data(as_text=True)

            # Simulate the real relay-on echo on the open run so the finished
            # run stays status='ok' (unconfirmed runs are downgraded to 'failed').
            app.db.mark_zone_run_confirmed(int(zone["id"]))

            r2 = admin_client.post(f"/api/zones/{zone['id']}/mqtt/stop")
            assert r2.status_code == 200, r2.get_data(as_text=True)

            # Broker acceptance is not physical truth.  The command path must
            # leave the run open until the relay's fresh OFF report arrives.
            assert app.db.get_open_zone_run(int(zone["id"])) is not None

            from services.observed_state import StateVerifier

            verifier = StateVerifier()
            verifier._db = app.db
            assert (
                verifier.apply_live_confirmation(
                    int(zone["id"]),
                    "off",
                    db_instance=app.db,
                    scheduler_getter=lambda: None,
                )
                is True
            )

        # After confirmed OFF the previously-open run should be gone (closed).
        assert app.db.get_open_zone_run(int(zone["id"])) is None

        # And get_last_watering_time must now resolve — that's the user-facing
        # bug we were fixing (zone 1 manually started/stopped showed None).
        last = app.db.get_last_watering_time(int(zone["id"]))
        assert last is not None, (
            "last_watering_time must be derivable from zone_runs after a "
            "manual start+stop — this was the original production bug"
        )

    def test_mqtt_start_with_duration_override_uses_5_minutes(self, admin_client, app):
        """POST {"duration": 5} → planned_end_time ≈ start + 5 min, scheduler called with 5."""
        from datetime import datetime, timedelta

        srv = app.db.create_mqtt_server(
            {
                "name": "S1",
                "host": "127.0.0.1",
                "port": 1883,
                "enabled": 1,
            }
        )
        zone = _make_zone(app, mqtt_id=srv["id"])

        # We can't rely on the real scheduler being live in TESTING, so
        # observe the override via planned_end_time (the canonical carrier).
        before = datetime.now()
        with (
            _patch_publish(),
            _water_monitor_patch(),
            patch("services.zone_control.state_verifier"),
        ):
            resp = admin_client.post(
                f"/api/zones/{zone['id']}/mqtt/start",
                data=json.dumps({"duration": 5}),
                content_type="application/json",
            )
        assert resp.status_code == 200, resp.get_data(as_text=True)

        z = app.db.get_zone(int(zone["id"]))
        pet = z.get("planned_end_time")
        assert pet is not None
        end_dt = datetime.strptime(pet, "%Y-%m-%d %H:%M:%S")
        # Must be ~5 minutes from now (allow ±60s for clock drift / wall-time
        # jitter inside the request).
        expected_low = before + timedelta(minutes=5) - timedelta(seconds=60)
        expected_high = before + timedelta(minutes=5) + timedelta(seconds=60)
        assert expected_low <= end_dt <= expected_high, (
            f"planned_end_time {end_dt} not within ±60s of {before + timedelta(minutes=5)}"
        )

        # Base duration in DB must NOT be overwritten by the override —
        # override is one-shot for this run only.
        assert int(z.get("duration") or 0) == 10

    def test_mqtt_start_already_on_reschedule_no_new_zone_run(self, admin_client, app):
        """Second POST while ON with new duration reschedules planned_end, does NOT open a 2nd run row."""
        from datetime import datetime, timedelta

        srv = app.db.create_mqtt_server(
            {
                "name": "S1",
                "host": "127.0.0.1",
                "port": 1883,
                "enabled": 1,
            }
        )
        zone = _make_zone(app, mqtt_id=srv["id"])

        with (
            _patch_publish(),
            _water_monitor_patch(),
            patch("services.zone_control.state_verifier"),
        ):
            r1 = admin_client.post(f"/api/zones/{zone['id']}/mqtt/start")
            assert r1.status_code == 200, r1.get_data(as_text=True)

            run_after_first = app.db.get_open_zone_run(int(zone["id"]))
            assert run_after_first is not None
            first_run_id = int(run_after_first["id"])

            # Second POST while ON — must NOT open a new run row.
            before_second = datetime.now()
            r2 = admin_client.post(
                f"/api/zones/{zone['id']}/mqtt/start",
                data=json.dumps({"duration": 7}),
                content_type="application/json",
            )
            assert r2.status_code == 200, r2.get_data(as_text=True)

        # Same row — only ONE open run exists for this zone.
        run_after_second = app.db.get_open_zone_run(int(zone["id"]))
        assert run_after_second is not None
        assert int(run_after_second["id"]) == first_run_id, (
            "reschedule must reuse the existing open zone_run row, not create a new one"
        )

        # planned_end_time updated to ~7 minutes from second POST.
        z = app.db.get_zone(int(zone["id"]))
        pet = z.get("planned_end_time")
        assert pet is not None
        end_dt = datetime.strptime(pet, "%Y-%m-%d %H:%M:%S")
        expected_low = before_second + timedelta(minutes=7) - timedelta(seconds=60)
        expected_high = before_second + timedelta(minutes=7) + timedelta(seconds=60)
        assert expected_low <= end_dt <= expected_high, (
            f"rescheduled planned_end_time {end_dt} not within ±60s of {before_second + timedelta(minutes=7)}"
        )

    def test_mqtt_start_emergency_stop_returns_400_no_run(self, admin_client, app):
        """EMERGENCY_STOP guard runs BEFORE delegate — no zone_run row, 400 response."""
        srv = app.db.create_mqtt_server(
            {
                "name": "S1",
                "host": "127.0.0.1",
                "port": 1883,
                "enabled": 1,
            }
        )
        zone = _make_zone(app, mqtt_id=srv["id"])

        # Flip the EMERGENCY_STOP flag on the test app config.
        app.config["EMERGENCY_STOP"] = True
        try:
            with (
                _patch_publish(),
                _water_monitor_patch(),
                patch("services.zone_control.state_verifier"),
            ):
                resp = admin_client.post(f"/api/zones/{zone['id']}/mqtt/start")
        finally:
            app.config["EMERGENCY_STOP"] = False

        assert resp.status_code == 400
        assert app.db.get_open_zone_run(int(zone["id"])) is None
