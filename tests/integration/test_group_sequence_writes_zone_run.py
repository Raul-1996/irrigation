"""Issue #32: _run_group_sequence must write zone_runs.

Pre-fix, IrrigationScheduler._run_group_sequence bypassed
services.zone_control.exclusive_start_zone and published MQTT directly,
which meant ``db.create_zone_run`` was never called. UI "Последний полив"
(derived from MAX(zone_runs.end_utc)) therefore stayed empty after manual
group runs and scheduled group_seq cron runs.

This test runs the real per-zone loop (SKIP_TESTING_SHORT_CIRCUIT_FOR_GROUP_SEQ=1),
lets it open AND close the run, then asserts a zone_runs row exists.
"""
import os
import sqlite3
import threading
import time

import pytest

os.environ['TESTING'] = '1'


@pytest.fixture
def runner_db(tmp_path):
    db_path = str(tmp_path / "gseq.db")
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
    return db


def test_group_sequence_opens_and_closes_zone_run(runner_db):
    """_run_group_sequence([1]) writes exactly one zone_run with status='ok'."""
    from unittest.mock import patch
    from irrigation_scheduler import IrrigationScheduler

    sched = IrrigationScheduler(runner_db)
    sched.group_cancel_events[1] = threading.Event()

    prior = os.environ.get('SKIP_TESTING_SHORT_CIRCUIT_FOR_GROUP_SEQ')
    os.environ['SKIP_TESTING_SHORT_CIRCUIT_FOR_GROUP_SEQ'] = '1'

    try:
        with patch('services.mqtt_pub.publish_mqtt_value', return_value=True), \
             patch('services.zone_control.publish_mqtt_value', return_value=True), \
             patch('services.zone_control.db', runner_db), \
             patch('services.zone_control.state_verifier'):
            t = threading.Thread(
                target=sched._run_group_sequence,
                args=(1, [1], 1),  # group_id, zone_ids, override_duration=1min
                daemon=True,
            )
            t.start()
            t.join(timeout=15)
            assert not t.is_alive(), "sequence thread should finish within 15s"
    finally:
        if prior is None:
            os.environ.pop('SKIP_TESTING_SHORT_CIRCUIT_FOR_GROUP_SEQ', None)
        else:
            os.environ['SKIP_TESTING_SHORT_CIRCUIT_FOR_GROUP_SEQ'] = prior

    with sqlite3.connect(runner_db.db_path) as conn:
        rows = conn.execute(
            'SELECT zone_id, group_id, status, end_utc FROM zone_runs WHERE zone_id = 1'
        ).fetchall()
    assert len(rows) == 1, (
        f"expected exactly one zone_run row written by _run_group_sequence, got {len(rows)}"
    )
    zone_id, group_id, status, end_utc = rows[0]
    assert zone_id == 1
    assert group_id == 1
    assert status == 'ok', f"expected status='ok' after sequence end, got {status!r}"
    assert end_utc is not None, "end_utc must be set after sequence closes the run"
