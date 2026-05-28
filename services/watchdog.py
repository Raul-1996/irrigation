"""Zone watchdog thread (TASK-010).

Background daemon thread that periodically checks for zones that have been ON
longer than the configured cap (default 240 minutes) and forcefully stops them.
Also monitors concurrent zone count per group and sends Telegram alerts on anomalies.
Master-valve supervisor (A7/A9, audits/2026-05-28-security/findings.md): on
startup recovers stale ``zones.state='on'`` rows left behind by SIGKILL/OOM;
each tick verifies that ``master_valve_observed='open'`` groups actually have
at least one ON zone, otherwise publishes a bounded-timeout master_close.
"""

import logging
import sqlite3
import threading
from datetime import datetime

from constants import (
    MAX_CONCURRENT_ZONES,
    WATCHDOG_INTERVAL_SEC,
    ZONE_CAP_DEFAULT_MIN,
)
from utils import normalize_topic

logger = logging.getLogger(__name__)

# Default zone cap in minutes (can be overridden via settings key 'zone_cap_minutes')
DEFAULT_ZONE_CAP_MINUTES = ZONE_CAP_DEFAULT_MIN

# A9: bounded publish path. paho-mqtt's blocking calls (publish + wait_for_publish)
# can stall ~45s on a dead broker. The supervisor tick MUST stay within the
# 5-sec cadence — so we pre-check is_connected() and skip publish if the
# client is offline (next tick retries). wait_for_publish timeout caps the
# fallback case.
SUPERVISOR_PUBLISH_TIMEOUT_SEC = 2.0


