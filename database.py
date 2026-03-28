import sqlite3
import json
import os
import shutil
from datetime import datetime, timedelta
from typing import List, Dict, Any, Optional, Tuple
import logging
from werkzeug.security import generate_password_hash, check_password_hash
from utils import encrypt_secret, decrypt_secret

# Настройка логирования
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)
try:
    fmt = logging.Formatter('%(asctime)s [%(levelname)s] [%(name)s] %(message)s', datefmt='%Y-%m-%d %H:%M:%S')
    for h in logging.getLogger().handlers:
        if isinstance(h, logging.StreamHandler):
            h.setFormatter(fmt)
except Exception:
    pass
# В тестах отключаем распространение в root, чтобы не писать в закрытый stdout из фоновых потоков
logger.propagate = False

class IrrigationDB:
    def __init__(self, db_path: str = 'irrigation.db'):
        self.db_path = db_path
        self.backup_dir = 'backups'
        self.init_database()
    
    def init_database(self):
        """Инициализация базы данных"""
        try:
            with sqlite3.connect(self.db_path, timeout=5) as conn:
                # PRAGMA
                try:
                    conn.execute('PRAGMA journal_mode=WAL')
                    conn.execute('PRAGMA foreign_keys=ON')
                    # Persistent, low-risk performance tweaks for embedded devices
                    conn.execute('PRAGMA synchronous=NORMAL')
                    conn.execute('PRAGMA wal_autocheckpoint=1000')
                    conn.execute('PRAGMA cache_size=-4000')
                    conn.execute('PRAGMA temp_store=MEMORY')
                except Exception:
                    pass
                # Создание таблиц
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
                # Таблица миграций (легковесный трекер применённых миграций)
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
                # Отмена текущего запуска программ (разовая, по дате)
                conn.execute('''
                    CREATE TABLE IF NOT EXISTS program_cancellations (
                        program_id INTEGER NOT NULL,
                        run_date TEXT NOT NULL,
                        group_id INTEGER,
                        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                        PRIMARY KEY (program_id, run_date, group_id)
                    )
                ''')
                
                # Создание индексов
                conn.execute('CREATE INDEX IF NOT EXISTS idx_zones_group ON zones(group_id)')
                conn.execute('CREATE INDEX IF NOT EXISTS idx_logs_type ON logs(type)')
                conn.execute('CREATE INDEX IF NOT EXISTS idx_logs_timestamp ON logs(timestamp)')
                conn.execute('CREATE INDEX IF NOT EXISTS idx_water_zone ON water_usage(zone_id)')
                conn.execute('CREATE INDEX IF NOT EXISTS idx_water_timestamp ON water_usage(timestamp)')
                
                conn.commit()
                
                # Вставка начальных данных
                self._insert_initial_data(conn)
                
                # Миграции
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
                # Safety: fault tracking columns for observed_state verification
                self._apply_named_migration(conn, 'zones_add_fault_tracking', self._migrate_add_fault_tracking)
                
                logger.info("База данных инициализирована успешно")
                
        except Exception as e:
            logger.error(f"Ошибка инициализации базы данных: {e}")
            raise
    
    def _insert_initial_data(self, conn):
        """Вставить начальные данные"""
        try:
                # Проверяем, есть ли уже данные
                cursor = conn.execute('SELECT COUNT(*) FROM zones')
                if cursor.fetchone()[0] > 0:
                    # Убедимся, что задан пароль по умолчанию
                    cur = conn.execute('SELECT value FROM settings WHERE key = ? LIMIT 1', ('password_hash',))
                    if cur.fetchone() is None:
                        conn.execute('INSERT OR REPLACE INTO settings(key, value) VALUES (?, ?)', (
                            'password_hash', generate_password_hash('1234', method='pbkdf2:sha256')
                        ))
                        conn.commit()
                    return  # Данные уже есть
                
                # Создаём только базовые группы: 1 — Насос-1, 999 — БЕЗ ПОЛИВА
                groups = [
                    (1, 'Насос-1'),
                    (999, 'БЕЗ ПОЛИВА')
                ]
                for group_id, name in groups:
                    conn.execute('INSERT OR IGNORE INTO groups (id, name) VALUES (?, ?)', (group_id, name))
                
                # Без предзаполнения зон/программ/логов — чистая база по умолчанию
                conn.commit()
                # Пароль по умолчанию 1234 (умеренные итерации для слабого CPU)
                conn.execute('INSERT OR REPLACE INTO settings(key, value) VALUES (?, ?)', (
                    'password_hash', generate_password_hash('1234', method='pbkdf2:sha256:120000')
                ))
                conn.commit()
                logger.info("Начальные данные вставлены: группы 1 (Насос-1) и 999 (БЕЗ ПОЛИВА)")
                
        except Exception as e:
            logger.error(f"Ошибка вставки начальных данных: {e}")

    def _apply_named_migration(self, conn, name: str, func):
        try:
            cur = conn.execute('SELECT name FROM migrations WHERE name = ? LIMIT 1', (name,))
            row = cur.fetchone()
            if row:
                return
            # Выполняем миграцию
            func(conn)
            conn.execute('INSERT OR REPLACE INTO migrations(name) VALUES (?)', (name,))
            conn.commit()
        except Exception as e:
            logger.error(f"Ошибка применения миграции {name}: {e}")

    def _migrate_days_format(self, conn):
        """Миграция формата дней программ к 0-6 (0=Пн)"""
        try:
            cursor = conn.execute('SELECT id, days FROM programs')
            rows = cursor.fetchall()
            for pid, days_json in rows:
                try:
                    days = json.loads(days_json)
                    if isinstance(days, list) and days:
                        # Если значения вне диапазона 0-6 — попробуем сместить из 1-7
                        if any(d < 0 or d > 6 for d in days):
                            migrated = []
                            for d in days:
                                try:
                                    nd = int(d) - 1
                                except Exception:
                                    continue
                                if nd < 0:
                                    nd = 0
                                if nd > 6:
                                    nd = 6
                                migrated.append(nd)
                            conn.execute('UPDATE programs SET days = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?', (json.dumps(sorted(set(migrated))), pid))
                except Exception:
                    continue
            conn.commit()
        except Exception as e:
            logger.error(f"Ошибка миграции формата дней: {e}")
    
    def _migrate_add_postpone_reason(self, conn):
        """Миграция: добавление поля postpone_reason"""
        try:
            # Проверяем, есть ли уже поле postpone_reason
            cursor = conn.execute("PRAGMA table_info(zones)")
            columns = [column[1] for column in cursor.fetchall()]
            
            if 'postpone_reason' not in columns:
                conn.execute('ALTER TABLE zones ADD COLUMN postpone_reason TEXT')
                conn.commit()
                logger.info("Добавлено поле postpone_reason в таблицу zones")
        except Exception as e:
            logger.error(f"Ошибка миграции postpone_reason: {e}")
    
    def _migrate_add_watering_start_time(self, conn):
        """Миграция: добавление поля watering_start_time"""
        try:
            # Проверяем, есть ли уже поле watering_start_time
            cursor = conn.execute("PRAGMA table_info(zones)")
            columns = [column[1] for column in cursor.fetchall()]
            
            if 'watering_start_time' not in columns:
                conn.execute('ALTER TABLE zones ADD COLUMN watering_start_time TEXT')
                conn.commit()
                logger.info("Добавлено поле watering_start_time в таблицу zones")
        except Exception as e:
            logger.error(f"Ошибка миграции watering_start_time: {e}")

    def _migrate_add_scheduled_start_time(self, conn):
        """Миграция: добавление поля scheduled_start_time (плановое время старта)"""
        try:
            cursor = conn.execute("PRAGMA table_info(zones)")
            columns = [column[1] for column in cursor.fetchall()]
            if 'scheduled_start_time' not in columns:
                conn.execute('ALTER TABLE zones ADD COLUMN scheduled_start_time TEXT')
                conn.commit()
                logger.info("Добавлено поле scheduled_start_time в таблицу zones")
        except Exception as e:
            logger.error(f"Ошибка миграции scheduled_start_time: {e}")

    def _migrate_add_last_watering_time(self, conn):
        """Миграция: добавление поля last_watering_time (время последнего полива)"""
        try:
            cursor = conn.execute("PRAGMA table_info(zones)")
            columns = [column[1] for column in cursor.fetchall()]
            if 'last_watering_time' not in columns:
                conn.execute('ALTER TABLE zones ADD COLUMN last_watering_time TEXT')
                conn.commit()
                logger.info("Добавлено поле last_watering_time в таблицу zones")
        except Exception as e:
            logger.error(f"Ошибка миграции last_watering_time: {e}")

    def _migrate_add_watering_start_source(self, conn):
        """Миграция: текстовый источник старта полива (manual|schedule)."""
        try:
            cursor = conn.execute("PRAGMA table_info(zones)")
            columns = [column[1] for column in cursor.fetchall()]
            if 'watering_start_source' not in columns:
                conn.execute('ALTER TABLE zones ADD COLUMN watering_start_source TEXT')
                conn.commit()
                logger.info("Добавлено поле watering_start_source в таблицу zones")
        except Exception as e:
            logger.error(f"Ошибка миграции watering_start_source: {e}")

    def _migrate_add_group_rain_flag(self, conn):
        """Миграция: флаг использования датчика дождя на уровне группы"""
        try:
            cursor = conn.execute("PRAGMA table_info(groups)")
            columns = [column[1] for column in cursor.fetchall()]
            if 'use_rain_sensor' not in columns:
                conn.execute('ALTER TABLE groups ADD COLUMN use_rain_sensor INTEGER DEFAULT 0')
                conn.commit()
                logger.info("Добавлено поле use_rain_sensor в таблицу groups")
        except Exception as e:
            logger.error(f"Ошибка миграции use_rain_sensor: {e}")

    def _migrate_add_mqtt_servers(self, conn):
        """Миграция: таблица MQTT серверов"""
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
        except Exception as e:
            logger.error(f"Ошибка миграции mqtt_servers: {e}")

    def _migrate_add_zone_mqtt_server_id(self, conn):
        """Миграция: поле mqtt_server_id у зон"""
        try:
            cursor = conn.execute("PRAGMA table_info(zones)")
            columns = [column[1] for column in cursor.fetchall()]
            if 'mqtt_server_id' not in columns:
                conn.execute('ALTER TABLE zones ADD COLUMN mqtt_server_id INTEGER')
                conn.commit()
                logger.info("Добавлено поле mqtt_server_id в таблицу zones")
        except Exception as e:
            logger.error(f"Ошибка миграции mqtt_server_id: {e}")

    def _migrate_ensure_special_group(self, conn):
        """Миграция: гарантировать наличие служебной группы 999 'БЕЗ ПОЛИВА'"""
        try:
            cur = conn.execute('SELECT COUNT(*) FROM groups WHERE id = 999')
            cnt = cur.fetchone()[0] if cur else 0
            if cnt == 0:
                conn.execute("INSERT OR IGNORE INTO groups (id, name) VALUES (999, 'БЕЗ ПОЛИВА')")
                conn.commit()
                logger.info("Добавлена служебная группа 999 'БЕЗ ПОЛИВА'")
        except Exception as e:
            logger.error(f"Ошибка миграции ensure_special_group: {e}")

    def _migrate_add_zones_indexes(self, conn):
        """Миграция: индексы для ускорения выборок зон по MQTT.

        Индексы безопасно создаются idempotent-но (IF NOT EXISTS).
        """
        try:
            conn.execute('CREATE INDEX IF NOT EXISTS idx_zones_mqtt_server ON zones(mqtt_server_id)')
            conn.execute('CREATE INDEX IF NOT EXISTS idx_zones_topic ON zones(topic)')
            conn.commit()
        except Exception as e:
            logger.error(f"Ошибка миграции индексов zones: {e}")

    def _migrate_add_mqtt_tls_options(self, conn):
        """Миграция: TLS-поля для mqtt_servers."""
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
        except Exception as e:
            logger.error(f"Ошибка миграции mqtt_tls_options: {e}")

    def _migrate_add_zone_control_fields(self, conn):
        """Добавить технические поля управления зоной: planned_end_time, sequence_id, command_id, version."""
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
        except Exception as e:
            logger.error(f"Ошибка миграции zone_control_fields: {e}")

    def _migrate_add_commanded_observed(self, conn):
        """Добавить commanded_state и observed_state поля для зон."""
        try:
            cursor = conn.execute("PRAGMA table_info(zones)")
            columns = [column[1] for column in cursor.fetchall()]
            if 'commanded_state' not in columns:
                conn.execute("ALTER TABLE zones ADD COLUMN commanded_state TEXT")
            if 'observed_state' not in columns:
                conn.execute("ALTER TABLE zones ADD COLUMN observed_state TEXT")
            conn.commit()
            logger.info('Добавлены поля commanded_state, observed_state в zones')
        except Exception as e:
            logger.error(f"Ошибка миграции commanded/observed: {e}")

    def _migrate_add_groups_master_and_sensors(self, conn):
        """Добавить в таблицу groups поля для мастер-клапана, датчика давления и счётчика воды."""
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
            # Water meter extended settings
            add('water_pulse_size', 'ALTER TABLE groups ADD COLUMN water_pulse_size TEXT DEFAULT "1l"')
            add('water_base_value_m3', 'ALTER TABLE groups ADD COLUMN water_base_value_m3 REAL DEFAULT 0')
            add('water_base_pulses', 'ALTER TABLE groups ADD COLUMN water_base_pulses INTEGER DEFAULT 0')
            conn.commit()
            logger.info('Добавлены поля мастер-клапана и сенсоров в таблицу groups')
        except Exception as e:
            logger.error(f"Ошибка миграции groups_add_master_and_sensors: {e}")

    def _migrate_add_groups_master_valve_observed(self, conn):
        try:
            cursor = conn.execute("PRAGMA table_info(groups)")
            cols = [r[1] for r in cursor.fetchall()]
            if 'master_valve_observed' not in cols:
                conn.execute('ALTER TABLE groups ADD COLUMN master_valve_observed TEXT')
                conn.commit()
                logger.info('Добавлено поле master_valve_observed в groups')
        except Exception as e:
            logger.error(f"Ошибка миграции groups_add_master_valve_observed: {e}")

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
        except Exception as e:
            logger.error(f"Ошибка миграции groups_add_water_meter_extended: {e}")

    def _migrate_add_zones_water_stats(self, conn):
        """Добавить в таблицу zones последние статистики воды для отображения на статусе."""
        try:
            cursor = conn.execute("PRAGMA table_info(zones)")
            cols = [r[1] for r in cursor.fetchall()]
            if 'last_avg_flow_lpm' not in cols:
                conn.execute('ALTER TABLE zones ADD COLUMN last_avg_flow_lpm REAL')
            if 'last_total_liters' not in cols:
                conn.execute('ALTER TABLE zones ADD COLUMN last_total_liters REAL')
            conn.commit()
            logger.info('Добавлены поля last_avg_flow_lpm, last_total_liters в zones')
        except Exception as e:
            logger.error(f"Ошибка миграции zones_add_water_stats: {e}")

    def _migrate_create_zone_runs(self, conn):
        """Создать таблицу zone_runs для фиксации снапшотов импульсов на старте/стопе."""
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
        except Exception as e:
            logger.error(f"Ошибка миграции create_zone_runs_v1: {e}")

    def _migrate_add_telegram_settings(self, conn):
        """Добавить ключи настроек телеграм-бота в settings (если отсутствуют)."""
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
        except Exception as e:
            logger.error(f"Ошибка миграции telegram_add_settings_fields: {e}")

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
        except Exception as e:
            logger.error(f"Ошибка миграции telegram_create_bot_users: {e}")

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
        except Exception as e:
            logger.error(f"Ошибка миграции telegram_create_bot_subscriptions: {e}")

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
        except Exception as e:
            logger.error(f"Ошибка миграции telegram_create_bot_audit: {e}")

    def _migrate_add_fsm_and_notif(self, conn):
        try:
            # Добавляем недостающие столбцы в bot_users
            cols = {
                'fsm_state': "ALTER TABLE bot_users ADD COLUMN fsm_state TEXT",
                'fsm_data': "ALTER TABLE bot_users ADD COLUMN fsm_data TEXT",
                'notif_critical': "ALTER TABLE bot_users ADD COLUMN notif_critical INTEGER DEFAULT 1",
                'notif_emergency': "ALTER TABLE bot_users ADD COLUMN notif_emergency INTEGER DEFAULT 1",
                'notif_postpone': "ALTER TABLE bot_users ADD COLUMN notif_postpone INTEGER DEFAULT 1",
                'notif_zone_events': "ALTER TABLE bot_users ADD COLUMN notif_zone_events INTEGER DEFAULT 0",
                'notif_rain': "ALTER TABLE bot_users ADD COLUMN notif_rain INTEGER DEFAULT 0",
            }
            # Определим текущие колонки
            cur = conn.execute('PRAGMA table_info(bot_users)')
            existing = {row[1] for row in cur.fetchall()}
            for name, ddl in cols.items():
                if name not in existing:
                    try:
                        conn.execute(ddl)
                    except Exception:
                        pass
            conn.commit()
        except Exception as e:
            logger.error(f"Ошибка миграции telegram_add_fsm_and_notif: {e}")

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
        except Exception as e:
            logger.error(f"Ошибка миграции telegram_create_bot_idempotency: {e}")

    def _migrate_encrypt_mqtt_passwords(self, conn):
        """Encrypt existing plaintext MQTT passwords in-place."""
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
                logger.info(f"Зашифровано {count} MQTT паролей")
            else:
                logger.info("Нет MQTT паролей для шифрования")
        except Exception as e:
            logger.error(f"Ошибка миграции encrypt_mqtt_passwords: {e}")

    def _migrate_add_fault_tracking(self, conn):
        """Add last_fault and fault_count columns to zones for observed_state verification."""
        try:
            cursor = conn.execute("PRAGMA table_info(zones)")
            columns = [col[1] for col in cursor.fetchall()]
            if 'last_fault' not in columns:
                conn.execute("ALTER TABLE zones ADD COLUMN last_fault TEXT")
            if 'fault_count' not in columns:
                conn.execute("ALTER TABLE zones ADD COLUMN fault_count INTEGER DEFAULT 0")
            conn.commit()
            logger.info("Добавлены поля last_fault, fault_count в zones")
        except Exception as e:
            logger.error(f"Ошибка миграции zones_add_fault_tracking: {e}")

    # --- Telegram bot helpers: FSM ---
    def set_bot_fsm(self, chat_id: int, state: Optional[str], data: Optional[dict]) -> bool:
        try:
            with sqlite3.connect(self.db_path, timeout=5) as conn:
                payload = None
                try:
                    payload = None if data is None else json.dumps(data, ensure_ascii=False)
                except Exception:
                    payload = None
                conn.execute(
                    'UPDATE bot_users SET fsm_state=?, fsm_data=?, last_seen_at=CURRENT_TIMESTAMP WHERE chat_id=?',
                    (None if state is None else str(state), payload, int(chat_id))
                )
                conn.commit()
                return True
        except Exception as e:
            logger.error(f"Ошибка установки FSM chat_id={chat_id}: {e}")
            return False

    def get_bot_fsm(self, chat_id: int) -> tuple[Optional[str], Optional[dict]]:
        try:
            with sqlite3.connect(self.db_path, timeout=5) as conn:
                conn.row_factory = sqlite3.Row
                cur = conn.execute('SELECT fsm_state, fsm_data FROM bot_users WHERE chat_id=?', (int(chat_id),))
                row = cur.fetchone()
                if not row:
                    return None, None
                st = row['fsm_state']
                data = None
                try:
                    data = json.loads(row['fsm_data']) if row['fsm_data'] else None
                except Exception:
                    data = None
                return st, data
        except Exception as e:
            logger.error(f"Ошибка чтения FSM chat_id={chat_id}: {e}")
            return None, None

    # --- Telegram bot helpers: Idempotency tokens ---
    def is_new_idempotency_token(self, token: str, chat_id: int, action: str, ttl_seconds: int = 600) -> bool:
        try:
            with sqlite3.connect(self.db_path, timeout=5) as conn:
                # Очистка старых токенов
                try:
                    conn.execute('DELETE FROM bot_idempotency WHERE created_at < datetime("now", ?)', (f'-{int(ttl_seconds)} seconds',))
                except Exception:
                    pass
                try:
                    conn.execute('INSERT INTO bot_idempotency(token, chat_id, action) VALUES(?,?,?)', (str(token), int(chat_id), str(action)))
                    conn.commit()
                    return True
                except sqlite3.IntegrityError:
                    return False
        except Exception as e:
            logger.error(f"Ошибка записи идемпотентного токена {token}: {e}")
            return False

    # --- Telegram bot helpers: Notification toggles ---
    def get_bot_user_notif_settings(self, chat_id: int) -> dict:
        try:
            with sqlite3.connect(self.db_path, timeout=5) as conn:
                conn.row_factory = sqlite3.Row
                cur = conn.execute('''
                    SELECT notif_critical, notif_emergency, notif_postpone, notif_zone_events, notif_rain
                    FROM bot_users WHERE chat_id=? LIMIT 1
                ''', (int(chat_id),))
                row = cur.fetchone()
                if not row:
                    return {}
                return {
                    'critical': int(row['notif_critical'] or 0),
                    'emergency': int(row['notif_emergency'] or 0),
                    'postpone': int(row['notif_postpone'] or 0),
                    'zone_events': int(row['notif_zone_events'] or 0),
                    'rain': int(row['notif_rain'] or 0),
                }
        except Exception as e:
            logger.error(f"Ошибка чтения настроек уведомлений chat_id={chat_id}: {e}")
            return {}

    def set_bot_user_notif_toggle(self, chat_id: int, key: str, enabled: bool) -> bool:
        allowed = {
            'critical': 'notif_critical',
            'emergency': 'notif_emergency',
            'postpone': 'notif_postpone',
            'zone_events': 'notif_zone_events',
            'rain': 'notif_rain',
        }
        col = allowed.get(key)
        if not col:
            return False
        try:
            with sqlite3.connect(self.db_path, timeout=5) as conn:
                conn.execute(f'UPDATE bot_users SET {col}=? WHERE chat_id=?', (1 if enabled else 0, int(chat_id)))
                conn.commit()
                return True
        except Exception as e:
            logger.error(f"Ошибка сохранения тумблера уведомлений {key} chat_id={chat_id}: {e}")
            return False

    # --- API для zone_runs ---
    def create_zone_run(self, zone_id: int, group_id: int, start_utc: str, start_monotonic: float,
                        start_raw_pulses: Optional[int], pulse_liters_at_start: int,
                        base_m3_at_start: Optional[float] = None) -> Optional[int]:
        try:
            with sqlite3.connect(self.db_path, timeout=5) as conn:
                cur = conn.execute('''
                    INSERT INTO zone_runs(zone_id, group_id, start_utc, start_monotonic, start_raw_pulses, pulse_liters_at_start, base_m3_at_start)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                ''', (int(zone_id), int(group_id), str(start_utc), float(start_monotonic),
                      None if start_raw_pulses is None else int(start_raw_pulses), int(pulse_liters_at_start),
                      None if base_m3_at_start is None else float(base_m3_at_start)))
                run_id = cur.lastrowid
                conn.commit()
                return int(run_id)
        except Exception as e:
            logger.error(f"Ошибка создания zone_run для зоны {zone_id}: {e}")
            return None

    def get_open_zone_run(self, zone_id: int) -> Optional[Dict[str, Any]]:
        try:
            with sqlite3.connect(self.db_path, timeout=5) as conn:
                conn.row_factory = sqlite3.Row
                cur = conn.execute('''
                    SELECT * FROM zone_runs WHERE zone_id = ? AND end_utc IS NULL ORDER BY id DESC LIMIT 1
                ''', (int(zone_id),))
                row = cur.fetchone()
                return dict(row) if row else None
        except Exception as e:
            logger.error(f"Ошибка чтения открытого run для зоны {zone_id}: {e}")
            return None

    def finish_zone_run(self, run_id: int, end_utc: str, end_monotonic: float, end_raw_pulses: Optional[int],
                         total_liters: Optional[float], avg_flow_lpm: Optional[float], status: str = 'ok') -> bool:
        try:
            with sqlite3.connect(self.db_path, timeout=5) as conn:
                fields = ['end_utc = ?', 'end_monotonic = ?', 'status = ?', 'updated_at = CURRENT_TIMESTAMP']
                params: List[Any] = [str(end_utc), float(end_monotonic), str(status)]
                if end_raw_pulses is not None:
                    fields.append('end_raw_pulses = ?')
                    params.append(int(end_raw_pulses))
                if total_liters is not None:
                    fields.append('total_liters = ?')
                    params.append(float(total_liters))
                if avg_flow_lpm is not None:
                    fields.append('avg_flow_lpm = ?')
                    params.append(float(avg_flow_lpm))
                params.append(int(run_id))
                sql = f"UPDATE zone_runs SET {', '.join(fields)} WHERE id = ?"
                conn.execute(sql, params)
                conn.commit()
                return True
        except Exception as e:
            logger.error(f"Ошибка завершения zone_run {run_id}: {e}")
            return False
    def get_zones(self) -> List[Dict[str, Any]]:
        """Получить все зоны"""
        try:
            with sqlite3.connect(self.db_path, timeout=5) as conn:
                conn.row_factory = sqlite3.Row
                cursor = conn.execute('''
                    SELECT z.*, g.name as group_name, g.use_water_meter as use_water_meter
                    FROM zones z 
                    LEFT JOIN groups g ON z.group_id = g.id 
                    ORDER BY z.id
                ''')
                zones = []
                for row in cursor.fetchall():
                    zone = dict(row)
                    zone['group'] = zone['group_id']  # Для совместимости с фронтендом
                    zones.append(zone)
                return zones
        except Exception as e:
            logger.error(f"Ошибка получения зон: {e}")
            return []
    
    def get_zone(self, zone_id: int) -> Optional[Dict[str, Any]]:
        """Получить зону по ID"""
        try:
            with sqlite3.connect(self.db_path, timeout=5) as conn:
                conn.row_factory = sqlite3.Row
                cursor = conn.execute('''
                    SELECT z.*, g.name as group_name 
                    FROM zones z 
                    LEFT JOIN groups g ON z.group_id = g.id 
                    WHERE z.id = ?
                ''', (zone_id,))
                row = cursor.fetchone()
                if row:
                    zone = dict(row)
                    zone['group'] = zone['group_id']
                    return zone
                return None
        except Exception as e:
            logger.error(f"Ошибка получения зоны {zone_id}: {e}")
            return None
    
    def create_zone(self, zone_data: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """Создать новую зону"""
        try:
            with sqlite3.connect(self.db_path, timeout=5) as conn:
                # Берём topic как есть, без какой-либо нормализации
                topic = (zone_data.get('topic') or '').strip()
                zid_explicit = None
                try:
                    zid_explicit = int(zone_data.get('id')) if zone_data.get('id') is not None else None
                except Exception:
                    zid_explicit = None
                
                if zid_explicit is not None:
                    try:
                        conn.execute('''
                            INSERT INTO zones (id, name, icon, duration, group_id, topic, mqtt_server_id)
                            VALUES (?, ?, ?, ?, ?, ?, ?)
                        ''', (
                            zid_explicit,
                            zone_data.get('name') or 'Зона',
                            zone_data.get('icon') or '🌿',
                            int(zone_data.get('duration') or 10),
                            int(zone_data.get('group_id', zone_data.get('group', 1))),
                            topic,
                            zone_data.get('mqtt_server_id')
                        ))
                        conn.commit()
                        return self.get_zone(zid_explicit)
                    except Exception:
                        # fallback — без явного id
                        pass
                cursor = conn.execute('''
                    INSERT INTO zones (name, icon, duration, group_id, topic, mqtt_server_id)
                    VALUES (?, ?, ?, ?, ?, ?)
                ''', (
                    zone_data.get('name') or 'Зона',
                    zone_data.get('icon') or '🌿',
                    int(zone_data.get('duration') or 10),
                    int(zone_data.get('group_id', zone_data.get('group', 1))),
                    topic,
                    zone_data.get('mqtt_server_id')
                ))
                zone_id = cursor.lastrowid
                conn.commit()
                return self.get_zone(zone_id)
        except Exception as e:
            logger.error(f"Ошибка создания зоны: {e}")
            return None
    
    def update_zone(self, zone_id: int, zone_data: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """Обновить зону"""
        try:
            with sqlite3.connect(self.db_path, timeout=5) as conn:
                # Получаем текущие данные зоны
                current_zone = self.get_zone(zone_id)
                if not current_zone:
                    return None
                
                # Объединяем текущие данные с новыми
                updated_data = current_zone.copy()
                updated_data.update(zone_data)
                
                # Подготавливаем SQL запрос с учетом всех полей
                sql_fields = []
                params = []
                
                if 'name' in updated_data:
                    sql_fields.append('name = ?')
                    params.append(updated_data['name'])
                
                if 'icon' in updated_data:
                    sql_fields.append('icon = ?')
                    params.append(updated_data['icon'])
                
                if 'duration' in updated_data:
                    sql_fields.append('duration = ?')
                    params.append(updated_data['duration'])
                
                if 'group_id' in updated_data or 'group' in updated_data:
                    sql_fields.append('group_id = ?')
                    params.append(updated_data.get('group_id', updated_data.get('group', 1)))
                
                if 'topic' in updated_data:
                    # Сохраняем topic как есть, без нормализации
                    sql_fields.append('topic = ?')
                    params.append((updated_data.get('topic') or '').strip())
                
                if 'state' in updated_data:
                    sql_fields.append('state = ?')
                    params.append(updated_data['state'])
                
                if 'postpone_until' in updated_data:
                    sql_fields.append('postpone_until = ?')
                    params.append(updated_data['postpone_until'])
                
                if 'photo_path' in updated_data:
                    sql_fields.append('photo_path = ?')
                    params.append(updated_data['photo_path'])
                
                # Поддержка времени начала полива
                if 'watering_start_time' in updated_data:
                    sql_fields.append('watering_start_time = ?')
                    params.append(updated_data['watering_start_time'])

                if 'scheduled_start_time' in updated_data:
                    sql_fields.append('scheduled_start_time = ?')
                    params.append(updated_data['scheduled_start_time'])

                if 'last_watering_time' in updated_data:
                    sql_fields.append('last_watering_time = ?')
                    params.append(updated_data['last_watering_time'])
                
                # Поля статистики воды для зоны
                if 'last_avg_flow_lpm' in updated_data:
                    sql_fields.append('last_avg_flow_lpm = ?')
                    params.append(updated_data['last_avg_flow_lpm'])
                if 'last_total_liters' in updated_data:
                    sql_fields.append('last_total_liters = ?')
                    params.append(updated_data['last_total_liters'])

                if 'mqtt_server_id' in updated_data:
                    sql_fields.append('mqtt_server_id = ?')
                    params.append(updated_data.get('mqtt_server_id'))
                
                # Добавляем updated_at
                sql_fields.append('updated_at = CURRENT_TIMESTAMP')
                
                # Добавляем ID зоны
                params.append(zone_id)
                
                # Выполняем обновление
                sql = f'''
                    UPDATE zones 
                    SET {', '.join(sql_fields)}
                    WHERE id = ?
                '''
                
                conn.execute(sql, params)

                # Если зону переводят в группу 999 (БЕЗ ПОЛИВА) — исключаем её из всех программ
                target_group_id = updated_data.get('group_id', updated_data.get('group'))
                if target_group_id == 999:
                    cursor = conn.execute('SELECT id, zones FROM programs')
                    for row in cursor.fetchall():
                        try:
                            zones_list = json.loads(row[1])
                        except Exception:
                            continue
                        if zone_id in zones_list:
                            zones_list = [z for z in zones_list if z != zone_id]
                            conn.execute('UPDATE programs SET zones = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?', (json.dumps(zones_list), row[0]))

                conn.commit()
                return self.get_zone(zone_id)
        except Exception as e:
            logger.error(f"Ошибка обновления зоны {zone_id}: {e}")
            return None

    def update_zone_versioned(self, zone_id: int, updates: Dict[str, Any]) -> bool:
        """Обновить зону с инкрементом version, защищаясь от гонок (optimistic lock).
        Возвращает True при успешном обновлении.
        """
        try:
            with sqlite3.connect(self.db_path, timeout=5) as conn:
                conn.row_factory = sqlite3.Row
                cur = conn.execute('SELECT version FROM zones WHERE id = ?', (zone_id,))
                row = cur.fetchone()
                if not row:
                    return False
                old_version = int(row['version'] or 0)
                fields = []
                params = []
                for k, v in updates.items():
                    fields.append(f"{k} = ?")
                    params.append(v)
                fields.append('version = version + 1')
                params.extend([zone_id, old_version])
                sql = f"UPDATE zones SET {', '.join(fields)}, updated_at = CURRENT_TIMESTAMP WHERE id = ? AND version = ?"
                cur2 = conn.execute(sql, params)
                conn.commit()
                return cur2.rowcount == 1
        except Exception as e:
            logger.error(f"Ошибка versioned-обновления зоны {zone_id}: {e}")
            return False

    def bulk_update_zones(self, updates: List[Dict[str, Any]]) -> Dict[str, Any]:
        """Пакетное обновление зон в одной транзакции.

        updates: [{ 'id': int, <fields...> }]
        Возвращает: { updated: int, failed: [zone_id, ...] }
        """
        updated = 0
        failed: List[int] = []
        if not updates:
            return { 'updated': 0, 'failed': [] }
        try:
            with sqlite3.connect(self.db_path, timeout=5) as conn:
                for upd in updates:
                    try:
                        zone_id = int(upd.get('id'))
                    except Exception:
                        continue
                    # Получим текущие поля для корректного апдейта
                    cur = conn.execute('SELECT * FROM zones WHERE id = ?', (zone_id,))
                    row = cur.fetchone()
                    if not row:
                        failed.append(zone_id)
                        continue
                    current = dict(zip([d[0] for d in cur.description], row))
                    merged = current.copy()
                    merged.update(upd)
                    fields = []
                    params = []
                    def add(field: str, value):
                        fields.append(f"{field} = ?"); params.append(value)
                    if 'name' in merged: add('name', merged['name'])
                    if 'icon' in merged: add('icon', merged['icon'])
                    if 'duration' in merged: add('duration', int(merged['duration']))
                    if ('group_id' in merged) or ('group' in merged):
                        add('group_id', int(merged.get('group_id', merged.get('group', 1))))
                    if 'topic' in merged: add('topic', (merged.get('topic') or '').strip())
                    if 'state' in merged: add('state', merged['state'])
                    if 'postpone_until' in merged: add('postpone_until', merged['postpone_until'])
                    if 'postpone_reason' in merged: add('postpone_reason', merged['postpone_reason'])
                    if 'photo_path' in merged: add('photo_path', merged['photo_path'])
                    if 'watering_start_time' in merged: add('watering_start_time', merged['watering_start_time'])
                    if 'scheduled_start_time' in merged: add('scheduled_start_time', merged['scheduled_start_time'])
                    if 'last_watering_time' in merged: add('last_watering_time', merged['last_watering_time'])
                    # Статистика воды
                    if 'last_avg_flow_lpm' in merged: add('last_avg_flow_lpm', merged['last_avg_flow_lpm'])
                    if 'last_total_liters' in merged: add('last_total_liters', merged['last_total_liters'])
                    if 'mqtt_server_id' in merged: add('mqtt_server_id', merged.get('mqtt_server_id'))
                    fields.append('updated_at = CURRENT_TIMESTAMP')
                    params.append(zone_id)
                    sql = f"UPDATE zones SET {', '.join(fields)} WHERE id = ?"
                    try:
                        conn.execute(sql, params)
                        updated += 1
                    except Exception:
                        failed.append(zone_id)
                conn.commit()
            return { 'updated': updated, 'failed': failed }
        except Exception as e:
            logger.error(f"Ошибка bulk-обновления зон: {e}")
            return { 'updated': updated, 'failed': failed or [] }

    def bulk_upsert_zones(self, zones: List[Dict[str, Any]]) -> Dict[str, Any]:
        """Импорт зон: upsert множества зон в одной транзакции.

        zones: [{ id?, name, icon, duration, group_id, topic, mqtt_server_id, state? }]
        Если id указан и зона существует — обновляем только переданные поля.
        Если id указан и зона не существует — вставляем с явным id.
        Если id не указан — создаём новую зону.
        Возвращает: { created: int, updated: int, failed: int }
        """
        created = 0
        updated = 0
        failed = 0
        if not zones:
            return { 'created': 0, 'updated': 0, 'failed': 0 }
        try:
            with sqlite3.connect(self.db_path, timeout=10) as conn:
                for z in zones:
                    try:
                        zid = int(z['id']) if z.get('id') is not None else None
                    except Exception:
                        zid = None
                    try:
                        if zid is not None:
                            cur = conn.execute('SELECT id FROM zones WHERE id = ?', (zid,))
                            row = cur.fetchone()
                            if row:
                                # update existing with provided fields only
                                fields = []
                                params = []
                                def add(field: str, value):
                                    fields.append(f"{field} = ?"); params.append(value)
                                if 'name' in z: add('name', z['name'])
                                if 'icon' in z: add('icon', z['icon'])
                                if 'duration' in z: add('duration', int(z['duration']))
                                if ('group_id' in z) or ('group' in z):
                                    add('group_id', int(z.get('group_id', z.get('group', 1))))
                                if 'topic' in z: add('topic', (z.get('topic') or '').strip())
                                if 'state' in z: add('state', z['state'])
                                if 'mqtt_server_id' in z: add('mqtt_server_id', z.get('mqtt_server_id'))
                                fields.append('updated_at = CURRENT_TIMESTAMP')
                                params.append(zid)
                                if fields:
                                    conn.execute(f"UPDATE zones SET {', '.join(fields)} WHERE id = ?", params)
                                    updated += 1
                            else:
                                # insert with explicit id
                                conn.execute('''
                                    INSERT INTO zones (id, name, icon, duration, group_id, topic, mqtt_server_id)
                                    VALUES (?, ?, ?, ?, ?, ?, ?)
                                ''', (
                                    zid,
                                    z.get('name') or 'Зона',
                                    z.get('icon') or '🌿',
                                    int(z.get('duration') or 10),
                                    int(z.get('group_id', z.get('group', 1))),
                                    (z.get('topic') or '').strip(),
                                    z.get('mqtt_server_id')
                                ))
                                created += 1
                        else:
                            # insert without explicit id
                            conn.execute('''
                                INSERT INTO zones (name, icon, duration, group_id, topic, mqtt_server_id)
                                VALUES (?, ?, ?, ?, ?, ?)
                            ''', (
                                z.get('name') or 'Зона',
                                z.get('icon') or '🌿',
                                int(z.get('duration') or 10),
                                int(z.get('group_id', z.get('group', 1))),
                                (z.get('topic') or '').strip(),
                                z.get('mqtt_server_id')
                            ))
                            created += 1
                    except Exception:
                        failed += 1
                conn.commit()
            return { 'created': created, 'updated': updated, 'failed': failed }
        except Exception as e:
            logger.error(f"Ошибка bulk-импорта зон: {e}")
            return { 'created': created, 'updated': updated, 'failed': (failed or 0) }
    
    def delete_zone(self, zone_id: int) -> bool:
        """Удалить зону"""
        try:
            with sqlite3.connect(self.db_path, timeout=5) as conn:
                conn.execute('DELETE FROM zones WHERE id = ?', (zone_id,))
                conn.commit()
                return True
        except Exception as e:
            logger.error(f"Ошибка удаления зоны {zone_id}: {e}")
            return False
    
    def get_groups(self) -> List[Dict[str, Any]]:
        """Получить все группы"""
        try:
            with sqlite3.connect(self.db_path, timeout=5) as conn:
                conn.row_factory = sqlite3.Row
                cursor = conn.execute('''
                    SELECT g.*, COUNT(z.id) as zone_count
                    FROM groups g
                    LEFT JOIN zones z ON g.id = z.group_id
                    GROUP BY g.id
                    ORDER BY g.id
                ''')
                return [dict(row) for row in cursor.fetchall()]
        except Exception as e:
            logger.error(f"Ошибка получения групп: {e}")
            return []

    def create_group(self, name: str) -> Optional[Dict[str, Any]]:
        """Создать новую группу"""
        try:
            with sqlite3.connect(self.db_path, timeout=5) as conn:
                cursor = conn.execute('INSERT INTO groups (name) VALUES (?)', (name,))
                new_id = cursor.lastrowid
                conn.commit()
                return {'id': new_id, 'name': name, 'zone_count': 0}
        except Exception as e:
            logger.error(f"Ошибка создания группы '{name}': {e}")
            return None

    def delete_group(self, group_id: int) -> bool:
        """Удалить группу. Запрещено, если в группе есть зоны.

        Политика: безопаснее явно запретить удаление непустых групп. Пользователь 
        должен сам перенести зоны в другие группы или 999 (БЕЗ ПОЛИВА), а затем удалить.
        """
        try:
            if group_id == 999:
                return False
            with sqlite3.connect(self.db_path, timeout=5) as conn:
                # Проверяем, есть ли зоны в группе
                cursor = conn.execute('SELECT COUNT(*) FROM zones WHERE group_id = ?', (group_id,))
                cnt = cursor.fetchone()[0]
                if cnt > 0:
                    return False
                # Удаляем группу
                conn.execute('DELETE FROM groups WHERE id = ?', (group_id,))
                conn.commit()
                return True
        except Exception as e:
            logger.error(f"Ошибка удаления группы {group_id}: {e}")
            return False

    def get_zones_by_group(self, group_id: int) -> List[Dict[str, Any]]:
        """Получить зоны по группе"""
        try:
            with sqlite3.connect(self.db_path, timeout=5) as conn:
                conn.row_factory = sqlite3.Row
                cursor = conn.execute('''
                    SELECT z.*, g.name as group_name 
                    FROM zones z 
                    LEFT JOIN groups g ON z.group_id = g.id 
                    WHERE z.group_id = ?
                    ORDER BY z.id
                ''', (group_id,))
                zones = []
                for row in cursor.fetchall():
                    zone = dict(row)
                    zone['group'] = zone['group_id']  # Для совместимости с фронтендом
                    zones.append(zone)
                return zones
        except Exception as e:
            logger.error(f"Ошибка получения зон группы {group_id}: {e}")
            return []

    def clear_group_scheduled_starts(self, group_id: int) -> None:
        """Очистить плановые времена старта у всех зон в группе"""
        try:
            with sqlite3.connect(self.db_path) as conn:
                conn.execute('''
                    UPDATE zones
                    SET scheduled_start_time = NULL, updated_at = CURRENT_TIMESTAMP
                    WHERE group_id = ?
                ''', (group_id,))
                conn.commit()
        except Exception as e:
            logger.error(f"Ошибка очистки scheduled_start_time в группе {group_id}: {e}")

    def set_group_scheduled_starts(self, group_id: int, schedule: Dict[int, str]) -> None:
        """Установить плановые времена старта по зоне в группе. schedule: {zone_id: '%Y-%m-%d %H:%M:%S'}"""
        try:
            with sqlite3.connect(self.db_path) as conn:
                for zone_id, ts in schedule.items():
                    conn.execute('''
                        UPDATE zones
                        SET scheduled_start_time = ?, updated_at = CURRENT_TIMESTAMP
                        WHERE id = ? AND group_id = ?
                    ''', (ts, zone_id, group_id))
                conn.commit()
        except Exception as e:
            logger.error(f"Ошибка установки расписания scheduled_start_time для группы {group_id}: {e}")

    # ===== Настройки (settings) — универсальные геттеры/сеттеры и конфиг датчика дождя =====
    def get_setting_value(self, key: str) -> Optional[str]:
        try:
            with sqlite3.connect(self.db_path) as conn:
                conn.row_factory = sqlite3.Row
                cur = conn.execute('SELECT value FROM settings WHERE key = ? LIMIT 1', (key,))
                row = cur.fetchone()
                return str(row['value']) if row and row['value'] is not None else None
        except Exception as e:
            logger.error(f"Ошибка чтения settings[{key}]: {e}")
            return None

    def set_setting_value(self, key: str, value: Optional[str]) -> bool:
        try:
            with sqlite3.connect(self.db_path) as conn:
                if value is None:
                    conn.execute('DELETE FROM settings WHERE key = ?', (key,))
                else:
                    conn.execute('INSERT OR REPLACE INTO settings(key, value) VALUES (?, ?)', (key, str(value)))
                conn.commit()
                return True
        except Exception as e:
            logger.error(f"Ошибка записи settings[{key}]: {e}")
            return False

    def ensure_password_change_required(self) -> None:
        """Если пароль не установлен — генерируем случайный временный пароль и требуем смену (TASK-013)."""
        try:
            with sqlite3.connect(self.db_path) as conn:
                conn.row_factory = sqlite3.Row
                cur = conn.execute('SELECT value FROM settings WHERE key = ? LIMIT 1', ('password_hash',))
                row = cur.fetchone()
                if not row:
                    # Нет пароля — генерируем случайный начальный пароль вместо '1234'
                    import secrets
                    temp_password = secrets.token_urlsafe(12)
                    from werkzeug.security import generate_password_hash
                    pw_hash = generate_password_hash(temp_password, method='pbkdf2:sha256')
                    conn.execute('INSERT OR REPLACE INTO settings(key, value) VALUES (?, ?)', ('password_hash', pw_hash))
                    conn.execute('INSERT OR REPLACE INTO settings(key, value) VALUES (?, ?)', ('password_must_change', '1'))
                    logger.warning("Initial random password generated: %s (change it on first login!)", temp_password)
                else:
                    # Если в базе уже есть хэш, но флаг обязательной смены не установлен — форсируем
                    cur2 = conn.execute('SELECT value FROM settings WHERE key = ? LIMIT 1', ('password_must_change',))
                    row2 = cur2.fetchone()
                    if not row2:
                        conn.execute('INSERT OR REPLACE INTO settings(key, value) VALUES (?, ?)', ('password_must_change', '1'))
                conn.commit()
        except Exception as e:
            logger.error(f"Ошибка установки флага обязательной смены пароля: {e}")

    # ===== Логирование: флаг debug =====
    def get_logging_debug(self) -> bool:
        val = self.get_setting_value('logging.debug')
        return str(val or '0') in ('1','true','True')

    def set_logging_debug(self, enabled: bool) -> bool:
        return self.set_setting_value('logging.debug', '1' if enabled else '0')

    def get_rain_config(self) -> Dict[str, Any]:
        """Глобальная конфигурация датчика дождя."""
        enabled = self.get_setting_value('rain.enabled')
        topic = self.get_setting_value('rain.topic') or ''
        sensor_type = self.get_setting_value('rain.type') or 'NO'
        server_id = self.get_setting_value('rain.server_id')
        return {
            'enabled': str(enabled or '0') in ('1', 'true', 'True'),
            'topic': topic,
            'type': sensor_type if sensor_type in ('NO', 'NC') else 'NO',
            'server_id': int(server_id) if server_id and str(server_id).isdigit() else None,
        }

    def set_rain_config(self, cfg: Dict[str, Any]) -> bool:
        ok = True
        ok &= self.set_setting_value('rain.enabled', '1' if cfg.get('enabled') else '0')
        if 'topic' in cfg:
            ok &= self.set_setting_value('rain.topic', cfg.get('topic') or '')
        if 'type' in cfg:
            t = cfg.get('type')
            ok &= self.set_setting_value('rain.type', t if t in ('NO', 'NC') else 'NO')
        if 'server_id' in cfg:
            sid = cfg.get('server_id')
            ok &= self.set_setting_value('rain.server_id', str(int(sid)) if sid is not None else None)
        return bool(ok)

    # ===== Master valve settings =====
    def get_master_config(self) -> Dict[str, Any]:
        try:
            enabled = self.get_setting_value('master.enabled')
            topic = self.get_setting_value('master.topic') or ''
            server_id = self.get_setting_value('master.server_id')
            delay_ms = self.get_setting_value('master.delay_ms')
            return {
                'enabled': str(enabled or '0') in ('1','true','True'),
                'topic': topic,
                'server_id': int(server_id) if server_id and str(server_id).isdigit() else None,
                'delay_ms': int(delay_ms) if (delay_ms and str(delay_ms).isdigit()) else 300
            }
        except Exception as e:
            logger.error(f"Ошибка чтения master_config: {e}")
            return {'enabled': False, 'topic': '', 'server_id': None, 'delay_ms': 300}

    def set_master_config(self, cfg: Dict[str, Any]) -> bool:
        ok = True
        try:
            ok &= self.set_setting_value('master.enabled', '1' if cfg.get('enabled') else '0')
            if 'topic' in cfg:
                ok &= self.set_setting_value('master.topic', cfg.get('topic') or '')
            if 'server_id' in cfg:
                sid = cfg.get('server_id')
                ok &= self.set_setting_value('master.server_id', str(int(sid)) if sid is not None else None)
            if 'delay_ms' in cfg:
                ok &= self.set_setting_value('master.delay_ms', str(int(cfg.get('delay_ms') or 300)))
            return bool(ok)
        except Exception as e:
            logger.error(f"Ошибка записи master_config: {e}")
            return False

    # ===== Датчики среды (температура/влажность) =====
    def get_env_config(self) -> Dict[str, Any]:
        temp_enabled = self.get_setting_value('env.temp.enabled')
        temp_topic = self.get_setting_value('env.temp.topic') or ''
        temp_server_id = self.get_setting_value('env.temp.server_id')
        hum_enabled = self.get_setting_value('env.hum.enabled')
        hum_topic = self.get_setting_value('env.hum.topic') or ''
        hum_server_id = self.get_setting_value('env.hum.server_id')
        return {
            'temp': {
                'enabled': str(temp_enabled or '0') in ('1','true','True'),
                'topic': temp_topic,
                'server_id': int(temp_server_id) if temp_server_id and str(temp_server_id).isdigit() else None,
            },
            'hum': {
                'enabled': str(hum_enabled or '0') in ('1','true','True'),
                'topic': hum_topic,
                'server_id': int(hum_server_id) if hum_server_id and str(hum_server_id).isdigit() else None,
            }
        }

    def set_env_config(self, cfg: Dict[str, Any]) -> bool:
        ok = True
        temp = cfg.get('temp') or {}
        hum = cfg.get('hum') or {}
        ok &= self.set_setting_value('env.temp.enabled', '1' if temp.get('enabled') else '0')
        ok &= self.set_setting_value('env.temp.topic', temp.get('topic') or '')
        ok &= self.set_setting_value('env.temp.server_id', str(int(temp.get('server_id'))) if temp.get('server_id') is not None else None)
        ok &= self.set_setting_value('env.hum.enabled', '1' if hum.get('enabled') else '0')
        ok &= self.set_setting_value('env.hum.topic', hum.get('topic') or '')
        ok &= self.set_setting_value('env.hum.server_id', str(int(hum.get('server_id'))) if hum.get('server_id') is not None else None)
        return bool(ok)

    def get_group_use_rain(self, group_id: int) -> bool:
        try:
            with sqlite3.connect(self.db_path) as conn:
                conn.row_factory = sqlite3.Row
                cur = conn.execute('SELECT use_rain_sensor FROM groups WHERE id = ? LIMIT 1', (group_id,))
                row = cur.fetchone()
                if not row:
                    return False
                val = row['use_rain_sensor']
                return bool(int(val or 0))
        except Exception as e:
            logger.error(f"Ошибка чтения use_rain_sensor для группы {group_id}: {e}")
            return False

    def set_group_use_rain(self, group_id: int, enabled: bool) -> bool:
        try:
            with sqlite3.connect(self.db_path) as conn:
                conn.execute('UPDATE groups SET use_rain_sensor = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?', (1 if enabled else 0, group_id))
                conn.commit()
                return True
        except Exception as e:
            logger.error(f"Ошибка записи use_rain_sensor для группы {group_id}: {e}")
            return False

    def clear_scheduled_for_zone_group_peers(self, zone_id: int, group_id: int) -> None:
        """Очистить scheduled_start_time у всех зон группы, кроме указанной"""
        try:
            with sqlite3.connect(self.db_path) as conn:
                conn.execute('''
                    UPDATE zones
                    SET scheduled_start_time = NULL, updated_at = CURRENT_TIMESTAMP
                    WHERE group_id = ? AND id != ?
                ''', (group_id, zone_id))
                conn.commit()
        except Exception as e:
            logger.error(f"Ошибка очистки расписания у одногруппных зон для зоны {zone_id}: {e}")

    # ===== Расчет следующего времени полива и перестройка очереди =====
    def compute_next_run_for_zone(self, zone_id: int) -> Optional[str]:
        """Рассчитать ближайшее будущее время запуска зоны по всем программам.
        Возвращает строку '%Y-%m-%d %H:%M:%S' или None, если программ нет.
        """
        try:
            zone = self.get_zone(zone_id)
            if not zone:
                return None
            programs = self.get_programs()
            if not programs:
                return None
            now = datetime.now()
            best_dt: Optional[datetime] = None
            for prog in programs:
                if zone_id not in prog.get('zones', []):
                    continue
                # Для каждого ближайшего дня из списка дней найдем ближайшую дату
                for offset in range(0, 14):  # ищем на 2 недели вперед
                    dt_candidate = now + timedelta(days=offset)
                    if dt_candidate.weekday() in prog['days']:
                        hour, minute = map(int, prog['time'].split(':'))
                        start_dt = dt_candidate.replace(hour=hour, minute=minute, second=0, microsecond=0)
                        if start_dt <= now:
                            continue
                        # Сдвиг по позиции зоны в программе
                        cum = 0
                        for zid in sorted(prog['zones']):
                            dur = self.get_zone_duration(zid)
                            if zid == zone_id:
                                candidate = start_dt + timedelta(minutes=cum)
                                if best_dt is None or candidate < best_dt:
                                    best_dt = candidate
                                break
                            cum += dur
                        break
            if best_dt:
                return best_dt.strftime('%Y-%m-%d %H:%M:%S')
            return None
        except Exception as e:
            logger.error(f"Ошибка расчета следующего запуска для зоны {zone_id}: {e}")
            return None

    def reschedule_group_to_next_program(self, group_id: int) -> None:
        """Пересчитать и записать scheduled_start_time всем зонам группы на ближайшие будущие запуски.
        Используется при отмене текущего полива группы/запуске вручную.
        """
        try:
            zones = self.get_zones_by_group(group_id)
            schedule: Dict[int, str] = {}
            for z in zones:
                nxt = self.compute_next_run_for_zone(z['id'])
                if nxt:
                    schedule[z['id']] = nxt
            self.clear_group_scheduled_starts(group_id)
            if schedule:
                self.set_group_scheduled_starts(group_id, schedule)
        except Exception as e:
            logger.error(f"Ошибка перестройки расписания группы {group_id}: {e}")
    
    # ==== Program cancellations (per date) ====
    def cancel_program_run_for_group(self, program_id: int, run_date: str, group_id: int) -> bool:
        try:
            with sqlite3.connect(self.db_path) as conn:
                conn.execute('''
                    INSERT OR REPLACE INTO program_cancellations(program_id, run_date, group_id)
                    VALUES (?, ?, ?)
                ''', (int(program_id), str(run_date), int(group_id)))
                conn.commit()
            return True
        except Exception as e:
            logger.error(f"Ошибка записи отмены программы {program_id} на {run_date} для группы {group_id}: {e}")
            return False

    def is_program_run_cancelled_for_group(self, program_id: int, run_date: str, group_id: int) -> bool:
        try:
            with sqlite3.connect(self.db_path) as conn:
                conn.row_factory = sqlite3.Row
                cur = conn.execute('''
                    SELECT 1 FROM program_cancellations
                    WHERE program_id = ? AND run_date = ? AND group_id = ? LIMIT 1
                ''', (int(program_id), str(run_date), int(group_id)))
                return cur.fetchone() is not None
        except Exception as e:
            logger.error(f"Ошибка чтения отмены программы {program_id} на {run_date} для группы {group_id}: {e}")
            return False

    def clear_program_cancellations_for_group_on_date(self, group_id: int, run_date: str) -> bool:
        """Удалить все отмены программ для указанной группы на указанную дату.
        Используется для снятия отмен, выставленных дождём, после окончания дождя.
        """
        try:
            with sqlite3.connect(self.db_path) as conn:
                conn.execute('''
                    DELETE FROM program_cancellations
                    WHERE group_id = ? AND run_date = ?
                ''', (int(group_id), str(run_date)))
                conn.commit()
            return True
        except Exception as e:
            logger.error(f"Ошибка очистки отмен программ на {run_date} для группы {group_id}: {e}")
            return False
    
    def update_group(self, group_id: int, name: str) -> bool:
        """Обновить название группы"""
        try:
            with sqlite3.connect(self.db_path) as conn:
                conn.execute('''
                    UPDATE groups 
                    SET name = ?, updated_at = CURRENT_TIMESTAMP
                    WHERE id = ?
                ''', (name, group_id))
                conn.commit()
                return True
        except Exception as e:
            logger.error(f"Ошибка обновления группы {group_id}: {e}")
            return False

    def update_group_fields(self, group_id: int, updates: Dict[str, Any]) -> bool:
        """Обновить произвольные поля группы (мастер-клапан, сенсоры)."""
        if not updates:
            return False
        allowed = {
            'use_master_valve', 'master_mqtt_topic', 'master_mode', 'master_mqtt_server_id',
            'use_pressure_sensor', 'pressure_mqtt_topic', 'pressure_unit', 'pressure_mqtt_server_id',
            'use_water_meter', 'water_mqtt_topic', 'water_mqtt_server_id', 'master_valve_observed',
            'water_pulse_size', 'water_base_value_m3', 'water_base_pulses'
        }
        set_parts = []
        params = []
        for k, v in updates.items():
            if k in allowed:
                set_parts.append(f"{k} = ?")
                params.append(v)
        if not set_parts:
            return False
        params.append(group_id)
        try:
            with sqlite3.connect(self.db_path) as conn:
                sql = f"UPDATE groups SET {', '.join(set_parts)}, updated_at = CURRENT_TIMESTAMP WHERE id = ?"
                conn.execute(sql, tuple(params))
                conn.commit()
                return True
        except Exception as e:
            logger.error(f"Ошибка обновления полей группы {group_id}: {e}")
            return False
    
    def get_programs(self) -> List[Dict[str, Any]]:
        """Получить все программы"""
        try:
            with sqlite3.connect(self.db_path) as conn:
                conn.row_factory = sqlite3.Row
                cursor = conn.execute('SELECT * FROM programs ORDER BY id')
                programs = []
                for row in cursor.fetchall():
                    program = dict(row)
                    program['days'] = [int(d) for d in json.loads(program['days'])]
                    program['zones'] = json.loads(program['zones'])
                    programs.append(program)
                return programs
        except Exception as e:
            logger.error(f"Ошибка получения программ: {e}")
            return []

    # ===== MQTT servers CRUD =====
    @staticmethod
    def _decrypt_mqtt_password(server: Dict[str, Any]) -> Dict[str, Any]:
        """Decrypt MQTT password if it's stored encrypted (ENC: prefix)."""
        pwd = server.get('password')
        if pwd and isinstance(pwd, str) and pwd.startswith('ENC:'):
            server['password'] = decrypt_secret(pwd[4:])
        return server

    def get_mqtt_servers(self) -> List[Dict[str, Any]]:
        try:
            with sqlite3.connect(self.db_path) as conn:
                conn.row_factory = sqlite3.Row
                cur = conn.execute('SELECT * FROM mqtt_servers ORDER BY id')
                return [self._decrypt_mqtt_password(dict(row)) for row in cur.fetchall()]
        except Exception as e:
            logger.error(f"Ошибка получения MQTT серверов: {e}")
            return []

    def create_mqtt_server(self, data: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        try:
            # Encrypt password before storing
            raw_password = data.get('password')
            enc_password = ('ENC:' + encrypt_secret(raw_password)) if raw_password else raw_password
            with sqlite3.connect(self.db_path) as conn:
                cur = conn.execute('''
                    INSERT INTO mqtt_servers (name, host, port, username, password, client_id, enabled,
                                              tls_enabled, tls_ca_path, tls_cert_path, tls_key_path, tls_insecure, tls_version)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ''', (
                    data.get('name', 'MQTT'),
                    data.get('host', 'localhost'),
                    int(data.get('port', 1883)),
                    data.get('username'),
                    enc_password,
                    data.get('client_id'),
                    1 if data.get('enabled', True) else 0,
                    1 if data.get('tls_enabled') else 0,
                    data.get('tls_ca_path'),
                    data.get('tls_cert_path'),
                    data.get('tls_key_path'),
                    1 if data.get('tls_insecure') else 0,
                    data.get('tls_version')
                ))
                server_id = cur.lastrowid
                conn.commit()
                return self.get_mqtt_server(server_id)
        except Exception as e:
            logger.error(f"Ошибка создания MQTT сервера: {e}")
            return None

    def get_mqtt_server(self, server_id: int) -> Optional[Dict[str, Any]]:
        try:
            with sqlite3.connect(self.db_path) as conn:
                conn.row_factory = sqlite3.Row
                cur = conn.execute('SELECT * FROM mqtt_servers WHERE id = ?', (server_id,))
                row = cur.fetchone()
                return self._decrypt_mqtt_password(dict(row)) if row else None
        except Exception as e:
            logger.error(f"Ошибка получения MQTT сервера {server_id}: {e}")
            return None

    def update_mqtt_server(self, server_id: int, data: Dict[str, Any]) -> bool:
        try:
            # Encrypt password before storing
            raw_password = data.get('password')
            enc_password = ('ENC:' + encrypt_secret(raw_password)) if raw_password else raw_password
            with sqlite3.connect(self.db_path) as conn:
                conn.execute('''
                    UPDATE mqtt_servers
                    SET name = ?, host = ?, port = ?, username = ?, password = ?, client_id = ?, enabled = ?,
                        tls_enabled = ?, tls_ca_path = ?, tls_cert_path = ?, tls_key_path = ?, tls_insecure = ?, tls_version = ?,
                        updated_at = CURRENT_TIMESTAMP
                    WHERE id = ?
                ''', (
                    data.get('name', 'MQTT'),
                    data.get('host', 'localhost'),
                    int(data.get('port', 1883)),
                    data.get('username'),
                    enc_password,
                    data.get('client_id'),
                    1 if data.get('enabled', True) else 0,
                    1 if data.get('tls_enabled') else 0,
                    data.get('tls_ca_path'),
                    data.get('tls_cert_path'),
                    data.get('tls_key_path'),
                    1 if data.get('tls_insecure') else 0,
                    data.get('tls_version'),
                    server_id
                ))
                conn.commit()
                return True
        except Exception as e:
            logger.error(f"Ошибка обновления MQTT сервера {server_id}: {e}")
            return False

    def delete_mqtt_server(self, server_id: int) -> bool:
        try:
            with sqlite3.connect(self.db_path) as conn:
                conn.execute('DELETE FROM mqtt_servers WHERE id = ?', (server_id,))
                conn.commit()
                return True
        except Exception as e:
            logger.error(f"Ошибка удаления MQTT сервера {server_id}: {e}")
            return False
    
    def get_logs(self, event_type: str = None, from_date: str = None, to_date: str = None) -> List[Dict[str, Any]]:
        """Получить логи с фильтрацией"""
        try:
            with sqlite3.connect(self.db_path) as conn:
                conn.row_factory = sqlite3.Row
                # В SQLite CURRENT_TIMESTAMP хранится в UTC. Для UI возвращаем локальное время.
                # Приводим timestamp к локальному в SELECT, остальное оставляем как есть.
                query = (
                    "SELECT id, type, details, "
                    "strftime('%Y-%m-%d %H:%M:%S', timestamp, 'localtime') AS timestamp "
                    "FROM logs WHERE 1=1"
                )
                params = []
                
                if event_type:
                    query += ' AND type = ?'
                    params.append(event_type)
                
                if from_date:
                    query += ' AND timestamp >= ?'
                    params.append(from_date)
                
                if to_date:
                    query += ' AND timestamp <= ?'
                    params.append(f"{to_date} 23:59:59")
                
                query += ' ORDER BY timestamp DESC LIMIT 1000'
                
                cursor = conn.execute(query, params)
                return [dict(row) for row in cursor.fetchall()]
        except Exception as e:
            logger.error(f"Ошибка получения логов: {e}")
            return []
    
    def add_log(self, log_type: str, details: str = None) -> Optional[int]:
        """Добавить запись в лог"""
        try:
            with sqlite3.connect(self.db_path) as conn:
                cursor = conn.execute('''
                    INSERT INTO logs (type, details)
                    VALUES (?, ?)
                ''', (log_type, details))
                log_id = cursor.lastrowid
                conn.commit()
                return log_id
        except Exception as e:
            logger.error(f"Ошибка добавления лога: {e}")
            return None
    
    def update_zone_postpone(self, zone_id: int, postpone_until: str = None, reason: str = None) -> bool:
        """Обновить отложенный полив зоны с указанием причины"""
        try:
            with sqlite3.connect(self.db_path) as conn:
                conn.execute('''
                    UPDATE zones 
                    SET postpone_until = ?, postpone_reason = ?, updated_at = CURRENT_TIMESTAMP
                    WHERE id = ?
                ''', (postpone_until, reason, zone_id))
                conn.commit()
                return True
        except Exception as e:
            logger.error(f"Ошибка обновления отложенного полива зоны {zone_id}: {e}")
            return False
    
    def create_backup(self) -> str:
        """Создать резервную копию базы данных"""
        try:
            if not os.path.exists(self.backup_dir):
                os.makedirs(self.backup_dir)
            
            timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
            backup_path = os.path.join(self.backup_dir, f'irrigation_backup_{timestamp}.db')
            
            # В режиме WAL прямое копирование файла .db может не включать данные из -wal.
            # Используем SQLite backup API, чтобы получить консистентную копию.
            try:
                with sqlite3.connect(self.db_path) as src_conn:
                    with sqlite3.connect(backup_path) as dst_conn:
                        src_conn.backup(dst_conn)
                # По возможности попросим чекпоинт (не критично для копии, но уменьшит артефакты у исходной БД)
                try:
                    with sqlite3.connect(self.db_path) as c:
                        c.execute("PRAGMA wal_checkpoint(TRUNCATE)")
                        c.commit()
                except Exception:
                    pass
            except Exception:
                # Fallback на физическое копирование, если backup API недоступен
                shutil.copy2(self.db_path, backup_path)
            
            # Удаляем старые резервные копии (оставляем последние 7)
            self._cleanup_old_backups()
            
            logger.info(f"Резервная копия создана: {backup_path}")
            return backup_path
        except Exception as e:
            logger.error(f"Ошибка создания резервной копии: {e}")
            return None
    
    def _cleanup_old_backups(self, keep_count: int = 7):
        """Удалить старые резервные копии"""
        try:
            backup_files = []
            for file in os.listdir(self.backup_dir):
                if file.startswith('irrigation_backup_') and file.endswith('.db'):
                    file_path = os.path.join(self.backup_dir, file)
                    backup_files.append((file_path, os.path.getmtime(file_path)))
            
            # Сортируем по времени создания (новые в конце)
            backup_files.sort(key=lambda x: x[1])
            
            # Удаляем старые файлы
            for file_path, _ in backup_files[:-keep_count]:
                os.remove(file_path)
                logger.info(f"Удалена старая резервная копия: {file_path}")
        except Exception as e:
            logger.error(f"Ошибка очистки старых резервных копий: {e}")

    def create_program(self, program_data: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """Создать новую программу"""
        try:
            with sqlite3.connect(self.db_path) as conn:
                # Нормализуем дни (0-6)
                try:
                    norm_days = [int(d) for d in program_data['days']]
                except Exception:
                    norm_days = []
                # Если формат 1..7, смещаем в 0..6
                if norm_days and min(norm_days) >= 1 and max(norm_days) <= 7:
                    norm_days = [max(0, min(6, d - 1)) for d in norm_days]
                cursor = conn.execute('''
                    INSERT INTO programs (name, time, days, zones)
                    VALUES (?, ?, ?, ?)
                ''', (
                    program_data['name'],
                    program_data['time'],
                    json.dumps(norm_days),
                    json.dumps(program_data['zones'])
                ))
                program_id = cursor.lastrowid
                conn.commit()
                return self.get_program(program_id)
        except Exception as e:
            logger.error(f"Ошибка создания программы: {e}")
            return None
    
    def get_program(self, program_id: int) -> Optional[Dict[str, Any]]:
        """Получить программу по ID"""
        try:
            with sqlite3.connect(self.db_path) as conn:
                conn.row_factory = sqlite3.Row
                cursor = conn.execute('SELECT * FROM programs WHERE id = ?', (program_id,))
                row = cursor.fetchone()
                if row:
                    program = dict(row)
                    program['days'] = [int(d) for d in json.loads(program['days'])]
                    program['zones'] = json.loads(program['zones'])
                    return program
                return None
        except Exception as e:
            logger.error(f"Ошибка получения программы {program_id}: {e}")
            return None
    
    def update_program(self, program_id: int, program_data: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """Обновить программу"""
        try:
            with sqlite3.connect(self.db_path) as conn:
                # Нормализуем дни (0-6)
                try:
                    norm_days = [int(d) for d in program_data['days']]
                except Exception:
                    norm_days = []
                if norm_days and min(norm_days) >= 1 and max(norm_days) <= 7:
                    norm_days = [max(0, min(6, d - 1)) for d in norm_days]
                conn.execute('''
                    UPDATE programs 
                    SET name = ?, time = ?, days = ?, zones = ?, updated_at = CURRENT_TIMESTAMP
                    WHERE id = ?
                ''', (
                    program_data['name'],
                    program_data['time'],
                    json.dumps(norm_days),
                    json.dumps(program_data['zones']),
                    program_id
                ))
                conn.commit()
                return self.get_program(program_id)
        except Exception as e:
            logger.error(f"Ошибка обновления программы {program_id}: {e}")
            return None
    
    def delete_program(self, program_id: int) -> bool:
        """Удалить программу"""
        try:
            with sqlite3.connect(self.db_path) as conn:
                conn.execute('DELETE FROM programs WHERE id = ?', (program_id,))
                conn.commit()
                return True
        except Exception as e:
            logger.error(f"Ошибка удаления программы {program_id}: {e}")
            return False

    def update_zone_photo(self, zone_id: int, photo_path: Optional[str]) -> bool:
        """Обновить фотографию зоны"""
        try:
            with sqlite3.connect(self.db_path) as conn:
                conn.execute('''
                    UPDATE zones 
                    SET photo_path = ?, updated_at = CURRENT_TIMESTAMP
                    WHERE id = ?
                ''', (photo_path, zone_id))
                conn.commit()
                return True
        except Exception as e:
            logger.error(f"Ошибка обновления фото зоны {zone_id}: {e}")
            return False

    def check_program_conflicts(self, program_id: int = None, time: str = None, zones: List[int] = None, days: List[str] = None) -> List[Dict[str, Any]]:
        """Проверка пересечения программ полива"""
        try:
            with sqlite3.connect(self.db_path) as conn:
                conn.row_factory = sqlite3.Row
                
                # Получаем все программы
                query = '''
                    SELECT id, name, time, days, zones 
                    FROM programs
                '''
                
                if program_id:
                    query += ' WHERE id != ?'
                    cursor = conn.execute(query, (program_id,))
                else:
                    cursor = conn.execute(query)
                
                programs = cursor.fetchall()
                conflicts = []
                
                if not time or not zones or not days:
                    return conflicts
                
                # Парсим время программы
                try:
                    program_hour, program_minute = map(int, time.split(':'))
                    program_minutes = program_hour * 60 + program_minute
                except:
                    return conflicts
                
                # Нормализуем массив дней (могут прийти строками)
                try:
                    norm_days = [int(d) for d in days]
                except Exception:
                    norm_days = days

                # Кэшируем длительности и группы заранее для ускорения
                durations_cache: Dict[int, int] = {}
                groups_cache: Dict[int, int] = {}
                try:
                    curz = conn.execute('SELECT id, duration, group_id FROM zones')
                    for zid, dur, gid in curz.fetchall():
                        durations_cache[int(zid)] = int(dur or 0)
                        groups_cache[int(zid)] = int(gid or 0)
                except Exception:
                    pass
                def _get_dur(zid: int) -> int:
                    try:
                        return int(durations_cache.get(int(zid), 0))
                    except Exception:
                        return 0
                def _get_gid(zid: int) -> int:
                    try:
                        return int(groups_cache.get(int(zid), 0))
                    except Exception:
                        return 0
                # Получаем суммарную продолжительность полива для выбранных зон
                # Зоны поливаются последовательно, поэтому суммируем их длительности
                total_duration = 0
                for zone_id in zones:
                    total_duration += _get_dur(int(zone_id))
                
                # Время окончания программы
                program_end_minutes = program_minutes + total_duration
                
                for program in programs:
                    program_data = dict(program)
                    program_data['days'] = json.loads(program_data['days'])
                    program_data['zones'] = json.loads(program_data['zones'])
                    
                    # Проверяем пересечение дней
                    common_days = set(norm_days) & set(program_data['days'])
                    if not common_days:
                        continue
                    
                    # Проверяем пересечение зон
                    common_zones = set(zones) & set(program_data['zones'])
                    
                    # Проверяем пересечение групп
                    zones_groups = { _get_gid(int(zid)) for zid in zones }
                    existing_zones_groups = { _get_gid(int(zid)) for zid in program_data['zones'] }
                    
                    # Проверяем пересечение групп
                    common_groups = zones_groups & existing_zones_groups
                    
                    # Конфликт есть, если есть пересечение по зонам ИЛИ по группам
                    if not common_zones and not common_groups:
                        continue
                    
                    # Парсим время существующей программы
                    try:
                        existing_hour, existing_minute = map(int, program_data['time'].split(':'))
                        existing_minutes = existing_hour * 60 + existing_minute
                    except:
                        continue
                    
                    # Получаем суммарную продолжительность существующей программы
                    # Зоны поливаются последовательно, поэтому суммируем их длительности
                    existing_total_duration = sum(_get_dur(int(zid)) for zid in program_data['zones'])
                    
                    # Время окончания существующей программы
                    existing_end_minutes = existing_minutes + existing_total_duration
                    
                    # Проверяем пересечение по времени
                    # Программы пересекаются, если:
                    # 1. Новая программа начинается во время работы существующей
                    # 2. Существующая программа начинается во время работы новой
                    # 3. Программы начинаются одновременно
                    
                    if (program_minutes < existing_end_minutes and program_end_minutes > existing_minutes):
                        conflicts.append({
                            'program_id': program_data['id'],
                            'program_name': program_data['name'],
                            'program_time': program_data['time'],
                            'program_duration': existing_total_duration,
                            'common_zones': list(common_zones),
                            'common_groups': list(common_groups),
                            'common_days': list(common_days),
                            'overlap_start': max(program_minutes, existing_minutes),
                            'overlap_end': min(program_end_minutes, existing_end_minutes)
                        })
                
                return conflicts
                
        except Exception as e:
            logger.error(f"Ошибка проверки пересечения программ: {e}")
            return []

    # Настройки/пароль
    def get_password_hash(self) -> Optional[str]:
        try:
            with sqlite3.connect(self.db_path) as conn:
                cur = conn.execute('SELECT value FROM settings WHERE key = ? LIMIT 1', ('password_hash',))
                row = cur.fetchone()
                return row[0] if row else None
        except Exception as e:
            logger.error(f"Ошибка чтения пароля: {e}")
            return None

    def set_password(self, new_password: str) -> bool:
        try:
            with sqlite3.connect(self.db_path) as conn:
                conn.execute('INSERT OR REPLACE INTO settings(key, value) VALUES (?, ?)', (
                    'password_hash', generate_password_hash(new_password, method='pbkdf2:sha256')
                ))
                # Сбрасываем флаг обязательной смены пароля
                conn.execute('INSERT OR REPLACE INTO settings(key, value) VALUES (?, ?)', (
                    'password_must_change', '0'
                ))
                conn.commit()
                return True
        except Exception as e:
            logger.error(f"Ошибка обновления пароля: {e}")
            return False

    # === Settings: early off seconds (0..15) ===
    def get_early_off_seconds(self) -> int:
        try:
            with sqlite3.connect(self.db_path) as conn:
                cur = conn.execute('SELECT value FROM settings WHERE key = ? LIMIT 1', ('early_off_seconds',))
                row = cur.fetchone()
                val = int(row[0]) if row and row[0] is not None else 3
                if val < 0: val = 0
                if val > 15: val = 15
                return val
        except Exception as e:
            logger.error(f"Ошибка чтения early_off_seconds: {e}")
            return 3

    def set_early_off_seconds(self, seconds: int) -> bool:
        try:
            try:
                val = int(seconds)
            except Exception:
                return False
            if val < 0: val = 0
            if val > 15: val = 15
            with sqlite3.connect(self.db_path) as conn:
                conn.execute('INSERT OR REPLACE INTO settings(key, value) VALUES (?, ?)', (
                    'early_off_seconds', str(val)
                ))
                conn.commit()
            return True
        except Exception as e:
            logger.error(f"Ошибка записи early_off_seconds: {e}")
            return False

    def get_zone_duration(self, zone_id: int) -> int:
        """Получить продолжительность полива зоны"""
        try:
            with sqlite3.connect(self.db_path) as conn:
                cursor = conn.execute('SELECT duration FROM zones WHERE id = ?', (zone_id,))
                result = cursor.fetchone()
                return result[0] if result else 0
        except Exception as e:
            logger.error(f"Ошибка получения продолжительности зоны {zone_id}: {e}")
            return 0

    def get_water_usage(self, days: int = 7, zone_id: int = None) -> List[Dict[str, Any]]:
        """Получить данные расхода воды"""
        try:
            with sqlite3.connect(self.db_path) as conn:
                conn.row_factory = sqlite3.Row
                
                if zone_id:
                    cursor = conn.execute('''
                        SELECT w.*, z.name as zone_name
                        FROM water_usage w
                        LEFT JOIN zones z ON w.zone_id = z.id
                        WHERE w.zone_id = ? AND w.timestamp >= datetime('now', '-{} days')
                        ORDER BY w.timestamp DESC
                    '''.format(days), (zone_id,))
                else:
                    cursor = conn.execute('''
                        SELECT w.*, z.name as zone_name
                        FROM water_usage w
                        LEFT JOIN zones z ON w.zone_id = z.id
                        WHERE w.timestamp >= datetime('now', '-{} days')
                        ORDER BY w.timestamp DESC
                    '''.format(days))
                
                return [dict(row) for row in cursor.fetchall()]
        except Exception as e:
            logger.error(f"Ошибка получения данных расхода воды: {e}")
            return []

    def add_water_usage(self, zone_id: int, liters: float) -> bool:
        """Добавить запись о расходе воды"""
        try:
            with sqlite3.connect(self.db_path) as conn:
                conn.execute('''
                    INSERT INTO water_usage (zone_id, liters)
                    VALUES (?, ?)
                ''', (zone_id, liters))
                conn.commit()
                return True
        except Exception as e:
            logger.error(f"Ошибка добавления записи расхода воды: {e}")
            return False

    def get_water_statistics(self, days: int = 30) -> Dict[str, Any]:
        """Получить статистику расхода воды"""
        try:
            with sqlite3.connect(self.db_path) as conn:
                # Общий расход за период
                cursor = conn.execute('''
                    SELECT SUM(liters) as total_liters
                    FROM water_usage
                    WHERE timestamp >= datetime('now', '-{} days')
                '''.format(days))
                total_liters = cursor.fetchone()[0] or 0
                
                # Расход по зонам
                cursor = conn.execute('''
                    SELECT z.name, SUM(w.liters) as liters
                    FROM water_usage w
                    LEFT JOIN zones z ON w.zone_id = z.id
                    WHERE w.timestamp >= datetime('now', '-{} days')
                    GROUP BY w.zone_id, z.name
                    ORDER BY liters DESC
                '''.format(days))
                zone_usage = [dict(row) for row in cursor.fetchall()]
                
                # Средний расход в день
                cursor = conn.execute('''
                    SELECT AVG(daily_liters) as avg_daily
                    FROM (
                        SELECT DATE(timestamp) as date, SUM(liters) as daily_liters
                        FROM water_usage
                        WHERE timestamp >= datetime('now', '-{} days')
                        GROUP BY DATE(timestamp)
                    )
                '''.format(days))
                avg_daily = cursor.fetchone()[0] or 0
                
                return {
                    'total_liters': round(total_liters, 2),
                    'avg_daily': round(avg_daily, 2),
                    'zone_usage': zone_usage,
                    'period_days': days
                }
        except Exception as e:
            logger.error(f"Ошибка получения статистики воды: {e}")
            return {
                'total_liters': 0,
                'avg_daily': 0,
                'zone_usage': [],
                'period_days': days
            }

    # ===== Telegram bot helpers =====
    def get_bot_user_by_chat(self, chat_id: int) -> Optional[Dict[str, Any]]:
        try:
            with sqlite3.connect(self.db_path, timeout=5) as conn:
                conn.row_factory = sqlite3.Row
                cur = conn.execute('SELECT * FROM bot_users WHERE chat_id = ? LIMIT 1', (int(chat_id),))
                row = cur.fetchone()
                return dict(row) if row else None
        except Exception as e:
            logger.error(f"Ошибка чтения bot_user chat_id={chat_id}: {e}")
            return None

    def upsert_bot_user(self, chat_id: int, username: Optional[str], first_name: Optional[str]) -> bool:
        try:
            with sqlite3.connect(self.db_path, timeout=5) as conn:
                conn.execute('''
                    INSERT INTO bot_users(chat_id, username, first_name, created_at)
                    VALUES(?, ?, ?, CURRENT_TIMESTAMP)
                    ON CONFLICT(chat_id) DO UPDATE SET username=excluded.username, first_name=excluded.first_name, last_seen_at=CURRENT_TIMESTAMP
                ''', (int(chat_id), username, first_name))
                conn.commit()
                return True
        except Exception as e:
            logger.error(f"Ошибка upsert bot_user chat_id={chat_id}: {e}")
            return False

    def set_bot_user_authorized(self, chat_id: int, role: str = 'user') -> bool:
        try:
            with sqlite3.connect(self.db_path, timeout=5) as conn:
                conn.execute('UPDATE bot_users SET is_authorized=1, role=?, failed_attempts=0, locked_until=NULL, last_seen_at=CURRENT_TIMESTAMP WHERE chat_id=?', (str(role), int(chat_id)))
                conn.commit()
                return True
        except Exception as e:
            logger.error(f"Ошибка авторизации bot_user chat_id={chat_id}: {e}")
            return False

    def inc_bot_user_failed(self, chat_id: int) -> int:
        try:
            with sqlite3.connect(self.db_path, timeout=5) as conn:
                conn.execute('UPDATE bot_users SET failed_attempts=COALESCE(failed_attempts,0)+1, last_seen_at=CURRENT_TIMESTAMP WHERE chat_id=?', (int(chat_id),))
                conn.commit()
                cur = conn.execute('SELECT failed_attempts FROM bot_users WHERE chat_id=?', (int(chat_id),))
                row = cur.fetchone()
                return int(row[0]) if row else 0
        except Exception as e:
            logger.error(f"Ошибка инкремента failed_attempts chat_id={chat_id}: {e}")
            return 0

    def lock_bot_user_until(self, chat_id: int, until_iso: str) -> bool:
        try:
            with sqlite3.connect(self.db_path, timeout=5) as conn:
                conn.execute('UPDATE bot_users SET locked_until=? WHERE chat_id=?', (str(until_iso), int(chat_id)))
                conn.commit()
                return True
        except Exception as e:
            logger.error(f"Ошибка блокировки bot_user chat_id={chat_id}: {e}")
            return False

    def list_groups_min(self) -> List[Dict[str, Any]]:
        try:
            with sqlite3.connect(self.db_path, timeout=5) as conn:
                conn.row_factory = sqlite3.Row
                cur = conn.execute('SELECT id, name FROM groups ORDER BY id')
                return [dict(r) for r in cur.fetchall()]
        except Exception:
            return []

    def list_zones_by_group_min(self, group_id: int) -> List[Dict[str, Any]]:
        try:
            with sqlite3.connect(self.db_path, timeout=5) as conn:
                conn.row_factory = sqlite3.Row
                cur = conn.execute('SELECT id, name, duration, state FROM zones WHERE group_id=? ORDER BY id', (int(group_id),))
                return [dict(r) for r in cur.fetchall()]
        except Exception:
            return []

    def get_due_bot_subscriptions(self, now_local: datetime) -> List[Dict[str, Any]]:
        try:
            hhmm = now_local.strftime('%H:%M')
            dow = now_local.weekday()  # 0=Mon
            with sqlite3.connect(self.db_path, timeout=5) as conn:
                conn.row_factory = sqlite3.Row
                cur = conn.execute('''
                    SELECT bs.*, bu.chat_id FROM bot_subscriptions bs
                    JOIN bot_users bu ON bu.id = bs.user_id
                    WHERE bs.enabled=1 AND bs.time_local=?
                ''', (hhmm,))
                out = []
                for r in cur.fetchall():
                    rec = dict(r)
                    if str(rec.get('type')) == 'weekly':
                        mask = (rec.get('dow_mask') or '').strip()
                        if not mask:
                            continue
                        try:
                            ok = mask[dow] == '1'
                        except Exception:
                            ok = False
                        if not ok:
                            continue
                    out.append(rec)
                return out
        except Exception as e:
            logger.error(f"Ошибка получения due подписок: {e}")
            return []

    def create_or_update_subscription(self, user_id: int, sub_type: str, fmt: str, time_local: str, dow_mask: Optional[str], enabled: bool = True) -> bool:
        try:
            with sqlite3.connect(self.db_path, timeout=5) as conn:
                cur = conn.execute('SELECT id FROM bot_subscriptions WHERE user_id=? AND type=?', (int(user_id), str(sub_type)))
                row = cur.fetchone()
                if row:
                    conn.execute('UPDATE bot_subscriptions SET format=?, time_local=?, dow_mask=?, enabled=? WHERE id=?', (str(fmt), str(time_local), (dow_mask or ''), 1 if enabled else 0, int(row[0])))
                else:
                    conn.execute('INSERT INTO bot_subscriptions(user_id, type, format, time_local, dow_mask, enabled) VALUES(?,?,?,?,?,?)', (int(user_id), str(sub_type), str(fmt), str(time_local), (dow_mask or ''), 1 if enabled else 0))
                conn.commit()
                return True
        except Exception as e:
            logger.error(f"Ошибка сохранения подписки: {e}")
            return False

# Глобальный экземпляр базы данных
db = IrrigationDB()
