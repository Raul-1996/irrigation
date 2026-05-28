import json
import logging
import sqlite3
from datetime import UTC

from werkzeug.security import generate_password_hash

from utils import encrypt_secret

logger = logging.getLogger(__name__)


class MigrationRunner:
    """Runs all named migrations for the irrigation database."""

    def __init__(self, db_path: str):
        self.db_path = db_path

    def init_database(self):
        """Initialize database schema and run all migrations."""
        try:
            with sqlite3.connect(self.db_path, timeout=5) as conn:
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
                        id INTEGER PRIMARY KEY,
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
                        id INTEGER PRIMARY KEY,
                        name TEXT NOT NULL UNIQUE,
                        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                        updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                    )
                """)
                conn.execute("""
                    CREATE TABLE IF NOT EXISTS programs (
                        id INTEGER PRIMARY KEY,
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
                        PRIMARY KEY (program_id, run_date, group_id)
                    )
                """)

                # Create indexes
                conn.execute("CREATE INDEX IF NOT EXISTS idx_zones_group ON zones(group_id)")
                conn.execute("CREATE INDEX IF NOT EXISTS idx_logs_type ON logs(type)")
                conn.execute("CREATE INDEX IF NOT EXISTS idx_logs_timestamp ON logs(timestamp)")
                conn.execute("CREATE INDEX IF NOT EXISTS idx_water_zone ON water_usage(zone_id)")
                conn.execute("CREATE INDEX IF NOT EXISTS idx_water_timestamp ON water_usage(timestamp)")

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
                # Issue #35: add zone_runs.source ('program' / 'manual') + composite
                # index, then backfill historical rows by matching start_utc to
                # the active programs' schedules (±120s) — manual otherwise.
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
                # Issue #52 — in-app auth from scratch.
                self._apply_named_migration(conn, "create_users", self._migrate_create_users)
                self._apply_named_migration(conn, "seed_default_users", self._migrate_seed_default_users)

                logger.info("База данных инициализирована успешно")

        except (ConnectionError, TimeoutError, OSError) as e:
            logger.error("Ошибка инициализации базы данных: %s", e)
            raise

    def _insert_initial_data(self, conn):
        """Вставить начальные данные."""
        try:
            cursor = conn.execute("SELECT COUNT(*) FROM zones")
            if cursor.fetchone()[0] > 0:
                cur = conn.execute("SELECT value FROM settings WHERE key = ? LIMIT 1", ("password_hash",))
                if cur.fetchone() is None:
                    conn.execute(
                        "INSERT OR REPLACE INTO settings(key, value) VALUES (?, ?)",
                        ("password_hash", generate_password_hash("1234", method="pbkdf2:sha256")),
                    )
                    conn.commit()
                return

            groups = [(1, "Насос-1"), (999, "БЕЗ ПОЛИВА")]
            for group_id, name in groups:
                conn.execute("INSERT OR IGNORE INTO groups (id, name) VALUES (?, ?)", (group_id, name))
            conn.commit()
            conn.execute(
                "INSERT OR REPLACE INTO settings(key, value) VALUES (?, ?)",
                ("password_hash", generate_password_hash("1234", method="pbkdf2:sha256:120000")),
            )
            conn.commit()
            logger.info("Начальные данные вставлены: группы 1 (Насос-1) и 999 (БЕЗ ПОЛИВА)")
        except sqlite3.Error as e:
            logger.error("Ошибка вставки начальных данных: %s", e)

    def _apply_named_migration(self, conn, name: str, func):
        try:
            cur = conn.execute("SELECT name FROM migrations WHERE name = ? LIMIT 1", (name,))
            row = cur.fetchone()
            if row:
                return
            func(conn)
            conn.execute("INSERT OR REPLACE INTO migrations(name) VALUES (?)", (name,))
            conn.commit()
        except sqlite3.Error as e:
            logger.error("Ошибка применения миграции %s: %s", name, e)

    def rollback_migration(self, name: str) -> bool:
        """Rollback (downgrade) a single named migration.

        Returns True if rollback succeeded, False otherwise.
        The migration must be in the DOWNGRADE_REGISTRY and must have been applied.
        """
        method_name = self.DOWNGRADE_REGISTRY.get(name)
        if method_name is None:
            logger.error("Миграция %s не поддерживает downgrade", name)
            return False
        down_func = getattr(self, method_name, None)
        if down_func is None:
            logger.error("Метод %s не найден для миграции %s", method_name, name)
            return False
        try:
            with sqlite3.connect(self.db_path, timeout=5) as conn:
                conn.execute("PRAGMA foreign_keys=OFF")
                cur = conn.execute("SELECT name FROM migrations WHERE name = ? LIMIT 1", (name,))
                if cur.fetchone() is None:
                    logger.warning("Миграция %s не была применена, пропуск rollback", name)
                    return False
                down_func(conn)
                conn.execute("DELETE FROM migrations WHERE name = ?", (name,))
                conn.execute("PRAGMA foreign_keys=ON")
                conn.commit()
                logger.info("Миграция %s откачена успешно", name)
                return True
        except sqlite3.Error as e:
            logger.error("Ошибка отката миграции %s: %s", name, e)
            return False

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

    # --- All migration methods ---

    def _migrate_days_format(self, conn):
        try:
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
        except sqlite3.Error as e:
            logger.error("Ошибка миграции формата дней: %s", e)

    def _migrate_add_postpone_reason(self, conn):
        try:
            cursor = conn.execute("PRAGMA table_info(zones)")
            columns = [column[1] for column in cursor.fetchall()]
            if "postpone_reason" not in columns:
                conn.execute("ALTER TABLE zones ADD COLUMN postpone_reason TEXT")
                conn.commit()
                logger.info("Добавлено поле postpone_reason в таблицу zones")
        except sqlite3.Error as e:
            logger.error("Ошибка миграции postpone_reason: %s", e)

    def _migrate_add_watering_start_time(self, conn):
        try:
            cursor = conn.execute("PRAGMA table_info(zones)")
            columns = [column[1] for column in cursor.fetchall()]
            if "watering_start_time" not in columns:
                conn.execute("ALTER TABLE zones ADD COLUMN watering_start_time TEXT")
                conn.commit()
                logger.info("Добавлено поле watering_start_time в таблицу zones")
        except sqlite3.Error as e:
            logger.error("Ошибка миграции watering_start_time: %s", e)

    def _migrate_add_scheduled_start_time(self, conn):
        try:
            cursor = conn.execute("PRAGMA table_info(zones)")
            columns = [column[1] for column in cursor.fetchall()]
            if "scheduled_start_time" not in columns:
                conn.execute("ALTER TABLE zones ADD COLUMN scheduled_start_time TEXT")
                conn.commit()
                logger.info("Добавлено поле scheduled_start_time в таблицу zones")
        except sqlite3.Error as e:
            logger.error("Ошибка миграции scheduled_start_time: %s", e)

    def _migrate_add_last_watering_time(self, conn):
        try:
            cursor = conn.execute("PRAGMA table_info(zones)")
            columns = [column[1] for column in cursor.fetchall()]
            if "last_watering_time" not in columns:
                conn.execute("ALTER TABLE zones ADD COLUMN last_watering_time TEXT")
                conn.commit()
                logger.info("Добавлено поле last_watering_time в таблицу zones")
        except sqlite3.Error as e:
            logger.error("Ошибка миграции last_watering_time: %s", e)

    def _migrate_add_watering_start_source(self, conn):
        try:
            cursor = conn.execute("PRAGMA table_info(zones)")
            columns = [column[1] for column in cursor.fetchall()]
            if "watering_start_source" not in columns:
                conn.execute("ALTER TABLE zones ADD COLUMN watering_start_source TEXT")
                conn.commit()
                logger.info("Добавлено поле watering_start_source в таблицу zones")
        except sqlite3.Error as e:
            logger.error("Ошибка миграции watering_start_source: %s", e)

    def _migrate_add_group_rain_flag(self, conn):
        try:
            cursor = conn.execute("PRAGMA table_info(groups)")
            columns = [column[1] for column in cursor.fetchall()]
            if "use_rain_sensor" not in columns:
                conn.execute("ALTER TABLE groups ADD COLUMN use_rain_sensor INTEGER DEFAULT 0")
                conn.commit()
                logger.info("Добавлено поле use_rain_sensor в таблицу groups")
        except sqlite3.Error as e:
            logger.error("Ошибка миграции use_rain_sensor: %s", e)

    def _migrate_add_mqtt_servers(self, conn):
        try:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS mqtt_servers (
                    id INTEGER PRIMARY KEY,
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
        except sqlite3.Error as e:
            logger.error("Ошибка миграции mqtt_servers: %s", e)

    def _migrate_add_zone_mqtt_server_id(self, conn):
        try:
            cursor = conn.execute("PRAGMA table_info(zones)")
            columns = [column[1] for column in cursor.fetchall()]
            if "mqtt_server_id" not in columns:
                conn.execute("ALTER TABLE zones ADD COLUMN mqtt_server_id INTEGER")
                conn.commit()
                logger.info("Добавлено поле mqtt_server_id в таблицу zones")
        except sqlite3.Error as e:
            logger.error("Ошибка миграции mqtt_server_id: %s", e)

    def _migrate_ensure_special_group(self, conn):
        try:
            cur = conn.execute("SELECT COUNT(*) FROM groups WHERE id = 999")
            cnt = cur.fetchone()[0] if cur else 0
            if cnt == 0:
                conn.execute("INSERT OR IGNORE INTO groups (id, name) VALUES (999, 'БЕЗ ПОЛИВА')")
                conn.commit()
                logger.info("Добавлена служебная группа 999 'БЕЗ ПОЛИВА'")
        except sqlite3.Error as e:
            logger.error("Ошибка миграции ensure_special_group: %s", e)

    def _migrate_add_zones_indexes(self, conn):
        try:
            conn.execute("CREATE INDEX IF NOT EXISTS idx_zones_mqtt_server ON zones(mqtt_server_id)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_zones_topic ON zones(topic)")
            conn.commit()
        except sqlite3.Error as e:
            logger.error("Ошибка миграции индексов zones: %s", e)

    def _migrate_add_mqtt_tls_options(self, conn):
        try:
            cursor = conn.execute("PRAGMA table_info(mqtt_servers)")
            columns = [column[1] for column in cursor.fetchall()]
            if "tls_enabled" not in columns:
                conn.execute("ALTER TABLE mqtt_servers ADD COLUMN tls_enabled INTEGER DEFAULT 0")
            if "tls_ca_path" not in columns:
                conn.execute("ALTER TABLE mqtt_servers ADD COLUMN tls_ca_path TEXT")
            if "tls_cert_path" not in columns:
                conn.execute("ALTER TABLE mqtt_servers ADD COLUMN tls_cert_path TEXT")
            if "tls_key_path" not in columns:
                conn.execute("ALTER TABLE mqtt_servers ADD COLUMN tls_key_path TEXT")
            if "tls_insecure" not in columns:
                conn.execute("ALTER TABLE mqtt_servers ADD COLUMN tls_insecure INTEGER DEFAULT 0")
            if "tls_version" not in columns:
                conn.execute("ALTER TABLE mqtt_servers ADD COLUMN tls_version TEXT")
            conn.commit()
        except sqlite3.Error as e:
            logger.error("Ошибка миграции mqtt_tls_options: %s", e)

    def _migrate_add_zone_control_fields(self, conn):
        try:
            cursor = conn.execute("PRAGMA table_info(zones)")
            columns = [column[1] for column in cursor.fetchall()]
            if "planned_end_time" not in columns:
                conn.execute("ALTER TABLE zones ADD COLUMN planned_end_time TEXT")
            if "sequence_id" not in columns:
                conn.execute("ALTER TABLE zones ADD COLUMN sequence_id TEXT")
            if "command_id" not in columns:
                conn.execute("ALTER TABLE zones ADD COLUMN command_id TEXT")
            if "version" not in columns:
                conn.execute("ALTER TABLE zones ADD COLUMN version INTEGER DEFAULT 0")
            conn.commit()
            logger.info("Добавлены поля planned_end_time, sequence_id, command_id, version в zones")
        except sqlite3.Error as e:
            logger.error("Ошибка миграции zone_control_fields: %s", e)

    def _migrate_add_commanded_observed(self, conn):
        try:
            cursor = conn.execute("PRAGMA table_info(zones)")
            columns = [column[1] for column in cursor.fetchall()]
            if "commanded_state" not in columns:
                conn.execute("ALTER TABLE zones ADD COLUMN commanded_state TEXT")
            if "observed_state" not in columns:
                conn.execute("ALTER TABLE zones ADD COLUMN observed_state TEXT")
            conn.commit()
            logger.info("Добавлены поля commanded_state, observed_state в zones")
        except sqlite3.Error as e:
            logger.error("Ошибка миграции commanded/observed: %s", e)

    def _migrate_add_groups_master_and_sensors(self, conn):
        try:
            cursor = conn.execute("PRAGMA table_info(groups)")
            columns = [column[1] for column in cursor.fetchall()]

            def add(col, ddl):
                if col not in columns:
                    conn.execute(ddl)

            add("use_master_valve", "ALTER TABLE groups ADD COLUMN use_master_valve INTEGER DEFAULT 0")
            add("master_mqtt_topic", 'ALTER TABLE groups ADD COLUMN master_mqtt_topic TEXT DEFAULT ""')
            add("master_mode", 'ALTER TABLE groups ADD COLUMN master_mode TEXT DEFAULT "NC"')
            add("master_mqtt_server_id", "ALTER TABLE groups ADD COLUMN master_mqtt_server_id INTEGER")
            add("master_valve_observed", "ALTER TABLE groups ADD COLUMN master_valve_observed TEXT")
            add("master_close_delay_sec", "ALTER TABLE groups ADD COLUMN master_close_delay_sec INTEGER DEFAULT 60")
            add("use_pressure_sensor", "ALTER TABLE groups ADD COLUMN use_pressure_sensor INTEGER DEFAULT 0")
            add("pressure_mqtt_topic", 'ALTER TABLE groups ADD COLUMN pressure_mqtt_topic TEXT DEFAULT ""')
            add("pressure_unit", 'ALTER TABLE groups ADD COLUMN pressure_unit TEXT DEFAULT "bar"')
            add("pressure_mqtt_server_id", "ALTER TABLE groups ADD COLUMN pressure_mqtt_server_id INTEGER")
            add("use_water_meter", "ALTER TABLE groups ADD COLUMN use_water_meter INTEGER DEFAULT 0")
            add("water_mqtt_topic", 'ALTER TABLE groups ADD COLUMN water_mqtt_topic TEXT DEFAULT ""')
            add("water_mqtt_server_id", "ALTER TABLE groups ADD COLUMN water_mqtt_server_id INTEGER")
            add("water_pulse_size", 'ALTER TABLE groups ADD COLUMN water_pulse_size TEXT DEFAULT "1l"')
            add("water_base_value_m3", "ALTER TABLE groups ADD COLUMN water_base_value_m3 REAL DEFAULT 0")
            add("water_base_pulses", "ALTER TABLE groups ADD COLUMN water_base_pulses INTEGER DEFAULT 0")
            conn.commit()
            logger.info("Добавлены поля мастер-клапана и сенсоров в таблицу groups")
        except sqlite3.Error as e:
            logger.error("Ошибка миграции groups_add_master_and_sensors: %s", e)

    def _migrate_add_groups_master_valve_observed(self, conn):
        try:
            cursor = conn.execute("PRAGMA table_info(groups)")
            cols = [r[1] for r in cursor.fetchall()]
            if "master_valve_observed" not in cols:
                conn.execute("ALTER TABLE groups ADD COLUMN master_valve_observed TEXT")
                conn.commit()
                logger.info("Добавлено поле master_valve_observed в groups")
        except sqlite3.Error as e:
            logger.error("Ошибка миграции groups_add_master_valve_observed: %s", e)

    def _migrate_add_groups_master_close_delay_sec(self, conn):
        try:
            cursor = conn.execute("PRAGMA table_info(groups)")
            cols = [r[1] for r in cursor.fetchall()]
            if "master_close_delay_sec" not in cols:
                conn.execute("ALTER TABLE groups ADD COLUMN master_close_delay_sec INTEGER DEFAULT 60")
                conn.commit()
                logger.info("Добавлено поле master_close_delay_sec в groups")
        except sqlite3.Error as e:
            logger.error("Ошибка миграции groups_add_master_close_delay_sec: %s", e)

    def _migrate_add_groups_water_meter_extended(self, conn):
        try:
            cursor = conn.execute("PRAGMA table_info(groups)")
            cols = [r[1] for r in cursor.fetchall()]
            if "water_pulse_size" not in cols:
                conn.execute('ALTER TABLE groups ADD COLUMN water_pulse_size TEXT DEFAULT "1l"')
            if "water_base_value_m3" not in cols:
                conn.execute("ALTER TABLE groups ADD COLUMN water_base_value_m3 REAL DEFAULT 0")
            if "water_base_pulses" not in cols:
                conn.execute("ALTER TABLE groups ADD COLUMN water_base_pulses INTEGER DEFAULT 0")
            conn.commit()
            logger.info("Добавлены поля water_pulse_size, water_base_value_m3, water_base_pulses в groups")
        except sqlite3.Error as e:
            logger.error("Ошибка миграции groups_add_water_meter_extended: %s", e)

    def _migrate_add_zones_water_stats(self, conn):
        try:
            cursor = conn.execute("PRAGMA table_info(zones)")
            cols = [r[1] for r in cursor.fetchall()]
            if "last_avg_flow_lpm" not in cols:
                conn.execute("ALTER TABLE zones ADD COLUMN last_avg_flow_lpm REAL")
            if "last_total_liters" not in cols:
                conn.execute("ALTER TABLE zones ADD COLUMN last_total_liters REAL")
            conn.commit()
            logger.info("Добавлены поля last_avg_flow_lpm, last_total_liters в zones")
        except sqlite3.Error as e:
            logger.error("Ошибка миграции zones_add_water_stats: %s", e)

    def _migrate_create_zone_runs(self, conn):
        try:
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
            conn.commit()
            logger.info("Создана таблица zone_runs")
        except sqlite3.Error as e:
            logger.error("Ошибка миграции create_zone_runs_v1: %s", e)

    def _migrate_add_telegram_settings(self, conn):
        try:
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
        except sqlite3.Error as e:
            logger.error("Ошибка миграции telegram_add_settings_fields: %s", e)

    def _migrate_create_bot_users(self, conn):
        try:
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
        except sqlite3.Error as e:
            logger.error("Ошибка миграции telegram_create_bot_users: %s", e)

    def _migrate_create_bot_subscriptions(self, conn):
        try:
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
        except sqlite3.Error as e:
            logger.error("Ошибка миграции telegram_create_bot_subscriptions: %s", e)

    def _migrate_create_bot_audit(self, conn):
        try:
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
        except sqlite3.Error as e:
            logger.error("Ошибка миграции telegram_create_bot_audit: %s", e)

    def _migrate_add_fsm_and_notif(self, conn):
        try:
            cols = {
                "fsm_state": "ALTER TABLE bot_users ADD COLUMN fsm_state TEXT",
                "fsm_data": "ALTER TABLE bot_users ADD COLUMN fsm_data TEXT",
                "notif_critical": "ALTER TABLE bot_users ADD COLUMN notif_critical INTEGER DEFAULT 1",
                "notif_emergency": "ALTER TABLE bot_users ADD COLUMN notif_emergency INTEGER DEFAULT 1",
                "notif_postpone": "ALTER TABLE bot_users ADD COLUMN notif_postpone INTEGER DEFAULT 1",
                "notif_zone_events": "ALTER TABLE bot_users ADD COLUMN notif_zone_events INTEGER DEFAULT 0",
                "notif_rain": "ALTER TABLE bot_users ADD COLUMN notif_rain INTEGER DEFAULT 0",
            }
            cur = conn.execute("PRAGMA table_info(bot_users)")
            existing = {row[1] for row in cur.fetchall()}
            for name, ddl in cols.items():
                if name not in existing:
                    try:
                        conn.execute(ddl)
                    except sqlite3.Error as e:
                        logger.debug("Не удалось добавить колонку %s: %s", name, e)
            conn.commit()
        except sqlite3.Error as e:
            logger.error("Ошибка миграции telegram_add_fsm_and_notif: %s", e)

    def _migrate_create_bot_idempotency(self, conn):
        try:
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
        except sqlite3.Error as e:
            logger.error("Ошибка миграции telegram_create_bot_idempotency: %s", e)

    def _migrate_encrypt_mqtt_passwords(self, conn):
        try:
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
        except sqlite3.Error as e:
            logger.error("Ошибка миграции encrypt_mqtt_passwords: %s", e)

    def _migrate_add_fault_tracking(self, conn):
        try:
            cursor = conn.execute("PRAGMA table_info(zones)")
            columns = [col[1] for col in cursor.fetchall()]
            if "last_fault" not in columns:
                conn.execute("ALTER TABLE zones ADD COLUMN last_fault TEXT")
            if "fault_count" not in columns:
                conn.execute("ALTER TABLE zones ADD COLUMN fault_count INTEGER DEFAULT 0")
            conn.commit()
            logger.info("Добавлены поля last_fault, fault_count в zones")
        except sqlite3.Error as e:
            logger.error("Ошибка миграции zones_add_fault_tracking: %s", e)

    def _migrate_create_weather_cache(self, conn):
        try:
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
        except sqlite3.Error as e:
            logger.error("Ошибка миграции weather_create_cache: %s", e)

    def _migrate_create_weather_log(self, conn):
        try:
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
        except sqlite3.Error as e:
            logger.error("Ошибка миграции weather_create_log: %s", e)

    def _migrate_add_weather_settings(self, conn):
        try:
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
        except sqlite3.Error as e:
            logger.error("Ошибка миграции weather_add_settings: %s", e)

    # --- Weather v2 migrations ---

    def _migrate_create_weather_decisions(self, conn):
        """Create weather_decisions table for tracking irrigation decisions."""
        try:
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
        except sqlite3.Error as e:
            logger.error("Ошибка миграции weather_create_decisions: %s", e)

    def _migrate_add_extended_weather_settings(self, conn):
        """Add extended weather settings: humidity threshold, per-factor toggles, wind m/s."""
        try:
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
        except sqlite3.Error as e:
            logger.error("Ошибка миграции weather_add_extended_settings: %s", e)

    def _migrate_wind_kmh_to_ms(self, conn):
        """Convert wind threshold from km/h to m/s if user had a custom value."""
        try:
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
        except (sqlite3.Error, ValueError, TypeError) as e:
            logger.error("Ошибка миграции weather_wind_kmh_to_ms: %s", e)

    # --- Queue & float support (spec v1.1) ---

    def _migrate_queue_and_float_support(self, conn):
        """Add float sensor fields, pause_remaining, queue log, float events tables."""
        try:
            # --- groups: float sensor columns ---
            gcur = conn.execute("PRAGMA table_info(groups)")
            gcols = [r[1] for r in gcur.fetchall()]

            def _add_group(col, ddl):
                if col not in gcols:
                    conn.execute(ddl)

            _add_group("float_enabled", "ALTER TABLE groups ADD COLUMN float_enabled INTEGER DEFAULT 0")
            _add_group("float_mqtt_topic", "ALTER TABLE groups ADD COLUMN float_mqtt_topic TEXT DEFAULT NULL")
            _add_group(
                "float_mqtt_server_id", "ALTER TABLE groups ADD COLUMN float_mqtt_server_id INTEGER DEFAULT NULL"
            )
            _add_group("float_mode", "ALTER TABLE groups ADD COLUMN float_mode TEXT DEFAULT 'NO'")
            _add_group(
                "float_timeout_minutes", "ALTER TABLE groups ADD COLUMN float_timeout_minutes INTEGER DEFAULT 30"
            )
            _add_group(
                "float_debounce_seconds", "ALTER TABLE groups ADD COLUMN float_debounce_seconds INTEGER DEFAULT 5"
            )

            # --- zones: pause_remaining_seconds ---
            zcur = conn.execute("PRAGMA table_info(zones)")
            zcols = [r[1] for r in zcur.fetchall()]
            if "pause_remaining_seconds" not in zcols:
                conn.execute("ALTER TABLE zones ADD COLUMN pause_remaining_seconds REAL DEFAULT NULL")
            if "pause_reason" not in zcols:
                conn.execute("ALTER TABLE zones ADD COLUMN pause_reason TEXT DEFAULT NULL")

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
        except sqlite3.Error as e:
            logger.error("Ошибка миграции queue_and_float_support: %s", e)

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
        try:
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
                   )
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
        except sqlite3.Error as e:
            logger.error("Ошибка миграции backfill_last_watering_from_zone_runs: %s", e)

    def _migrate_drop_last_watering_time(self, conn):
        """Drop the denormalised ``zones.last_watering_time`` column.

        Single source of truth for "when did this zone last finish watering"
        is now ``zone_runs.end_utc`` (status='ok'). The value is injected
        into zone dicts at read time by :meth:`db.zones.ZoneRepository.get_zones`
        / :meth:`db.zones.ZoneRepository.get_zone` so all API/UI consumers
        keep working unchanged.

        SQLite < 3.35 (Debian 11 / WB-244 has 3.34.1) has no native
        ``ALTER TABLE … DROP COLUMN``, so we use the table-rebuild helper
        :meth:`_recreate_table_without_columns` which preserves PK
        AUTOINCREMENT and column defaults. The rebuild drops all indexes on
        the table, so we reissue the indexes from
        :meth:`_migrate_add_zones_indexes` here as well.

        IRREVERSIBLE: no downgrade is registered. The column is gone with
        no preserved data; reverting the migration alone would re-create
        the column NULL — callers must re-run the issue-#2 backfill from
        zone_runs to restore values. See the rollback notes in the PR.
        """
        try:
            cur = conn.execute("PRAGMA table_info(zones)")
            cols = [c[1] for c in cur.fetchall()]
            if "last_watering_time" not in cols:
                return
            self._recreate_table_without_columns(conn, "zones", ["last_watering_time"])
            # Table rebuild drops all indexes — reissue ours.
            # (Mirror of _migrate_add_zones_indexes; IF NOT EXISTS so the
            # call is also safe to re-run on a manually-fixed DB.)
            conn.execute("CREATE INDEX IF NOT EXISTS idx_zones_mqtt_server ON zones(mqtt_server_id)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_zones_topic ON zones(topic)")
            conn.commit()
            logger.info("Dropped zones.last_watering_time (single source of truth = zone_runs)")
        except sqlite3.Error as e:
            logger.error("drop_last_watering_time: %s", e)

    def _migrate_add_photo_thumb(self, conn):
        """Issue #11: add photo_thumb column to zones for the 400x400 thumb."""
        try:
            cursor = conn.execute("PRAGMA table_info(zones)")
            columns = [column[1] for column in cursor.fetchall()]
            if "photo_thumb" not in columns:
                conn.execute("ALTER TABLE zones ADD COLUMN photo_thumb TEXT")
                conn.commit()
                logger.info("Добавлено поле photo_thumb в таблицу zones")
        except sqlite3.Error as e:
            logger.error("Ошибка миграции zones_add_photo_thumb: %s", e)

    def _migrate_add_zone_runs_source(self, conn):
        """Issue #35: add zone_runs.source TEXT + composite index (zone_id, start_utc).

        ``source`` distinguishes programmatic vs manual runs in the history UI:
          - 'program' — opened by the scheduler (irrigation_scheduler)
          - 'manual'  — opened via the UI/API (services.zone_control)

        NULL is allowed for rows written before this migration; the follow-up
        backfill migration ``zone_runs_backfill_source`` fills them in by
        matching start_utc against the active programs' schedules.
        """
        try:
            cursor = conn.execute("PRAGMA table_info(zone_runs)")
            columns = [column[1] for column in cursor.fetchall()]
            if "source" not in columns:
                conn.execute("ALTER TABLE zone_runs ADD COLUMN source TEXT")
                logger.info("Добавлено поле source в таблицу zone_runs")
            # Composite index for fast per-zone date-range scans used by the
            # /api/zones/<id>/history endpoint (filter zone_id + sort start_utc).
            conn.execute("CREATE INDEX IF NOT EXISTS idx_zone_runs_zone_start ON zone_runs(zone_id, start_utc)")
            conn.commit()
        except sqlite3.Error as e:
            logger.error("Ошибка миграции zone_runs_add_source: %s", e)

    def _backfill_zone_runs_source(self, conn):
        """Issue #35: backfill source on pre-existing zone_runs.

        Algorithm (decisions Q3=a):
          For each row with source IS NULL:
            - parse start_utc (ISO 8601, possibly with trailing 'Z')
            - convert to local time-of-day (HH:MM:SS) and weekday/day-of-month
            - look up active programs that include the run's zone
            - if any program's scheduled time on that calendar day is within
              ±120 seconds of start_utc, mark as 'program', else 'manual'

        Only enabled programs that contain the zone are considered. Programs
        that were deleted/disabled after the run will produce a false-positive
        'manual' — accepted per decisions Q3.
        """
        try:
            # Guard: zone_runs may be absent on very old/odd DBs.
            cur = conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='zone_runs'")
            if cur.fetchone() is None:
                logger.info("zone_runs_backfill_source: zone_runs absent, skip")
                return
            # Pull NULL rows.
            cur = conn.execute("SELECT id, zone_id, start_utc FROM zone_runs WHERE source IS NULL")
            null_rows = cur.fetchall()
            if not null_rows:
                logger.info("zone_runs_backfill_source: nothing to backfill")
                return
            # Load programs (raw, since we run inside migration before
            # repositories are guaranteed wired). Treat 'enabled' missing as 1.
            cur = conn.execute("PRAGMA table_info(programs)")
            prog_cols = {row[1] for row in cur.fetchall()}
            has_enabled = "enabled" in prog_cols
            has_extra = "extra_times" in prog_cols
            has_sched_type = "schedule_type" in prog_cols
            has_iv_days = "interval_days" in prog_cols
            has_eo = "even_odd" in prog_cols
            select_cols = ["id", "time", "days", "zones"]
            if has_enabled:
                select_cols.append("enabled")
            if has_extra:
                select_cols.append("extra_times")
            if has_sched_type:
                select_cols.append("schedule_type")
            if has_iv_days:
                select_cols.append("interval_days")
            if has_eo:
                select_cols.append("even_odd")
            cur = conn.execute(f"SELECT {', '.join(select_cols)} FROM programs")
            programs = []
            for row in cur.fetchall():
                rec = dict(zip(select_cols, row))
                if has_enabled and int(rec.get("enabled") or 0) == 0:
                    continue
                try:
                    rec_zones = set(int(z) for z in json.loads(rec.get("zones") or "[]"))
                except (ValueError, TypeError):
                    rec_zones = set()
                if not rec_zones:
                    continue
                try:
                    rec_days = [int(d) for d in json.loads(rec.get("days") or "[]")]
                except (ValueError, TypeError):
                    rec_days = []
                times = [rec.get("time")] if rec.get("time") else []
                if has_extra:
                    try:
                        extra = json.loads(rec.get("extra_times") or "[]")
                        times.extend([t for t in extra if t])
                    except (ValueError, TypeError):
                        pass
                programs.append(
                    {
                        "id": int(rec.get("id") or 0),
                        "zones": rec_zones,
                        "days": rec_days,
                        "times": times,
                        "schedule_type": rec.get("schedule_type") or "weekdays",
                        "interval_days": rec.get("interval_days"),
                        "even_odd": rec.get("even_odd"),
                    }
                )

            from datetime import datetime

            updated_program = 0
            updated_manual = 0
            for run_id, zone_id, start_utc in null_rows:
                if not start_utc:
                    # No timestamp — default to 'manual' (nothing to match against).
                    conn.execute(
                        "UPDATE zone_runs SET source = ? WHERE id = ?",
                        ("manual", run_id),
                    )
                    updated_manual += 1
                    continue
                # Parse start_utc — be tolerant: accept '...Z' or '+00:00'.
                ts_str = start_utc.replace("Z", "+00:00") if isinstance(start_utc, str) else None
                try:
                    dt_utc = datetime.fromisoformat(ts_str)
                except (ValueError, TypeError):
                    conn.execute(
                        "UPDATE zone_runs SET source = ? WHERE id = ?",
                        ("manual", run_id),
                    )
                    updated_manual += 1
                    continue
                # Use the server's local time for comparison: scheduler uses
                # local time for triggers. astimezone() returns local TZ when
                # passed no argument (Python 3.6+).
                if dt_utc.tzinfo is None:
                    dt_utc = dt_utc.replace(tzinfo=UTC)
                dt_local = dt_utc.astimezone()
                weekday = dt_local.weekday()  # 0=Mon..6=Sun (matches programs.days)
                day_of_month = dt_local.day
                run_seconds = dt_local.hour * 3600 + dt_local.minute * 60 + dt_local.second

                matched = False
                for prog in programs:
                    if int(zone_id) not in prog["zones"]:
                        continue
                    # Day-of-schedule check.
                    if prog["schedule_type"] == "weekdays":
                        if weekday not in prog["days"]:
                            continue
                    elif prog["schedule_type"] == "even-odd":
                        is_even = day_of_month % 2 == 0
                        if prog["even_odd"] == "even" and not is_even:
                            continue
                        if prog["even_odd"] == "odd" and is_even:
                            continue
                    # 'interval' — no reliable anchor for the past, treat as
                    # matching by time only (best-effort).
                    for t_str in prog["times"]:
                        try:
                            hh, mm = t_str.split(":")[:2]
                            prog_sec = int(hh) * 3600 + int(mm) * 60
                        except (ValueError, AttributeError):
                            continue
                        if abs(run_seconds - prog_sec) <= 120:
                            matched = True
                            break
                    if matched:
                        break
                src = "program" if matched else "manual"
                conn.execute(
                    "UPDATE zone_runs SET source = ? WHERE id = ?",
                    (src, run_id),
                )
                if matched:
                    updated_program += 1
                else:
                    updated_manual += 1
            conn.commit()
            logger.info(
                "zone_runs_backfill_source: marked %d program, %d manual",
                updated_program,
                updated_manual,
            )
        except sqlite3.Error as e:
            logger.error("Ошибка миграции zone_runs_backfill_source: %s", e)

    def _migrate_create_audit_log(self, conn):
        """Create the audit_log table for principal-critical mutation tracking.

        Separate from the existing ``logs`` table, which keeps low-fidelity
        operational events.  ``audit_log`` stores who/what/when/how for every
        mutating UI/API action.  Idempotent (IF NOT EXISTS guards everywhere).
        """
        try:
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
        except sqlite3.Error as e:
            logger.error("Ошибка миграции create_audit_log: %s", e)

    def _migrate_programs_v2_fields(self, conn):
        """Add v2 fields to programs table: type, schedule_type, interval_days, even_odd, color, enabled, extra_times."""
        try:
            cursor = conn.execute("PRAGMA table_info(programs)")
            columns = [column[1] for column in cursor.fetchall()]

            migrations = [
                ("type", "ALTER TABLE programs ADD COLUMN type TEXT DEFAULT 'time-based'"),
                ("schedule_type", "ALTER TABLE programs ADD COLUMN schedule_type TEXT DEFAULT 'weekdays'"),
                ("interval_days", "ALTER TABLE programs ADD COLUMN interval_days INTEGER DEFAULT NULL"),
                ("even_odd", "ALTER TABLE programs ADD COLUMN even_odd TEXT DEFAULT NULL"),
                ("color", "ALTER TABLE programs ADD COLUMN color TEXT DEFAULT '#42a5f5'"),
                ("enabled", "ALTER TABLE programs ADD COLUMN enabled INTEGER DEFAULT 1"),
                ("extra_times", "ALTER TABLE programs ADD COLUMN extra_times TEXT DEFAULT '[]'"),
            ]

            for col_name, ddl in migrations:
                if col_name not in columns:
                    conn.execute(ddl)
                    logger.info(f"Добавлено поле {col_name} в таблицу programs")

            conn.commit()
            logger.info("Миграция programs v2 fields завершена")
        except sqlite3.Error as e:
            logger.error("Ошибка миграции programs v2 fields: %s", e)

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

    # --- Issue #52: in-app auth migrations ---

    def _migrate_create_users(self, conn):
        """Create the users table per issue #52 schema."""
        try:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS users (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    username TEXT NOT NULL UNIQUE,
                    password_hash TEXT NOT NULL,
                    role TEXT NOT NULL CHECK(role IN ('viewer', 'admin')),
                    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    last_login_at TIMESTAMP,
                    is_active INTEGER NOT NULL DEFAULT 1
                )
                """
            )
            conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_users_username ON users(username)")
            conn.commit()
            logger.info("Создана таблица users")
        except sqlite3.Error as e:
            logger.error("Ошибка миграции create_users: %s", e)

    def _migrate_seed_default_users(self, conn):
        """Seed default credentials: admin/1234 (admin), Poliv/Poliv (viewer).

        B13: both INSERTs run in a single transaction (``with conn:``) so a
        failure on the second row rolls back the first — never leave the DB
        with admin but no viewer (or vice versa).

        If the legacy ``settings.password_hash`` row exists (created by older
        first-start init), reuse it as the admin's hash to preserve Raul's
        currently-set password instead of resetting to "1234".
        """
        from werkzeug.security import generate_password_hash

        # Skip if any users already exist (idempotency across re-runs).
        try:
            existing = conn.execute("SELECT COUNT(*) FROM users").fetchone()[0]
            if existing:
                logger.info("seed_default_users: пропуск — таблица users непуста (%d)", existing)
                return
        except sqlite3.Error as e:
            logger.error("seed_default_users: count failed: %s", e)
            return

        # Preserve admin's existing pw hash if a legacy settings.password_hash exists.
        admin_hash: str | None = None
        try:
            row = conn.execute(
                "SELECT value FROM settings WHERE key = ? LIMIT 1", ("password_hash",)
            ).fetchone()
            if row and row[0]:
                admin_hash = str(row[0])
        except sqlite3.Error as e:
            logger.debug("seed_default_users: legacy hash lookup: %s", e)
        if not admin_hash:
            admin_hash = generate_password_hash("1234", method="pbkdf2:sha256")
        poliv_hash = generate_password_hash("Poliv", method="pbkdf2:sha256")

        # B13: атомарная транзакция — если второй INSERT упадёт, первый откатится.
        with conn:
            conn.execute(
                "INSERT INTO users(username, password_hash, role, is_active) VALUES (?, ?, ?, 1)",
                ("admin", admin_hash, "admin"),
            )
            conn.execute(
                "INSERT INTO users(username, password_hash, role, is_active) VALUES (?, ?, ?, 1)",
                ("Poliv", poliv_hash, "viewer"),
            )
        logger.info("seed_default_users: вставлены admin (admin) и Poliv (viewer)")
