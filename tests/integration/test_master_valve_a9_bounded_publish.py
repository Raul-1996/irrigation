"""A9 — watchdog MQTT publish must NOT block more than ~2s on dead broker.

See audits/2026-05-28-security/findings.md section A9.

Pre-fix bug: ``paho-mqtt`` publish + wait_for_publish can block ~45s on
a dead broker. The watchdog supervisor runs single-threaded → one bad
tick stalls the next ones, exactly at the moment the master valve most
needs supervisor coverage.

Fix (this branch's approach, both belt and suspenders):
  1. Pre-check the cached client's ``is_connected()`` — if offline,
     skip publish entirely and let the next tick retry.
  2. Run the publish call on a worker thread and ``join(timeout=2s)`` —
     if the worker overruns, we leave it alive and proceed. Stops a
     just-died broker from blocking the supervisor.

Also covers the run() resilience requirement from the brief: the
narrow exception tuple is replaced with ``except Exception`` +
``logger.exception``, so KeyError/TypeError/AttributeError from
``db.get_groups()`` (or anywhere else) does NOT kill the daemon thread.
"""

import os
import time
from unittest.mock import MagicMock, patch

os.environ["TESTING"] = "1"


def _make_master_group(test_db, *, observed: str = "open"):
    srv = test_db.create_mqtt_server({"name": "S", "host": "127.0.0.1", "port": 1883})
    test_db.create_group("MV Group A9")
    groups = test_db.get_groups()
    gid = int(groups[-1]["id"])
    test_db.update_group_fields(
        gid,
        {
            "use_master_valve": 1,
            "master_mqtt_topic": "/devices/wb-mrwm2_42/controls/K1",
            "master_mqtt_server_id": int(srv["id"]),
            "master_mode": "NC",
            "master_close_delay_sec": 60,
            "master_valve_observed": observed,
        },
    )
    return gid, int(srv["id"])


class TestA9BoundedPublish:
    def test_single_tick_completes_within_3s_on_dead_broker(self, test_db):
        """publish_mqtt_value sleeps 30s → _check_master_valves tick < 3s."""
        from services.watchdog import ZoneWatchdog
        import services.mqtt_pub as mqtt_pub

        gid, _ = _make_master_group(test_db, observed="open")
        # Group has observed=open AND no active zones → supervisor will
        # call _publish_master_close_bounded.

        def _slow_publish(*a, **kw):
            time.sleep(30)
            return True

        # Bypass the is_connected pre-check so the bounded join is the
        # ONLY guard — that's the worst case we need to prove.
        with (
            patch.object(mqtt_pub, "publish_mqtt_value", side_effect=_slow_publish),
            patch.object(mqtt_pub, "_MQTT_CLIENTS", {}),
        ):
            wd = ZoneWatchdog(test_db, MagicMock(), interval=1)
            t0 = time.monotonic()
            wd._check_master_valves()
            elapsed = time.monotonic() - t0

        assert elapsed < 3.0, (
            f"single _check_master_valves tick took {elapsed:.2f}s on simulated dead broker; "
            f"must be < 3s per A9 requirement"
        )

    def test_is_connected_false_skips_publish_fast(self, test_db):
        """Cached client offline → no publish call at all, tick is near-zero."""
        from services.watchdog import ZoneWatchdog
        import services.mqtt_pub as mqtt_pub

        gid, srv_id = _make_master_group(test_db, observed="open")

        # Inject a fake cached client that reports offline.
        fake_cl = MagicMock()
        fake_cl.is_connected = MagicMock(return_value=False)

        publish_called = []

        def _record_publish(*a, **kw):
            publish_called.append(a)
            return True

        with (
            patch.object(mqtt_pub, "_MQTT_CLIENTS", {srv_id: fake_cl}),
            patch.object(mqtt_pub, "publish_mqtt_value", side_effect=_record_publish),
        ):
            wd = ZoneWatchdog(test_db, MagicMock(), interval=1)
            t0 = time.monotonic()
            wd._check_master_valves()
            elapsed = time.monotonic() - t0

        assert elapsed < 1.0, f"offline-broker fast path took {elapsed:.2f}s, expected < 1s"
        assert not publish_called, (
            f"publish_mqtt_value must NOT be called when cached client reports offline; "
            f"got {publish_called!r}"
        )


class TestRunResilience:
    def test_run_loop_survives_keyerror_from_get_groups(self, test_db):
        """db.get_groups raising KeyError must not kill the watchdog thread.

        Before the fix, the run() loop's narrow exception tuple
        (ConnectionError, TimeoutError, OSError, sqlite3.Error,
        ValueError, RuntimeError) didn't include KeyError/TypeError/
        AttributeError, so any of those would unwind the daemon thread
        and the supervisor was permanently OFF until process restart.
        """
        from services.watchdog import ZoneWatchdog
        import services.watchdog as wd_mod

        wd = ZoneWatchdog(test_db, MagicMock(), interval=1)

        # Replace get_groups + get_zones with the same KeyError raiser so
        # both _check_zones and _check_master_valves blow up.
        boom_count = {"n": 0}

        def _boom(*a, **kw):
            boom_count["n"] += 1
            raise KeyError("simulated downstream KeyError")

        # Use a stubbed db on the instance — easier than patching the
        # real test_db.
        stub_db = MagicMock()
        stub_db.get_zones = MagicMock(side_effect=_boom)
        stub_db.get_groups = MagicMock(side_effect=_boom)
        stub_db.get_setting_value = MagicMock(return_value=None)
        wd.db = stub_db

        # Make wait() return False once (so a tick happens) then True (so
        # the loop exits cleanly without hanging the test).
        wait_results = iter([False, False, True, True, True, True])
        wd._stop_event = MagicMock()
        wd._stop_event.wait = MagicMock(side_effect=lambda timeout=0: next(wait_results, True))
        wd._stop_event.is_set = MagicMock(side_effect=lambda: next(wait_results, True))

        # Also patch _recover_stale_zones (its own try/except) so the
        # initial recovery call doesn't dominate the test.
        with patch.object(wd, "_recover_stale_zones"):
            # If the narrow except were still in place, this would raise
            # KeyError out of run() and the test would fail with an
            # uncaught exception.
            wd.run()

        # The boom function was called → run() did iterate.
        assert boom_count["n"] >= 1, (
            "expected at least one tick to hit the stubbed db.get_zones/get_groups"
        )
        # Quietly assert we used the broadened exception path: run() must
        # have completed normally (no propagating KeyError).
