"""Tests for watchdog: cap-time, concurrent zone enforcement."""

import json
import os
from datetime import datetime, timedelta
from unittest.mock import MagicMock, patch

os.environ["TESTING"] = "1"


class TestZoneWatchdog:
    def test_zone_cap_enforcement(self, test_db):
        """Zone ON longer than cap should be force-stopped."""
        from services.watchdog import ZoneWatchdog

        # Create a zone that's been ON for 300 minutes
        server = test_db.create_mqtt_server({"name": "broker", "host": "127.0.0.1", "port": 1883})
        zone = test_db.create_zone(
            {
                "name": "Test Zone",
                "duration": 10,
                "group_id": 1,
                "mqtt_server_id": server["id"],
                "topic": "relay/test/on",
            }
        )
        past = (datetime.now() - timedelta(minutes=300)).strftime("%Y-%m-%d %H:%M:%S")
        test_db.update_zone(zone["id"], {"state": "on", "watering_start_time": past})

        mock_zc = MagicMock()

        def confirm_off(*_args, **_kwargs):
            test_db.update_zone(
                zone["id"],
                {"state": "off", "commanded_state": "off", "observed_state": "off"},
            )
            return True

        mock_zc.stop_zone.side_effect = confirm_off
        wd = ZoneWatchdog(test_db, mock_zc, interval=1)
        with patch.object(wd, "_send_alert") as mock_alert:
            wd._check_zones()

        # Should have called stop_zone
        mock_zc.stop_zone.assert_called_once_with(
            zone["id"],
            reason="watchdog_cap",
            force=True,
            require_observed_confirmation=True,
        )
        assert "Принудительно остановлена" in mock_alert.call_args.args[0]
        details = json.loads(test_db.get_logs(event_type="watchdog_cap_stop")[0]["details"])
        assert details["outcome"] == "confirmed_off"
        assert details["physical_channel"] is True
        assert details["observed_state"] == "off"

    def test_zone_cap_unconfirmed_stop_has_truthful_alert_and_log(self, test_db):
        """A rejected/unconfirmed OFF must be escalated, not reported as stopped."""
        from services.watchdog import ZoneWatchdog

        zone = test_db.create_zone({"name": "Test Zone", "duration": 10, "group_id": 1})
        past = (datetime.now() - timedelta(minutes=300)).strftime("%Y-%m-%d %H:%M:%S")
        test_db.update_zone(zone["id"], {"state": "on", "watering_start_time": past})

        mock_zc = MagicMock()
        mock_zc.stop_zone.return_value = False
        wd = ZoneWatchdog(test_db, mock_zc, interval=1)
        with patch.object(wd, "_send_alert") as mock_alert:
            wd._check_zones()

        message = mock_alert.call_args.args[0]
        assert "Принудительно остановлена" not in message
        assert "НЕ подтверждена" in message
        assert test_db.get_logs(event_type="watchdog_cap_stop") == []
        unresolved = test_db.get_logs(event_type="watchdog_cap_stop_unresolved")
        assert json.loads(unresolved[0]["details"])["outcome"] == "unresolved"

    def test_zone_cap_truthy_stop_with_unconfirmed_observed_state_is_unresolved(self, test_db):
        """Even an exact True cannot override a physical row that is not observed OFF."""
        from services.watchdog import ZoneWatchdog

        server = test_db.create_mqtt_server({"name": "broker", "host": "127.0.0.1", "port": 1883})
        zone = test_db.create_zone(
            {
                "name": "Physical Zone",
                "duration": 10,
                "group_id": 1,
                "mqtt_server_id": server["id"],
                "topic": "relay/physical/on",
            }
        )
        past = (datetime.now() - timedelta(minutes=300)).strftime("%Y-%m-%d %H:%M:%S")
        test_db.update_zone(zone["id"], {"state": "on", "watering_start_time": past})

        mock_zc = MagicMock()

        def leave_physical_confirmation_pending(*_args, **_kwargs):
            test_db.update_zone(
                zone["id"],
                {
                    "state": "off",
                    "commanded_state": "off",
                    "observed_state": "unconfirmed",
                },
            )
            return True

        mock_zc.stop_zone.side_effect = leave_physical_confirmation_pending
        wd = ZoneWatchdog(test_db, mock_zc, interval=1)
        with patch.object(wd, "_send_alert") as mock_alert:
            wd._check_zones()

        assert "Принудительно остановлена" not in mock_alert.call_args.args[0]
        assert test_db.get_logs(event_type="watchdog_cap_stop") == []
        details = json.loads(test_db.get_logs(event_type="watchdog_cap_stop_unresolved")[0]["details"])
        assert details["state"] == "off"
        assert details["commanded_state"] == "off"
        assert details["observed_state"] == "unconfirmed"

    def test_zone_within_cap_not_stopped(self, test_db):
        """Zone ON within cap should NOT be stopped."""
        from services.watchdog import ZoneWatchdog

        zone = test_db.create_zone(
            {
                "name": "Test Zone",
                "duration": 10,
                "group_id": 1,
            }
        )
        recent = (datetime.now() - timedelta(minutes=5)).strftime("%Y-%m-%d %H:%M:%S")
        test_db.update_zone(zone["id"], {"state": "on", "watering_start_time": recent})

        mock_zc = MagicMock()
        wd = ZoneWatchdog(test_db, mock_zc, interval=1)
        wd._check_zones()

        mock_zc.stop_zone.assert_not_called()

    def test_concurrent_zone_detection(self, test_db):
        """Detect too many concurrent ON zones."""
        from services.watchdog import ZoneWatchdog

        # Create 6 zones all ON (MAX_CONCURRENT_ZONES = 4)
        for i in range(6):
            zone = test_db.create_zone(
                {
                    "name": f"Zone {i}",
                    "duration": 10,
                    "group_id": 1,
                }
            )
            recent = (datetime.now() - timedelta(minutes=2)).strftime("%Y-%m-%d %H:%M:%S")
            test_db.update_zone(zone["id"], {"state": "on", "watering_start_time": recent})

        mock_zc = MagicMock()
        wd = ZoneWatchdog(test_db, mock_zc, interval=1)

        with patch.object(wd, "_send_alert") as mock_alert:
            wd._check_zones()
            # Should have sent an alert about concurrent zones
            mock_alert.assert_called()

    def test_get_zone_cap_default(self, test_db):
        """Default cap should be 240 minutes."""
        from services.watchdog import ZoneWatchdog

        wd = ZoneWatchdog(test_db, MagicMock(), interval=1)
        cap = wd._get_zone_cap_minutes()
        assert cap == 240

    def test_get_zone_cap_from_settings(self, test_db):
        """Cap from settings should override default."""
        from services.watchdog import ZoneWatchdog

        test_db.set_setting_value("zone_cap_minutes", "60")
        wd = ZoneWatchdog(test_db, MagicMock(), interval=1)
        cap = wd._get_zone_cap_minutes()
        assert cap == 60
