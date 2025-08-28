import sqlite3
import json
import os
import shutil
from datetime import datetime, timedelta
from typing import List, Dict, Any, Optional
import logging
from werkzeug.security import generate_password_hash, check_password_hash

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
                self._migrate_days_format(conn)
                self._migrate_add_postpone_reason(conn)
                self._migrate_add_watering_start_time(conn)
                self._migrate_add_scheduled_start_time(conn)
                self._migrate_add_last_watering_time(conn)
                self._migrate_add_mqtt_servers(conn)
                self._migrate_add_zone_mqtt_server_id(conn)
                self._migrate_ensure_special_group(conn)
                self._migrate_add_zones_indexes(conn)
                self._migrate_add_group_rain_flag(conn)
                self._migrate_add_watering_start_source(conn)
                
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
                # Пароль по умолчанию 1234
                conn.execute('INSERT OR REPLACE INTO settings(key, value) VALUES (?, ?)', (
                    'password_hash', generate_password_hash('1234', method='pbkdf2:sha256')
                ))
                conn.commit()
                logger.info("Начальные данные вставлены: группы 1 (Насос-1) и 999 (БЕЗ ПОЛИВА)")
                
        except Exception as e:
            logger.error(f"Ошибка вставки начальных данных: {e}")

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

    def get_zones(self) -> List[Dict[str, Any]]:
        """Получить все зоны"""
        try:
            with sqlite3.connect(self.db_path, timeout=5) as conn:
                conn.row_factory = sqlite3.Row
                cursor = conn.execute('''
                    SELECT z.*, g.name as group_name 
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
        """Если пароль используется дефолтный (1234) либо пароль ещё не меняли, выставить флаг обязательной смены."""
        try:
            with sqlite3.connect(self.db_path) as conn:
                conn.row_factory = sqlite3.Row
                cur = conn.execute('SELECT value FROM settings WHERE key = ? LIMIT 1', ('password_hash',))
                row = cur.fetchone()
                if not row:
                    # Нет пароля — требуем смену
                    conn.execute('INSERT OR REPLACE INTO settings(key, value) VALUES (?, ?)', ('password_must_change', '1'))
                else:
                    # Если в базе ещё дефолтный хэш (грубая эвристика: допускаем, что 1234 был записан),
                    # всё равно форсируем смену при первом входе
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
    def get_mqtt_servers(self) -> List[Dict[str, Any]]:
        try:
            with sqlite3.connect(self.db_path) as conn:
                conn.row_factory = sqlite3.Row
                cur = conn.execute('SELECT * FROM mqtt_servers ORDER BY id')
                return [dict(row) for row in cur.fetchall()]
        except Exception as e:
            logger.error(f"Ошибка получения MQTT серверов: {e}")
            return []

    def create_mqtt_server(self, data: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        try:
            with sqlite3.connect(self.db_path) as conn:
                cur = conn.execute('''
                    INSERT INTO mqtt_servers (name, host, port, username, password, client_id, enabled)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                ''', (
                    data.get('name', 'MQTT'),
                    data.get('host', 'localhost'),
                    int(data.get('port', 1883)),
                    data.get('username'),
                    data.get('password'),
                    data.get('client_id'),
                    1 if data.get('enabled', True) else 0
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
                return dict(row) if row else None
        except Exception as e:
            logger.error(f"Ошибка получения MQTT сервера {server_id}: {e}")
            return None

    def update_mqtt_server(self, server_id: int, data: Dict[str, Any]) -> bool:
        try:
            with sqlite3.connect(self.db_path) as conn:
                conn.execute('''
                    UPDATE mqtt_servers
                    SET name = ?, host = ?, port = ?, username = ?, password = ?, client_id = ?, enabled = ?, updated_at = CURRENT_TIMESTAMP
                    WHERE id = ?
                ''', (
                    data.get('name', 'MQTT'),
                    data.get('host', 'localhost'),
                    int(data.get('port', 1883)),
                    data.get('username'),
                    data.get('password'),
                    data.get('client_id'),
                    1 if data.get('enabled', True) else 0,
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
                query = 'SELECT * FROM logs WHERE 1=1'
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

                # Получаем суммарную продолжительность полива для выбранных зон
                # Зоны поливаются последовательно, поэтому суммируем их длительности
                total_duration = 0
                for zone_id in zones:
                    duration = self.get_zone_duration(zone_id)
                    total_duration += duration
                
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
                    zones_groups = set()
                    existing_zones_groups = set()
                    
                    # Получаем группы для зон новой программы
                    for zone_id in zones:
                        zone = self.get_zone(zone_id)
                        if zone:
                            zones_groups.add(zone['group_id'])
                    
                    # Получаем группы для зон существующей программы
                    for zone_id in program_data['zones']:
                        zone = self.get_zone(zone_id)
                        if zone:
                            existing_zones_groups.add(zone['group_id'])
                    
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
                    existing_total_duration = 0
                    for zone_id in program_data['zones']:
                        duration = self.get_zone_duration(zone_id)
                        existing_total_duration += duration
                    
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

# Глобальный экземпляр базы данных
db = IrrigationDB()
