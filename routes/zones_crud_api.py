"""Zones CRUD API — create, read, update, delete, import, next-watering, duration-conflicts."""
from flask import Blueprint, request, jsonify
from datetime import datetime, timedelta
import json
import logging

from database import db
from services.helpers import parse_dt
import sqlite3

logger = logging.getLogger(__name__)

zones_crud_api_bp = Blueprint('zones_crud_api', __name__)


# ---- Zone CRUD ----

@zones_crud_api_bp.route('/api/zones')
def api_zones():
    zones = db.get_zones()
    return jsonify(zones)


@zones_crud_api_bp.route('/api/zones/<int:zone_id>', methods=['GET', 'PUT', 'DELETE'])
def api_zone(zone_id):
    if request.method == 'GET':
        zone = db.get_zone(zone_id)
        if zone:
            return jsonify(zone)
        return jsonify({'success': False, 'message': 'Zone not found'}), 404

    elif request.method == 'PUT':
        data = request.get_json() or {}
        try:
            if 'duration' in data:
                d = int(data['duration'])
                if d < 1 or d > 3600:
                    return jsonify({'success': False, 'message': 'duration must be 1..3600'}), 400
            if 'name' in data and (not str(data['name']).strip()):
                return jsonify({'success': False, 'message': 'name must be non-empty'}), 400
        except (ValueError, TypeError, KeyError) as e:
            logger.debug("Exception in api_zone: %s", e)
            return jsonify({'success': False, 'message': 'invalid zone payload'}), 400
        try:
            is_csv = (request.headers.get('X-Import-Op') == 'csv') or (request.args.get('source') == 'csv')
        except (KeyError, TypeError, ValueError) as e:
            logger.debug("Exception in api_zone: %s", e)
            is_csv = False
        if is_csv:
            try:
                logging.getLogger('import_export').info(f"PUT zone from CSV id={zone_id} payload={json.dumps(data, ensure_ascii=False)}")
            except (json.JSONDecodeError, KeyError, TypeError, ValueError) as e:
                logger.debug("Handled exception in api_zone: %s", e)
        zone = db.update_zone(zone_id, data)
        if zone:
            if is_csv:
                try:
                    logging.getLogger('import_export').info(f"PUT result id={zone_id} OK")
                except (OSError, ValueError) as e:
                    logger.debug("Handled exception in line_128: %s", e)
            db.add_log('zone_edit', json.dumps({"zone": zone_id, "changes": data}))
            return jsonify(zone)
        if is_csv:
            try:
                logging.getLogger('import_export').info(f"PUT result id={zone_id} NOT_FOUND")
            except (OSError, ValueError) as e:
                logger.debug("Handled exception in line_135: %s", e)
        return ('Zone not found', 404)

    elif request.method == 'DELETE':
        if db.delete_zone(zone_id):
            db.add_log('zone_delete', json.dumps({"zone": zone_id}))
            return ('', 204)
        return ('Zone not found', 404)


@zones_crud_api_bp.route('/api/zones', methods=['POST'])
def api_create_zone():
    data = request.get_json() or {}
    try:
        name = str(data.get('name') or 'Зона').strip()
        duration = int(data.get('duration') or 10)
        if duration < 1 or duration > 3600:
            return jsonify({'success': False, 'message': 'duration must be 1..3600'}), 400
        if not name:
            return jsonify({'success': False, 'message': 'name must be non-empty'}), 400
    except (ValueError, TypeError, KeyError) as e:
        logger.debug("Exception in api_create_zone: %s", e)
        return jsonify({'success': False, 'message': 'invalid zone payload'}), 400
    try:
        is_csv = (request.headers.get('X-Import-Op') == 'csv') or (request.args.get('source') == 'csv')
    except (KeyError, TypeError, ValueError) as e:
        logger.debug("Exception in api_create_zone: %s", e)
        is_csv = False
    if is_csv:
        try:
            logging.getLogger('import_export').info(f"POST create zone from CSV payload={json.dumps(data, ensure_ascii=False)}")
        except (json.JSONDecodeError, KeyError, TypeError, ValueError) as e:
            logger.debug("Handled exception in api_create_zone: %s", e)
    zone = db.create_zone(data)
    if zone and zone.get('mqtt_server_id') is None:
        # Zone created but no MQTT server assigned — warn caller
        db.add_log('zone_create', json.dumps({"zone": zone['id'], "name": zone['name'], "warning": "mqtt_server_id is NULL"}))
        return jsonify({
            'success': True,
            'warning': 'MQTT-сервер не выбран. Выберите сервер в настройках зоны для управления реле.',
            'zone': zone
        }), 201
    if zone:
        db.add_log('zone_create', json.dumps({"zone": zone['id'], "name": zone['name']}))
        if is_csv:
            try:
                logging.getLogger('import_export').info(f"POST result id={zone.get('id')} OK")
            except (KeyError, TypeError, ValueError) as e:
                logger.debug("Handled exception in api_create_zone: %s", e)
        return jsonify(zone), 201
    if is_csv:
        try:
            logging.getLogger('import_export').info("POST result ERROR")
        except (OSError, ValueError) as e:
            logger.debug("Handled exception in line_181: %s", e)
    return ('Error creating zone', 400)


