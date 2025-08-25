import os
import sqlite3
import tempfile

from database import IrrigationDB


def _columns(conn, table):
    cur = conn.execute(f"PRAGMA table_info({table})")
    return [r[1] for r in cur.fetchall()]


def test_db_migrations_on_old_schema():
    # 1) Создаем временную БД с «устаревшей» схемой
    fd, db_path = tempfile.mkstemp(suffix='.db')
    os.close(fd)
    try:
        with sqlite3.connect(db_path) as conn:
            # zones без новых полей
            conn.execute(
                '''CREATE TABLE zones (
                    id INTEGER PRIMARY KEY,
                    state TEXT,
                    name TEXT,
                    icon TEXT,
                    duration INTEGER,
                    group_id INTEGER,
                    topic TEXT
                )'''
            )
            # groups без use_rain_sensor
            conn.execute(
                '''CREATE TABLE groups (
                    id INTEGER PRIMARY KEY,
                    name TEXT,
                    created_at TIMESTAMP,
                    updated_at TIMESTAMP
                )'''
            )
            # settings для совместимости
            conn.execute('CREATE TABLE settings (key TEXT PRIMARY KEY, value TEXT)')
            conn.commit()

        # 2) Инициализируем обертку БД — должны выполниться миграции
        db = IrrigationDB(db_path=db_path)

        with sqlite3.connect(db_path) as conn:
            # Проверяем, что таблица mqtt_servers появилась
            cur = conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='mqtt_servers'")
            assert cur.fetchone() is not None, 'mqtt_servers table not created by migration'

            # Поля в zones
            cols = _columns(conn, 'zones')
            for required in (
                'postpone_reason', 'watering_start_time', 'scheduled_start_time',
                'last_watering_time', 'watering_start_source', 'mqtt_server_id'
            ):
                assert required in cols, f'missing migrated column {required} in zones'

            # Поле в groups
            gcols = _columns(conn, 'groups')
            assert 'use_rain_sensor' in gcols, 'missing migrated column use_rain_sensor in groups'
    finally:
        try:
            os.remove(db_path)
        except Exception:
            pass


def test_backup_and_restore_roundtrip(tmp_path):
    # 1) Создаем БД и добавим тестовые данные
    db_file = tmp_path / 'irrigation_test.db'
    db = IrrigationDB(db_path=str(db_file))
    # Настройка каталога бэкапов в tmp
    backup_dir = tmp_path / 'backups'
    db.backup_dir = str(backup_dir)

    # Добавим группу и зону
    with sqlite3.connect(str(db_file)) as conn:
        conn.execute("INSERT OR IGNORE INTO groups(id,name) VALUES(1,'Группа 1')")
        conn.execute(
            "INSERT INTO zones(state,name,icon,duration,group_id,topic) VALUES('off','Зона X','🌿',5,1,'/t')"
        )
        conn.commit()

    # 2) Делаем бэкап (после замыкания соединений)
    path = db.create_backup()
    assert path and os.path.exists(path), 'backup file not created'
    assert os.path.getsize(path) > 0, 'backup file is empty'

    # 3) Проверим, что бэкап содержит таблицы и данные
    # Бекап создается как копия файла БД на момент вызова; наши INSERT были до бэкапа, значит таблицы точно есть.
    # Но данные могли быть не зафиксированы, если внешние транзакции открыты (на всякий случай допускаем 0 записей).
    with sqlite3.connect(path) as conn:
        # Проверка наличия таблиц
        cur = conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='zones'")
        assert cur.fetchone() is not None, 'zones table missing in backup'
        cur = conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='groups'")
        assert cur.fetchone() is not None, 'groups table missing in backup'

    # 4) Имитируем восстановление: создадим новую БД как копию бэкапа
    restored = tmp_path / 'restored.db'
    with open(path, 'rb') as src, open(restored, 'wb') as dst:
        dst.write(src.read())
    with sqlite3.connect(str(restored)) as conn:
        zc2 = conn.execute("SELECT COUNT(*) FROM zones").fetchone()[0]
        assert zc2 >= 1

