"""A6 — _PENDING_CLOSE_TIMERS must be popped after _do_close on every path.

See audits/2026-05-28-security/findings.md section A6.

Pre-fix bug: ``_schedule_master_close`` planted the timer in
``_PENDING_CLOSE_TIMERS[topic]`` and never removed it. The watchdog
supervisor's "topic in _PENDING_CLOSE_TIMERS" guard treated this stale
entry as "close already armed" and refused to publish its own close,
leaving the master valve open up to cap_minutes (default 4h) after a
broker flap.

The fix wraps ``_do_close`` in a try/finally that pops the topic, but
ONLY if the cached Timer is identity-equal to the one this invocation
scheduled — so a concurrent ``_schedule_master_close`` for the same
topic (which would have popped + cancelled the old one already) wins
the race and we don't wipe its fresh Timer.
"""

import os
import time
from unittest.mock import patch

os.environ["TESTING"] = "1"


def _make_master_group(test_db, *, observed: str = "open"):
    srv = test_db.create_mqtt_server({"name": "S", "host": "127.0.0.1", "port": 1883})
    test_db.create_group("MV Group A6")
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


class TestA6PendingCloseCleanup:
    def test_pop_after_successful_close(self, test_db):
        """_do_close returns OK → topic absent from _PENDING_CLOSE_TIMERS."""
        import services.zone_control as zc
        from services import sse_hub as _sse_hub
        from utils import normalize_topic

        gid, _ = _make_master_group(test_db)
        group_dict = next(g for g in test_db.get_groups() if int(g["id"]) == gid)
        t_norm = normalize_topic(group_dict["master_mqtt_topic"])

        with (
            patch.object(zc, "db", test_db),
            patch.object(zc, "publish_mqtt_value", return_value=True),
            patch.object(zc, "TESTING", False),
            patch.object(_sse_hub, "broadcast"),
        ):
            zc._schedule_master_close(group_dict, immediate=True)
            # delay=0 spawns daemon; let it land.
            for _ in range(20):
                with zc._PENDING_CLOSE_LOCK:
                    if t_norm not in zc._PENDING_CLOSE_TIMERS:
                        break
                time.sleep(0.1)

        with zc._PENDING_CLOSE_LOCK:
            assert t_norm not in zc._PENDING_CLOSE_TIMERS, (
                f"_PENDING_CLOSE_TIMERS leaked entry for {t_norm!r} after happy-path close. "
                f"Watchdog supervisor will refuse to publish for this topic forever."
            )

    def test_pop_after_publish_failure(self, test_db):
        """_do_close hits an MQTT failure → topic still cleaned up."""
        import services.zone_control as zc
        from services import sse_hub as _sse_hub
        from utils import normalize_topic

        gid, _ = _make_master_group(test_db)
        group_dict = next(g for g in test_db.get_groups() if int(g["id"]) == gid)
        t_norm = normalize_topic(group_dict["master_mqtt_topic"])

        with (
            patch.object(zc, "db", test_db),
            patch.object(zc, "publish_mqtt_value", return_value=False),
            patch.object(zc, "TESTING", False),
            patch.object(_sse_hub, "broadcast"),
        ):
            zc._schedule_master_close(group_dict, immediate=True)
            for _ in range(20):
                with zc._PENDING_CLOSE_LOCK:
                    if t_norm not in zc._PENDING_CLOSE_TIMERS:
                        break
                time.sleep(0.1)

        with zc._PENDING_CLOSE_LOCK:
            assert t_norm not in zc._PENDING_CLOSE_TIMERS, (
                "_PENDING_CLOSE_TIMERS leaked entry after publish failure path"
            )

    def test_pop_after_exception_in_close(self, test_db):
        """_do_close raises → topic still cleaned up (finally semantics)."""
        import services.zone_control as zc
        from utils import normalize_topic

        gid, _ = _make_master_group(test_db)
        group_dict = next(g for g in test_db.get_groups() if int(g["id"]) == gid)
        t_norm = normalize_topic(group_dict["master_mqtt_topic"])

        # Make publish_mqtt_value raise — outer try/except inside _do_close
        # catches it, then finally still pops.
        def _boom(*a, **kw):
            raise RuntimeError("simulated mqtt blowup")

        with (
            patch.object(zc, "db", test_db),
            patch.object(zc, "publish_mqtt_value", side_effect=_boom),
            patch.object(zc, "TESTING", False),
        ):
            zc._schedule_master_close(group_dict, immediate=True)
            for _ in range(20):
                with zc._PENDING_CLOSE_LOCK:
                    if t_norm not in zc._PENDING_CLOSE_TIMERS:
                        break
                time.sleep(0.1)

        with zc._PENDING_CLOSE_LOCK:
            assert t_norm not in zc._PENDING_CLOSE_TIMERS, (
                "_PENDING_CLOSE_TIMERS leaked entry after exception in close body"
            )

    def test_pop_after_skipped_due_to_active_zone(self, test_db):
        """_do_close sees an ON zone, returns early → still cleaned up."""
        import services.zone_control as zc
        from utils import normalize_topic

        gid, _ = _make_master_group(test_db)
        group_dict = next(g for g in test_db.get_groups() if int(g["id"]) == gid)
        t_norm = normalize_topic(group_dict["master_mqtt_topic"])

        # Plant an ON zone in this group so _do_close's any_on guard fires.
        z = test_db.create_zone({"name": "Z1", "duration": 10, "group_id": gid})
        test_db.update_zone(z["id"], {"state": "on"})

        with (
            patch.object(zc, "db", test_db),
            patch.object(zc, "publish_mqtt_value", return_value=True),
            patch.object(zc, "TESTING", False),
        ):
            zc._schedule_master_close(group_dict, immediate=True)
            for _ in range(20):
                with zc._PENDING_CLOSE_LOCK:
                    if t_norm not in zc._PENDING_CLOSE_TIMERS:
                        break
                time.sleep(0.1)

        with zc._PENDING_CLOSE_LOCK:
            assert t_norm not in zc._PENDING_CLOSE_TIMERS, "_PENDING_CLOSE_TIMERS leaked entry after any-on skip"

    def test_race_two_schedule_calls_no_foreign_wipeout(self, test_db):
        """Two _schedule_master_close in quick succession → second wins.

        Verifies the identity guard: when the SECOND ``_schedule_master_close``
        runs it pops the prior timer from the dict (cancelling it) and
        installs its own. If the first (cancelled) timer somehow still
        fires its body before being garbage-collected, its finally must
        NOT wipe the second timer's fresh entry.

        Implementation: drive the race ourselves by stubbing publish to
        block briefly so we can sample the dict state between events.
        """
        import services.zone_control as zc
        from utils import normalize_topic

        gid, _ = _make_master_group(test_db)
        group_dict = next(g for g in test_db.get_groups() if int(g["id"]) == gid)
        t_norm = normalize_topic(group_dict["master_mqtt_topic"])

        publish_calls = []

        def _slow_publish(*a, **kw):
            publish_calls.append(a)
            time.sleep(0.05)
            return True

        with (
            patch.object(zc, "db", test_db),
            patch.object(zc, "publish_mqtt_value", side_effect=_slow_publish),
            patch.object(zc, "TESTING", False),
        ):
            # First scheduling — delay=0 fires almost immediately.
            zc._schedule_master_close(group_dict, immediate=True)
            with zc._PENDING_CLOSE_LOCK:
                first_timer = zc._PENDING_CLOSE_TIMERS.get(t_norm)
            assert first_timer is not None
            # Second scheduling for the SAME topic, also immediate. The
            # prev pop+cancel inside _schedule_master_close must cancel
            # the first and install the second.
            zc._schedule_master_close(group_dict, immediate=True)
            with zc._PENDING_CLOSE_LOCK:
                second_timer = zc._PENDING_CLOSE_TIMERS.get(t_norm)
            # Either the second timer is there (still pending) or both
            # have finished and the dict is clean — both are acceptable
            # post-conditions. The forbidden state is "first_timer object
            # still in the dict" because that means the second timer's
            # identity wasn't recorded.
            assert second_timer is not first_timer, (
                "second _schedule_master_close did not replace first timer reference"
            )
            # Wait for both timers to land.
            for _ in range(30):
                with zc._PENDING_CLOSE_LOCK:
                    if t_norm not in zc._PENDING_CLOSE_TIMERS:
                        break
                time.sleep(0.1)

        with zc._PENDING_CLOSE_LOCK:
            assert t_norm not in zc._PENDING_CLOSE_TIMERS, "_PENDING_CLOSE_TIMERS leaked entry after two-call race"
