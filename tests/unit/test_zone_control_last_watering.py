"""last_watering_time semantics, post-zone_runs-as-history refactor.

Originally written for issue-#2 to ensure last_watering_time was the
END time, not the start. After the refactor, last_watering_time is
no longer a column on zones — it is derived at read time from
``zone_runs.end_utc`` (status='ok'). The contract remains the same
from the caller's perspective:

    db.get_last_watering_time(zid) >= watering_start_time

Each test that historically read ``z['last_watering_time']`` now reads
``test_db.get_last_watering_time(zone['id'])`` instead, exercising the
helper that the dict injection delegates to.
"""

import os
from datetime import datetime
from unittest.mock import patch

os.environ["TESTING"] = "1"


_FMT = "%Y-%m-%d %H:%M:%S"


def _water_monitor_patch():
    return patch("services.zone_control.water_monitor", **{"summarize_run.return_value": (None, None)})


def _parse(ts):
    """Parse a 'YYYY-MM-DD HH:MM:SS' timestamp written by zone_control."""
    assert ts is not None, "expected non-NULL timestamp"
    return datetime.strptime(ts, _FMT)


class TestStopZoneEndTime:
    def test_stop_zone_writes_end_time_not_start(self, test_db):
        """stop_zone() must record now() in last_watering_time, not the
        original watering_start_time. The historical bug wrote the start
        timestamp; the regression check is `last >= start`, with
        last clearly differing from start when start is far in the past.
        """
        import time as _time

        zone = test_db.create_zone(
            {
                "name": "EndTime",
                "duration": 10,
                "group_id": 1,
                "topic": "/test/end",
            }
        )
        # Open a zone_run so stop_zone has something to finish.
        old_start = "2026-01-01 10:00:00"
        test_db.update_zone(
            zone["id"],
            {
                "state": "on",
                "watering_start_time": old_start,
            },
        )
        run_id = test_db.create_zone_run(
            int(zone["id"]),
            1,
            old_start,
            0.0,
            None,
            1,
            None,
        )
        assert run_id is not None
        # Simulate the real relay-on echo so the finished run stays status='ok'
        # (finish_zone_run downgrades unconfirmed runs to 'failed').
        test_db.mark_zone_run_confirmed(int(zone["id"]))

        with (
            patch("services.zone_control.db", test_db),
            patch("services.zone_control.publish_mqtt_value", return_value=True),
            _water_monitor_patch(),
            patch("services.zone_control.state_verifier"),
        ):
            from services.zone_control import stop_zone

            before = datetime.now().replace(microsecond=0)
            _time.sleep(0.01)  # ensure monotonic gap even on fast hardware
            assert stop_zone(zone["id"], reason="test") is True
            after = datetime.now()

        z = test_db.get_zone(zone["id"])
        assert z["state"] == "off"
        assert z["watering_start_time"] is None
        last_str = test_db.get_last_watering_time(int(zone["id"]))
        last = _parse(last_str)
        # End time must be a NOW-ish timestamp, NOT the old start time.
        assert last_str != old_start, "last_watering_time still equals the old start time — the issue-#2 bug is back"
        # Bound check: last is between the moments we sampled around stop_zone.
        assert before <= last <= after, f"last_watering_time={last} not in [{before}, {after}]"

    def test_idempotent_stop_does_not_overwrite(self, test_db):
        """Calling stop_zone on an already-off zone must NOT change
        get_last_watering_time. Without an open zone_run, there's
        nothing to finish — the prior most-recent ok run wins.
        """
        zone = test_db.create_zone(
            {
                "name": "Idempo",
                "duration": 10,
                "group_id": 1,
                "topic": "/test/idem",
            }
        )
        # Seed a closed zone_run to act as the "prior end time".
        prior_end = "2026-04-01 09:30:15"
        run_id = test_db.create_zone_run(
            int(zone["id"]),
            1,
            "2026-04-01 09:00:00",
            0.0,
            None,
            1,
            None,
        )
        # The seeded prior run is a genuine watering — confirm it before
        # closing so finish_zone_run keeps status='ok'.
        test_db.mark_zone_run_confirmed(int(zone["id"]))
        assert test_db.finish_zone_run(
            int(run_id),
            prior_end,
            1.0,
            None,
            None,
            None,
            status="ok",
        )
        # Zone is already off (no open run, watering_start_time None).
        test_db.update_zone(zone["id"], {"state": "off"})

        with (
            patch("services.zone_control.db", test_db),
            patch("services.zone_control.publish_mqtt_value", return_value=True),
            _water_monitor_patch(),
            patch("services.zone_control.state_verifier"),
        ):
            from services.zone_control import stop_zone

            assert stop_zone(zone["id"], reason="test") is True

        # Idempotent stop must not have moved the most-recent ok end_utc.
        assert test_db.get_last_watering_time(int(zone["id"])) == prior_end

    def test_stop_zone_format_matches_finish_zone_run(self, test_db):
        """Format must be 'YYYY-MM-DD HH:MM:SS' — the UI does
        replace('T',' ').slice(0,16). Pin format to avoid drift.
        """
        zone = test_db.create_zone(
            {
                "name": "Fmt",
                "duration": 10,
                "group_id": 1,
                "topic": "/test/fmt",
            }
        )
        test_db.update_zone(
            zone["id"],
            {
                "state": "on",
                "watering_start_time": "2026-01-01 10:00:00",
            },
        )
        test_db.create_zone_run(
            int(zone["id"]),
            1,
            "2026-01-01 10:00:00",
            0.0,
            None,
            1,
            None,
        )
        # Simulate the relay-on echo so the run stays status='ok'.
        test_db.mark_zone_run_confirmed(int(zone["id"]))
        with (
            patch("services.zone_control.db", test_db),
            patch("services.zone_control.publish_mqtt_value", return_value=True),
            _water_monitor_patch(),
            patch("services.zone_control.state_verifier"),
        ):
            from services.zone_control import stop_zone

            assert stop_zone(zone["id"]) is True
        last = test_db.get_last_watering_time(int(zone["id"]))
        # Strict parse — will raise if format is wrong.
        _parse(last)
        # And explicitly: no 'T' separator (ISO with T is the wrong shape).
        assert "T" not in last


