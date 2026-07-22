"""Tests for graceful shutdown (shutdown_all_zones in services/app_init.py)."""

from unittest.mock import MagicMock, patch

import pytest


def _confirmed_message_info() -> MagicMock:
    info = MagicMock()
    info.rc = 0
    info.wait_for_publish.return_value = None
    info.is_published.return_value = True
    return info


@pytest.fixture(autouse=True)
def _reset_shutdown():
    """Reset shutdown state between tests."""
    from services.app_init import reset_shutdown

    reset_shutdown()
    yield
    reset_shutdown()


@pytest.fixture
def mqtt_server_id(test_db):
    server = test_db.create_mqtt_server({"name": "Test", "host": "127.0.0.1", "port": 1883, "enabled": 1})
    return server["id"]


class TestShutdownAllZones:
    """Tests for shutdown_all_zones()."""

    def test_shutdown_sends_off_to_all_zones(self, test_db, mqtt_server_id):
        """Should publish OFF to all zone topics."""
        test_db.create_zone(
            {
                "name": "Z1",
                "duration": 10,
                "group_id": 1,
                "topic": "/devices/wb-mr6cv3_85/controls/K1",
                "mqtt_server_id": mqtt_server_id,
            }
        )
        test_db.create_zone(
            {
                "name": "Z2",
                "duration": 10,
                "group_id": 1,
                "topic": "/devices/wb-mr6cv3_85/controls/K2",
                "mqtt_server_id": mqtt_server_id,
            }
        )

        mock_client = MagicMock()
        mock_result = _confirmed_message_info()
        mock_client.publish.return_value = mock_result

        with patch("services.mqtt_pub.get_or_create_mqtt_client", return_value=mock_client):
            from services.app_init import shutdown_all_zones

            shutdown_all_zones(db=test_db)

        # One actuator command topic (/on) per zone; never write report/base.
        assert mock_client.publish.call_count == 2

    def test_shutdown_idempotent(self, test_db, mqtt_server_id):
        """Should only run once."""
        mock_client = MagicMock()
        mock_result = _confirmed_message_info()
        mock_client.publish.return_value = mock_result
        test_db.create_zone(
            {
                "name": "Z1",
                "duration": 10,
                "group_id": 1,
                "topic": "/devices/test/K1",
                "mqtt_server_id": mqtt_server_id,
            }
        )

        with patch("services.mqtt_pub.get_or_create_mqtt_client", return_value=mock_client):
            from services.app_init import shutdown_all_zones

            shutdown_all_zones(db=test_db)
            first_count = mock_client.publish.call_count
            shutdown_all_zones(db=test_db)
            assert mock_client.publish.call_count == first_count

    def test_shutdown_no_zones(self, test_db):
        """Should handle DB with no zones gracefully."""
        from services.app_init import shutdown_all_zones

        shutdown_all_zones(db=test_db)

    def test_shutdown_waits_for_publish(self, test_db, mqtt_server_id):
        """Should call wait_for_publish on each result."""
        test_db.create_zone(
            {
                "name": "Z1",
                "duration": 10,
                "group_id": 1,
                "topic": "/devices/test/K1",
                "mqtt_server_id": mqtt_server_id,
            }
        )

        mock_result = _confirmed_message_info()
        mock_client = MagicMock()
        mock_client.publish.return_value = mock_result

        with patch("services.mqtt_pub.get_or_create_mqtt_client", return_value=mock_client):
            from services.app_init import shutdown_all_zones

            shutdown_all_zones(db=test_db)

        assert mock_result.wait_for_publish.call_count >= 1
        waited = mock_result.wait_for_publish.call_args.kwargs["timeout"]
        assert 0 < waited <= 10.0

    def test_shutdown_handles_publish_timeout(self, test_db, mqtt_server_id):
        """Should handle wait_for_publish timeout gracefully."""
        test_db.create_zone(
            {
                "name": "Z1",
                "duration": 10,
                "group_id": 1,
                "topic": "/devices/test/K1",
                "mqtt_server_id": mqtt_server_id,
            }
        )

        mock_result = _confirmed_message_info()
        mock_result.wait_for_publish.side_effect = RuntimeError("timeout")
        mock_client = MagicMock()
        mock_client.publish.return_value = mock_result

        with patch("services.mqtt_pub.get_or_create_mqtt_client", return_value=mock_client):
            from services.app_init import shutdown_all_zones

            shutdown_all_zones(db=test_db)

    def test_shutdown_handles_no_mqtt_client(self, test_db, mqtt_server_id):
        """Should handle unavailable MQTT client gracefully."""
        test_db.create_zone(
            {
                "name": "Z1",
                "duration": 10,
                "group_id": 1,
                "topic": "/devices/test/K1",
                "mqtt_server_id": mqtt_server_id,
            }
        )

        with patch("services.mqtt_pub.get_or_create_mqtt_client", return_value=None):
            from services.app_init import shutdown_all_zones

            shutdown_all_zones(db=test_db)

    def test_shutdown_skips_zones_without_topic(self, test_db, mqtt_server_id):
        """Should skip zones that have no MQTT topic."""
        test_db.create_zone({"name": "Z1", "duration": 10, "group_id": 1})
        test_db.create_zone(
            {
                "name": "Z2",
                "duration": 10,
                "group_id": 1,
                "topic": "/devices/test/K1",
                "mqtt_server_id": mqtt_server_id,
            }
        )

        mock_client = MagicMock()
        mock_result = _confirmed_message_info()
        mock_client.publish.return_value = mock_result

        with patch("services.mqtt_pub.get_or_create_mqtt_client", return_value=mock_client):
            from services.app_init import shutdown_all_zones

            shutdown_all_zones(db=test_db)

        assert mock_client.publish.call_count == 1  # Only Z2 actuator /on
