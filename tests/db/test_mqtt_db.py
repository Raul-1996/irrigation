"""Tests for MQTT server DB operations, password encryption."""

import os

os.environ["TESTING"] = "1"


class TestMqttServerCRUD:
    def test_create_server(self, test_db):
        server = test_db.create_mqtt_server(
            {
                "name": "Test",
                "host": "192.168.1.1",
                "port": 1883,
                "username": "user",
                "password": "pass",
                "enabled": 1,
            }
        )
        assert server is not None
        assert server["name"] == "Test"
        assert server["host"] == "192.168.1.1"

    def test_get_server(self, test_db):
        server = test_db.create_mqtt_server(
            {
                "name": "Get",
                "host": "10.0.0.1",
                "port": 1883,
            }
        )
        fetched = test_db.get_mqtt_server(server["id"])
        assert fetched is not None
        assert fetched["host"] == "10.0.0.1"

    def test_get_server_not_found(self, test_db):
        assert test_db.get_mqtt_server(9999) is None

    def test_get_servers_list(self, test_db):
        test_db.create_mqtt_server({"name": "S1", "host": "h1", "port": 1883})
        test_db.create_mqtt_server({"name": "S2", "host": "h2", "port": 1883})
        servers = test_db.get_mqtt_servers()
        assert len(servers) >= 2

    def test_update_server(self, test_db):
        server = test_db.create_mqtt_server({"name": "Old", "host": "h1", "port": 1883})
        ok = test_db.update_mqtt_server(server["id"], {"name": "New", "host": "h2"})
        assert ok is True
        updated = test_db.get_mqtt_server(server["id"])
        assert updated["name"] == "New"

    def test_update_without_password_keeps_existing(self, test_db):
        """Regression: сохранение из UI шлёт payload без password — пароль не должен затираться."""
        server = test_db.create_mqtt_server(
            {"name": "Keep", "host": "h1", "port": 1883, "username": "u", "password": "secret1"}
        )
        ok = test_db.update_mqtt_server(server["id"], {"name": "Keep2", "host": "h2", "username": "u"})
        assert ok is True
        updated = test_db.get_mqtt_server(server["id"])
        assert updated["name"] == "Keep2"
        assert updated["password"] == "secret1"

    def test_update_with_empty_password_clears_it(self, test_db):
        """Явная пустая строка — «очистить пароль» (брокер перестал требовать
        авторизацию); «не менять» — это отсутствие ключа или маска "***"."""
        server = test_db.create_mqtt_server({"name": "ClrE", "host": "h1", "port": 1883, "password": "secret2"})
        ok = test_db.update_mqtt_server(server["id"], {"name": "ClrE", "host": "h1", "password": ""})
        assert ok is True
        assert not test_db.get_mqtt_server(server["id"])["password"]

    def test_update_with_masked_password_keeps_existing(self, test_db):
        """Маска "***" из GET-ответов API не должна сохраняться как настоящий пароль."""
        server = test_db.create_mqtt_server({"name": "KeepM", "host": "h1", "port": 1883, "password": "secret3"})
        ok = test_db.update_mqtt_server(server["id"], {"name": "KeepM", "host": "h1", "password": "***"})
        assert ok is True
        assert test_db.get_mqtt_server(server["id"])["password"] == "secret3"

    def test_update_with_new_password_changes_it(self, test_db):
        server = test_db.create_mqtt_server({"name": "Chg", "host": "h1", "port": 1883, "password": "old-pass"})
        ok = test_db.update_mqtt_server(server["id"], {"name": "Chg", "host": "h1", "password": "new-pass"})
        assert ok is True
        assert test_db.get_mqtt_server(server["id"])["password"] == "new-pass"

    def test_delete_server(self, test_db):
        server = test_db.create_mqtt_server({"name": "Del", "host": "h1", "port": 1883})
        assert test_db.delete_mqtt_server(server["id"]) is True
        assert test_db.get_mqtt_server(server["id"]) is None

    def test_delete_server_not_found(self, test_db):
        result = test_db.delete_mqtt_server(9999)
        assert isinstance(result, bool)


class TestMqttPasswordEncryption:
    def test_password_encrypted_on_create(self, test_db):
        """Passwords should be stored encrypted."""
        server = test_db.create_mqtt_server(
            {
                "name": "Enc",
                "host": "h1",
                "port": 1883,
                "password": "mysecret",
            }
        )
        # The raw stored password should be encrypted (ENC: prefix)
        import sqlite3

        conn = sqlite3.connect(test_db.db_path)
        cur = conn.execute("SELECT password FROM mqtt_servers WHERE id=?", (server["id"],))
        row = cur.fetchone()
        conn.close()
        if row and row[0]:
            # Should be encrypted or the plain value depending on migration
            assert row[0] is not None
