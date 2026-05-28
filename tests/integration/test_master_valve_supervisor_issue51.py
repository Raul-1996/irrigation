"""Issue #51 — regression test: master valve supervisor must force-close
a master valve that was left ``open`` by a program with partial failure.

Root cause: when ``_run_program_threaded`` (or any zone-finalising path) raises
between the call to ``exclusive_start_zone`` (which physically opens the master
valve) and the matching ``stop_zone`` (which schedules the master close), the
master valve stays physically open. No pending close timer is armed.
``master_valve_observed`` stays ``'open'``. The UI shows reality correctly, but
no actor will close the relay.

This watchdog/supervisor approach: a periodic check in ``ZoneWatchdog`` looks
for any group with ``use_master_valve=1`` AND ``master_valve_observed='open'``
AND no active zones under that master topic AND no pending close timer in
``services.zone_control._PENDING_CLOSE_TIMERS``. When found, the supervisor
synchronously publishes the master close (mode-aware) and flips
``master_valve_observed`` to ``'closed'``.

Differs from Senior #1's try/finally approach: the fix is in a *supervisor*
loop rather than at the call site. Pros: catches any future code path that
forgets to close. Cons: extra background activity (5s interval).
"""

import os
import time
from unittest.mock import patch

os.environ["TESTING"] = "1"


def _make_master_group(test_db, observed: str = "open"):
    """Create an MQTT server + a group with NC master valve.

    Returns ``(group_id, server_id)``.
    """
    srv = test_db.create_mqtt_server({"name": "S", "host": "127.0.0.1", "port": 1883})
    test_db.create_group("MV Group")
    groups = test_db.get_groups()
    gid = int(groups[-1]["id"])
    test_db.update_group_fields(
        gid,
        {
            "use_master_valve": 1,
            "master_mqtt_topic": "/devices/wb-mrwm2_42/controls/K1",
            "master_mqtt_server_id": int(srv["id"]),
            "master_mode": "NC",
            "master_close_delay_sec": 1,
            "master_valve_observed": observed,
        },
    )
    return gid, int(srv["id"])


