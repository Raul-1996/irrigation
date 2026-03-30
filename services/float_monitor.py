"""FloatMonitor — per-group tank float valve monitoring via MQTT.

Spec: program-queue-spec.md §3 (Tank Float Valve)
Key principles:
  K3: FloatMonitor does NOT call exclusive_start_zone. Only sets events.
  K4: remaining_seconds persisted in DB (zones.pause_remaining_seconds)
  K6: Signals queue manager about pause (for excluded_wait_seconds)
  S5: wb-rules tripped lifecycle
  S6: Hysteresis — min_run_time=60s, 3 trips in 5min → emergency stop
"""

import logging
import sqlite3
import threading
import time
from datetime import datetime
from typing import Dict, Optional, List, Any

logger = logging.getLogger(__name__)

# Hysteresis constants (S6)
FLOAT_MIN_RUN_TIME = 60      # seconds — min run time after resume before re-pause
FLOAT_MAX_TRIPS = 3           # max trips within window
FLOAT_TRIP_WINDOW = 300       # seconds (5 min)


class _GroupState:
    """Internal per-group float state."""

    def __init__(self, group_id):
        # type: (int) -> None
        self.group_id = group_id
        self.level_ok = True          # True = water present
        self.paused = False
        self.paused_since = None      # type: Optional[str]  # ISO datetime
        self.paused_since_mono = None  # type: Optional[float]  # monotonic
        self.timeout_at = None        # type: Optional[float]  # monotonic
        self.timeout_minutes = 30
        self.paused_zones = []        # type: List[int]
        self.resume_event = threading.Event()
        self.emergency_stopped = False

        # Debounce
        self.debounce_seconds = 5
        self.pending_level_ok = None   # type: Optional[bool]
        self.pending_since = None      # type: Optional[float]  # monotonic
        self.confirmed_level_ok = True  # last confirmed (debounced) value

        # Hysteresis
        self.trip_times = []           # type: List[float]  # monotonic timestamps
        self.last_resume_at = None     # type: Optional[float]


