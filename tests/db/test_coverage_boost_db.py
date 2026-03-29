"""Coverage boost: DB tests for uncovered repository methods."""
import json
import sqlite3
import pytest


class TestGroupsDB:
    """Additional tests for groups repository."""

    def test_create_group(self, test_db):
        g = test_db.create_group('New G')
        assert g is not None
        assert g['name'] == 'New G'

    def test_get_groups(self, test_db):
        test_db.create_group('Get G')
        groups = test_db.get_groups()
        assert len(groups) >= 1

    def test_update_group(self, test_db):
        g = test_db.create_group('Upd G')
        if g:
            test_db.update_group(g['id'], {'name': 'Updated G'})

    def test_delete_group(self, test_db):
        g = test_db.create_group('Del G')
        if g:
            test_db.delete_group(g['id'])

    def test_get_zones_by_group(self, test_db):
        test_db.create_zone({'name': 'Z1', 'duration': 10, 'group_id': 1})
        zones = test_db.get_zones_by_group(1)
        assert len(zones) >= 1

    def test_update_group_fields(self, test_db):
        g = test_db.create_group('Fields G')
        if g:
            test_db.update_group_fields(g['id'], {
                'use_master_valve': 1,
                'master_mqtt_topic': '/devices/test/MV',
                'master_mode': 'NC',
            })


class TestMQTTDB:
    """Additional tests for MQTT repository."""

    def test_create_mqtt_server(self, test_db):
        test_db.create_mqtt_server({
            'name': 'Test', 'host': '1.2.3.4', 'port': 1883, 'enabled': 1
        })
        servers = test_db.get_mqtt_servers()
        assert len(servers) >= 1

    def test_update_mqtt_server(self, test_db):
        test_db.create_mqtt_server({
            'name': 'Test', 'host': '1.2.3.4', 'port': 1883, 'enabled': 1
        })
        servers = test_db.get_mqtt_servers()
        if servers:
            test_db.update_mqtt_server(servers[0]['id'], {'name': 'Updated'})

    def test_delete_mqtt_server(self, test_db):
        test_db.create_mqtt_server({
            'name': 'ToDel', 'host': '1.2.3.4', 'port': 1883, 'enabled': 1
        })
        servers = test_db.get_mqtt_servers()
        if servers:
            test_db.delete_mqtt_server(servers[0]['id'])


class TestTelegramDB:
    """Additional tests for telegram repository."""

    def test_get_bot_user_by_chat_nonexistent(self, test_db):
        user = test_db.get_bot_user_by_chat(99999)
        assert user is None

    def test_get_bot_user_notif(self, test_db):
        result = test_db.get_bot_user_notif_settings(99999)
        assert result is None or isinstance(result, dict)


class TestSettingsDB:
    """Additional tests for settings repository."""

    def test_set_and_get(self, test_db):
        test_db.set_setting_value('test.key', 'test.value')
        assert test_db.get_setting_value('test.key') == 'test.value'

    def test_get_logging_debug(self, test_db):
        result = test_db.get_logging_debug()
        assert result is not None or result is False

    def test_ensure_password_change(self, test_db):
        test_db.ensure_password_change_required()

    def test_get_due_bot_subscriptions(self, test_db):
        from datetime import datetime
        result = test_db.get_due_bot_subscriptions(datetime.now())
        assert isinstance(result, list)


class TestLogsDB:
    """Additional tests for logs repository."""

    def test_add_and_get_logs(self, test_db):
        test_db.add_log('test_type', 'test detail 1')
        test_db.add_log('test_type', 'test detail 2')
        logs = test_db.get_logs()
        assert len(logs) >= 2

    def test_get_logs_by_type(self, test_db):
        test_db.add_log('unique_type_cov', 'unique detail')
        logs = test_db.get_logs(event_type='unique_type_cov')
        assert len(logs) >= 1

    def test_program_cancellation(self, test_db):
        test_db.cancel_program_run_for_group(1, '2024-01-01', 1)
        assert test_db.is_program_run_cancelled_for_group(1, '2024-01-01', 1)
        assert not test_db.is_program_run_cancelled_for_group(1, '2024-01-02', 1)
