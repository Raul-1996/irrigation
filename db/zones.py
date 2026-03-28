import sqlite3
import json
import logging
from typing import List, Dict, Any, Optional
from datetime import datetime, timedelta

from db.base import BaseRepository, retry_on_busy

logger = logging.getLogger(__name__)


class ZoneRepository(BaseRepository):
    """Repository for zone CRUD, bulk operations, and zone_runs."""

    def get_zones(self) -> List[Dict[str, Any]]:
        """Получить все зоны."""
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
                    zone['group'] = zone['group_id']
                    zones.append(zone)
                return zones
        except sqlite3.Error as e:
            logger.error("Ошибка получения зон: %s", e)
            return []

    def get_zone(self, zone_id: int) -> Optional[Dict[str, Any]]:
        """Получить зону по ID."""
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
        except sqlite3.Error as e:
            logger.error("Ошибка получения зоны %s: %s", zone_id, e)
            return None

    @retry_on_busy()
    def create_zone(self, zone_data: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """Создать новую зону."""
        try:
            with sqlite3.connect(self.db_path, timeout=5) as conn:
                topic = (zone_data.get('topic') or '').strip()
                zid_explicit = None
                try:
                    zid_explicit = int(zone_data.get('id')) if zone_data.get('id') is not None else None
                except (TypeError, ValueError):
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
                    except sqlite3.Error:
                        logger.warning("Не удалось вставить зону с явным id=%s, пробуем без id", zid_explicit)

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
        except sqlite3.Error as e:
            logger.error("Ошибка создания зоны: %s", e)
            return None

    @retry_on_busy()
    def update_zone(self, zone_id: int, zone_data: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """Обновить зону."""
        try:
            with sqlite3.connect(self.db_path, timeout=5) as conn:
                current_zone = self.get_zone(zone_id)
                if not current_zone:
                    return None

                updated_data = current_zone.copy()
                updated_data.update(zone_data)

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
                if 'watering_start_time' in updated_data:
                    sql_fields.append('watering_start_time = ?')
                    params.append(updated_data['watering_start_time'])
                if 'scheduled_start_time' in updated_data:
                    sql_fields.append('scheduled_start_time = ?')
                    params.append(updated_data['scheduled_start_time'])
                if 'last_watering_time' in updated_data:
                    sql_fields.append('last_watering_time = ?')
                    params.append(updated_data['last_watering_time'])
                if 'last_avg_flow_lpm' in updated_data:
                    sql_fields.append('last_avg_flow_lpm = ?')
                    params.append(updated_data['last_avg_flow_lpm'])
                if 'last_total_liters' in updated_data:
                    sql_fields.append('last_total_liters = ?')
                    params.append(updated_data['last_total_liters'])
                if 'mqtt_server_id' in updated_data:
                    sql_fields.append('mqtt_server_id = ?')
                    params.append(updated_data.get('mqtt_server_id'))

                sql_fields.append('updated_at = CURRENT_TIMESTAMP')
                params.append(zone_id)

                sql = f'''
                    UPDATE zones 
                    SET {', '.join(sql_fields)}
                    WHERE id = ?
                '''
                conn.execute(sql, params)

                # Если зону переводят в группу 999 — исключаем из всех программ
                target_group_id = updated_data.get('group_id', updated_data.get('group'))
                if target_group_id == 999:
                    cursor = conn.execute('SELECT id, zones FROM programs')
                    for row in cursor.fetchall():
                        try:
                            zones_list = json.loads(row[1])
                        except (json.JSONDecodeError, TypeError):
                            continue
                        if zone_id in zones_list:
                            zones_list = [z for z in zones_list if z != zone_id]
                            conn.execute('UPDATE programs SET zones = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?',
                                         (json.dumps(zones_list), row[0]))

                conn.commit()
                return self.get_zone(zone_id)
        except sqlite3.Error as e:
            logger.error("Ошибка обновления зоны %s: %s", zone_id, e)
            return None

    @retry_on_busy()
    def update_zone_versioned(self, zone_id: int, updates: Dict[str, Any]) -> bool:
        """Обновить зону с инкрементом version (optimistic lock)."""
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
        except sqlite3.Error as e:
            logger.error("Ошибка versioned-обновления зоны %s: %s", zone_id, e)
            return False

    @retry_on_busy()
    def bulk_update_zones(self, updates: List[Dict[str, Any]]) -> Dict[str, Any]:
        """Пакетное обновление зон в одной транзакции."""
        updated = 0
        failed: List[int] = []
        if not updates:
            return {'updated': 0, 'failed': []}
        try:
            with sqlite3.connect(self.db_path, timeout=5) as conn:
                for upd in updates:
                    try:
                        zone_id = int(upd.get('id'))
                    except (TypeError, ValueError):
                        continue
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
                        fields.append(f"{field} = ?")
                        params.append(value)

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
                    if 'last_avg_flow_lpm' in merged: add('last_avg_flow_lpm', merged['last_avg_flow_lpm'])
                    if 'last_total_liters' in merged: add('last_total_liters', merged['last_total_liters'])
                    if 'mqtt_server_id' in merged: add('mqtt_server_id', merged.get('mqtt_server_id'))
                    fields.append('updated_at = CURRENT_TIMESTAMP')
                    params.append(zone_id)
                    sql = f"UPDATE zones SET {', '.join(fields)} WHERE id = ?"
                    try:
                        conn.execute(sql, params)
                        updated += 1
                    except sqlite3.Error as e:
                        logger.warning("Ошибка обновления зоны %s в bulk: %s", zone_id, e)
                        failed.append(zone_id)
                conn.commit()
            return {'updated': updated, 'failed': failed}
        except sqlite3.Error as e:
            logger.error("Ошибка bulk-обновления зон: %s", e)
            return {'updated': updated, 'failed': failed or []}

    @retry_on_busy()
    def bulk_upsert_zones(self, zones: List[Dict[str, Any]]) -> Dict[str, Any]:
        """Импорт зон: upsert множества зон в одной транзакции."""
        created = 0
        updated = 0
        failed = 0
        if not zones:
            return {'created': 0, 'updated': 0, 'failed': 0}
        try:
            with sqlite3.connect(self.db_path, timeout=10) as conn:
                for z in zones:
                    try:
                        zid = int(z['id']) if z.get('id') is not None else None
                    except (TypeError, ValueError):
                        zid = None
                    try:
                        if zid is not None:
                            cur = conn.execute('SELECT id FROM zones WHERE id = ?', (zid,))
                            row = cur.fetchone()
                            if row:
                                fields = []
                                params = []

                                def add(field: str, value):
                                    fields.append(f"{field} = ?")
                                    params.append(value)

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
                    except sqlite3.Error as e:
                        logger.warning("Ошибка upsert зоны: %s", e)
                        failed += 1
                conn.commit()
            return {'created': created, 'updated': updated, 'failed': failed}
        except sqlite3.Error as e:
            logger.error("Ошибка bulk-импорта зон: %s", e)
            return {'created': created, 'updated': updated, 'failed': (failed or 0)}

    @retry_on_busy()
    def delete_zone(self, zone_id: int) -> bool:
        """Удалить зону."""
        try:
            with sqlite3.connect(self.db_path, timeout=5) as conn:
                conn.execute('DELETE FROM zones WHERE id = ?', (zone_id,))
                conn.commit()
                return True
        except sqlite3.Error as e:
            logger.error("Ошибка удаления зоны %s: %s", zone_id, e)
            return False

    def get_zones_by_group(self, group_id: int) -> List[Dict[str, Any]]:
        """Получить зоны по группе."""
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
                    zone['group'] = zone['group_id']
                    zones.append(zone)
                return zones
        except sqlite3.Error as e:
            logger.error("Ошибка получения зон группы %s: %s", group_id, e)
            return []

    @retry_on_busy()
    def clear_group_scheduled_starts(self, group_id: int) -> None:
        """Очистить плановые времена старта у всех зон в группе."""
        try:
            with sqlite3.connect(self.db_path) as conn:
                conn.execute('''
                    UPDATE zones
                    SET scheduled_start_time = NULL, updated_at = CURRENT_TIMESTAMP
                    WHERE group_id = ?
                ''', (group_id,))
                conn.commit()
        except sqlite3.Error as e:
            logger.error("Ошибка очистки scheduled_start_time в группе %s: %s", group_id, e)

    @retry_on_busy()
    def set_group_scheduled_starts(self, group_id: int, schedule: Dict[int, str]) -> None:
        """Установить плановые времена старта по зоне в группе."""
        try:
            with sqlite3.connect(self.db_path) as conn:
                for zone_id, ts in schedule.items():
                    conn.execute('''
                        UPDATE zones
                        SET scheduled_start_time = ?, updated_at = CURRENT_TIMESTAMP
                        WHERE id = ? AND group_id = ?
                    ''', (ts, zone_id, group_id))
                conn.commit()
        except sqlite3.Error as e:
            logger.error("Ошибка установки расписания scheduled_start_time для группы %s: %s", group_id, e)

    @retry_on_busy()
    def clear_scheduled_for_zone_group_peers(self, zone_id: int, group_id: int) -> None:
        """Очистить scheduled_start_time у всех зон группы, кроме указанной."""
        try:
            with sqlite3.connect(self.db_path) as conn:
                conn.execute('''
                    UPDATE zones
                    SET scheduled_start_time = NULL, updated_at = CURRENT_TIMESTAMP
                    WHERE group_id = ? AND id != ?
                ''', (group_id, zone_id))
                conn.commit()
        except sqlite3.Error as e:
            logger.error("Ошибка очистки расписания у одногруппных зон для зоны %s: %s", zone_id, e)

    @retry_on_busy()
    def update_zone_postpone(self, zone_id: int, postpone_until: str = None, reason: str = None) -> bool:
        """Обновить отложенный полив зоны с указанием причины."""
        try:
            with sqlite3.connect(self.db_path) as conn:
                conn.execute('''
                    UPDATE zones 
                    SET postpone_until = ?, postpone_reason = ?, updated_at = CURRENT_TIMESTAMP
                    WHERE id = ?
                ''', (postpone_until, reason, zone_id))
                conn.commit()
                return True
        except sqlite3.Error as e:
            logger.error("Ошибка обновления отложенного полива зоны %s: %s", zone_id, e)
            return False

    @retry_on_busy()
    def update_zone_photo(self, zone_id: int, photo_path: Optional[str]) -> bool:
        """Обновить фотографию зоны."""
        try:
            with sqlite3.connect(self.db_path) as conn:
                conn.execute('''
                    UPDATE zones 
                    SET photo_path = ?, updated_at = CURRENT_TIMESTAMP
                    WHERE id = ?
                ''', (photo_path, zone_id))
                conn.commit()
                return True
        except sqlite3.Error as e:
            logger.error("Ошибка обновления фото зоны %s: %s", zone_id, e)
            return False

    def get_zone_duration(self, zone_id: int) -> int:
        """Получить продолжительность полива зоны."""
        try:
            with sqlite3.connect(self.db_path) as conn:
                cursor = conn.execute('SELECT duration FROM zones WHERE id = ?', (zone_id,))
                result = cursor.fetchone()
                return result[0] if result else 0
        except sqlite3.Error as e:
            logger.error("Ошибка получения продолжительности зоны %s: %s", zone_id, e)
            return 0

    # --- Zone runs ---
    @retry_on_busy()
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
        except sqlite3.Error as e:
            logger.error("Ошибка создания zone_run для зоны %s: %s", zone_id, e)
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
        except sqlite3.Error as e:
            logger.error("Ошибка чтения открытого run для зоны %s: %s", zone_id, e)
            return None

    @retry_on_busy()
    def finish_zone_run(self, run_id: int, end_utc: str, end_monotonic: float, end_raw_pulses: Optional[int],
                        total_liters: Optional[float], avg_flow_lpm: Optional[float], status: str = 'ok') -> bool:
        try:
            with sqlite3.connect(self.db_path, timeout=5) as conn:
                fields = ['end_utc = ?', 'end_monotonic = ?', 'status = ?', 'updated_at = CURRENT_TIMESTAMP']
                params: list = [str(end_utc), float(end_monotonic), str(status)]
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
        except sqlite3.Error as e:
            logger.error("Ошибка завершения zone_run %s: %s", run_id, e)
            return False

    def compute_next_run_for_zone(self, zone_id: int, programs_getter=None) -> Optional[str]:
        """Рассчитать ближайшее будущее время запуска зоны по всем программам.
        programs_getter: callable that returns list of programs (injected from facade).
        """
        try:
            zone = self.get_zone(zone_id)
            if not zone:
                return None
            programs = programs_getter() if programs_getter else []
            if not programs:
                return None
            now = datetime.now()
            best_dt: Optional[datetime] = None
            for prog in programs:
                if zone_id not in prog.get('zones', []):
                    continue
                for offset in range(0, 14):
                    dt_candidate = now + timedelta(days=offset)
                    if dt_candidate.weekday() in prog['days']:
                        hour, minute = map(int, prog['time'].split(':'))
                        start_dt = dt_candidate.replace(hour=hour, minute=minute, second=0, microsecond=0)
                        if start_dt <= now:
                            continue
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
            logger.exception("Ошибка расчета следующего запуска для зоны %s: %s", zone_id, e)
            return None

    def reschedule_group_to_next_program(self, group_id: int, programs_getter=None) -> None:
        """Пересчитать и записать scheduled_start_time всем зонам группы."""
        try:
            zones = self.get_zones_by_group(group_id)
            schedule: Dict[int, str] = {}
            for z in zones:
                nxt = self.compute_next_run_for_zone(z['id'], programs_getter=programs_getter)
                if nxt:
                    schedule[z['id']] = nxt
            self.clear_group_scheduled_starts(group_id)
            if schedule:
                self.set_group_scheduled_starts(group_id, schedule)
        except Exception as e:
            logger.exception("Ошибка перестройки расписания группы %s: %s", group_id, e)
