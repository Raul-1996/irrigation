"""Issue #38 — regression test: master valve close must not write
``master_valve_observed='closed'`` and must not broadcast SSE
``mv_state='closed'`` when the underlying MQTT publish fails.

Root cause recap (see audits/2026-05-16-issue-38/ARCHITECT.md): on
Wirenboard the relay only obeys the ``<topic>/on`` command channel; the
base topic is the report channel. The ``/on`` companion publish used to
be fire-and-forget. When it silently dropped on a transient broker
hiccup, the base topic (retained) updated, the SSE-hub heard the echo
and wrote ``master_valve_observed='closed'`` — UI lied while the relay
stayed open. The fix makes ``publish_mqtt_value`` return ``False`` when
the ``/on`` ack is missed and gates the optimistic DB+SSE writes on
that return value.
"""

import os
import time
from unittest.mock import patch

os.environ["TESTING"] = "1"


def _make_master_group(test_db):
    """Create an MQTT server + a group with NC master valve configured.

    Returns ``(group_id, server_id)``. The group starts with
    ``master_valve_observed='open'`` so that the post-condition assertion
    'observed is NOT closed' has a concrete prior value to compare against.
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
            "master_valve_observed": "open",
        },
    )
    return gid, int(srv["id"])


class TestMasterCloseDesyncIssue38:
    def test_publish_failure_does_not_write_closed(self, test_db):
        """publish_mqtt_value returns False → DB stays 'open', no SSE 'closed'."""
        import services.zone_control as zc
        from services import sse_hub as _sse_hub

        gid, _srv_id = _make_master_group(test_db)
        group_dict = next(g for g in test_db.get_groups() if int(g["id"]) == gid)

        broadcasts = []

        def _capture_broadcast(payload):
            broadcasts.append(payload)

        # Patch:
        #   - zone_control.db so the closure reads our test DB
        #   - zone_control.publish_mqtt_value → False (simulating /on drop)
        #   - zone_control.TESTING=False so the timer actually fires
        #   - sse_hub.broadcast to capture (would otherwise no-op in tests)
        with (
            patch.object(zc, "db", test_db),
            patch.object(zc, "publish_mqtt_value", return_value=False) as mock_pub,
            patch.object(
                zc.state_verifier,
                "verify_master_command",
                side_effect=lambda _sid, _topic, _expected, callback, **_kwargs: callback(),
            ),
            patch.object(zc, "TESTING", False),
            patch.object(_sse_hub, "broadcast", side_effect=_capture_broadcast),
        ):
            zc._schedule_master_close(group_dict, immediate=True)
            # delay=0 → daemon thread runs _do_close almost immediately;
            # give it a moment to land.
            time.sleep(0.5)

        # The close publish was attempted with the NC close value '0'.
        assert mock_pub.called, "publish_mqtt_value must be called for the master-close"
        call_args = mock_pub.call_args
        assert call_args.args[2] == "0", f"NC close payload must be '0', got {call_args.args[2]!r}"

        # CRITICAL: observed stays 'open' — we did NOT lie to the UI.
        g_after = next(g for g in test_db.get_groups() if int(g["id"]) == gid)
        assert g_after.get("master_valve_observed") == "open", (
            f"master_valve_observed must NOT be written to 'closed' on publish failure; "
            f"got {g_after.get('master_valve_observed')!r}"
        )

        # CRITICAL: no SSE broadcast announcing mv_state='closed'.
        for payload in broadcasts:
            assert "closed" not in str(payload), f"SSE must not broadcast 'closed' on publish failure; got {payload!r}"

    def test_publish_success_still_waits_for_fresh_physical_closed_echo(self, test_db):
        """Broker ACK is command delivery, never physical observed truth."""
        import services.zone_control as zc
        from services import sse_hub as _sse_hub

        gid, _srv_id = _make_master_group(test_db)
        group_dict = next(g for g in test_db.get_groups() if int(g["id"]) == gid)

        broadcasts = []

        with (
            patch.object(zc, "db", test_db),
            patch.object(zc, "publish_mqtt_value", return_value=True),
            patch.object(
                zc.state_verifier,
                "verify_master_command",
                side_effect=lambda _sid, _topic, _expected, callback, **_kwargs: callback() and False,
            ),
            patch.object(zc, "TESTING", False),
            patch.object(_sse_hub, "broadcast", side_effect=lambda p: broadcasts.append(p)),
        ):
            zc._schedule_master_close(group_dict, immediate=True)
            time.sleep(0.5)

        g_after = next(g for g in test_db.get_groups() if int(g["id"]) == gid)
        assert g_after.get("master_valve_observed") == "open", (
            f"master_valve_observed must remain 'open' until a fresh base-topic echo; "
            f"got {g_after.get('master_valve_observed')!r}"
        )

        closed_broadcasts = [p for p in broadcasts if "closed" in str(p) and str(gid) in str(p)]
        assert closed_broadcasts == []
