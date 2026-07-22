import json
import logging
import os
import re
import shutil
import sqlite3
import tempfile
from contextlib import closing
from urllib.parse import quote

from db.identity import DURABLE_ENTITIES, MAX_ENTITY_ID, parse_explicit_entity_id
from db.schema import APPLICATION_ID, USER_VERSION
from utils import encrypt_secret

logger = logging.getLogger(__name__)


_ColumnContract = tuple[str, int, str | None, int, int]
_TableContract = tuple[dict[str, _ColumnContract], bool]
_IndexContract = tuple[str, int, str, int, tuple[tuple[str, int, str], ...]]
_TrackedIndexContract = tuple[str, int, str, int, tuple[tuple[str, int, str], ...], str | None]


def _column(
    declared_type: str,
    *,
    not_null: int = 0,
    default: str | None = None,
    primary_key: int = 0,
) -> _ColumnContract:
    return declared_type, not_null, default, primary_key, 0


_LEGACY_ZONE_COLUMNS = {
    "id": _column("INTEGER", primary_key=1),
    "state": _column("TEXT", default="'off'"),
    "name": _column("TEXT", not_null=1),
    "icon": _column("TEXT", default="'🌿'"),
    "duration": _column("INTEGER", default="10"),
    "group_id": _column("INTEGER", default="1"),
    "topic": _column("TEXT"),
    "postpone_until": _column("TEXT"),
    "photo_path": _column("TEXT"),
    "created_at": _column("TIMESTAMP", default="CURRENT_TIMESTAMP"),
    "updated_at": _column("TIMESTAMP", default="CURRENT_TIMESTAMP"),
}
_LEGACY_SETTINGS_COLUMNS = {
    "key": _column("TEXT", primary_key=1),
    "value": _column("TEXT"),
}
_LEGACY_GROUP_COLUMNS = {
    "id": _column("INTEGER", primary_key=1),
    "name": _column("TEXT", not_null=1),
    "created_at": _column("TIMESTAMP", default="CURRENT_TIMESTAMP"),
    "updated_at": _column("TIMESTAMP", default="CURRENT_TIMESTAMP"),
}
_LEGACY_PROGRAM_COLUMNS = {
    "id": _column("INTEGER", primary_key=1),
    "name": _column("TEXT", not_null=1),
    "time": _column("TEXT", not_null=1),
    "days": _column("TEXT", not_null=1),
    "zones": _column("TEXT", not_null=1),
    "created_at": _column("TIMESTAMP", default="CURRENT_TIMESTAMP"),
    "updated_at": _column("TIMESTAMP", default="CURRENT_TIMESTAMP"),
}
_LEGACY_LOG_COLUMNS = {
    "id": _column("INTEGER", primary_key=1),
    "type": _column("TEXT", not_null=1),
    "details": _column("TEXT"),
    "timestamp": _column("TIMESTAMP", default="CURRENT_TIMESTAMP"),
}
_LEGACY_WATER_USAGE_COLUMNS = {
    "id": _column("INTEGER", primary_key=1),
    "zone_id": _column("INTEGER"),
    "liters": _column("REAL"),
    "timestamp": _column("TIMESTAMP", default="CURRENT_TIMESTAMP"),
}
_LEGACY_MQTT_COLUMNS = {
    "id": _column("INTEGER", primary_key=1),
    "name": _column("TEXT", not_null=1),
    "host": _column("TEXT", not_null=1),
    "port": _column("INTEGER", default="1883"),
    "username": _column("TEXT"),
    "password": _column("TEXT"),
    "client_id": _column("TEXT"),
    "enabled": _column("INTEGER", default="1"),
    "created_at": _column("TIMESTAMP", default="CURRENT_TIMESTAMP"),
    "updated_at": _column("TIMESTAMP", default="CURRENT_TIMESTAMP"),
}
_LEGACY_CANCELLATION_COLUMNS = {
    "program_id": _column("INTEGER", not_null=1, primary_key=1),
    "run_date": _column("TEXT", not_null=1, primary_key=2),
    "group_id": _column("INTEGER", primary_key=3),
    "created_at": _column("TIMESTAMP", default="CURRENT_TIMESTAMP"),
}

_LEGACY_BASE_INDEXES: dict[str, _IndexContract] = {
    "sqlite_autoindex_groups_1": ("groups", 1, "u", 0, (("name", 0, "BINARY"),)),
    "idx_zones_group": ("zones", 0, "c", 0, (("group_id", 0, "BINARY"),)),
    "idx_logs_type": ("logs", 0, "c", 0, (("type", 0, "BINARY"),)),
    "idx_logs_timestamp": ("logs", 0, "c", 0, (("timestamp", 0, "BINARY"),)),
    "idx_water_zone": ("water_usage", 0, "c", 0, (("zone_id", 0, "BINARY"),)),
    "idx_water_timestamp": ("water_usage", 0, "c", 0, (("timestamp", 0, "BINARY"),)),
}
_LEGACY_SETTINGS_INDEX: dict[str, _IndexContract] = {
    "sqlite_autoindex_settings_1": ("settings", 1, "pk", 0, (("key", 0, "BINARY"),)),
}
_LEGACY_ZONE_EXTRA_INDEXES: dict[str, _IndexContract] = {
    "idx_zones_mqtt_server": ("zones", 0, "c", 0, (("mqtt_server_id", 0, "BINARY"),)),
    "idx_zones_topic": ("zones", 0, "c", 0, (("topic", 0, "BINARY"),)),
}
_LEGACY_CANCELLATION_INDEX: dict[str, _IndexContract] = {
    "sqlite_autoindex_program_cancellations_1": (
        "program_cancellations",
        1,
        "pk",
        0,
        (("program_id", 0, "BINARY"), ("run_date", 0, "BINARY"), ("group_id", 0, "BINARY")),
    ),
}


def _legacy_schema_stage(
    zone_columns: dict[str, _ColumnContract],
    *,
    include_settings: bool = True,
    group_columns: dict[str, _ColumnContract] = _LEGACY_GROUP_COLUMNS,
    mqtt_columns: dict[str, _ColumnContract] | None = None,
    extra_zone_indexes: bool = False,
    include_cancellations: bool = False,
) -> tuple[dict[str, _TableContract], dict[str, _IndexContract]]:
    tables: dict[str, _TableContract] = {
        "zones": (zone_columns, False),
        "groups": (group_columns, False),
        "programs": (_LEGACY_PROGRAM_COLUMNS, False),
        "logs": (_LEGACY_LOG_COLUMNS, True),
        "water_usage": (_LEGACY_WATER_USAGE_COLUMNS, True),
    }
    indexes = dict(_LEGACY_BASE_INDEXES)
    if include_settings:
        tables["settings"] = (_LEGACY_SETTINGS_COLUMNS, False)
        indexes.update(_LEGACY_SETTINGS_INDEX)
    if mqtt_columns is not None:
        tables["mqtt_servers"] = (mqtt_columns, False)
    if extra_zone_indexes:
        indexes.update(_LEGACY_ZONE_EXTRA_INDEXES)
    if include_cancellations:
        tables["program_cancellations"] = (_LEGACY_CANCELLATION_COLUMNS, False)
        indexes.update(_LEGACY_CANCELLATION_INDEX)
    return tables, indexes


_ZONES_WITH_TIMER = {
    **_LEGACY_ZONE_COLUMNS,
    "postpone_reason": _column("TEXT"),
    "watering_start_time": _column("TEXT"),
}
_ZONES_WITH_SCHEDULE = {
    **_ZONES_WITH_TIMER,
    "scheduled_start_time": _column("TEXT"),
    "last_watering_time": _column("TEXT"),
}
_ZONES_WITH_MQTT = {**_ZONES_WITH_SCHEDULE, "mqtt_server_id": _column("INTEGER")}
_ZONES_WITH_SOURCE = {**_ZONES_WITH_MQTT, "watering_start_source": _column("TEXT")}
_GROUPS_WITH_RAIN = {**_LEGACY_GROUP_COLUMNS, "use_rain_sensor": _column("INTEGER", default="0")}
_MQTT_WITH_TLS = {
    **_LEGACY_MQTT_COLUMNS,
    "tls_enabled": _column("INTEGER", default="0"),
    "tls_ca_path": _column("TEXT"),
    "tls_cert_path": _column("TEXT"),
    "tls_key_path": _column("TEXT"),
    "tls_insecure": _column("INTEGER", default="0"),
    "tls_version": _column("TEXT"),
}

# Exact, monotonic schema stages emitted on the first-parent history before
# named migration tracking was introduced in 24941f7. Column order is
# intentionally ignored: SQLite ALTER TABLE appends columns, so a long-lived
# database and a fresh database from the same release can have different
# physical order while retaining the same complete contract.
#
# This compatibility applies to schema, not unsafe data. Identifiers above the
# durable range and arbitrary dangling group references remain fail-closed:
# only reserved groups 1/999 have a deterministic repair meaning.
_PRE_NAMED_MIGRATION_STAGES = (
    ("ea4da158", *_legacy_schema_stage(_LEGACY_ZONE_COLUMNS, include_settings=False)),
    ("6656d668", *_legacy_schema_stage(_LEGACY_ZONE_COLUMNS)),
    ("03b7dc41", *_legacy_schema_stage(_ZONES_WITH_TIMER)),
    ("7b87622e", *_legacy_schema_stage(_ZONES_WITH_SCHEDULE)),
    ("b113c94a", *_legacy_schema_stage(_ZONES_WITH_MQTT, mqtt_columns=_LEGACY_MQTT_COLUMNS)),
    (
        "6249f525",
        *_legacy_schema_stage(
            _ZONES_WITH_MQTT,
            mqtt_columns=_LEGACY_MQTT_COLUMNS,
            extra_zone_indexes=True,
        ),
    ),
    (
        "50e3b5d9",
        *_legacy_schema_stage(
            _ZONES_WITH_MQTT,
            group_columns=_GROUPS_WITH_RAIN,
            mqtt_columns=_LEGACY_MQTT_COLUMNS,
            extra_zone_indexes=True,
        ),
    ),
    (
        "f1c6f8a1",
        *_legacy_schema_stage(
            _ZONES_WITH_SOURCE,
            group_columns=_GROUPS_WITH_RAIN,
            mqtt_columns=_LEGACY_MQTT_COLUMNS,
            extra_zone_indexes=True,
        ),
    ),
    (
        "90f0ef67",
        *_legacy_schema_stage(
            _ZONES_WITH_SOURCE,
            group_columns=_GROUPS_WITH_RAIN,
            mqtt_columns=_MQTT_WITH_TLS,
            extra_zone_indexes=True,
        ),
    ),
    (
        "01d75961",
        *_legacy_schema_stage(
            _ZONES_WITH_SOURCE,
            group_columns=_GROUPS_WITH_RAIN,
            mqtt_columns=_MQTT_WITH_TLS,
            extra_zone_indexes=True,
            include_cancellations=True,
        ),
    ),
)


def _schema_sql_tokens(value: object) -> tuple[str, ...]:
    """Tokenize stored DDL while preserving the bytes of SQL literals."""

    sql = str(value)
    tokens: list[str] = []
    index = 0
    while index < len(sql):
        character = sql[index]
        if character.isspace():
            index += 1
            continue
        if sql.startswith("--", index):
            tokens.append("<COMMENT>")
            line_end = sql.find("\n", index + 2)
            index = len(sql) if line_end < 0 else line_end
            continue
        if sql.startswith("/*", index):
            tokens.append("<COMMENT>")
            comment_end = sql.find("*/", index + 2)
            index = len(sql) if comment_end < 0 else comment_end + 2
            continue
        if character == "'":
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
            tokens.append(sql[literal_start:index])
            continue
        if character.isalnum() or character in {"_", "$"}:
            token_start = index
            index += 1
            while index < len(sql) and (sql[index].isalnum() or sql[index] in {"_", "$"}):
                index += 1
            tokens.append(sql[token_start:index].upper())
            continue
        tokens.append(character)
        index += 1
    return tuple(tokens)


