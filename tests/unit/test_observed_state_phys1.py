"""PHYS-1 / MASTER-C1 reconciliation contract tests.

These tests guard the observed-state contract:

    desired -> commanded -> observed

When the relay does NOT acknowledge the commanded state within
OBSERVED_STATE_MAX_RETRIES attempts, the zone must be pinned to
state='fault' so downstream consumers (scheduler, watchdog, UI)
stop trusting the application-side zone model and refuse to schedule
new irrigation on a physically unknown valve.

The core test `test_verify_sets_state_fault_after_mqtt_timeout`
mocks the MQTT subscribe/wait layer to always return False
(simulating a broker that stays silent), runs StateVerifier.verify()
synchronously, and asserts the zone row is transitioned to
state='fault' with fault_count incremented and last_fault stamped.
"""

import os
from unittest.mock import MagicMock, patch

os.environ["TESTING"] = "1"

from constants import OBSERVED_STATE_MAX_RETRIES


class TestPhys1Reconciliation:
    """PHYS-1: MQTT observation timeout -> state=fault."""

    def _make_zone_with_mqtt(self, test_db):
        """Create a zone wired to an MQTT server so verify() proceeds past
        the 'no topic / no server' early-return."""
        # Create MQTT server row
        server = test_db.create_mqtt_server(
            {
                "name": "test-broker",
                "host": "127.0.0.1",
                "port": 1883,
            }
        )
        assert server and server.get("id"), "fixture: mqtt server not created"

        zone = test_db.create_zone(
            {
                "name": "PHYS1-Zone",
                "duration": 10,
                "group_id": 1,
                "topic": "/devices/wb-mr6cv3_42/controls/K1",
                "mqtt_server_id": server["id"],
            }
        )
        assert zone and zone.get("id"), "fixture: zone not created"
        return zone, server

    def test_verify_sets_state_fault_after_mqtt_timeout(self, test_db):
        """Core PHYS-1 invariant.

        When MQTT never echoes the commanded state, after
        OBSERVED_STATE_MAX_RETRIES the zone is written as:
            state        = 'fault'
            fault_count += 1
            last_fault   = <timestamp>
        and verify() returns False.
        """
        from services.observed_state import StateVerifier

        zone, _server = self._make_zone_with_mqtt(test_db)
        zone_id = zone["id"]

        sv = StateVerifier()
        sv._db = test_db
        sv._notifier = MagicMock()

        before = test_db.get_zone(zone_id)
        initial_faults = int(before.get("fault_count") or 0)
        assert before.get("state") != "fault", "precondition: zone not already fault"

        # Mock the MQTT subscribe/wait layer to always time out.
        with (
            patch.object(StateVerifier, "_subscribe_and_wait", return_value=False) as mocked_wait,
            patch("services.events.publish", MagicMock()),
            patch("services.mqtt_pub.publish_mqtt_value", MagicMock()),
        ):
            result = sv.verify(zone_id, "on", timeout=0.01, retries=OBSERVED_STATE_MAX_RETRIES)

        # Contract: verify() returns False on full retry exhaustion.
        assert result is False, "verify() must return False on MQTT timeout"

        # Contract: _subscribe_and_wait invoked exactly OBSERVED_STATE_MAX_RETRIES.
        assert mocked_wait.call_count == OBSERVED_STATE_MAX_RETRIES, (
            f"expected {OBSERVED_STATE_MAX_RETRIES} MQTT wait attempts, got {mocked_wait.call_count}"
        )

        # Contract: zone is pinned to state='fault'.
        after = test_db.get_zone(zone_id)
        assert after.get("state") == "fault", f"PHYS-1 violated: expected state='fault', got {after.get('state')!r}"
        assert int(after.get("fault_count") or 0) == initial_faults + 1, "fault_count must be incremented"
        assert after.get("last_fault"), "last_fault must be stamped"

    def test_verify_success_does_not_mark_fault(self, test_db):
        """Inverse: if MQTT echoes the expected state, zone stays healthy."""
        from services.observed_state import StateVerifier

        zone, _server = self._make_zone_with_mqtt(test_db)
        zone_id = zone["id"]

        sv = StateVerifier()
        sv._db = test_db
        sv._notifier = MagicMock()

        # First attempt returns True -> verify() succeeds immediately.
        with patch.object(StateVerifier, "_subscribe_and_wait", return_value=True):
            result = sv.verify(zone_id, "on", timeout=0.01, retries=3)

        assert result is True
        after = test_db.get_zone(zone_id)
        assert after.get("state") != "fault", "successful verify must NOT mark zone as fault"
        assert int(after.get("fault_count") or 0) == 0

    def test_verify_retries_publish_between_attempts(self, test_db):
        """Between MQTT-wait attempts, the publish is retried
        (except on the final attempt — nothing to re-publish for)."""
        from services.observed_state import StateVerifier

        zone, _server = self._make_zone_with_mqtt(test_db)
        zone_id = zone["id"]

        sv = StateVerifier()
        sv._db = test_db
        sv._notifier = MagicMock()

        with (
            patch.object(StateVerifier, "_subscribe_and_wait", return_value=False),
            patch("services.events.publish", MagicMock()),
            patch("services.mqtt_pub.publish_mqtt_value") as mocked_publish,
        ):
            sv.verify(zone_id, "on", timeout=0.01, retries=OBSERVED_STATE_MAX_RETRIES)

        # retries=N -> publish retried (N-1) times (no republish after last wait)
        expected_publishes = OBSERVED_STATE_MAX_RETRIES - 1
        assert mocked_publish.call_count == expected_publishes, (
            f"expected {expected_publishes} retry-publishes, got {mocked_publish.call_count}"
        )

    def test_record_fault_persists_all_fields(self, test_db):
        """_record_fault() must persist state/fault_count/last_fault
        atomically through update_zone()."""
        from services.observed_state import StateVerifier

        zone, _server = self._make_zone_with_mqtt(test_db)
        zone_id = zone["id"]
        zone_data = test_db.get_zone(zone_id)

        sv = StateVerifier()
        sv._db = test_db
        sv._notifier = MagicMock()

        with patch("services.events.publish", MagicMock()):
            sv._record_fault(zone_id, zone_data, "on")

        after = test_db.get_zone(zone_id)
        assert after.get("state") == "fault"
        assert int(after.get("fault_count") or 0) == 1
        assert after.get("last_fault") is not None

    def test_verify_on_success_confirms_open_run(self, test_db):
        """A successful 'on' verification confirms the zone's open run — the
        independent second source so a real watering isn't recorded 'failed'
        even if the SSE hub's subscription is dead."""
        import sqlite3
        import time

        from services.observed_state import StateVerifier

        zone, _server = self._make_zone_with_mqtt(test_db)
        zone_id = zone["id"]
        run_id = test_db.create_zone_run(zone_id, 1, "2026-01-01 10:00:00", time.monotonic(), None, 1)

        sv = StateVerifier()
        sv._db = test_db
        sv._notifier = MagicMock()
        with patch.object(StateVerifier, "_subscribe_and_wait", return_value=True):
            assert sv.verify(zone_id, "on", timeout=0.01, retries=3) is True

        with sqlite3.connect(test_db.db_path) as conn:
            confirmed = conn.execute("SELECT confirmed FROM zone_runs WHERE id = ?", (run_id,)).fetchone()[0]
        assert confirmed == 1

    def test_verify_off_success_does_not_confirm(self, test_db):
        """An 'off' verification must NOT confirm a run — only a physical 'on'
        means the zone actually watered."""
        import sqlite3
        import time

        from services.observed_state import StateVerifier

        zone, _server = self._make_zone_with_mqtt(test_db)
        zone_id = zone["id"]
        start_time = "2026-01-01 10:00:00"
        run_id = test_db.create_zone_run(zone_id, 1, start_time, time.monotonic(), None, 1)
        # verify('off') normally follows the command transition.  Preserve
        # that ownership evidence so confirmation may finalize the exact run
        # while still leaving confirmed=0 (only ON proves watering occurred).
        test_db.update_zone(
            zone_id,
            {
                "state": "stopping",
                "commanded_state": "off",
                "observed_state": "unconfirmed",
                "watering_start_time": start_time,
                "command_id": "test-off-generation",
            },
        )

        sv = StateVerifier()
        sv._db = test_db
        sv._notifier = MagicMock()
        with patch.object(StateVerifier, "_subscribe_and_wait", return_value=True):
            result = sv.verify(zone_id, "off", timeout=0.01, retries=3)
        assert result is True, test_db.get_zone(zone_id)

        with sqlite3.connect(test_db.db_path) as conn:
            confirmed = conn.execute("SELECT confirmed FROM zone_runs WHERE id = ?", (run_id,)).fetchone()[0]
        assert confirmed == 0
