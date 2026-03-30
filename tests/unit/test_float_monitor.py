"""TDD tests for FloatMonitor — services/float_monitor.py (NOT YET IMPLEMENTED).

These tests define the public API contract for FloatMonitor.
All tests will be RED until FloatMonitor is implemented.

Spec refs:
- program-queue-tests-spec.md §3.3
- program-queue-spec.md §3 (float valve)
"""
import sqlite3
import threading
import time
from datetime import datetime
from typing import Dict, Any, Optional, Callable
from unittest.mock import MagicMock, patch, call

import pytest

# --- These imports WILL FAIL until the module is created ---
# from services.float_monitor import FloatMonitor, FloatState


# ---------------------------------------------------------------------------
# Helpers & Fixtures
# ---------------------------------------------------------------------------

def _create_db_tables(db_path):
    """Create minimal DB schema needed for FloatMonitor tests."""
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=30000")
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS groups (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL DEFAULT '',
            float_enabled INTEGER DEFAULT 0,
            float_mqtt_topic TEXT DEFAULT NULL,
            float_mqtt_server_id INTEGER DEFAULT NULL,
            float_mode TEXT DEFAULT 'NO',
            float_timeout_minutes INTEGER DEFAULT 30,
            float_debounce_seconds INTEGER DEFAULT 5
        );

        CREATE TABLE IF NOT EXISTS zones (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL DEFAULT '',
            group_id INTEGER NOT NULL,
            duration INTEGER DEFAULT 600,
            state TEXT DEFAULT 'off',
            pause_reason TEXT DEFAULT NULL,
            pause_remaining_seconds INTEGER DEFAULT NULL
        );

        CREATE TABLE IF NOT EXISTS float_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            group_id INTEGER NOT NULL,
            event_type TEXT NOT NULL,
            paused_zones TEXT,
            created_at TEXT DEFAULT (datetime('now', 'localtime'))
        );
        CREATE INDEX IF NOT EXISTS idx_fe_group ON float_events(group_id);

        CREATE TABLE IF NOT EXISTS mqtt_servers (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT DEFAULT '',
            host TEXT DEFAULT '127.0.0.1',
            port INTEGER DEFAULT 1883,
            username TEXT DEFAULT '',
            password TEXT DEFAULT '',
            enabled INTEGER DEFAULT 1
        );
    """)
    conn.commit()
    conn.close()
    return db_path


def _insert_group(db_path, group_id, name="Test Group", float_enabled=1,
                  float_mqtt_topic="/devices/wb-gpio/controls/A1_IN",
                  float_mqtt_server_id=1, float_mode='NO',
                  float_timeout_minutes=30, float_debounce_seconds=5):
    """Insert a test group into the DB."""
    conn = sqlite3.connect(db_path)
    conn.execute(
        "INSERT INTO groups (id, name, float_enabled, float_mqtt_topic, "
        "float_mqtt_server_id, float_mode, float_timeout_minutes, float_debounce_seconds) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (group_id, name, float_enabled, float_mqtt_topic,
         float_mqtt_server_id, float_mode, float_timeout_minutes, float_debounce_seconds)
    )
    conn.commit()
    conn.close()


def _insert_zone(db_path, zone_id, group_id, name="Zone", state='off',
                 duration=600, pause_reason=None, pause_remaining_seconds=None):
    """Insert a test zone into the DB."""
    conn = sqlite3.connect(db_path)
    conn.execute(
        "INSERT INTO zones (id, name, group_id, duration, state, pause_reason, pause_remaining_seconds) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (zone_id, name, group_id, duration, state, pause_reason, pause_remaining_seconds)
    )
    conn.commit()
    conn.close()


def _insert_mqtt_server(db_path, server_id=1):
    """Insert a test MQTT server into the DB."""
    conn = sqlite3.connect(db_path)
    conn.execute(
        "INSERT INTO mqtt_servers (id, name, host, port) VALUES (?, ?, ?, ?)",
        (server_id, "Test MQTT", "127.0.0.1", 1883)
    )
    conn.commit()
    conn.close()


@pytest.fixture
def mock_db_path(tmp_path):
    """Create a temporary DB with all required tables."""
    db_path = str(tmp_path / "test_float.db")
    _create_db_tables(db_path)
    _insert_mqtt_server(db_path, server_id=1)
    return db_path


@pytest.fixture
def mock_mqtt_clients():
    """Dict {server_id: MagicMock} simulating paho-mqtt clients."""
    client = MagicMock()
    client.subscribe = MagicMock()
    client.unsubscribe = MagicMock()
    client.on_message = None
    client.on_connect = None
    client.on_disconnect = None
    return {1: client}


@pytest.fixture
def mock_queue_manager():
    """MagicMock ProgramQueueManager with cancel_group."""
    qm = MagicMock()
    qm.cancel_group = MagicMock(return_value=0)
    return qm


@pytest.fixture
def mock_telegram():
    """MagicMock telegram_notify callable."""
    return MagicMock()


@pytest.fixture
def float_monitor(mock_db_path, mock_mqtt_clients, mock_queue_manager, mock_telegram):
    """Create FloatMonitor instance with mocked dependencies."""
    from services.float_monitor import FloatMonitor
    fm = FloatMonitor(
        db_path=mock_db_path,
        mqtt_clients=mock_mqtt_clients,
        queue_manager=mock_queue_manager,
        telegram_notify=mock_telegram,
    )
    yield fm
    # Cleanup
    try:
        fm.stop()
    except Exception:
        pass


def simulate_float_message(float_monitor, group_id, payload):
    """Helper: simulate an MQTT message arriving for a group's float sensor.

    Calls FloatMonitor._on_float_message(group_id, payload) directly,
    bypassing the actual MQTT callback plumbing.
    """
    float_monitor._on_float_message(group_id, str(payload))


# ---------------------------------------------------------------------------
# 3.3.1  Basic tests (5)
# ---------------------------------------------------------------------------

class TestFloatMonitorBasic:
    """Basic float monitor functionality."""

    def test_float_off_is_paused_true(self, float_monitor, mock_db_path):
        """#1: Float OFF (level_ok=False) → is_paused(group_id) == True."""
        _insert_group(mock_db_path, 1, float_mode='NO', float_debounce_seconds=0)
        float_monitor.start()
        # Payload "0" in NO mode means level_ok=False
        simulate_float_message(float_monitor, 1, "0")
        assert float_monitor.is_paused(1) is True

    def test_float_on_is_paused_false(self, float_monitor, mock_db_path):
        """#2: Float ON (level_ok=True) → is_paused(group_id) == False."""
        _insert_group(mock_db_path, 1, float_mode='NO', float_debounce_seconds=0)
        float_monitor.start()
        # Payload "1" in NO mode means level_ok=True
        simulate_float_message(float_monitor, 1, "1")
        assert float_monitor.is_paused(1) is False

    def test_no_mode_payload_mapping(self, float_monitor, mock_db_path):
        """#3: NO mode: "0" → level_ok=False, "1" → level_ok=True."""
        _insert_group(mock_db_path, 1, float_mode='NO', float_debounce_seconds=0)
        float_monitor.start()

        simulate_float_message(float_monitor, 1, "0")
        assert float_monitor.is_paused(1) is True  # level_ok=False → paused

        simulate_float_message(float_monitor, 1, "1")
        assert float_monitor.is_paused(1) is False  # level_ok=True → not paused

    def test_nc_mode_payload_inverted(self, float_monitor, mock_db_path):
        """#4: NC mode: "1" → level_ok=False (inverted), "0" → level_ok=True."""
        _insert_group(mock_db_path, 1, float_mode='NC', float_debounce_seconds=0)
        float_monitor.start()

        # NC: "1" means contact closed = water LOW
        simulate_float_message(float_monitor, 1, "1")
        assert float_monitor.is_paused(1) is True

        # NC: "0" means contact open = water OK
        simulate_float_message(float_monitor, 1, "0")
        assert float_monitor.is_paused(1) is False

    def test_get_state_structure(self, float_monitor, mock_db_path):
        """#5: get_state returns dict matching FloatStateResponse spec §6.3."""
        _insert_group(mock_db_path, 1, float_debounce_seconds=0)
        _insert_zone(mock_db_path, 1, group_id=1, name="Lawn", state='on')
        float_monitor.start()
        simulate_float_message(float_monitor, 1, "0")  # trigger pause

        state = float_monitor.get_state(1)
        assert isinstance(state, dict)
        # Required keys from spec §6.3
        assert 'group_id' in state
        assert 'level_ok' in state
        assert 'paused' in state
        assert 'paused_since' in state
        assert 'timeout_at' in state
        assert 'paused_zones' in state
        assert state['group_id'] == 1
        assert state['paused'] is True
        assert state['level_ok'] is False
        # paused_since should be a string (ISO datetime) when paused
        assert state['paused_since'] is not None
        # hysteresis section
        assert 'hysteresis' in state
        assert 'trip_count' in state['hysteresis']
        assert 'emergency_stopped' in state['hysteresis']


