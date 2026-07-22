"""Tests for observed_state service: verify cycle, timeout, fault increment."""

import os
from unittest.mock import MagicMock, patch

os.environ["TESTING"] = "1"


class TestStateVerifier:
    def test_expected_payloads_on(self):
        from services.observed_state import StateVerifier

        sv = StateVerifier()
        payloads = sv._expected_payloads("on")
        assert "1" in payloads
        assert "on" in payloads
        assert "ON" in payloads

    def test_expected_payloads_off(self):
        from services.observed_state import StateVerifier

        sv = StateVerifier()
        payloads = sv._expected_payloads("off")
        assert "0" in payloads
        assert "off" in payloads
        assert "OFF" in payloads

    def test_verify_no_mqtt_module(self):
        """Without paho-mqtt, verify should return False."""
        from services.observed_state import StateVerifier

        sv = StateVerifier()
        sv._db = MagicMock()
        with patch("services.observed_state.mqtt", None):
            result = sv.verify(1, "on")
            assert result is False

    def test_verify_zone_not_found(self, test_db):
        """Verify on nonexistent zone should return False."""
        from services.observed_state import StateVerifier

        sv = StateVerifier()
        sv._db = test_db
        # Zone 9999 doesn't exist
        with patch("services.observed_state.mqtt", MagicMock()):
            result = sv.verify(9999, "on")
            assert result is False

    def test_verify_no_topic_skips(self, test_db):
        """Zone without topic should return True (nothing to verify)."""
        from services.observed_state import StateVerifier

        sv = StateVerifier()
        sv._db = test_db
        zone = test_db.create_zone({"name": "Z", "duration": 10, "group_id": 1})
        with patch("services.observed_state.mqtt", MagicMock()):
            result = sv.verify(zone["id"], "on")
            assert result is True

    def test_record_fault_increments_count(self, test_db):
        """Recording a fault should increment fault_count."""
        from services.observed_state import StateVerifier

        sv = StateVerifier()
        sv._db = test_db
        sv._notifier = MagicMock()
        server = test_db.create_mqtt_server({"name": "Test", "host": "127.0.0.1", "port": 1883, "enabled": 1})
        zone = test_db.create_zone(
            {
                "name": "Z",
                "duration": 10,
                "group_id": 1,
                "topic": "/test/z1",
                "mqtt_server_id": server["id"],
            }
        )
        zone_data = test_db.get_zone(zone["id"])
        initial_faults = int(zone_data.get("fault_count") or 0)

        with patch("services.events.publish", MagicMock()):
            sv._record_fault(zone["id"], zone_data, "on")

        # Re-read using the same db instance used by StateVerifier
        updated = sv._db.get_zone(zone["id"])
        assert int(updated.get("fault_count") or 0) == initial_faults + 1
        assert updated.get("last_fault") is not None

    def test_verify_async_skips_in_testing(self):
        """verify_async should skip in TESTING mode."""
        from services.observed_state import StateVerifier

        sv = StateVerifier()
        # Should not raise, just return immediately
        sv.verify_async(1, "on")