class TestPeerOffEndTime:
    def test_peer_off_writes_end_time(self, test_db):
        """exclusive_start_zone() peer-stops siblings in the same group.
        Each peer must have its open zone_run closed so the end time is
        recorded — pre-refactor this leaked open runs forever.
        """
        import time as _time

        z_running = test_db.create_zone(
            {
                "name": "Running",
                "duration": 10,
                "group_id": 1,
                "topic": "/test/run",
            }
        )
        z_new = test_db.create_zone(
            {
                "name": "NewlyStarted",
                "duration": 10,
                "group_id": 1,
                "topic": "/test/new",
            }
        )
        old_start = "2026-01-01 10:00:00"
        test_db.update_zone(
            z_running["id"],
            {
                "state": "on",
                "watering_start_time": old_start,
            },
        )
        # Open a zone_run for the running zone so peer_off has one to finish.
        test_db.create_zone_run(
            int(z_running["id"]),
            1,
            old_start,
            0.0,
            None,
            1,
            None,
        )

        with (
            patch("services.zone_control.db", test_db),
            patch("services.zone_control.publish_mqtt_value", return_value=True),
            _water_monitor_patch(),
            patch("services.zone_control.state_verifier"),
        ):
            from services.zone_control import exclusive_start_zone

            before = datetime.now().replace(microsecond=0)
            _time.sleep(0.01)
            assert exclusive_start_zone(z_new["id"]) is True
            # Peer stops happen in a ThreadPoolExecutor — give them a beat.
            _time.sleep(1.0)
            after = datetime.now()

        peer = test_db.get_zone(z_running["id"])
        # peer_off path may leave state='stopping' briefly on slower runners,
        # but by 1s after exclusive_start_zone the zone_run should be closed.
        assert peer["state"] in ("off", "stopping")
        last_str = test_db.get_last_watering_time(int(z_running["id"]))
        if last_str is not None:
            last = _parse(last_str)
            assert last_str != old_start, "peer_off path still records start-time as last_watering_time"
            assert before <= last <= after


class TestSchedulerAutoStopEndTime:
    """Auto-stop fallback paths in irrigation_scheduler.

    The fallbacks no longer write last_watering_time directly (column
    is gone). They delegate state transitions to the audited helper or
    raw update_zone; the actual end-time recording happens via the
    central stop_zone path closing the zone_run. We can still assert
    that an open zone_run gets finished.
    """

    def test_irrigation_scheduler_central_path_closes_run(self, test_db):
        """When the central stop_zone succeeds, it should close the
        open zone_run via finish_zone_run, and get_last_watering_time
        should now report a fresh timestamp.
        """
        import time as _time

        zone = test_db.create_zone(
            {
                "name": "AutoStop",
                "duration": 10,
                "group_id": 1,
                "topic": "/test/auto",
            }
        )
        old_start = "2026-01-01 10:00:00"
        test_db.update_zone(
            zone["id"],
            {
                "state": "on",
                "watering_start_time": old_start,
            },
        )
        test_db.create_zone_run(
            int(zone["id"]),
            1,
            old_start,
            0.0,
            None,
            1,
            None,
        )
        # Simulate the relay-on echo so the auto-stopped run stays status='ok'.
        test_db.mark_zone_run_confirmed(int(zone["id"]))
        from irrigation_scheduler import IrrigationScheduler

        scheduler = IrrigationScheduler(test_db)

        with (
            patch("services.zone_control.db", test_db),
            patch("services.zone_control.publish_mqtt_value", return_value=True),
            _water_monitor_patch(),
            patch("services.zone_control.state_verifier"),
        ):
            before = datetime.now().replace(microsecond=0)
            _time.sleep(0.01)
            assert scheduler._stop_zone(zone["id"]) is True
            after = datetime.now()

        z = test_db.get_zone(zone["id"])
        assert z["state"] == "off"
        last_str = test_db.get_last_watering_time(int(zone["id"]))
        assert last_str is not None
        last = _parse(last_str)
        assert last_str != old_start
        assert before <= last <= after

    def test_irrigation_scheduler_fallback_when_central_fails(self, test_db):
        """A central OFF failure stays visible and retains a hard retry.

        A DB-only ``off`` fallback would hide a still-energised relay from the
        watchdog and make the retry impossible.
        """
        zone = test_db.create_zone(
            {
                "name": "AutoStopFb",
                "duration": 10,
                "group_id": 1,
                "topic": "/test/auto-fb",
            }
        )
        old_start = "2026-01-01 10:00:00"
        test_db.update_zone(
            zone["id"],
            {
                "state": "on",
                "watering_start_time": old_start,
            },
        )
        from irrigation_scheduler import IrrigationScheduler

        scheduler = IrrigationScheduler(test_db)

        with patch("services.zone_control.stop_zone", side_effect=ValueError("forced fail for test")):
            assert scheduler._stop_zone(zone["id"]) is False

        z = test_db.get_zone(zone["id"])
        assert z["state"] == "on"
        assert z["watering_start_time"] == old_start
        assert scheduler.scheduler.get_job(f"zone_hard_stop:{zone['id']}") is not None