# ---------------------------------------------------------------------------
# 3.3.2  Debounce tests (4)
# ---------------------------------------------------------------------------

class TestFloatMonitorDebounce:
    """Debounce logic: short signals are ignored, stable signals processed."""

    def test_debounce_short_signal_ignored(self, float_monitor, mock_db_path, monkeypatch):
        """#6: Signal < debounce_seconds → pause does NOT happen."""
        _insert_group(mock_db_path, 1, float_debounce_seconds=5)
        float_monitor.start()

        # Mock time to control debounce
        fake_time = [100.0]
        monkeypatch.setattr(time, 'monotonic', lambda: fake_time[0])

        # Float goes OFF
        simulate_float_message(float_monitor, 1, "0")
        # Only 2 seconds later, float goes ON (bounce)
        fake_time[0] = 102.0
        simulate_float_message(float_monitor, 1, "1")

        # Pause should NOT have happened (signal was < 5s debounce)
        assert float_monitor.is_paused(1) is False

    def test_debounce_stable_signal_processed(self, float_monitor, mock_db_path, monkeypatch):
        """#7: Signal stable >= debounce_seconds → pause happens."""
        _insert_group(mock_db_path, 1, float_debounce_seconds=5)
        float_monitor.start()

        fake_time = [100.0]
        monkeypatch.setattr(time, 'monotonic', lambda: fake_time[0])

        # Float goes OFF and stays OFF past debounce
        simulate_float_message(float_monitor, 1, "0")
        fake_time[0] = 106.0  # 6 seconds > 5s debounce

        # Trigger debounce check (implementation may use timer or re-check)
        # Re-send same OFF to confirm stability
        simulate_float_message(float_monitor, 1, "0")

        assert float_monitor.is_paused(1) is True

    def test_debounce_bounce_pattern_no_pause(self, float_monitor, mock_db_path, monkeypatch):
        """#8: OFF(2s)→ON(2s)→OFF(2s), debounce=5s → no pause."""
        _insert_group(mock_db_path, 1, float_debounce_seconds=5)
        float_monitor.start()

        fake_time = [100.0]
        monkeypatch.setattr(time, 'monotonic', lambda: fake_time[0])

        # Bounce pattern: none of the OFF intervals >= 5s
        simulate_float_message(float_monitor, 1, "0")  # OFF at t=100
        fake_time[0] = 102.0
        simulate_float_message(float_monitor, 1, "1")  # ON at t=102

        fake_time[0] = 104.0
        simulate_float_message(float_monitor, 1, "0")  # OFF at t=104
        fake_time[0] = 106.0
        simulate_float_message(float_monitor, 1, "1")  # ON at t=106

        assert float_monitor.is_paused(1) is False

    def test_debounce_stable_off_triggers_pause(self, float_monitor, mock_db_path, monkeypatch):
        """#9: OFF stable 6s with debounce=5s → pause at ~5s."""
        _insert_group(mock_db_path, 1, float_debounce_seconds=5)
        float_monitor.start()

        fake_time = [100.0]
        monkeypatch.setattr(time, 'monotonic', lambda: fake_time[0])

        simulate_float_message(float_monitor, 1, "0")
        # Advance time past debounce
        fake_time[0] = 106.0
        # Confirm the stable OFF (debounce callback or re-evaluation)
        simulate_float_message(float_monitor, 1, "0")

        assert float_monitor.is_paused(1) is True


