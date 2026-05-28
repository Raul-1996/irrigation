"""Zone watchdog thread (TASK-010).

Background daemon thread that periodically checks for zones that have been ON
longer than the configured cap (default 240 minutes) and forcefully stops them.
Also monitors concurrent zone count per group and sends Telegram alerts on anomalies.

Issue #51: also runs a master-valve consistency supervisor — see
``_check_master_valves``. The supervisor force-closes any master valve that is
observed='open' while no zones under that master topic are on/starting and no
pending close timer is armed. This catches partial-failure paths in
program_runner where the legitimate close was never scheduled.
"""

import contextlib
import json
import logging
import sqlite3
import threading
from datetime import datetime

from constants import (
    MAX_CONCURRENT_ZONES,
    WATCHDOG_INTERVAL_SEC,
    ZONE_CAP_DEFAULT_MIN,
)

logger = logging.getLogger(__name__)

# Default zone cap in minutes (can be overridden via settings key 'zone_cap_minutes')
DEFAULT_ZONE_CAP_MINUTES = ZONE_CAP_DEFAULT_MIN


class ZoneWatchdog(threading.Thread):
    """Daemon thread that enforces zone time caps and monitors anomalies."""

    daemon = True

    def __init__(self, db, zone_control_module, interval: int = WATCHDOG_INTERVAL_SEC):
        """
        Args:
            db: Database instance (database.db).
            zone_control_module: Module with stop_zone(zone_id, reason, force) function.
            interval: Check interval in seconds for the zone-cap check.
        """
        super().__init__(name="ZoneWatchdog")
        self.db = db
        self.zone_control = zone_control_module
        self.interval = interval
        # Issue #51: master-valve supervisor runs on a faster cadence than the
        # zone-cap sweep — orphan master valves must be caught within seconds
        # of program completion, while the cap check (240 min ceiling) is fine
        # at the default 30s.
        self.master_valve_interval = 5
        self._stop_event = threading.Event()

    def stop(self) -> None:
        """Signal the watchdog to stop."""
        self._stop_event.set()

    def run(self) -> None:
        logger.info(
            "ZoneWatchdog started (cap_interval=%ds, master_valve_interval=%ds)",
            self.interval,
            self.master_valve_interval,
        )
        # Initial delay to let the app fully start
        self._stop_event.wait(5)
        # Throttle the zone-cap sweep relative to the master-valve supervisor
        # tick (which is the loop cadence). We run cap-check once every
        # ceil(self.interval / self.master_valve_interval) ticks.
        ticks_per_cap_check = max(1, self.interval // max(1, self.master_valve_interval))
        tick = 0
        while not self._stop_event.is_set():
            # Master-valve supervisor (Issue #51) — runs every tick.
            try:
                self._check_master_valves()
            except (
                ConnectionError,
                TimeoutError,
                OSError,
                sqlite3.Error,
                ValueError,
                RuntimeError,
            ) as e:
                logger.exception("Watchdog master-valve check error: %s", e)
            # Zone-cap sweep — runs at the slower self.interval cadence.
            if tick % ticks_per_cap_check == 0:
                try:
                    self._check_zones()
                except (
                    ConnectionError,
                    TimeoutError,
                    OSError,
                    sqlite3.Error,
                    ValueError,
                    RuntimeError,
                ) as e:  # catch-all: intentional
                    logger.exception("Watchdog error: %s", e)
            tick += 1
            self._stop_event.wait(self.master_valve_interval)
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

    def _check_master_valves(self) -> None:
        """Issue #51 supervisor: force-close orphan master valves.

        Orphan = a group with ``use_master_valve=1`` AND
        ``master_valve_observed='open'`` AND no zone in ``on``/``starting``
        under any group sharing the same master topic AND no pending close
        timer armed in ``services.zone_control._PENDING_CLOSE_TIMERS``.

        This is a safety net for failure modes where ``stop_zone`` was never
        called (e.g., exception in ``_run_program_threaded`` after
        ``exclusive_start_zone`` opened the master valve but before the
        finalisation block scheduled the close). In the happy path the close
        is scheduled with ``MASTER_VALVE_CLOSE_DELAY_SEC`` delay and the
        timer's presence prevents the supervisor from interfering — see
        ``_PENDING_CLOSE_TIMERS`` check.
        """
        try:
            from utils import normalize_topic
        except ImportError as e:
            logger.debug("watchdog master-valve: utils.normalize_topic unavailable: %s", e)
            return
        try:
            groups = self.db.get_groups() or []
        except (sqlite3.Error, OSError) as e:
            logger.debug("watchdog master-valve: get_groups failed: %s", e)
            return
        # Cache zones-by-master-topic so we don't re-query DB for each group
        # that shares the same topic.
        # topic_norm -> bool (True = any zone on/starting)
        active_by_topic: dict[str, bool] = {}

        for g in groups:
            try:
                if int(g.get("use_master_valve") or 0) != 1:
                    continue
            except (ValueError, TypeError):
                continue
            observed = str(g.get("master_valve_observed") or "").lower()
            if observed != "open":
                continue
            mtopic = (g.get("master_mqtt_topic") or "").strip()
            msid = g.get("master_mqtt_server_id")
            if not mtopic or not msid:
                continue
            try:
                t_norm = normalize_topic(mtopic)
            except (ValueError, TypeError, OSError):
                t_norm = mtopic
            try:
                gid = int(g.get("id") or 0)
            except (ValueError, TypeError):
                continue
            if not gid:
                continue

            # Skip if a legitimate close timer is already in flight.
            try:
                with self.zone_control._PENDING_CLOSE_LOCK:
                    if t_norm in self.zone_control._PENDING_CLOSE_TIMERS:
                        continue
            except AttributeError:
                # Module shape changed — fail safe by not touching the valve.
                logger.debug("watchdog master-valve: _PENDING_CLOSE_TIMERS not available")
                return

            # Any zone under a group sharing this master topic active?
            if t_norm in active_by_topic:
                any_active = active_by_topic[t_norm]
            else:
                any_active = False
                for gg in groups:
                    try:
                        gg_topic = (gg.get("master_mqtt_topic") or "").strip()
                        if not gg_topic:
                            continue
                        if normalize_topic(gg_topic) != t_norm:
                            continue
                    except (ValueError, TypeError, OSError):
                        continue
                    try:
                        zones_in_group = self.db.get_zones_by_group(int(gg.get("id"))) or []
                    except (sqlite3.Error, OSError) as e:
                        logger.debug("watchdog master-valve: get_zones_by_group failed: %s", e)
                        continue
                    for z2 in zones_in_group:
                        st = str(z2.get("state") or "").lower()
                        if st in ("on", "starting"):
                            any_active = True
                            break
                    if any_active:
                        break
                active_by_topic[t_norm] = any_active

            if any_active:
                continue

            # Orphan detected — force-close synchronously (mirrors
            # emergency_stop_all Phase C: one direct publish, no Timer).
            try:
                mode = (g.get("master_mode") or "NC").strip().upper()
            except (ValueError, TypeError, AttributeError):
                mode = "NC"
            close_val = "1" if mode == "NO" else "0"
            try:
                mserver = self.db.get_mqtt_server(int(msid))
            except (sqlite3.Error, OSError, ValueError, TypeError) as e:
                logger.exception(
                    "watchdog master-valve: get_mqtt_server(%s) failed: %s", msid, e
                )
                continue
            if not mserver:
                logger.warning(
                    "watchdog master-valve: group=%s msid=%s — server not found", gid, msid
                )
                continue
            logger.critical(
                "WATCHDOG: master valve orphan detected — group=%s topic=%s "
                "observed=open, no active zones, no pending timer. Force-closing.",
                gid,
                t_norm,
            )
            try:
                ok = self.zone_control.publish_mqtt_value(
                    mserver,
                    t_norm,
                    close_val,
                    min_interval_sec=0.0,
                    qos=2,
                    retain=True,
                    meta={"cmd": "master_off", "src": "watchdog_supervisor"},
                )
            except (ConnectionError, TimeoutError, OSError, RuntimeError) as e:
                logger.exception(
                    "watchdog master-valve: publish failed group=%s topic=%s: %s", gid, t_norm, e
                )
                continue
            if not ok:
                # Issue #38 contract: do NOT lie about observed state if publish
                # didn't confirm. Leave observed='open' so the next supervisor
                # tick will retry.
                logger.warning(
                    "watchdog master-valve: publish returned False — leaving "
                    "observed unchanged group=%s topic=%s",
                    gid,
                    t_norm,
                )
                continue
            # Successful publish — flip observed to 'closed' + SSE + audit.
            try:
                self.db.update_group_fields(int(gid), {"master_valve_observed": "closed"})
            except (sqlite3.Error, OSError, ValueError, TypeError) as e:
                logger.debug(
                    "watchdog master-valve: update observed=closed failed gid=%s: %s", gid, e
                )
            try:
                from services import sse_hub as _sse_hub

                _sse_hub.broadcast(json.dumps({"mv_group_id": int(gid), "mv_state": "closed"}))
            except (ImportError, ValueError, TypeError) as e:
                logger.debug("watchdog master-valve: SSE broadcast failed: %s", e)
            try:
                self.db.add_log(
                    "watchdog_master_close",
                    json.dumps(
                        {
                            "group_id": int(gid),
                            "topic": t_norm,
                            "mode": mode,
                            "reason": "orphan_master_valve",
                        }
                    ),
                )
            except (sqlite3.Error, OSError, json.JSONDecodeError, TypeError, ValueError) as e:
                logger.debug("watchdog master-valve: add_log failed: %s", e)
            try:
                from services.audit import record_audit

                record_audit(
                    action_type="watchdog_master_close",
                    source="watchdog",
                    target=f"group:{int(gid)}",
                    payload={
                        "topic": t_norm,
                        "value": close_val,
                        "mode": mode,
                        "reason": "orphan_master_valve",
                    },
                    actor="system",
                )
            except (ImportError, sqlite3.Error, OSError) as e:
                logger.debug("watchdog master-valve: record_audit failed: %s", e)
            # Mark this topic as 'handled' for the rest of this tick so we
            # don't re-publish if another group shares the same topic.
            active_by_topic[t_norm] = True
            # Best-effort Telegram alert.
            with contextlib.suppress(ImportError, ValueError, TypeError, OSError):
                self._send_alert(
                    f"⚠️ WATCHDOG: master valve группы {gid} был открыт без активных зон. "
                    "Принудительно закрыт."
                )

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