class ZoneWatchdog(threading.Thread):
    """Daemon thread that enforces zone time caps and monitors anomalies."""

    daemon = True

    def __init__(self, db, zone_control_module, interval: int = WATCHDOG_INTERVAL_SEC):
        """
        Args:
            db: Database instance (database.db).
            zone_control_module: Module with stop_zone(zone_id, reason, force) function.
            interval: Check interval in seconds.
        """
        super().__init__(name="ZoneWatchdog")
        self.db = db
        self.zone_control = zone_control_module
        self.interval = interval
        self._stop_event = threading.Event()

    def stop(self) -> None:
        """Signal the watchdog to stop."""
        self._stop_event.set()

    def run(self) -> None:
        logger.info("ZoneWatchdog started (interval=%ds)", self.interval)
        # A7: stale-zone recovery BEFORE the first supervisor tick. Without
        # this, SIGKILL/OOM leaves zones.state='on' in the DB → the
        # supervisor sees "active zones" and refuses to close the master
        # valve, leaving it open up to cap_minutes (default 4h).
        try:
            self._recover_stale_zones()
        except Exception:
            logger.exception("ZoneWatchdog: stale-zone recovery failed")
        # Initial delay to let the app fully start
        self._stop_event.wait(5)
        while not self._stop_event.is_set():
            try:
                self._check_zones()
                self._check_master_valves()
            except Exception as e:  # noqa: BLE001 — daemon thread must survive every tick
                # A7/A9: narrow tuples used to let KeyError/TypeError/
                # AttributeError kill the daemon thread; supervisor was off
                # until process restart. Broaden + logger.exception so we
                # see the traceback and keep cycling.
                logger.exception("Watchdog error: %s", e)
            self._stop_event.wait(self.interval)
        logger.info("ZoneWatchdog stopped")

    def _get_zone_cap_minutes(self) -> int:
        """Read zone cap from settings, fallback to default."""
        try:
            val = self.db.get_setting_value("zone_cap_minutes")
            if val is not None:
                return max(1, int(val))
        except (sqlite3.Error, OSError) as e:
            logger.debug("Handled exception in _get_zone_cap_minutes: %s", e)
        return DEFAULT_ZONE_CAP_MINUTES

    def _check_zones(self) -> None:
        """Main watchdog check: enforce time cap and monitor concurrency."""
        zones = self.db.get_zones() or []
        cap_minutes = self._get_zone_cap_minutes()
        now = datetime.now()

        on_zones = []
        for z in zones:
            if str(z.get("state") or "").lower() != "on":
                continue
            on_zones.append(z)
            # Check time cap
            start_str = z.get("watering_start_time")
            if not start_str:
                continue
            try:
                start_dt = datetime.strptime(start_str, "%Y-%m-%d %H:%M:%S")
            except (ValueError, TypeError, KeyError) as e:
                logger.debug("Exception in _check_zones: %s", e)
                continue
            elapsed_min = (now - start_dt).total_seconds() / 60.0
            if elapsed_min > cap_minutes:
                zone_id = int(z.get("id"))
                zone_name = z.get("name", f"Zone {zone_id}")
                logger.critical(
                    "WATCHDOG: Zone %d (%s) has been ON for %.0f min (cap=%d min). Force stopping!",
                    zone_id,
                    zone_name,
                    elapsed_min,
                    cap_minutes,
                )
                # Force stop the zone
                try:
                    self.zone_control.stop_zone(zone_id, reason="watchdog_cap", force=True)
                except (ConnectionError, TimeoutError, OSError, sqlite3.Error):
                    logger.exception("Watchdog: failed to stop zone %d", zone_id)
                # Send Telegram alert
                self._send_alert(
                    f"⚠️ WATCHDOG: Зона {zone_id} ({zone_name}) была включена {int(elapsed_min)} мин "
                    f"(лимит {cap_minutes} мин). Принудительно остановлена!"
                )
                # Log to DB
                try:
                    import json

                    self.db.add_log(
                        "watchdog_cap_stop",
                        json.dumps(
                            {
                                "zone_id": zone_id,
                                "zone_name": zone_name,
                                "elapsed_min": int(elapsed_min),
                                "cap_min": cap_minutes,
                            }
                        ),
                    )
                except (json.JSONDecodeError, KeyError, TypeError, ValueError) as e:
                    logger.debug("Handled exception in line_112: %s", e)

        # Check concurrent count
        if len(on_zones) > MAX_CONCURRENT_ZONES:
            logger.warning(
                "WATCHDOG: %d zones are ON simultaneously (threshold=%d)", len(on_zones), MAX_CONCURRENT_ZONES
            )
            self._send_alert(
                f"⚠️ WATCHDOG: {len(on_zones)} зон включены одновременно "
                f"(порог {MAX_CONCURRENT_ZONES}). Проверьте систему!"
            )

    def _recover_stale_zones(self) -> None:
        """A7: clean up zones.state='on'/'starting' left behind by SIGKILL/OOM.

        On a normal restart there should be no such rows — the scheduler's
        stop_zone() finalises state to 'off'. After SIGKILL the row is
        frozen in 'on' which makes the master-valve supervisor think
        watering is in progress and never close the valve. We treat a row
        as stale when ``watering_start_time + zone.duration < now()`` — i.e.
        the watering should already have ended even at the longest
        legitimate duration. Audit reason ``stale_on_recovery_after_restart``
        per spec; the per-group master close is best-effort (the periodic
        supervisor will publish the close anyway on its first tick).
        """
        try:
            zones = self.db.get_zones() or []
        except Exception:
            logger.exception("_recover_stale_zones: get_zones failed")
            return
        now = datetime.now()
        recovered_gids: set = set()
        for z in zones:
            state = str(z.get("state") or "").lower()
            if state not in ("on", "starting"):
                continue
            start_str = z.get("watering_start_time")
            if not start_str:
                continue
            try:
                start_dt = datetime.strptime(start_str, "%Y-%m-%d %H:%M:%S")
            except (ValueError, TypeError):
                continue
            try:
                duration_min = int(z.get("duration") or 0)
            except (ValueError, TypeError):
                duration_min = 0
            elapsed_sec = (now - start_dt).total_seconds()
            # Treat as stale only when the legitimate watering window has
            # provably closed (elapsed > duration in minutes). Zero/negative
            # duration → fall back to a 1-minute floor so we still recover
            # obviously-stuck rows but don't sweep healthy startups.
            limit_sec = max(60.0, float(duration_min) * 60.0)
            if elapsed_sec <= limit_sec:
                continue
            zone_id = int(z.get("id"))
            try:
                self.db.update_zone(
                    zone_id, {"state": "off", "watering_start_time": None}
                )
            except Exception:
                logger.exception(
                    "_recover_stale_zones: update_zone(off) failed zone_id=%s", zone_id
                )
                continue
            try:
                import json as _json

                self.db.add_log(
                    "stale_on_recovery_after_restart",
                    _json.dumps(
                        {
                            "zone_id": zone_id,
                            "prev_state": state,
                            "watering_start_time": start_str,
                            "duration_min": duration_min,
                            "elapsed_min": int(elapsed_sec / 60.0),
                        }
                    ),
                )
            except Exception:
                logger.exception(
                    "_recover_stale_zones: add_log failed zone_id=%s", zone_id
                )
            try:
                from services.audit import record_audit

                record_audit(
                    action_type="stale_on_recovery_after_restart",
                    source="watchdog",
                    target=f"zone:{zone_id}",
                    payload={
                        "prev_state": state,
                        "watering_start_time": start_str,
                        "duration_min": duration_min,
                        "elapsed_min": int(elapsed_sec / 60.0),
                    },
                    actor="system",
                )
            except Exception:
                logger.debug("_recover_stale_zones: record_audit best-effort failed", exc_info=True)
            try:
                gid = int(z.get("group_id") or 0)
                if gid and gid != 999:
                    recovered_gids.add(gid)
            except (ValueError, TypeError):
                pass
            logger.warning(
                "WATCHDOG stale_on_recovery: zone_id=%s prev_state=%s elapsed_min=%d duration_min=%d",
                zone_id,
                state,
                int(elapsed_sec / 60.0),
                duration_min,
            )
        # Best-effort: schedule a master-valve close (immediate) for every
        # group we just cleaned up — the periodic supervisor will retry on
        # its next tick if this attempt loses to a dead broker.
        if recovered_gids:
            try:
                groups = self.db.get_groups() or []
            except Exception:
                logger.exception("_recover_stale_zones: get_groups failed")
                groups = []
            for g in groups:
                try:
                    if int(g.get("id") or 0) not in recovered_gids:
                        continue
                    if int(g.get("use_master_valve") or 0) != 1:
                        continue
                    if str(g.get("master_valve_observed") or "").lower() != "open":
                        continue
                except (ValueError, TypeError):
                    continue
                try:
                    self._publish_master_close_bounded(g)
                except Exception:
                    logger.exception(
                        "_recover_stale_zones: bounded close failed gid=%s",
                        g.get("id"),
                    )

    def _check_master_valves(self) -> None:
        """Periodic supervisor: close master valves with no active zones.

        Each tick: for every group that has ``use_master_valve=1`` and
        ``master_valve_observed='open'``, look across ALL groups sharing the
        same master topic; if NONE of them have a zone in state 'on' /
        'starting', publish a bounded master_close. Bounded publish path
        (A9) keeps this tick within the 5-sec cadence even when the broker
        is dead — we pre-check is_connected() and skip publish if offline
        (next tick retries).
        """
        try:
            groups = self.db.get_groups() or []
        except Exception:
            logger.exception("_check_master_valves: get_groups failed")
            return
        # Group rows keyed by normalized master topic for cross-group ON check.
        topic_to_groups: dict[str, list] = {}
        for g in groups:
            try:
                if int(g.get("use_master_valve") or 0) != 1:
                    continue
            except (ValueError, TypeError):
                continue
            mtopic = (g.get("master_mqtt_topic") or "").strip()
            if not mtopic:
                continue
            try:
                t_norm = normalize_topic(mtopic)
            except Exception:
                t_norm = mtopic
            topic_to_groups.setdefault(t_norm, []).append(g)
        # Already-published topics (avoid double publish per tick when the
        # same topic is shared across multiple groups).
        published_topics: set = set()
        for t_norm, peer_groups in topic_to_groups.items():
            # Anyone observed-open on this topic?
            any_open = False
            for g in peer_groups:
                if str(g.get("master_valve_observed") or "").lower() == "open":
                    any_open = True
                    break
            if not any_open:
                continue
            # Anyone with active zones on this topic?
            any_active = False
            for g in peer_groups:
                try:
                    gid_peer = int(g.get("id") or 0)
                except (ValueError, TypeError):
                    continue
                try:
                    zones_peer = self.db.get_zones_by_group(gid_peer) or []
                except Exception:
                    logger.exception(
                        "_check_master_valves: get_zones_by_group(%s) failed", gid_peer
                    )
                    zones_peer = []
                for z in zones_peer:
                    if str(z.get("state") or "").lower() in ("on", "starting"):
                        any_active = True
                        break
                if any_active:
                    break
            if any_active:
                continue
            # Skip if zone_control has a pending close already armed for
            # this topic — let the existing timer fire. After A6 the dict
            # is cleaned up reliably, so this guard now works correctly.
            try:
                from services.zone_control import _PENDING_CLOSE_LOCK, _PENDING_CLOSE_TIMERS

                with _PENDING_CLOSE_LOCK:
                    if t_norm in _PENDING_CLOSE_TIMERS:
                        continue
            except Exception:
                logger.debug(
                    "_check_master_valves: pending-timers introspection failed",
                    exc_info=True,
                )
            if t_norm in published_topics:
                continue
            # Pick the first peer group as the "owner" of this publish for
            # logging/audit. Ops uses the warning to spot orphan-open
            # master valves regardless of which peer logs it.
            g_owner = peer_groups[0]
            try:
                logger.warning(
                    "WATCHDOG master_valve_supervisor: topic=%s observed=open with no active zones — publishing close",
                    t_norm,
                )
                ok = self._publish_master_close_bounded(g_owner)
                if ok:
                    published_topics.add(t_norm)
            except Exception:
                logger.exception(
                    "_check_master_valves: bounded close failed topic=%s", t_norm
                )

    def _publish_master_close_bounded(self, group_dict: dict) -> bool:
        """A9: bounded master-valve close publish.

        Returns True on confirmed publish, False on skip (offline broker)
        or failure. The bound is enforced two ways:

        1. Pre-check ``client.is_connected()`` — if the cached paho client
           is offline, skip publish entirely (next tick retries). This is
           the main bound: paho's publish() + wait_for_publish() can
           block ~45s when the broker is dead, and we MUST stay under the
           5-sec supervisor cadence.
        2. Hard ceiling on the publish call: spawn a worker thread and
           ``join(timeout=SUPERVISOR_PUBLISH_TIMEOUT_SEC)``. If the worker
           doesn't return in time we treat the publish as failed and
           move on — the worker will finish eventually on its own.
        """
        mtopic = (group_dict.get("master_mqtt_topic") or "").strip()
        msid = group_dict.get("master_mqtt_server_id")
        if not mtopic or msid is None:
            return False
        try:
            t_norm = normalize_topic(mtopic)
        except Exception:
            t_norm = mtopic
        try:
            mserver = self.db.get_mqtt_server(int(msid))
        except Exception:
            logger.exception(
                "_publish_master_close_bounded: get_mqtt_server(%s) failed", msid
            )
            return False
        if not mserver:
            logger.warning(
                "_publish_master_close_bounded: server msid=%s not found", msid
            )
            return False
        try:
            mode = (group_dict.get("master_mode") or "NC").strip().upper()
        except (ValueError, TypeError, AttributeError):
            mode = "NC"
        close_val = "1" if mode == "NO" else "0"
        # Pre-check the cached client. Without this, paho.publish() blocks
        # ~45s if the broker is dead → tick falls out of cadence.
        try:
            from services.mqtt_pub import _MQTT_CLIENTS

            try:
                sid_int = int(msid)
            except (ValueError, TypeError):
                sid_int = None
            cl = _MQTT_CLIENTS.get(sid_int) if sid_int is not None else None
            if cl is not None and hasattr(cl, "is_connected"):
                try:
                    if not cl.is_connected():
                        logger.info(
                            "WATCHDOG master_valve_supervisor: broker offline (sid=%s) — skip publish, next tick retries",
                            sid_int,
                        )
                        return False
                except Exception:
                    logger.debug(
                        "_publish_master_close_bounded: is_connected probe failed",
                        exc_info=True,
                    )
        except Exception:
            logger.debug(
                "_publish_master_close_bounded: pre-check failed",
                exc_info=True,
            )
        # Hard bound on the publish call itself — paho ack-wait inside
        # publish_mqtt_value can block when broker just-died between our
        # is_connected() check and publish().
        result: dict = {"ok": False}

        def _do_publish():
            try:
                from services.mqtt_pub import publish_mqtt_value

                result["ok"] = bool(
                    publish_mqtt_value(
                        mserver,
                        t_norm,
                        close_val,
                        min_interval_sec=0.0,
                        qos=2,
                        retain=True,
                        meta={"cmd": "master_off", "src": "watchdog"},
                    )
                )
            except Exception:
                logger.exception(
                    "_publish_master_close_bounded: publish raised topic=%s",
                    t_norm,
                )

        worker = threading.Thread(
            target=_do_publish, name=f"mv-supervisor-pub-{t_norm}", daemon=True
        )
        worker.start()
        worker.join(SUPERVISOR_PUBLISH_TIMEOUT_SEC)
        if worker.is_alive():
            logger.warning(
                "WATCHDOG master_valve_supervisor: publish exceeded %.1fs (topic=%s) — leaving worker, next tick retries",
                SUPERVISOR_PUBLISH_TIMEOUT_SEC,
                t_norm,
            )
            return False
        if not result["ok"]:
            return False
        # Mirror zone_control._do_close: only write observed=closed when
        # publish confirmed. SSE-hub will heal anyway if the relay echo
        # arrives later.
        try:
            gid = int(group_dict.get("id") or 0)
            if gid:
                self.db.update_group_fields(int(gid), {"master_valve_observed": "closed"})
                try:
                    import json as _json_sb

                    from services import sse_hub as _sse_hub_sb

                    _sse_hub_sb.broadcast(
                        _json_sb.dumps({"mv_group_id": int(gid), "mv_state": "closed"})
                    )
                except Exception:
                    logger.debug(
                        "_publish_master_close_bounded: SSE broadcast failed",
                        exc_info=True,
                    )
        except Exception:
            logger.exception(
                "_publish_master_close_bounded: group fields update failed gid=%s",
                group_dict.get("id"),
            )
        try:
            from services.audit import record_audit

            record_audit(
                action_type="master_valve_supervisor_close",
                source="watchdog",
                target=f"group:{int(group_dict.get('id') or 0)}",
                payload={"topic": t_norm, "value": close_val, "mode": mode},
                actor="system",
            )
        except Exception:
            logger.debug(
                "_publish_master_close_bounded: record_audit best-effort failed",
                exc_info=True,
            )
        return True

    def _send_alert(self, message: str) -> None:
        """Send alert via Telegram to admin chat (best-effort)."""
        try:
            admin_chat = self.db.get_setting_value("telegram_admin_chat_id")
            if not admin_chat:
                return
            from services.telegram_bot import notifier

            if notifier:
                notifier.send_message(int(admin_chat), message)
        except ImportError:
            logger.exception("Watchdog: Telegram alert failed")


# Module-level reference (set by start_watchdog)
_watchdog_instance = None
_watchdog_lock = threading.Lock()


def start_watchdog(db, zone_control_module, interval: int = WATCHDOG_INTERVAL_SEC) -> ZoneWatchdog:
    """Start the watchdog singleton. Idempotent — only starts once."""
    global _watchdog_instance
    with _watchdog_lock:
        if _watchdog_instance is not None and _watchdog_instance.is_alive():
            return _watchdog_instance
        wd = ZoneWatchdog(db, zone_control_module, interval=interval)
        wd.start()
        _watchdog_instance = wd
        return wd
