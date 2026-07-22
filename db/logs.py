import logging
import os
import re
import sqlite3
import stat as stat_module
from datetime import datetime
from typing import Any
from urllib.parse import quote

from db.base import BaseRepository, retry_on_busy
from db.identity import DURABLE_ENTITIES as _DURABLE_ENTITIES
from db.identity import MAX_ENTITY_ID
from db.schema import APPLICATION_ID, USER_VERSION

logger = logging.getLogger(__name__)

_SQLITE_MAX_ROWID = 9_223_372_036_854_775_807

_MQTT_SETTING_KEYS = (
    "rain.server_id",
    "master.server_id",
    "env.temp.server_id",
    "env.hum.server_id",
)
_GROUP_MQTT_SERVER_COLUMNS = (
    "master_mqtt_server_id",
    "pressure_mqtt_server_id",
    "water_mqtt_server_id",
    "float_mqtt_server_id",
)


def _normalize_schema_sql(value: object) -> str:
    """Canonicalize SQL tokens without changing string/blob literal bytes."""

    sql = str(value)
    normalized: list[str] = []
    whitespace_pending = False
    index = 0
    while index < len(sql):
        character = sql[index]
        if character.isspace():
            whitespace_pending = True
            index += 1
            continue
        if whitespace_pending and normalized:
            normalized.append(" ")
        whitespace_pending = False

        if character != "'":
            normalized.append(character.upper())
            index += 1
            continue

        # SQLite single-quoted literals escape an embedded quote by doubling
        # it. Preserve the complete literal verbatim: trigger values and index
        # predicates are case-sensitive application semantics, not SQL tokens.
        literal_start = index
        index += 1
        while index < len(sql):
            if sql[index] != "'":
                index += 1
                continue
            index += 1
            if index < len(sql) and sql[index] == "'":
                index += 1
                continue
            break
        normalized.append(sql[literal_start:index])

    return "".join(normalized).strip()


def _normalize_default_sql(value: object | None) -> str | None:
    if value is None:
        return None
    return " ".join(str(value).split())


def _schema_keyword_offset(value: object, keyword: str) -> int | None:
    """Find an unquoted SQL keyword and return its original string offset."""

    sql = str(value)
    expected = keyword.upper()
    index = 0
    while index < len(sql):
        if sql.startswith("--", index):
            line_end = sql.find("\n", index + 2)
            index = len(sql) if line_end < 0 else line_end
            continue
        if sql.startswith("/*", index):
            comment_end = sql.find("*/", index + 2)
            index = len(sql) if comment_end < 0 else comment_end + 2
            continue
        if sql[index] in {"'", '"', "`", "["}:
            opener = sql[index]
            closer = "]" if opener == "[" else opener
            index += 1
            while index < len(sql):
                if sql[index] != closer:
                    index += 1
                    continue
                index += 1
                if closer != "]" and index < len(sql) and sql[index] == closer:
                    index += 1
                    continue
                break
            continue
        if sql[index].isalpha() or sql[index] == "_":
            start = index
            index += 1
            while index < len(sql) and (sql[index].isalnum() or sql[index] in {"_", "$"}):
                index += 1
            if sql[start:index].upper() == expected:
                return start
            continue
        index += 1
    return None


def _normalized_where_predicate(value: object) -> str | None:
    sql = str(value)
    offset = _schema_keyword_offset(sql, "WHERE")
    if offset is None:
        return None
    return _normalize_schema_sql(sql[offset + len("WHERE") :])


def _schema_sql_code(value: object) -> str:
    """Return SQL code with comments, literals, and quoted names removed."""

    sql = str(value)
    code: list[str] = []
    index = 0
    while index < len(sql):
        if sql.startswith("--", index):
            line_end = sql.find("\n", index + 2)
            index = len(sql) if line_end < 0 else line_end
            code.append(" ")
            continue
        if sql.startswith("/*", index):
            comment_end = sql.find("*/", index + 2)
            index = len(sql) if comment_end < 0 else comment_end + 2
            code.append(" ")
            continue
        if sql[index] in {"'", '"', "`", "["}:
            opener = sql[index]
            closer = "]" if opener == "[" else opener
            index += 1
            while index < len(sql):
                if sql[index] != closer:
                    index += 1
                    continue
                index += 1
                if closer != "]" and index < len(sql) and sql[index] == closer:
                    index += 1
                    continue
                break
            code.append(" ")
            continue
        code.append(sql[index])
        index += 1

    return " ".join("".join(code).upper().split())


def _has_canonical_autoincrement_id(create_table_sql: object) -> bool:
    """Return whether ``id`` has the real canonical AUTOINCREMENT clause."""

    normalized = _schema_sql_code(create_table_sql)
    return (
        re.search(
            r"(?:\(|,)\s*ID\s+INTEGER\s+PRIMARY\s+KEY\s+AUTOINCREMENT\s*(?:,|\))",
            normalized,
        )
        is not None
    )


