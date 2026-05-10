"""Integration tests: weather coef=0 forces zone skip in scheduler runner.

Covers Issue #27 Phase 1: when get_coefficient() returns 0 (forced skip via
safety thresholds), the runner must NOT start the zone.
"""
import os
import sqlite3
import pytest
from unittest.mock import patch, MagicMock

os.environ['TESTING'] = '1'


def _populate_db(db_path):
    conn = sqlite3.connect(db_path)
    conn.execute("INSERT OR IGNORE INTO groups (id, name) VALUES (1, 'G1')")
    conn.execute(
        "INSERT OR IGNORE INTO mqtt_servers (id, name, host, port) VALUES (1, 'Local', '127.0.0.1', 1883)"
    )
    conn.execute(
        "INSERT OR IGNORE INTO zones (id, name, duration, group_id, topic, mqtt_server_id) "
        "VALUES (1, 'Газон', 15, 1, '/devices/wb-mr6cv3_1/controls/K1', 1)"
    )
    conn.commit()
    conn.close()


@pytest.fixture
def runner_db(tmp_path):
    db_path = str(tmp_path / "runner.db")
    from database import IrrigationDB
    db_instance = IrrigationDB(db_path=db_path)
    _populate_db(db_path)
    return db_instance


def test_zero_coef_skips_zone(runner_db):
    """coef=0 → adjusted duration=0 → zone is skipped (no exclusive_start_zone call)."""
    from irrigation_scheduler import IrrigationScheduler

    sched = IrrigationScheduler(runner_db)

    with patch('services.weather_adjustment.get_weather_adjustment') as mock_wa, \
         patch('services.zone_control.exclusive_start_zone', return_value=True) as mock_start, \
         patch('services.mqtt_pub.publish_mqtt_value', return_value=True):
        mock_adj = MagicMock()
        mock_adj.is_enabled.return_value = True
        mock_adj.get_coefficient.return_value = 0  # safety skip (e.g. factor_rain off + 50mm)
        mock_adj.should_skip.return_value = {'skip': False}  # flags off → should_skip says no
        mock_adj.log_adjustment = MagicMock()
        mock_wa.return_value = mock_adj

        # Verify: helper returns 0 for any base duration
        adjusted = sched._get_weather_adjusted_duration(1, 15)
        assert adjusted == 0, "coef=0 must produce adjusted duration=0 (not base_duration)"

        # And: running the program with this zone must NOT call exclusive_start_zone
        sched._run_program_threaded(1, [1], 'Test')
        assert mock_start.call_count == 0, "Zone with adjusted=0 must be skipped, not started"