class FloatMonitor:
    """Monitors tank float valves per-group via MQTT."""

    def __init__(self, db_path, mqtt_clients, queue_manager, telegram_notify=None):
        # type: (str, Dict[int, Any], Any, Optional[Any]) -> None
        self.db_path = db_path
        self.mqtt_clients = mqtt_clients or {}
        self.queue_manager = queue_manager
        self.telegram_notify = telegram_notify

        self._lock = threading.Lock()
        self._states = {}              # type: Dict[int, _GroupState]
        self._subscriptions = {}       # type: Dict[int, dict]  # group_id -> {topic, server_id, tripped_topic}
        self._topic_to_group = {}      # type: Dict[str, int]   # mqtt topic -> group_id
        self._started = False
        self._original_on_message = {}  # type: Dict[int, Any]  # server_id -> original callback

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def start(self):
        """Subscribe to MQTT topics for all float_enabled groups."""
        with self._lock:
            self._started = True
        self._load_all_groups()

    def stop(self):
        """Unsubscribe from all float MQTT topics."""
        with self._lock:
            self._started = False
            for group_id in list(self._subscriptions.keys()):
                self._unsubscribe_group(group_id)
            self._subscriptions.clear()
            self._topic_to_group.clear()
            self._states.clear()

    def reload_group(self, group_id):
        """Reload MQTT subscription for a single group (e.g. after config change)."""
        with self._lock:
            # Unsubscribe old
            if group_id in self._subscriptions:
                self._unsubscribe_group(group_id)
            # Load new config from DB
            self._load_group(group_id)

    def get_state(self, group_id):
        # type: (int) -> dict
        """Get current float state for a group (spec §6.3)."""
        with self._lock:
            gs = self._states.get(group_id)
            if gs is None:
                return {
                    'group_id': group_id,
                    'level_ok': True,
                    'paused': False,
                    'paused_since': None,
                    'timeout_at': None,
                    'paused_zones': [],
                    'hysteresis': {
                        'trip_count': 0,
                        'emergency_stopped': False,
                    },
                }
            timeout_at_str = None
            if gs.timeout_at is not None:
                try:
                    remaining_timeout = gs.timeout_at - time.monotonic()
                    if remaining_timeout > 0:
                        timeout_at_str = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
                except Exception:
                    pass
            return {
                'group_id': group_id,
                'level_ok': gs.level_ok,
                'paused': gs.paused,
                'paused_since': gs.paused_since,
                'timeout_at': timeout_at_str,
                'paused_zones': list(gs.paused_zones),
                'hysteresis': {
                    'trip_count': len(gs.trip_times),
                    'emergency_stopped': gs.emergency_stopped,
                },
            }

    def get_all_states(self):
        # type: () -> Dict[int, dict]
        """Get float states for all monitored groups."""
        with self._lock:
            group_ids = list(self._states.keys())
        return {gid: self.get_state(gid) for gid in group_ids}

    def is_paused(self, group_id):
        # type: (int) -> bool
        """Check if group is currently paused due to float."""
        with self._lock:
            gs = self._states.get(group_id)
            if gs is None:
                return False
            return gs.paused

    def get_resume_event(self, group_id):
        # type: (int) -> threading.Event
        """Get the resume event for a group (worker waits on this)."""
        with self._lock:
            gs = self._states.get(group_id)
            if gs is None:
                gs = _GroupState(group_id)
                self._states[group_id] = gs
            return gs.resume_event

    def wait_for_resume_or_cancel(self, group_id, cancel_event, shutdown_event, timeout=1800):
        # type: (int, Optional[threading.Event], Optional[threading.Event], int) -> str
        """Wait for resume, cancel, shutdown, or timeout.

        Returns: 'resumed' | 'cancelled' | 'shutdown' | 'timeout'
        """
        resume_event = self.get_resume_event(group_id)
        deadline = time.monotonic() + timeout

        while True:
            if resume_event.is_set():
                return 'resumed'
            if cancel_event and cancel_event.is_set():
                return 'cancelled'
            if shutdown_event and shutdown_event.is_set():
                return 'shutdown'
            if time.monotonic() >= deadline:
                return 'timeout'
            # Wait with short timeout for responsive checking
            resume_event.wait(timeout=1.0)

    # ------------------------------------------------------------------
    # MQTT message handling
    # ------------------------------------------------------------------

    def _on_float_message(self, group_id, payload):
        # type: (int, str) -> None
        """Handle incoming MQTT message for a group's float sensor."""
        payload = str(payload).strip().lower()

        # Parse payload to raw boolean
        if payload in ('1', 'true', 'on'):
            raw_val = True
        elif payload in ('0', 'false', 'off'):
            raw_val = False
        else:
            # Invalid payload — ignore
            logger.debug("FloatMonitor: invalid payload '%s' for group %d", payload, group_id)
            return

        with self._lock:
            gs = self._states.get(group_id)
            if gs is None:
                return

            # Apply NO/NC mode
            group_cfg = self._subscriptions.get(group_id, {})
            mode = group_cfg.get('float_mode', 'NO')
            if mode == 'NC':
                level_ok = not raw_val
            else:
                level_ok = raw_val

            # Debounce logic
            debounce_sec = gs.debounce_seconds
            now = time.monotonic()

            if debounce_sec <= 0:
                # No debounce — apply immediately
                self._apply_level(gs, level_ok, now)
                return

            # With debounce: track pending state
            if gs.pending_level_ok is None or gs.pending_level_ok != level_ok:
                # New signal direction — start debounce timer
                gs.pending_level_ok = level_ok
                gs.pending_since = now
                return
            else:
                # Same signal direction — check if debounce period elapsed
                if gs.pending_since is not None and (now - gs.pending_since) >= debounce_sec:
                    # Stable for debounce period — apply
                    self._apply_level(gs, level_ok, now)
                    gs.pending_level_ok = None
                    gs.pending_since = None
                    return
                # Not yet stable — keep waiting
                return

    def _apply_level(self, gs, level_ok, now):
        # type: (_GroupState, bool, float) -> None
        """Apply confirmed level change (called with lock held)."""
        gs.level_ok = level_ok

        if not level_ok:
            self._on_level_low(gs, now)
        else:
            self._on_level_restored(gs, now)

    def _on_level_low(self, gs, now):
        # type: (_GroupState, float) -> None
        """Handle confirmed low water level (called with lock held)."""
        if gs.emergency_stopped:
            return

        # Hysteresis: record trip
        gs.trip_times.append(now)
        # Prune old trips outside window
        cutoff = now - FLOAT_TRIP_WINDOW
        gs.trip_times = [t for t in gs.trip_times if t >= cutoff]

        # Check max trips → emergency stop
        if len(gs.trip_times) >= FLOAT_MAX_TRIPS:
            gs.emergency_stopped = True
            gs.paused = True
            gs.paused_since = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
            gs.paused_since_mono = now
            gs.resume_event.clear()

            group_name = self._subscriptions.get(gs.group_id, {}).get('name', 'Группа %d' % gs.group_id)
            trip_count = len(gs.trip_times)

            # Release lock for external calls
            self._lock.release()
            try:
                try:
                    self.queue_manager.cancel_group(gs.group_id)
                except Exception:
                    logger.exception("FloatMonitor: cancel_group failed for group %d", gs.group_id)
                if self.telegram_notify:
                    try:
                        self.telegram_notify(
                            "🚨 АВАРИЙНЫЙ СТОП: Группа %s — поплавок нестабилен "
                            "(%d срабатываний за 5 мин). Проверьте датчик и ёмкость!" % (group_name, trip_count)
                        )
                    except Exception:
                        logger.exception("FloatMonitor: telegram_notify failed")
            finally:
                self._lock.acquire()
            return

        # Check hysteresis min_run_time warning
        if gs.last_resume_at is not None and (now - gs.last_resume_at) < FLOAT_MIN_RUN_TIME:
            logger.warning(
                "FloatMonitor: float_pause_too_soon for group %d "
                "(%.0fs since last resume, min_run_time=%ds)",
                gs.group_id, now - gs.last_resume_at, FLOAT_MIN_RUN_TIME
            )

        # Set pause state
        gs.paused = True
        gs.paused_since = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        gs.paused_since_mono = now
        gs.timeout_at = now + gs.timeout_minutes * 60
        gs.resume_event.clear()

        # Pause active zones in DB (K4)
        paused_zones = self._pause_active_zones_in_db(gs.group_id)
        gs.paused_zones = paused_zones

        # Log float event
        self._log_float_event(gs.group_id, 'float_pause', paused_zones)

        # Telegram notification (release lock)
        if self.telegram_notify:
            group_name = self._subscriptions.get(gs.group_id, {}).get('name', 'Группа %d' % gs.group_id)
            self._lock.release()
            try:
                try:
                    self.telegram_notify(
                        "⚠️ Группа %s: уровень воды низкий, полив приостановлен" % group_name
                    )
                except Exception:
                    logger.exception("FloatMonitor: telegram_notify failed")
            finally:
                self._lock.acquire()

    def _on_level_restored(self, gs, now):
        # type: (_GroupState, float) -> None
        """Handle confirmed water level restored (called with lock held)."""
        if gs.emergency_stopped:
            return

        if not gs.paused:
            # Not paused — nothing to resume
            return

        # Record resume time for hysteresis
        gs.last_resume_at = now

        # Clear pause state
        gs.paused = False
        gs.timeout_at = None
        gs.resume_event.set()

        # Log float event
        self._log_float_event(gs.group_id, 'float_resume', gs.paused_zones)
        gs.paused_zones = []

        # Telegram notification
        if self.telegram_notify:
            group_name = self._subscriptions.get(gs.group_id, {}).get('name', 'Группа %d' % gs.group_id)
            self._lock.release()
            try:
                try:
                    self.telegram_notify(
                        "✅ Группа %s: уровень восстановлен, полив возобновлён" % group_name
                    )
                except Exception:
                    logger.exception("FloatMonitor: telegram_notify failed")
            finally:
                self._lock.acquire()

    # ------------------------------------------------------------------
    # Timeout checking
    # ------------------------------------------------------------------

    def _check_timeouts(self):
        """Check all groups for float timeout → emergency stop."""
        now = time.monotonic()
        timed_out = []

        with self._lock:
            for group_id, gs in self._states.items():
                if gs.paused and gs.timeout_at is not None and now >= gs.timeout_at:
                    gs.timeout_at = None  # prevent re-trigger
                    gs.emergency_stopped = True
                    timed_out.append(group_id)

        # Process timeouts outside lock
        for group_id in timed_out:
            group_name = self._subscriptions.get(group_id, {}).get('name', 'Группа %d' % group_id)
            timeout_min = self._subscriptions.get(group_id, {}).get('float_timeout_minutes', 30)

            try:
                self.queue_manager.cancel_group(group_id)
            except Exception:
                logger.exception("FloatMonitor: cancel_group failed for group %d", group_id)

            self._log_float_event(group_id, 'float_timeout_emergency_stop', [])

            if self.telegram_notify:
                try:
                    self.telegram_notify(
                        "🚨 АВАРИЙНЫЙ СТОП: Группа %s — уровень воды не восстановился "
                        "за %d мин. Все программы группы отменены. "
                        "Проверьте ёмкость и насос!" % (group_name, timeout_min)
                    )
                except Exception:
                    logger.exception("FloatMonitor: telegram_notify failed")

    # ------------------------------------------------------------------
    # MQTT reconnect handler
    # ------------------------------------------------------------------

    def _on_mqtt_connect(self, client, userdata, flags, rc):
        """Re-subscribe to all topics on MQTT reconnect."""
        with self._lock:
            for topic in self._topic_to_group:
                try:
                    client.subscribe(topic)
                except Exception:
                    logger.exception("FloatMonitor: resubscribe failed for %s", topic)

    # ------------------------------------------------------------------
    # DB operations
    # ------------------------------------------------------------------

    def _get_db(self):
        conn = sqlite3.connect(self.db_path)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=30000")
        conn.row_factory = sqlite3.Row
        return conn

    def _pause_active_zones_in_db(self, group_id):
        # type: (int) -> List[int]
        """Mark active zones as paused in DB, return list of paused zone IDs."""
        paused = []
        try:
            conn = self._get_db()
            try:
                rows = conn.execute(
                    "SELECT id, duration FROM zones WHERE group_id=? AND state='on'",
                    (group_id,)
                ).fetchall()
                for row in rows:
                    zone_id = row['id']
                    duration = row['duration'] or 0
                    conn.execute(
                        "UPDATE zones SET state='paused', pause_reason='float', "
                        "pause_remaining_seconds=? WHERE id=?",
                        (duration, zone_id)
                    )
                    paused.append(zone_id)
                conn.commit()
            finally:
                conn.close()
        except Exception:
            logger.exception("FloatMonitor: _pause_active_zones_in_db failed for group %d", group_id)
        return paused

    def _log_float_event(self, group_id, event_type, paused_zones):
        # type: (int, str, List[int]) -> None
        """Log float event to DB."""
        try:
            conn = self._get_db()
            try:
                conn.execute(
                    "INSERT INTO float_events (group_id, event_type, paused_zones) VALUES (?, ?, ?)",
                    (group_id, event_type, str(paused_zones))
                )
                conn.commit()
            finally:
                conn.close()
        except Exception:
            logger.exception("FloatMonitor: _log_float_event failed")

    # ------------------------------------------------------------------
    # Group loading & MQTT subscription
    # ------------------------------------------------------------------

    def _load_all_groups(self):
        """Load all float-enabled groups from DB and subscribe."""
        try:
            conn = self._get_db()
            try:
                rows = conn.execute(
                    "SELECT id, name, float_enabled, float_mqtt_topic, float_mqtt_server_id, "
                    "float_mode, float_timeout_minutes, float_debounce_seconds "
                    "FROM groups WHERE float_enabled=1"
                ).fetchall()
            finally:
                conn.close()

            with self._lock:
                for row in rows:
                    self._subscribe_group(dict(row))
        except Exception:
            logger.exception("FloatMonitor: _load_all_groups failed")

    def _load_group(self, group_id):
        # type: (int) -> None
        """Load a single group from DB and subscribe (called with lock held)."""
        try:
            conn = self._get_db()
            try:
                row = conn.execute(
                    "SELECT id, name, float_enabled, float_mqtt_topic, float_mqtt_server_id, "
                    "float_mode, float_timeout_minutes, float_debounce_seconds "
                    "FROM groups WHERE id=?",
                    (group_id,)
                ).fetchone()
            finally:
                conn.close()

            if row and row['float_enabled']:
                self._subscribe_group(dict(row))
        except Exception:
            logger.exception("FloatMonitor: _load_group failed for group %d", group_id)

    def _subscribe_group(self, cfg):
        # type: (dict) -> None
        """Subscribe to MQTT for a group (called with lock held)."""
        group_id = cfg['id']
        topic = (cfg.get('float_mqtt_topic') or '').strip()
        server_id = cfg.get('float_mqtt_server_id')
        mode = cfg.get('float_mode', 'NO')
        timeout_min = cfg.get('float_timeout_minutes', 30)
        debounce_sec = cfg.get('float_debounce_seconds', 5)
        name = cfg.get('name', '')

        if not topic or not server_id:
            return

        server_id = int(server_id)
        client = self.mqtt_clients.get(server_id)
        if client is None:
            return

        # Store subscription info
        tripped_topic = "/devices/float-watchdog/controls/group_%d_tripped" % group_id
        self._subscriptions[group_id] = {
            'topic': topic,
            'server_id': server_id,
            'float_mode': mode,
            'float_timeout_minutes': timeout_min,
            'float_debounce_seconds': debounce_sec,
            'tripped_topic': tripped_topic,
            'name': name,
        }

        # Map topics to group
        self._topic_to_group[topic] = group_id
        self._topic_to_group[tripped_topic] = group_id

        # Initialize state if not exists
        if group_id not in self._states:
            gs = _GroupState(group_id)
            gs.debounce_seconds = debounce_sec
            gs.timeout_minutes = timeout_min
            self._states[group_id] = gs
        else:
            gs = self._states[group_id]
            gs.debounce_seconds = debounce_sec
            gs.timeout_minutes = timeout_min

        # Subscribe
        try:
            client.subscribe(topic)
        except Exception:
            logger.exception("FloatMonitor: subscribe failed for %s", topic)

        try:
            client.subscribe(tripped_topic)
        except Exception:
            logger.exception("FloatMonitor: subscribe failed for %s", tripped_topic)

        # Store on_connect for reconnection
        if server_id not in self._original_on_message:
            self._original_on_message[server_id] = getattr(client, 'on_connect', None)
            client.on_connect = self._on_mqtt_connect

    def _unsubscribe_group(self, group_id):
        # type: (int) -> None
        """Unsubscribe MQTT topics for a group (called with lock held)."""
        sub = self._subscriptions.get(group_id)
        if not sub:
            return

        topic = sub['topic']
        tripped_topic = sub['tripped_topic']
        server_id = sub['server_id']
        client = self.mqtt_clients.get(server_id)

        if client:
            try:
                client.unsubscribe(topic)
            except Exception:
                logger.exception("FloatMonitor: unsubscribe failed for %s", topic)
            try:
                client.unsubscribe(tripped_topic)
            except Exception:
                logger.exception("FloatMonitor: unsubscribe failed for %s", tripped_topic)

        # Clean up mappings
        self._topic_to_group.pop(topic, None)
        self._topic_to_group.pop(tripped_topic, None)
        del self._subscriptions[group_id]