def _required_integrity_trigger_sql() -> dict[str, str]:
    """Return canonical trigger SQL expected after forward reconciliation."""

    definitions: dict[str, str] = {}

    def add(name: str, sql: str) -> None:
        definitions[name] = _normalize_schema_sql(sql)

    for table, entity in _DURABLE_ENTITIES:
        add(
            f"trg_{table}_retire_id",
            f"""
                CREATE TRIGGER trg_{table}_retire_id
                AFTER DELETE ON {table}
                BEGIN
                    INSERT OR IGNORE INTO retired_entity_ids(entity, id)
                    VALUES ('{entity}', OLD.id);
                END
            """,
        )
        add(
            f"trg_{table}_reject_out_of_range_id",
            f"""
                CREATE TRIGGER trg_{table}_reject_out_of_range_id
                AFTER INSERT ON {table}
                WHEN NEW.id <= 0 OR NEW.id > {MAX_ENTITY_ID}
                BEGIN
                    SELECT RAISE(ABORT, 'entity identifier out of range');
                END
            """,
        )
        add(
            f"trg_{table}_reject_explicit_id_boundary",
            f"""
                CREATE TRIGGER trg_{table}_reject_explicit_id_boundary
                BEFORE INSERT ON {table}
                WHEN NEW.id != -1 AND NEW.id >= {MAX_ENTITY_ID}
                BEGIN
                    SELECT RAISE(ABORT, 'entity identifier out of range');
                END
            """,
        )
        add(
            f"trg_{table}_reject_id_update",
            f"""
                CREATE TRIGGER trg_{table}_reject_id_update
                BEFORE UPDATE OF id ON {table}
                WHEN NEW.id != OLD.id
                BEGIN
                    SELECT RAISE(ABORT, 'entity identifier is immutable');
                END
            """,
        )
        add(
            f"trg_{table}_reject_retired_id",
            f"""
                CREATE TRIGGER trg_{table}_reject_retired_id
                BEFORE INSERT ON {table}
                WHEN EXISTS (
                    SELECT 1 FROM retired_entity_ids
                    WHERE entity = '{entity}' AND id = NEW.id
                )
                BEGIN
                    SELECT RAISE(ABORT, 'retired identifier cannot be reused');
                END
            """,
        )

    add(
        "trg_zones_version_invalidate",
        """
            CREATE TRIGGER trg_zones_version_invalidate
            AFTER UPDATE ON zones
            WHEN NEW.version IS NULL OR NEW.version <= COALESCE(OLD.version, 0)
            BEGIN
                UPDATE zones
                SET version = COALESCE(OLD.version, 0) + 1
                WHERE id = OLD.id;
            END
        """,
    )

    for operation in ("INSERT", "UPDATE"):
        suffix = operation.lower()
        add(
            f"trg_zones_mqtt_server_{suffix}",
            f"""
                CREATE TRIGGER trg_zones_mqtt_server_{suffix}
                BEFORE {operation} ON zones
                WHEN NEW.mqtt_server_id IS NOT NULL
                 AND NOT EXISTS (SELECT 1 FROM mqtt_servers WHERE id = NEW.mqtt_server_id)
                BEGIN
                    SELECT RAISE(ABORT, 'missing mqtt server reference');
                END
            """,
        )
        missing_group_server = " OR ".join(
            f"(NEW.{column} IS NOT NULL AND NOT EXISTS (SELECT 1 FROM mqtt_servers WHERE id = NEW.{column}))"
            for column in _GROUP_MQTT_SERVER_COLUMNS
        )
        add(
            f"trg_groups_mqtt_server_{suffix}",
            f"""
                CREATE TRIGGER trg_groups_mqtt_server_{suffix}
                BEFORE {operation} ON groups
                WHEN {missing_group_server}
                BEGIN
                    SELECT RAISE(ABORT, 'missing mqtt server reference');
                END
            """,
        )
        setting_keys = ", ".join(f"'{key}'" for key in _MQTT_SETTING_KEYS)
        add(
            f"trg_settings_mqtt_server_{suffix}",
            f"""
                CREATE TRIGGER trg_settings_mqtt_server_{suffix}
                BEFORE {operation} ON settings
                WHEN NEW.key IN ({setting_keys}) AND NEW.value IS NOT NULL AND NOT EXISTS
                    (SELECT 1 FROM mqtt_servers WHERE CAST(id AS TEXT) = NEW.value)
                BEGIN
                    SELECT RAISE(ABORT, 'missing mqtt server reference');
                END
            """,
        )

    setting_keys = ", ".join(f"'{key}'" for key in _MQTT_SETTING_KEYS)
    mqtt_delete_references = ["EXISTS (SELECT 1 FROM zones WHERE mqtt_server_id = OLD.id)"]
    mqtt_delete_references.extend(
        f"EXISTS (SELECT 1 FROM groups WHERE {column} = OLD.id)" for column in _GROUP_MQTT_SERVER_COLUMNS
    )
    mqtt_delete_references.append(
        "EXISTS (SELECT 1 FROM settings "
        f"WHERE key IN ({setting_keys}) AND ("
        "value = CAST(OLD.id AS TEXT) OR ("
        "value GLOB '[0-9]*' AND value NOT GLOB '*[^0-9]*' "
        "AND CAST(value AS INTEGER) = OLD.id)))"
    )
    add(
        "trg_mqtt_servers_restrict_referenced_delete",
        f"""
            CREATE TRIGGER trg_mqtt_servers_restrict_referenced_delete
            BEFORE DELETE ON mqtt_servers
            WHEN {" OR ".join(mqtt_delete_references)}
            BEGIN
                SELECT RAISE(ABORT, 'mqtt server is referenced');
            END
        """,
    )

    missing_group = (
        "NEW.group_id IS NOT NULL AND NEW.group_id != 0 AND NOT EXISTS (SELECT 1 FROM groups WHERE id = NEW.group_id)"
    )
    for operation in ("INSERT", "UPDATE"):
        suffix = operation.lower()
        add(
            f"trg_zones_group_{suffix}",
            f"""
                CREATE TRIGGER trg_zones_group_{suffix}
                BEFORE {operation} ON zones
                WHEN {missing_group}
                BEGIN
                    SELECT RAISE(ABORT, 'missing group reference');
                END
            """,
        )
    add(
        "trg_groups_restrict_referenced_delete",
        """
            CREATE TRIGGER trg_groups_restrict_referenced_delete
            BEFORE DELETE ON groups
            WHEN EXISTS (SELECT 1 FROM zones WHERE group_id = OLD.id)
            BEGIN
                SELECT RAISE(ABORT, 'group is referenced');
            END
        """,
    )
    add(
        "trg_groups_restrict_reserved_delete",
        """
            CREATE TRIGGER trg_groups_restrict_reserved_delete
            BEFORE DELETE ON groups
            WHEN OLD.id IN (1, 999)
            BEGIN
                SELECT RAISE(ABORT, 'reserved group cannot be deleted');
            END
        """,
    )
    add(
        "trg_groups_reject_replacing_name",
        """
            CREATE TRIGGER trg_groups_reject_replacing_name
            BEFORE INSERT ON groups
            WHEN EXISTS (
                SELECT 1 FROM groups
                WHERE name = NEW.name AND id != NEW.id
            )
            BEGIN
                SELECT RAISE(ABORT, 'group name conflict cannot replace existing group');
            END
        """,
    )
    add(
        "trg_groups_reject_replacing_name_update",
        """
            CREATE TRIGGER trg_groups_reject_replacing_name_update
            BEFORE UPDATE OF name ON groups
            WHEN EXISTS (
                SELECT 1 FROM groups
                WHERE name = NEW.name AND id != OLD.id
            )
            BEGIN
                SELECT RAISE(ABORT, 'group name conflict cannot replace existing group');
            END
        """,
    )
    return definitions


