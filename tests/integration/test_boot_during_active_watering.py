"""Integration test: boot reconcile of stale open zone_runs.

Scenario: the controller crashed/was killed while a zone was watering.
Its zone_runs row is left with end_utc=NULL and status=NULL. On the
next boot, ``services.app_init._boot_sync`` must mark it status='aborted'
so it doesn't shadow real ok history (and so subsequent get_open_zone_run
calls don't try to finish a pre-reboot run).
"""

import os
import sqlite3

os.environ["TESTING"] = "1"


class TestBootSyncAbortsStaleRuns:
    def test_open_run_marked_aborted_on_boot(self, test_db):
        """A row with end_utc IS NULL and status NULL must become
        status='aborted' after _boot_sync runs.
        """
        zone = test_db.create_zone(
            {
                "name": "Stale",
                "duration": 10,
                "group_id": 1,
            }
        )
        # Seed a closed ok run so we can prove get_last_watering_time
        # returns the prior-good value, not the stale-aborted one.
        prior = test_db.create_zone_run(
            int(zone["id"]),
            1,
            "2026-04-01 09:00:00",
            0.0,
            None,
            1,
            None,
        )
        assert test_db.finish_zone_run(
            int(prior),
            "2026-04-01 09:15:00",
            1.0,
            None,
            None,
            None,
            status="ok",
        )
        # Seed the stale open run (simulating a crash mid-watering).
        stale_id = test_db.create_zone_run(
            int(zone["id"]),
            1,
            "2026-04-02 10:00:00",
            0.0,
            None,
            1,
            None,
        )
        assert stale_id is not None

        # Mock just enough of the app/db surface that _boot_sync uses.
        from unittest.mock import MagicMock
        from unittest.mock import patch as _patch

        import services.mqtt_pub as _mp

        # Stub out the side-effect-y bits we don't want to actually run:
        # zone_control.stop_all_in_group, MQTT publishes, master valve
        # closes. Patch on the module that _boot_sync imports from.
        import services.zone_control as _zc
        from services import app_init

        with (
            _patch.object(_zc, "stop_all_in_group", return_value=None),
            _patch.object(_mp, "publish_mqtt_value", return_value=True),
        ):
            app_init._boot_sync(MagicMock(), test_db)

        # The stale open run must now be aborted.
        with sqlite3.connect(test_db.db_path) as conn:
            row = conn.execute(
                "SELECT status, end_utc FROM zone_runs WHERE id = ?",
                (int(stale_id),),
            ).fetchone()
        assert row is not None
        assert row[0] == "aborted", f"stale open run should be aborted, got status={row[0]!r}"
        # end_utc was NULL when boot_sync ran, the helper update doesn't
        # set it — that's fine, get_last_watering_time filters
        # status='ok' AND end_utc IS NOT NULL anyway.

        # The prior good run is still the one reported by the helper.
        assert test_db.get_last_watering_time(int(zone["id"])) == "2026-04-01 09:15:00"

    def test_already_finished_run_left_alone(self, test_db):
        """A row that already has status='ok' and a real end_utc must
        not be touched by the boot-time abort sweep.
        """
        zone = test_db.create_zone(
            {
                "name": "Closed",
                "duration": 10,
                "group_id": 1,
            }
        )
        ok_id = test_db.create_zone_run(
            int(zone["id"]),
            1,
            "2026-04-01 09:00:00",
            0.0,
            None,
            1,
            None,
        )
        assert test_db.finish_zone_run(
            int(ok_id),
            "2026-04-01 09:15:00",
            1.0,
            None,
            None,
            None,
            status="ok",
        )

        from unittest.mock import MagicMock
        from unittest.mock import patch as _patch

        import services.mqtt_pub as _mp
        import services.zone_control as _zc
        from services import app_init

        with (
            _patch.object(_zc, "stop_all_in_group", return_value=None),
            _patch.object(_mp, "publish_mqtt_value", return_value=True),
        ):
            app_init._boot_sync(MagicMock(), test_db)

        with sqlite3.connect(test_db.db_path) as conn:
            row = conn.execute(
                "SELECT status, end_utc FROM zone_runs WHERE id = ?",
                (int(ok_id),),
            ).fetchone()
        assert row[0] == "ok"
        assert row[1] == "2026-04-01 09:15:00"
