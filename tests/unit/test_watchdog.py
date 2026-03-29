"""Tests for watchdog: cap-time, concurrent zone enforcement."""
import pytest
import os
from unittest.mock import patch, MagicMock
from datetime import datetime, timedelta

os.environ['TESTING'] = '1'


class TestZoneWatchdog:
    def test_zone_cap_enforcement(self, test_db):
        """Zone ON longer than cap should be force-stopped."""
        from services.watchdog import ZoneWatchdog

        # Create a zone that's been ON for 300 minutes
        zone = test_db.create_zone({
            'name': 'Test Zone', 'duration': 10, 'group_id': 1,
        })
        past = (datetime.now() - timedelta(minutes=300)).strftime('%Y-%m-%d %H:%M:%S')
        test_db.update_zone(zone['id'], {'state': 'on', 'watering_start_time': past})

        mock_zc = MagicMock()
        wd = ZoneWatchdog(test_db, mock_zc, interval=1)
        wd._check_zones()

        # Should have called stop_zone
        mock_zc.stop_zone.assert_called_once_with(zone['id'], reason='watchdog_cap', force=True)

    def test_zone_within_cap_not_stopped(self, test_db):
        """Zone ON within cap should NOT be stopped."""
        from services.watchdog import ZoneWatchdog

        zone = test_db.create_zone({
            'name': 'Test Zone', 'duration': 10, 'group_id': 1,
        })
        recent = (datetime.now() - timedelta(minutes=5)).strftime('%Y-%m-%d %H:%M:%S')
        test_db.update_zone(zone['id'], {'state': 'on', 'watering_start_time': recent})

        mock_zc = MagicMock()
        wd = ZoneWatchdog(test_db, mock_zc, interval=1)
        wd._check_zones()

        mock_zc.stop_zone.assert_not_called()

    def test_concurrent_zone_detection(self, test_db):
        """Detect too many concurrent ON zones."""
        from services.watchdog import ZoneWatchdog

        # Create 6 zones all ON (MAX_CONCURRENT_ZONES = 4)
        for i in range(6):
            zone = test_db.create_zone({
                'name': f'Zone {i}', 'duration': 10, 'group_id': 1,
            })
            recent = (datetime.now() - timedelta(minutes=2)).strftime('%Y-%m-%d %H:%M:%S')
            test_db.update_zone(zone['id'], {'state': 'on', 'watering_start_time': recent})

        mock_zc = MagicMock()
        wd = ZoneWatchdog(test_db, mock_zc, interval=1)

        with patch.object(wd, '_send_alert') as mock_alert:
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
        test_db.set_setting_value('zone_cap_minutes', '60')
        wd = ZoneWatchdog(test_db, MagicMock(), interval=1)
        cap = wd._get_zone_cap_minutes()
        assert cap == 60
