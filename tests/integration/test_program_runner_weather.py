"""Integration tests: weather coef=0 forces zone skip in scheduler runner.

Covers Issue #27 Phase 1: when get_coefficient() returns 0 (forced skip via
safety thresholds), the runner must NOT start the zone.

Phase 2 (#28): runner writes weather_log + weather_decisions during program
execution.
"""
import json
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
    # Reset the weather adjustment singleton so the next get_weather_adjustment
    # call binds to THIS test's db_path (singleton is process-keyed).
    import services.weather.singletons as _wsing
    _wsing._adjustment = None
    _wsing._weather_service = None
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


def _mock_weather_data(temperature=22.0, humidity=55.0, precipitation_24h=0.5,
                       wind_speed=3.0):
    m = MagicMock()
    m.temperature = temperature
    m.humidity = humidity
    m.precipitation_24h = precipitation_24h
    m.precipitation_forecast_6h = 0.0
    m.wind_speed = wind_speed
    m.daily_et0 = 4.5
    m.timestamp = None
    m.min_temp_forecast_6h = temperature
    m.to_dict.return_value = {
        'temperature': temperature, 'humidity': humidity,
        'precipitation_24h': precipitation_24h, 'wind_speed': wind_speed,
    }
    return m


def test_program_writes_weather_log(runner_db):
    """_run_program_threaded → adjusted duration written to weather_log with non-empty weather_data."""
    from irrigation_scheduler import IrrigationScheduler
    sched = IrrigationScheduler(runner_db)

    # Enable weather in the test DB so the real WeatherAdjustment is_enabled() is True.
    conn = sqlite3.connect(runner_db.db_path)
    conn.execute("INSERT OR REPLACE INTO settings(key,value) VALUES('weather.enabled','1')")
    conn.commit()
    conn.close()

    weather = _mock_weather_data()
    with patch('services.weather.adjustment.WeatherAdjustment._get_weather', return_value=weather), \
         patch('services.weather.adjustment.WeatherAdjustment.get_coefficient', return_value=80), \
         patch('services.weather.adjustment.WeatherAdjustment.should_skip', return_value={'skip': False}), \
         patch('services.zone_control.exclusive_start_zone', return_value=True), \
         patch('services.mqtt_pub.publish_mqtt_value', return_value=True):
        sched._run_program_threaded(1, [1], 'TestPhase2')

    # Verify weather_log got a row with non-empty weather_data JSON.
    conn = sqlite3.connect(runner_db.db_path)
    cur = conn.execute(
        'SELECT zone_id, original_duration, adjusted_duration, coefficient, weather_data '
        'FROM weather_log ORDER BY id DESC LIMIT 1'
    )
    row = cur.fetchone()
    conn.close()
    assert row is not None, "weather_log must have at least one entry after program run"
    zone_id, orig, adj, coef, wdata = row
    assert zone_id == 1
    assert orig == 15
    assert coef == 80
    decoded = json.loads(wdata)
    assert decoded.get('temperature') == 22.0
    assert decoded.get('humidity') == 55.0


def test_program_writes_weather_decision(runner_db):
    """_run_program_threaded → log_decision writes one row to weather_decisions."""
    from irrigation_scheduler import IrrigationScheduler
    sched = IrrigationScheduler(runner_db)

    conn = sqlite3.connect(runner_db.db_path)
    conn.execute("INSERT OR REPLACE INTO settings(key,value) VALUES('weather.enabled','1')")
    conn.commit()
    conn.close()

    weather = _mock_weather_data(temperature=24.0, humidity=60.0)
    with patch('services.weather.adjustment.WeatherAdjustment._get_weather', return_value=weather), \
         patch('services.weather.adjustment.WeatherAdjustment.get_coefficient', return_value=110), \
         patch('services.weather.adjustment.WeatherAdjustment.should_skip', return_value={'skip': False}), \
         patch('services.zone_control.exclusive_start_zone', return_value=True), \
         patch('services.mqtt_pub.publish_mqtt_value', return_value=True):
        sched._run_program_threaded(1, [1], 'TestPhase2Decision')

    conn = sqlite3.connect(runner_db.db_path)
    cur = conn.execute(
        'SELECT decision, coefficient, temperature, mode '
        'FROM weather_decisions ORDER BY id DESC LIMIT 1'
    )
    row = cur.fetchone()
    conn.close()
    assert row is not None, "weather_decisions must have at least one entry after program run"
    decision, coef, temp, mode = row
    assert decision == 'adjust'  # coef != 100
    assert coef == 110
    assert temp == 24.0
    assert mode == 'auto'