@zones_crud_api_bp.route('/api/zones/import', methods=['POST'])
def api_import_zones_bulk():
    """Import/bulk apply zone changes in one transaction."""
    try:
        body = request.get_json(silent=True) or {}
        zones = body.get('zones') or []
        if not isinstance(zones, list) or not zones:
            return jsonify({'success': False, 'message': 'Нет данных для импорта'}), 400
        stats = db.bulk_upsert_zones(zones)
        try:
            db.add_log('zones_import', json.dumps({'counts': stats}))
        except (json.JSONDecodeError, KeyError, TypeError, ValueError) as e:
            logger.debug("Handled exception in api_import_zones_bulk: %s", e)
        return jsonify({'success': True, **stats})
    except (json.JSONDecodeError, KeyError, TypeError, ValueError) as e:
        logger.error(f"Ошибка импорта зон: {e}")
        return jsonify({'success': False, 'message': 'Ошибка импорта'}), 500


# ---- Next watering ----

@zones_crud_api_bp.route('/api/zones/<int:zone_id>/next-watering')
def api_zone_next_watering(zone_id):
    """API для получения времени следующего полива зоны"""
    try:
        zone = db.get_zone(zone_id)
        if not zone:
            return jsonify({'error': 'Зона не найдена'}), 404

        programs = db.get_programs()
        zone_programs = []

        for program in programs:
            if isinstance(program['zones'], str):
                program_zones = json.loads(program['zones'])
            else:
                program_zones = program['zones']
            if zone_id in program_zones:
                zone_programs.append(program)

        if not zone_programs:
            return jsonify({
                'zone_id': zone_id,
                'next_watering': 'Никогда',
                'reason': 'Зона не включена ни в одну программу'
            })

        now = datetime.now()
        try:
            pu = zone.get('postpone_until')
            if pu:
                pu_dt = parse_dt(pu)
                if pu_dt and pu_dt > now:
                    now = pu_dt
        except (KeyError, TypeError, ValueError) as e:
            logger.debug("Handled exception in line_240: %s", e)
        best_dt = None
        best_payload = None

        for program in zone_programs:
            program_time = datetime.strptime(program['time'], '%H:%M').time()
            prog_weekdays = set(int(d) for d in program['days'])
            program_zones = program['zones'] if isinstance(program['zones'], list) else json.loads(program['zones'])
            program_zones.sort()
            zone_position = program_zones.index(zone_id)

            total_duration_before = 0
            for i in range(zone_position):
                prev_zone_id = program_zones[i]
                prev_zone = db.get_zone(prev_zone_id)
                if prev_zone:
                    total_duration_before += prev_zone['duration']

            for add_days in range(0, 14):
                day_date = now.date() + timedelta(days=add_days)
                if prog_weekdays and ((day_date.weekday() + 0) % 7) not in prog_weekdays:
                    continue
                zone_start_minutes = program_time.hour * 60 + program_time.minute + total_duration_before
                zone_dt = datetime.combine(day_date, datetime.min.time()) + timedelta(minutes=zone_start_minutes)
                if zone_dt > now:
                    try:
                        gid = int((zone or {}).get('group_id') or 0)
                        if gid and day_date == datetime.now().date():
                            run_date = datetime.now().strftime('%Y-%m-%d')
                            if db.is_program_run_cancelled_for_group(int(program['id']), run_date, gid):
                                continue
                    except (sqlite3.Error, OSError) as e:
                        logger.debug("Handled exception in line_272: %s", e)
                    if best_dt is None or zone_dt < best_dt:
                        best_dt = zone_dt
                        best_payload = {
                            'zone_id': zone_id,
                            'next_watering': zone_dt.strftime('%H:%M'),
                            'next_datetime': zone_dt.strftime('%Y-%m-%d %H:%M'),
                            'program_name': program['name'],
                            'program_time': program['time'],
                            'zone_position': zone_position + 1,
                            'total_zones_in_program': len(program_zones)
                        }
                    break

        if best_payload is None:
            return jsonify({'zone_id': zone_id, 'next_watering': 'Никогда'})
        return jsonify(best_payload)

    except (sqlite3.Error, OSError) as e:
        logger.error(f"Ошибка получения времени следующего полива для зоны {zone_id}: {e}")
        return jsonify({'error': 'Ошибка получения времени полива'}), 500


