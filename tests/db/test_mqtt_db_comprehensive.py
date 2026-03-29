"""Comprehensive tests for db/mqtt.py."""
import pytest
import os

os.environ['TESTING'] = '1'


class TestMqttServerCRUD:
    def test_create_server(self, test_db):
        s = test_db.create_mqtt_server({
            'name': 'Test MQTT', 'host': '127.0.0.1', 'port': 1883,
        })
        assert s is not None

    def test_get_servers(self, test_db):
        test_db.create_mqtt_server({'name': 'S1', 'host': '10.0.0.1', 'port': 1883})
        test_db.create_mqtt_server({'name': 'S2', 'host': '10.0.0.2', 'port': 1883})
        servers = test_db.get_mqtt_servers()
        assert len(servers) >= 2

    def test_get_server(self, test_db):
        s = test_db.create_mqtt_server({'name': 'Get', 'host': '10.0.0.1', 'port': 1883})
        fetched = test_db.get_mqtt_server(s['id'])
        assert fetched is not None

    def test_get_server_not_found(self, test_db):
        assert test_db.get_mqtt_server(99999) is None

    def test_update_server(self, test_db):
        s = test_db.create_mqtt_server({'name': 'Old', 'host': '10.0.0.1', 'port': 1883})
        result = test_db.update_mqtt_server(s['id'], {
            'name': 'New', 'host': '10.0.0.2', 'port': 8883,
        })
        assert result is True

    def test_delete_server(self, test_db):
        s = test_db.create_mqtt_server({'name': 'Del', 'host': '10.0.0.1', 'port': 1883})
        result = test_db.delete_mqtt_server(s['id'])
        assert result is True
        assert test_db.get_mqtt_server(s['id']) is None

    def test_create_with_auth(self, test_db):
        s = test_db.create_mqtt_server({
            'name': 'Auth', 'host': '10.0.0.1', 'port': 1883,
            'username': 'user', 'password': 'pass',
        })
        assert s is not None
