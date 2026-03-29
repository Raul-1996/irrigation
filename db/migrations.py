import sqlite3
import json
import logging
from typing import Optional

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
                    conn.execute('PRAGMA journal_mode=WAL')
                    conn.execute('PRAGMA foreign_keys=ON')
                    conn.execute('PRAGMA synchronous=NORMAL')
                    conn.execute('PRAGMA wal_autocheckpoint=1000')
                    conn.execute('PRAGMA cache_size=-4000')
                    conn.execute('PRAGMA temp_store=MEMORY')
                except sqlite3.Error as e:
                    logger.warning("PRAGMA setup warning: %s", e)

                # Create tables
                conn.execute('''
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
                ''')
                conn.execute('''
                    CREATE TABLE IF NOT EXISTS migrations (
                        name TEXT PRIMARY KEY,
                        applied_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                    )
                ''')
                conn.execute('''
                    CREATE TABLE IF NOT EXISTS settings (
                        key TEXT PRIMARY KEY,
                        value TEXT
                    )
                ''')
                conn.execute('''
                    CREATE TABLE IF NOT EXISTS groups (
                        id INTEGER PRIMARY KEY,
                        name TEXT NOT NULL UNIQUE,
                        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                        updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                    )
                ''')
                conn.execute('''
                    CREATE TABLE IF NOT EXISTS programs (
                        id INTEGER PRIMARY KEY,
                        name TEXT NOT NULL,
                        time TEXT NOT NULL,
                        days TEXT NOT NULL,
                        zones TEXT NOT NULL,
                        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                        updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                    )
                ''')
                conn.execute('''
                    CREATE TABLE IF NOT EXISTS logs (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        type TEXT NOT NULL,
                        details TEXT,
                        timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                    )
                ''')
                conn.execute('''
                    CREATE TABLE IF NOT EXISTS water_usage (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        zone_id INTEGER,
                        liters REAL,
                        timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                    )
                ''')
                conn.execute('''
                    CREATE TABLE IF NOT EXISTS program_cancellations (
                        program_id INTEGER NOT NULL,
                        run_date TEXT NOT NULL,
                        group_id INTEGER,
                        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                        PRIMARY KEY (program_id, run_date, group_id)
                    )
                ''')

                # Create indexes
                conn.execute('CREATE INDEX IF NOT EXISTS idx_zones_group ON zones(group_id)')
                conn.execute('CREATE INDEX IF NOT EXISTS idx_logs_type ON logs(type)')
                conn.execute('CREATE INDEX IF NOT EXISTS idx_logs_timestamp ON logs(timestamp)')
                conn.execute('CREATE INDEX IF NOT EXISTS idx_water_zone ON water_usage(zone_id)')
                conn.execute('CREATE INDEX IF NOT EXISTS idx_water_timestamp ON water_usage(timestamp)')

                conn.commit()

                # Initial data
                self._insert_initial_data(conn)

                # Named migrations
                self._apply_named_migration(conn, 'days_format', self._migrate_days_format)
                self._apply_named_migration(conn, 'zones_add_postpone_reason', self._migrate_add_postpone_reason)
                self._apply_named_migration(conn, 'zones_add_watering_start_time', self._migrate_add_watering_start_time)
                self._apply_named_migration(conn, 'zones_add_scheduled_start_time', self._migrate_add_scheduled_start_time)
                self._apply_named_migration(conn, 'zones_add_last_watering_time', self._migrate_add_last_watering_time)
                self._apply_named_migration(conn, 'create_mqtt_servers', self._migrate_add_mqtt_servers)
                self._apply_named_migration(conn, 'zones_add_mqtt_server_id', self._migrate_add_zone_mqtt_server_id)
                self._apply_named_migration(conn, 'ensure_group_999', self._migrate_ensure_special_group)
                self._apply_named_migration(conn, 'zones_add_indexes', self._migrate_add_zones_indexes)
                self._apply_named_migration(conn, 'groups_add_use_rain', self._migrate_add_group_rain_flag)
                self._apply_named_migration(conn, 'zones_add_watering_start_source', self._migrate_add_watering_start_source)
                self._apply_named_migration(conn, 'mqtt_add_tls_options', self._migrate_add_mqtt_tls_options)
                self._apply_named_migration(conn, 'zones_add_control_fields', self._migrate_add_zone_control_fields)
                self._apply_named_migration(conn, 'zones_add_commanded_observed', self._migrate_add_commanded_observed)
                self._apply_named_migration(conn, 'groups_add_master_and_sensors', self._migrate_add_groups_master_and_sensors)
                self._apply_named_migration(conn, 'groups_add_master_valve_observed', self._migrate_add_groups_master_valve_observed)
                self._apply_named_migration(conn, 'groups_add_water_meter_extended', self._migrate_add_groups_water_meter_extended)
                self._apply_named_migration(conn, 'zones_add_water_stats', self._migrate_add_zones_water_stats)
                self._apply_named_migration(conn, 'create_zone_runs_v1', self._migrate_create_zone_runs)
                # Telegram bot migrations
                self._apply_named_migration(conn, 'telegram_add_settings_fields', self._migrate_add_telegram_settings)
                self._apply_named_migration(conn, 'telegram_create_bot_users', self._migrate_create_bot_users)
                self._apply_named_migration(conn, 'telegram_create_bot_subscriptions', self._migrate_create_bot_subscriptions)
                self._apply_named_migration(conn, 'telegram_create_bot_audit', self._migrate_create_bot_audit)
                self._apply_named_migration(conn, 'telegram_add_fsm_and_notif', self._migrate_add_fsm_and_notif)
                self._apply_named_migration(conn, 'telegram_create_bot_idempotency', self._migrate_create_bot_idempotency)
                # Security: encrypt plaintext MQTT passwords
                self._apply_named_migration(conn, 'encrypt_mqtt_passwords', self._migrate_encrypt_mqtt_passwords)
                # Safety: fault tracking
                self._apply_named_migration(conn, 'zones_add_fault_tracking', self._migrate_add_fault_tracking)
                # Weather: tables and settings
                self._apply_named_migration(conn, 'weather_create_cache', self._migrate_create_weather_cache)
                self._apply_named_migration(conn, 'weather_create_log', self._migrate_create_weather_log)
                self._apply_named_migration(conn, 'weather_add_settings', self._migrate_add_weather_settings)

                logger.info("База данных инициализирована успешно")

        except (ConnectionError, TimeoutError, OSError) as e:
            logger.error("Ошибка инициализации базы данных: %s", e)
            raise

    def _insert_initial_data(self, conn):
        """Вставить начальные данные."""
        try:
            cursor = conn.execute('SELECT COUNT(*) FROM zones')
            if cursor.fetchone()[0] > 0:
                cur = conn.execute('SELECT value FROM settings WHERE key = ? LIMIT 1', ('password_hash',))
                if cur.fetchone() is None:
                    conn.execute('INSERT OR REPLACE INTO settings(key, value) VALUES (?, ?)', (
                        'password_hash', generate_password_hash('1234', method='pbkdf2:sha256')
                    ))
                    conn.commit()
                return

            groups = [
                (1, 'Насос-1'),
                (999, 'БЕЗ ПОЛИВА')
            ]
            for group_id, name in groups:
                conn.execute('INSERT OR IGNORE INTO groups (id, name) VALUES (?, ?)', (group_id, name))
            conn.commit()
            conn.execute('INSERT OR REPLACE INTO settings(key, value) VALUES (?, ?)', (
                'password_hash', generate_password_hash('1234', method='pbkdf2:sha256:120000')
            ))
            conn.commit()
            logger.info("Начальные данные вставлены: группы 1 (Насос-1) и 999 (БЕЗ ПОЛИВА)")
        except sqlite3.Error as e:
            logger.error("Ошибка вставки начальных данных: %s", e)

    def _apply_named_migration(self, conn, name: str, func):
        try:
            cur = conn.execute('SELECT name FROM migrations WHERE name = ? LIMIT 1', (name,))
            row = cur.fetchone()
            if row:
                return
            func(conn)
            conn.execute('INSERT OR REPLACE INTO migrations(name) VALUES (?)', (name,))
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
                conn.execute('PRAGMA foreign_keys=OFF')
                cur = conn.execute('SELECT name FROM migrations WHERE name = ? LIMIT 1', (name,))
                if cur.fetchone() is None:
                    logger.warning("Миграция %s не была применена, пропуск rollback", name)
                    return False
                down_func(conn)
                conn.execute('DELETE FROM migrations WHERE name = ?', (name,))
                conn.execute('PRAGMA foreign_keys=ON')
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
        cur = conn.execute("PRAGMA table_info(%s)" % table)
        columns_info = cur.fetchall()
        # columns_info: (cid, name, type, notnull, dflt_value, pk)
        keep = [c for c in columns_info if c[1] not in drop_columns]
        if not keep:
            logger.error("_recreate_table_without_columns: cannot drop ALL columns from %s", table)
            return
        keep_names = [c[1] for c in keep]
        col_defs = []
        for c in keep:
            cid, name, ctype, notnull, dflt, pk = c
            parts = [name, ctype or 'TEXT']
            if pk:
                parts.append('PRIMARY KEY')
                # Check if the PK column is AUTOINCREMENT
                # We need to check the original CREATE TABLE SQL
                try:
                    schema_cur = conn.execute(
                        "SELECT sql FROM sqlite_master WHERE type='table' AND name=?", (table,)
                    )
                    schema_row = schema_cur.fetchone()
                    if schema_row and schema_row[0] and 'AUTOINCREMENT' in schema_row[0].upper() and name.lower() == 'id':
                        parts.append('AUTOINCREMENT')
                except sqlite3.Error:
                    pass
            if notnull and not pk:
                parts.append('NOT NULL')
            if dflt is not None and not pk:
                parts.append('DEFAULT %s' % dflt)
            col_defs.append(' '.join(parts))
        cols_csv = ', '.join(keep_names)
        defs_csv = ', '.join(col_defs)
        tmp = table + '__down_tmp'
        conn.execute('DROP TABLE IF EXISTS %s' % tmp)
        conn.execute('CREATE TABLE %s (%s)' % (tmp, defs_csv))
        conn.execute('INSERT INTO %s (%s) SELECT %s FROM %s' % (tmp, cols_csv, cols_csv, table))
        conn.execute('DROP TABLE %s' % table)
        conn.execute('ALTER TABLE %s RENAME TO %s' % (tmp, table))
        conn.commit()

    # --- All migration methods ---

    def _migrate_days_format(self, conn):
        try:
            cursor = conn.execute('SELECT id, days FROM programs')
            rows = cursor.fetchall()
            for pid, days_json in rows:
                try:
                    days = json.loads(days_json)
                    if isinstance(days, list) and days:
                        if any(d < 0 or d > 6 for d in days):
                            migrated = []
                            for d in days:
                                try:
                                    nd = int(d) - 1
                                except (TypeError, ValueError) as e:
                                    logger.debug("migration day parse skip: %s", e)
                                    continue
                                if nd < 0: nd = 0
                                if nd > 6: nd = 6
                                migrated.append(nd)
                            conn.execute('UPDATE programs SET days = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?',
                                         (json.dumps(sorted(set(migrated))), pid))
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
            if 'postpone_reason' not in columns:
                conn.execute('ALTER TABLE zones ADD COLUMN postpone_reason TEXT')
                conn.commit()
                logger.info("Добавлено поле postpone_reason в таблицу zones")
        except sqlite3.Error as e:
            logger.error("Ошибка миграции postpone_reason: %s", e)

    def _migrate_add_watering_start_time(self, conn):
        try:
            cursor = conn.execute("PRAGMA table_info(zones)")
            columns = [column[1] for column in cursor.fetchall()]
            if 'watering_start_time' not in columns:
                conn.execute('ALTER TABLE zones ADD COLUMN watering_start_time TEXT')
                conn.commit()
                logger.info("Добавлено поле watering_start_time в таблицу zones")
        except sqlite3.Error as e:
            logger.error("Ошибка миграции watering_start_time: %s", e)

    def _migrate_add_scheduled_start_time(self, conn):
        try:
            cursor = conn.execute("PRAGMA table_info(zones)")
            columns = [column[1] for column in cursor.fetchall()]
            if 'scheduled_start_time' not in columns:
                conn.execute('ALTER TABLE zones ADD COLUMN scheduled_start_time TEXT')
                conn.commit()
                logger.info("Добавлено поле scheduled_start_time в таблицу zones")
        except sqlite3.Error as e:
            logger.error("Ошибка миграции scheduled_start_time: %s", e)

    def _migrate_add_last_watering_time(self, conn):
        try:
            cursor = conn.execute("PRAGMA table_info(zones)")
            columns = [column[1] for column in cursor.fetchall()]
            if 'last_watering_time' not in columns:
                conn.execute('ALTER TABLE zones ADD COLUMN last_watering_time TEXT')
                conn.commit()
                logger.info("Добавлено поле last_watering_time в таблицу zones")
        except sqlite3.Error as e:
            logger.error("Ошибка миграции last_watering_time: %s", e)

    def _migrate_add_watering_start_source(self, conn):
        try:
            cursor = conn.execute("PRAGMA table_info(zones)")
            columns = [column[1] for column in cursor.fetchall()]
            if 'watering_start_source' not in columns:
                conn.execute('ALTER TABLE zones ADD COLUMN watering_start_source TEXT')
                conn.commit()
                logger.info("Добавлено поле watering_start_source в таблицу zones")
        except sqlite3.Error as e:
            logger.error("Ошибка миграции watering_start_source: %s", e)

    def _migrate_add_group_rain_flag(self, conn):
        try:
            cursor = conn.execute("PRAGMA table_info(groups)")
            columns = [column[1] for column in cursor.fetchall()]
            if 'use_rain_sensor' not in columns:
                conn.execute('ALTER TABLE groups ADD COLUMN use_rain_sensor INTEGER DEFAULT 0')
                conn.commit()
                logger.info("Добавлено поле use_rain_sensor в таблицу groups")
        except sqlite3.Error as e:
            logger.error("Ошибка миграции use_rain_sensor: %s", e)

    def _migrate_add_mqtt_servers(self, conn):
        try:
            conn.execute('''
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
            ''')
            conn.commit()
        except sqlite3.Error as e:
            logger.error("Ошибка миграции mqtt_servers: %s", e)

    def _migrate_add_zone_mqtt_server_id(self, conn):
        try:
            cursor = conn.execute("PRAGMA table_info(zones)")
            columns = [column[1] for column in cursor.fetchall()]
            if 'mqtt_server_id' not in columns:
                conn.execute('ALTER TABLE zones ADD COLUMN mqtt_server_id INTEGER')
                conn.commit()
                logger.info("Добавлено поле mqtt_server_id в таблицу zones")
        except sqlite3.Error as e:
            logger.error("Ошибка миграции mqtt_server_id: %s", e)

    def _migrate_ensure_special_group(self, conn):
        try:
            cur = conn.execute('SELECT COUNT(*) FROM groups WHERE id = 999')
            cnt = cur.fetchone()[0] if cur else 0
            if cnt == 0:
                conn.execute("INSERT OR IGNORE INTO groups (id, name) VALUES (999, 'БЕЗ ПОЛИВА')")
                conn.commit()
                logger.info("Добавлена служебная группа 999 'БЕЗ ПОЛИВА'")
        except sqlite3.Error as e:
            logger.error("Ошибка миграции ensure_special_group: %s", e)

    def _migrate_add_zones_indexes(self, conn):
        try:
            conn.execute('CREATE INDEX IF NOT EXISTS idx_zones_mqtt_server ON zones(mqtt_server_id)')
            conn.execute('CREATE INDEX IF NOT EXISTS idx_zones_topic ON zones(topic)')
            conn.commit()
        except sqlite3.Error as e:
            logger.error("Ошибка миграции индексов zones: %s", e)

    def _migrate_add_mqtt_tls_options(self, conn):
        try:
            cursor = conn.execute("PRAGMA table_info(mqtt_servers)")
            columns = [column[1] for column in cursor.fetchall()]
            if 'tls_enabled' not in columns:
                conn.execute('ALTER TABLE mqtt_servers ADD COLUMN tls_enabled INTEGER DEFAULT 0')
            if 'tls_ca_path' not in columns:
                conn.execute('ALTER TABLE mqtt_servers ADD COLUMN tls_ca_path TEXT')
            if 'tls_cert_path' not in columns:
                conn.execute('ALTER TABLE mqtt_servers ADD COLUMN tls_cert_path TEXT')
            if 'tls_key_path' not in columns:
                conn.execute('ALTER TABLE mqtt_servers ADD COLUMN tls_key_path TEXT')
            if 'tls_insecure' not in columns:
                conn.execute('ALTER TABLE mqtt_servers ADD COLUMN tls_insecure INTEGER DEFAULT 0')
            if 'tls_version' not in columns:
                conn.execute('ALTER TABLE mqtt_servers ADD COLUMN tls_version TEXT')
            conn.commit()
        except sqlite3.Error as e:
            logger.error("Ошибка миграции mqtt_tls_options: %s", e)

    def _migrate_add_zone_control_fields(self, conn):
        try:
            cursor = conn.execute("PRAGMA table_info(zones)")
            columns = [column[1] for column in cursor.fetchall()]
            if 'planned_end_time' not in columns:
                conn.execute('ALTER TABLE zones ADD COLUMN planned_end_time TEXT')
            if 'sequence_id' not in columns:
                conn.execute('ALTER TABLE zones ADD COLUMN sequence_id TEXT')
            if 'command_id' not in columns:
                conn.execute('ALTER TABLE zones ADD COLUMN command_id TEXT')
            if 'version' not in columns:
                conn.execute('ALTER TABLE zones ADD COLUMN version INTEGER DEFAULT 0')
            conn.commit()
            logger.info('Добавлены поля planned_end_time, sequence_id, command_id, version в zones')
        except sqlite3.Error as e:
            logger.error("Ошибка миграции zone_control_fields: %s", e)

    def _migrate_add_commanded_observed(self, conn):
        try:
            cursor = conn.execute("PRAGMA table_info(zones)")
            columns = [column[1] for column in cursor.fetchall()]
            if 'commanded_state' not in columns:
                conn.execute("ALTER TABLE zones ADD COLUMN commanded_state TEXT")
            if 'observed_state' not in columns:
                conn.execute("ALTER TABLE zones ADD COLUMN observed_state TEXT")
            conn.commit()
            logger.info('Добавлены поля commanded_state, observed_state в zones')
        except sqlite3.Error as e:
            logger.error("Ошибка миграции commanded/observed: %s", e)

    def _migrate_add_groups_master_and_sensors(self, conn):
        try:
            cursor = conn.execute("PRAGMA table_info(groups)")
            columns = [column[1] for column in cursor.fetchall()]

            def add(col, ddl):
                if col not in columns:
                    conn.execute(ddl)

            add('use_master_valve', 'ALTER TABLE groups ADD COLUMN use_master_valve INTEGER DEFAULT 0')
            add('master_mqtt_topic', 'ALTER TABLE groups ADD COLUMN master_mqtt_topic TEXT DEFAULT ""')
            add('master_mode', 'ALTER TABLE groups ADD COLUMN master_mode TEXT DEFAULT "NC"')
            add('master_mqtt_server_id', 'ALTER TABLE groups ADD COLUMN master_mqtt_server_id INTEGER')
            add('master_valve_observed', 'ALTER TABLE groups ADD COLUMN master_valve_observed TEXT')
            add('use_pressure_sensor', 'ALTER TABLE groups ADD COLUMN use_pressure_sensor INTEGER DEFAULT 0')
            add('pressure_mqtt_topic', 'ALTER TABLE groups ADD COLUMN pressure_mqtt_topic TEXT DEFAULT ""')
            add('pressure_unit', 'ALTER TABLE groups ADD COLUMN pressure_unit TEXT DEFAULT "bar"')
            add('pressure_mqtt_server_id', 'ALTER TABLE groups ADD COLUMN pressure_mqtt_server_id INTEGER')
            add('use_water_meter', 'ALTER TABLE groups ADD COLUMN use_water_meter INTEGER DEFAULT 0')
            add('water_mqtt_topic', 'ALTER TABLE groups ADD COLUMN water_mqtt_topic TEXT DEFAULT ""')
            add('water_mqtt_server_id', 'ALTER TABLE groups ADD COLUMN water_mqtt_server_id INTEGER')
            add('water_pulse_size', 'ALTER TABLE groups ADD COLUMN water_pulse_size TEXT DEFAULT "1l"')
            add('water_base_value_m3', 'ALTER TABLE groups ADD COLUMN water_base_value_m3 REAL DEFAULT 0')
            add('water_base_pulses', 'ALTER TABLE groups ADD COLUMN water_base_pulses INTEGER DEFAULT 0')
            conn.commit()
            logger.info('Добавлены поля мастер-клапана и сенсоров в таблицу groups')
        except sqlite3.Error as e:
            logger.error("Ошибка миграции groups_add_master_and_sensors: %s", e)

    def _migrate_add_groups_master_valve_observed(self, conn):
        try:
            cursor = conn.execute("PRAGMA table_info(groups)")
            cols = [r[1] for r in cursor.fetchall()]
            if 'master_valve_observed' not in cols:
                conn.execute('ALTER TABLE groups ADD COLUMN master_valve_observed TEXT')
                conn.commit()
                logger.info('Добавлено поле master_valve_observed в groups')
        except sqlite3.Error as e:
            logger.error("Ошибка миграции groups_add_master_valve_observed: %s", e)

    def _migrate_add_groups_water_meter_extended(self, conn):
        try:
            cursor = conn.execute("PRAGMA table_info(groups)")
            cols = [r[1] for r in cursor.fetchall()]
            if 'water_pulse_size' not in cols:
                conn.execute('ALTER TABLE groups ADD COLUMN water_pulse_size TEXT DEFAULT "1l"')
            if 'water_base_value_m3' not in cols:
                conn.execute('ALTER TABLE groups ADD COLUMN water_base_value_m3 REAL DEFAULT 0')
            if 'water_base_pulses' not in cols:
                conn.execute('ALTER TABLE groups ADD COLUMN water_base_pulses INTEGER DEFAULT 0')
            conn.commit()
            logger.info('Добавлены поля water_pulse_size, water_base_value_m3, water_base_pulses в groups')
        except sqlite3.Error as e:
            logger.error("Ошибка миграции groups_add_water_meter_extended: %s", e)

    def _migrate_add_zones_water_stats(self, conn):
        try:
            cursor = conn.execute("PRAGMA table_info(zones)")
            cols = [r[1] for r in cursor.fetchall()]
            if 'last_avg_flow_lpm' not in cols:
                conn.execute('ALTER TABLE zones ADD COLUMN last_avg_flow_lpm REAL')
            if 'last_total_liters' not in cols:
                conn.execute('ALTER TABLE zones ADD COLUMN last_total_liters REAL')
            conn.commit()
            logger.info('Добавлены поля last_avg_flow_lpm, last_total_liters в zones')
        except sqlite3.Error as e:
            logger.error("Ошибка миграции zones_add_water_stats: %s", e)

    def _migrate_create_zone_runs(self, conn):
        try:
            conn.execute('''
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
            ''')
            conn.execute('CREATE INDEX IF NOT EXISTS idx_zone_runs_zone ON zone_runs(zone_id)')
            conn.execute('CREATE INDEX IF NOT EXISTS idx_zone_runs_group ON zone_runs(group_id)')
            conn.execute('CREATE INDEX IF NOT EXISTS idx_zone_runs_active ON zone_runs(zone_id, end_utc)')
            conn.commit()
            logger.info('Создана таблица zone_runs')
        except sqlite3.Error as e:
            logger.error("Ошибка миграции create_zone_runs_v1: %s", e)

    def _migrate_add_telegram_settings(self, conn):
        try:
            keys = [
                'telegram_bot_token_encrypted',
                'telegram_access_password_hash',
                'telegram_webhook_secret_path',
                'telegram_admin_chat_id',
            ]
            for k in keys:
                cur = conn.execute('SELECT 1 FROM settings WHERE key=?', (k,))
                if cur.fetchone() is None:
                    conn.execute('INSERT OR REPLACE INTO settings(key, value) VALUES(?, ?)', (k, None))
            conn.commit()
            logger.info('Добавлены ключи настроек телеграм-бота в settings')
        except sqlite3.Error as e:
            logger.error("Ошибка миграции telegram_add_settings_fields: %s", e)

    def _migrate_create_bot_users(self, conn):
        try:
            conn.execute('''
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
            ''')
            conn.execute('CREATE INDEX IF NOT EXISTS idx_bot_users_chat ON bot_users(chat_id)')
            conn.commit()
            logger.info('Создана таблица bot_users')
        except sqlite3.Error as e:
            logger.error("Ошибка миграции telegram_create_bot_users: %s", e)

    def _migrate_create_bot_subscriptions(self, conn):
        try:
            conn.execute('''
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
            ''')
            conn.execute('CREATE INDEX IF NOT EXISTS idx_bot_subs_user ON bot_subscriptions(user_id)')
            conn.commit()
            logger.info('Создана таблица bot_subscriptions')
        except sqlite3.Error as e:
            logger.error("Ошибка миграции telegram_create_bot_subscriptions: %s", e)

    def _migrate_create_bot_audit(self, conn):
        try:
            conn.execute('''
                CREATE TABLE IF NOT EXISTS bot_audit (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER,
                    action TEXT,
                    payload_json TEXT,
                    ts TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    FOREIGN KEY(user_id) REFERENCES bot_users(id) ON DELETE SET NULL
                )
            ''')
            conn.execute('CREATE INDEX IF NOT EXISTS idx_bot_audit_user ON bot_audit(user_id)')
            conn.commit()
            logger.info('Создана таблица bot_audit')
        except sqlite3.Error as e:
            logger.error("Ошибка миграции telegram_create_bot_audit: %s", e)

    def _migrate_add_fsm_and_notif(self, conn):
        try:
            cols = {
                'fsm_state': "ALTER TABLE bot_users ADD COLUMN fsm_state TEXT",
                'fsm_data': "ALTER TABLE bot_users ADD COLUMN fsm_data TEXT",
                'notif_critical': "ALTER TABLE bot_users ADD COLUMN notif_critical INTEGER DEFAULT 1",
                'notif_emergency': "ALTER TABLE bot_users ADD COLUMN notif_emergency INTEGER DEFAULT 1",
                'notif_postpone': "ALTER TABLE bot_users ADD COLUMN notif_postpone INTEGER DEFAULT 1",
                'notif_zone_events': "ALTER TABLE bot_users ADD COLUMN notif_zone_events INTEGER DEFAULT 0",
                'notif_rain': "ALTER TABLE bot_users ADD COLUMN notif_rain INTEGER DEFAULT 0",
            }
            cur = conn.execute('PRAGMA table_info(bot_users)')
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
            conn.execute('''
                CREATE TABLE IF NOT EXISTS bot_idempotency (
                    token TEXT PRIMARY KEY,
                    chat_id INTEGER,
                    action TEXT,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            ''')
            conn.execute('CREATE INDEX IF NOT EXISTS idx_bot_idemp_chat ON bot_idempotency(chat_id)')
            conn.commit()
            logger.info('Создана таблица bot_idempotency')
        except sqlite3.Error as e:
            logger.error("Ошибка миграции telegram_create_bot_idempotency: %s", e)

    def _migrate_encrypt_mqtt_passwords(self, conn):
        try:
            cur = conn.execute("SELECT id, password FROM mqtt_servers WHERE password IS NOT NULL AND password != ''")
            rows = cur.fetchall()
            count = 0
            for row_id, pwd in rows:
                if pwd and not pwd.startswith('ENC:'):
                    enc = encrypt_secret(pwd)
                    if enc:
                        conn.execute('UPDATE mqtt_servers SET password = ? WHERE id = ?', ('ENC:' + enc, row_id))
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
            if 'last_fault' not in columns:
                conn.execute("ALTER TABLE zones ADD COLUMN last_fault TEXT")
            if 'fault_count' not in columns:
                conn.execute("ALTER TABLE zones ADD COLUMN fault_count INTEGER DEFAULT 0")
            conn.commit()
            logger.info("Добавлены поля last_fault, fault_count в zones")
        except sqlite3.Error as e:
            logger.error("Ошибка миграции zones_add_fault_tracking: %s", e)

    def _migrate_create_weather_cache(self, conn):
        try:
            conn.execute('''
                CREATE TABLE IF NOT EXISTS weather_cache (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    latitude REAL NOT NULL,
                    longitude REAL NOT NULL,
                    data TEXT NOT NULL,
                    fetched_at REAL NOT NULL
                )
            ''')
            conn.execute('CREATE INDEX IF NOT EXISTS idx_weather_cache_loc ON weather_cache(latitude, longitude)')
            conn.execute('CREATE INDEX IF NOT EXISTS idx_weather_cache_time ON weather_cache(fetched_at)')
            conn.commit()
            logger.info('Создана таблица weather_cache')
        except sqlite3.Error as e:
            logger.error("Ошибка миграции weather_create_cache: %s", e)

    def _migrate_create_weather_log(self, conn):
        try:
            conn.execute('''
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
            ''')
            conn.execute('CREATE INDEX IF NOT EXISTS idx_weather_log_zone ON weather_log(zone_id)')
            conn.execute('CREATE INDEX IF NOT EXISTS idx_weather_log_time ON weather_log(created_at)')
            conn.commit()
            logger.info('Создана таблица weather_log')
        except sqlite3.Error as e:
            logger.error("Ошибка миграции weather_create_log: %s", e)

    def _migrate_add_weather_settings(self, conn):
        try:
            weather_keys = {
                'weather.enabled': '0',
                'weather.latitude': None,
                'weather.longitude': None,
                'weather.rain_threshold_mm': '5.0',
                'weather.freeze_threshold_c': '2.0',
                'weather.wind_threshold_kmh': '25.0',
            }
            for key, default_val in weather_keys.items():
                cur = conn.execute('SELECT 1 FROM settings WHERE key = ?', (key,))
                if cur.fetchone() is None:
                    conn.execute('INSERT INTO settings(key, value) VALUES(?, ?)', (key, default_val))
            conn.commit()
            logger.info('Добавлены настройки погоды в settings')
        except sqlite3.Error as e:
            logger.error("Ошибка миграции weather_add_settings: %s", e)

    # =====================================================================
    # Downgrade methods for the last 10 migrations
    # =====================================================================

    # Registry: migration_name -> method_name (resolved at runtime via getattr)
    DOWNGRADE_REGISTRY = {
        'telegram_create_bot_users': '_down_create_bot_users',
        'telegram_create_bot_subscriptions': '_down_create_bot_subscriptions',
        'telegram_create_bot_audit': '_down_create_bot_audit',
        'telegram_add_fsm_and_notif': '_down_add_fsm_and_notif',
        'telegram_create_bot_idempotency': '_down_create_bot_idempotency',
        'encrypt_mqtt_passwords': '_down_encrypt_mqtt_passwords',
        'zones_add_fault_tracking': '_down_add_fault_tracking',
        'weather_create_cache': '_down_create_weather_cache',
        'weather_create_log': '_down_create_weather_log',
        'weather_add_settings': '_down_add_weather_settings',
    }

    def _down_create_bot_users(self, conn):
        conn.execute('DROP TABLE IF EXISTS bot_users')
        conn.commit()
        logger.info('Downgrade: удалена таблица bot_users')

    def _down_create_bot_subscriptions(self, conn):
        conn.execute('DROP TABLE IF EXISTS bot_subscriptions')
        conn.commit()
        logger.info('Downgrade: удалена таблица bot_subscriptions')

    def _down_create_bot_audit(self, conn):
        conn.execute('DROP TABLE IF EXISTS bot_audit')
        conn.commit()
        logger.info('Downgrade: удалена таблица bot_audit')

    def _down_add_fsm_and_notif(self, conn):
        drop_cols = ['fsm_state', 'fsm_data', 'notif_critical', 'notif_emergency',
                     'notif_postpone', 'notif_zone_events', 'notif_rain']
        self._recreate_table_without_columns(conn, 'bot_users', drop_cols)
        logger.info('Downgrade: удалены FSM/notif колонки из bot_users')

    def _down_create_bot_idempotency(self, conn):
        conn.execute('DROP TABLE IF EXISTS bot_idempotency')
        conn.commit()
        logger.info('Downgrade: удалена таблица bot_idempotency')

    def _down_encrypt_mqtt_passwords(self, conn):
        # Decrypting passwords is not safely reversible — mark migration as rolled back
        # but leave data as-is (encrypted passwords will fail on connect; user must re-enter)
        logger.warning('Downgrade: encrypt_mqtt_passwords — зашифрованные пароли НЕ расшифрованы. '
                        'Пользователь должен ввести пароли заново.')

    def _down_add_fault_tracking(self, conn):
        self._recreate_table_without_columns(conn, 'zones', ['last_fault', 'fault_count'])
        logger.info('Downgrade: удалены поля last_fault, fault_count из zones')

    def _down_create_weather_cache(self, conn):
        conn.execute('DROP TABLE IF EXISTS weather_cache')
        conn.commit()
        logger.info('Downgrade: удалена таблица weather_cache')

    def _down_create_weather_log(self, conn):
        conn.execute('DROP TABLE IF EXISTS weather_log')
        conn.commit()
        logger.info('Downgrade: удалена таблица weather_log')

    def _down_add_weather_settings(self, conn):
        weather_keys = [
            'weather.enabled', 'weather.latitude', 'weather.longitude',
            'weather.rain_threshold_mm', 'weather.freeze_threshold_c',
            'weather.wind_threshold_kmh',
        ]
        for key in weather_keys:
            conn.execute('DELETE FROM settings WHERE key = ?', (key,))
        conn.commit()
        logger.info('Downgrade: удалены настройки погоды из settings')
