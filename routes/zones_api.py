"""Zones API blueprint — all /api/zones* endpoints."""
from flask import Blueprint, request, jsonify, current_app, Response, stream_with_context, send_file
from datetime import datetime, timedelta
import json
import os
import time
import queue
import logging

from database import db
from utils import normalize_topic
from irrigation_scheduler import get_scheduler
from services.mqtt_pub import publish_mqtt_value as _publish_mqtt_value
from services.helpers import api_error, api_soft, parse_dt, UPLOAD_FOLDER, ZONE_MEDIA_SUBDIR, ALLOWED_EXTENSIONS, ALLOWED_MIME_TYPES, MAX_FILE_SIZE
from services.security import admin_required
from services import sse_hub as _sse_hub
from services.api_rate_limiter import rate_limit
import sqlite3

try:
    import paho.mqtt.client as mqtt
except ImportError as e:
    logger.debug("Exception in line_20: %s", e)
    mqtt = None

try:
    from PIL import Image, ImageOps
except ImportError as e:
    logger.debug("Exception in line_26: %s", e)
    Image = None
    ImageOps = None

logger = logging.getLogger(__name__)

zones_api_bp = Blueprint('zones_api', __name__)


# ---- Image helpers ----
def allowed_file(filename):
    return '.' in filename and filename.rsplit('.', 1)[1].lower() in ALLOWED_EXTENSIONS


