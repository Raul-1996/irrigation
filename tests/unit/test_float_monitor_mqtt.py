"""TDD tests for FloatMonitor MQTT integration — services/float_monitor.py (NOT YET IMPLEMENTED).

These tests verify MQTT subscription/unsubscription mechanics.
All tests will be RED until FloatMonitor is implemented.

Spec refs:
- program-queue-tests-spec.md §3.4
- program-queue-spec.md §3.3, §3.4, §3.10
"""
import sqlite3
import threading
from unittest.mock import MagicMock, patch, call

import pytest

# --- These imports WILL FAIL until the module is created ---
# from services.float_monitor import FloatMonitor


# ---------------------------------------------------------------------------
# Helpers & Fixtures
# ---------------------------------------------------------------------------

def _create_db_tables(db_path):
    """Create minimal DB schema for FloatMonitor MQTT tests."""
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
    """Insert a test group."""
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


def _insert_mqtt_server(db_path, server_id=1):
    """Insert a test MQTT server."""
    conn = sqlite3.connect(db_path)
    conn.execute(
        "INSERT INTO mqtt_servers (id, name, host, port) VALUES (?, ?, ?, ?)",
        (server_id, "Test MQTT", "127.0.0.1", 1883)
    )
    conn.commit()
    conn.close()


@pytest.fixture
def mock_db_path(tmp_path):
    """Temporary DB with required tables."""
    db_path = str(tmp_path / "test_float_mqtt.db")
    _create_db_tables(db_path)
    _insert_mqtt_server(db_path, server_id=1)
    return db_path


@pytest.fixture
def mock_mqtt_client():
    """Single MagicMock MQTT client."""
    client = MagicMock()
    client.subscribe = MagicMock()
    client.unsubscribe = MagicMock()
    client.on_message = None
    client.on_connect = None
    client.on_disconnect = None
    return client


@pytest.fixture
def mock_mqtt_clients(mock_mqtt_client):
    """Dict {server_id: MagicMock} for MQTT clients."""
    return {1: mock_mqtt_client}


@pytest.fixture
def mock_queue_manager():
    """MagicMock ProgramQueueManager."""
    qm = MagicMock()
    qm.cancel_group = MagicMock(return_value=0)
    return qm


@pytest.fixture
def mock_telegram():
    """MagicMock telegram_notify."""
    return MagicMock()


@pytest.fixture
def float_monitor(mock_db_path, mock_mqtt_clients, mock_queue_manager, mock_telegram):
    """FloatMonitor with mocked dependencies."""
    from services.float_monitor import FloatMonitor
    fm = FloatMonitor(
        db_path=mock_db_path,
        mqtt_clients=mock_mqtt_clients,
        queue_manager=mock_queue_manager,
        telegram_notify=mock_telegram,
    )
    yield fm
    try:
        fm.stop()
    except Exception:
        pass


# ---------------------------------------------------------------------------
# 3.4  MQTT Integration Tests (8)
# ---------------------------------------------------------------------------

