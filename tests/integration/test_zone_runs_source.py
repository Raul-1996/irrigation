"""Issue #35: zone_runs.source is populated at INSERT time.

- Scheduler's _run_group_sequence ⇒ source='program'
- exclusive_start_zone (UI/API /api/zones/<id>/start) ⇒ source='manual'
- create_zone_run without ``source=`` keyword ⇒ source IS NULL
"""

import os
import sqlite3
import threading
from unittest.mock import patch

import pytest

from tests.safety_contracts import confirmed_group_stop

os.environ["TESTING"] = "1"


@pytest.fixture
def runner_db(tmp_path):
    db_path = str(tmp_path / "src.db")
    from database import IrrigationDB

    db = IrrigationDB(db_path=db_path)
    conn = sqlite3.connect(db_path)
    conn.execute("INSERT OR IGNORE INTO groups (id, name) VALUES (1, 'G1')")
    conn.execute("INSERT OR IGNORE INTO mqtt_servers (id, name, host, port) VALUES (1, 'Local', '127.0.0.1', 1883)")
    conn.execute(
        "INSERT OR IGNORE INTO zones (id, name, duration, group_id, topic, mqtt_server_id) "
        "VALUES (1, 'Газон', 1, 1, '/test/zone1', 1)"
    )
    conn.commit()
    conn.close()
    return db


def test_group_sequence_writes_source_program(runner_db):
    """Scheduler-driven runs must record source='program'."""
    from irrigation_scheduler import IrrigationScheduler

    sched = IrrigationScheduler(runner_db)
    sched.group_cancel_events[1] = threading.Event()

    prior = os.environ.get("SKIP_TESTING_SHORT_CIRCUIT_FOR_GROUP_SEQ")
    os.environ["SKIP_TESTING_SHORT_CIRCUIT_FOR_GROUP_SEQ"] = "1"
    try:
        with (
            patch("services.mqtt_pub.publish_mqtt_value", return_value=True),
            patch("services.zone_control.publish_mqtt_value", return_value=True),
            patch("services.zone_control.db", runner_db),
            patch("services.zone_control.state_verifier"),
        ):
            t = threading.Thread(
                target=sched._run_group_sequence,
                args=(1, [1], 1),
                daemon=True,
            )
            t.start()
            t.join(timeout=15)
            assert not t.is_alive()
    finally:
        if prior is None:
            os.environ.pop("SKIP_TESTING_SHORT_CIRCUIT_FOR_GROUP_SEQ", None)
        else:
            os.environ["SKIP_TESTING_SHORT_CIRCUIT_FOR_GROUP_SEQ"] = prior

    with sqlite3.connect(runner_db.db_path) as conn:
        rows = conn.execute("SELECT source FROM zone_runs WHERE zone_id = 1").fetchall()
    assert rows, "no zone_run row written"
    assert rows[0][0] == "program", f"expected source='program', got {rows[0][0]!r}"


def test_manual_start_writes_source_manual(admin_client, app):
    """POST /api/zones/<id>/start (UI path) ⇒ source='manual'."""
    srv = app.db.create_mqtt_server(
        {
            "name": "S1",
            "host": "127.0.0.1",
            "port": 1883,
            "enabled": 1,
        }
    )
    zone = app.db.create_zone(
        {
            "name": "Z1",
            "duration": 10,
            "group_id": 1,
            "topic": "/devices/t/K1",
            "mqtt_server_id": srv["id"],
        }
    )
    with (
        confirmed_group_stop(app.db),
        patch("services.zone_control.publish_mqtt_value", return_value=True),
        patch("services.zone_control.water_monitor"),
        patch("services.zone_control.state_verifier"),
    ):
        resp = admin_client.post(f"/api/zones/{zone['id']}/mqtt/start")
    assert resp.status_code == 200, resp.get_data(as_text=True)

    with sqlite3.connect(app.db.db_path) as conn:
        rows = conn.execute("SELECT source FROM zone_runs WHERE zone_id = ?", (zone["id"],)).fetchall()
    assert rows, "no zone_run row after /mqtt/start"
    assert rows[0][0] == "manual", f"expected source='manual', got {rows[0][0]!r}"


def test_exclusive_start_zone_with_source_program(runner_db):
    """exclusive_start_zone(zone_id, source='program') ⇒ zone_runs.source='program'.

    Regression guard: scheduler paths (irrigation_scheduler._run_program_threaded
    line 962) pass source='program' so program-triggered runs aren't mislabeled
    as 'manual' in history UI.
    """
    from services.zone_control import exclusive_start_zone

    with (
        patch("services.mqtt_pub.publish_mqtt_value", return_value=True),
        patch("services.zone_control.publish_mqtt_value", return_value=True),
        patch("services.zone_control.db", runner_db),
        patch("services.zone_control.state_verifier"),
    ):
        ok = exclusive_start_zone(1, source="program")
    assert ok, "exclusive_start_zone returned False"

    with sqlite3.connect(runner_db.db_path) as conn:
        rows = conn.execute("SELECT source FROM zone_runs WHERE zone_id = 1").fetchall()
    assert rows, "no zone_run row written"
    assert rows[0][0] == "program", f"expected source='program', got {rows[0][0]!r}"


def test_exclusive_start_zone_default_source_manual(runner_db):
    """exclusive_start_zone(zone_id) without source kwarg ⇒ defaults to 'manual'.

    Back-compat: existing manual API callers don't pass source explicitly.
    """
    from services.zone_control import exclusive_start_zone

    with (
        patch("services.mqtt_pub.publish_mqtt_value", return_value=True),
        patch("services.zone_control.publish_mqtt_value", return_value=True),
        patch("services.zone_control.db", runner_db),
        patch("services.zone_control.state_verifier"),
    ):
        ok = exclusive_start_zone(1)
    assert ok, "exclusive_start_zone returned False"

    with sqlite3.connect(runner_db.db_path) as conn:
        rows = conn.execute("SELECT source FROM zone_runs WHERE zone_id = 1").fetchall()
    assert rows, "no zone_run row written"
    assert rows[0][0] == "manual", f"expected source='manual', got {rows[0][0]!r}"


def test_create_zone_run_without_source_keeps_null(test_db):
    """create_zone_run() without source= keyword ⇒ stays NULL (back-compat)."""
    test_db.create_zone(
        {
            "name": "Z",
            "duration": 5,
            "group_id": 1,
            "topic": "/d/x",
        }
    )
    rid = test_db.create_zone_run(1, 1, "2026-01-01T00:00:00Z", 0.0, None, 1)
    assert rid is not None
    with sqlite3.connect(test_db.db_path) as conn:
        row = conn.execute("SELECT source FROM zone_runs WHERE id = ?", (rid,)).fetchone()
    assert row is not None
    assert row[0] is None
