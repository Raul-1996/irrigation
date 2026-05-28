"""A7 — stale-zone recovery + master-valve supervisor on watchdog startup.

See audits/2026-05-28-security/findings.md section A7.

Pre-fix bug: after SIGKILL/OOM ``zones.state='on'`` is left in DB. On
the next process start the master-valve supervisor sees "active zones"
→ refuses to publish master_close → master valve stays open up to
``zone_cap_minutes`` (default 4h). The fix runs a stale-zone cleanup
BEFORE the first supervisor tick so the supervisor sees a coherent
DB snapshot.
"""

import os
from datetime import datetime, timedelta
from unittest.mock import MagicMock, patch

os.environ["TESTING"] = "1"


def _make_master_group(test_db, *, observed: str = "open"):
    srv = test_db.create_mqtt_server({"name": "S", "host": "127.0.0.1", "port": 1883})
    test_db.create_group("MV Group A7")
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


class TestA7StaleRecovery:
    def test_stale_on_zone_marked_off_with_audit(self, test_db):
        """Zone state='on' for 2h with duration=30 → marked off + DB log."""
        from services.watchdog import ZoneWatchdog

        gid, _ = _make_master_group(test_db)
        zone = test_db.create_zone({"name": "Stale", "duration": 30, "group_id": gid})
        past = (datetime.now() - timedelta(hours=2)).strftime("%Y-%m-%d %H:%M:%S")
        test_db.update_zone(zone["id"], {"state": "on", "watering_start_time": past})

        wd = ZoneWatchdog(test_db, MagicMock(), interval=1)
        wd._recover_stale_zones()

        z_after = test_db.get_zone(zone["id"])
        assert str(z_after.get("state")).lower() == "off", (
            f"stale ON zone must be marked off; got state={z_after.get('state')!r}"
        )
        assert z_after.get("watering_start_time") in (None, "", "None"), (
            "watering_start_time must be cleared on stale recovery"
        )

        # Audit / DB log entry with reason 'stale_on_recovery_after_restart'
        logs = test_db.get_logs() if hasattr(test_db, "get_logs") else None
        if logs is None:
            # Fall back to a direct query of the logs table.
            import sqlite3

            with sqlite3.connect(test_db.db_path) as conn:
                cur = conn.execute(
                    "SELECT type, details FROM logs WHERE type = ?",
                    ("stale_on_recovery_after_restart",),
                )
                rows = cur.fetchall()
            assert rows, "expected logs row with type=stale_on_recovery_after_restart"
            assert str(zone["id"]) in rows[0][1], "audit payload must include zone_id"
        else:
            matching = [
                lg
                for lg in logs
                if str(lg.get("type") or lg.get("log_type") or "") == "stale_on_recovery_after_restart"
            ]
            assert matching, "expected log entry stale_on_recovery_after_restart"

    def test_starting_zone_also_recovered(self, test_db):
        """state='starting' with stale start time is also recovered."""
        from services.watchdog import ZoneWatchdog

        gid, _ = _make_master_group(test_db)
        zone = test_db.create_zone({"name": "Stuck", "duration": 5, "group_id": gid})
        past = (datetime.now() - timedelta(hours=1)).strftime("%Y-%m-%d %H:%M:%S")
        test_db.update_zone(zone["id"], {"state": "starting", "watering_start_time": past})

        wd = ZoneWatchdog(test_db, MagicMock(), interval=1)
        wd._recover_stale_zones()

        z_after = test_db.get_zone(zone["id"])
        assert str(z_after.get("state")).lower() == "off"

    def test_fresh_on_zone_not_recovered(self, test_db):
        """Zone within its expected duration must NOT be recovered."""
        from services.watchdog import ZoneWatchdog

        gid, _ = _make_master_group(test_db)
        zone = test_db.create_zone({"name": "Live", "duration": 30, "group_id": gid})
        recent = (datetime.now() - timedelta(minutes=2)).strftime("%Y-%m-%d %H:%M:%S")
        test_db.update_zone(zone["id"], {"state": "on", "watering_start_time": recent})

        wd = ZoneWatchdog(test_db, MagicMock(), interval=1)
        wd._recover_stale_zones()

        z_after = test_db.get_zone(zone["id"])
        assert str(z_after.get("state")).lower() == "on", "fresh ON zone must not be marked off by stale-recovery"

    def test_recovery_triggers_master_close_when_observed_open(self, test_db):
        """After cleanup, supervisor publishes master close for observed=open groups."""
        from services import watchdog as wd_mod
        from services.watchdog import ZoneWatchdog

        gid, _ = _make_master_group(test_db, observed="open")
        zone = test_db.create_zone({"name": "S", "duration": 10, "group_id": gid})
        past = (datetime.now() - timedelta(hours=2)).strftime("%Y-%m-%d %H:%M:%S")
        test_db.update_zone(zone["id"], {"state": "on", "watering_start_time": past})

        publishes = []

        def _capture_publish(server, topic, value, **kw):
            publishes.append((topic, value, kw))
            return True

        # Patch the lazy import inside _publish_master_close_bounded.
        import services.mqtt_pub as mqtt_pub

        # Bypass the is_connected pre-check by leaving _MQTT_CLIENTS empty
        # (so the helper falls through to publish_mqtt_value).
        with (
            patch.object(mqtt_pub, "publish_mqtt_value", side_effect=_capture_publish),
            patch.object(mqtt_pub, "_MQTT_CLIENTS", {}),
            patch.object(wd_mod, "SUPERVISOR_PUBLISH_TIMEOUT_SEC", 5.0),
        ):
            wd = ZoneWatchdog(test_db, MagicMock(), interval=1)
            wd._recover_stale_zones()

        assert publishes, "expected at least one master_close publish via supervisor after recovery"
        # NC mode → close value is '0'
        assert any(value == "0" for _, value, _ in publishes), f"NC master close payload must be '0'; got {publishes!r}"

    def test_recovery_no_master_close_when_observed_closed(self, test_db):
        """If observed=closed already, don't republish on recovery."""
        import services.mqtt_pub as mqtt_pub
        from services import watchdog as wd_mod
        from services.watchdog import ZoneWatchdog

        gid, _ = _make_master_group(test_db, observed="closed")
        zone = test_db.create_zone({"name": "S", "duration": 10, "group_id": gid})
        past = (datetime.now() - timedelta(hours=2)).strftime("%Y-%m-%d %H:%M:%S")
        test_db.update_zone(zone["id"], {"state": "on", "watering_start_time": past})

        publishes = []

        def _capture_publish(server, topic, value, **kw):
            publishes.append((topic, value))
            return True

        with (
            patch.object(mqtt_pub, "publish_mqtt_value", side_effect=_capture_publish),
            patch.object(mqtt_pub, "_MQTT_CLIENTS", {}),
            patch.object(wd_mod, "SUPERVISOR_PUBLISH_TIMEOUT_SEC", 5.0),
        ):
            wd = ZoneWatchdog(test_db, MagicMock(), interval=1)
            wd._recover_stale_zones()

        assert not publishes, f"no master close publish expected when observed=closed; got {publishes!r}"
