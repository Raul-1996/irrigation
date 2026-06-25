"""History truth: a run is recorded 'ok' only when the relay-on was
physically confirmed (MQTT echo, simulated via mark_zone_run_confirmed).
An unconfirmed run is downgraded to 'failed' by finish_zone_run so the
history never claims a watering that didn't physically happen.
"""

import os
import sqlite3
import time

os.environ["TESTING"] = "1"


class TestZoneRunConfirmed:
    def _zone(self, test_db):
        return test_db.create_zone({"name": "Z", "duration": 10, "group_id": 1, "topic": "/t/z"})["id"]

    def _open_run(self, test_db, zid):
        return test_db.create_zone_run(zid, 1, "2026-01-01 10:00:00", time.monotonic(), None, 1)

    def _row(self, test_db, run_id):
        with sqlite3.connect(test_db.db_path) as conn:
            return conn.execute("SELECT status, confirmed FROM zone_runs WHERE id = ?", (run_id,)).fetchone()

    def test_unconfirmed_run_downgraded_to_failed(self, test_db):
        zid = self._zone(test_db)
        run_id = self._open_run(test_db, zid)
        assert run_id is not None
        # No relay-on echo was observed → confirmed stays 0.
        assert test_db.finish_zone_run(run_id, "2026-01-01 10:10:00", time.monotonic(), None, None, None, "ok") is True
        status, confirmed = self._row(test_db, run_id)
        assert status == "failed"
        assert confirmed == 0

    def test_confirmed_run_stays_ok(self, test_db):
        zid = self._zone(test_db)
        run_id = self._open_run(test_db, zid)
        # Relay physically confirmed 'on' during the run.
        assert test_db.mark_zone_run_confirmed(zid) is True
        assert test_db.finish_zone_run(run_id, "2026-01-01 10:10:00", time.monotonic(), None, None, None, "ok") is True
        status, confirmed = self._row(test_db, run_id)
        assert status == "ok"
        assert confirmed == 1
        # And it counts as the last successful watering.
        assert test_db.get_last_watering_time(zid) == "2026-01-01 10:10:00"

    def test_explicit_non_ok_status_preserved(self, test_db):
        zid = self._zone(test_db)
        run_id = self._open_run(test_db, zid)
        # Caller explicitly aborts — not 'ok', so the confirmation rule does
        # not apply; status stays 'aborted'.
        test_db.finish_zone_run(run_id, "2026-01-01 10:10:00", time.monotonic(), None, None, None, "aborted")
        status, _ = self._row(test_db, run_id)
        assert status == "aborted"

    def test_mark_confirmed_only_touches_open_run(self, test_db):
        zid = self._zone(test_db)
        run_id = self._open_run(test_db, zid)
        # Close the run first, then mark — there is no open run, so confirmed
        # must stay 0 (no retroactive confirmation).
        test_db.finish_zone_run(run_id, "2026-01-01 10:10:00", time.monotonic(), None, None, None, "aborted")
        test_db.mark_zone_run_confirmed(zid)
        _, confirmed = self._row(test_db, run_id)
        assert confirmed == 0

    def test_unconfirmed_run_not_counted_as_last_watering(self, test_db):
        zid = self._zone(test_db)
        run_id = self._open_run(test_db, zid)
        test_db.finish_zone_run(run_id, "2026-01-01 10:10:00", time.monotonic(), None, None, None, "ok")
        # Downgraded to 'failed' → get_last_watering_time (status='ok' only) ignores it.
        assert test_db.get_last_watering_time(zid) is None


def test_failed_run_excluded_from_actual_totals():
    """A 'failed' run stays in the list but must not inflate summary
    minutes/run-count aggregates."""
    from datetime import date

    from services.history_calc import calculate_actual_for_zone

    d = date(2026, 1, 1)
    runs = [
        {"start_utc": "2026-01-01 10:00:00", "end_utc": "2026-01-01 10:10:00", "status": "ok"},
        {"start_utc": "2026-01-01 11:00:00", "end_utc": "2026-01-01 11:10:00", "status": "failed"},
    ]
    minutes, counts = calculate_actual_for_zone(runs, [d])
    assert counts[d] == 1  # the 'failed' run is excluded from the count
    assert minutes[d] >= 9  # only the 'ok' run (~10 min) contributes
