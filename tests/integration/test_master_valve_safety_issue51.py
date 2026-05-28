"""Issue #51 — master valve safety net (A6/A7/A8/A9).

Audit reference: ``audits/2026-05-28-security/findings.md``.

A6 — ``_PENDING_CLOSE_TIMERS`` cleanup must be identity-guarded so a
fired timer never wipes a freshly scheduled foreign timer for the same
topic.

A7 — Watchdog ``run()`` startup must reconcile zones left in
``state='on'/'starting'`` past their duration window (SIGKILL/OOM
artefacts) BEFORE the first supervisor tick — otherwise the supervisor
thinks the run is still in progress and refuses to close the master.

A8 — ``_run_program_threaded`` / ``_run_group_sequence`` ``finally``
must schedule a master close for every group the run touched, so a raise
between ``exclusive_start_zone`` (which opens the master) and the
matching ``stop_zone`` cannot leak an open valve.

A9 — Watchdog must publish via a bounded path (pre-check
``is_connected()`` and worker-thread join timeout) so a dead broker
cannot stall the daemon thread, and ``run()`` must broaden the exception
class to ``Exception`` with ``logger.exception`` so an unexpected
``KeyError``/``TypeError`` cannot kill the supervisor.
"""

import os
import threading
import time
from unittest.mock import MagicMock, patch

os.environ["TESTING"] = "1"


# --- Shared helpers ---------------------------------------------------------


def _make_master_group(test_db, *, observed: str = "open", topic: str = "/devices/mv/controls/K1"):
    """Group + MQTT server with master valve enabled, returns (gid, srv_id, group_dict)."""
    srv = test_db.create_mqtt_server({"name": "S", "host": "127.0.0.1", "port": 1883})
    test_db.create_group("MV Group")
    groups = test_db.get_groups()
    gid = int(groups[-1]["id"])
    test_db.update_group_fields(
        gid,
        {
            "use_master_valve": 1,
            "master_mqtt_topic": topic,
            "master_mqtt_server_id": int(srv["id"]),
            "master_mode": "NC",
            "master_close_delay_sec": 1,
            "master_valve_observed": observed,
        },
    )
    group_dict = next(g for g in test_db.get_groups() if int(g["id"]) == gid)
    return gid, int(srv["id"]), group_dict


# --- A6 tests ---------------------------------------------------------------