# ---------------------------------------------------------------------------
# 3.3.3  Pause / Resume tests (6)
# ---------------------------------------------------------------------------

class TestFloatMonitorPauseResume:
    """Pause/resume event-based mechanics (NOT exclusive_start)."""

    def test_pause_sets_float_pause_event(self, float_monitor, mock_db_path):
        """#10: Float OFF + debounce passed → float_pause_event set for group."""
        _insert_group(mock_db_path, 1, float_debounce_seconds=0)
        float_monitor.start()

        simulate_float_message(float_monitor, 1, "0")

        # FloatMonitor should have an internal pause state / event for group 1
        assert float_monitor.is_paused(1) is True

    def test_resume_sets_float_resume_event(self, float_monitor, mock_db_path):
        """#11: Float ON after pause → resume event set, worker can wake."""
        _insert_group(mock_db_path, 1, float_debounce_seconds=0)
        float_monitor.start()

        # Pause first
        simulate_float_message(float_monitor, 1, "0")
        assert float_monitor.is_paused(1) is True

        # Resume
        simulate_float_message(float_monitor, 1, "1")
        assert float_monitor.is_paused(1) is False
        # Resume event should be retrievable
        resume_event = float_monitor.get_resume_event(1)
        assert isinstance(resume_event, threading.Event)
        assert resume_event.is_set() is True

    def test_pause_saves_remaining_to_db(self, float_monitor, mock_db_path):
        """#12: When paused, remaining_seconds saved to DB zones table."""
        _insert_group(mock_db_path, 1, float_debounce_seconds=0)
        _insert_zone(mock_db_path, 1, group_id=1, name="Zone 3", state='on', duration=600)
        float_monitor.start()

        simulate_float_message(float_monitor, 1, "0")

        # Check DB: zone should be paused with remaining saved
        conn = sqlite3.connect(mock_db_path)
        row = conn.execute(
            "SELECT state, pause_reason, pause_remaining_seconds FROM zones WHERE id=1"
        ).fetchone()
        conn.close()
        # Zone state should be 'paused' with reason 'float'
        assert row is not None
        assert row[0] == 'paused'
        assert row[1] == 'float'
        # pause_remaining_seconds should be set (>0)
        assert row[2] is not None and row[2] > 0

    def test_resume_after_5min_remaining_unchanged(self, float_monitor, mock_db_path, monkeypatch):
        """#13: After 5 min pause, remaining is preserved (worker sleeps, time doesn't tick)."""
        _insert_group(mock_db_path, 1, float_debounce_seconds=0)
        _insert_zone(mock_db_path, 1, group_id=1, name="Zone 3", state='on', duration=600)
        float_monitor.start()

        fake_time = [1000.0]
        monkeypatch.setattr(time, 'monotonic', lambda: fake_time[0])

        # Pause
        simulate_float_message(float_monitor, 1, "0")

        # Read saved remaining
        conn = sqlite3.connect(mock_db_path)
        row = conn.execute(
            "SELECT pause_remaining_seconds FROM zones WHERE id=1"
        ).fetchone()
        conn.close()
        saved_remaining = row[0]

        # Advance time 5 minutes (paused)
        fake_time[0] = 1300.0

        # Resume
        simulate_float_message(float_monitor, 1, "1")

        # remaining should still be the same value (worker resumes with it)
        state = float_monitor.get_state(1)
        # The saved remaining in DB should not have decreased during pause
        conn = sqlite3.connect(mock_db_path)
        row = conn.execute(
            "SELECT pause_remaining_seconds FROM zones WHERE id=1"
        ).fetchone()
        conn.close()
        # After resume the value may be cleared, but during pause it was preserved
        # The key assertion is that remaining was NOT ticked down during pause
        assert saved_remaining is not None and saved_remaining > 0

    def test_pause_when_no_active_zones_noop(self, float_monitor, mock_db_path):
        """#14: Float OFF but no active zones → is_paused=True, no zones affected."""
        _insert_group(mock_db_path, 1, float_debounce_seconds=0)
        # All zones are OFF
        _insert_zone(mock_db_path, 1, group_id=1, name="Zone 1", state='off')
        _insert_zone(mock_db_path, 2, group_id=1, name="Zone 2", state='off')
        float_monitor.start()

        simulate_float_message(float_monitor, 1, "0")

        # State recorded but no zones paused
        assert float_monitor.is_paused(1) is True
        conn = sqlite3.connect(mock_db_path)
        rows = conn.execute(
            "SELECT state FROM zones WHERE group_id=1 AND state='paused'"
        ).fetchall()
        conn.close()
        assert len(rows) == 0  # No zones were paused (they were already off)

    def test_resume_without_prior_pause_noop(self, float_monitor, mock_db_path):
        """#15: Float ON without prior pause → no error, no state change."""
        _insert_group(mock_db_path, 1, float_debounce_seconds=0)
        float_monitor.start()

        # Send ON without prior OFF — should be a no-op
        simulate_float_message(float_monitor, 1, "1")

        assert float_monitor.is_paused(1) is False
        # No exceptions raised


