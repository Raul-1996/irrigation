"""Zone watchdog thread (TASK-010).

Background daemon thread that periodically checks for zones that have been ON
longer than the configured cap (default 240 minutes) and forcefully stops them.
Also monitors concurrent zone count per group and sends Telegram alerts on anomalies.
"""

import threading
import time
import logging
from datetime import datetime

logger = logging.getLogger(__name__)

# Default zone cap in minutes (can be overridden via settings key 'zone_cap_minutes')
DEFAULT_ZONE_CAP_MINUTES = 240
# Maximum reasonable concurrent ON zones across all groups
MAX_CONCURRENT_ZONES = 10


class ZoneWatchdog(threading.Thread):
    """Daemon thread that enforces zone time caps and monitors anomalies."""

    daemon = True

    def __init__(self, db, zone_control_module, interval: int = 30):
        """
        Args:
            db: Database instance (database.db).
            zone_control_module: Module with stop_zone(zone_id, reason, force) function.
            interval: Check interval in seconds.
        """
        super().__init__(name='ZoneWatchdog')
        self.db = db
        self.zone_control = zone_control_module
        self.interval = interval
        self._stop_event = threading.Event()

    def stop(self) -> None:
        """Signal the watchdog to stop."""
        self._stop_event.set()

    def run(self) -> None:
        logger.info("ZoneWatchdog started (interval=%ds)", self.interval)
        # Initial delay to let the app fully start
        self._stop_event.wait(5)
        while not self._stop_event.is_set():
            try:
                self._check_zones()
            except Exception as e:
                logger.exception("Watchdog error: %s", e)
            self._stop_event.wait(self.interval)
        logger.info("ZoneWatchdog stopped")

    def _get_zone_cap_minutes(self) -> int:
        """Read zone cap from settings, fallback to default."""
        try:
            val = self.db.get_setting_value('zone_cap_minutes')
            if val is not None:
                return max(1, int(val))
        except Exception as e:
            logger.debug("Handled exception in _get_zone_cap_minutes: %s", e)
        return DEFAULT_ZONE_CAP_MINUTES

    def _check_zones(self) -> None:
        """Main watchdog check: enforce time cap and monitor concurrency."""
        zones = self.db.get_zones() or []
        cap_minutes = self._get_zone_cap_minutes()
        now = datetime.now()

        on_zones = []
        for z in zones:
            if str(z.get('state') or '').lower() != 'on':
                continue
            on_zones.append(z)
            # Check time cap
            start_str = z.get('watering_start_time')
            if not start_str:
                continue
            try:
                start_dt = datetime.strptime(start_str, '%Y-%m-%d %H:%M:%S')
            except Exception as e:
                logger.debug("Exception in _check_zones: %s", e)
                continue
            elapsed_min = (now - start_dt).total_seconds() / 60.0
            if elapsed_min > cap_minutes:
                zone_id = int(z.get('id'))
                zone_name = z.get('name', f'Zone {zone_id}')
                logger.critical(
                    "WATCHDOG: Zone %d (%s) has been ON for %.0f min (cap=%d min). Force stopping!",
                    zone_id, zone_name, elapsed_min, cap_minutes
                )
                # Force stop the zone
                try:
                    self.zone_control.stop_zone(zone_id, reason='watchdog_cap', force=True)
                except Exception:
                    logger.exception("Watchdog: failed to stop zone %d", zone_id)
                # Send Telegram alert
                self._send_alert(
                    f"⚠️ WATCHDOG: Зона {zone_id} ({zone_name}) была включена {int(elapsed_min)} мин "
                    f"(лимит {cap_minutes} мин). Принудительно остановлена!"
                )
                # Log to DB
                try:
                    import json
                    self.db.add_log('watchdog_cap_stop', json.dumps({
                        'zone_id': zone_id,
                        'zone_name': zone_name,
                        'elapsed_min': int(elapsed_min),
                        'cap_min': cap_minutes
                    }))
                except Exception as e:
                    logger.debug("Handled exception in line_112: %s", e)

        # Check concurrent count
        if len(on_zones) > MAX_CONCURRENT_ZONES:
            logger.warning(
                "WATCHDOG: %d zones are ON simultaneously (threshold=%d)",
                len(on_zones), MAX_CONCURRENT_ZONES
            )
            self._send_alert(
                f"⚠️ WATCHDOG: {len(on_zones)} зон включены одновременно "
                f"(порог {MAX_CONCURRENT_ZONES}). Проверьте систему!"
            )

    def _send_alert(self, message: str) -> None:
        """Send alert via Telegram to admin chat (best-effort)."""
        try:
            admin_chat = self.db.get_setting_value('telegram_admin_chat_id')
            if not admin_chat:
                return
            from services.telegram_bot import notifier
            if notifier:
                notifier.send_message(int(admin_chat), message)
        except Exception:
            logger.exception("Watchdog: Telegram alert failed")


# Module-level reference (set by start_watchdog)
_watchdog_instance = None
_watchdog_lock = threading.Lock()


def start_watchdog(db, zone_control_module, interval: int = 30) -> ZoneWatchdog:
    """Start the watchdog singleton. Idempotent — only starts once."""
    global _watchdog_instance
    with _watchdog_lock:
        if _watchdog_instance is not None and _watchdog_instance.is_alive():
            return _watchdog_instance
        wd = ZoneWatchdog(db, zone_control_module, interval=interval)
        wd.start()
        _watchdog_instance = wd
        return wd
