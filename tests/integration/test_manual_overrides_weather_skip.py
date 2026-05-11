"""Issue #31: manual runs must override weather skip.

When the weather adjustment service flags a skip (e.g. heavy rain, low
temperature), scheduled programs/group_seq should respect it — but a
manual user-initiated run must NOT be blocked. The user looked at the
weather, judged conditions OK, and pressed Run.

This pins behaviour on both code paths:
  - `_run_program_threaded(..., manual=True)` (manual program run)
  - `_run_group_sequence(..., manual=True)` (manual group/zone selection)
"""
import os
import sqlite3
import threading

import pytest

os.environ['TESTING'] = '1'


@pytest.fixture
def runner_db(tmp_path):
    db_path = str(tmp_path / "manual_override.db")
    from database import IrrigationDB
    db = IrrigationDB(db_path=db_path)
    conn = sqlite3.connect(db_path)
    conn.execute("INSERT OR IGNORE INTO groups (id, name) VALUES (1, 'G1')")
    conn.execute(
        "INSERT OR IGNORE INTO mqtt_servers (id, name, host, port) "
        "VALUES (1, 'Local', '127.0.0.1', 1883)"
    )
    conn.execute(
        "INSERT OR IGNORE INTO zones (id, name, duration, group_id, topic, mqtt_server_id) "
        "VALUES (1, 'Газон', 1, 1, '/test/zone1', 1)"
    )
    conn.commit()
    conn.close()
    # Reset singleton so adjustment binds to THIS db_path
    import services.weather.singletons as _wsing
    _wsing._adjustment = None
    _wsing._weather_service = None
    return db


def test_manual_program_run_ignores_weather_skip(runner_db):
    """_run_program_threaded(manual=True) must NOT return early on weather skip."""
    from unittest.mock import patch
    from irrigation_scheduler import IrrigationScheduler

    sched = IrrigationScheduler(runner_db)

    with patch.object(sched, '_check_weather_skip',
                      return_value={'skip': True, 'reason': 'heavy_rain'}) as mock_skip, \
         patch('services.zone_control.exclusive_start_zone', return_value=True) as mock_start, \
         patch('services.zone_control.publish_mqtt_value', return_value=True), \
         patch('services.zone_control.db', runner_db), \
         patch('services.mqtt_pub.publish_mqtt_value', return_value=True), \
         patch('services.zone_control.state_verifier'):
        sched._run_program_threaded(1, [1], 'TestProgram', manual=True)

    assert mock_start.call_count >= 1, (
        "manual=True must bypass weather skip and reach exclusive_start_zone"
    )


def test_scheduled_program_run_still_respects_weather_skip(runner_db):
    """_run_program_threaded(manual=False) must skip on weather (regression guard)."""
    from unittest.mock import patch
    from irrigation_scheduler import IrrigationScheduler

    sched = IrrigationScheduler(runner_db)

    with patch.object(sched, '_check_weather_skip',
                      return_value={'skip': True, 'reason': 'heavy_rain'}), \
         patch('services.zone_control.exclusive_start_zone', return_value=True) as mock_start, \
         patch('services.zone_control.publish_mqtt_value', return_value=True), \
         patch('services.zone_control.db', runner_db), \
         patch('services.mqtt_pub.publish_mqtt_value', return_value=True), \
         patch('services.zone_control.state_verifier'):
        sched._run_program_threaded(1, [1], 'TestProgram')  # manual=False default

    assert mock_start.call_count == 0, (
        "scheduled run (manual=False) must respect weather skip and NOT start zones"
    )


def test_manual_group_sequence_ignores_weather_skip(runner_db):
    """_run_group_sequence(manual=True) must NOT abort on weather skip."""
    from unittest.mock import patch
    from irrigation_scheduler import IrrigationScheduler

    sched = IrrigationScheduler(runner_db)
    sched.group_cancel_events[1] = threading.Event()

    prior = os.environ.get('SKIP_TESTING_SHORT_CIRCUIT_FOR_GROUP_SEQ')
    os.environ['SKIP_TESTING_SHORT_CIRCUIT_FOR_GROUP_SEQ'] = '1'

    try:
        with patch.object(sched, '_check_weather_skip',
                          return_value={'skip': True, 'reason': 'heavy_rain'}), \
             patch('services.mqtt_pub.publish_mqtt_value', return_value=True), \
             patch('services.zone_control.publish_mqtt_value', return_value=True), \
             patch('services.zone_control.db', runner_db), \
             patch('services.zone_control.state_verifier'):
            t = threading.Thread(
                target=sched._run_group_sequence,
                args=(1, [1], 1),  # group_id, zone_ids, override_duration
                kwargs={'manual': True},
                daemon=True,
            )
            t.start()
            t.join(timeout=15)
            assert not t.is_alive()
    finally:
        if prior is None:
            os.environ.pop('SKIP_TESTING_SHORT_CIRCUIT_FOR_GROUP_SEQ', None)
        else:
            os.environ['SKIP_TESTING_SHORT_CIRCUIT_FOR_GROUP_SEQ'] = prior

    # If manual=True bypassed the skip, the run reached create_zone_run.
    with sqlite3.connect(runner_db.db_path) as conn:
        rows = conn.execute(
            'SELECT zone_id FROM zone_runs WHERE zone_id = 1'
        ).fetchall()
    assert len(rows) >= 1, (
        "manual=True group_sequence must bypass weather skip and open a zone_run"
    )