class TestFloatMonitorMQTT:
    """MQTT subscription/unsubscription mechanics for FloatMonitor."""

    def test_start_subscribes_to_correct_topics(self, float_monitor, mock_db_path,
                                                 mock_mqtt_client):
        """#1: start() subscribes to MQTT topics of all float_enabled groups."""
        _insert_group(mock_db_path, 1, name="Group 1", float_enabled=1,
                      float_mqtt_topic="/devices/wb-gpio/controls/A1_IN")
        _insert_group(mock_db_path, 2, name="Group 2", float_enabled=1,
                      float_mqtt_topic="/devices/wb-gpio/controls/A2_IN")

        float_monitor.start()

        # Should have subscribed to both topics
        subscribe_calls = mock_mqtt_client.subscribe.call_args_list
        subscribed_topics = [c[0][0] for c in subscribe_calls]
        assert "/devices/wb-gpio/controls/A1_IN" in subscribed_topics
        assert "/devices/wb-gpio/controls/A2_IN" in subscribed_topics

    def test_stop_unsubscribes(self, float_monitor, mock_db_path, mock_mqtt_client):
        """#2: stop() unsubscribes from all float MQTT topics."""
        _insert_group(mock_db_path, 1, float_enabled=1,
                      float_mqtt_topic="/devices/wb-gpio/controls/A1_IN")

        float_monitor.start()
        float_monitor.stop()

        # unsubscribe should have been called
        mock_mqtt_client.unsubscribe.assert_called()
        unsub_calls = mock_mqtt_client.unsubscribe.call_args_list
        unsub_topics = [c[0][0] for c in unsub_calls]
        assert "/devices/wb-gpio/controls/A1_IN" in unsub_topics

    def test_reload_group_resubscribes(self, float_monitor, mock_db_path, mock_mqtt_client):
        """#3: reload_group() unsubscribes old topic, subscribes new one."""
        _insert_group(mock_db_path, 1, float_enabled=1,
                      float_mqtt_topic="/devices/wb-gpio/controls/A1_IN")

        float_monitor.start()

        # Change the topic in DB
        conn = sqlite3.connect(mock_db_path)
        conn.execute(
            "UPDATE groups SET float_mqtt_topic='/devices/wb-gpio/controls/A3_IN' WHERE id=1"
        )
        conn.commit()
        conn.close()

        mock_mqtt_client.subscribe.reset_mock()
        mock_mqtt_client.unsubscribe.reset_mock()

        float_monitor.reload_group(1)

        # Old topic unsubscribed
        unsub_calls = mock_mqtt_client.unsubscribe.call_args_list
        unsub_topics = [c[0][0] for c in unsub_calls]
        assert "/devices/wb-gpio/controls/A1_IN" in unsub_topics

        # New topic subscribed
        sub_calls = mock_mqtt_client.subscribe.call_args_list
        sub_topics = [c[0][0] for c in sub_calls]
        assert "/devices/wb-gpio/controls/A3_IN" in sub_topics

    def test_mqtt_reconnect_restores_subscriptions(self, float_monitor, mock_db_path,
                                                    mock_mqtt_client):
        """#4: MQTT disconnect → reconnect → subscriptions restored."""
        _insert_group(mock_db_path, 1, float_enabled=1,
                      float_mqtt_topic="/devices/wb-gpio/controls/A1_IN")

        float_monitor.start()
        initial_sub_count = mock_mqtt_client.subscribe.call_count

        # Simulate reconnect by calling on_connect callback
        # FloatMonitor should have registered an on_connect handler
        # that re-subscribes to all topics
        if hasattr(float_monitor, '_on_mqtt_connect'):
            float_monitor._on_mqtt_connect(mock_mqtt_client, None, None, 0)
        elif mock_mqtt_client.on_connect is not None:
            mock_mqtt_client.on_connect(mock_mqtt_client, None, None, 0)

        # subscribe should have been called again
        assert mock_mqtt_client.subscribe.call_count > initial_sub_count

    def test_invalid_payload_ignored(self, float_monitor, mock_db_path):
        """#5: MQTT message with payload 'garbage' → no state change, no exception."""
        _insert_group(mock_db_path, 1, float_enabled=1, float_debounce_seconds=0)
        float_monitor.start()

        # Should not raise
        try:
            float_monitor._on_float_message(1, "garbage")
        except Exception:
            pytest.fail("Invalid payload should not raise an exception")

        # State should not change
        assert float_monitor.is_paused(1) is False

    def test_float_disabled_no_subscription(self, float_monitor, mock_db_path,
                                             mock_mqtt_client):
        """#6: Group with float_enabled=False → no MQTT subscription for it."""
        _insert_group(mock_db_path, 1, float_enabled=0,
                      float_mqtt_topic="/devices/wb-gpio/controls/A1_IN")

        float_monitor.start()

        # Check that no subscribe was called for the disabled group's topic
        sub_calls = mock_mqtt_client.subscribe.call_args_list
        if sub_calls:
            sub_topics = [c[0][0] for c in sub_calls]
            assert "/devices/wb-gpio/controls/A1_IN" not in sub_topics

    def test_two_floats_two_subscriptions(self, float_monitor, mock_db_path,
                                           mock_mqtt_client):
        """#7: 2 groups with float_enabled → 2 separate subscriptions, correct routing."""
        _insert_group(mock_db_path, 1, name="Group 1", float_enabled=1,
                      float_mqtt_topic="/devices/wb-gpio/controls/A1_IN",
                      float_debounce_seconds=0)
        _insert_group(mock_db_path, 2, name="Group 2", float_enabled=1,
                      float_mqtt_topic="/devices/wb-gpio/controls/A2_IN",
                      float_debounce_seconds=0)

        float_monitor.start()

        sub_calls = mock_mqtt_client.subscribe.call_args_list
        sub_topics = [c[0][0] for c in sub_calls]

        # Both topics subscribed
        assert "/devices/wb-gpio/controls/A1_IN" in sub_topics
        assert "/devices/wb-gpio/controls/A2_IN" in sub_topics

        # Messages routed correctly
        float_monitor._on_float_message(1, "0")  # Group 1 OFF
        assert float_monitor.is_paused(1) is True
        assert float_monitor.is_paused(2) is False

        float_monitor._on_float_message(2, "0")  # Group 2 OFF
        assert float_monitor.is_paused(2) is True

    def test_tripped_topic_subscription(self, float_monitor, mock_db_path,
                                         mock_mqtt_client):
        """#8: float_enabled group also subscribes to wb-rules tripped topic."""
        _insert_group(mock_db_path, 1, float_enabled=1,
                      float_mqtt_topic="/devices/wb-gpio/controls/A1_IN")

        float_monitor.start()

        sub_calls = mock_mqtt_client.subscribe.call_args_list
        sub_topics = [c[0][0] for c in sub_calls]

        # Should subscribe to the float sensor topic
        assert "/devices/wb-gpio/controls/A1_IN" in sub_topics

        # Should ALSO subscribe to the wb-rules watchdog tripped topic
        expected_tripped = "/devices/float-watchdog/controls/group_1_tripped"
        assert expected_tripped in sub_topics