@zones_crud_api_bp.route('/api/zones/next-watering-bulk', methods=['POST'])
def api_zones_next_watering_bulk():
    try:
        data = request.get_json(silent=True) or {}
        zone_ids = data.get('zone_ids')
        all_zones = db.get_zones() or []
        if not zone_ids:
            zone_ids = [int(z.get('id')) for z in all_zones if int(z.get('group_id') or z.get('group') or 0) != 999]
        zone_ids = [int(z) for z in zone_ids]
        duration_by_zone = {int(z['id']): int(z.get('duration') or 0) for z in all_zones}
        programs = db.get_programs() or []
        offset_map_per_program = []
        for p in programs:
            try:
                zones_list = sorted([int(x) for x in (p.get('zones') or [])])
            except (ValueError, TypeError, KeyError) as e:
                logger.debug("Exception in api_zones_next_watering_bulk: %s", e)
                zones_list = []
            offsets = {}
            cum = 0
            for zid in zones_list:
                offsets[zid] = cum
                cum += int(duration_by_zone.get(zid, 0))
            offset_map_per_program.append({'prog': p, 'offsets': offsets})
        now = datetime.now()
        prog_info = {}
        for pm in offset_map_per_program:
            p = pm['prog']
            try:
                hh, mm = [int(x) for x in str(p.get('time') or '00:00').split(':', 1)]
            except (ValueError, TypeError, KeyError) as e:
                logger.debug("Exception in api_zones_next_watering_bulk: %s", e)
                hh, mm = 0, 0
            best = None
            days = p.get('days') or []
            today_start = None
            if now.weekday() in days:
                today_start = now.replace(hour=hh, minute=mm, second=0, microsecond=0)
            try:
                zones_list = p.get('zones') or []
                total_prog_min = sum(int(duration_by_zone.get(int(zid), 0)) for zid in zones_list)
            except (ValueError, TypeError, KeyError) as e:
                logger.debug("Exception in line_337: %s", e)
                total_prog_min = 0
            in_progress = False
            elapsed_min = 0
            if today_start and today_start <= now and total_prog_min > 0:
                today_end = today_start + timedelta(minutes=total_prog_min)
                if now < today_end:
                    in_progress = True
                    elapsed_min = int((now - today_start).total_seconds() // 60)
            for off in range(0, 14):
                d = now + timedelta(days=off)
                if d.weekday() in days:
                    cand = d.replace(hour=hh, minute=mm, second=0, microsecond=0)
                    if cand <= now:
                        continue
                    best = cand
                    break
            prog_info[p.get('id')] = {
                'next_start': best,
                'today_start': today_start,
                'in_progress': in_progress,
                'elapsed_min': elapsed_min
            }
        items = []
        for zid in zone_ids:
            best_dt = None
            for pm in offset_map_per_program:
                p = pm['prog']; offsets = pm['offsets']
                if zid not in offsets:
                    continue
                pinfo = prog_info.get(p.get('id')) or {}
                cancelled_today = False
                try:
                    zinfo = next((zz for zz in all_zones if int(zz.get('id')) == int(zid)), None)
                    gid = int(zinfo.get('group_id') or 0) if zinfo else 0
                    if gid and pinfo.get('today_start'):
                        run_date = pinfo['today_start'].strftime('%Y-%m-%d')
                        cancelled_today = db.is_program_run_cancelled_for_group(int(p.get('id')), run_date, gid)
                except (sqlite3.Error, OSError) as e:
                    logger.debug("Exception in line_376: %s", e)
                    cancelled_today = False

                if pinfo.get('in_progress') and pinfo.get('today_start') and not cancelled_today:
                    off_min = int(offsets.get(zid, 0))
                    if off_min >= int(pinfo.get('elapsed_min') or 0):
                        cand = pinfo['today_start'] + timedelta(minutes=off_min)
                    else:
                        start_dt = pinfo.get('next_start')
                        if not start_dt:
                            continue
                        cand = start_dt + timedelta(minutes=off_min)
                else:
                    start_dt = pinfo.get('next_start')
                    if not start_dt:
                        continue
                    cand = start_dt + timedelta(minutes=int(offsets.get(zid, 0)))
                    try:
                        if p.get('id') is not None and pinfo.get('today_start') and cancelled_today:
                            hh, mm = map(int, str(p.get('time') or '00:00').split(':', 1))
                            ns = None
                            for off in range(1, 15):
                                d = now + timedelta(days=off)
                                if d.weekday() in (p.get('days') or []):
                                    ns = d.replace(hour=hh, minute=mm, second=0, microsecond=0)
                                    break
                            if ns:
                                cand = ns + timedelta(minutes=int(offsets.get(zid, 0)))
                    except (ValueError, TypeError, KeyError) as e:
                        logger.debug("Handled exception in line_405: %s", e)
                if cand <= now:
                    continue
                if best_dt is None or cand < best_dt:
                    best_dt = cand
            items.append({
                'zone_id': int(zid),
                'next_datetime': best_dt.strftime('%Y-%m-%d %H:%M:%S') if best_dt else None,
                'next_watering': 'Никогда' if best_dt is None else best_dt.strftime('%Y-%m-%d %H:%M:%S')
            })
        return jsonify({'success': True, 'items': items})
    except (ValueError, TypeError, KeyError) as e:
        logger.error(f"bulk next-watering failed: {e}")
        return jsonify({'success': False}), 500


# ---- Duration conflict checks ----

@zones_crud_api_bp.route('/api/zones/check-duration-conflicts', methods=['POST'])
def api_check_zone_duration_conflicts():
    """Check program conflicts when changing a zone's duration."""
    try:
        data = request.get_json() or {}
        zone_id = data.get('zone_id')
        new_duration = data.get('new_duration')

        if not isinstance(zone_id, int) or not isinstance(new_duration, int):
            return jsonify({'success': False, 'message': 'Некорректные параметры'}), 400

        zone = db.get_zone(zone_id)
        if not zone:
            return jsonify({'success': False, 'message': 'Зона не найдена'}), 404

        programs = db.get_programs()
        conflicts = []

        def get_zone_group(zid: int):
            z = db.get_zone(zid)
            return z['group_id'] if z else None

        for program in programs:
            prog_days = program['days'] if isinstance(program['days'], list) else json.loads(program['days'])
            prog_zones = program['zones'] if isinstance(program['zones'], list) else json.loads(program['zones'])
            if zone_id not in prog_zones:
                continue
            try:
                p_hour, p_min = map(int, program['time'].split(':'))
            except (ValueError, TypeError, KeyError) as e:
                logger.debug("Exception in get_zone_group: %s", e)
                continue
            start_a = p_hour * 60 + p_min
            total_duration_a = 0
            for zid in prog_zones:
                if zid == zone_id:
                    total_duration_a += int(new_duration)
                else:
                    total_duration_a += int(db.get_zone_duration(zid))
            end_a = start_a + total_duration_a
            groups_a = set(filter(lambda g: g is not None, [get_zone_group(zid) for zid in prog_zones]))

            for other in programs:
                if other['id'] == program['id']:
                    continue
                other_days = other['days'] if isinstance(other['days'], list) else json.loads(other['days'])
                common_days = set(prog_days) & set(other_days)
                if not common_days:
                    continue
                other_zones = other['zones'] if isinstance(other['zones'], list) else json.loads(other['zones'])
                common_zones = set(prog_zones) & set(other_zones)
                groups_b = set(filter(lambda g: g is not None, [get_zone_group(zid) for zid in other_zones]))
                common_groups = groups_a & groups_b
                if not common_zones and not common_groups:
                    continue
                try:
                    oh, om = map(int, other['time'].split(':'))
                except (ValueError, TypeError, KeyError) as e:
                    logger.debug("Exception in line_481: %s", e)
                    continue
                start_b = oh * 60 + om
                total_duration_b = 0
                for zid in other_zones:
                    total_duration_b += int(db.get_zone_duration(zid))
                end_b = start_b + total_duration_b
                if start_a < end_b and end_a > start_b:
                    conflicts.append({
                        'checked_program_id': program['id'],
                        'checked_program_name': program['name'],
                        'checked_program_time': program['time'],
                        'other_program_id': other['id'],
                        'other_program_name': other['name'],
                        'other_program_time': other['time'],
                        'common_zones': list(common_zones),
                        'common_groups': list(common_groups),
                        'overlap_start': max(start_a, start_b),
                        'overlap_end': min(end_a, end_b)
                    })

        return jsonify({'success': True, 'has_conflicts': len(conflicts) > 0, 'conflicts': conflicts})
    except (sqlite3.Error, OSError) as e:
        logger.error(f"Ошибка проверки конфликтов длительности зоны: {e}")
        return jsonify({'success': False, 'message': 'Ошибка проверки конфликтов'}), 500


@zones_crud_api_bp.route('/api/zones/check-duration-conflicts-bulk', methods=['POST'])
def api_check_zone_duration_conflicts_bulk():
    """Bulk duration conflict check for multiple zones."""
    try:
        payload = request.get_json() or {}
        changes = payload.get('changes') or []
        normalized = []
        for ch in changes:
            try:
                zid = int(ch.get('zone_id'))
                dur = int(ch.get('new_duration'))
                normalized.append((zid, dur))
            except (ValueError, TypeError, KeyError) as e:
                logger.debug("Exception in api_check_zone_duration_conflicts_bulk: %s", e)
                continue
        if not normalized:
            return jsonify({'success': False, 'message': 'Нет валидных изменений'}), 400

        all_programs = db.get_programs()
        zones_cache = {z['id']: z for z in db.get_zones()}

        def get_zone_group(zid: int):
            z = zones_cache.get(zid)
            return z['group_id'] if z else None

        def get_zone_duration(zid: int):
            z = zones_cache.get(zid)
            if not z:
                return 0
            try:
                return int(z.get('duration') or 0)
            except (ValueError, TypeError, KeyError) as e:
                logger.debug("Exception in get_zone_duration: %s", e)
                return 0

        results = {}
        for (zone_id, new_duration) in normalized:
            conflicts = []
            for program in all_programs:
                prog_days = program['days'] if isinstance(program['days'], list) else json.loads(program['days'])
                prog_zones = program['zones'] if isinstance(program['zones'], list) else json.loads(program['zones'])
                if zone_id not in prog_zones:
                    continue
                try:
                    p_hour, p_min = map(int, program['time'].split(':'))
                except (ValueError, TypeError, KeyError) as e:
                    logger.debug("Exception in get_zone_duration: %s", e)
                    continue
                start_a = p_hour * 60 + p_min
                total_duration_a = 0
                for zid in prog_zones:
                    total_duration_a += new_duration if zid == zone_id else get_zone_duration(zid)
                end_a = start_a + total_duration_a
                groups_a = set(filter(lambda g: g is not None, [get_zone_group(zid) for zid in prog_zones]))
                for other in all_programs:
                    if other['id'] == program['id']:
                        continue
                    other_days = other['days'] if isinstance(other['days'], list) else json.loads(other['days'])
                    if not (set(prog_days) & set(other_days)):
                        continue
                    other_zones = other['zones'] if isinstance(other['zones'], list) else json.loads(other['zones'])
                    common_zones = set(prog_zones) & set(other_zones)
                    groups_b = set(filter(lambda g: g is not None, [get_zone_group(zid) for zid in other_zones]))
                    if not common_zones and not (groups_a & groups_b):
                        continue
                    try:
                        oh, om = map(int, other['time'].split(':'))
                    except (ValueError, TypeError, KeyError) as e:
                        logger.debug("Exception in line_576: %s", e)
                        continue
                    start_b = oh * 60 + om
                    total_duration_b = 0
                    for zid in other_zones:
                        total_duration_b += get_zone_duration(zid)
                    end_b = start_b + total_duration_b
                    if start_a < end_b and end_a > start_b:
                        conflicts.append({
                            'checked_program_id': program['id'],
                            'checked_program_name': program['name'],
                            'checked_program_time': program['time'],
                            'other_program_id': other['id'],
                            'other_program_name': other['name'],
                            'other_program_time': other['time'],
                            'common_zones': list(common_zones),
                            'common_groups': list(groups_a & groups_b),
                            'overlap_start': max(start_a, start_b),
                            'overlap_end': min(end_a, end_b)
                        })
            results[str(zone_id)] = {'has_conflicts': len(conflicts) > 0, 'conflicts': conflicts}

        return jsonify({'success': True, 'results': results})
    except (sqlite3.Error, OSError) as e:
        logger.error(f"Ошибка bulk-проверки конфликтов длительности зон: {e}")
        return jsonify({'success': False, 'message': 'Ошибка проверки конфликтов'}), 500
