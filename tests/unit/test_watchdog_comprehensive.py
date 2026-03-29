"""Comprehensive tests for services/watchdog.py."""
import pytest
import os
import time
import json
from datetime import datetime, timedelta
from unittest.mock import patch, MagicMock

os.environ['TESTING'] = '1'


class TestZoneWatchdog:
    def test_init(self, test_db):
        from services.watchdog import ZoneWatchdog
        mock_zc = MagicMock()
        wd = ZoneWatchdog(test_db, mock_zc, interval=1)
        assert wd.db is test_db
        assert wd.interval == 1

    def test_get_zone_cap_default(self, test_db):
        from services.watchdog import ZoneWatchdog
        wd = ZoneWatchdog(test_db, MagicMock())
        cap = wd._get_zone_cap_minutes()
        assert cap == 240  # default

    def test_get_zone_cap_from_settings(self, test_db):
        from services.watchdog import ZoneWatchdog
        test_db.set_setting_value('zone_cap_minutes', '120')
        wd = ZoneWatchdog(test_db, MagicMock())
        cap = wd._get_zone_cap_minutes()
        assert cap == 120

    def test_get_zone_cap_minimum_1(self, test_db):
        from services.watchdog import ZoneWatchdog
        test_db.set_setting_value('zone_cap_minutes', '0')
        wd = ZoneWatchdog(test_db, MagicMock())
        cap = wd._get_zone_cap_minutes()
        assert cap == 1

    def test_check_zones_no_zones(self, test_db):
        from services.watchdog import ZoneWatchdog
        wd = ZoneWatchdog(test_db, MagicMock())
        wd._check_zones()  # should not crash

    def test_check_zones_under_cap(self, test_db):
        from services.watchdog import ZoneWatchdog
        z = test_db.create_zone({'name': 'Z1', 'duration': 10, 'group_id': 1})
        start = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        test_db.update_zone(z['id'], {'state': 'on', 'watering_start_time': start})

        mock_zc = MagicMock()
        wd = ZoneWatchdog(test_db, mock_zc)
        wd._check_zones()
        mock_zc.stop_zone.assert_not_called()

    def test_check_zones_over_cap(self, test_db):
        from services.watchdog import ZoneWatchdog
        z = test_db.create_zone({'name': 'Z1', 'duration': 10, 'group_id': 1})
        old_start = (datetime.now() - timedelta(minutes=300)).strftime('%Y-%m-%d %H:%M:%S')
        test_db.update_zone(z['id'], {'state': 'on', 'watering_start_time': old_start})

        mock_zc = MagicMock()
        wd = ZoneWatchdog(test_db, mock_zc)
        with patch.object(wd, '_send_alert'):
            wd._check_zones()
            mock_zc.stop_zone.assert_called_once_with(z['id'], reason='watchdog_cap', force=True)

    def test_check_zones_concurrent_alert(self, test_db):
        from services.watchdog import ZoneWatchdog
        # Create more than MAX_CONCURRENT_ZONES zones all ON
        for i in range(6):
            z = test_db.create_zone({'name': f'Z{i}', 'duration': 10, 'group_id': 1})
            start = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
            test_db.update_zone(z['id'], {'state': 'on', 'watering_start_time': start})

        mock_zc = MagicMock()
        wd = ZoneWatchdog(test_db, mock_zc)
        with patch.object(wd, '_send_alert') as mock_alert:
            wd._check_zones()
            # Should alert about concurrent zones
            assert mock_alert.called

    def test_send_alert_no_admin(self, test_db):
        from services.watchdog import ZoneWatchdog
        wd = ZoneWatchdog(test_db, MagicMock())
        wd._send_alert("test")  # no admin chat, should not crash

    def test_send_alert_with_admin(self, test_db):
        from services.watchdog import ZoneWatchdog
        test_db.set_setting_value('telegram_admin_chat_id', '12345')
        wd = ZoneWatchdog(test_db, MagicMock())
        with patch('services.telegram_bot.notifier') as mock_notifier:
            wd._send_alert("test alert")

    def test_stop_event(self, test_db):
        from services.watchdog import ZoneWatchdog
        wd = ZoneWatchdog(test_db, MagicMock(), interval=1)
        wd.stop()
        assert wd._stop_event.is_set()


class TestStartWatchdog:
    def test_start_watchdog(self, test_db):
        from services.watchdog import start_watchdog, _watchdog_lock
        import services.watchdog as wdmod
        old = wdmod._watchdog_instance
        wdmod._watchdog_instance = None
        try:
            mock_zc = MagicMock()
            wd = start_watchdog(test_db, mock_zc, interval=1)
            assert wd is not None
            wd.stop()
            wd.join(timeout=3)
        finally:
            wdmod._watchdog_instance = old