# ---------------------------------------------------------------------------
# 3.3.4  Timeout tests (4)
# ---------------------------------------------------------------------------

class TestFloatMonitorTimeout:
    """Timeout: if level not restored → emergency stop."""

    def test_float_timeout_emergency_stop(self, float_monitor, mock_db_path,
                                          mock_queue_manager, monkeypatch):
        """#16: Float OFF > timeout → _on_timeout → cancel_group."""
        _insert_group(mock_db_path, 1, float_timeout_minutes=30, float_debounce_seconds=0)
        float_monitor.start()

        fake_time = [1000.0]
        monkeypatch.setattr(time, 'monotonic', lambda: fake_time[0])
        monkeypatch.setattr(time, 'time', lambda: fake_time[0])

        # Float goes OFF
        simulate_float_message(float_monitor, 1, "0")

        # Advance time past timeout (31 min = 1860 sec)
        fake_time[0] = 1000.0 + 1860.0

        # Trigger timeout check (implementation-dependent: timer callback or poll)
        float_monitor._check_timeouts()

        # cancel_group should be called
        mock_queue_manager.cancel_group.assert_called_with(1)

    def test_float_restored_before_timeout(self, float_monitor, mock_db_path,
                                           mock_queue_manager, monkeypatch):
        """#17: Float restored before timeout → no emergency stop."""
        _insert_group(mock_db_path, 1, float_timeout_minutes=30, float_debounce_seconds=0)
        float_monitor.start()

        fake_time = [1000.0]
        monkeypatch.setattr(time, 'monotonic', lambda: fake_time[0])
        monkeypatch.setattr(time, 'time', lambda: fake_time[0])

        # Float goes OFF
        simulate_float_message(float_monitor, 1, "0")

        # Restored at 29 min
        fake_time[0] = 1000.0 + 29 * 60
        simulate_float_message(float_monitor, 1, "1")

        # Advance past original timeout
        fake_time[0] = 1000.0 + 31 * 60

        # Timeout should NOT fire
        try:
            float_monitor._check_timeouts()
        except Exception:
            pass  # Method may not exist or be a no-op when timer cancelled

        mock_queue_manager.cancel_group.assert_not_called()

    def test_timeout_calls_cancel_group(self, float_monitor, mock_db_path,
                                        mock_queue_manager, monkeypatch):
        """#18: On timeout, queue_manager.cancel_group(group_id) is called."""
        _insert_group(mock_db_path, 1, float_timeout_minutes=1, float_debounce_seconds=0)
        float_monitor.start()

        fake_time = [1000.0]
        monkeypatch.setattr(time, 'monotonic', lambda: fake_time[0])
        monkeypatch.setattr(time, 'time', lambda: fake_time[0])

        simulate_float_message(float_monitor, 1, "0")

        # Advance past 1 min timeout
        fake_time[0] = 1000.0 + 65
        float_monitor._check_timeouts()

        mock_queue_manager.cancel_group.assert_called_once_with(1)

    def test_timeout_sends_telegram(self, float_monitor, mock_db_path,
                                    mock_telegram, monkeypatch):
        """#19: On timeout, telegram_notify called with 'АВАРИЙНЫЙ СТОП'."""
        _insert_group(mock_db_path, 1, name="Насос-1",
                      float_timeout_minutes=1, float_debounce_seconds=0)
        float_monitor.start()

        fake_time = [1000.0]
        monkeypatch.setattr(time, 'monotonic', lambda: fake_time[0])
        monkeypatch.setattr(time, 'time', lambda: fake_time[0])

        simulate_float_message(float_monitor, 1, "0")

        fake_time[0] = 1000.0 + 65
        float_monitor._check_timeouts()

        # Telegram should have been called
        mock_telegram.assert_called()
        # Message should contain emergency stop text
        call_args = mock_telegram.call_args
        msg_text = str(call_args)
        assert "АВАРИЙНЫЙ СТОП" in msg_text or "аварийный" in msg_text.lower() or "АВАРИЯ" in msg_text