class TestMasterValveSupervisorIssue51:
    def test_supervisor_force_closes_orphan_master_valve(self, test_db):
        """master_valve_observed='open' + no active zones + no pending timer
        → supervisor must publish close and flip observed to 'closed'."""
        import services.zone_control as zc
        from services.watchdog import ZoneWatchdog

        gid, _ = _make_master_group(test_db, observed="open")

        # No zones in this group at all (no zone in on/starting).
        # No pending close timer either.
        with zc._PENDING_CLOSE_LOCK:
            zc._PENDING_CLOSE_TIMERS.clear()

        wd = ZoneWatchdog(test_db, zc, interval=5)

        with (
            patch.object(zc, "db", test_db),
            patch.object(zc, "publish_mqtt_value", return_value=True) as mock_pub,
        ):
            wd._check_master_valves()

        # Supervisor must have published the NC close value '0'.
        assert mock_pub.called, "supervisor must publish master close when valve is orphaned"
        call_args = mock_pub.call_args
        assert call_args.args[2] == "0", f"NC close payload must be '0', got {call_args.args[2]!r}"

        # observed flipped to 'closed' in DB.
        g_after = next(g for g in test_db.get_groups() if int(g["id"]) == gid)
        assert g_after.get("master_valve_observed") == "closed", (
            f"observed must be 'closed' after supervisor force-close; got "
            f"{g_after.get('master_valve_observed')!r}"
        )

    def test_supervisor_skips_when_zone_active(self, test_db):
        """If any zone under the master topic is on/starting, supervisor must
        NOT touch the master valve — legitimate watering is in progress."""
        import services.zone_control as zc
        from services.watchdog import ZoneWatchdog

        gid, _ = _make_master_group(test_db, observed="open")

        # Active zone in the same group.
        zone = test_db.create_zone(
            {"name": "Active Zone", "duration": 10, "group_id": gid, "topic": "/devices/wb/controls/Z1"}
        )
        from datetime import datetime

        now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        test_db.update_zone(zone["id"], {"state": "on", "watering_start_time": now_str})

        with zc._PENDING_CLOSE_LOCK:
            zc._PENDING_CLOSE_TIMERS.clear()

        wd = ZoneWatchdog(test_db, zc, interval=5)

        with (
            patch.object(zc, "db", test_db),
            patch.object(zc, "publish_mqtt_value", return_value=True) as mock_pub,
        ):
            wd._check_master_valves()

        assert not mock_pub.called, "supervisor must not publish close while a zone is active"

        g_after = next(g for g in test_db.get_groups() if int(g["id"]) == gid)
        assert g_after.get("master_valve_observed") == "open", (
            "observed must stay 'open' while a zone is active"
        )

    def test_supervisor_skips_when_pending_timer_armed(self, test_db):
        """If ``_PENDING_CLOSE_TIMERS`` has a timer for this master topic,
        a legitimate close is in flight — supervisor must not interfere."""
        import threading

        import services.zone_control as zc
        from services.watchdog import ZoneWatchdog
        from utils import normalize_topic

        gid, _ = _make_master_group(test_db, observed="open")

        # Plant a pending timer manually (don't actually start it — we just
        # want the dict entry; cancel() on never-started Timer is a no-op).
        t_norm = normalize_topic("/devices/wb-mrwm2_42/controls/K1")
        dummy_timer = threading.Timer(60.0, lambda: None)
        try:
            with zc._PENDING_CLOSE_LOCK:
                zc._PENDING_CLOSE_TIMERS[t_norm] = dummy_timer

            wd = ZoneWatchdog(test_db, zc, interval=5)

            with (
                patch.object(zc, "db", test_db),
                patch.object(zc, "publish_mqtt_value", return_value=True) as mock_pub,
            ):
                wd._check_master_valves()

            assert not mock_pub.called, (
                "supervisor must not publish close while a legitimate close timer is armed"
            )
        finally:
            with zc._PENDING_CLOSE_LOCK:
                zc._PENDING_CLOSE_TIMERS.pop(t_norm, None)
            dummy_timer.cancel()

    def test_supervisor_skips_when_observed_already_closed(self, test_db):
        """master_valve_observed='closed' → nothing to do."""
        import services.zone_control as zc
        from services.watchdog import ZoneWatchdog

        gid, _ = _make_master_group(test_db, observed="closed")

        with zc._PENDING_CLOSE_LOCK:
            zc._PENDING_CLOSE_TIMERS.clear()

        wd = ZoneWatchdog(test_db, zc, interval=5)

        with (
            patch.object(zc, "db", test_db),
            patch.object(zc, "publish_mqtt_value", return_value=True) as mock_pub,
        ):
            wd._check_master_valves()

        assert not mock_pub.called, "supervisor must not publish close when observed is already 'closed'"

    def test_supervisor_skips_when_use_master_valve_disabled(self, test_db):
        """Group with use_master_valve=0 must be ignored entirely."""
        import services.zone_control as zc
        from services.watchdog import ZoneWatchdog

        gid, _ = _make_master_group(test_db, observed="open")
        test_db.update_group_fields(gid, {"use_master_valve": 0})

        with zc._PENDING_CLOSE_LOCK:
            zc._PENDING_CLOSE_TIMERS.clear()

        wd = ZoneWatchdog(test_db, zc, interval=5)

        with (
            patch.object(zc, "db", test_db),
            patch.object(zc, "publish_mqtt_value", return_value=True) as mock_pub,
        ):
            wd._check_master_valves()

        assert not mock_pub.called, "supervisor must ignore groups where use_master_valve=0"

    def test_supervisor_force_close_after_simulated_program_partial_failure(self, test_db):
        """End-to-end regression: simulate the bug scenario from Issue #51.

        1. Create 2-zone program setup (group + 2 zones + master valve).
        2. Simulate that exclusive_start_zone opened the master valve
           (observed='open') and started zone1.
        3. Simulate that the last zone's stop_zone raised before scheduling
           the master close — so zone is now 'off' but master observed='open'
           with NO pending close timer.
        4. supervisor tick should:
           - detect the inconsistency
           - publish master close (NC mode → '0')
           - flip observed to 'closed'
           - emit watchdog_master_close audit log entry

        This is the exact failure mode described in Issue #51 acceptance
        criterion #3.
        """
        import services.zone_control as zc
        from services.watchdog import ZoneWatchdog

        gid, _ = _make_master_group(test_db, observed="open")

        # Create 2 zones — both 'off' (simulating program finished but master
        # stayed open due to partial failure).
        from datetime import datetime

        z1 = test_db.create_zone({"name": "Zone 1", "duration": 5, "group_id": gid, "topic": "/devices/wb/controls/Z1"})
        z2 = test_db.create_zone({"name": "Zone 2", "duration": 5, "group_id": gid, "topic": "/devices/wb/controls/Z2"})
        # Mark both zones as 'off' (zone2 stopped normally; zone1 errored before
        # stop_zone could schedule master close).
        for zid in (z1["id"], z2["id"]):
            test_db.update_zone(zid, {"state": "off", "watering_start_time": None})

        # Critically: NO pending close timer for this master topic.
        with zc._PENDING_CLOSE_LOCK:
            zc._PENDING_CLOSE_TIMERS.clear()

        # Pre-condition: master valve is observed 'open', no zones active,
        # no timer pending. This is the exact orphan state.
        g_before = next(g for g in test_db.get_groups() if int(g["id"]) == gid)
        assert g_before.get("master_valve_observed") == "open"

        wd = ZoneWatchdog(test_db, zc, interval=5)

        published = []

        def _capture_publish(server, topic, value, *args, **kwargs):
            published.append((topic, value))
            return True

        # Acceptance: detection happens in a single supervisor tick (≤5s
        # interval → worst-case 5s detection latency).
        t0 = time.monotonic()
        with (
            patch.object(zc, "db", test_db),
            patch.object(zc, "publish_mqtt_value", side_effect=_capture_publish),
        ):
            wd._check_master_valves()
        elapsed = time.monotonic() - t0

        # 1) Master close MQTT publish happened.
        assert published, f"expected one master close publish, got {published!r}"
        topic, value = published[0]
        assert "K1" in topic, f"expected master topic to contain 'K1', got {topic!r}"
        assert value == "0", f"NC close value must be '0', got {value!r}"

        # 2) observed flipped to 'closed' in DB.
        g_after = next(g for g in test_db.get_groups() if int(g["id"]) == gid)
        assert g_after.get("master_valve_observed") == "closed", (
            f"observed must be 'closed'; got {g_after.get('master_valve_observed')!r}"
        )

        # 3) supervisor tick itself is fast (well under 5s) — the periodicity
        # of the watchdog is what bounds detection latency, not the tick cost.
        assert elapsed < 1.0, f"supervisor tick took {elapsed:.2f}s — too slow"