class LogRepository(BaseRepository):
    """Repository for logs, water_usage, water_stats, and backups."""

    _REQUIRED_BACKUP_TABLES = frozenset(
        {
            "zones",
            "groups",
            "programs",
            "settings",
            "migrations",
            "mqtt_servers",
            "retired_entity_ids",
            "zone_runs",
            "program_cancellations",
            "program_queue_log",
            "float_events",
            "logs",
            "water_usage",
            "bot_users",
            "bot_subscriptions",
            "bot_audit",
            "bot_idempotency",
            "weather_cache",
            "weather_log",
            "weather_decisions",
            "weather_balance_log",
            "audit_log",
        }
    )
    # The stamped USER_VERSION describes one exact writable schema. Future
    # columns require a version bump and an explicit recovery path; otherwise
    # an extra NOT NULL/generated column can make every current INSERT fail.
    # Values are (canonical type family, exact NOT NULL flag). ``NUMERIC`` is
    # the canonical TIMESTAMP declaration used by this schema.
    _REQUIRED_BACKUP_COLUMNS: dict[str, dict[str, tuple[str, bool]]] = {
        "zones": {
            "id": ("INTEGER", False),
            "state": ("TEXT", False),
            "name": ("TEXT", True),
            "icon": ("TEXT", False),
            "duration": ("INTEGER", False),
            "group_id": ("INTEGER", False),
            "topic": ("TEXT", False),
            "postpone_until": ("TEXT", False),
            "postpone_reason": ("TEXT", False),
            "photo_path": ("TEXT", False),
            "created_at": ("NUMERIC", False),
            "updated_at": ("NUMERIC", False),
            "watering_start_time": ("TEXT", False),
            "scheduled_start_time": ("TEXT", False),
            "mqtt_server_id": ("INTEGER", False),
            "watering_start_source": ("TEXT", False),
            "planned_end_time": ("TEXT", False),
            "sequence_id": ("TEXT", False),
            "command_id": ("TEXT", False),
            "version": ("INTEGER", False),
            "commanded_state": ("TEXT", False),
            "observed_state": ("TEXT", False),
            "last_avg_flow_lpm": ("REAL", False),
            "last_total_liters": ("REAL", False),
            "last_fault": ("TEXT", False),
            "fault_count": ("INTEGER", False),
            "pause_remaining_seconds": ("REAL", False),
            "pause_reason": ("TEXT", False),
            "photo_thumb": ("TEXT", False),
        },
        "groups": {
            "id": ("INTEGER", False),
            "name": ("TEXT", True),
            "created_at": ("NUMERIC", False),
            "updated_at": ("NUMERIC", False),
            "use_rain_sensor": ("INTEGER", False),
            "use_master_valve": ("INTEGER", False),
            "master_mqtt_topic": ("TEXT", False),
            "master_mode": ("TEXT", False),
            "master_mqtt_server_id": ("INTEGER", False),
            "master_valve_observed": ("TEXT", False),
            "master_close_delay_sec": ("INTEGER", False),
            "use_pressure_sensor": ("INTEGER", False),
            "pressure_mqtt_topic": ("TEXT", False),
            "pressure_unit": ("TEXT", False),
            "pressure_mqtt_server_id": ("INTEGER", False),
            "use_water_meter": ("INTEGER", False),
            "water_mqtt_topic": ("TEXT", False),
            "water_mqtt_server_id": ("INTEGER", False),
            "water_pulse_size": ("TEXT", False),
            "water_base_value_m3": ("REAL", False),
            "water_base_pulses": ("INTEGER", False),
            "float_enabled": ("INTEGER", False),
            "float_mqtt_topic": ("TEXT", False),
            "float_mqtt_server_id": ("INTEGER", False),
            "float_mode": ("TEXT", False),
            "float_timeout_minutes": ("INTEGER", False),
            "float_debounce_seconds": ("INTEGER", False),
        },
        "programs": {
            "id": ("INTEGER", False),
            "name": ("TEXT", True),
            "time": ("TEXT", True),
            "days": ("TEXT", True),
            "zones": ("TEXT", True),
            "created_at": ("NUMERIC", False),
            "updated_at": ("NUMERIC", False),
            "type": ("TEXT", False),
            "schedule_type": ("TEXT", False),
            "interval_days": ("INTEGER", False),
            "even_odd": ("TEXT", False),
            "color": ("TEXT", False),
            "enabled": ("INTEGER", False),
            "extra_times": ("TEXT", False),
        },
        "settings": {
            "key": ("TEXT", False),
            "value": ("TEXT", False),
        },
        "migrations": {
            "name": ("TEXT", False),
            "applied_at": ("NUMERIC", False),
        },
        "mqtt_servers": {
            "id": ("INTEGER", False),
            "name": ("TEXT", True),
            "host": ("TEXT", True),
            "port": ("INTEGER", False),
            "username": ("TEXT", False),
            "password": ("TEXT", False),
            "client_id": ("TEXT", False),
            "enabled": ("INTEGER", False),
            "created_at": ("NUMERIC", False),
            "updated_at": ("NUMERIC", False),
            "tls_enabled": ("INTEGER", False),
            "tls_ca_path": ("TEXT", False),
            "tls_cert_path": ("TEXT", False),
            "tls_key_path": ("TEXT", False),
            "tls_insecure": ("INTEGER", False),
            "tls_version": ("TEXT", False),
        },
        "retired_entity_ids": {
            "entity": ("TEXT", True),
            "id": ("INTEGER", True),
            "retired_at": ("NUMERIC", False),
        },
        "zone_runs": {
            "id": ("INTEGER", False),
            "zone_id": ("INTEGER", True),
            "group_id": ("INTEGER", True),
            "start_utc": ("TEXT", False),
            "end_utc": ("TEXT", False),
            "start_monotonic": ("REAL", False),
            "end_monotonic": ("REAL", False),
            "start_raw_pulses": ("INTEGER", False),
            "end_raw_pulses": ("INTEGER", False),
            "pulse_liters_at_start": ("INTEGER", False),
            "base_m3_at_start": ("REAL", False),
            "total_liters": ("REAL", False),
            "avg_flow_lpm": ("REAL", False),
            "status": ("TEXT", False),
            "created_at": ("NUMERIC", False),
            "updated_at": ("NUMERIC", False),
            "source": ("TEXT", False),
            "confirmed": ("INTEGER", False),
        },
        "program_cancellations": {
            "program_id": ("INTEGER", True),
            "run_date": ("TEXT", True),
            "group_id": ("INTEGER", False),
            "created_at": ("NUMERIC", False),
        },
        "program_queue_log": {
            "id": ("INTEGER", False),
            "entry_id": ("TEXT", True),
            "program_id": ("INTEGER", True),
            "program_run_id": ("TEXT", False),
            "group_id": ("INTEGER", True),
            "zone_ids": ("TEXT", True),
            "scheduled_time": ("TEXT", True),
            "enqueued_at": ("TEXT", True),
            "started_at": ("TEXT", False),
            "completed_at": ("TEXT", False),
            "state": ("TEXT", True),
            "wait_seconds": ("INTEGER", False),
            "run_seconds": ("INTEGER", False),
            "created_at": ("TEXT", False),
        },
        "float_events": {
            "id": ("INTEGER", False),
            "group_id": ("INTEGER", True),
            "event_type": ("TEXT", True),
            "paused_zones": ("TEXT", False),
            "created_at": ("TEXT", False),
        },
        "logs": {
            "id": ("INTEGER", False),
            "type": ("TEXT", True),
            "details": ("TEXT", False),
            "timestamp": ("NUMERIC", False),
        },
        "water_usage": {
            "id": ("INTEGER", False),
            "zone_id": ("INTEGER", False),
            "liters": ("REAL", False),
            "timestamp": ("NUMERIC", False),
        },
        "bot_users": {
            "id": ("INTEGER", False),
            "chat_id": ("INTEGER", False),
            "username": ("TEXT", False),
            "first_name": ("TEXT", False),
            "role": ("TEXT", False),
            "is_authorized": ("INTEGER", False),
            "failed_attempts": ("INTEGER", False),
            "locked_until": ("TEXT", False),
            "created_at": ("NUMERIC", False),
            "last_seen_at": ("NUMERIC", False),
            "fsm_state": ("TEXT", False),
            "fsm_data": ("TEXT", False),
            "notif_critical": ("INTEGER", False),
            "notif_emergency": ("INTEGER", False),
            "notif_postpone": ("INTEGER", False),
            "notif_zone_events": ("INTEGER", False),
            "notif_rain": ("INTEGER", False),
        },
        "bot_subscriptions": {
            "id": ("INTEGER", False),
            "user_id": ("INTEGER", True),
            "type": ("TEXT", True),
            "format": ("TEXT", True),
            "time_local": ("TEXT", True),
            "dow_mask": ("TEXT", False),
            "enabled": ("INTEGER", False),
            "created_at": ("NUMERIC", False),
        },
        "bot_audit": {
            "id": ("INTEGER", False),
            "user_id": ("INTEGER", False),
            "action": ("TEXT", False),
            "payload_json": ("TEXT", False),
            "ts": ("NUMERIC", False),
        },
        "bot_idempotency": {
            "token": ("TEXT", False),
            "chat_id": ("INTEGER", False),
            "action": ("TEXT", False),
            "created_at": ("NUMERIC", False),
        },
        "weather_cache": {
            "id": ("INTEGER", False),
            "latitude": ("REAL", True),
            "longitude": ("REAL", True),
            "data": ("TEXT", True),
            "fetched_at": ("REAL", True),
        },
        "weather_log": {
            "id": ("INTEGER", False),
            "zone_id": ("INTEGER", False),
            "original_duration": ("INTEGER", False),
            "adjusted_duration": ("INTEGER", False),
            "coefficient": ("INTEGER", False),
            "skipped": ("INTEGER", False),
            "skip_reason": ("TEXT", False),
            "weather_data": ("TEXT", False),
            "created_at": ("NUMERIC", False),
        },
        "weather_decisions": {
            "id": ("INTEGER", False),
            "date": ("TEXT", True),
            "time": ("TEXT", True),
            "temperature": ("REAL", False),
            "humidity": ("REAL", False),
            "precipitation_24h": ("REAL", False),
            "wind_speed": ("REAL", False),
            "coefficient": ("INTEGER", True),
            "decision": ("TEXT", True),
            "reason": ("TEXT", False),
            "mode": ("TEXT", True),
            "data_sources": ("TEXT", False),
            "user_override": ("INTEGER", False),
            "created_at": ("NUMERIC", False),
        },
        "weather_balance_log": {
            "id": ("INTEGER", False),
            "date": ("TEXT", False),
            "et0_fact": ("REAL", False),
            "et0_norm": ("REAL", False),
            "precip_fact": ("REAL", False),
            "precip_eff": ("REAL", False),
            "deficit_day": ("REAL", False),
            "deficit_window": ("REAL", False),
            "coefficient": ("INTEGER", False),
            "created_at": ("NUMERIC", False),
        },
        "audit_log": {
            "id": ("INTEGER", False),
            "ts": ("NUMERIC", True),
            "actor": ("TEXT", False),
            "source": ("TEXT", True),
            "action_type": ("TEXT", True),
            "target": ("TEXT", False),
            "payload_json": ("TEXT", False),
            "result": ("TEXT", False),
            "error_msg": ("TEXT", False),
            "ip": ("TEXT", False),
            "duration_ms": ("INTEGER", False),
        },
    }
    # Every canonical column not listed here must have no DEFAULT clause. Keep
    # defaults separate from the exact named-column contract so both dimensions
    # are reviewed explicitly when USER_VERSION changes.
    _REQUIRED_COLUMN_DEFAULTS: dict[str, dict[str, str]] = {
        "audit_log": {"ts": "CURRENT_TIMESTAMP"},
        "bot_audit": {"ts": "CURRENT_TIMESTAMP"},
        "bot_idempotency": {"created_at": "CURRENT_TIMESTAMP"},
        "bot_subscriptions": {"enabled": "1", "created_at": "CURRENT_TIMESTAMP"},
        "bot_users": {
            "role": "'user'",
            "is_authorized": "0",
            "failed_attempts": "0",
            "created_at": "CURRENT_TIMESTAMP",
            "notif_critical": "1",
            "notif_emergency": "1",
            "notif_postpone": "1",
            "notif_zone_events": "0",
            "notif_rain": "0",
        },
        "float_events": {"created_at": "datetime('now', 'localtime')"},
        "groups": {
            "created_at": "CURRENT_TIMESTAMP",
            "updated_at": "CURRENT_TIMESTAMP",
            "use_rain_sensor": "0",
            "use_master_valve": "0",
            "master_mqtt_topic": '""',
            "master_mode": '"NC"',
            "master_close_delay_sec": "60",
            "use_pressure_sensor": "0",
            "pressure_mqtt_topic": '""',
            "pressure_unit": '"bar"',
            "use_water_meter": "0",
            "water_mqtt_topic": '""',
            "water_pulse_size": '"1l"',
            "water_base_value_m3": "0",
            "water_base_pulses": "0",
            "float_enabled": "0",
            "float_mqtt_topic": "NULL",
            "float_mqtt_server_id": "NULL",
            "float_mode": "'NO'",
            "float_timeout_minutes": "30",
            "float_debounce_seconds": "5",
        },
        "logs": {"timestamp": "CURRENT_TIMESTAMP"},
        "migrations": {"applied_at": "CURRENT_TIMESTAMP"},
        "mqtt_servers": {
            "port": "1883",
            "enabled": "1",
            "created_at": "CURRENT_TIMESTAMP",
            "updated_at": "CURRENT_TIMESTAMP",
            "tls_enabled": "0",
            "tls_insecure": "0",
        },
        "program_cancellations": {"created_at": "CURRENT_TIMESTAMP"},
        "program_queue_log": {"created_at": "datetime('now', 'localtime')"},
        "programs": {
            "created_at": "CURRENT_TIMESTAMP",
            "updated_at": "CURRENT_TIMESTAMP",
            "type": "'time-based'",
            "schedule_type": "'weekdays'",
            "interval_days": "NULL",
            "even_odd": "NULL",
            "color": "'#42a5f5'",
            "enabled": "1",
            "extra_times": "'[]'",
        },
        "retired_entity_ids": {"retired_at": "CURRENT_TIMESTAMP"},
        "water_usage": {"timestamp": "CURRENT_TIMESTAMP"},
        "weather_balance_log": {"created_at": "CURRENT_TIMESTAMP"},
        "weather_decisions": {
            "mode": "'auto'",
            "data_sources": "'{}'",
            "user_override": "0",
            "created_at": "CURRENT_TIMESTAMP",
        },
        "weather_log": {"skipped": "0", "created_at": "CURRENT_TIMESTAMP"},
        "zone_runs": {
            "created_at": "CURRENT_TIMESTAMP",
            "updated_at": "CURRENT_TIMESTAMP",
            "confirmed": "0",
        },
        "zones": {
            "state": "'off'",
            "icon": "'🌿'",
            "duration": "10",
            "group_id": "1",
            "created_at": "CURRENT_TIMESTAMP",
            "updated_at": "CURRENT_TIMESTAMP",
            "version": "0",
            "fault_count": "0",
            "pause_remaining_seconds": "NULL",
            "pause_reason": "NULL",
        },
    }
    _REQUIRED_PRIMARY_KEYS = {
        "zones": ("id",),
        "groups": ("id",),
        "programs": ("id",),
        "settings": ("key",),
        "migrations": ("name",),
        "mqtt_servers": ("id",),
        "retired_entity_ids": ("entity", "id"),
        "zone_runs": ("id",),
        "program_cancellations": ("program_id", "run_date", "group_id"),
        "program_queue_log": ("id",),
        "float_events": ("id",),
        "logs": ("id",),
        "water_usage": ("id",),
        "bot_users": ("id",),
        "bot_subscriptions": ("id",),
        "bot_audit": ("id",),
        "bot_idempotency": ("token",),
        "weather_cache": ("id",),
        "weather_log": ("id",),
        "weather_decisions": ("id",),
        "weather_balance_log": ("id",),
        "audit_log": ("id",),
    }
    _REQUIRED_UNIQUE_KEYS = {
        "groups": frozenset({("name",)}),
        "bot_users": frozenset({("chat_id",)}),
    }
    _REQUIRED_AUTOINCREMENT_TABLES = frozenset(
        {
            "zones",
            "groups",
            "programs",
            "mqtt_servers",
            "zone_runs",
            "program_queue_log",
            "float_events",
            "logs",
            "water_usage",
            "bot_users",
            "bot_subscriptions",
            "bot_audit",
            "weather_cache",
            "weather_log",
            "weather_decisions",
            "weather_balance_log",
            "audit_log",
        }
    )
    _REQUIRED_BACKUP_MIGRATIONS = frozenset(
        {
            "days_format",
            "zones_add_postpone_reason",
            "zones_add_watering_start_time",
            "zones_add_scheduled_start_time",
            "zones_add_last_watering_time",
            "create_mqtt_servers",
            "zones_add_mqtt_server_id",
            "ensure_group_999",
            "zones_add_indexes",
            "groups_add_use_rain",
            "zones_add_watering_start_source",
            "mqtt_add_tls_options",
            "zones_add_control_fields",
            "zones_add_commanded_observed",
            "groups_add_master_and_sensors",
            "groups_add_master_valve_observed",
            "groups_add_master_close_delay_sec",
            "groups_add_water_meter_extended",
            "zones_add_water_stats",
            "create_zone_runs_v1",
            "telegram_add_settings_fields",
            "telegram_create_bot_users",
            "telegram_create_bot_subscriptions",
            "telegram_create_bot_audit",
            "telegram_add_fsm_and_notif",
            "telegram_create_bot_idempotency",
            "encrypt_mqtt_passwords",
            "zones_add_fault_tracking",
            "weather_create_cache",
            "weather_create_log",
            "weather_add_settings",
            "weather_create_decisions",
            "weather_add_extended_settings",
            "weather_wind_kmh_to_ms",
            "weather_add_balance_settings",
            "weather_create_balance_log",
            "queue_and_float_support",
            "programs_v2_fields",
            "create_audit_log",
            "backfill_last_watering_from_zone_runs",
            "zones_drop_last_watering_time",
            "zones_add_photo_thumb",
            "zone_runs_add_source",
            "zone_runs_backfill_source",
            "zone_runs_add_confirmed",
            "programs_canonical_even_odd",
            "durable_entity_ids_v1",
            "durable_entity_ids_v2",
            "program_cancellations_fk_v1",
            "restore_runtime_indexes_v1",
            "mqtt_reference_integrity_v1",
            "group_reference_integrity_v1",
            "zone_runs_clear_unverifiable_source_v1",
            "programs_disable_unsupported_smart_v1",
            "zones_version_invalidation_v1",
        }
    )
    _REQUIRED_INTEGRITY_TRIGGERS = {
        **{
            f"trg_{table}_{suffix}": table
            for table in ("zones", "groups", "programs", "mqtt_servers")
            for suffix in (
                "retire_id",
                "reject_out_of_range_id",
                "reject_explicit_id_boundary",
                "reject_id_update",
                "reject_retired_id",
            )
        },
        "trg_zones_mqtt_server_insert": "zones",
        "trg_zones_mqtt_server_update": "zones",
        "trg_groups_mqtt_server_insert": "groups",
        "trg_groups_mqtt_server_update": "groups",
        "trg_settings_mqtt_server_insert": "settings",
        "trg_settings_mqtt_server_update": "settings",
        "trg_mqtt_servers_restrict_referenced_delete": "mqtt_servers",
        "trg_zones_group_insert": "zones",
        "trg_zones_group_update": "zones",
        "trg_groups_restrict_referenced_delete": "groups",
        "trg_groups_restrict_reserved_delete": "groups",
        "trg_groups_reject_replacing_name": "groups",
        "trg_groups_reject_replacing_name_update": "groups",
        "trg_zones_version_invalidate": "zones",
    }
    _REQUIRED_TRIGGER_SQL = _required_integrity_trigger_sql()
    _REQUIRED_INDEXES: dict[str, tuple[str, tuple[str, ...]]] = {
        "idx_zones_group": ("zones", ("group_id",)),
        "idx_zones_mqtt_server": ("zones", ("mqtt_server_id",)),
        "idx_zones_topic": ("zones", ("topic",)),
        "idx_logs_type": ("logs", ("type",)),
        "idx_logs_timestamp": ("logs", ("timestamp",)),
        "idx_water_zone": ("water_usage", ("zone_id",)),
        "idx_water_timestamp": ("water_usage", ("timestamp",)),
        "idx_zone_runs_zone": ("zone_runs", ("zone_id",)),
        "idx_zone_runs_group": ("zone_runs", ("group_id",)),
        "idx_zone_runs_active": ("zone_runs", ("zone_id", "end_utc")),
        "idx_zone_runs_last_ok": ("zone_runs", ("zone_id", "end_utc")),
        "idx_zone_runs_start": ("zone_runs", ("start_utc",)),
        "idx_zone_runs_group_start": ("zone_runs", ("group_id", "start_utc")),
        "idx_zone_runs_zone_start": ("zone_runs", ("zone_id", "start_utc")),
        "idx_bot_users_chat": ("bot_users", ("chat_id",)),
        "idx_bot_subs_user": ("bot_subscriptions", ("user_id",)),
        "idx_bot_audit_user": ("bot_audit", ("user_id",)),
        "idx_bot_idemp_chat": ("bot_idempotency", ("chat_id",)),
        "idx_weather_cache_loc": ("weather_cache", ("latitude", "longitude")),
        "idx_weather_cache_time": ("weather_cache", ("fetched_at",)),
        "idx_weather_log_zone": ("weather_log", ("zone_id",)),
        "idx_weather_log_time": ("weather_log", ("created_at",)),
        "idx_weather_decisions_date": ("weather_decisions", ("date",)),
        "idx_weather_decisions_created": ("weather_decisions", ("created_at",)),
        "idx_weather_balance_log_date": ("weather_balance_log", ("date",)),
        "idx_weather_balance_log_created": ("weather_balance_log", ("created_at",)),
        "idx_pql_program": ("program_queue_log", ("program_id",)),
        "idx_pql_state": ("program_queue_log", ("state",)),
        "idx_float_events_group": ("float_events", ("group_id",)),
        "idx_audit_log_ts": ("audit_log", ("ts",)),
        "idx_audit_log_action": ("audit_log", ("action_type",)),
        "idx_audit_log_target": ("audit_log", ("target",)),
    }
    _REQUIRED_INDEX_DESCENDING = {
        "idx_zone_runs_last_ok": (False, True),
        "idx_zone_runs_start": (True,),
        "idx_zone_runs_group_start": (False, True),
    }
    _REQUIRED_INDEX_PREDICATES = {
        "idx_zone_runs_last_ok": "STATUS = 'ok' AND END_UTC IS NOT NULL",
    }
    # (referenced table, local column, referenced column,
    #  ON UPDATE action, ON DELETE action, MATCH mode)
    _REQUIRED_FOREIGN_KEYS = {
        "program_cancellations": frozenset({("programs", "program_id", "id", "NO ACTION", "CASCADE", "NONE")}),
        "bot_subscriptions": frozenset({("bot_users", "user_id", "id", "NO ACTION", "CASCADE", "NONE")}),
        "bot_audit": frozenset({("bot_users", "user_id", "id", "NO ACTION", "SET NULL", "NONE")}),
    }

    def __init__(self, db_path: str, backup_dir: str = "backups"):
        super().__init__(db_path)
        self.backup_dir = backup_dir

    def get_logs(
        self, event_type: str | None = None, from_date: str | None = None, to_date: str | None = None
    ) -> list[dict[str, Any]]:
        """Получить логи с фильтрацией."""
        try:
            with self._connect() as conn:
                conn.row_factory = sqlite3.Row
                query = (
                    "SELECT id, type, details, "
                    "strftime('%Y-%m-%d %H:%M:%S', timestamp, 'localtime') AS timestamp "
                    "FROM logs WHERE 1=1"
                )
                params = []

                if event_type:
                    query += " AND type = ?"
                    params.append(event_type)
                if from_date:
                    query += " AND date(timestamp, 'localtime') >= date(?)"
                    params.append(from_date)
                if to_date:
                    query += " AND date(timestamp, 'localtime') <= date(?)"
                    params.append(to_date)

                query += " ORDER BY timestamp DESC LIMIT 1000"
                cursor = conn.execute(query, params)
                return [dict(row) for row in cursor.fetchall()]
        except sqlite3.Error as e:
            logger.error("Ошибка получения логов: %s", e)
            return []

    @retry_on_busy()
    def add_log(self, log_type: str, details: str | None = None) -> int | None:
        """Добавить запись в лог."""
        try:
            with self._connect() as conn:
                cursor = conn.execute(
                    """
                    INSERT INTO logs (type, details)
                    VALUES (?, ?)
                """,
                    (log_type, details),
                )
                log_id = cursor.lastrowid
                conn.commit()
                return log_id
        except sqlite3.Error as e:
            logger.error("Ошибка добавления лога: %s", e)
            return None

    def get_water_usage(self, days: int = 7, zone_id: int | None = None) -> list[dict[str, Any]]:
        """Получить данные расхода воды."""
        try:
            days = int(days)
            day_modifier = f"-{days} days"
            with self._connect() as conn:
                conn.row_factory = sqlite3.Row
                if zone_id:
                    cursor = conn.execute(
                        """
                        SELECT w.*, z.name as zone_name
                        FROM water_usage w
                        LEFT JOIN zones z ON w.zone_id = z.id
                        WHERE w.zone_id = ? AND w.timestamp >= datetime('now', ?)
                        ORDER BY w.timestamp DESC
                    """,
                        (zone_id, day_modifier),
                    )
                else:
                    cursor = conn.execute(
                        """
                        SELECT w.*, z.name as zone_name
                        FROM water_usage w
                        LEFT JOIN zones z ON w.zone_id = z.id
                        WHERE w.timestamp >= datetime('now', ?)
                        ORDER BY w.timestamp DESC
                    """,
                        (day_modifier,),
                    )
                return [dict(row) for row in cursor.fetchall()]
        except sqlite3.Error as e:
            logger.error("Ошибка получения данных расхода воды: %s", e)
            return []

    @retry_on_busy()
    def add_water_usage(self, zone_id: int, liters: float) -> bool:
        """Добавить запись о расходе воды."""
        try:
            with self._connect() as conn:
                conn.execute(
                    """
                    INSERT INTO water_usage (zone_id, liters)
                    VALUES (?, ?)
                """,
                    (zone_id, liters),
                )
                conn.commit()
                return True
        except sqlite3.Error as e:
            logger.error("Ошибка добавления записи расхода воды: %s", e)
            return False

    def get_water_statistics(self, days: int = 30) -> dict[str, Any]:
        """Получить статистику расхода воды."""
        try:
            days = int(days)
            day_modifier = f"-{days} days"
            with self._connect() as conn:
                cursor = conn.execute(
                    """
                    SELECT SUM(liters) as total_liters
                    FROM water_usage
                    WHERE timestamp >= datetime('now', ?)
                """,
                    (day_modifier,),
                )
                total_liters = cursor.fetchone()[0] or 0

                cursor = conn.execute(
                    """
                    SELECT z.name, SUM(w.liters) as liters
                    FROM water_usage w
                    LEFT JOIN zones z ON w.zone_id = z.id
                    WHERE w.timestamp >= datetime('now', ?)
                    GROUP BY w.zone_id, z.name
                    ORDER BY liters DESC
                """,
                    (day_modifier,),
                )
                zone_usage = [dict(row) for row in cursor.fetchall()]

                cursor = conn.execute(
                    """
                    SELECT AVG(daily_liters) as avg_daily
                    FROM (
                        SELECT DATE(timestamp) as date, SUM(liters) as daily_liters
                        FROM water_usage
                        WHERE timestamp >= datetime('now', ?)
                        GROUP BY DATE(timestamp)
                    )
                """,
                    (day_modifier,),
                )
                avg_daily = cursor.fetchone()[0] or 0

                return {
                    "total_liters": round(total_liters, 2),
                    "avg_daily": round(avg_daily, 2),
                    "zone_usage": zone_usage,
                    "period_days": days,
                }
        except sqlite3.Error as e:
            logger.error("Ошибка получения статистики воды: %s", e)
            return {"total_liters": 0, "avg_daily": 0, "zone_usage": [], "period_days": days}

    def create_backup(self) -> str | None:
        """Create a WAL-consistent, validated SQLite snapshot.

        Copying only the main ``.db`` file is never a valid fallback while
        WAL mode is active: committed pages may still live exclusively in
        ``-wal``. Both strategies below ask SQLite itself for one coherent
        read snapshot; if both fail, backup creation fails closed.
        """
        try:
            if not self._is_valid_backup_source(self.db_path):
                return None

            self._ensure_private_backup_directory()

            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
            backup_path = os.path.join(self.backup_dir, f"irrigation_backup_{timestamp}.db")
            partial_path = backup_path + ".partial"

            try:
                self._backup_via_api(self.db_path, partial_path)
            except (sqlite3.Error, OSError) as api_error:
                logger.warning("SQLite backup API failed, trying VACUUM INTO: %s", api_error)
                self._remove_incomplete_backup(partial_path)
                try:
                    self._backup_via_vacuum(self.db_path, partial_path)
                except (sqlite3.Error, OSError) as vacuum_error:
                    logger.error(
                        "Both WAL-consistent backup methods failed (backup=%s, vacuum=%s)",
                        api_error,
                        vacuum_error,
                    )
                    self._remove_incomplete_backup(partial_path)
                    return None

            try:
                # The containing directory is private throughout creation;
                # chmod before validation/rename makes the final file 0600
                # even if the process was started with a permissive umask.
                os.chmod(partial_path, 0o600)
            except OSError as e:
                logger.error("Не удалось защитить права бэкапа %s: %s", partial_path, e)
                self._remove_incomplete_backup(partial_path)
                return None

            published = False
            try:
                self.validate_application_database(partial_path)
                self._fsync_file(partial_path)
                # Probe directory durability before making the final name
                # visible.  The second sync below is still authoritative: a
                # later I/O error cannot be predicted, but in that case the
                # already-published valid backup must never be unlinked.
                self._fsync_directory(self.backup_dir)
                os.replace(partial_path, backup_path)
                published = True
                self._remove_sqlite_sidecars(partial_path)
                self._fsync_directory(self.backup_dir)
            except (sqlite3.Error, OSError) as e:
                logger.error("Backup validation or publication failed: %s", e)
                if published:
                    logger.error(
                        "Backup %s was published but its directory sync failed; preserving the valid file",
                        backup_path,
                    )
                else:
                    self._remove_incomplete_backup(partial_path)
                return None

            self._cleanup_old_backups(protected_path=backup_path)
            logger.info("Резервная копия создана: %s", backup_path)
            return backup_path
        except OSError as e:
            logger.error("Ошибка создания резервной копии: %s", e)
            return None

    @staticmethod
    def _backup_via_api(source_path: str, target_path: str) -> None:
        with sqlite3.connect(LogRepository._read_only_uri(source_path), timeout=30, uri=True) as source:
            source.execute("PRAGMA busy_timeout=30000")
            with sqlite3.connect(target_path, timeout=30) as target:
                source.backup(target)
                # backup() also copies the source journal-mode header. Convert
                # the finished snapshot back to a standalone main DB through
                # SQLite itself so restore never depends on sidecar files.
                target.execute("PRAGMA journal_mode=DELETE")

    @staticmethod
    def _backup_via_vacuum(source_path: str, target_path: str) -> None:
        with sqlite3.connect(LogRepository._read_only_uri(source_path), timeout=30, uri=True) as source:
            source.execute("PRAGMA busy_timeout=30000")
            source.execute("VACUUM INTO ?", (target_path,))

    @staticmethod
    def _read_only_uri(path: str) -> str:
        return f"file:{quote(os.path.abspath(path), safe='/')}?mode=ro"

    @staticmethod
    def _fsync_file(path: str) -> None:
        descriptor = os.open(path, os.O_RDONLY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)

    @staticmethod
    def _fsync_directory(path: str) -> None:
        flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
        descriptor = os.open(path, flags)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)

    def _ensure_private_backup_directory(self) -> None:
        """Create the backup path and durably publish every new directory."""

        if not self.backup_dir:
            raise OSError("backup directory path is empty")
        absolute_path = os.path.abspath(self.backup_dir)
        missing_paths: list[str] = []
        probe = absolute_path
        while not os.path.exists(probe):
            missing_paths.append(probe)
            parent = os.path.dirname(probe)
            if parent == probe:
                raise OSError(f"cannot resolve backup directory parent for {absolute_path!r}")
            probe = parent

        os.makedirs(absolute_path, mode=0o700, exist_ok=True)
        for created_path in reversed(missing_paths):
            os.chmod(created_path, 0o700)
            # mkdir durability lives in the containing directory, not in the
            # newly created directory itself. Sync each parent from the first
            # new component down to the configured leaf.
            self._fsync_directory(os.path.dirname(created_path))
        os.chmod(absolute_path, 0o700)

    @staticmethod
    def _quoted_identifier(value: str) -> str:
        return f'"{value.replace(chr(34), chr(34) * 2)}"'

    @classmethod
    def _validate_table_contract(cls, conn: sqlite3.Connection, table: str) -> None:
        quoted_table = cls._quoted_identifier(table)
        rows = conn.execute(f"PRAGMA main.table_xinfo({quoted_table})").fetchall()
        columns = {str(row[1]).casefold(): row for row in rows}
        required = cls._REQUIRED_BACKUP_COLUMNS[table]
        actual_columns = set(columns)
        expected_columns = set(required)
        missing_columns = sorted(expected_columns - actual_columns)
        if missing_columns:
            raise sqlite3.DatabaseError(f"application table {table!r} is missing columns: {missing_columns!r}")
        unexpected_columns = sorted(actual_columns - expected_columns)
        if unexpected_columns:
            raise sqlite3.DatabaseError(f"application table {table!r} has unexpected columns: {unexpected_columns!r}")

        table_list_row = next(
            (
                row
                for row in conn.execute("PRAGMA main.table_list").fetchall()
                if str(row[1]) == table and str(row[2]).casefold() == "table"
            ),
            None,
        )
        if (
            table_list_row is None
            or int(table_list_row[3]) != len(required)
            or bool(table_list_row[4])
            or bool(table_list_row[5])
        ):
            raise sqlite3.DatabaseError(f"application table {table!r} has invalid table options")

        schema_row = conn.execute(
            "SELECT sql FROM main.sqlite_master WHERE type = 'table' AND name = ?",
            (table,),
        ).fetchone()
        schema_sql = str(schema_row[0]) if schema_row and schema_row[0] else ""
        schema_code = _schema_sql_code(schema_sql)
        unsupported_constraint = re.search(
            r"\b(?:ASC|CHECK|COLLATE|CONSTRAINT|DESC|GENERATED)\b|"
            r"\bON\s+CONFLICT\b|\bWITHOUT\s+ROWID\b|\bSTRICT\b",
            schema_code,
        )
        if unsupported_constraint is not None:
            raise sqlite3.DatabaseError(
                f"application table {table!r} has unexpected constraint or option {unsupported_constraint.group(0)!r}"
            )

        for column, (expected_type_family, expected_not_null) in required.items():
            row = columns[column]
            expected_declared_type = "TIMESTAMP" if expected_type_family == "NUMERIC" else expected_type_family
            actual_declared_type = " ".join(str(row[2]).upper().split())
            if actual_declared_type != expected_declared_type:
                raise sqlite3.DatabaseError(
                    f"application table {table!r} column {column!r} has declared type "
                    f"{actual_declared_type!r}, expected {expected_declared_type!r}"
                )
            if bool(row[3]) != expected_not_null:
                qualifier = "missing" if expected_not_null else "unexpected"
                raise sqlite3.DatabaseError(
                    f"application table {table!r} column {column!r} has {qualifier} NOT NULL constraint"
                )
            if len(row) <= 6 or int(row[6]) != 0:
                raise sqlite3.DatabaseError(
                    f"application table {table!r} column {column!r} has invalid hidden/generated state"
                )
            expected_default = cls._REQUIRED_COLUMN_DEFAULTS.get(table, {}).get(column)
            actual_default = _normalize_default_sql(row[4])
            if actual_default != expected_default:
                raise sqlite3.DatabaseError(
                    f"application table {table!r} column {column!r} has default "
                    f"{actual_default!r}, expected {expected_default!r}"
                )

        actual_primary_key = tuple(
            str(row[1]).casefold() for row in sorted((row for row in rows if row[5]), key=lambda row: row[5])
        )
        expected_primary_key = cls._REQUIRED_PRIMARY_KEYS[table]
        if actual_primary_key != expected_primary_key:
            raise sqlite3.DatabaseError(
                f"application table {table!r} has primary key {actual_primary_key!r}, expected {expected_primary_key!r}"
            )

        required_unique_keys = cls._REQUIRED_UNIQUE_KEYS.get(table, frozenset())
        actual_unique_keys: list[tuple[str, ...]] = []
        for index in conn.execute(f"PRAGMA main.index_list({quoted_table})").fetchall():
            # PRIMARY KEY indexes are already covered by the exact PK contract.
            if not bool(index[2]) or str(index[3]).casefold() == "pk":
                continue
            index_name = str(index[1])
            if len(index) > 4 and bool(index[4]):
                raise sqlite3.DatabaseError(
                    f"application index {index_name!r} has invalid unexpected unique constraint on table {table!r}"
                )
            quoted_index = cls._quoted_identifier(index_name)
            key_rows = [
                row
                for row in conn.execute(f"PRAGMA main.index_xinfo({quoted_index})").fetchall()
                if len(row) <= 5 or bool(row[5])
            ]
            if any(row[2] is None or bool(row[3]) or str(row[4]).upper() != "BINARY" for row in key_rows):
                raise sqlite3.DatabaseError(
                    f"application index {index_name!r} has invalid unexpected unique constraint on table {table!r}"
                )
            index_columns = tuple(str(row[2]).casefold() for row in key_rows)
            if index_columns not in required_unique_keys:
                raise sqlite3.DatabaseError(
                    f"application index {index_name!r} has invalid unexpected unique constraint on table {table!r}"
                )
            actual_unique_keys.append(index_columns)
        if len(actual_unique_keys) != len(set(actual_unique_keys)):
            raise sqlite3.DatabaseError(f"application table {table!r} has duplicate unexpected unique constraints")
        missing_unique = required_unique_keys - set(actual_unique_keys)
        if missing_unique:
            raise sqlite3.DatabaseError(
                f"application table {table!r} is missing unique constraint: {sorted(missing_unique)!r}"
            )

        if table in cls._REQUIRED_AUTOINCREMENT_TABLES:
            # AUTOINCREMENT is meaningful only on an INTEGER PRIMARY KEY.  The
            # PRAGMA checks above prove that ``id`` is that sole key; this
            # grammar-aware check proves rowid reuse protection is enabled.
            if not _has_canonical_autoincrement_id(schema_sql):
                raise sqlite3.DatabaseError(f"application table {table!r} is missing AUTOINCREMENT constraint")
        elif re.search(r"\bAUTOINCREMENT\b", schema_code) is not None:
            raise sqlite3.DatabaseError(f"application table {table!r} has unexpected AUTOINCREMENT constraint")

    @classmethod
    def _validate_integrity_constraints(cls, conn: sqlite3.Connection) -> None:
        allowed_entities = tuple(entity for _table, entity in _DURABLE_ENTITIES)
        placeholders = ", ".join("?" for _ in allowed_entities)
        invalid_retired_metadata = conn.execute(
            "SELECT entity, id, typeof(id) FROM retired_entity_ids "
            f"WHERE entity NOT IN ({placeholders}) OR typeof(id) != 'integer' OR id <= 0 OR id > ? "
            "ORDER BY entity, id LIMIT 1",
            (*allowed_entities, MAX_ENTITY_ID),
        ).fetchone()
        if invalid_retired_metadata is not None:
            raise sqlite3.DatabaseError(
                f"retired {invalid_retired_metadata[0]} identifier {invalid_retired_metadata[1]!r} "
                f"is out of range or has invalid storage ({invalid_retired_metadata[2]!r})"
            )

        for table, entity in _DURABLE_ENTITIES:
            invalid_live = conn.execute(
                f"SELECT id FROM {table} WHERE id <= 0 OR id > ? ORDER BY id LIMIT 1",
                (MAX_ENTITY_ID,),
            ).fetchone()
            if invalid_live is not None:
                raise sqlite3.DatabaseError(f"{table} contains out-of-range durable identifier {invalid_live[0]!r}")
            collision = conn.execute(
                f"SELECT current.id FROM {table} current "
                "JOIN retired_entity_ids retired ON retired.entity = ? AND retired.id = current.id "
                "ORDER BY current.id LIMIT 1",
                (entity,),
            ).fetchone()
            if collision is not None:
                raise sqlite3.DatabaseError(f"live {entity} identifier {collision[0]!r} is also marked retired")
            sequence_rows = conn.execute(
                "SELECT seq, typeof(seq) FROM sqlite_sequence WHERE name = ?",
                (table,),
            ).fetchall()
            if len(sequence_rows) != 1:
                raise sqlite3.DatabaseError(
                    f"sqlite_sequence for {table} must contain exactly one row, found {len(sequence_rows)}"
                )
            sequence_value, sequence_type = sequence_rows[0]
            if sequence_type != "integer" or not isinstance(sequence_value, int):
                raise sqlite3.DatabaseError(f"sqlite_sequence for {table} is missing or invalid")
            sequence = int(sequence_value)
            if not 0 <= sequence <= MAX_ENTITY_ID:
                raise sqlite3.DatabaseError(
                    f"sqlite_sequence for {table} is outside the supported durable identifier range: {sequence!r}"
                )
            live_max = int(conn.execute(f"SELECT COALESCE(MAX(id), 0) FROM {table}").fetchone()[0])
            retired_max = int(
                conn.execute(
                    "SELECT COALESCE(MAX(id), 0) FROM retired_entity_ids WHERE entity = ?",
                    (entity,),
                ).fetchone()[0]
            )
            if sequence < max(live_max, retired_max):
                raise sqlite3.DatabaseError(
                    f"sqlite_sequence for {table} is below its durable identifier high-water mark"
                )

        missing_reserved_group = conn.execute(
            "SELECT required.id FROM (SELECT 1 AS id UNION ALL SELECT 999) required "
            "LEFT JOIN groups ON groups.id = required.id WHERE groups.id IS NULL ORDER BY required.id LIMIT 1"
        ).fetchone()
        if missing_reserved_group is not None:
            raise sqlite3.DatabaseError(f"application reserved group {missing_reserved_group[0]!r} is missing")

        durable_tables = {table for table, _entity in _DURABLE_ENTITIES}
        for table in sorted(cls._REQUIRED_AUTOINCREMENT_TABLES - durable_tables):
            sequence_rows = conn.execute(
                "SELECT seq, typeof(seq) FROM sqlite_sequence WHERE name = ?",
                (table,),
            ).fetchall()
            if len(sequence_rows) > 1:
                raise sqlite3.DatabaseError(
                    f"sqlite_sequence for {table} must contain at most one row, found {len(sequence_rows)}"
                )
            live_max = int(conn.execute(f"SELECT COALESCE(MAX(id), 0) FROM {table}").fetchone()[0])
            if not sequence_rows:
                if live_max:
                    raise sqlite3.DatabaseError(f"sqlite_sequence for non-empty table {table} is missing")
                continue
            sequence_value, sequence_type = sequence_rows[0]
            if (
                sequence_type != "integer"
                or not isinstance(sequence_value, int)
                or not 0 <= sequence_value < _SQLITE_MAX_ROWID
            ):
                raise sqlite3.DatabaseError(f"sqlite_sequence for {table} has invalid value {sequence_value!r}")
            if sequence_value < live_max:
                raise sqlite3.DatabaseError(f"sqlite_sequence for {table} is below its live identifier high-water mark")

        dangling_groups = conn.execute(
            "SELECT zones.id, zones.group_id FROM zones "
            "LEFT JOIN groups ON groups.id = zones.group_id "
            "WHERE zones.group_id IS NOT NULL AND zones.group_id != 0 AND groups.id IS NULL "
            "ORDER BY zones.id LIMIT 20"
        ).fetchall()
        if dangling_groups:
            raise sqlite3.DatabaseError(f"zones contain dangling group references: {dangling_groups!r}")

        cancellations_info = conn.execute("PRAGMA main.table_info(program_cancellations)").fetchall()
        if not cancellations_info:
            raise sqlite3.DatabaseError("application schema is missing table 'program_cancellations'")
        cancellation_pk = tuple(
            str(row[1]).casefold()
            for row in sorted((row for row in cancellations_info if row[5]), key=lambda row: row[5])
        )
        if cancellation_pk != ("program_id", "run_date", "group_id"):
            raise sqlite3.DatabaseError("program_cancellations has an invalid primary key")

        for table in sorted(cls._REQUIRED_BACKUP_TABLES):
            required_foreign_keys = cls._REQUIRED_FOREIGN_KEYS.get(table, frozenset())
            quoted_table = cls._quoted_identifier(table)
            foreign_key_rows = conn.execute(f"PRAGMA main.foreign_key_list({quoted_table})").fetchall()
            actual_foreign_key_list = [
                (
                    str(row[2]).casefold(),
                    str(row[3]).casefold(),
                    str(row[4]).casefold(),
                    str(row[5]).upper(),
                    str(row[6]).upper(),
                    str(row[7]).upper(),
                )
                for row in foreign_key_rows
            ]
            actual_foreign_keys = set(actual_foreign_key_list)
            table_sql_row = conn.execute(
                "SELECT sql FROM main.sqlite_master WHERE type = 'table' AND name = ?",
                (table,),
            ).fetchone()
            table_sql = str(table_sql_row[0]) if table_sql_row and table_sql_row[0] else ""
            unsupported_clause = re.search(
                r"\b(?:MATCH\s+[A-Z_][A-Z0-9_$]*|(?:NOT\s+)?DEFERRABLE)\b",
                _schema_sql_code(table_sql),
            )
            missing_foreign_keys = required_foreign_keys - actual_foreign_keys
            if missing_foreign_keys:
                raise sqlite3.DatabaseError(
                    f"application table {table!r} is missing foreign keys: {sorted(missing_foreign_keys)!r}"
                )
            if (
                len(actual_foreign_key_list) != len(actual_foreign_keys)
                or actual_foreign_keys != required_foreign_keys
                or unsupported_clause is not None
            ):
                raise sqlite3.DatabaseError(
                    f"application table {table!r} has invalid foreign keys {sorted(actual_foreign_keys)!r}, "
                    f"expected {sorted(required_foreign_keys)!r}"
                )

        persisted_tables = {
            str(row[0])
            for row in conn.execute(
                "SELECT name FROM main.sqlite_master WHERE type = 'table' AND lower(substr(name, 1, 7)) != 'sqlite_'"
            ).fetchall()
        }
        unexpected_tables = sorted(persisted_tables - cls._REQUIRED_BACKUP_TABLES)
        if unexpected_tables:
            raise sqlite3.DatabaseError(f"application schema has unexpected tables: {unexpected_tables!r}")

        unexpected_views = sorted(
            str(row[0])
            for row in conn.execute(
                "SELECT name FROM main.sqlite_master WHERE type = 'view' AND lower(substr(name, 1, 7)) != 'sqlite_'"
            ).fetchall()
        )
        if unexpected_views:
            raise sqlite3.DatabaseError(f"application schema has unexpected views: {unexpected_views!r}")

        triggers = {
            str(row[0]): (str(row[1]), row[2])
            for row in conn.execute(
                "SELECT name, tbl_name, sql FROM main.sqlite_master WHERE type = 'trigger'"
            ).fetchall()
        }
        for trigger, expected_table in cls._REQUIRED_INTEGRITY_TRIGGERS.items():
            trigger_row = triggers.get(trigger)
            if trigger_row is None or trigger_row[0] != expected_table or not trigger_row[1]:
                raise sqlite3.DatabaseError(
                    f"application schema is missing integrity trigger {trigger!r} on {expected_table!r}"
                )
            normalized_trigger_sql = _normalize_schema_sql(trigger_row[1])
            if normalized_trigger_sql != cls._REQUIRED_TRIGGER_SQL[trigger]:
                raise sqlite3.DatabaseError(f"application integrity trigger {trigger!r} has an invalid definition")
        unexpected_triggers = sorted(set(triggers) - set(cls._REQUIRED_INTEGRITY_TRIGGERS))
        if unexpected_triggers:
            raise sqlite3.DatabaseError(f"application schema has unexpected triggers: {unexpected_triggers!r}")

        indexes = {
            str(row[0]): (str(row[1]).casefold(), row[2])
            for row in conn.execute(
                "SELECT name, tbl_name, sql FROM main.sqlite_master WHERE type = 'index'"
            ).fetchall()
        }
        unexpected_indexes = sorted(
            name for name, (_table, sql) in indexes.items() if sql and name not in cls._REQUIRED_INDEXES
        )
        if unexpected_indexes:
            raise sqlite3.DatabaseError(f"application schema has unexpected indexes: {unexpected_indexes!r}")
        for index_name, (expected_table, expected_columns) in cls._REQUIRED_INDEXES.items():
            index_row = indexes.get(index_name)
            if index_row is None or index_row[0] != expected_table or not index_row[1]:
                raise sqlite3.DatabaseError(
                    f"application schema is missing required index {index_name!r} on {expected_table!r}"
                )
            quoted_table = cls._quoted_identifier(expected_table)
            metadata = next(
                (
                    row
                    for row in conn.execute(f"PRAGMA main.index_list({quoted_table})").fetchall()
                    if str(row[1]) == index_name
                ),
                None,
            )
            quoted_index = cls._quoted_identifier(index_name)
            key_rows = [
                row
                for row in conn.execute(f"PRAGMA main.index_xinfo({quoted_index})").fetchall()
                if len(row) <= 5 or bool(row[5])
            ]
            actual_columns = tuple(str(row[2]).casefold() for row in key_rows if row[2] is not None)
            actual_descending = tuple(bool(row[3]) for row in key_rows)
            actual_collations = tuple(str(row[4]).upper() for row in key_rows)
            expected_descending = cls._REQUIRED_INDEX_DESCENDING.get(
                index_name,
                (False,) * len(expected_columns),
            )
            expected_predicate = cls._REQUIRED_INDEX_PREDICATES.get(index_name)
            actual_predicate = _normalized_where_predicate(index_row[1])
            expected_partial = expected_predicate is not None
            invalid_flags = (
                metadata is None
                or bool(metadata[2])
                or str(metadata[3]).casefold() != "c"
                or bool(metadata[4]) != expected_partial
            )
            if (
                invalid_flags
                or actual_columns != expected_columns
                or actual_descending != expected_descending
                or actual_collations != ("BINARY",) * len(expected_columns)
                or actual_predicate != expected_predicate
            ):
                raise sqlite3.DatabaseError(f"application index {index_name!r} has an invalid definition")

    @classmethod
    def validate_application_database(cls, path: str) -> None:
        """Validate physical integrity and the stamped application schema."""

        with sqlite3.connect(cls._read_only_uri(path), timeout=30, uri=True) as conn:
            conn.execute("PRAGMA busy_timeout=30000")
            conn.execute("PRAGMA query_only=ON")
            conn.execute("BEGIN")
            integrity = conn.execute("PRAGMA integrity_check").fetchone()
            if integrity != ("ok",):
                raise sqlite3.DatabaseError(f"integrity_check returned {integrity!r}")

            application_id = conn.execute("PRAGMA main.application_id").fetchone()
            if application_id != (APPLICATION_ID,):
                raise sqlite3.DatabaseError(
                    f"unsupported application_id {application_id!r}; expected {(APPLICATION_ID,)!r}"
                )
            user_version = conn.execute("PRAGMA main.user_version").fetchone()
            if user_version != (USER_VERSION,):
                raise sqlite3.DatabaseError(
                    f"unsupported application user_version {user_version!r}; expected {(USER_VERSION,)!r}"
                )
            schema_version = conn.execute("PRAGMA main.schema_version").fetchone()
            if not schema_version or not isinstance(schema_version[0], int) or schema_version[0] <= 0:
                raise sqlite3.DatabaseError(f"invalid schema_version {schema_version!r}")

            tables = {
                str(row[0])
                for row in conn.execute("SELECT name FROM main.sqlite_master WHERE type = 'table'").fetchall()
            }
            missing = cls._REQUIRED_BACKUP_TABLES - tables
            if missing:
                raise sqlite3.DatabaseError(f"application schema is missing tables: {sorted(missing)!r}")
            for table in sorted(cls._REQUIRED_BACKUP_TABLES):
                cls._validate_table_contract(conn, table)
                conn.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()

            migrations = {str(row[0]) for row in conn.execute("SELECT name FROM migrations").fetchall()}
            missing_migrations = cls._REQUIRED_BACKUP_MIGRATIONS - migrations
            if missing_migrations:
                raise sqlite3.DatabaseError(
                    f"application schema is missing migration markers: {sorted(missing_migrations)!r}"
                )
            unexpected_migrations = migrations - cls._REQUIRED_BACKUP_MIGRATIONS
            if unexpected_migrations:
                raise sqlite3.DatabaseError(
                    f"application schema has unexpected migration markers: {sorted(unexpected_migrations)!r}"
                )
            cls._validate_integrity_constraints(conn)

            violations = conn.execute("PRAGMA foreign_key_check").fetchall()
            if violations:
                raise sqlite3.DatabaseError("foreign_key_check returned violations")

    @classmethod
    def _validate_application_database(cls, path: str) -> None:
        """Backward-compatible alias for older callers and focused tests."""

        cls.validate_application_database(path)

    @classmethod
    def _is_valid_backup_source(cls, path: str) -> bool:
        try:
            source_stat = os.stat(path)
            if not stat_module.S_ISREG(source_stat.st_mode) or source_stat.st_size <= 0:
                logger.error("Backup source is not a non-empty regular file: %s", path)
                return False
            cls.validate_application_database(path)
            return True
        except (OSError, sqlite3.Error) as e:
            logger.error("Backup source validation failed for %s: %s", path, e)
            return False

    @staticmethod
    def _remove_incomplete_backup(path: str) -> None:
        LogRepository._remove_sqlite_sidecars(path)
        try:
            os.remove(path)
        except FileNotFoundError:
            pass
        except OSError as e:
            logger.error("Не удалось удалить незавершённый бэкап %s: %s", path, e)

    @staticmethod
    def _remove_sqlite_sidecars(path: str) -> None:
        for suffix in ("-wal", "-shm"):
            sidecar = path + suffix
            try:
                os.remove(sidecar)
            except FileNotFoundError:
                pass
            except OSError as e:
                logger.error("Не удалось удалить sidecar бэкапа %s: %s", sidecar, e)

    def _cleanup_old_backups(self, keep_count: int = 7, protected_path: str | None = None):
        """Удалить старые копии, не затрагивая только что опубликованную."""
        deleted_any = False
        try:
            backup_files = []
            for file in os.listdir(self.backup_dir):
                if file.startswith("irrigation_backup_") and file.endswith(".db"):
                    file_path = os.path.join(self.backup_dir, file)
                    backup_files.append((file_path, os.path.getmtime(file_path)))

            backup_files.sort(key=lambda x: x[1])
            protected = os.path.abspath(protected_path) if protected_path is not None else None
            delete_count = max(0, len(backup_files) - keep_count)
            deletion_candidates = [
                item for item in backup_files if protected is None or os.path.abspath(item[0]) != protected
            ]

            for file_path, _ in deletion_candidates[:delete_count]:
                os.remove(file_path)
                deleted_any = True
                logger.info("Удалена старая резервная копия: %s", file_path)
        except OSError as e:
            logger.error("Ошибка очистки старых резервных копий: %s", e)
        finally:
            if deleted_any:
                try:
                    self._fsync_directory(self.backup_dir)
                except OSError as e:
                    logger.error("Ошибка синхронизации каталога после очистки бэкапов: %s", e)
