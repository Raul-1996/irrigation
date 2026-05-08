"""Unit tests for ``ZoneRepository.get_last_watering_time``.

The helper is the canonical replacement for the dropped
``zones.last_watering_time`` column. It computes
``MAX(end_utc) WHERE zone_id=? AND status='ok' AND end_utc IS NOT NULL``.
"""
import os

import pytest

os.environ['TESTING'] = '1'


class TestGetLastWateringTime:
    def test_zone_with_no_runs_returns_none(self, test_db):
        """Brand-new zone, no zone_runs at all → None."""
        zone = test_db.create_zone({
            'name': 'Empty', 'duration': 10, 'group_id': 1,
        })
        assert test_db.get_last_watering_time(int(zone['id'])) is None

    def test_only_open_run_returns_none(self, test_db):
        """A zone_run that has been opened but never finished must NOT
        be reported as the last watering time — end_utc is NULL.
        """
        zone = test_db.create_zone({
            'name': 'Open', 'duration': 10, 'group_id': 1,
        })
        test_db.create_zone_run(
            int(zone['id']), 1, '2026-04-01 10:00:00', 0.0, None, 1, None,
        )
        assert test_db.get_last_watering_time(int(zone['id'])) is None

    def test_aborted_run_excluded(self, test_db):
        """A run finished with status='aborted' must NOT appear as the
        last watering time. Only status='ok' counts.
        """
        zone = test_db.create_zone({
            'name': 'Aborted', 'duration': 10, 'group_id': 1,
        })
        run = test_db.create_zone_run(
            int(zone['id']), 1, '2026-04-01 10:00:00', 0.0, None, 1, None,
        )
        # Manually mark the run as aborted via raw SQL — the helper has
        # no public API for that, but boot_sync writes status='aborted'
        # and we want a stable check that the filter excludes it.
        import sqlite3
        with sqlite3.connect(test_db.db_path) as conn:
            conn.execute(
                "UPDATE zone_runs SET end_utc = ?, status = 'aborted' "
                "WHERE id = ?",
                ('2026-04-01 10:15:00', int(run)),
            )
            conn.commit()
        assert test_db.get_last_watering_time(int(zone['id'])) is None

    def test_returns_most_recent_ok_run(self, test_db):
        """With multiple ok runs, the helper must return the MAX(end_utc)."""
        zone = test_db.create_zone({
            'name': 'Multi', 'duration': 10, 'group_id': 1,
        })
        r1 = test_db.create_zone_run(
            int(zone['id']), 1, '2026-04-01 09:00:00', 0.0, None, 1, None,
        )
        assert test_db.finish_zone_run(
            int(r1), '2026-04-01 09:15:00', 1.0, None, None, None, status='ok',
        )
        r2 = test_db.create_zone_run(
            int(zone['id']), 1, '2026-04-02 09:00:00', 0.0, None, 1, None,
        )
        assert test_db.finish_zone_run(
            int(r2), '2026-04-02 09:30:00', 1.0, None, None, None, status='ok',
        )
        assert test_db.get_last_watering_time(int(zone['id'])) == \
               '2026-04-02 09:30:00'

    def test_non_meter_zone_returns_end_utc(self, test_db):
        """A zone whose group has use_water_meter=0 still records ok runs
        (NULL meter cols). The helper must still return its end_utc.
        """
        zone = test_db.create_zone({
            'name': 'NoMeter', 'duration': 10, 'group_id': 1,
        })
        # Open + finish a run with no meter columns.
        run_id = test_db.create_zone_run(
            int(zone['id']), 1, '2026-04-03 10:00:00', 0.0, None, 1, None,
        )
        assert test_db.finish_zone_run(
            int(run_id), '2026-04-03 10:20:00', 1.0, None, None, None,
            status='ok',
        )
        assert test_db.get_last_watering_time(int(zone['id'])) == \
               '2026-04-03 10:20:00'

    def test_per_zone_isolation(self, test_db):
        """The helper must scope by zone_id — runs on other zones don't
        leak into the answer.
        """
        z1 = test_db.create_zone({'name': 'A', 'duration': 10, 'group_id': 1})
        z2 = test_db.create_zone({'name': 'B', 'duration': 10, 'group_id': 1})
        r1 = test_db.create_zone_run(
            int(z1['id']), 1, '2026-04-01 09:00:00', 0.0, None, 1, None,
        )
        test_db.finish_zone_run(
            int(r1), '2026-04-01 09:15:00', 1.0, None, None, None, status='ok',
        )
        # z2 has no runs — must report None even though z1 has one.
        assert test_db.get_last_watering_time(int(z2['id'])) is None
        assert test_db.get_last_watering_time(int(z1['id'])) == \
               '2026-04-01 09:15:00'
