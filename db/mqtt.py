import logging
import sqlite3
from typing import Any

from db.base import BaseRepository, retry_on_busy
from utils import decrypt_secret, encrypt_secret

logger = logging.getLogger(__name__)


class MqttRepository(BaseRepository):
    """Repository for MQTT server CRUD + encrypt/decrypt passwords."""

    @staticmethod
    def _decrypt_mqtt_password(server: dict[str, Any]) -> dict[str, Any]:
        """Decrypt MQTT password if it's stored encrypted (ENC: prefix)."""
        pwd = server.get("password")
        if pwd and isinstance(pwd, str) and pwd.startswith("ENC:"):
            server["password"] = decrypt_secret(pwd[4:])
        return server

    def get_mqtt_servers(self) -> list[dict[str, Any]]:
        try:
            with self._connect() as conn:
                conn.row_factory = sqlite3.Row
                cur = conn.execute("SELECT * FROM mqtt_servers ORDER BY id")
                return [self._decrypt_mqtt_password(dict(row)) for row in cur.fetchall()]
        except sqlite3.Error as e:
            logger.error("Ошибка получения MQTT серверов: %s", e)
            return []

    def get_mqtt_server(self, server_id: int) -> dict[str, Any] | None:
        try:
            with self._connect() as conn:
                conn.row_factory = sqlite3.Row
                cur = conn.execute("SELECT * FROM mqtt_servers WHERE id = ?", (server_id,))
                row = cur.fetchone()
                return self._decrypt_mqtt_password(dict(row)) if row else None
        except sqlite3.Error as e:
            logger.error("Ошибка получения MQTT сервера %s: %s", server_id, e)
            return None

    @retry_on_busy()
    def create_mqtt_server(self, data: dict[str, Any]) -> dict[str, Any] | None:
        try:
            raw_password = data.get("password")
            enc_password = ("ENC:" + encrypt_secret(raw_password)) if raw_password else raw_password
            with self._connect() as conn:
                cur = conn.execute(
                    """
                    INSERT INTO mqtt_servers (name, host, port, username, password, client_id, enabled,
                                              tls_enabled, tls_ca_path, tls_cert_path, tls_key_path, tls_insecure, tls_version)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                    (
                        data.get("name", "MQTT"),
                        data.get("host", "localhost"),
                        int(data.get("port", 1883)),
                        data.get("username"),
                        enc_password,
                        data.get("client_id"),
                        1 if data.get("enabled", True) else 0,
                        1 if data.get("tls_enabled") else 0,
                        data.get("tls_ca_path"),
                        data.get("tls_cert_path"),
                        data.get("tls_key_path"),
                        1 if data.get("tls_insecure") else 0,
                        data.get("tls_version"),
                    ),
                )
                server_id = cur.lastrowid
                conn.commit()
                return self.get_mqtt_server(server_id)
        except sqlite3.Error as e:
            logger.error("Ошибка создания MQTT сервера: %s", e)
            return None

    @retry_on_busy()
    def update_mqtt_server(self, server_id: int, data: dict[str, Any]) -> bool:
        try:
            assignments: list[str] = []
            params: list[Any] = []

            converters = {
                "name": lambda value: value,
                "host": lambda value: value,
                "port": int,
                "username": lambda value: value,
                "client_id": lambda value: value,
                "enabled": lambda value: 1 if value else 0,
                "tls_enabled": lambda value: 1 if value else 0,
                "tls_ca_path": lambda value: value,
                "tls_cert_path": lambda value: value,
                "tls_key_path": lambda value: value,
                "tls_insecure": lambda value: 1 if value else 0,
                "tls_version": lambda value: value,
            }
            for field, converter in converters.items():
                if field in data:
                    assignments.append(f"{field} = ?")
                    params.append(converter(data[field]))

            # An absent password and the API mask mean "leave unchanged".
            # An explicit empty string clears authentication.
            raw_password = data.get("password")
            set_password = "password" in data and raw_password != "***" and raw_password is not None
            if set_password:
                assignments.append("password = ?")
                params.append(("ENC:" + encrypt_secret(raw_password)) if raw_password else None)

            assignments.append("updated_at = CURRENT_TIMESTAMP")
            params.append(server_id)
            with self._connect() as conn:
                conn.execute(
                    f"""
                    UPDATE mqtt_servers
                    SET {", ".join(assignments)}
                    WHERE id = ?
                """,
                    params,
                )
                conn.commit()
                return True
        except sqlite3.Error as e:
            logger.error("Ошибка обновления MQTT сервера %s: %s", server_id, e)
            return False

    @retry_on_busy()
    def delete_mqtt_server(self, server_id: int) -> bool:
        try:
            with self._connect() as conn:
                conn.execute("DELETE FROM mqtt_servers WHERE id = ?", (server_id,))
                conn.commit()
                return True
        except sqlite3.Error as e:
            logger.error("Ошибка удаления MQTT сервера %s: %s", server_id, e)
            return False