class MigrationRunner:
    """Runs all named migrations for the irrigation database."""

    def __init__(self, db_path: str):
        self.db_path = db_path

    def init_database(self):
        """Initialize database schema and run all migrations."""
        try:
            self._validate_schema_file_preflight()
            with sqlite3.connect(self.db_path, timeout=5) as conn:
                self._validate_schema_stamp_preflight(conn)

                # PRAGMA
                try:
                    conn.execute("PRAGMA journal_mode=WAL")
                    conn.execute("PRAGMA foreign_keys=ON")
                    conn.execute("PRAGMA synchronous=NORMAL")
                    conn.execute("PRAGMA wal_autocheckpoint=1000")
                    conn.execute("PRAGMA cache_size=-4000")
                    conn.execute("PRAGMA temp_store=MEMORY")
                except sqlite3.Error as e:
                    logger.warning("PRAGMA setup warning: %s", e)

                # Create tables
                conn.execute("""
                    CREATE TABLE IF NOT EXISTS zones (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        state TEXT DEFAULT 'off',
                        name TEXT NOT NULL,
                        icon TEXT DEFAULT '🌿',
                        duration INTEGER DEFAULT 10,
                        group_id INTEGER DEFAULT 1,
                        topic TEXT,
                        postpone_until TEXT,
                        postpone_reason TEXT,
                        photo_path TEXT,
                        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                        updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                    )
                """)
                conn.execute("""
                    CREATE TABLE IF NOT EXISTS migrations (
                        name TEXT PRIMARY KEY,
                        applied_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                    )
                """)
                conn.execute("""
                    CREATE TABLE IF NOT EXISTS settings (
                        key TEXT PRIMARY KEY,
                        value TEXT
                    )
                """)
                conn.execute("""
                    CREATE TABLE IF NOT EXISTS groups (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        name TEXT NOT NULL UNIQUE,
                        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                        updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                    )
                """)
                conn.execute("""
                    CREATE TABLE IF NOT EXISTS programs (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        name TEXT NOT NULL,
                        time TEXT NOT NULL,
                        days TEXT NOT NULL,
                        zones TEXT NOT NULL,
                        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                        updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                    )
                """)
                conn.execute("""
                    CREATE TABLE IF NOT EXISTS logs (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        type TEXT NOT NULL,
                        details TEXT,
                        timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                    )
                """)
                conn.execute("""
                    CREATE TABLE IF NOT EXISTS water_usage (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        zone_id INTEGER,
                        liters REAL,
                        timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                    )
                """)
                conn.execute("""
                    CREATE TABLE IF NOT EXISTS program_cancellations (
                        program_id INTEGER NOT NULL,
                        run_date TEXT NOT NULL,
                        group_id INTEGER,
                        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                        PRIMARY KEY (program_id, run_date, group_id),
                        FOREIGN KEY (program_id) REFERENCES programs(id) ON DELETE CASCADE
                    )
                """)

                # Create indexes
                conn.execute("CREATE INDEX IF NOT EXISTS idx_zones_group ON zones(group_id)")
                conn.execute("CREATE INDEX IF NOT EXISTS idx_logs_type ON logs(type)")
                conn.execute("CREATE INDEX IF NOT EXISTS idx_logs_timestamp ON logs(timestamp)")
                conn.execute("CREATE INDEX IF NOT EXISTS idx_water_zone ON water_usage(zone_id)")
                conn.execute("CREATE INDEX IF NOT EXISTS idx_water_timestamp ON water_usage(timestamp)")

                conn.commit()

                # This one trigger is explicitly self-healing.  Preflight may
                # admit its known name with wrong/missing SQL so it can be
                # replaced here before any initial-data or migration writes.
                conn.execute("DROP TRIGGER IF EXISTS trg_zones_version_invalidate")
                zone_columns = {str(row[1]) for row in conn.execute("PRAGMA table_info(zones)").fetchall()}
                if "version" in zone_columns:
                    self._migrate_zone_version_invalidation(conn)
                conn.commit()

                # Initial data
                self._insert_initial_data(conn)

                # Named migrations
                self._apply_named_migration(conn, "days_format", self._migrate_days_format)
                self._apply_named_migration(conn, "zones_add_postpone_reason", self._migrate_add_postpone_reason)
                self._apply_named_migration(
                    conn, "zones_add_watering_start_time", self._migrate_add_watering_start_time
                )
                self._apply_named_migration(
                    conn, "zones_add_scheduled_start_time", self._migrate_add_scheduled_start_time
                )
                self._apply_named_migration(conn, "zones_add_last_watering_time", self._migrate_add_last_watering_time)
                self._apply_named_migration(conn, "create_mqtt_servers", self._migrate_add_mqtt_servers)
                self._apply_named_migration(conn, "zones_add_mqtt_server_id", self._migrate_add_zone_mqtt_server_id)
                self._apply_named_migration(conn, "ensure_group_999", self._migrate_ensure_special_group)
                self._apply_named_migration(conn, "zones_add_indexes", self._migrate_add_zones_indexes)
                self._apply_named_migration(conn, "groups_add_use_rain", self._migrate_add_group_rain_flag)
                self._apply_named_migration(
                    conn, "zones_add_watering_start_source", self._migrate_add_watering_start_source
                )
                self._apply_named_migration(conn, "mqtt_add_tls_options", self._migrate_add_mqtt_tls_options)
                self._apply_named_migration(conn, "zones_add_control_fields", self._migrate_add_zone_control_fields)
                self._apply_named_migration(conn, "zones_add_commanded_observed", self._migrate_add_commanded_observed)
                self._apply_named_migration(
                    conn, "groups_add_master_and_sensors", self._migrate_add_groups_master_and_sensors
                )
                self._apply_named_migration(
                    conn, "groups_add_master_valve_observed", self._migrate_add_groups_master_valve_observed
                )
                self._apply_named_migration(
                    conn, "groups_add_master_close_delay_sec", self._migrate_add_groups_master_close_delay_sec
                )
                self._apply_named_migration(
                    conn, "groups_add_water_meter_extended", self._migrate_add_groups_water_meter_extended
                )
                self._apply_named_migration(conn, "zones_add_water_stats", self._migrate_add_zones_water_stats)
                self._apply_named_migration(conn, "create_zone_runs_v1", self._migrate_create_zone_runs)
                # Telegram bot migrations
                self._apply_named_migration(conn, "telegram_add_settings_fields", self._migrate_add_telegram_settings)
                self._apply_named_migration(conn, "telegram_create_bot_users", self._migrate_create_bot_users)
                self._apply_named_migration(
                    conn, "telegram_create_bot_subscriptions", self._migrate_create_bot_subscriptions
                )
                self._apply_named_migration(conn, "telegram_create_bot_audit", self._migrate_create_bot_audit)
                self._apply_named_migration(conn, "telegram_add_fsm_and_notif", self._migrate_add_fsm_and_notif)
                self._apply_named_migration(
                    conn, "telegram_create_bot_idempotency", self._migrate_create_bot_idempotency
                )
                # Security: encrypt plaintext MQTT passwords
                self._apply_named_migration(conn, "encrypt_mqtt_passwords", self._migrate_encrypt_mqtt_passwords)
                # Safety: fault tracking
                self._apply_named_migration(conn, "zones_add_fault_tracking", self._migrate_add_fault_tracking)
                # Weather: tables and settings
                self._apply_named_migration(conn, "weather_create_cache", self._migrate_create_weather_cache)
                self._apply_named_migration(conn, "weather_create_log", self._migrate_create_weather_log)
                self._apply_named_migration(conn, "weather_add_settings", self._migrate_add_weather_settings)
                # Weather v2: decisions table, extended settings, wind unit migration
                self._apply_named_migration(conn, "weather_create_decisions", self._migrate_create_weather_decisions)
                self._apply_named_migration(
                    conn, "weather_add_extended_settings", self._migrate_add_extended_weather_settings
                )
                self._apply_named_migration(conn, "weather_wind_kmh_to_ms", self._migrate_wind_kmh_to_ms)
                # Weather H2: virtual water balance (additive, default off)
                self._apply_named_migration(
                    conn, "weather_add_balance_settings", self._migrate_add_water_balance_settings
                )
                self._apply_named_migration(conn, "weather_create_balance_log", self._migrate_create_water_balance_log)
                # Queue & float support (spec v1.1)
                self._apply_named_migration(conn, "queue_and_float_support", self._migrate_queue_and_float_support)
                # Programs v2: new fields (type, schedule_type, interval_days, even_odd, color, enabled, extra_times)
                self._apply_named_migration(conn, "programs_v2_fields", self._migrate_programs_v2_fields)
                # Audit log (two-tier logging spec)
                self._apply_named_migration(conn, "create_audit_log", self._migrate_create_audit_log)
                # Issue #2: backfill last_watering_time from zone_runs.end_utc
                # for zones whose value is NULL after the bug-fix release.
                self._apply_named_migration(
                    conn,
                    "backfill_last_watering_from_zone_runs",
                    self._migrate_backfill_last_watering_from_zone_runs,
                )
                # Single-source-of-truth refactor: drop the denormalised
                # zones.last_watering_time column entirely. Reads now derive
                # the value from zone_runs.end_utc via get_last_watering_time.
                # IRREVERSIBLE — no downgrade registered.
                self._apply_named_migration(
                    conn,
                    "zones_drop_last_watering_time",
                    self._migrate_drop_last_watering_time,
                )
                # Issue #11: add photo_thumb column for separate 400x400 thumb file.
                self._apply_named_migration(
                    conn,
                    "zones_add_photo_thumb",
                    self._migrate_add_photo_thumb,
                )
                # Issue #35: add zone_runs.source ('program' / 'manual') and its
                # lookup index.  The retained historical backfill marker is now
                # non-mutating: schedule proximity cannot prove run ownership.
                self._apply_named_migration(
                    conn,
                    "zone_runs_add_source",
                    self._migrate_add_zone_runs_source,
                )
                self._apply_named_migration(
                    conn,
                    "zone_runs_backfill_source",
                    self._backfill_zone_runs_source,
                )
                # History truth: track whether the relay's physical 'on' was
                # ever confirmed (MQTT echo) during a run, so a run that never
                # actually opened the valve is recorded as 'failed', not 'ok'.
                self._apply_named_migration(
                    conn,
                    "zone_runs_add_confirmed",
                    self._migrate_add_zone_runs_confirmed,
                )
                # Канонизация schedule_type: мастер программ исторически слал
                # 'even_odd' (подчёркивание), планировщик сравнивает с 'even-odd'.
                self._apply_named_migration(
                    conn,
                    "programs_canonical_even_odd",
                    self._migrate_canonical_even_odd,
                )
                # Durable identity / integrity hardening. These migrations are
                # intentionally last: legacy columns and history tables must
                # exist before dangling identifiers can be discovered.
                self._apply_named_migration(
                    conn,
                    "durable_entity_ids_v1",
                    self._migrate_durable_entity_ids,
                )
                # v2 re-runs the idempotent durable-ID pass on installations
                # that already recorded v1: it discovers additional legacy
                # history references and installs the bounded-ID triggers.
                self._apply_named_migration(
                    conn,
                    "durable_entity_ids_v2",
                    self._migrate_durable_entity_ids,
                )
                self._apply_named_migration(
                    conn,
                    "program_cancellations_fk_v1",
                    self._migrate_program_cancellations_fk,
                )
                self._apply_named_migration(
                    conn,
                    "restore_runtime_indexes_v1",
                    self._migrate_restore_runtime_indexes,
                )
                self._apply_named_migration(
                    conn,
                    "mqtt_reference_integrity_v1",
                    self._migrate_mqtt_reference_integrity,
                )
                self._apply_named_migration(
                    conn,
                    "group_reference_integrity_v1",
                    self._migrate_group_reference_integrity,
                )
                # Corrective, forward-only release migrations.  These are
                # deliberately separate markers so installations that already
                # recorded the historical migrations still receive the repair.
                self._apply_named_migration(
                    conn,
                    "zone_runs_clear_unverifiable_source_v1",
                    self._migrate_clear_unverifiable_zone_run_sources,
                )
                self._apply_named_migration(
                    conn,
                    "programs_disable_unsupported_smart_v1",
                    self._migrate_disable_unsupported_smart_programs,
                )
                self._apply_named_migration(
                    conn,
                    "zones_version_invalidation_v1",
                    self._migrate_zone_version_invalidation,
                )

                # Reconcile forward-only safety artifacts even when an older
                # release already recorded the corresponding marker. This
                # repairs missing triggers/indexes and advances durable ID
                # sequences from tombstones without relying on live downgrade.
                self._migrate_durable_entity_ids(conn)
                self._migrate_program_cancellations_fk(conn)
                self._migrate_restore_runtime_indexes(conn)
                self._migrate_mqtt_reference_integrity(conn)
                self._migrate_group_reference_integrity(conn)
                self._migrate_zone_version_invalidation(conn)

                # Stamp only after the complete named-migration chain has
                # succeeded. Backup validation uses these SQLite header values
                # to reject unrelated or partially initialized databases.
                conn.execute(f"PRAGMA application_id = {APPLICATION_ID}")
                conn.execute(f"PRAGMA user_version = {USER_VERSION}")
                conn.commit()

                # Applied migration markers must never mask a missing runtime
                # artifact. Fail readiness closed on a torn/manual schema
                # instead of allowing repositories to degrade into empty data.
                from db.logs import LogRepository

                LogRepository.validate_application_database(self.db_path)

                logger.info("База данных инициализирована успешно")

        except (ConnectionError, TimeoutError, OSError) as e:
            logger.error("Ошибка инициализации базы данных: %s", e)
            raise

    def _validate_schema_file_preflight(self) -> None:
        """Inspect an existing file without permitting SQLite sidecar writes."""

        if not os.path.exists(self.db_path) or os.path.getsize(self.db_path) == 0:
            return

        sidecar_suffixes = tuple(suffix for suffix in ("-wal", "-journal") if os.path.exists(f"{self.db_path}{suffix}"))
        if not sidecar_suffixes:
            uri = f"file:{quote(os.path.abspath(self.db_path), safe='/')}?mode=ro&immutable=1"
            with closing(sqlite3.connect(uri, timeout=5, uri=True)) as conn:
                self._validate_schema_stamp_preflight(conn)
            return

        # ``immutable=1`` deliberately ignores WAL. A durable marker can live
        # only in WAL while its migration artifact has already reached the
        # main file, so immutable inspection can reject a coherent crash
        # state. Reconstruct that exact view in a private temporary directory;
        # SQLite may recover/create sidecars there but never touches the
        # original database or its WAL/SHM. A copied ``-shm`` would contain
        # process-local locks, so the temporary connection rebuilds it.
        with tempfile.TemporaryDirectory(prefix="wb-irrigation-preflight-") as temporary_directory:
            snapshot_path = os.path.join(temporary_directory, "snapshot.db")
            shutil.copyfile(self.db_path, snapshot_path)
            for suffix in sidecar_suffixes:
                try:
                    shutil.copyfile(f"{self.db_path}{suffix}", f"{snapshot_path}{suffix}")
                except FileNotFoundError:
                    # A completed checkpoint may remove a sidecar between the
                    # existence probe and the copy. The copied main file is
                    # still safe to inspect and the original remains untouched.
                    continue
            with closing(sqlite3.connect(snapshot_path, timeout=5)) as conn:
                conn.execute("PRAGMA query_only=ON")
                self._validate_schema_stamp_preflight(conn)

    @staticmethod
    def _validate_schema_stamp_preflight(conn: sqlite3.Connection) -> None:
        """Reject foreign/future databases before any migration can write.

        Historical irrigation databases predate the SQLite header stamp, so
        an unstamped file is adoptable only when its core tables have the
        recognizable legacy shape. An empty file is a genuinely new database.
        """

        application_id = int(conn.execute("PRAGMA application_id").fetchone()[0])
        user_version = int(conn.execute("PRAGMA user_version").fetchone()[0])

        if application_id == APPLICATION_ID:
            if not 0 <= user_version <= USER_VERSION:
                raise sqlite3.DatabaseError(
                    f"unsupported application user_version {user_version}; maximum supported is {USER_VERSION}"
                )
        elif application_id != 0:
            raise sqlite3.DatabaseError(f"unsupported application_id {application_id}; expected 0 or {APPLICATION_ID}")
        elif user_version != 0:
            raise sqlite3.DatabaseError(f"unstamped database has unsupported user_version {user_version}")

        tables = {
            str(row[0])
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table' AND lower(substr(name, 1, 7)) != 'sqlite_'"
            ).fetchall()
        }
        if not tables:
            if application_id == 0:
                return
            raise sqlite3.DatabaseError("stamped database is missing the irrigation schema")

        legacy_columns = {
            "zones": {"id": "INTEGER", "name": "TEXT"},
            "groups": {"id": "INTEGER", "name": "TEXT"},
            "programs": {
                "id": "INTEGER",
                "name": "TEXT",
                "time": "TEXT",
                "days": "TEXT",
                "zones": "TEXT",
            },
            "settings": {"key": "TEXT", "value": "TEXT"},
        }
        if "migrations" not in tables:
            if application_id == 0 and user_version == 0:
                MigrationRunner._validate_pre_named_migrations_schema(conn, tables)
                return
            if not legacy_columns.keys() <= tables:
                raise sqlite3.DatabaseError("unstamped database is not a recognizable irrigation schema")
            raise sqlite3.DatabaseError("stamped database is missing the migrations table")
        MigrationRunner._validate_tracked_historical_schema(conn, tables)

    @staticmethod
    def _validate_pre_named_migrations_schema(conn: sqlite3.Connection, tables: set[str]) -> None:
        """Accept exact semantic contracts emitted by proven pre-tracker releases."""

        candidates = [
            (name, table_contracts, index_contracts)
            for name, table_contracts, index_contracts in _PRE_NAMED_MIGRATION_STAGES
            if set(table_contracts) == tables
        ]
        if not candidates:
            MigrationRunner._raise_unsupported_pre_named_schema(tables, set())

        schema_rows = conn.execute(
            "SELECT type, name, tbl_name, sql FROM sqlite_master "
            "WHERE lower(substr(name, 1, 7)) != 'sqlite_' ORDER BY type, name"
        ).fetchall()
        unexpected_objects = [
            (str(kind), str(name)) for kind, name, _table_name, _sql in schema_rows if kind not in {"table", "index"}
        ]
        if unexpected_objects:
            MigrationRunner._raise_unsupported_pre_named_schema(tables, set())

        table_sql = {
            str(name): str(sql)
            for kind, name, table_name, sql in schema_rows
            if kind == "table" and name == table_name and sql is not None
        }
        if set(table_sql) != tables:
            MigrationRunner._raise_unsupported_pre_named_schema(tables, set())

        actual_tables: dict[str, _TableContract] = {}
        actual_indexes: dict[str, _IndexContract] = {}
        forbidden_table_tokens = {
            "<COMMENT>",
            '"',
            "`",
            "[",
            "]",
            "ASC",
            "CHECK",
            "COLLATE",
            "CONSTRAINT",
            "DEFERRABLE",
            "DESC",
            "FOREIGN",
            "GENERATED",
            "MATCH",
            "REFERENCES",
            "STRICT",
            "VIRTUAL",
            "WITHOUT",
        }

        for table in sorted(tables):
            quoted_table = table.replace('"', '""')
            table_info = conn.execute(f'PRAGMA table_xinfo("{quoted_table}")').fetchall()
            columns = {
                str(row[1]): (
                    " ".join(str(row[2] or "").upper().split()),
                    int(row[3]),
                    None if row[4] is None else " ".join(str(row[4]).split()),
                    int(row[5]),
                    int(row[6]),
                )
                for row in table_info
            }
            tokens = _schema_sql_tokens(table_sql[table])
            if (
                forbidden_table_tokens.intersection(tokens)
                or any(tokens[index : index + 2] == ("ON", "CONFLICT") for index in range(len(tokens) - 1))
                or tokens.count("AUTOINCREMENT") > 1
                or conn.execute(f'PRAGMA foreign_key_list("{quoted_table}")').fetchall()
            ):
                MigrationRunner._raise_unsupported_pre_named_schema(tables, set(actual_indexes))
            actual_tables[table] = (columns, tokens.count("AUTOINCREMENT") == 1)

            for _seq, index_name, unique, origin, partial in conn.execute(
                f'PRAGMA index_list("{quoted_table}")'
            ).fetchall():
                index_name = str(index_name)
                quoted_index = index_name.replace('"', '""')
                key_columns = tuple(
                    (str(row[2]), int(row[3]), str(row[4]))
                    for row in conn.execute(f'PRAGMA index_xinfo("{quoted_index}")').fetchall()
                    if row[5]
                )
                actual_indexes[index_name] = (
                    table,
                    int(unique),
                    str(origin),
                    int(partial),
                    key_columns,
                )

        if any(
            actual_tables == expected_tables and actual_indexes == expected_indexes
            for _name, expected_tables, expected_indexes in candidates
        ):
            return
        MigrationRunner._raise_unsupported_pre_named_schema(tables, set(actual_indexes))

    @staticmethod
    def _raise_unsupported_pre_named_schema(tables: set[str], indexes: set[str]) -> None:
        table_names = ",".join(sorted(tables)) or "<none>"
        index_names = ",".join(sorted(indexes)) or "<none>"
        raise sqlite3.DatabaseError(
            "unstamped database is not a recognizable irrigation schema: exact columns, declared types, "
            "defaults, nullability, keys, constraints, and indexes do not match a recognized historical schema "
            f"from the supported first-parent releases before 24941f7 (tables={table_names}; indexes={index_names}); "
            "restore a known irrigation backup or export the data explicitly"
        )

    @staticmethod
    def _validate_tracked_historical_schema(conn: sqlite3.Connection, tables: set[str]) -> None:
        """Validate named-migration history before opening it writable."""

        from db.logs import LogRepository, _normalize_schema_sql, _normalized_where_predicate

        canonical_columns: dict[str, dict[str, _ColumnContract]] = {}
        for table, columns in LogRepository._REQUIRED_BACKUP_COLUMNS.items():
            primary_key_positions = {
                column: position for position, column in enumerate(LogRepository._REQUIRED_PRIMARY_KEYS[table], start=1)
            }
            canonical_columns[table] = {
                column: _column(
                    "TIMESTAMP" if type_family == "NUMERIC" else type_family,
                    not_null=int(not_null),
                    default=LogRepository._REQUIRED_COLUMN_DEFAULTS.get(table, {}).get(column),
                    primary_key=primary_key_positions.get(column, 0),
                )
                for column, (type_family, not_null) in columns.items()
            }
        # This column is present in the 24941f7 floor and is intentionally
        # absent from the current schema after zones_drop_last_watering_time.
        canonical_columns["zones"]["last_watering_time"] = _column("TEXT")

        _base_name, base_tables, _base_indexes = next(
            stage for stage in _PRE_NAMED_MIGRATION_STAGES if stage[0] == "01d75961"
        )
        tracked_base_tables = {
            table: (dict(columns), autoincrement) for table, (columns, autoincrement) in base_tables.items()
        }
        tracked_base_tables["migrations"] = (
            {
                "name": _column("TEXT", primary_key=1),
                "applied_at": _column("TIMESTAMP", default="CURRENT_TIMESTAMP"),
            },
            False,
        )
        required_tables = set(tracked_base_tables)
        allowed_tables = set(canonical_columns)
        if not required_tables <= tables or not tables <= allowed_tables:
            missing = sorted(required_tables - tables)
            unexpected = sorted(tables - allowed_tables)
            MigrationRunner._raise_unsupported_tracked_schema(
                f"table set differs from the 24941f7 floor (missing={missing!r}; unexpected={unexpected!r})"
            )

        schema_rows = conn.execute(
            "SELECT type, name, tbl_name, sql FROM sqlite_master "
            "WHERE lower(substr(name, 1, 7)) != 'sqlite_' ORDER BY type, name"
        ).fetchall()
        unexpected_objects = [
            (str(kind), str(name))
            for kind, name, _table_name, _sql in schema_rows
            if kind not in {"table", "index", "trigger"}
        ]
        if unexpected_objects:
            MigrationRunner._raise_unsupported_tracked_schema(f"unexpected schema objects {unexpected_objects!r}")

        table_sql = {
            str(name): str(sql)
            for kind, name, table_name, sql in schema_rows
            if kind == "table" and name == table_name and sql is not None
        }
        if set(table_sql) != tables:
            MigrationRunner._raise_unsupported_tracked_schema("table definitions are missing or malformed")

        migration_rows = conn.execute("SELECT name, typeof(name) FROM migrations").fetchall()
        invalid_markers = sorted(
            str(name)
            for name, storage_type in migration_rows
            if storage_type != "text" or str(name) not in LogRepository._REQUIRED_BACKUP_MIGRATIONS
        )
        if invalid_markers:
            MigrationRunner._raise_unsupported_tracked_schema(f"unknown migration markers {invalid_markers!r}")
        migration_markers = {str(name) for name, _storage_type in migration_rows}
        last_watering_was_dropped = "zones_drop_last_watering_time" in migration_markers

        optional_minimum_columns = {
            table: set(columns) for table, columns in canonical_columns.items() if table not in required_tables
        }
        optional_minimum_columns["zone_runs"] -= {"source", "confirmed"}
        optional_minimum_columns["bot_users"] -= {
            "fsm_state",
            "fsm_data",
            "notif_critical",
            "notif_emergency",
            "notif_postpone",
            "notif_zone_events",
            "notif_rain",
        }

        forbidden_table_tokens = {
            "<COMMENT>",
            "ASC",
            "CHECK",
            "COLLATE",
            "CONSTRAINT",
            "DEFERRABLE",
            "DESC",
            "GENERATED",
            "MATCH",
            "STRICT",
            "VIRTUAL",
            "WITHOUT",
        }
        flexible_autoincrement_tables = {"zones", "groups", "programs", "mqtt_servers"}

        for table in sorted(tables):
            quoted_table = table.replace('"', '""')
            table_info = conn.execute(f'PRAGMA table_xinfo("{quoted_table}")').fetchall()
            actual_columns = {
                str(row[1]): (
                    " ".join(str(row[2] or "").upper().split()),
                    int(row[3]),
                    None if row[4] is None else " ".join(str(row[4]).split()),
                    int(row[5]),
                    int(row[6]),
                )
                for row in table_info
            }
            minimum_columns = (
                set(tracked_base_tables[table][0]) if table in tracked_base_tables else optional_minimum_columns[table]
            )
            if table == "zones" and "last_watering_time" not in actual_columns:
                # The forward rebuild was committed before its marker in
                # historical releases. Missing marker + missing artifact is
                # therefore a recoverable crash boundary: the idempotent
                # migration records the marker on restart.
                minimum_columns.remove("last_watering_time")
            allowed_columns = canonical_columns[table]
            if not minimum_columns <= set(actual_columns) or not set(actual_columns) <= set(allowed_columns):
                MigrationRunner._raise_unsupported_tracked_schema(
                    f"table {table!r} has missing floor columns or unknown additions"
                )
            if table == "zones" and last_watering_was_dropped and "last_watering_time" in actual_columns:
                MigrationRunner._raise_unsupported_tracked_schema(
                    "zones_drop_last_watering_time marker disagrees with the zones.last_watering_time artifact"
                )
            for column, actual_contract in actual_columns.items():
                expected_contract = allowed_columns[column]
                if actual_contract == expected_contract:
                    continue
                actual_type, actual_not_null, actual_default, actual_primary_key, actual_hidden = actual_contract
                expected_type, expected_not_null, expected_default, expected_primary_key, expected_hidden = (
                    expected_contract
                )
                if actual_type != expected_type:
                    detail = (
                        f"table {table!r} column {column!r} has declared type {actual_type!r}, "
                        f"expected canonical {expected_type!r}; affinity-compatible aliases are not a supported "
                        "migration input"
                    )
                elif actual_not_null != expected_not_null:
                    qualifier = "missing" if expected_not_null else "unexpected"
                    detail = f"table {table!r} column {column!r} has {qualifier} NOT NULL constraint"
                elif actual_default != expected_default:
                    detail = (
                        f"table {table!r} column {column!r} has default {actual_default!r}, "
                        f"expected {expected_default!r}"
                    )
                elif actual_primary_key != expected_primary_key:
                    detail = (
                        f"table {table!r} column {column!r} has primary key position {actual_primary_key}, "
                        f"expected {expected_primary_key}"
                    )
                else:
                    detail = (
                        f"table {table!r} column {column!r} has hidden/generated state {actual_hidden}, "
                        f"expected {expected_hidden}"
                    )
                MigrationRunner._raise_unsupported_tracked_schema(detail)

            tokens = _schema_sql_tokens(table_sql[table])
            autoincrement_count = tokens.count("AUTOINCREMENT")
            expected_autoincrement = table in LogRepository._REQUIRED_AUTOINCREMENT_TABLES
            if (
                forbidden_table_tokens.intersection(tokens)
                or any(tokens[index : index + 2] == ("ON", "CONFLICT") for index in range(len(tokens) - 1))
                or autoincrement_count > 1
                or (table not in flexible_autoincrement_tables and (autoincrement_count == 1) != expected_autoincrement)
            ):
                MigrationRunner._raise_unsupported_tracked_schema(
                    f"table {table!r} has an unsupported constraint or AUTOINCREMENT contract"
                )
        allowed_indexes: dict[str, _TrackedIndexContract] = {}
        for index_name, (table, columns) in LogRepository._REQUIRED_INDEXES.items():
            descending = LogRepository._REQUIRED_INDEX_DESCENDING.get(index_name, (False,) * len(columns))
            predicate = LogRepository._REQUIRED_INDEX_PREDICATES.get(index_name)
            allowed_indexes[index_name] = (
                table,
                0,
                "c",
                int(predicate is not None),
                tuple((column, int(is_descending), "BINARY") for column, is_descending in zip(columns, descending)),
                predicate,
            )

        base_index_contracts = {
            **_LEGACY_BASE_INDEXES,
            **_LEGACY_SETTINGS_INDEX,
            **_LEGACY_ZONE_EXTRA_INDEXES,
            **_LEGACY_CANCELLATION_INDEX,
            "sqlite_autoindex_migrations_1": (
                "migrations",
                1,
                "pk",
                0,
                (("name", 0, "BINARY"),),
            ),
        }
        for index_name, contract in base_index_contracts.items():
            table, unique, origin, partial, columns = contract
            allowed_indexes[index_name] = (table, unique, origin, partial, columns, None)
        allowed_indexes.update(
            {
                "sqlite_autoindex_retired_entity_ids_1": (
                    "retired_entity_ids",
                    1,
                    "pk",
                    0,
                    (("entity", 0, "BINARY"), ("id", 0, "BINARY")),
                    None,
                ),
                "sqlite_autoindex_bot_users_1": (
                    "bot_users",
                    1,
                    "u",
                    0,
                    (("chat_id", 0, "BINARY"),),
                    None,
                ),
                "sqlite_autoindex_bot_idempotency_1": (
                    "bot_idempotency",
                    1,
                    "pk",
                    0,
                    (("token", 0, "BINARY"),),
                    None,
                ),
            }
        )

        index_sql = {
            str(name): str(sql) for kind, name, _table_name, sql in schema_rows if kind == "index" and sql is not None
        }
        actual_indexes: dict[str, _TrackedIndexContract] = {}
        for table in sorted(tables):
            quoted_table = table.replace('"', '""')
            for _seq, index_name, unique, origin, partial in conn.execute(
                f'PRAGMA index_list("{quoted_table}")'
            ).fetchall():
                index_name = str(index_name)
                quoted_index = index_name.replace('"', '""')
                key_columns = tuple(
                    (str(row[2]), int(row[3]), str(row[4]))
                    for row in conn.execute(f'PRAGMA index_xinfo("{quoted_index}")').fetchall()
                    if row[5]
                )
                predicate = _normalized_where_predicate(index_sql[index_name]) if index_name in index_sql else None
                actual_indexes[index_name] = (
                    table,
                    int(unique),
                    str(origin),
                    int(partial),
                    key_columns,
                    predicate,
                )

        for index_name, actual_contract in actual_indexes.items():
            if allowed_indexes.get(index_name) != actual_contract:
                MigrationRunner._raise_unsupported_tracked_schema(
                    f"index {index_name!r} is unknown or has noncanonical semantics"
                )
        required_base_index_names = set(base_index_contracts)
        if last_watering_was_dropped:
            # The historical forward table rebuild re-created the two MQTT
            # indexes but lost idx_zones_group. The reconciliation pass below
            # restores it idempotently before the database is stamped.
            required_base_index_names.remove("idx_zones_group")
        missing_base_indexes = sorted(required_base_index_names - set(actual_indexes))
        if missing_base_indexes:
            MigrationRunner._raise_unsupported_tracked_schema(
                f"24941f7 floor index or unique constraint is missing: {missing_base_indexes!r}"
            )
        required_auto_indexes = {
            "retired_entity_ids": "sqlite_autoindex_retired_entity_ids_1",
            "bot_users": "sqlite_autoindex_bot_users_1",
            "bot_idempotency": "sqlite_autoindex_bot_idempotency_1",
        }
        for table, index_name in required_auto_indexes.items():
            if table in tables and index_name not in actual_indexes:
                MigrationRunner._raise_unsupported_tracked_schema(
                    f"table {table!r} is missing canonical key index {index_name!r}"
                )

        required_foreign_keys = LogRepository._REQUIRED_FOREIGN_KEYS
        for table in sorted(tables):
            quoted_table = table.replace('"', '""')
            actual_foreign_keys = frozenset(
                (str(row[2]), str(row[3]), str(row[4]), str(row[5]), str(row[6]), str(row[7]))
                for row in conn.execute(f'PRAGMA foreign_key_list("{quoted_table}")').fetchall()
            )
            expected_foreign_keys = required_foreign_keys.get(table, frozenset())
            if table == "program_cancellations":
                valid_foreign_keys = actual_foreign_keys in {frozenset(), expected_foreign_keys}
            else:
                valid_foreign_keys = actual_foreign_keys == expected_foreign_keys
            if not valid_foreign_keys:
                MigrationRunner._raise_unsupported_tracked_schema(
                    f"table {table!r} has invalid foreign keys {sorted(actual_foreign_keys)!r}, "
                    f"expected {sorted(expected_foreign_keys)!r}"
                )

        trigger_rows = {
            str(name): (str(table_name), str(sql))
            for kind, name, table_name, sql in schema_rows
            if kind == "trigger" and sql is not None
        }
        for trigger_name, (table, sql) in trigger_rows.items():
            if trigger_name == "trg_zones_version_invalidate":
                # This artifact is dropped/recreated immediately after the DB
                # is opened writable. Final validation remains exact.
                continue
            expected_table = LogRepository._REQUIRED_INTEGRITY_TRIGGERS.get(trigger_name)
            expected_sql = LogRepository._REQUIRED_TRIGGER_SQL.get(trigger_name)
            if expected_table is None or expected_sql is None:
                MigrationRunner._raise_unsupported_tracked_schema(
                    f"application schema has unexpected triggers: {[trigger_name]!r}"
                )
            if expected_table != table or _normalize_schema_sql(sql) != expected_sql:
                MigrationRunner._raise_unsupported_tracked_schema(f"trigger {trigger_name!r} has an invalid definition")

    @staticmethod
    def _raise_unsupported_tracked_schema(detail: str) -> None:
        raise sqlite3.DatabaseError(
            f"tracked irrigation schema rejected before migration: {detail}; "
            "the original file was not opened writable; restore a known backup or export the data explicitly"
        )

    def _insert_initial_data(self, conn):
        """Вставить начальные данные."""
        try:
            cursor = conn.execute("SELECT COUNT(*) FROM zones")
            if cursor.fetchone()[0] > 0:
                return

            groups = [(1, "Насос-1"), (999, "БЕЗ ПОЛИВА")]
            for group_id, name in groups:
                conn.execute("INSERT OR IGNORE INTO groups (id, name) VALUES (?, ?)", (group_id, name))
            conn.commit()
            logger.info("Начальные данные вставлены: группы 1 (Насос-1) и 999 (БЕЗ ПОЛИВА)")
        except sqlite3.Error as e:
            logger.error("Ошибка вставки начальных данных: %s", e)

    def _apply_named_migration(self, conn, name: str, func):
        """Применить именованную миграцию ровно один раз.

        Имя записывается в ``migrations`` ТОЛЬКО после успешного ``func(conn)``:
        упавшая миграция НЕ помечается применённой и прерывает инициализацию.
        Продолжать запуск на частично обновлённой схеме опаснее, чем оставить
        сервис неготовым и повторить идемпотентную миграцию после исправления.
        """
        try:
            cur = conn.execute("SELECT name FROM migrations WHERE name = ? LIMIT 1", (name,))
            if cur.fetchone():
                return
        except sqlite3.Error as e:
            logger.error("Ошибка проверки миграции %s: %s", name, e)
            raise
        try:
            func(conn)
        except Exception:
            logger.exception("Ошибка применения миграции %s — инициализация прервана", name)
            try:
                conn.rollback()
            except sqlite3.Error:
                logger.exception("Не удалось откатить упавшую миграцию %s", name)
            raise
        try:
            conn.execute("INSERT OR REPLACE INTO migrations(name) VALUES (?)", (name,))
            conn.commit()
        except sqlite3.Error as e:
            logger.error("Ошибка фиксации миграции %s: %s", name, e)
            raise

    def rollback_migration(self, name: str) -> bool:
        """Refuse in-place downgrade; recovery requires a known-good backup."""
        preview = self.preview_rollback_migration(name)
        logger.error(
            "Live migration downgrade refused migration=%s known=%s applied=%s; %s",
            name,
            preview["known"],
            preview["applied"],
            preview["recovery"],
        )
        return False

    def preview_rollback_migration(self, name: str) -> dict[str, object]:
        """Describe downgrade state without opening the database for writes."""
        known = name in self.DOWNGRADE_REGISTRY
        applied = False
        uri = f"file:{quote(os.path.abspath(self.db_path))}?mode=ro"
        try:
            with sqlite3.connect(uri, uri=True, timeout=5) as conn:
                table = conn.execute(
                    "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'migrations'"
                ).fetchone()
                if table is not None:
                    applied = (
                        conn.execute("SELECT 1 FROM migrations WHERE name = ? LIMIT 1", (name,)).fetchone() is not None
                    )
        except sqlite3.Error:
            logger.exception("Migration downgrade preview failed migration=%s", name)
        return {
            "migration": name,
            "known": known,
            "applied": applied,
            "supported": False,
            "would_mutate": False,
            "error_code": "LIVE_DOWNGRADE_UNSUPPORTED",
            "recovery": "restore a pre-upgrade database backup",
        }

    @staticmethod
    def _recreate_table_without_columns(conn, table: str, drop_columns: list):
        """SQLite-compatible DROP COLUMN via table recreation.

        Reads existing schema, removes specified columns, recreates the table
        and copies data back. Works on all SQLite versions.
        """
        cur = conn.execute(f"PRAGMA table_info({table})")
        columns_info = cur.fetchall()
        # columns_info: (cid, name, type, notnull, dflt_value, pk)
        keep = [c for c in columns_info if c[1] not in drop_columns]
        if not keep:
            logger.error("_recreate_table_without_columns: cannot drop ALL columns from %s", table)
            return
        keep_names = [c[1] for c in keep]
        col_defs = []
        for c in keep:
            _cid, name, ctype, notnull, dflt, pk = c
            parts = [name, ctype or "TEXT"]
            if pk:
                parts.append("PRIMARY KEY")
                # Check if the PK column is AUTOINCREMENT
                # We need to check the original CREATE TABLE SQL
                try:
                    schema_cur = conn.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name=?", (table,))
                    schema_row = schema_cur.fetchone()
                    if (
                        schema_row
                        and schema_row[0]
                        and "AUTOINCREMENT" in schema_row[0].upper()
                        and name.lower() == "id"
                    ):
                        parts.append("AUTOINCREMENT")
                except sqlite3.Error:
                    pass
            if notnull and not pk:
                parts.append("NOT NULL")
            if dflt is not None and not pk:
                parts.append(f"DEFAULT {dflt}")
            col_defs.append(" ".join(parts))

        cols_csv = ", ".join(keep_names)
        defs_csv = ", ".join(col_defs)
        tmp = table + "__down_tmp"

        conn.execute(f"DROP TABLE IF EXISTS {tmp}")
        conn.execute(f"CREATE TABLE {tmp} ({defs_csv})")
        conn.execute(f"INSERT INTO {tmp} ({cols_csv}) SELECT {cols_csv} FROM {table}")
        conn.execute(f"DROP TABLE {table}")
        conn.execute(f"ALTER TABLE {tmp} RENAME TO {table}")
        conn.commit()

    @staticmethod
    def _recreate_forward_table_without_columns_preserving_identity(
        conn: sqlite3.Connection,
        table: str,
        drop_columns: list[str],
    ) -> None:
        """Forward-only rebuild that atomically preserves AUTOINCREMENT state."""

        schema_row = conn.execute(
            "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = ?",
            (table,),
        ).fetchone()
        original_table_sql = str(schema_row[0]) if schema_row and schema_row[0] else ""
        cur = conn.execute(f"PRAGMA table_info({table})")
        columns_info = cur.fetchall()
        keep = [column for column in columns_info if column[1] not in drop_columns]
        if not keep:
            raise sqlite3.DatabaseError(f"forward rebuild cannot drop every column from {table!r}")

        keep_names = [str(column[1]) for column in keep]
        column_definitions = []
        for column in keep:
            _cid, name, declared_type, not_null, default, primary_key = column
            parts = [str(name), str(declared_type or "TEXT")]
            if primary_key:
                parts.append("PRIMARY KEY")
                if "AUTOINCREMENT" in original_table_sql.upper() and str(name).casefold() == "id":
                    parts.append("AUTOINCREMENT")
            if not_null and not primary_key:
                parts.append("NOT NULL")
            if default is not None and not primary_key:
                parts.append(f"DEFAULT {default}")
            column_definitions.append(" ".join(parts))

        sequence_watermark: int | None = None
        if "AUTOINCREMENT" in original_table_sql.upper() and "id" in keep_names:
            sequence_rows = conn.execute(
                "SELECT seq, typeof(seq) FROM sqlite_sequence WHERE name = ?",
                (table,),
            ).fetchall()
            if len(sequence_rows) > 1:
                raise sqlite3.DatabaseError(f"sqlite_sequence for {table} contains duplicate rows")
            if sequence_rows:
                sequence_value, sequence_type = sequence_rows[0]
                if sequence_type != "integer" or not isinstance(sequence_value, int) or sequence_value < 0:
                    raise sqlite3.DatabaseError(f"sqlite_sequence for {table} has invalid value")
                sequence_watermark = sequence_value
            else:
                sequence_watermark = 0
            live_max = int(conn.execute(f"SELECT COALESCE(MAX(id), 0) FROM {table}").fetchone()[0])
            sequence_watermark = max(sequence_watermark, live_max)

        columns_csv = ", ".join(keep_names)
        definitions_csv = ", ".join(column_definitions)
        temporary_table = f"{table}__forward_tmp"
        conn.execute(f"DROP TABLE IF EXISTS {temporary_table}")
        conn.execute(f"CREATE TABLE {temporary_table} ({definitions_csv})")
        conn.execute(f"INSERT INTO {temporary_table} ({columns_csv}) SELECT {columns_csv} FROM {table}")
        conn.execute(f"DROP TABLE {table}")
        conn.execute(f"ALTER TABLE {temporary_table} RENAME TO {table}")
        if sequence_watermark is not None:
            cursor = conn.execute(
                "UPDATE sqlite_sequence SET seq = MAX(seq, ?) WHERE name = ?",
                (sequence_watermark, table),
            )
            if cursor.rowcount == 0:
                conn.execute(
                    "INSERT INTO sqlite_sequence(name, seq) VALUES (?, ?)",
                    (table, sequence_watermark),
                )

    @staticmethod
    def _add_column_if_missing(conn, table: str, column: str, decl: str) -> bool:
        """``ALTER TABLE <table> ADD COLUMN <column> <decl>``, если колонки нет.

        Возвращает True, если ALTER был выполнен. Имена таблиц/колонок и DDL
        всегда литеральные (задаются в коде миграций), commit — на вызывающем.
        """
        cur = conn.execute(f"PRAGMA table_info({table})")
        if column in {row[1] for row in cur.fetchall()}:
            return False
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {decl}")
        return True

    @classmethod
    def _add_columns_if_missing(cls, conn, table: str, columns) -> list[str]:
        """Пакетный вариант :meth:`_add_column_if_missing`.

        ``columns`` — итерируемое из пар (имя, DDL-декларация). Возвращает
        имена реально добавленных колонок.
        """
        return [column for column, decl in columns if cls._add_column_if_missing(conn, table, column, decl)]

    @staticmethod
    def _ensure_autoincrement_identity(conn: sqlite3.Connection, table: str) -> None:
        """Rebuild ``table.id`` with AUTOINCREMENT without losing its schema.

        Plain ``INTEGER PRIMARY KEY`` may reuse the deleted highest rowid.
        That is unsafe for identifiers retained in schedules, history and
        hardware configuration.  The rebuild copies every column and restores
        user-created indexes/triggers whose SQL SQLite exposes.
        """

        row = conn.execute(
            "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = ?",
            (table,),
        ).fetchone()
        if not row or not row[0]:
            raise RuntimeError(f"required table {table!r} is missing")
        old_sql = str(row[0])
        id_match = re.search(r"\bid\s+INTEGER\s+PRIMARY\s+KEY(?:\s+AUTOINCREMENT)?", old_sql, re.IGNORECASE)
        if id_match is None:
            raise RuntimeError(f"{table}.id is not an INTEGER PRIMARY KEY")
        if "AUTOINCREMENT" in id_match.group(0).upper():
            return

        schema_objects = [
            str(item[0])
            for item in conn.execute(
                "SELECT sql FROM sqlite_master "
                "WHERE tbl_name = ? AND type IN ('index', 'trigger') AND sql IS NOT NULL "
                "ORDER BY type, name",
                (table,),
            ).fetchall()
        ]
        columns = [str(item[1]) for item in conn.execute(f"PRAGMA table_info({table})").fetchall()]
        if not columns:
            raise RuntimeError(f"required table {table!r} has no columns")

        # DROP TABLE executes ON DELETE CASCADE actions even though the parent
        # is recreated immediately. Snapshot direct cascade children and put
        # them back after the replacement parent exists. This notably protects
        # program_cancellations while upgrading legacy programs.id.
        cascade_children = []
        for (child_table,) in conn.execute(
            "SELECT name FROM sqlite_master "
            "WHERE type = 'table' AND lower(substr(name, 1, 7)) != 'sqlite_' ORDER BY name"
        ).fetchall():
            quoted_child = '"' + str(child_table).replace('"', '""') + '"'
            foreign_keys = conn.execute(f"PRAGMA foreign_key_list({quoted_child})").fetchall()
            if not any(str(row[2]) == table and str(row[6]).upper() == "CASCADE" for row in foreign_keys):
                continue
            child_columns = [str(row[1]) for row in conn.execute(f"PRAGMA table_info({quoted_child})")]
            child_rows = conn.execute(f"SELECT * FROM {quoted_child}").fetchall()
            cascade_children.append((str(child_table), child_columns, child_rows))

        temp_table = f"{table}__durable_id"
        create_sql = re.sub(
            rf"(?i)CREATE\s+TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?(?:\"{re.escape(table)}\"|{re.escape(table)})",
            f'CREATE TABLE "{temp_table}"',
            old_sql,
            count=1,
        )
        create_sql = re.sub(
            r"\bid\s+INTEGER\s+PRIMARY\s+KEY(?!\s+AUTOINCREMENT)",
            "id INTEGER PRIMARY KEY AUTOINCREMENT",
            create_sql,
            count=1,
            flags=re.IGNORECASE,
        )
        quoted_columns = ", ".join(f'"{column}"' for column in columns)
        conn.execute(f'DROP TABLE IF EXISTS "{temp_table}"')
        conn.execute(create_sql)
        conn.execute(f'INSERT INTO "{temp_table}" ({quoted_columns}) SELECT {quoted_columns} FROM "{table}"')
        conn.execute(f'DROP TABLE "{table}"')
        conn.execute(f'ALTER TABLE "{temp_table}" RENAME TO "{table}"')
        for child_table, child_columns, child_rows in cascade_children:
            if not child_rows:
                continue
            quoted_child = '"' + child_table.replace('"', '""') + '"'
            quoted_child_columns = ", ".join(f'"{column.replace(chr(34), chr(34) * 2)}"' for column in child_columns)
            placeholders = ", ".join("?" for _ in child_columns)
            conn.execute(f"DELETE FROM {quoted_child}")
            conn.executemany(
                f"INSERT INTO {quoted_child} ({quoted_child_columns}) VALUES ({placeholders})",
                child_rows,
            )
        for schema_sql in schema_objects:
            conn.execute(schema_sql)

    @staticmethod
    def _retire_identifier(conn: sqlite3.Connection, entity: str, identifier: object) -> None:
        try:
            normalized = int(identifier)
        except (TypeError, ValueError):
            return
        if normalized <= 0 or normalized > MAX_ENTITY_ID:
            return
        conn.execute(
            "INSERT OR IGNORE INTO retired_entity_ids(entity, id) VALUES (?, ?)",
            (entity, normalized),
        )

    @classmethod
    def _backfill_retired_identifiers(cls, conn: sqlite3.Connection) -> None:
        """Preserve legacy dangling IDs so an upgrade cannot rebind them."""

        for table, column in (
            ("zone_runs", "zone_id"),
            ("water_usage", "zone_id"),
            ("weather_log", "zone_id"),
        ):
            table_exists = conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
                (table,),
            ).fetchone()
            if table_exists:
                rows = conn.execute(
                    f"SELECT DISTINCT source.{column} FROM {table} source "
                    f"LEFT JOIN zones current ON current.id = source.{column} "
                    f"WHERE source.{column} IS NOT NULL AND current.id IS NULL"
                ).fetchall()
                for (identifier,) in rows:
                    cls._retire_identifier(conn, "zone", identifier)

        for table, column in (
            ("program_queue_log", "zone_ids"),
            ("float_events", "paused_zones"),
        ):
            table_exists = conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
                (table,),
            ).fetchone()
            if not table_exists:
                continue
            for (identifiers_json,) in conn.execute(
                f"SELECT {column} FROM {table} WHERE {column} IS NOT NULL"
            ).fetchall():
                try:
                    identifiers = json.loads(identifiers_json or "[]")
                except (json.JSONDecodeError, TypeError):
                    continue
                if not isinstance(identifiers, list):
                    continue
                for identifier in identifiers:
                    try:
                        normalized = parse_explicit_entity_id(identifier)
                    except ValueError:
                        continue
                    if conn.execute("SELECT 1 FROM zones WHERE id = ?", (normalized,)).fetchone() is None:
                        cls._retire_identifier(conn, "zone", normalized)

        for (zones_json,) in conn.execute("SELECT zones FROM programs").fetchall():
            try:
                identifiers = json.loads(zones_json or "[]")
            except (json.JSONDecodeError, TypeError):
                continue
            if not isinstance(identifiers, list):
                continue
            for identifier in identifiers:
                try:
                    exists = conn.execute("SELECT 1 FROM zones WHERE id = ?", (int(identifier),)).fetchone()
                except (TypeError, ValueError):
                    continue
                if exists is None:
                    cls._retire_identifier(conn, "zone", identifier)

        group_sources = (
            ("zone_runs", "group_id"),
            ("program_queue_log", "group_id"),
            ("float_events", "group_id"),
            ("program_cancellations", "group_id"),
            ("zones", "group_id"),
        )
        for table, column in group_sources:
            table_exists = conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
                (table,),
            ).fetchone()
            if not table_exists:
                continue
            for (identifier,) in conn.execute(
                f"SELECT DISTINCT source.{column} FROM {table} source "
                f"LEFT JOIN groups current ON current.id = source.{column} "
                f"WHERE source.{column} IS NOT NULL AND current.id IS NULL"
            ).fetchall():
                cls._retire_identifier(conn, "group", identifier)

        mqtt_candidates: set[int] = set()

        def add_mqtt_candidate(value: object) -> None:
            try:
                mqtt_candidates.add(int(value))
            except (TypeError, ValueError):
                logger.warning("Ignoring malformed legacy MQTT server reference %r", value)

        zone_columns = {str(row[1]) for row in conn.execute("PRAGMA table_info(zones)").fetchall()}
        if "mqtt_server_id" in zone_columns:
            for (value,) in conn.execute(
                "SELECT DISTINCT mqtt_server_id FROM zones WHERE mqtt_server_id IS NOT NULL"
            ).fetchall():
                add_mqtt_candidate(value)
        group_columns = {str(row[1]) for row in conn.execute("PRAGMA table_info(groups)").fetchall()}
        for column in (
            "master_mqtt_server_id",
            "pressure_mqtt_server_id",
            "water_mqtt_server_id",
            "float_mqtt_server_id",
        ):
            if column in group_columns:
                for (value,) in conn.execute(
                    f"SELECT DISTINCT {column} FROM groups WHERE {column} IS NOT NULL"
                ).fetchall():
                    add_mqtt_candidate(value)
        setting_keys = (
            "rain.server_id",
            "master.server_id",
            "env.temp.server_id",
            "env.hum.server_id",
        )
        placeholders = ", ".join("?" for _ in setting_keys)
        for (value,) in conn.execute(
            f"SELECT value FROM settings WHERE key IN ({placeholders}) AND value IS NOT NULL",
            setting_keys,
        ).fetchall():
            add_mqtt_candidate(value)
        for identifier in mqtt_candidates:
            if conn.execute("SELECT 1 FROM mqtt_servers WHERE id = ?", (identifier,)).fetchone() is None:
                cls._retire_identifier(conn, "mqtt_server", identifier)

        for (identifier,) in conn.execute(
            "SELECT DISTINCT cancellations.program_id FROM program_cancellations cancellations "
            "LEFT JOIN programs current ON current.id = cancellations.program_id "
            "WHERE current.id IS NULL"
        ).fetchall():
            cls._retire_identifier(conn, "program", identifier)
        queue_exists = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'program_queue_log'"
        ).fetchone()
        if queue_exists:
            for (identifier,) in conn.execute(
                "SELECT DISTINCT history.program_id FROM program_queue_log history "
                "LEFT JOIN programs current ON current.id = history.program_id "
                "WHERE current.id IS NULL"
            ).fetchall():
                cls._retire_identifier(conn, "program", identifier)

    @staticmethod
    def _install_retired_id_triggers(conn: sqlite3.Connection, table: str, entity: str) -> None:
        conn.execute(f"DROP TRIGGER IF EXISTS trg_{table}_retire_id")
        conn.execute(f"DROP TRIGGER IF EXISTS trg_{table}_reject_retired_id")
        conn.execute(f"DROP TRIGGER IF EXISTS trg_{table}_reject_id_update")
        conn.execute(f"""
            CREATE TRIGGER trg_{table}_retire_id
            AFTER DELETE ON {table}
            BEGIN
                INSERT OR IGNORE INTO retired_entity_ids(entity, id)
                VALUES ('{entity}', OLD.id);
            END
        """)
        conn.execute(f"DROP TRIGGER IF EXISTS trg_{table}_reject_out_of_range_id")
        conn.execute(f"""
            CREATE TRIGGER trg_{table}_reject_out_of_range_id
            AFTER INSERT ON {table}
            WHEN NEW.id <= 0 OR NEW.id > {MAX_ENTITY_ID}
            BEGIN
                SELECT RAISE(ABORT, 'entity identifier out of range');
            END
        """)
        conn.execute(f"DROP TRIGGER IF EXISTS trg_{table}_reject_explicit_id_boundary")
        conn.execute(f"""
            CREATE TRIGGER trg_{table}_reject_explicit_id_boundary
            BEFORE INSERT ON {table}
            WHEN NEW.id != -1 AND NEW.id >= {MAX_ENTITY_ID}
            BEGIN
                SELECT RAISE(ABORT, 'entity identifier out of range');
            END
        """)
        conn.execute(f"""
            CREATE TRIGGER trg_{table}_reject_id_update
            BEFORE UPDATE OF id ON {table}
            WHEN NEW.id != OLD.id
            BEGIN
                SELECT RAISE(ABORT, 'entity identifier is immutable');
            END
        """)
        conn.execute(f"""
            CREATE TRIGGER trg_{table}_reject_retired_id
            BEFORE INSERT ON {table}
            WHEN EXISTS (
                SELECT 1 FROM retired_entity_ids
                WHERE entity = '{entity}' AND id = NEW.id
            )
            BEGIN
                SELECT RAISE(ABORT, 'retired identifier cannot be reused');
            END
        """)

    @staticmethod
    def _advance_autoincrement_sequence(conn: sqlite3.Connection, table: str, entity: str) -> None:
        invalid_live = conn.execute(
            f"SELECT id FROM {table} WHERE id <= 0 OR id > ? ORDER BY id LIMIT 1",
            (MAX_ENTITY_ID,),
        ).fetchone()
        if invalid_live is not None:
            raise sqlite3.DatabaseError(
                f"{table} contains out-of-range durable identifier {invalid_live[0]!r}; "
                f"remap live identifiers into 1..{MAX_ENTITY_ID} or export the data before upgrade"
            )
        invalid_retired = conn.execute(
            "SELECT id, typeof(id) FROM retired_entity_ids "
            "WHERE entity = ? AND (typeof(id) != 'integer' OR id <= 0 OR id > ?) "
            "ORDER BY id LIMIT 1",
            (entity, MAX_ENTITY_ID),
        ).fetchone()
        if invalid_retired is not None:
            raise sqlite3.DatabaseError(
                f"retired {entity} identifier {invalid_retired[0]!r} has invalid storage or range"
            )

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
        if len(sequence_rows) > 1:
            raise sqlite3.DatabaseError(
                f"sqlite_sequence for {table} must contain at most one row before reconciliation"
            )
        if sequence_rows:
            sequence_value, sequence_type = sequence_rows[0]
            if sequence_type != "integer" or not isinstance(sequence_value, int):
                raise sqlite3.DatabaseError(f"sqlite_sequence for {table} has invalid value: {sequence_value!r}")
            if not 0 <= sequence_value <= MAX_ENTITY_ID:
                raise sqlite3.DatabaseError(
                    f"sqlite_sequence for {table} exceeds the supported durable identifier range: {sequence_value!r}"
                )

        row = conn.execute(
            f"SELECT MAX(value) FROM ("
            f"SELECT COALESCE(MAX(id), 0) AS value FROM {table} "
            "UNION ALL "
            "SELECT COALESCE(MAX(id), 0) AS value FROM retired_entity_ids WHERE entity = ?"
            ")",
            (entity,),
        ).fetchone()
        high_watermark = int(row[0] or 0)
        cursor = conn.execute(
            "UPDATE sqlite_sequence SET seq = MAX(seq, ?) WHERE name = ?",
            (high_watermark, table),
        )
        if cursor.rowcount == 0:
            conn.execute("INSERT INTO sqlite_sequence(name, seq) VALUES (?, ?)", (table, high_watermark))

    def _migrate_durable_entity_ids(self, conn: sqlite3.Connection) -> None:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS retired_entity_ids (
                entity TEXT NOT NULL,
                id INTEGER NOT NULL,
                retired_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (entity, id)
            )
        """)
        allowed_entities = tuple(entity for _table, entity in DURABLE_ENTITIES)
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
        durable_tables = ("zones", "groups", "mqtt_servers", "programs")
        rebuild_tables = []
        for table in durable_tables:
            row = conn.execute(
                "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = ?",
                (table,),
            ).fetchone()
            if not row or not row[0]:
                raise RuntimeError(f"required table {table!r} is missing")
            id_match = re.search(
                r"\bid\s+INTEGER\s+PRIMARY\s+KEY(?:\s+AUTOINCREMENT)?",
                str(row[0]),
                re.IGNORECASE,
            )
            if id_match is None:
                raise RuntimeError(f"{table}.id is not an INTEGER PRIMARY KEY")
            if "AUTOINCREMENT" not in id_match.group(0).upper():
                rebuild_tables.append(table)

        # Current reference guards live on other tables and are reparsed while
        # a durable parent is temporarily absent. Suspend only these known
        # forward artifacts; canonical definitions are reinstalled below.
        suspended_reference_guards = bool(rebuild_tables)
        if suspended_reference_guards:
            for trigger in (
                "trg_zones_mqtt_server_insert",
                "trg_zones_mqtt_server_update",
                "trg_groups_mqtt_server_insert",
                "trg_groups_mqtt_server_update",
                "trg_settings_mqtt_server_insert",
                "trg_settings_mqtt_server_update",
                "trg_mqtt_servers_restrict_referenced_delete",
                "trg_zones_group_insert",
                "trg_zones_group_update",
                "trg_groups_restrict_referenced_delete",
                "trg_groups_restrict_reserved_delete",
                "trg_groups_reject_replacing_name",
                "trg_groups_reject_replacing_name_update",
            ):
                conn.execute(f"DROP TRIGGER IF EXISTS {trigger}")

        for table in durable_tables:
            self._ensure_autoincrement_identity(conn, table)

        self._backfill_retired_identifiers(conn)
        for table, entity in (
            ("zones", "zone"),
            ("groups", "group"),
            ("mqtt_servers", "mqtt_server"),
            ("programs", "program"),
        ):
            self._install_retired_id_triggers(conn, table, entity)
            self._advance_autoincrement_sequence(conn, table, entity)
        if suspended_reference_guards:
            self._migrate_mqtt_reference_integrity(conn)
            self._migrate_group_reference_integrity(conn)
        conn.commit()

    @staticmethod
    def _migrate_program_cancellations_fk(conn: sqlite3.Connection) -> None:
        row = conn.execute(
            "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'program_cancellations'"
        ).fetchone()
        if not row or not row[0]:
            raise RuntimeError("required table 'program_cancellations' is missing")
        normalized = " ".join(str(row[0]).upper().split())
        if "REFERENCES PROGRAMS(ID) ON DELETE CASCADE" in normalized:
            return

        orphan_count = conn.execute(
            "SELECT COUNT(*) FROM program_cancellations cancellations "
            "LEFT JOIN programs current ON current.id = cancellations.program_id "
            "WHERE current.id IS NULL"
        ).fetchone()[0]
        conn.execute("DROP TABLE IF EXISTS program_cancellations__fk")
        conn.execute("""
            CREATE TABLE program_cancellations__fk (
                program_id INTEGER NOT NULL,
                run_date TEXT NOT NULL,
                group_id INTEGER,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (program_id, run_date, group_id),
                FOREIGN KEY (program_id) REFERENCES programs(id) ON DELETE CASCADE
            )
        """)
        conn.execute("""
            INSERT INTO program_cancellations__fk(program_id, run_date, group_id, created_at)
            SELECT cancellations.program_id, cancellations.run_date,
                   cancellations.group_id, cancellations.created_at
            FROM program_cancellations cancellations
            JOIN programs current ON current.id = cancellations.program_id
        """)
        conn.execute("DROP TABLE program_cancellations")
        conn.execute("ALTER TABLE program_cancellations__fk RENAME TO program_cancellations")
        violations = conn.execute("PRAGMA foreign_key_check(program_cancellations)").fetchall()
        if violations:
            raise RuntimeError(f"program_cancellations FK violations after migration: {violations!r}")
        conn.commit()
        if orphan_count:
            logger.warning(
                "Removed %s orphan program cancellation rows; retired program IDs remain tombstoned",
                orphan_count,
            )

    @staticmethod
    def _migrate_restore_runtime_indexes(conn: sqlite3.Connection) -> None:
        conn.execute("CREATE INDEX IF NOT EXISTS idx_zones_group ON zones(group_id)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_zones_mqtt_server ON zones(mqtt_server_id)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_zones_topic ON zones(topic)")
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_zone_runs_last_ok "
            "ON zone_runs(zone_id, end_utc DESC) "
            "WHERE status = 'ok' AND end_utc IS NOT NULL"
        )
        conn.execute("CREATE INDEX IF NOT EXISTS idx_zone_runs_start ON zone_runs(start_utc DESC)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_zone_runs_group_start ON zone_runs(group_id, start_utc DESC)")
        conn.commit()

    @staticmethod
    def _migrate_mqtt_reference_integrity(conn: sqlite3.Connection) -> None:
        """Install DB-level MQTT reference guards.

        Repository preflight produces friendlier API errors, while these
        triggers close the writer race between reference validation and MQTT
        server deletion and protect every direct repository/settings write.
        """

        trigger_names = (
            "trg_zones_mqtt_server_insert",
            "trg_zones_mqtt_server_update",
            "trg_groups_mqtt_server_insert",
            "trg_groups_mqtt_server_update",
            "trg_settings_mqtt_server_insert",
            "trg_settings_mqtt_server_update",
            "trg_mqtt_servers_restrict_referenced_delete",
        )
        for name in trigger_names:
            conn.execute(f"DROP TRIGGER IF EXISTS {name}")

        table_columns = {
            table: {str(row[1]) for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}
            for table in ("zones", "groups", "settings", "mqtt_servers")
        }
        delete_references: list[str] = []

        if "mqtt_server_id" in table_columns["zones"]:
            for operation in ("INSERT", "UPDATE"):
                conn.execute(f"""
                    CREATE TRIGGER trg_zones_mqtt_server_{operation.lower()}
                    BEFORE {operation} ON zones
                    WHEN NEW.mqtt_server_id IS NOT NULL
                     AND NOT EXISTS (SELECT 1 FROM mqtt_servers WHERE id = NEW.mqtt_server_id)
                    BEGIN
                        SELECT RAISE(ABORT, 'missing mqtt server reference');
                    END
                """)
            delete_references.append("EXISTS (SELECT 1 FROM zones WHERE mqtt_server_id = OLD.id)")

        group_server_columns = [
            column
            for column in (
                "master_mqtt_server_id",
                "pressure_mqtt_server_id",
                "water_mqtt_server_id",
                "float_mqtt_server_id",
            )
            if column in table_columns["groups"]
        ]
        if group_server_columns:
            missing_group_server = " OR ".join(
                f"(NEW.{column} IS NOT NULL AND NOT EXISTS (SELECT 1 FROM mqtt_servers WHERE id = NEW.{column}))"
                for column in group_server_columns
            )
            for operation in ("INSERT", "UPDATE"):
                conn.execute(f"""
                    CREATE TRIGGER trg_groups_mqtt_server_{operation.lower()}
                    BEFORE {operation} ON groups
                    WHEN {missing_group_server}
                    BEGIN
                        SELECT RAISE(ABORT, 'missing mqtt server reference');
                    END
                """)
            delete_references.extend(
                f"EXISTS (SELECT 1 FROM groups WHERE {column} = OLD.id)" for column in group_server_columns
            )

        setting_keys = "'rain.server_id', 'master.server_id', 'env.temp.server_id', 'env.hum.server_id'"
        if {"key", "value"} <= table_columns["settings"]:
            for key, value in conn.execute(
                f"SELECT key, value FROM settings WHERE key IN ({setting_keys}) AND value IS NOT NULL"
            ).fetchall():
                text_value = str(value)
                if re.fullmatch(r"[0-9]+", text_value) is None:
                    continue
                server_id = int(text_value)
                if not 1 <= server_id <= MAX_ENTITY_ID:
                    continue
                if conn.execute("SELECT 1 FROM mqtt_servers WHERE id = ?", (server_id,)).fetchone():
                    conn.execute("UPDATE settings SET value = ? WHERE key = ?", (str(server_id), key))

            missing_setting_server = (
                f"NEW.key IN ({setting_keys}) AND NEW.value IS NOT NULL AND NOT EXISTS "
                "(SELECT 1 FROM mqtt_servers WHERE CAST(id AS TEXT) = NEW.value)"
            )
            for operation in ("INSERT", "UPDATE"):
                conn.execute(f"""
                    CREATE TRIGGER trg_settings_mqtt_server_{operation.lower()}
                    BEFORE {operation} ON settings
                    WHEN {missing_setting_server}
                    BEGIN
                        SELECT RAISE(ABORT, 'missing mqtt server reference');
                    END
                """)
            delete_references.append(
                "EXISTS (SELECT 1 FROM settings "
                f"WHERE key IN ({setting_keys}) AND ("
                "value = CAST(OLD.id AS TEXT) OR ("
                "value GLOB '[0-9]*' AND value NOT GLOB '*[^0-9]*' "
                "AND CAST(value AS INTEGER) = OLD.id)))"
            )

        if delete_references:
            delete_condition = " OR ".join(delete_references)
            conn.execute(f"""
                CREATE TRIGGER trg_mqtt_servers_restrict_referenced_delete
                BEFORE DELETE ON mqtt_servers
                WHEN {delete_condition}
                BEGIN
                    SELECT RAISE(ABORT, 'mqtt server is referenced');
                END
            """)
        conn.commit()

    @staticmethod
    def _migrate_group_reference_integrity(conn: sqlite3.Connection) -> None:
        """Require every non-legacy zone group reference to stay live."""

        # IDs 1 and 999 are application-owned identities: write paths default
        # to group 1 and group 999 represents the explicit no-watering group.
        # Recover installations that deleted either row before the database
        # guard existed. This must precede the generic dangling check because
        # legacy zones may still legitimately reference the deleted default.
        reserved_groups = ((1, "Насос-1"), (999, "БЕЗ ПОЛИВА"))
        for group_id, name in reserved_groups:
            group_exists = conn.execute("SELECT 1 FROM groups WHERE id = ?", (group_id,)).fetchone() is not None
            retired_exists = (
                conn.execute(
                    "SELECT 1 FROM retired_entity_ids WHERE entity = 'group' AND id = ?",
                    (group_id,),
                ).fetchone()
                is not None
            )
            if group_exists:
                if retired_exists:
                    raise sqlite3.DatabaseError(f"live group identifier {group_id!r} is also marked retired")
                continue
            conn.execute(
                "DELETE FROM retired_entity_ids WHERE entity = 'group' AND id = ?",
                (group_id,),
            )
            existing_names = {str(row[0]) for row in conn.execute("SELECT name FROM groups").fetchall()}
            recovered_name = name
            if recovered_name in existing_names:
                base_name = f"{name} (системная {group_id})"
                recovered_name = base_name
                suffix = 2
                while recovered_name in existing_names:
                    recovered_name = f"{base_name} {suffix}"
                    suffix += 1
            conn.execute("INSERT INTO groups(id, name) VALUES (?, ?)", (group_id, recovered_name))

        dangling = conn.execute(
            "SELECT zones.id, zones.group_id FROM zones "
            "LEFT JOIN groups ON groups.id = zones.group_id "
            "WHERE zones.group_id IS NOT NULL AND zones.group_id != 0 AND groups.id IS NULL "
            "ORDER BY zones.id LIMIT 20"
        ).fetchall()
        if dangling:
            raise RuntimeError(
                f"zones contain dangling group references: {dangling!r}; "
                "restore the referenced groups or explicitly remap the affected zones before upgrade"
            )

        trigger_names = (
            "trg_zones_group_insert",
            "trg_zones_group_update",
            "trg_groups_restrict_referenced_delete",
            "trg_groups_restrict_reserved_delete",
            "trg_groups_reject_replacing_name",
            "trg_groups_reject_replacing_name_update",
        )
        for name in trigger_names:
            conn.execute(f"DROP TRIGGER IF EXISTS {name}")

        missing_group = (
            "NEW.group_id IS NOT NULL AND NEW.group_id != 0 "
            "AND NOT EXISTS (SELECT 1 FROM groups WHERE id = NEW.group_id)"
        )
        for operation in ("INSERT", "UPDATE"):
            conn.execute(f"""
                CREATE TRIGGER trg_zones_group_{operation.lower()}
                BEFORE {operation} ON zones
                WHEN {missing_group}
                BEGIN
                    SELECT RAISE(ABORT, 'missing group reference');
                END
            """)
        conn.execute("""
            CREATE TRIGGER trg_groups_restrict_referenced_delete
            BEFORE DELETE ON groups
            WHEN EXISTS (SELECT 1 FROM zones WHERE group_id = OLD.id)
            BEGIN
                SELECT RAISE(ABORT, 'group is referenced');
            END
        """)
        conn.execute("""
            CREATE TRIGGER trg_groups_restrict_reserved_delete
            BEFORE DELETE ON groups
            WHEN OLD.id IN (1, 999)
            BEGIN
                SELECT RAISE(ABORT, 'reserved group cannot be deleted');
            END
        """)
        conn.execute("""
            CREATE TRIGGER trg_groups_reject_replacing_name
            BEFORE INSERT ON groups
            WHEN EXISTS (
                SELECT 1 FROM groups
                WHERE name = NEW.name AND id != NEW.id
            )
            BEGIN
                SELECT RAISE(ABORT, 'group name conflict cannot replace existing group');
            END
        """)
        conn.execute("""
            CREATE TRIGGER trg_groups_reject_replacing_name_update
            BEFORE UPDATE OF name ON groups
            WHEN EXISTS (
                SELECT 1 FROM groups
                WHERE name = NEW.name AND id != OLD.id
            )
            BEGIN
                SELECT RAISE(ABORT, 'group name conflict cannot replace existing group');
            END
        """)
        conn.commit()

    # --- All migration methods ---

    def _migrate_days_format(self, conn):
        cursor = conn.execute("SELECT id, days FROM programs")
        rows = cursor.fetchall()
        for pid, days_json in rows:
            try:
                days = json.loads(days_json)
                if isinstance(days, list) and days and any(d < 0 or d > 6 for d in days):
                    migrated = []
                    for d in days:
                        try:
                            nd = int(d) - 1
                        except (TypeError, ValueError) as e:
                            logger.debug("migration day parse skip: %s", e)
                            continue
                        if nd < 0:
                            nd = 0
                        if nd > 6:
                            nd = 6
                        migrated.append(nd)
                    conn.execute(
                        "UPDATE programs SET days = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                        (json.dumps(sorted(set(migrated))), pid),
                    )
            except (json.JSONDecodeError, TypeError, ValueError) as e:
                logger.debug("migration days JSON parse skip for row: %s", e)
                continue
        conn.commit()

    def _migrate_add_postpone_reason(self, conn):
        if self._add_column_if_missing(conn, "zones", "postpone_reason", "TEXT"):
            conn.commit()
            logger.info("Добавлено поле postpone_reason в таблицу zones")

    def _migrate_add_watering_start_time(self, conn):
        if self._add_column_if_missing(conn, "zones", "watering_start_time", "TEXT"):
            conn.commit()
            logger.info("Добавлено поле watering_start_time в таблицу zones")

    def _migrate_add_scheduled_start_time(self, conn):
        if self._add_column_if_missing(conn, "zones", "scheduled_start_time", "TEXT"):
            conn.commit()
            logger.info("Добавлено поле scheduled_start_time в таблицу zones")

    def _migrate_add_last_watering_time(self, conn):
        if self._add_column_if_missing(conn, "zones", "last_watering_time", "TEXT"):
            conn.commit()
            logger.info("Добавлено поле last_watering_time в таблицу zones")

    def _migrate_add_watering_start_source(self, conn):
        if self._add_column_if_missing(conn, "zones", "watering_start_source", "TEXT"):
            conn.commit()
            logger.info("Добавлено поле watering_start_source в таблицу zones")

    def _migrate_add_group_rain_flag(self, conn):
        if self._add_column_if_missing(conn, "groups", "use_rain_sensor", "INTEGER DEFAULT 0"):
            conn.commit()
            logger.info("Добавлено поле use_rain_sensor в таблицу groups")

    def _migrate_add_mqtt_servers(self, conn):
        conn.execute("""
            CREATE TABLE IF NOT EXISTS mqtt_servers (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                host TEXT NOT NULL,
                port INTEGER DEFAULT 1883,
                username TEXT,
                password TEXT,
                client_id TEXT,
                enabled INTEGER DEFAULT 1,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        conn.commit()

    def _migrate_add_zone_mqtt_server_id(self, conn):
        if self._add_column_if_missing(conn, "zones", "mqtt_server_id", "INTEGER"):
            conn.commit()
            logger.info("Добавлено поле mqtt_server_id в таблицу zones")

    def _migrate_ensure_special_group(self, conn):
        cur = conn.execute("SELECT COUNT(*) FROM groups WHERE id = 999")
        cnt = cur.fetchone()[0] if cur else 0
        if cnt == 0:
            conn.execute("INSERT OR IGNORE INTO groups (id, name) VALUES (999, 'БЕЗ ПОЛИВА')")
            conn.commit()
            logger.info("Добавлена служебная группа 999 'БЕЗ ПОЛИВА'")

    def _migrate_add_zones_indexes(self, conn):
        conn.execute("CREATE INDEX IF NOT EXISTS idx_zones_mqtt_server ON zones(mqtt_server_id)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_zones_topic ON zones(topic)")
        conn.commit()

    def _migrate_add_mqtt_tls_options(self, conn):
        self._add_columns_if_missing(
            conn,
            "mqtt_servers",
            [
                ("tls_enabled", "INTEGER DEFAULT 0"),
                ("tls_ca_path", "TEXT"),
                ("tls_cert_path", "TEXT"),
                ("tls_key_path", "TEXT"),
                ("tls_insecure", "INTEGER DEFAULT 0"),
                ("tls_version", "TEXT"),
            ],
        )
        conn.commit()

    def _migrate_add_zone_control_fields(self, conn):
        self._add_columns_if_missing(
            conn,
            "zones",
            [
                ("planned_end_time", "TEXT"),
                ("sequence_id", "TEXT"),
                ("command_id", "TEXT"),
                ("version", "INTEGER DEFAULT 0"),
            ],
        )
        conn.commit()
        logger.info("Добавлены поля planned_end_time, sequence_id, command_id, version в zones")

    def _migrate_add_commanded_observed(self, conn):
        self._add_columns_if_missing(
            conn,
            "zones",
            [
                ("commanded_state", "TEXT"),
                ("observed_state", "TEXT"),
            ],
        )
        conn.commit()
        logger.info("Добавлены поля commanded_state, observed_state в zones")

    def _migrate_add_groups_master_and_sensors(self, conn):
        self._add_columns_if_missing(
            conn,
            "groups",
            [
                ("use_master_valve", "INTEGER DEFAULT 0"),
                ("master_mqtt_topic", 'TEXT DEFAULT ""'),
                ("master_mode", 'TEXT DEFAULT "NC"'),
                ("master_mqtt_server_id", "INTEGER"),
                ("master_valve_observed", "TEXT"),
                ("master_close_delay_sec", "INTEGER DEFAULT 60"),
                ("use_pressure_sensor", "INTEGER DEFAULT 0"),
                ("pressure_mqtt_topic", 'TEXT DEFAULT ""'),
                ("pressure_unit", 'TEXT DEFAULT "bar"'),
                ("pressure_mqtt_server_id", "INTEGER"),
                ("use_water_meter", "INTEGER DEFAULT 0"),
                ("water_mqtt_topic", 'TEXT DEFAULT ""'),
                ("water_mqtt_server_id", "INTEGER"),
                ("water_pulse_size", 'TEXT DEFAULT "1l"'),
                ("water_base_value_m3", "REAL DEFAULT 0"),
                ("water_base_pulses", "INTEGER DEFAULT 0"),
            ],
        )
        conn.commit()
        logger.info("Добавлены поля мастер-клапана и сенсоров в таблицу groups")

    def _migrate_add_groups_master_valve_observed(self, conn):
        if self._add_column_if_missing(conn, "groups", "master_valve_observed", "TEXT"):
            conn.commit()
            logger.info("Добавлено поле master_valve_observed в groups")

    def _migrate_add_groups_master_close_delay_sec(self, conn):
        if self._add_column_if_missing(conn, "groups", "master_close_delay_sec", "INTEGER DEFAULT 60"):
            conn.commit()
            logger.info("Добавлено поле master_close_delay_sec в groups")

    def _migrate_add_groups_water_meter_extended(self, conn):
        self._add_columns_if_missing(
            conn,
            "groups",
            [
                ("water_pulse_size", 'TEXT DEFAULT "1l"'),
                ("water_base_value_m3", "REAL DEFAULT 0"),
                ("water_base_pulses", "INTEGER DEFAULT 0"),
            ],
        )
        conn.commit()
        logger.info("Добавлены поля water_pulse_size, water_base_value_m3, water_base_pulses в groups")

    def _migrate_add_zones_water_stats(self, conn):
        self._add_columns_if_missing(
            conn,
            "zones",
            [
                ("last_avg_flow_lpm", "REAL"),
                ("last_total_liters", "REAL"),
            ],
        )
        conn.commit()
        logger.info("Добавлены поля last_avg_flow_lpm, last_total_liters в zones")

    def _migrate_create_zone_runs(self, conn):
        conn.execute("""
            CREATE TABLE IF NOT EXISTS zone_runs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                zone_id INTEGER NOT NULL,
                group_id INTEGER NOT NULL,
                start_utc TEXT,
                end_utc TEXT,
                start_monotonic REAL,
                end_monotonic REAL,
                start_raw_pulses INTEGER,
                end_raw_pulses INTEGER,
                pulse_liters_at_start INTEGER,
                base_m3_at_start REAL,
                total_liters REAL,
                avg_flow_lpm REAL,
                status TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        conn.execute("CREATE INDEX IF NOT EXISTS idx_zone_runs_zone ON zone_runs(zone_id)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_zone_runs_group ON zone_runs(group_id)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_zone_runs_active ON zone_runs(zone_id, end_utc)")
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_zone_runs_last_ok "
            "ON zone_runs(zone_id, end_utc DESC) "
            "WHERE status = 'ok' AND end_utc IS NOT NULL"
        )
        conn.execute("CREATE INDEX IF NOT EXISTS idx_zone_runs_start ON zone_runs(start_utc DESC)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_zone_runs_group_start ON zone_runs(group_id, start_utc DESC)")
        conn.commit()
        logger.info("Создана таблица zone_runs")

    def _migrate_add_telegram_settings(self, conn):
        keys = [
            "telegram_bot_token_encrypted",
            "telegram_access_password_hash",
            "telegram_webhook_secret_path",
            "telegram_admin_chat_id",
        ]
        for k in keys:
            cur = conn.execute("SELECT 1 FROM settings WHERE key=?", (k,))
            if cur.fetchone() is None:
                conn.execute("INSERT OR REPLACE INTO settings(key, value) VALUES(?, ?)", (k, None))
        conn.commit()
        logger.info("Добавлены ключи настроек телеграм-бота в settings")

    def _migrate_create_bot_users(self, conn):
        conn.execute("""
            CREATE TABLE IF NOT EXISTS bot_users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                chat_id INTEGER UNIQUE,
                username TEXT,
                first_name TEXT,
                role TEXT DEFAULT 'user',
                is_authorized INTEGER DEFAULT 0,
                failed_attempts INTEGER DEFAULT 0,
                locked_until TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                last_seen_at TIMESTAMP
            )
        """)
        conn.execute("CREATE INDEX IF NOT EXISTS idx_bot_users_chat ON bot_users(chat_id)")
        conn.commit()
        logger.info("Создана таблица bot_users")

    def _migrate_create_bot_subscriptions(self, conn):
        conn.execute("""
            CREATE TABLE IF NOT EXISTS bot_subscriptions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                type TEXT NOT NULL,
                format TEXT NOT NULL,
                time_local TEXT NOT NULL,
                dow_mask TEXT,
                enabled INTEGER DEFAULT 1,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY(user_id) REFERENCES bot_users(id) ON DELETE CASCADE
            )
        """)
        conn.execute("CREATE INDEX IF NOT EXISTS idx_bot_subs_user ON bot_subscriptions(user_id)")
        conn.commit()
        logger.info("Создана таблица bot_subscriptions")

    def _migrate_create_bot_audit(self, conn):
        conn.execute("""
            CREATE TABLE IF NOT EXISTS bot_audit (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER,
                action TEXT,
                payload_json TEXT,
                ts TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY(user_id) REFERENCES bot_users(id) ON DELETE SET NULL
            )
        """)
        conn.execute("CREATE INDEX IF NOT EXISTS idx_bot_audit_user ON bot_audit(user_id)")
        conn.commit()
        logger.info("Создана таблица bot_audit")

    def _migrate_add_fsm_and_notif(self, conn):
        self._add_columns_if_missing(
            conn,
            "bot_users",
            [
                ("fsm_state", "TEXT"),
                ("fsm_data", "TEXT"),
                ("notif_critical", "INTEGER DEFAULT 1"),
                ("notif_emergency", "INTEGER DEFAULT 1"),
                ("notif_postpone", "INTEGER DEFAULT 1"),
                ("notif_zone_events", "INTEGER DEFAULT 0"),
                ("notif_rain", "INTEGER DEFAULT 0"),
            ],
        )
        conn.commit()

    def _migrate_create_bot_idempotency(self, conn):
        conn.execute("""
            CREATE TABLE IF NOT EXISTS bot_idempotency (
                token TEXT PRIMARY KEY,
                chat_id INTEGER,
                action TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        conn.execute("CREATE INDEX IF NOT EXISTS idx_bot_idemp_chat ON bot_idempotency(chat_id)")
        conn.commit()
        logger.info("Создана таблица bot_idempotency")

    def _migrate_encrypt_mqtt_passwords(self, conn):
        cur = conn.execute("SELECT id, password FROM mqtt_servers WHERE password IS NOT NULL AND password != ''")
        rows = cur.fetchall()
        count = 0
        for row_id, pwd in rows:
            if pwd and not pwd.startswith("ENC:"):
                enc = encrypt_secret(pwd)
                if enc:
                    conn.execute("UPDATE mqtt_servers SET password = ? WHERE id = ?", ("ENC:" + enc, row_id))
                    count += 1
        if count:
            conn.commit()
            logger.info("Зашифровано %d MQTT паролей", count)
        else:
            logger.info("Нет MQTT паролей для шифрования")

    def _migrate_add_fault_tracking(self, conn):
        self._add_columns_if_missing(
            conn,
            "zones",
            [
                ("last_fault", "TEXT"),
                ("fault_count", "INTEGER DEFAULT 0"),
            ],
        )
        conn.commit()
        logger.info("Добавлены поля last_fault, fault_count в zones")

    def _migrate_create_weather_cache(self, conn):
        conn.execute("""
            CREATE TABLE IF NOT EXISTS weather_cache (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                latitude REAL NOT NULL,
                longitude REAL NOT NULL,
                data TEXT NOT NULL,
                fetched_at REAL NOT NULL
            )
        """)
        conn.execute("CREATE INDEX IF NOT EXISTS idx_weather_cache_loc ON weather_cache(latitude, longitude)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_weather_cache_time ON weather_cache(fetched_at)")
        conn.commit()
        logger.info("Создана таблица weather_cache")

    def _migrate_create_weather_log(self, conn):
        conn.execute("""
            CREATE TABLE IF NOT EXISTS weather_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                zone_id INTEGER,
                original_duration INTEGER,
                adjusted_duration INTEGER,
                coefficient INTEGER,
                skipped INTEGER DEFAULT 0,
                skip_reason TEXT,
                weather_data TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        conn.execute("CREATE INDEX IF NOT EXISTS idx_weather_log_zone ON weather_log(zone_id)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_weather_log_time ON weather_log(created_at)")
        conn.commit()
        logger.info("Создана таблица weather_log")

    def _migrate_add_weather_settings(self, conn):
        weather_keys = {
            "weather.enabled": "0",
            "weather.latitude": None,
            "weather.longitude": None,
            "weather.rain_threshold_mm": "5.0",
            "weather.freeze_threshold_c": "2.0",
            "weather.wind_threshold_kmh": "25.0",
        }
        for key, default_val in weather_keys.items():
            cur = conn.execute("SELECT 1 FROM settings WHERE key = ?", (key,))
            if cur.fetchone() is None:
                conn.execute("INSERT INTO settings(key, value) VALUES(?, ?)", (key, default_val))
        conn.commit()
        logger.info("Добавлены настройки погоды в settings")

    # --- Weather v2 migrations ---

    def _migrate_create_weather_decisions(self, conn):
        """Create weather_decisions table for tracking irrigation decisions."""
        conn.execute("""
            CREATE TABLE IF NOT EXISTS weather_decisions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                date TEXT NOT NULL,
                time TEXT NOT NULL,
                temperature REAL,
                humidity REAL,
                precipitation_24h REAL,
                wind_speed REAL,
                coefficient INTEGER NOT NULL,
                decision TEXT NOT NULL,
                reason TEXT,
                mode TEXT NOT NULL DEFAULT 'auto',
                data_sources TEXT DEFAULT '{}',
                user_override INTEGER DEFAULT 0,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        conn.execute("CREATE INDEX IF NOT EXISTS idx_weather_decisions_date ON weather_decisions(date)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_weather_decisions_created ON weather_decisions(created_at)")
        conn.commit()
        logger.info("Создана таблица weather_decisions")

    def _migrate_add_extended_weather_settings(self, conn):
        """Add extended weather settings: humidity threshold, per-factor toggles, wind m/s."""
        weather_new_keys = {
            "weather.wind_threshold_ms": "7.0",
            "weather.humidity_threshold_pct": "80.0",
            "weather.humidity_reduction_pct": "30",
            "weather.factor.rain": "1",
            "weather.factor.freeze": "1",
            "weather.factor.wind": "1",
            "weather.factor.humidity": "1",
            "weather.factor.heat": "1",
        }
        for key, default_val in weather_new_keys.items():
            cur = conn.execute("SELECT 1 FROM settings WHERE key = ?", (key,))
            if cur.fetchone() is None:
                conn.execute("INSERT INTO settings(key, value) VALUES(?, ?)", (key, default_val))
        conn.commit()
        logger.info("Добавлены расширенные настройки погоды в settings")

    def _migrate_wind_kmh_to_ms(self, conn):
        """Convert wind threshold from km/h to m/s if user had a custom value."""
        cur = conn.execute("SELECT value FROM settings WHERE key = 'weather.wind_threshold_kmh'")
        row = cur.fetchone()
        if row and row[0]:
            kmh = float(row[0])
            # Only convert if it differs from default 25.0 (meaning user customized it)
            if abs(kmh - 25.0) > 0.01:
                ms = round(kmh / 3.6, 1)
                conn.execute(
                    "INSERT OR REPLACE INTO settings(key, value) VALUES('weather.wind_threshold_ms', ?)", (str(ms),)
                )
                logger.info("Конвертирован порог ветра: %.0f км/ч → %.1f м/с", kmh, ms)
        conn.commit()

    # --- Weather H2: virtual water balance (additive, default off) ---

    def _migrate_add_water_balance_settings(self, conn):
        """Add H2 water-balance settings defaults (mode off by default)."""
        balance_keys = {
            "weather.balance.enabled": "0",
            "weather.balance.window_days": "3",
            "weather.balance.norm_window_days": "30",
            "weather.balance.coef_min": "50",
            "weather.balance.coef_max": "150",
            "weather.balance.kc": "1.0",
            "weather.balance.intercept_mm": "4.0",
            "weather.balance.stale_fallback_days": "2",
            "weather.balance.et0_norm_daily": "",
            "weather.balance.coef_cached": "100",
        }
        for key, default_val in balance_keys.items():
            cur = conn.execute("SELECT 1 FROM settings WHERE key = ?", (key,))
            if cur.fetchone() is None:
                conn.execute("INSERT INTO settings(key, value) VALUES(?, ?)", (key, default_val))
        conn.commit()
        logger.info("Добавлены настройки водного баланса (H2) в settings")

    def _migrate_create_water_balance_log(self, conn):
        """Create weather_balance_log for shadow-mode forecast/fact auditing."""
        conn.execute("""
            CREATE TABLE IF NOT EXISTS weather_balance_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                date TEXT,
                et0_fact REAL,
                et0_norm REAL,
                precip_fact REAL,
                precip_eff REAL,
                deficit_day REAL,
                deficit_window REAL,
                coefficient INTEGER,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        conn.execute("CREATE INDEX IF NOT EXISTS idx_weather_balance_log_date ON weather_balance_log(date)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_weather_balance_log_created ON weather_balance_log(created_at)")
        conn.commit()
        logger.info("Создана таблица weather_balance_log")

    # --- Queue & float support (spec v1.1) ---

    def _migrate_queue_and_float_support(self, conn):
        """Add float sensor fields, pause_remaining, queue log, float events tables."""
        # --- groups: float sensor columns ---
        self._add_columns_if_missing(
            conn,
            "groups",
            [
                ("float_enabled", "INTEGER DEFAULT 0"),
                ("float_mqtt_topic", "TEXT DEFAULT NULL"),
                ("float_mqtt_server_id", "INTEGER DEFAULT NULL"),
                ("float_mode", "TEXT DEFAULT 'NO'"),
                ("float_timeout_minutes", "INTEGER DEFAULT 30"),
                ("float_debounce_seconds", "INTEGER DEFAULT 5"),
            ],
        )

        # --- zones: pause_remaining_seconds ---
        self._add_columns_if_missing(
            conn,
            "zones",
            [
                ("pause_remaining_seconds", "REAL DEFAULT NULL"),
                ("pause_reason", "TEXT DEFAULT NULL"),
            ],
        )

        # --- program_queue_log table ---
        conn.execute("""
            CREATE TABLE IF NOT EXISTS program_queue_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                entry_id TEXT NOT NULL,
                program_id INTEGER NOT NULL,
                program_run_id TEXT,
                group_id INTEGER NOT NULL,
                zone_ids TEXT NOT NULL,
                scheduled_time TEXT NOT NULL,
                enqueued_at TEXT NOT NULL,
                started_at TEXT,
                completed_at TEXT,
                state TEXT NOT NULL,
                wait_seconds INTEGER,
                run_seconds INTEGER,
                created_at TEXT DEFAULT (datetime('now', 'localtime'))
            )
        """)
        conn.execute("CREATE INDEX IF NOT EXISTS idx_pql_program ON program_queue_log(program_id)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_pql_state ON program_queue_log(state)")

        # --- float_events table ---
        conn.execute("""
            CREATE TABLE IF NOT EXISTS float_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                group_id INTEGER NOT NULL,
                event_type TEXT NOT NULL,
                paused_zones TEXT,
                created_at TEXT DEFAULT (datetime('now', 'localtime'))
            )
        """)
        conn.execute("CREATE INDEX IF NOT EXISTS idx_float_events_group ON float_events(group_id)")

        # --- settings ---
        conn.execute("INSERT OR IGNORE INTO settings (key, value) VALUES ('max_queue_wait_minutes', '120')")
        conn.execute("INSERT OR IGNORE INTO settings (key, value) VALUES ('max_weather_coefficient', '200')")

        conn.commit()
        logger.info("Миграция queue_and_float_support выполнена")

    def _down_queue_and_float_support(self, conn):
        """Downgrade: remove queue/float tables and columns."""
        conn.execute("DROP TABLE IF EXISTS program_queue_log")
        conn.execute("DROP TABLE IF EXISTS float_events")
        # Remove float columns from groups
        self._recreate_table_without_columns(
            conn,
            "groups",
            [
                "float_enabled",
                "float_mqtt_topic",
                "float_mqtt_server_id",
                "float_mode",
                "float_timeout_minutes",
                "float_debounce_seconds",
            ],
        )
        # Remove pause columns from zones
        self._recreate_table_without_columns(
            conn,
            "zones",
            [
                "pause_remaining_seconds",
                "pause_reason",
            ],
        )
        # Remove settings
        conn.execute("DELETE FROM settings WHERE key IN ('max_queue_wait_minutes', 'max_weather_coefficient')")
        conn.commit()
        logger.info("Downgrade: queue_and_float_support откачена")

    def _migrate_backfill_last_watering_from_zone_runs(self, conn):
        """Issue #2: backfill ``zones.last_watering_time`` from zone_runs.

        Prior to the issue-#2 fix the codebase wrote the watering START time
        into ``last_watering_time`` (instead of end-time) at eight different
        callsites.  After the fix is deployed, zones whose state changed via
        the buggy paths still hold start-time values; zones that never ran
        since the bug was introduced may have NULL.  We can't rewrite the
        wrong-but-non-NULL values safely (we no longer know which timestamps
        came from start vs end), but we CAN repair NULL rows from the
        authoritative ``zone_runs.end_utc`` history.

        The migration is idempotent: rows with non-NULL last_watering_time
        are left alone, and re-running the SQL is a no-op once the NULLs
        are filled.
        """
        # Guard: zone_runs may not exist on extremely old DBs that
        # somehow skipped create_zone_runs_v1 (shouldn't happen — it's
        # in the same init flow — but be defensive).
        cur = conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='zone_runs'")
        if cur.fetchone() is None:
            logger.info("backfill_last_watering: zone_runs table missing, skip")
            return
        # For each zone whose last_watering_time is currently NULL,
        # set it to the most recent zone_runs.end_utc for that zone
        # (if any exists). Correlated subquery keeps it portable
        # across SQLite versions without requiring CTE recursion.
        conn.execute(
            """
            UPDATE zones
               SET last_watering_time = (
                   SELECT zr.end_utc
                     FROM zone_runs zr
                    WHERE zr.zone_id = zones.id
                      AND zr.end_utc IS NOT NULL
                    ORDER BY zr.id DESC
                   LIMIT 1
               ),
                   version = COALESCE(version, 0) + 1
             WHERE last_watering_time IS NULL
               AND EXISTS (
                   SELECT 1 FROM zone_runs zr2
                    WHERE zr2.zone_id = zones.id
                      AND zr2.end_utc IS NOT NULL
               )
            """
        )
        conn.commit()
        # Report how many rows we touched (best-effort, just for ops).
        try:
            cur2 = conn.execute("SELECT COUNT(*) FROM zones WHERE last_watering_time IS NOT NULL")
            filled = cur2.fetchone()[0]
            logger.info(
                "backfill_last_watering: zones with last_watering_time after backfill = %s",
                filled,
            )
        except sqlite3.Error:
            pass

    def _migrate_drop_last_watering_time(self, conn):
        """Drop the denormalised ``zones.last_watering_time`` column.

        Single source of truth for "when did this zone last finish watering"
        is now ``zone_runs.end_utc`` (status='ok'). The value is injected
        into zone dicts at read time by :meth:`db.zones.ZoneRepository.get_zones`
        / :meth:`db.zones.ZoneRepository.get_zone` so all API/UI consumers
        keep working unchanged.

        SQLite < 3.35 (Debian 11 / WB-244 has 3.34.1) has no native
        ``ALTER TABLE … DROP COLUMN``, so this forward path uses the dedicated
        identity-preserving rebuild helper. The generic downgrade helper stays
        unchanged. The rebuild drops all indexes on the table, so we reissue
        the indexes from
        :meth:`_migrate_add_zones_indexes` here as well.

        IRREVERSIBLE: no downgrade is registered. The column is gone with
        no preserved data; reverting the migration alone would re-create
        the column NULL — callers must re-run the issue-#2 backfill from
        zone_runs to restore values. See the rollback notes in the PR.
        """
        cur = conn.execute("PRAGMA table_info(zones)")
        cols = [c[1] for c in cur.fetchall()]
        if "last_watering_time" not in cols:
            return
        # Current forward guards on parent tables reference ``zones``. SQLite
        # reparses those triggers while the rebuilt table is being renamed and
        # otherwise fails because the old table has just been dropped. This is
        # intentionally limited to the known forward guards; the generic table
        # rebuild/downgrade contract remains unchanged.
        for trigger in (
            "trg_mqtt_servers_restrict_referenced_delete",
            "trg_groups_restrict_referenced_delete",
        ):
            conn.execute(f"DROP TRIGGER IF EXISTS {trigger}")
        self._recreate_forward_table_without_columns_preserving_identity(
            conn,
            "zones",
            ["last_watering_time"],
        )
        # Table rebuild drops all indexes — reissue ours.
        # (Mirror of _migrate_add_zones_indexes; IF NOT EXISTS so the
        # call is also safe to re-run on a manually-fixed DB.)
        conn.execute("CREATE INDEX IF NOT EXISTS idx_zones_mqtt_server ON zones(mqtt_server_id)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_zones_topic ON zones(topic)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_zones_group ON zones(group_id)")
        logger.info("Dropped zones.last_watering_time (single source of truth = zone_runs)")

    def _migrate_add_photo_thumb(self, conn):
        """Issue #11: add photo_thumb column to zones for the 400x400 thumb."""
        if self._add_column_if_missing(conn, "zones", "photo_thumb", "TEXT"):
            conn.commit()
            logger.info("Добавлено поле photo_thumb в таблицу zones")

    def _migrate_add_zone_runs_source(self, conn):
        """Issue #35: add zone_runs.source TEXT + composite index (zone_id, start_utc).

        ``source`` distinguishes programmatic vs manual runs in the history UI:
          - 'program' — opened by the scheduler (irrigation_scheduler)
          - 'manual'  — opened via the UI/API (services.zone_control)

        NULL is preserved for rows written before this migration.  A schedule
        timestamp cannot prove which program (or manual action) owned a run.
        """
        if self._add_column_if_missing(conn, "zone_runs", "source", "TEXT"):
            logger.info("Добавлено поле source в таблицу zone_runs")
        # Composite index for fast per-zone date-range scans used by the
        # /api/zones/<id>/history endpoint (filter zone_id + sort start_utc).
        conn.execute("CREATE INDEX IF NOT EXISTS idx_zone_runs_zone_start ON zone_runs(zone_id, start_utc)")
        conn.commit()

    def _migrate_add_zone_runs_confirmed(self, conn):
        """Add zone_runs.confirmed — was the relay 'on' physically confirmed
        (MQTT echo) at least once during this run.

        History must not report a successful watering when the valve never
        actually opened. ``finish_zone_run`` downgrades status 'ok' -> 'failed'
        for any run that ends with confirmed=0. The SSE hub sets it to 1 when a
        real relay-on echo arrives for the zone's open run.

        Default 0. Legacy rows finished before this migration already have an
        explicit status persisted, so the downgrade only affects runs finished
        after it.
        """
        if self._add_column_if_missing(conn, "zone_runs", "confirmed", "INTEGER DEFAULT 0"):
            logger.info("Добавлено поле confirmed в таблицу zone_runs")
        conn.commit()

    def _backfill_zone_runs_source(self, conn):
        """Leave historical rows unresolved when their execution owner is unknown."""
        preview = self._zone_runs_source_backfill_preview(conn)
        if preview["unresolved_rows"]:
            logger.warning(
                "zone_runs_backfill_source: %s rows left NULL; historical program identity is unavailable",
                preview["unresolved_rows"],
            )

    @staticmethod
    def _zone_runs_source_backfill_preview(conn: sqlite3.Connection) -> dict[str, object]:
        table = conn.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'zone_runs'").fetchone()
        unresolved_rows = 0
        if table is not None:
            columns = {row[1] for row in conn.execute("PRAGMA table_info(zone_runs)").fetchall()}
            if "source" in columns:
                unresolved_rows = int(conn.execute("SELECT COUNT(*) FROM zone_runs WHERE source IS NULL").fetchone()[0])
        return {
            "supported": False,
            "would_mutate": False,
            "error_code": "HISTORICAL_SOURCE_IDENTITY_UNAVAILABLE",
            "unresolved_rows": unresolved_rows,
        }

    def preview_zone_runs_source_backfill(self) -> dict[str, object]:
        """Count unresolved historical runs through a read-only connection."""
        uri = f"file:{quote(os.path.abspath(self.db_path))}?mode=ro"
        with sqlite3.connect(uri, uri=True, timeout=5) as conn:
            return self._zone_runs_source_backfill_preview(conn)

    def _migrate_clear_unverifiable_zone_run_sources(self, conn: sqlite3.Connection) -> None:
        """Clear labels that the retired schedule heuristic could have invented.

        The historical backfill marker is the only durable boundary available:
        rows created no later than that marker could have been rewritten from
        ``NULL`` to ``program``/``manual`` without execution identity.  We
        conservatively restore those recognised heuristic values to ``NULL``.
        Rows written after the marker retain their caller-supplied source.
        """

        marker = conn.execute("SELECT applied_at FROM migrations WHERE name = 'zone_runs_backfill_source'").fetchone()
        if marker is None:
            raise sqlite3.IntegrityError("zone_runs_backfill_source marker is missing")

        applied_at = marker[0]
        marker_julian_day = None
        if applied_at is not None:
            marker_julian_day = conn.execute("SELECT julianday(?)", (applied_at,)).fetchone()[0]
        if marker_julian_day is None:
            cursor = conn.execute("UPDATE zone_runs SET source = NULL WHERE source IN ('program', 'manual')")
        else:
            cursor = conn.execute(
                """
                UPDATE zone_runs
                SET source = NULL
                WHERE source IN ('program', 'manual')
                  AND (
                      created_at IS NULL
                      OR julianday(created_at) IS NULL
                      OR julianday(created_at) <= julianday(?)
                  )
                """,
                (marker_julian_day,),
            )
        if cursor.rowcount:
            logger.warning(
                "Cleared unverifiable historical zone_runs.source labels: %s",
                cursor.rowcount,
            )

    @staticmethod
    def _migrate_disable_unsupported_smart_programs(conn: sqlite3.Connection) -> None:
        """Disable legacy smart programs and leave a durable audit reason."""

        program_ids = [
            int(row[0])
            for row in conn.execute(
                "SELECT id FROM programs WHERE lower(trim(COALESCE(type, ''))) = 'smart' AND enabled = 1 ORDER BY id"
            ).fetchall()
        ]
        if not program_ids:
            return

        placeholders = ", ".join("?" for _ in program_ids)
        cursor = conn.execute(
            f"UPDATE programs SET enabled = 0, updated_at = CURRENT_TIMESTAMP WHERE id IN ({placeholders})",
            program_ids,
        )
        if cursor.rowcount != len(program_ids):
            raise sqlite3.IntegrityError("smart program disable count changed during migration")

        payload = json.dumps(
            {
                "error_code": "PROGRAM_TYPE_UNSUPPORTED",
                "program_type": "smart",
            },
            sort_keys=True,
        )
        for program_id in program_ids:
            conn.execute(
                """
                INSERT INTO audit_log(
                    actor, source, action_type, target, payload_json, result, error_msg
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    "system",
                    "migration",
                    "migration_disable_unsupported_smart",
                    f"program:{program_id}",
                    payload,
                    "disabled",
                    "PROGRAM_TYPE_UNSUPPORTED",
                ),
            )
        logger.warning("Disabled unsupported smart programs: %s", program_ids)

    @staticmethod
    def _migrate_zone_version_invalidation(conn: sqlite3.Connection) -> None:
        """Install the fallback version guard for direct/raw zone writers."""

        columns = {str(row[1]) for row in conn.execute("PRAGMA table_info(zones)").fetchall()}
        if "version" not in columns:
            raise sqlite3.IntegrityError("zones.version is missing")
        conn.execute("DROP TRIGGER IF EXISTS trg_zones_version_invalidate")
        conn.execute("UPDATE zones SET version = 0 WHERE version IS NULL")
        conn.execute(
            """
            CREATE TRIGGER trg_zones_version_invalidate
            AFTER UPDATE ON zones
            WHEN NEW.version IS NULL OR NEW.version <= COALESCE(OLD.version, 0)
            BEGIN
                UPDATE zones
                SET version = COALESCE(OLD.version, 0) + 1
                WHERE id = OLD.id;
            END
            """
        )

    def _migrate_create_audit_log(self, conn):
        """Create the audit_log table for principal-critical mutation tracking.

        Separate from the existing ``logs`` table, which keeps low-fidelity
        operational events.  ``audit_log`` stores who/what/when/how for every
        mutating UI/API action.  Idempotent (IF NOT EXISTS guards everywhere).
        """
        conn.execute("""
            CREATE TABLE IF NOT EXISTS audit_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ts TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
                actor TEXT,
                source TEXT NOT NULL,
                action_type TEXT NOT NULL,
                target TEXT,
                payload_json TEXT,
                result TEXT,
                error_msg TEXT,
                ip TEXT,
                duration_ms INTEGER
            )
        """)
        conn.execute("CREATE INDEX IF NOT EXISTS idx_audit_log_ts ON audit_log(ts)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_audit_log_action ON audit_log(action_type)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_audit_log_target ON audit_log(target)")
        conn.commit()
        logger.info("Создана таблица audit_log с индексами (ts/action/target)")

    def _migrate_programs_v2_fields(self, conn):
        """Add v2 fields to programs table: type, schedule_type, interval_days, even_odd, color, enabled, extra_times."""
        added = self._add_columns_if_missing(
            conn,
            "programs",
            [
                ("type", "TEXT DEFAULT 'time-based'"),
                ("schedule_type", "TEXT DEFAULT 'weekdays'"),
                ("interval_days", "INTEGER DEFAULT NULL"),
                ("even_odd", "TEXT DEFAULT NULL"),
                ("color", "TEXT DEFAULT '#42a5f5'"),
                ("enabled", "INTEGER DEFAULT 1"),
                ("extra_times", "TEXT DEFAULT '[]'"),
            ],
        )
        for col_name in added:
            logger.info(f"Добавлено поле {col_name} в таблицу programs")
        conn.commit()
        logger.info("Миграция programs v2 fields завершена")

    def _migrate_canonical_even_odd(self, conn):
        """Normalise legacy schedule_type 'even_odd' rows to canonical 'even-odd'."""
        cur = conn.execute("UPDATE programs SET schedule_type = 'even-odd' WHERE schedule_type = 'even_odd'")
        conn.commit()
        if cur.rowcount:
            logger.info("Канонизировано schedule_type even_odd → even-odd: %s программ", cur.rowcount)

    # =====================================================================
    # Downgrade methods for the last 10 migrations
    # =====================================================================

    # Registry: migration_name -> method_name (resolved at runtime via getattr)
    DOWNGRADE_REGISTRY = {
        "telegram_create_bot_users": "_down_create_bot_users",
        "telegram_create_bot_subscriptions": "_down_create_bot_subscriptions",
        "telegram_create_bot_audit": "_down_create_bot_audit",
        "telegram_add_fsm_and_notif": "_down_add_fsm_and_notif",
        "telegram_create_bot_idempotency": "_down_create_bot_idempotency",
        "encrypt_mqtt_passwords": "_down_encrypt_mqtt_passwords",
        "zones_add_fault_tracking": "_down_add_fault_tracking",
        "weather_create_cache": "_down_create_weather_cache",
        "weather_create_log": "_down_create_weather_log",
        "weather_add_settings": "_down_add_weather_settings",
        "weather_create_decisions": "_down_create_weather_decisions",
        "weather_add_extended_settings": "_down_add_extended_weather_settings",
        "weather_wind_kmh_to_ms": "_down_wind_kmh_to_ms",
        "weather_add_balance_settings": "_down_add_water_balance_settings",
        "weather_create_balance_log": "_down_create_water_balance_log",
        "queue_and_float_support": "_down_queue_and_float_support",
        "create_audit_log": "_down_create_audit_log",
        "zone_runs_add_source": "_down_add_zone_runs_source",
        "zone_runs_backfill_source": "_down_backfill_zone_runs_source",
    }

    def _down_add_zone_runs_source(self, conn):
        """Downgrade: drop idx_zone_runs_zone_start + remove source column."""
        conn.execute("DROP INDEX IF EXISTS idx_zone_runs_zone_start")
        self._recreate_table_without_columns(conn, "zone_runs", ["source"])
        # Reissue base zone_runs indexes wiped by table recreation.
        conn.execute("CREATE INDEX IF NOT EXISTS idx_zone_runs_zone ON zone_runs(zone_id)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_zone_runs_group ON zone_runs(group_id)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_zone_runs_active ON zone_runs(zone_id, end_utc)")
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_zone_runs_last_ok "
            "ON zone_runs(zone_id, end_utc DESC) "
            "WHERE status = 'ok' AND end_utc IS NOT NULL"
        )
        conn.execute("CREATE INDEX IF NOT EXISTS idx_zone_runs_start ON zone_runs(start_utc DESC)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_zone_runs_group_start ON zone_runs(group_id, start_utc DESC)")
        conn.commit()
        logger.info("Downgrade: удалена колонка source и индекс idx_zone_runs_zone_start из zone_runs")

    def _down_backfill_zone_runs_source(self, conn):
        """Downgrade backfill: blank out source values (column drop handled by sibling)."""
        try:
            conn.execute("UPDATE zone_runs SET source = NULL")
            conn.commit()
        except sqlite3.Error:
            # Column may already be gone if _down_add_zone_runs_source ran first.
            pass
        logger.info("Downgrade: zone_runs.source значения очищены")

    def _down_create_audit_log(self, conn):
        conn.execute("DROP TABLE IF EXISTS audit_log")
        conn.commit()
        logger.info("Downgrade: удалена таблица audit_log")

    def _down_create_bot_users(self, conn):
        conn.execute("DROP TABLE IF EXISTS bot_users")
        conn.commit()
        logger.info("Downgrade: удалена таблица bot_users")

    def _down_create_bot_subscriptions(self, conn):
        conn.execute("DROP TABLE IF EXISTS bot_subscriptions")
        conn.commit()
        logger.info("Downgrade: удалена таблица bot_subscriptions")

    def _down_create_bot_audit(self, conn):
        conn.execute("DROP TABLE IF EXISTS bot_audit")
        conn.commit()
        logger.info("Downgrade: удалена таблица bot_audit")

    def _down_add_fsm_and_notif(self, conn):
        drop_cols = [
            "fsm_state",
            "fsm_data",
            "notif_critical",
            "notif_emergency",
            "notif_postpone",
            "notif_zone_events",
            "notif_rain",
        ]
        self._recreate_table_without_columns(conn, "bot_users", drop_cols)
        logger.info("Downgrade: удалены FSM/notif колонки из bot_users")

    def _down_create_bot_idempotency(self, conn):
        conn.execute("DROP TABLE IF EXISTS bot_idempotency")
        conn.commit()
        logger.info("Downgrade: удалена таблица bot_idempotency")

    def _down_encrypt_mqtt_passwords(self, conn):
        # Decrypting passwords is not safely reversible — mark migration as rolled back
        # but leave data as-is (encrypted passwords will fail on connect; user must re-enter)
        logger.warning(
            "Downgrade: encrypt_mqtt_passwords — зашифрованные пароли НЕ расшифрованы. "
            "Пользователь должен ввести пароли заново."
        )

    def _down_add_fault_tracking(self, conn):
        self._recreate_table_without_columns(conn, "zones", ["last_fault", "fault_count"])
        logger.info("Downgrade: удалены поля last_fault, fault_count из zones")

    def _down_create_weather_cache(self, conn):
        conn.execute("DROP TABLE IF EXISTS weather_cache")
        conn.commit()
        logger.info("Downgrade: удалена таблица weather_cache")

    def _down_create_weather_log(self, conn):
        conn.execute("DROP TABLE IF EXISTS weather_log")
        conn.commit()
        logger.info("Downgrade: удалена таблица weather_log")

    def _down_add_weather_settings(self, conn):
        weather_keys = [
            "weather.enabled",
            "weather.latitude",
            "weather.longitude",
            "weather.rain_threshold_mm",
            "weather.freeze_threshold_c",
            "weather.wind_threshold_kmh",
        ]
        for key in weather_keys:
            conn.execute("DELETE FROM settings WHERE key = ?", (key,))
        conn.commit()
        logger.info("Downgrade: удалены настройки погоды из settings")

    def _down_create_weather_decisions(self, conn):
        conn.execute("DROP TABLE IF EXISTS weather_decisions")
        conn.commit()
        logger.info("Downgrade: удалена таблица weather_decisions")

    def _down_add_extended_weather_settings(self, conn):
        extended_keys = [
            "weather.wind_threshold_ms",
            "weather.humidity_threshold_pct",
            "weather.humidity_reduction_pct",
            "weather.factor.rain",
            "weather.factor.freeze",
            "weather.factor.wind",
            "weather.factor.humidity",
            "weather.factor.heat",
        ]
        for key in extended_keys:
            conn.execute("DELETE FROM settings WHERE key = ?", (key,))
        conn.commit()
        logger.info("Downgrade: удалены расширенные настройки погоды из settings")

    def _down_wind_kmh_to_ms(self, conn):
        # Nothing to reverse — the km/h value was kept, ms value will be removed
        # by _down_add_extended_weather_settings
        logger.info("Downgrade: weather_wind_kmh_to_ms — noop (ms key removed by extended settings downgrade)")

    def _down_add_water_balance_settings(self, conn):
        balance_keys = [
            "weather.balance.enabled",
            "weather.balance.window_days",
            "weather.balance.norm_window_days",
            "weather.balance.coef_min",
            "weather.balance.coef_max",
            "weather.balance.kc",
            "weather.balance.intercept_mm",
            "weather.balance.stale_fallback_days",
            "weather.balance.et0_norm_daily",
            "weather.balance.coef_cached",
            "weather.balance.deficit_buffer",
            "weather.balance.last_recalc_date",
            "weather.balance.norm_last_day",
        ]
        for key in balance_keys:
            conn.execute("DELETE FROM settings WHERE key = ?", (key,))
        conn.commit()
        logger.info("Downgrade: удалены настройки водного баланса (H2) из settings")

    def _down_create_water_balance_log(self, conn):
        conn.execute("DROP TABLE IF EXISTS weather_balance_log")
        conn.commit()
        logger.info("Downgrade: удалена таблица weather_balance_log")
