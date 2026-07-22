import logging
import sqlite3
from typing import Any

from db.base import BaseRepository, retry_on_busy
from utils import decrypt_secret, encrypt_secret

logger = logging.getLogger(__name__)


class MqttRepository(BaseRepository):
    """Repository for MQTT server CRUD + encrypt/decrypt passwords."""

    _SERVER_SETTING_KEYS = (
        "rain.server_id",
        "master.server_id",
        "env.temp.server_id",
        "env.hum.server_id",
    )

    @staticmethod
    def _decrypt_mqtt_password(server: dict[str, Any]) -> dict[str, Any]:
        """Decrypt MQTT password if it's stored encrypted (ENC: prefix)."""
        pwd = server.get("password")
        if pwd and isinstance(pwd, str) and pwd.startswith("ENC:"):
            server["password"] = decrypt_secret(pwd[4:])
        return server

    def get_mqtt_servers(self) -> list[dict[str, Any]]:
        try:
            return self.get_mqtt_servers_strict()
        except sqlite3.Error as e:
            logger.error("Ошибка получения MQTT серверов: %s", e)
            return []

    def get_mqtt_servers_strict(self) -> list[dict[str, Any]]:
        """Return public/decrypted server rows and propagate DB failures."""

        with self._connect() as conn:
            conn.row_factory = sqlite3.Row
            cur = conn.execute("SELECT * FROM mqtt_servers ORDER BY id")
            return [self._decrypt_mqtt_password(dict(row)) for row in cur.fetchall()]

    def get_mqtt_server(self, server_id: int) -> dict[str, Any] | None:
        try:
            return self.get_mqtt_server_strict(server_id)
        except sqlite3.Error as e:
            logger.error("Ошибка получения MQTT сервера %s: %s", server_id, e)
            return None

    def get_mqtt_server_strict(self, server_id: int) -> dict[str, Any] | None:
        """Return one public/decrypted server row and propagate DB failures."""

        with self._connect() as conn:
            conn.row_factory = sqlite3.Row
            cur = conn.execute("SELECT * FROM mqtt_servers WHERE id = ?", (server_id,))
            row = cur.fetchone()
            return self._decrypt_mqtt_password(dict(row)) if row else None

    def get_mqtt_server_storage_snapshot(self, server_id: int) -> dict[str, Any] | None:
        """Return the exact at-rest row, retaining encrypted password bytes."""

        return self._get_storage_snapshot("mqtt_servers", server_id)

    def restore_mqtt_server_snapshot(
        self,
        before: dict[str, Any],
        expected_current: dict[str, Any] | None = None,
    ) -> bool:
        return self._restore_storage_snapshot("mqtt_servers", "mqtt_server", before, expected_current)

    def delete_mqtt_server_if_unchanged(self, expected: dict[str, Any]) -> bool:
        return self._delete_storage_snapshot_if_unchanged("mqtt_servers", expected)

    @classmethod
    def _collect_server_references(cls, conn: sqlite3.Connection, server_id: int) -> dict[str, list[Any]]:
        sid = int(server_id)
        references: dict[str, list[Any]] = {
            "zones": [
                int(row[0])
                for row in conn.execute(
                    "SELECT id FROM zones WHERE mqtt_server_id = ? ORDER BY id",
                    (sid,),
                ).fetchall()
            ],
            "groups_master": [
                int(row[0])
                for row in conn.execute(
                    "SELECT id FROM groups WHERE master_mqtt_server_id = ? ORDER BY id",
                    (sid,),
                ).fetchall()
            ],
            "groups_pressure": [
                int(row[0])
                for row in conn.execute(
                    "SELECT id FROM groups WHERE pressure_mqtt_server_id = ? ORDER BY id",
                    (sid,),
                ).fetchall()
            ],
            "groups_water": [
                int(row[0])
                for row in conn.execute(
                    "SELECT id FROM groups WHERE water_mqtt_server_id = ? ORDER BY id",
                    (sid,),
                ).fetchall()
            ],
            "groups_float": [
                int(row[0])
                for row in conn.execute(
                    "SELECT id FROM groups WHERE float_mqtt_server_id = ? ORDER BY id",
                    (sid,),
                ).fetchall()
            ],
            "settings": [],
        }
        placeholders = ", ".join("?" for _ in cls._SERVER_SETTING_KEYS)
        for key, value in conn.execute(
            f"SELECT key, value FROM settings WHERE key IN ({placeholders}) ORDER BY key",
            cls._SERVER_SETTING_KEYS,
        ).fetchall():
            try:
                if int(value) == sid:
                    references["settings"].append(str(key))
            except (TypeError, ValueError):
                continue
        return references

    def get_mqtt_server_references(self, server_id: int) -> dict[str, list[Any]]:
        """Return structured hardware/config references to ``server_id``."""

        try:
            with self._connect() as conn:
                return self._collect_server_references(conn, server_id)
        except sqlite3.Error as e:
            logger.error("Ошибка получения ссылок MQTT сервера %s: %s", server_id, e)
            raise

    @staticmethod
    def _references_within_settings_scope(
        references: dict[str, list[Any]],
        allowed_settings: set[str] | frozenset[str],
    ) -> bool:
        for kind, items in references.items():
            if not items:
                continue
            if kind != "settings":
                return False
            if not set(str(item) for item in items).issubset(set(allowed_settings)):
                return False
        return True

    @staticmethod
    def _rain_config_from_connection(conn: sqlite3.Connection) -> dict[str, Any]:
        rows = dict(
            conn.execute(
                "SELECT key, value FROM settings WHERE key IN (?, ?, ?, ?)",
                ("rain.enabled", "rain.topic", "rain.type", "rain.server_id"),
            ).fetchall()
        )
        server_value = rows.get("rain.server_id")
        return {
            "enabled": str(rows.get("rain.enabled") or "0") in ("1", "true", "True"),
            "topic": str(rows.get("rain.topic") or ""),
            "type": rows.get("rain.type") if rows.get("rain.type") in ("NO", "NC") else "NO",
            "server_id": int(server_value) if server_value and str(server_value).isdigit() else None,
        }

    @staticmethod
    def _update_assignments(data: dict[str, Any]) -> tuple[list[str], list[Any]]:
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

        raw_password = data.get("password")
        set_password = "password" in data and raw_password != "***" and raw_password is not None
        if set_password:
            assignments.append("password = ?")
            params.append(("ENC:" + encrypt_secret(raw_password)) if raw_password else None)
        return assignments, params

    @retry_on_busy()
    def update_mqtt_server_reference_guarded(
        self,
        server_id: int,
        data: dict[str, Any],
        *,
        allowed_settings: set[str] | frozenset[str],
    ) -> dict[str, Any]:
        """Atomically check reference scope and update one runtime row."""
        assignments, params = self._update_assignments(data)
        set_clause = ", ".join((*assignments, "updated_at = CURRENT_TIMESTAMP"))
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            before_row = conn.execute(
                "SELECT * FROM mqtt_servers WHERE id = ?",
                (int(server_id),),
            ).fetchone()
            if before_row is None:
                conn.rollback()
                return {"status": "not_found", "references": {}, "rain_config": None}
            before_snapshot = dict(before_row)
            references = self._collect_server_references(conn, server_id)
            nonempty_references = {kind: items for kind, items in references.items() if items}
            if not self._references_within_settings_scope(references, allowed_settings):
                conn.rollback()
                return {"status": "blocked", "references": nonempty_references, "rain_config": None}
            cursor = conn.execute(
                f"UPDATE mqtt_servers SET {set_clause} WHERE id = ?",
                (*params, int(server_id)),
            )
            updated_row = conn.execute(
                "SELECT * FROM mqtt_servers WHERE id = ?",
                (int(server_id),),
            ).fetchone()
            if cursor.rowcount != 1 or updated_row is None:
                conn.rollback()
                return {"status": "not_found", "references": {}, "rain_config": None, "snapshot": None}
            rain_config = self._rain_config_from_connection(conn)
            snapshot = dict(updated_row)
            conn.commit()
            return {
                "status": "updated",
                "references": nonempty_references,
                "rain_config": rain_config,
                "before_snapshot": before_snapshot,
                "snapshot": snapshot,
            }

    @retry_on_busy()
    def restore_mqtt_server_snapshot_reference_guarded(
        self,
        before: dict[str, Any],
        expected_current: dict[str, Any],
        *,
        allowed_settings: set[str] | frozenset[str],
    ) -> dict[str, Any]:
        """CAS rollback only while no disallowed reference appeared."""
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            columns = self._storage_columns(conn, "mqtt_servers")
            before_values = self._validated_snapshot_values(before, columns)
            if before_values is None:
                conn.rollback()
                return {"restored": False, "references": {}}
            server_id = int(before["id"])
            references = self._collect_server_references(conn, server_id)
            nonempty_references = {kind: items for kind, items in references.items() if items}
            if not self._references_within_settings_scope(references, allowed_settings):
                conn.rollback()
                return {"restored": False, "references": nonempty_references}
            current = conn.execute("SELECT * FROM mqtt_servers WHERE id = ?", (server_id,)).fetchone()
            if not self._snapshot_matches(current, expected_current, columns):
                conn.rollback()
                return {"restored": False, "references": nonempty_references}
            mutable_columns = tuple(column for column in columns if column != "id")
            assignments = ", ".join(f'"{column}" = ?' for column in mutable_columns)
            conn.execute(
                f"UPDATE mqtt_servers SET {assignments} WHERE id = ?",
                (*[before[column] for column in mutable_columns], server_id),
            )
            conn.commit()
            return {"restored": True, "references": nonempty_references}

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
            assignments, params = self._update_assignments(data)
            set_clause = ", ".join((*assignments, "updated_at = CURRENT_TIMESTAMP"))
            params.append(server_id)
            with self._connect() as conn:
                cursor = conn.execute(
                    f"""
                    UPDATE mqtt_servers
                    SET {set_clause}
                    WHERE id = ?
                """,
                    params,
                )
                conn.commit()
                return cursor.rowcount == 1
        except sqlite3.Error as e:
            logger.error("Ошибка обновления MQTT сервера %s: %s", server_id, e)
            return False

    @retry_on_busy()
    def delete_mqtt_server(self, server_id: int) -> bool:
        """Delete only an existing, completely unreferenced server.

        Hardware/config references are RESTRICT rather than SET NULL or
        CASCADE: silently disconnecting a zone or rebinding it to a later row
        is unsafe. ``BEGIN IMMEDIATE`` makes the reference check and DELETE a
        single writer transaction.
        """
        try:
            with self._connect() as conn:
                conn.execute("BEGIN IMMEDIATE")
                if conn.execute("SELECT 1 FROM mqtt_servers WHERE id = ?", (int(server_id),)).fetchone() is None:
                    conn.rollback()
                    return False
                references = self._collect_server_references(conn, server_id)
                if any(references.values()):
                    logger.warning("MQTT server %s delete restricted by references: %s", server_id, references)
                    conn.rollback()
                    return False
                cursor = conn.execute("DELETE FROM mqtt_servers WHERE id = ?", (int(server_id),))
                conn.commit()
                return cursor.rowcount == 1
        except sqlite3.Error as e:
            logger.error("Ошибка удаления MQTT сервера %s: %s", server_id, e)
            return False