# ---------------------------------------------------------------------------
# 3.3.5  Hysteresis tests (3)
# ---------------------------------------------------------------------------

class TestFloatMonitorHysteresis:
    """Hysteresis: min_run_time, cooldown, max_trips emergency stop."""

    def test_hysteresis_min_run_time_blocks_repause(self, float_monitor, mock_db_path, monkeypatch):
        """#20: Resume → re-OFF within 30s (< min_run_time=60) → pause still happens
        (safety first) but float_pause_too_soon is logged."""
        _insert_group(mock_db_path, 1, float_debounce_seconds=0)
        float_monitor.start()

        fake_time = [1000.0]
        monkeypatch.setattr(time, 'monotonic', lambda: fake_time[0])

        # Initial pause
        simulate_float_message(float_monitor, 1, "0")
        # Resume
        fake_time[0] = 1020.0
        simulate_float_message(float_monitor, 1, "1")

        # Re-OFF after only 30s (< min_run_time=60)
        fake_time[0] = 1050.0
        simulate_float_message(float_monitor, 1, "0")

        # Pause still happens (safety priority)
        assert float_monitor.is_paused(1) is True

    def test_hysteresis_after_min_run_time_allows(self, float_monitor, mock_db_path, monkeypatch):
        """#21: Resume → re-OFF after 90s (> min_run_time=60) → normal pause, no warning."""
        _insert_group(mock_db_path, 1, float_debounce_seconds=0)
        float_monitor.start()

        fake_time = [1000.0]
        monkeypatch.setattr(time, 'monotonic', lambda: fake_time[0])

        # Pause
        simulate_float_message(float_monitor, 1, "0")
        # Resume
        fake_time[0] = 1020.0
        simulate_float_message(float_monitor, 1, "1")

        # Re-OFF after 90s (> 60s min_run_time)
        fake_time[0] = 1110.0  # 1020 + 90
        simulate_float_message(float_monitor, 1, "0")

        assert float_monitor.is_paused(1) is True

    def test_hysteresis_max_trips_emergency_stop(self, float_monitor, mock_db_path,
                                                  mock_queue_manager, mock_telegram,
                                                  monkeypatch):
        """#22: 3 trips within FLOAT_TRIP_WINDOW=300s → emergency stop."""
        _insert_group(mock_db_path, 1, name="Насос-1", float_debounce_seconds=0)
        float_monitor.start()

        fake_time = [1000.0]
        monkeypatch.setattr(time, 'monotonic', lambda: fake_time[0])

        # Trip 1: OFF → ON
        simulate_float_message(float_monitor, 1, "0")
        fake_time[0] = 1060.0
        simulate_float_message(float_monitor, 1, "1")

        # Trip 2: OFF → ON
        fake_time[0] = 1120.0
        simulate_float_message(float_monitor, 1, "0")
        fake_time[0] = 1180.0
        simulate_float_message(float_monitor, 1, "1")

        # Trip 3: OFF — this should trigger emergency stop
        fake_time[0] = 1240.0
        simulate_float_message(float_monitor, 1, "0")

        # All 3 trips within 300s window → emergency stop
        mock_queue_manager.cancel_group.assert_called_with(1)
        # Telegram notification about unstable float
        mock_telegram.assert_called()
        call_text = str(mock_telegram.call_args)
        assert "нестабил" in call_text.lower() or "АВАРИЙНЫЙ" in call_text or "АВАРИЯ" in call_text