def normalize_image(image_data, max_long_side=1024, fmt='WEBP', quality=90, lossless=False, target_size=None):
    """Normalize image: auto-rotate by EXIF, convert to RGB, scale and save in chosen format."""
    from typing import Tuple, Optional
    import io
    try:
        img = Image.open(io.BytesIO(image_data))
        try:
            img = ImageOps.exif_transpose(img)
        except Exception as e:  # catch-all: intentional
            logger.debug("Handled exception in normalize_image: %s", e)
        if img.mode in ('RGBA', 'LA', 'P'):
            img = img.convert('RGB')
        w, h = img.size
        if target_size:
            tw, th = target_size
            scale = max(tw / w, th / h)
            new_size = (int(w * scale), int(h * scale))
            img = img.resize(new_size, Image.Resampling.LANCZOS)
            left = max(0, (img.size[0] - tw) // 2)
            top = max(0, (img.size[1] - th) // 2)
            img = img.crop((left, top, left + tw, top + th))
        else:
            if max(w, h) > max_long_side:
                scale = max_long_side / float(max(w, h))
                new_size = (int(w * scale), int(h * scale))
                img = img.resize(new_size, Image.Resampling.LANCZOS)
        out = io.BytesIO()
        fmt_upper = fmt.upper()
        if fmt_upper == 'WEBP':
            img.save(out, format='WEBP', quality=quality, lossless=lossless, method=6)
            ext = '.webp'
        elif fmt_upper in ('JPEG', 'JPG'):
            img.save(out, format='JPEG', quality=quality, optimize=True)
            ext = '.jpg'
        else:
            img.save(out, format='PNG', optimize=True)
            ext = '.png'
        out.seek(0)
        return out.getvalue(), ext
    except (ValueError, TypeError, KeyError) as e:
        logger.debug("Exception in line_80: %s", e)
        return image_data, '.jpg'


# ---- Zone CRUD ----

@zones_api_bp.route('/api/zones')
def api_zones():
    zones = db.get_zones()
    return jsonify(zones)


@zones_api_bp.route('/api/zones/<int:zone_id>', methods=['GET', 'PUT', 'DELETE'])
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
                except Exception as e:  # catch-all: intentional
                    logger.debug("Handled exception in line_128: %s", e)
            db.add_log('zone_edit', json.dumps({"zone": zone_id, "changes": data}))
            return jsonify(zone)
        if is_csv:
            try:
                logging.getLogger('import_export').info(f"PUT result id={zone_id} NOT_FOUND")
            except Exception as e:  # catch-all: intentional
                logger.debug("Handled exception in line_135: %s", e)
        return ('Zone not found', 404)

    elif request.method == 'DELETE':
        if db.delete_zone(zone_id):
            db.add_log('zone_delete', json.dumps({"zone": zone_id}))
            return ('', 204)
        return ('Zone not found', 404)


@zones_api_bp.route('/api/zones', methods=['POST'])
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
        except Exception as e:  # catch-all: intentional
            logger.debug("Handled exception in line_181: %s", e)
    return ('Error creating zone', 400)


@zones_api_bp.route('/api/zones/import', methods=['POST'])
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

@zones_api_bp.route('/api/zones/<int:zone_id>/next-watering')
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


@zones_api_bp.route('/api/zones/next-watering-bulk', methods=['POST'])
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

@zones_api_bp.route('/api/zones/check-duration-conflicts', methods=['POST'])
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


@zones_api_bp.route('/api/zones/check-duration-conflicts-bulk', methods=['POST'])
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


# ---- Photo endpoints ----

@zones_api_bp.route('/api/zones/<int:zone_id>/photo', methods=['POST'])
def upload_zone_photo(zone_id):
    """Upload photo for a zone."""
    try:
        if 'photo' not in request.files:
            return jsonify({'success': False, 'message': 'Файл не найден'}), 400
        file = request.files['photo']
        if file.filename == '':
            return jsonify({'success': False, 'message': 'Файл не выбран'}), 400
        if not allowed_file(file.filename):
            return jsonify({'success': False, 'message': 'Неподдерживаемый формат файла'}), 400
        try:
            mime = file.mimetype
        except Exception as e:  # catch-all: intentional
            logger.debug("Exception in upload_zone_photo: %s", e)
            mime = None
        if not mime or mime not in ALLOWED_MIME_TYPES:
            return jsonify({'success': False, 'message': 'Неподдерживаемый тип содержимого'}), 400
        file_data = file.read()
        if len(file_data) > MAX_FILE_SIZE:
            return jsonify({'success': False, 'message': 'Файл слишком большой'}), 400

        is_testing = bool(current_app.config.get('TESTING'))
        if is_testing:
            out_bytes = file_data
            out_ext = os.path.splitext(file.filename)[1].lower() or '.jpg'
        else:
            try:
                out_bytes, out_ext = normalize_image(file_data, target_size=(800, 600), fmt='WEBP', quality=90)
            except Exception:  # catch-all: intentional
                logger.exception('normalize_image failed, storing original bytes')
                out_bytes = file_data
                out_ext = os.path.splitext(file.filename)[1].lower() or '.jpg'

        try:
            current = db.get_zone(zone_id)
            old_rel = (current or {}).get('photo_path')
            if old_rel:
                old_abs = os.path.join('static', old_rel)
                if os.path.exists(old_abs):
                    old_dir = os.path.join(UPLOAD_FOLDER, 'OLD')
                    os.makedirs(old_dir, exist_ok=True)
                    os.replace(old_abs, os.path.join(old_dir, os.path.basename(old_abs)))
        except (sqlite3.Error, OSError) as e:
            logger.debug("Handled exception in line_650: %s", e)

        base_name = f"ZONE_{zone_id}"
        filename = f"{base_name}{out_ext}"
        filepath = os.path.join(UPLOAD_FOLDER, filename)
        with open(filepath, 'wb') as f:
            f.write(out_bytes)

        db_relative = f"media/{ZONE_MEDIA_SUBDIR}/{filename}"
        db.update_zone_photo(zone_id, db_relative)
        db.add_log('photo_upload', json.dumps({"zone": zone_id, "filename": filename}))
        return jsonify({'success': True, 'message': 'Фотография загружена', 'photo_path': db_relative})
    except (json.JSONDecodeError, KeyError, TypeError, ValueError) as e:
        logger.error(f"Ошибка загрузки фото: {e}")
        return jsonify({'success': False, 'message': 'Ошибка загрузки'}), 500


@zones_api_bp.route('/api/zones/<int:zone_id>/photo', methods=['DELETE'])
def delete_zone_photo(zone_id):
    """Delete zone photo."""
    try:
        zone = db.get_zone(zone_id)
        if not zone:
            return jsonify({'success': False, 'message': 'Зона не найдена'}), 404
        if zone.get('photo_path'):
            filepath = os.path.join('static', zone['photo_path'])
            if os.path.exists(filepath):
                os.remove(filepath)
            db.update_zone_photo(zone_id, None)
            db.add_log('photo_delete', json.dumps({"zone": zone_id}))
            return jsonify({'success': True, 'message': 'Фотография удалена'})
        else:
            return jsonify({'success': False, 'message': 'Фотография не найдена'}), 404
    except (json.JSONDecodeError, KeyError, TypeError, ValueError) as e:
        logger.error(f"Ошибка удаления фото: {e}")
        return jsonify({'success': False, 'message': 'Ошибка удаления'}), 500


@zones_api_bp.route('/api/zones/<int:zone_id>/photo/rotate', methods=['POST'])
def rotate_zone_photo(zone_id):
    """Rotate zone photo by a multiple of 90 degrees."""
    try:
        zone = db.get_zone(zone_id)
        if not zone:
            return jsonify({'success': False, 'message': 'Зона не найдена'}), 404
        angle = 90
        try:
            data = request.get_json(silent=True) or {}
            angle = int(data.get('angle', 90))
        except (ValueError, TypeError, KeyError) as e:
            logger.debug("Exception in rotate_zone_photo: %s", e)
            angle = 90
        photo_path = zone.get('photo_path')
        if not photo_path:
            return jsonify({'success': False, 'message': 'Фото отсутствует'}), 404
        filepath = os.path.join('static', photo_path)
        if not os.path.exists(filepath):
            return jsonify({'success': False, 'message': 'Файл не найден'}), 404
        try:
            with Image.open(filepath) as img:
                img = img.rotate(-angle, expand=True)
                fmt = img.format or 'JPEG'
                img.save(filepath, format=fmt)
        except (IOError, OSError, PermissionError) as e:
            logger.error(f"rotate failed: {e}")
            return jsonify({'success': False, 'message': 'Ошибка обработки изображения'}), 500
        try:
            db.add_log('photo_rotate', json.dumps({'zone': zone_id, 'angle': angle}))
        except (json.JSONDecodeError, KeyError, TypeError, ValueError) as e:
            logger.debug("Handled exception in rotate_zone_photo: %s", e)
        return jsonify({'success': True})
    except (json.JSONDecodeError, KeyError, TypeError, ValueError) as e:
        logger.error(f"Ошибка поворота фото: {e}")
        return jsonify({'success': False, 'message': 'Ошибка поворота'}), 500


@zones_api_bp.route('/api/zones/<int:zone_id>/photo', methods=['GET'])
def get_zone_photo(zone_id):
    """Get zone photo info or image."""
    try:
        zone = db.get_zone(zone_id)
        if not zone:
            return jsonify({'success': False, 'message': 'Зона не найдена'}), 404
        accept_header = request.headers.get('Accept', '')
        if 'image' in accept_header or request.args.get('image') == 'true':
            photo_path = zone.get('photo_path')
            if not photo_path:
                return jsonify({'success': False, 'message': 'Фотография не найдена'}), 404
            filepath = os.path.join('static', photo_path)
            if not os.path.exists(filepath):
                return jsonify({'success': False, 'message': 'Файл не найден'}), 404
            ext = os.path.splitext(filepath)[1].lower()
            mime = 'image/jpeg'
            if ext == '.png':
                mime = 'image/png'
            elif ext == '.gif':
                mime = 'image/gif'
            elif ext == '.webp':
                mime = 'image/webp'
            return send_file(filepath, mimetype=mime)
        else:
            has_photo = bool(zone.get('photo_path'))
            return jsonify({'success': True, 'has_photo': has_photo, 'photo_path': zone.get('photo_path')})
    except (sqlite3.Error, OSError) as e:
        logger.error(f"Ошибка получения фото зоны {zone_id}: {e}")
        return jsonify({'success': False, 'message': 'Ошибка получения фото'}), 500


# ---- Zone start/stop ----

@zones_api_bp.route('/api/zones/<int:zone_id>/start', methods=['POST'])
def start_zone(zone_id):
    """Start zone watering."""
    try:
        if current_app.config.get('EMERGENCY_STOP'):
            return jsonify({'success': False, 'message': 'Аварийная остановка активна. Сначала отключите аварийный режим.'}), 400
        zone = db.get_zone(zone_id)
        if not zone:
            return jsonify({'success': False, 'message': 'Зона не найдена'}), 404

        try:
            scheduler = get_scheduler()
            if scheduler:
                scheduler.cancel_group_jobs(int(zone['group_id']))
        except (ValueError, TypeError, KeyError) as e:
            logger.debug("Handled exception in start_zone: %s", e)

        # Turn off all other zones in group
        try:
            zones = db.get_zones()
            group_id = int(zone.get('group_id') or 0)
            if group_id:
                group_zones = [z for z in zones if z['group_id'] == group_id and int(z['id']) != int(zone_id)]
                for gz in group_zones:
                    try:
                        sid = gz.get('mqtt_server_id'); topic = (gz.get('topic') or '').strip()
                        if mqtt and sid and topic:
                            t = topic if str(topic).startswith('/') else '/' + str(topic)
                            server = db.get_mqtt_server(int(sid))
                            if server:
                                _publish_mqtt_value(server, t, '0', min_interval_sec=0.0, qos=2, retain=True)
                    except (ConnectionError, TimeoutError, OSError):
                        logger.exception("Ошибка публикации MQTT '0' при ручном запуске: выключение соседей")
                    try:
                        db.update_zone(int(gz['id']), {'state': 'off', 'watering_start_time': None})
                    except (sqlite3.Error, OSError) as e:
                        logger.debug("Handled exception in line_796: %s", e)
        except (ConnectionError, TimeoutError, OSError) as e:
            logger.debug("Handled exception in line_798: %s", e)

        try:
            from services.zone_control import exclusive_start_zone as _start_central
            ok = _start_central(int(zone_id))
            if not ok:
                return jsonify({'success': False, 'message': 'Не удалось запустить зону'}), 500
        except (ValueError, TypeError, KeyError):
            logger.exception('start_zone: central start failed')
            return jsonify({'success': False, 'message': 'Не удалось запустить зону'}), 500

        try:
            scheduler = get_scheduler()
            if scheduler:
                scheduler.schedule_zone_stop(zone_id, int(zone['duration']), command_id=str(int(time.time())))
        except (ValueError, TypeError, KeyError) as e:
            logger.error(f"Ошибка планирования остановки зоны {zone_id}: {e}")

        group_id = int(zone.get('group_id') or 0)
        db.add_log('zone_start', json.dumps({
            "zone": zone_id, "group": group_id, "source": "manual", "duration": int(zone['duration'])
        }))
        return jsonify({'success': True, 'message': f'Зона {zone_id} запущена', 'zone_id': zone_id, 'state': 'on'})
    except (json.JSONDecodeError, KeyError, TypeError, ValueError) as e:
        logger.error(f"Ошибка запуска зоны {zone_id}: {e}")
        return jsonify({'success': False, 'message': 'Ошибка запуска зоны'}), 500


@zones_api_bp.route('/api/zones/<int:zone_id>/stop', methods=['POST'])
def stop_zone(zone_id):
    """Stop zone watering."""
    try:
        zone = db.get_zone(zone_id)
        if not zone:
            return jsonify({'success': False, 'message': 'Зона не найдена'}), 404
        try:
            from services.zone_control import stop_zone as _stop_central
            if not _stop_central(int(zone_id), reason='manual', force=False):
                return jsonify({'success': False, 'message': 'Не удалось остановить зону'}), 500
        except (ValueError, TypeError, KeyError):
            logger.exception('stop_zone: central stop failed')
            return jsonify({'success': False, 'message': 'Не удалось остановить зону'}), 500
        try:
            db.add_log('zone_stop', json.dumps({
                "zone": int(zone_id), "group": int(zone.get('group_id') or 0), "source": "manual"
            }))
        except (json.JSONDecodeError, KeyError, TypeError, ValueError) as e:
            logger.debug("Handled exception in stop_zone: %s", e)
        return jsonify({'success': True, 'message': f'Зона {zone_id} остановлена', 'zone_id': zone_id, 'state': 'off'})
    except (json.JSONDecodeError, KeyError, TypeError, ValueError) as e:
        logger.error(f"Ошибка остановки зоны {zone_id}: {e}")
        return jsonify({'success': False, 'message': 'Ошибка остановки зоны'}), 500


@zones_api_bp.route('/api/zones/<int:zone_id>/watering-time')
def api_zone_watering_time(zone_id):
    """Returns remaining and elapsed watering time for a zone."""
    try:
        zone = db.get_zone(zone_id)
        if not zone:
            resp = jsonify({'success': False, 'message': 'Зона не найдена'})
            resp.headers['Cache-Control'] = 'no-store'
            return resp, 404

        total_duration = int(zone.get('duration') or 0)
        start_str = zone.get('watering_start_time')
        if zone.get('state') != 'on' or not start_str:
            resp = jsonify({
                'success': True, 'zone_id': zone_id, 'is_watering': False,
                'elapsed_time': 0, 'remaining_time': 0, 'total_duration': total_duration,
                'elapsed_seconds': 0, 'remaining_seconds': 0, 'total_seconds': total_duration * 60
            })
            resp.headers['Cache-Control'] = 'no-store'
            return resp

        try:
            start_dt = datetime.strptime(start_str, '%Y-%m-%d %H:%M:%S')
        except (ValueError, TypeError, KeyError) as e:
            logger.debug("Exception in api_zone_watering_time: %s", e)
            db.update_zone(zone_id, {'watering_start_time': None})
            resp = jsonify({
                'success': True, 'zone_id': zone_id, 'is_watering': False,
                'elapsed_time': 0, 'remaining_time': 0, 'total_duration': total_duration,
                'elapsed_seconds': 0, 'remaining_seconds': 0, 'total_seconds': total_duration * 60
            })
            resp.headers['Cache-Control'] = 'no-store'
            return resp

        now = datetime.now()
        elapsed_seconds = max(0, int((now - start_dt).total_seconds()))
        total_seconds = int(total_duration * 60)
        if elapsed_seconds >= total_seconds:
            db.update_zone(zone_id, {'state': 'off', 'watering_start_time': None})
            resp = jsonify({
                'success': True, 'zone_id': zone_id, 'is_watering': False,
                'elapsed_time': total_duration, 'remaining_time': 0, 'total_duration': total_duration,
                'elapsed_seconds': total_seconds, 'remaining_seconds': 0, 'total_seconds': total_seconds
            })
            resp.headers['Cache-Control'] = 'no-store'
            return resp
        remaining_seconds = max(0, total_seconds - elapsed_seconds)
        elapsed_min = int(elapsed_seconds // 60)
        remaining_min = int(remaining_seconds // 60)
        resp = jsonify({
            'success': True, 'zone_id': zone_id, 'is_watering': True,
            'elapsed_time': elapsed_min, 'remaining_time': remaining_min, 'total_duration': total_duration,
            'elapsed_seconds': elapsed_seconds, 'remaining_seconds': remaining_seconds, 'total_seconds': total_seconds
        })
        resp.headers['Cache-Control'] = 'no-store'
        return resp
    except (sqlite3.Error, OSError) as e:
        logger.error(f"Ошибка получения времени полива зоны {zone_id}: {e}")
        resp = jsonify({'success': False, 'message': 'Ошибка получения времени полива'})
        resp.headers['Cache-Control'] = 'no-store'
        return resp, 500


# ---- MQTT zones SSE ----

@zones_api_bp.route('/api/mqtt/zones-sse')
def api_mqtt_zones_sse():
    """Unified SSE hub for MQTT zone updates."""
    if mqtt is None:
        if current_app.config.get('TESTING'):
            return jsonify({'success': False, 'message': 'paho-mqtt not installed'}), 200
        return api_error('MQTT_LIB_MISSING', 'paho-mqtt not installed', 500)
    
    # Return mock SSE in tests
    if current_app.config.get('TESTING'):
        @stream_with_context
        def mock_gen():
            yield 'event: open\n' + 'data: {}\n\n'
            yield 'event: ping\n' + 'data: {}\n\n'
        return Response(mock_gen(), mimetype='text/event-stream', headers={'Cache-Control': 'no-cache', 'X-Accel-Buffering': 'no'})
    
    try:
        _sse_hub.ensure_hub_started()
        msg_queue = _sse_hub.register_client()

        @stream_with_context
        def _gen():
            try:
                yield 'event: open\n' + 'data: {}\n\n'
                while True:
                    try:
                        data = msg_queue.get(timeout=0.5)
                        yield f'data: {data}\n\n'
                    except queue.Empty:  # Expected: send keepalive ping on poll timeout
                        yield 'event: ping\n' + 'data: {}\n\n'
            finally:
                _sse_hub.unregister_client(msg_queue)
        return Response(_gen(), mimetype='text/event-stream', headers={'Cache-Control': 'no-cache', 'X-Accel-Buffering': 'no'})
    except queue.Full as e:
        logger.error(f"zones SSE failed: {e}")
        if current_app.config.get('TESTING'):
            return jsonify({'success': False}), 200
        return api_error('SSE_FAILED', 'sse failed', 500)


# ---- Zone MQTT start/stop ----

@zones_api_bp.route('/api/zones/<int:zone_id>/mqtt/start', methods=['POST'])
@rate_limit('mqtt_control', max_requests=10, window_sec=60)
def api_zone_mqtt_start(zone_id: int):
    t0 = time.time()
    try:
        z = db.get_zone(zone_id)
        if not z:
            return jsonify({'success': False, 'message': 'Зона не найдена'}), 404
        if current_app.config.get('EMERGENCY_STOP'):
            return jsonify({'success': False, 'message': 'Аварийная остановка активна'}), 400
        if str(z.get('state') or '') == 'on':
            return jsonify({'success': True, 'message': 'Зона уже запущена'})
        gid = int(z.get('group_id') or 0)
        try:
            if gid:
                sched = get_scheduler()
                if sched:
                    sched.cancel_group_jobs(int(gid))
                try:
                    programs = db.get_programs() or []
                    now = datetime.now()
                    today = now.strftime('%Y-%m-%d')
                    for p in programs:
                        try:
                            hh, mm = map(int, str(p.get('time') or '00:00').split(':', 1))
                        except (ValueError, TypeError, KeyError) as e:
                            logger.debug("Exception in api_zone_mqtt_start: %s", e)
                            hh, mm = 0, 0
                        start_today = now.replace(hour=hh, minute=mm, second=0, microsecond=0)
                        if start_today <= now:
                            db.cancel_program_run_for_group(int(p.get('id')), today, int(gid))
                except (ConnectionError, TimeoutError, OSError) as e:
                    logger.debug("Handled exception in api_zone_mqtt_start: %s", e)
                try:
                    db.reschedule_group_to_next_program(int(gid))
                except (sqlite3.Error, OSError) as e:
                    logger.debug("Handled exception in line_985: %s", e)
        except (ConnectionError, TimeoutError, OSError) as e:
            logger.debug("Handled exception in line_987: %s", e)
        t1 = time.time()
        # Fast OFF peers in background
        try:
            zones_all = db.get_zones() or []
            peers_on = [zz for zz in zones_all if int(zz.get('group_id') or 0) == gid and int(zz.get('id')) != int(zone_id) and str(zz.get('state') or '').lower() == 'on']
            if peers_on:
                import threading, concurrent.futures
                def _bg_off():
                    try:
                        with concurrent.futures.ThreadPoolExecutor(max_workers=min(8, len(peers_on))) as pool:
                            def _off_peer(peer):
                                try:
                                    sid = peer.get('mqtt_server_id'); topic = (peer.get('topic') or '').strip()
                                    if mqtt and sid and topic:
                                        tpc = normalize_topic(topic)
                                        server = db.get_mqtt_server(int(sid))
                                        if server:
                                            _publish_mqtt_value(server, tpc, '0', min_interval_sec=0.0, qos=2, retain=True)
                                except (ConnectionError, TimeoutError, OSError) as e:
                                    logger.debug("Handled exception in _off_peer: %s", e)
                                try:
                                    db.update_zone(int(peer['id']), {'state': 'off', 'watering_start_time': None})
                                except (sqlite3.Error, OSError) as e:
                                    logger.debug("Handled exception in _off_peer: %s", e)
                            list(pool.map(_off_peer, peers_on))
                    except (ConnectionError, TimeoutError, OSError) as e:
                        logger.debug("Exception in _off_peer: %s", e)
                        try:
                            logger.exception('fast OFF peers bg failed')
                        except Exception as e:  # catch-all: intentional
                            logger.debug("Handled exception in _off_peer: %s", e)
                import threading as _th
                _th.Thread(target=_bg_off, daemon=True).start()
        except (RuntimeError, OSError):
            logger.exception('fast parallel OFF peers failed')
        t2 = time.time()
        # Open master valve + publish zone ON
        try:
            try:
                gid2 = int(z.get('group_id') or 0)
            except (ValueError, TypeError, KeyError) as e:
                logger.debug("Exception in line_1029: %s", e)
                gid2 = 0
            if gid2:
                try:
                    g = next((gg for gg in (db.get_groups() or []) if int(gg.get('id')) == gid2), None)
                except (sqlite3.Error, OSError) as e:
                    logger.debug("Exception in line_1035: %s", e)
                    g = None
                if g and int(g.get('use_master_valve') or 0) == 1:
                    mtopic = (g.get('master_mqtt_topic') or '').strip()
                    msid = g.get('master_mqtt_server_id')
                    if mtopic and msid:
                        server_mv = db.get_mqtt_server(int(msid))
                        if server_mv:
                            try:
                                mode = (g.get('master_mode') or 'NC').strip().upper()
                            except (ValueError, TypeError, KeyError) as e:
                                logger.debug("Exception in line_1046: %s", e)
                                mode = 'NC'
                            _publish_mqtt_value(server_mv, normalize_topic(mtopic), ('0' if mode == 'NO' else '1'), min_interval_sec=0.0, qos=2, retain=True)
            sid = z.get('mqtt_server_id'); topic = (z.get('topic') or '').strip()
            if mqtt and sid and topic:
                tpc = normalize_topic(topic)
                server = db.get_mqtt_server(int(sid))
                if server:
                    _publish_mqtt_value(server, tpc, '1', min_interval_sec=0.0, qos=2, retain=True)
        except (ConnectionError, TimeoutError, OSError):
            logger.exception('fast ON publish failed')
            return jsonify({'success': False, 'message': 'MQTT publish failed'}), 500
        t3 = time.time()
        # DB update
        try:
            db.update_zone(int(zone_id), {'state': 'on', 'watering_start_time': datetime.now().strftime('%Y-%m-%d %H:%M:%S'), 'watering_start_source': 'manual', 'commanded_state': 'on'})
        except (sqlite3.Error, OSError) as e:
            logger.debug("Handled exception in line_1063: %s", e)
        t4 = time.time()
        # Schedule auto-stop in background
        t5 = time.time()
        try:
            import threading
            _is_testing = current_app.config.get('TESTING', False)
            def _bg_schedule():
                try:
                    sched = get_scheduler()
                    if sched and not _is_testing:
                        dur = int((db.get_zone(int(zone_id)) or {}).get('duration') or 0)
                        if dur > 0:
                            sched.schedule_zone_stop(int(zone_id), dur, command_id=str(int(time.time())))
                            sched.schedule_zone_hard_stop(int(zone_id), datetime.now() + timedelta(minutes=dur))
                    if (not sched) and not _is_testing:
                        try:
                            dur = int((db.get_zone(int(zone_id)) or {}).get('duration') or 0)
                        except (sqlite3.Error, OSError) as e:
                            logger.debug("Exception in _bg_schedule: %s", e)
                            dur = 0
                        if dur > 0:
                            def _fallback_stop():
                                try:
                                    time.sleep(max(1, dur * 60))
                                    from services.zone_control import stop_zone as _stop
                                    _stop(int(zone_id), reason='auto_fallback', force=True)
                                except ImportError as e:
                                    logger.debug("Exception in _fallback_stop: %s", e)
                                    try:
                                        logger.exception('fallback auto-stop failed')
                                    except Exception as e:  # catch-all: intentional
                                        logger.debug("Handled exception in _fallback_stop: %s", e)
                            import threading as _th2
                            _th2.Thread(target=_fallback_stop, daemon=True).start()
                except ImportError as e:
                    logger.debug("Exception in _fallback_stop: %s", e)
                    try:
                        logger.exception('manual mqtt start: schedule auto-stop failed')
                    except (ConnectionError, TimeoutError, OSError) as e:
                        logger.debug("Handled exception in _fallback_stop: %s", e)
            threading.Thread(target=_bg_schedule, daemon=True).start()
        except (RuntimeError, OSError) as e:
            logger.debug("Handled exception in _fallback_stop: %s", e)
        try:
            db.add_log('diag_manual_start_timing', json.dumps({
                'zone': int(zone_id),
                't_fast_off_ms': int((t2 - t1) * 1000),
                't_on_publish_ms': int((t3 - t2) * 1000),
                't_db_update_ms': int((t4 - t3) * 1000),
                't_schedule_ms': int((t5 - t4) * 1000),
                't_total_ms': int((t5 - t0) * 1000)
            }))
        except (json.JSONDecodeError, KeyError, TypeError, ValueError) as e:
            logger.debug("Handled exception in line_1116: %s", e)
        try:
            db.add_log('zone_start_manual', json.dumps({'zone': int(zone_id), 'group': gid}))
        except (json.JSONDecodeError, KeyError, TypeError, ValueError) as e:
            logger.debug("Handled exception in line_1120: %s", e)
        return jsonify({'success': True, 'message': f'Зона {int(zone_id)} запущена'})
    except (json.JSONDecodeError, KeyError, TypeError, ValueError):
        logger.exception('api_zone_mqtt_start failed')
        return jsonify({'success': False, 'message': 'Ошибка запуска зоны'}), 500


@zones_api_bp.route('/api/zones/<int:zone_id>/mqtt/stop', methods=['POST'])
@rate_limit('mqtt_control', max_requests=10, window_sec=60)
def api_zone_mqtt_stop(zone_id: int):
    z = db.get_zone(zone_id)
    if not z:
        return jsonify({'success': False}), 404
    try:
        from services.zone_control import stop_zone as _stop_central
        if _stop_central(int(zone_id), reason='manual', force=False):
            return jsonify({'success': True, 'message': 'Зона остановлена'})
    except (ValueError, TypeError, KeyError):
        logger.exception('api_zone_mqtt_stop: central stop failed, fallback to direct publish')
    sid = z.get('mqtt_server_id'); topic = (z.get('topic') or '').strip()
    if not sid or not topic:
        return jsonify({'success': False, 'message': 'No MQTT config for zone'}), 400
    t = normalize_topic(topic)
    try:
        server = db.get_mqtt_server(int(sid))
        if not server:
            return jsonify({'success': False, 'message': 'MQTT server not found'}), 400
        logger.info(f"HTTP publish OFF zone={zone_id} topic={t}")
        _publish_mqtt_value(server, t, '0', min_interval_sec=0.0, qos=2, retain=True)
        try:
            db.update_zone(zone_id, {'state': 'off', 'watering_start_time': None})
        except (sqlite3.Error, OSError) as e:
            logger.debug("Handled exception in api_zone_mqtt_stop: %s", e)
        return jsonify({'success': True, 'message': 'Зона остановлена'})
    except (ConnectionError, TimeoutError, OSError) as e:
        logger.error(f"MQTT publish stop failed: {e}")
        return jsonify({'success': False, 'message': 'MQTT publish failed'}), 500