class TestA6PendingCloseCleanup:
    """A6: cleanup happens, and ONLY for the timer we ourselves scheduled."""

    def test_dict_entry_is_popped_after_close_fires(self, test_db):
        """Happy path: _do_close runs to completion → entry gone from dict."""
        import services.zone_control as zc

        _gid, _srv_id, group_dict = _make_master_group(test_db)

        with (
            patch.object(zc, "db", test_db),
            patch.object(zc, "publish_mqtt_value", return_value=True),
            patch.object(zc, "TESTING", False),
        ):
            zc._schedule_master_close(group_dict, immediate=True)
            # delay=0 → daemon thread runs almost immediately
            time.sleep(0.5)

        from utils import normalize_topic

        t_norm = normalize_topic(group_dict["master_mqtt_topic"])
        with zc._PENDING_CLOSE_LOCK:
            assert t_norm not in zc._PENDING_CLOSE_TIMERS, (
                f"After _do_close completes, _PENDING_CLOSE_TIMERS must not "
                f"retain entry for topic={t_norm}; got {dict(zc._PENDING_CLOSE_TIMERS)!r}"
            )

    def test_dict_entry_popped_even_when_publish_raises(self, test_db):
        """Cleanup must run in ``finally`` — exception in publish does not leak the dict entry."""
        import services.zone_control as zc

        _gid, _srv_id, group_dict = _make_master_group(test_db)

        def _boom(*_args, **_kwargs):
            raise RuntimeError("simulated broker failure")

        with (
            patch.object(zc, "db", test_db),
            patch.object(zc, "publish_mqtt_value", side_effect=_boom),
            patch.object(zc, "TESTING", False),
        ):
            zc._schedule_master_close(group_dict, immediate=True)
            time.sleep(0.5)

        from utils import normalize_topic

        t_norm = normalize_topic(group_dict["master_mqtt_topic"])
        with zc._PENDING_CLOSE_LOCK:
            assert t_norm not in zc._PENDING_CLOSE_TIMERS, (
                "publish failure must not leave a stale entry in _PENDING_CLOSE_TIMERS; "
                f"got {dict(zc._PENDING_CLOSE_TIMERS)!r}"
            )

    def test_foreign_newer_timer_is_NOT_wiped_when_older_fires(self, test_db):
        """Race: callerA's timer fires AFTER callerB swapped in a newer Timer
        for the SAME topic. The older _do_close MUST identity-check and leave
        the newer Timer in the dict untouched.

        This is the most important guarantee from A6 — without it, the
        cleanup is racy and silently neutralises a freshly-scheduled close.
        """
        import services.zone_control as zc

        _gid, _srv_id, group_dict = _make_master_group(test_db)
        from utils import normalize_topic

        t_norm = normalize_topic(group_dict["master_mqtt_topic"])

        # Build a Sentinel "newer" timer — a real Timer object that will
        # never run (cancelled immediately) but whose identity we can check
        # against what remains in the dict.
        newer_timer = threading.Timer(999.0, lambda: None)
        newer_timer.daemon = True

        # Force the older _do_close to run synchronously inside the test:
        # block its publish on an Event so we can swap the dict entry BEFORE
        # the finally runs.
        publish_can_finish = threading.Event()

        def _slow_publish(*_args, **_kwargs):
            # While the publish is "in flight", a concurrent caller swaps in
            # the newer Timer for this same topic.
            with zc._PENDING_CLOSE_LOCK:
                zc._PENDING_CLOSE_TIMERS[t_norm] = newer_timer
            publish_can_finish.wait(timeout=2.0)
            return True

        try:
            with (
                patch.object(zc, "db", test_db),
                patch.object(zc, "publish_mqtt_value", side_effect=_slow_publish),
                patch.object(zc, "TESTING", False),
            ):
                zc._schedule_master_close(group_dict, immediate=True)
                # Give the daemon a moment to enter publish
                time.sleep(0.3)
                # Let the older _do_close finish its publish + finally
                publish_can_finish.set()
                time.sleep(0.5)

            # CRITICAL: the newer Timer must still be in the dict.
            with zc._PENDING_CLOSE_LOCK:
                cached = zc._PENDING_CLOSE_TIMERS.get(t_norm)
            assert cached is newer_timer, (
                "Older _do_close finally MUST identity-check before popping — "
                f"newer foreign Timer must remain. cached={cached!r}, expected={newer_timer!r}"
            )
        finally:
            # Cleanup so other tests don't see leftover entry
            try:
                newer_timer.cancel()
            except Exception:
                pass
            with zc._PENDING_CLOSE_LOCK:
                if zc._PENDING_CLOSE_TIMERS.get(t_norm) is newer_timer:
                    zc._PENDING_CLOSE_TIMERS.pop(t_norm, None)


# --- A7 tests ---------------------------------------------------------------