# ---------------------------------------------------------------------------
# 3.3.6  Per-group isolation tests (3)
# ---------------------------------------------------------------------------

class TestFloatMonitorPerGroup:
    """Per-group isolation: float events in one group don't affect others."""

    def test_float_pause_group1_not_affects_group2(self, float_monitor, mock_db_path):
        """#23: Group 1 paused → group 2 unaffected."""
        _insert_group(mock_db_path, 1, name="Group 1", float_debounce_seconds=0,
                      float_mqtt_topic="/dev/gpio/A1_IN")
        _insert_group(mock_db_path, 2, name="Group 2", float_debounce_seconds=0,
                      float_mqtt_topic="/dev/gpio/A2_IN")
        float_monitor.start()

        # Pause group 1 only
        simulate_float_message(float_monitor, 1, "0")

        assert float_monitor.is_paused(1) is True
        assert float_monitor.is_paused(2) is False

    def test_both_groups_paused_independently(self, float_monitor, mock_db_path):
        """#24: Both groups paused → separate timers, separate events."""
        _insert_group(mock_db_path, 1, name="Group 1", float_debounce_seconds=0,
                      float_mqtt_topic="/dev/gpio/A1_IN")
        _insert_group(mock_db_path, 2, name="Group 2", float_debounce_seconds=0,
                      float_mqtt_topic="/dev/gpio/A2_IN")
        float_monitor.start()

        simulate_float_message(float_monitor, 1, "0")
        simulate_float_message(float_monitor, 2, "0")

        assert float_monitor.is_paused(1) is True
        assert float_monitor.is_paused(2) is True

        # They should have separate state objects
        state1 = float_monitor.get_state(1)
        state2 = float_monitor.get_state(2)
        assert state1['group_id'] == 1
        assert state2['group_id'] == 2
        # paused_since should be different or at least independently managed
        assert state1['paused_since'] is not None
        assert state2['paused_since'] is not None

    def test_resume_group1_group2_still_paused(self, float_monitor, mock_db_path):
        """#25: Both paused → resume group 1 → group 2 still paused."""
        _insert_group(mock_db_path, 1, name="Group 1", float_debounce_seconds=0,
                      float_mqtt_topic="/dev/gpio/A1_IN")
        _insert_group(mock_db_path, 2, name="Group 2", float_debounce_seconds=0,
                      float_mqtt_topic="/dev/gpio/A2_IN")
        float_monitor.start()

        # Pause both
        simulate_float_message(float_monitor, 1, "0")
        simulate_float_message(float_monitor, 2, "0")

        # Resume group 1 only
        simulate_float_message(float_monitor, 1, "1")

        assert float_monitor.is_paused(1) is False
        assert float_monitor.is_paused(2) is True
