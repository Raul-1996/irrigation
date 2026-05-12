"""Pin: zone_runs are written even for groups without a water meter.

Pre-refactor, ``exclusive_start_zone`` only called ``create_zone_run``
when the group had ``use_water_meter=1``. After the refactor, every
started zone (in any non-999 group) opens a zone_runs row, so
``get_last_watering_time`` works for non-meter groups too.
"""

import os
import sqlite3
import time as _time
from unittest.mock import MagicMock, patch

os.environ["TESTING"] = "1"


def _has_meter_flag(test_db, group_id):
    """Sanity helper to assert what the seeded group looks like."""
    g = next((g for g in (test_db.get_groups() or []) if int(g.get("id")) == int(group_id)), None)
    assert g is not None
    return int(g.get("use_water_meter") or 0)


def _make_no_meter_water_monitor():
    """A water_monitor stub that behaves like a non-meter group: every
    pulse query returns None, summarize_run returns (None, None).
    """
    wm = MagicMock()
    wm.get_pulses_at_or_before.return_value = None
    wm.get_pulses_at_or_after.return_value = None
    wm.summarize_run.return_value = (None, None)
    return wm


class TestZoneRunsWrittenForNonMeterGroups:
    def test_start_then_stop_writes_one_ok_run(self, test_db):
        """Group 1 ships with use_water_meter=0 by default. Start a zone
        in it, stop it, and verify exactly one zone_run row exists with
        status='ok' and NULL meter columns.
        """
        # Sanity: default group 1 has no meter.
        assert _has_meter_flag(test_db, 1) == 0

        zone = test_db.create_zone(
            {
                "name": "NoMeterZone",
                "duration": 10,
                "group_id": 1,
                "topic": "/test/no-meter",
            }
        )

        with (
            patch("services.zone_control.db", test_db),
            patch("services.zone_control.publish_mqtt_value", return_value=True),
            patch("services.zone_control.water_monitor", _make_no_meter_water_monitor()),
            patch("services.zone_control.state_verifier"),
        ):
            from services.zone_control import exclusive_start_zone, stop_zone

            assert exclusive_start_zone(zone["id"]) is True
            _time.sleep(0.05)  # give monotonic clock a tick
            assert stop_zone(zone["id"], reason="test") is True

        with sqlite3.connect(test_db.db_path) as conn:
            rows = conn.execute(
                "SELECT id, status, end_utc, "
                "       start_raw_pulses, end_raw_pulses, total_liters, "
                "       avg_flow_lpm, base_m3_at_start "
                "FROM zone_runs WHERE zone_id = ?",
                (int(zone["id"]),),
            ).fetchall()
        assert len(rows) == 1, f"expected exactly one zone_run for non-meter zone, got {len(rows)}"
        run = rows[0]
        # status='ok' and end_utc populated by stop_zone -> finish_zone_run.
        assert run[1] == "ok"
        assert run[2] is not None
        # All meter-derived columns must be NULL — no pulses, no liters,
        # no avg flow, no base_m3 (group has no meter).
        assert run[3] is None, f"start_raw_pulses should be NULL, got {run[3]!r}"
        assert run[4] is None, f"end_raw_pulses should be NULL, got {run[4]!r}"
        assert run[5] is None, f"total_liters should be NULL, got {run[5]!r}"
        assert run[6] is None, f"avg_flow_lpm should be NULL, got {run[6]!r}"
        assert run[7] is None, f"base_m3_at_start should be NULL, got {run[7]!r}"

        # And get_last_watering_time picks up the end_utc.
        assert test_db.get_last_watering_time(int(zone["id"])) == run[2]