class TestA7StaleZoneRecoveryOnStartup:
    """A7: watchdog.run() startup recovers stale zones BEFORE first tick."""

    def test_stale_on_zone_is_reset_to_off_with_audit_log(self, test_db):
        """``state='on'`` with elapsed > duration → reset to ``off`` with audit log."""
        from datetime import datetime, timedelta

        from services.watchdog import ZoneWatchdog

        # Set up: zone with state='on', watering_start_time far in the past
        test_db.create_group("G")
        gid = int(test_db.get_groups()[-1]["id"])
        z_created = test_db.create_zone(
            {
                "name": "Z1",
                "duration": 5,  # 5 minutes
                "group_id": gid,
                "topic": "/x/K1",
                "mqtt_server_id": 1,
            }
        )
        zone_id = int(z_created["id"])
        old_start = (datetime.now() - timedelta(hours=2)).strftime("%Y-%m-%d %H:%M:%S")
        test_db.update_zone(
            zone_id,
            {"state": "on", "watering_start_time": old_start},
        )

        wd = ZoneWatchdog(test_db, zone_control_module=MagicMock())
        # Direct call — we test the recovery method, not the run() loop.
        wd._recover_stale_zones()

        zone_after = test_db.get_zone(zone_id)
        assert str(zone_after.get("state")).lower() == "off", (
            f"Stale zone must be reset to 'off'; got state={zone_after.get('state')!r}"
        )
        # watering_start_time cleared so the next legitimate run isn't seen as elapsed
        assert not zone_after.get("watering_start_time"), (
            "Stale zone watering_start_time must be cleared on recovery"
        )

        # Audit log with the spec-required action_type
        logs = test_db.get_logs(event_type="stale_on_recovery_after_restart") or []
        assert logs, (
            "Recovery must emit DB log 'stale_on_recovery_after_restart' per audit spec"
        )

    def test_starting_state_is_also_recovered(self, test_db):
        """``state='starting'`` is just as dangerous as ``state='on'`` — both recover."""
        from datetime import datetime, timedelta

        from services.watchdog import ZoneWatchdog

        test_db.create_group("G")
        gid = int(test_db.get_groups()[-1]["id"])
        z_created = test_db.create_zone(
            {
                "name": "Z1",
                "duration": 3,
                "group_id": gid,
                "topic": "/x/K1",
                "mqtt_server_id": 1,
            }
        )
        zone_id = int(z_created["id"])
        old_start = (datetime.now() - timedelta(hours=1)).strftime("%Y-%m-%d %H:%M:%S")
        test_db.update_zone(
            zone_id, {"state": "starting", "watering_start_time": old_start}
        )

        wd = ZoneWatchdog(test_db, zone_control_module=MagicMock())
        wd._recover_stale_zones()

        zone_after = test_db.get_zone(zone_id)
        assert str(zone_after.get("state")).lower() == "off"

    def test_fresh_on_zone_within_duration_is_NOT_touched(self, test_db):
        """Sanity: a zone that just started must not be recovered as stale."""
        from datetime import datetime

        from services.watchdog import ZoneWatchdog

        test_db.create_group("G")
        gid = int(test_db.get_groups()[-1]["id"])
        z_created = test_db.create_zone(
            {
                "name": "Z1",
                "duration": 30,  # 30-minute legitimate window
                "group_id": gid,
                "topic": "/x/K1",
                "mqtt_server_id": 1,
            }
        )
        zone_id = int(z_created["id"])
        now_start = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        test_db.update_zone(
            zone_id, {"state": "on", "watering_start_time": now_start}
        )

        wd = ZoneWatchdog(test_db, zone_control_module=MagicMock())
        wd._recover_stale_zones()

        zone_after = test_db.get_zone(zone_id)
        assert str(zone_after.get("state")).lower() == "on", (
            "Active zone within duration window must NOT be reset"
        )


# --- A8 tests ---------------------------------------------------------------


class TestA8MasterCloseInFinally:
    """A8: program/group sequence ``finally`` closes master for touched groups.

    These tests exercise the ``finally`` block directly by raising mid-loop
    via a patched dependency, then assert ``_schedule_master_close`` was
    called for the affected groups.
    """

    def test_group_sequence_finally_schedules_master_close_for_touched_group(self, test_db):
        """A raise inside _run_group_sequence after master opened → finally
        still schedules a close for the group's master valve.

        We bypass the TESTING short-circuit and force the weather check to
        raise — this exercises the real ``finally`` block which (per A8)
        must call ``_schedule_master_close`` for the touched group.
        """
        import irrigation_scheduler as sched_mod

        _gid, _srv_id, _g = _make_master_group(test_db)

        sched = sched_mod.IrrigationScheduler(test_db)

        scheduled_for: list = []

        def _capture(group_dict, immediate=False):
            scheduled_for.append((int(group_dict.get("id") or 0), immediate))

        # Bypass the TESTING short-circuit so we hit the real loop +
        # finally path. Make _check_weather_skip raise mid-run.
        os.environ["SKIP_TESTING_SHORT_CIRCUIT_FOR_GROUP_SEQ"] = "1"
        try:
            with (
                patch.object(sched, "db", test_db),
                patch("services.zone_control._schedule_master_close", side_effect=_capture),
                patch.object(
                    sched, "_check_weather_skip", side_effect=RuntimeError("boom")
                ),
            ):
                try:
                    sched._run_group_sequence(_gid, zone_ids=[], manual=False)
                except RuntimeError:
                    pass
        finally:
            os.environ.pop("SKIP_TESTING_SHORT_CIRCUIT_FOR_GROUP_SEQ", None)

        # Finally must have scheduled at least one close for the group we
        # passed in (use_master_valve=1).
        assert any(item[0] == _gid for item in scheduled_for), (
            f"_run_group_sequence finally must schedule master close for touched group {_gid}; "
            f"got {scheduled_for!r}"
        )


# --- A9 tests ---------------------------------------------------------------


class TestA9BoundedPublishAndRunLoop:
    """A9: bounded publish path + broad-exception run() loop."""

    def test_bounded_publish_skips_when_broker_disconnected(self, test_db):
        """``is_connected()`` False → return False FAST, no paho.publish call."""
        from services.watchdog import ZoneWatchdog

        _gid, srv_id, group_dict = _make_master_group(test_db)

        # Fake offline client: is_connected() returns False
        fake_client = MagicMock()
        fake_client.is_connected.return_value = False

        wd = ZoneWatchdog(test_db, zone_control_module=MagicMock())

        with (
            patch("services.mqtt_pub._MQTT_CLIENTS", {srv_id: fake_client}),
            patch("services.mqtt_pub.publish_mqtt_value") as mock_pub,
        ):
            start = time.monotonic()
            ok = wd._publish_master_close_bounded(group_dict)
            elapsed = time.monotonic() - start

        assert ok is False, "Offline broker → bounded publish must return False"
        # MUST NOT have called the underlying publish at all
        mock_pub.assert_not_called()
        # Sub-second skip
        assert elapsed < 1.0, (
            f"Offline pre-check must return fast; took {elapsed:.2f}s"
        )

    def test_bounded_publish_returns_within_timeout_when_publish_hangs(self, test_db):
        """Hard ceiling: if paho.publish() blocks 30s, bounded path returns
        in < ~3s (worker join timeout is 2s)."""
        from services import watchdog as wd_mod
        from services.watchdog import ZoneWatchdog

        _gid, srv_id, group_dict = _make_master_group(test_db)

        # is_connected() reports OK so the bounded path actually calls publish.
        fake_client = MagicMock()
        fake_client.is_connected.return_value = True

        def _hanging_publish(*_a, **_kw):
            time.sleep(30)
            return True

        wd = ZoneWatchdog(test_db, zone_control_module=MagicMock())

        with (
            patch("services.mqtt_pub._MQTT_CLIENTS", {srv_id: fake_client}),
            patch("services.mqtt_pub.publish_mqtt_value", side_effect=_hanging_publish),
        ):
            start = time.monotonic()
            ok = wd._publish_master_close_bounded(group_dict)
            elapsed = time.monotonic() - start

        assert ok is False, "Hanging publish must be treated as failure"
        assert elapsed < 3.0, (
            f"Bounded publish must exit within ~{wd_mod.SUPERVISOR_PUBLISH_TIMEOUT_SEC}s + slack; "
            f"took {elapsed:.2f}s"
        )

    def test_run_loop_survives_keyerror_in_get_groups(self, test_db):
        """``run()`` must catch ANY exception (KeyError/TypeError/etc.) and keep ticking.

        Pre-fix, the narrow tuple let a KeyError kill the daemon thread —
        supervisor was off until process restart.
        """
        from services.watchdog import ZoneWatchdog

        # Wrap db so get_groups raises KeyError on first call, succeeds after
        call_count = {"n": 0}
        original_get_groups = test_db.get_groups

        def _flaky_get_groups(*a, **kw):
            call_count["n"] += 1
            if call_count["n"] == 1:
                raise KeyError("simulated startup bug")
            return original_get_groups(*a, **kw)

        wd = ZoneWatchdog(test_db, zone_control_module=MagicMock(), interval=1)
        # Patch only the get_groups method, so other methods on db keep working
        with patch.object(test_db, "get_groups", side_effect=_flaky_get_groups):
            wd.start()
            try:
                # Let the loop run a couple of ticks (initial 5s wait + interval)
                time.sleep(7)
                assert wd.is_alive(), (
                    "Watchdog thread MUST survive KeyError in db.get_groups — "
                    "run() must use broad `except Exception` per audit A9"
                )
            finally:
                wd.stop()
                wd.join(timeout=3)

        assert call_count["n"] >= 1, "get_groups should have been invoked at least once"
