from flask import Flask, render_template, jsonify, request, send_file, redirect, url_for, session
from datetime import datetime, timedelta
import json
from database import db
import os
from werkzeug.utils import secure_filename
from PIL import Image
import io
import logging
from irrigation_scheduler import init_scheduler, get_scheduler
from flask_wtf.csrf import CSRFProtect
try:
    import paho.mqtt.client as mqtt
except Exception:
    mqtt = None
from flask import Response, stream_with_context
import threading
import queue
import time
from config import Config
from routes.status import status_bp
from routes.files import files_bp
from routes.zones import zones_bp
from routes.programs import programs_bp
from routes.groups import groups_bp
from routes.auth import auth_bp
from werkzeug.security import check_password_hash

# Настройка логирования
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)
try:
    from logging.handlers import RotatingFileHandler
    log_dir = os.path.join(os.getcwd(), 'backups')
    os.makedirs(log_dir, exist_ok=True)
    fh = RotatingFileHandler(os.path.join(log_dir, 'app.log'), maxBytes=1_000_000, backupCount=3)
    fh.setLevel(logging.INFO)
    fmt = logging.Formatter('%(asctime)s %(levelname)s %(name)s: %(message)s')
    fh.setFormatter(fmt)
    logger.addHandler(fh)
    # Отдельный логгер для импорт/экспорт операций
    imp_logger = logging.getLogger('import_export')
    imp_logger.setLevel(logging.INFO)
    if not any(isinstance(h, RotatingFileHandler) and 'import-export.log' in getattr(h, 'baseFilename', '') for h in imp_logger.handlers):
        imp_fh = RotatingFileHandler(os.path.join(log_dir, 'import-export.log'), maxBytes=1_000_000, backupCount=3)
        imp_fh.setLevel(logging.INFO)
        imp_fh.setFormatter(fmt)
        imp_logger.addHandler(imp_fh)
except Exception:
    pass

app = Flask(__name__)
app.config.from_object(Config)
app.db = db  # Добавляем атрибут db для тестов
csrf = CSRFProtect(app)

# Настройки хранения медиафайлов
MEDIA_ROOT = 'static/media'
ZONE_MEDIA_SUBDIR = 'zones'
MAP_MEDIA_SUBDIR = 'maps'
UPLOAD_FOLDER = os.path.join(MEDIA_ROOT, ZONE_MEDIA_SUBDIR)
ALLOWED_EXTENSIONS = {'png', 'jpg', 'jpeg', 'gif', 'webp'}
MAX_FILE_SIZE = 5 * 1024 * 1024  # 5MB

# Создаем папки для медиа
os.makedirs(UPLOAD_FOLDER, exist_ok=True)
MAP_DIR = os.path.join(MEDIA_ROOT, MAP_MEDIA_SUBDIR)
os.makedirs(MAP_DIR, exist_ok=True)

def allowed_file(filename):
    return '.' in filename and \
           filename.rsplit('.', 1)[1].lower() in ALLOWED_EXTENSIONS

def compress_image(image_data, max_size=(800, 600), quality=85):
    """Сжатие изображения"""
    try:
        img = Image.open(io.BytesIO(image_data))
        
        # Конвертируем в RGB если нужно
        if img.mode in ('RGBA', 'LA', 'P'):
            img = img.convert('RGB')
        
        # Изменяем размер
        img.thumbnail(max_size, Image.Resampling.LANCZOS)
        
        # Сохраняем сжатое изображение
        output = io.BytesIO()
        img.save(output, format='JPEG', quality=quality, optimize=True)
        output.seek(0)
        
        return output.getvalue()
    except Exception as e:
        logger.error(f"Ошибка сжатия изображения: {e}")
        return image_data

# удалены устаревшие функции generate_water_data/login_required/admin_required


_SCHEDULER_INIT_DONE = False
_INITIAL_SYNC_DONE = False

@app.before_request
def _init_scheduler_before_request():
    global _SCHEDULER_INIT_DONE, _INITIAL_SYNC_DONE
    # Default role is "user" (no password)
    if 'role' not in session:
        session['role'] = 'user'
    if not _SCHEDULER_INIT_DONE and not app.config.get('TESTING'):
        try:
            init_scheduler(db)
            _SCHEDULER_INIT_DONE = True
        except Exception as e:
            logger.error(f"Ошибка инициализации планировщика: {e}")
    # Запускаем сторож одновременности зон (один раз)
    try:
        _start_single_zone_watchdog()
    except Exception:
        pass
    # Стартовая синхронизация: единоразово выключаем все зоны и публикуем OFF
    if not _INITIAL_SYNC_DONE and not app.config.get('TESTING'):
        try:
            zones = db.get_zones()
            for z in zones:
                try:
                    db.update_zone(int(z['id']), {'state': 'off', 'watering_start_time': None})
                except Exception:
                    pass
                try:
                    sid = z.get('mqtt_server_id'); topic = (z.get('topic') or '').strip()
                    if mqtt and sid and topic:
                        t = topic if str(topic).startswith('/') else '/' + str(topic)
                        server = db.get_mqtt_server(int(sid))
                        if server:
                            _publish_mqtt_value(server, t, '0')
                except Exception:
                    pass
            _INITIAL_SYNC_DONE = True
            logger.info("Initial sync: all zones set to OFF and MQTT OFF published")
        except Exception as e:
            logger.error(f"Initial sync failed: {e}")


app.register_blueprint(status_bp)
app.register_blueprint(files_bp)
app.register_blueprint(zones_bp)
app.register_blueprint(programs_bp)
app.register_blueprint(groups_bp)
app.register_blueprint(auth_bp)
try:
    from routes.mqtt import mqtt_bp
    app.register_blueprint(mqtt_bp)
except Exception as _e:
    logger.warning(f"MQTT blueprint not registered: {_e}")
@csrf.exempt
@app.route('/api/settings/early-off', methods=['GET', 'POST'])
def api_setting_early_off():
    try:
        if request.method == 'GET':
            seconds = db.get_early_off_seconds()
            return jsonify({'success': True, 'seconds': seconds})
        data = request.get_json(silent=True) or {}
        seconds = int(data.get('seconds', 3))
        if seconds < 0 or seconds > 15:
            return jsonify({'success': False, 'message': 'seconds must be within 0..15'}), 400
        ok = db.set_early_off_seconds(seconds)
        return jsonify({'success': bool(ok), 'seconds': seconds})
    except Exception as e:
        logger.error(f"early-off setting failed: {e}")
        return jsonify({'success': False}), 500
# (initial sync moved into before_request)
# Глобальная защита от дребезга запусков по группам (анти-флаппер)
_GROUP_CHANGE_GUARD = {}
_GROUP_GUARD_LOCK = threading.Lock()
def _should_throttle_group(group_id: int, window_sec: float = 0.8) -> bool:
    now = time.time()
    with _GROUP_GUARD_LOCK:
        last = _GROUP_CHANGE_GUARD.get(group_id, 0)
        if now - last < window_sec:
            return True
        _GROUP_CHANGE_GUARD[group_id] = now
    return False

# Отправка MQTT с анти-дребезгом по топику
_TOPIC_LAST_SEND: dict[tuple[int, str], tuple[str, float]] = {}
_TOPIC_LOCK = threading.Lock()

def _publish_mqtt_value(server: dict, topic: str, value: str, min_interval_sec: float = 0.5) -> bool:
    try:
        t = topic if str(topic).startswith('/') else '/' + str(topic)
        sid = int(server.get('id')) if server.get('id') else None
        key = (sid or 0, t)
        now = time.time()
        with _TOPIC_LOCK:
            last = _TOPIC_LAST_SEND.get(key)
            if last and last[0] == value and (now - last[1]) < min_interval_sec:
                logger.info(f"MQTT skip duplicate topic={t} value={value}")
                return True  # считаем, что уже отослали недавно
            _TOPIC_LAST_SEND[key] = (value, now)
        logger.info(f"MQTT publish topic={t} value={value}")
        cl = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2)
        if server.get('username'):
            cl.username_pw_set(server.get('username'), server.get('password') or None)
        cl.connect(server.get('host') or '127.0.0.1', int(server.get('port') or 1883), 5)
        cl.publish(t, payload=value, qos=0, retain=False)
        cl.disconnect()
        return True
    except Exception:
        logger.exception("MQTT publish failed")
        return False

# === Safety: only one zone may be ON inside a single group (groups independent) ===
def _force_group_exclusive(group_id: int, reason: str = "group_exclusive") -> None:
    try:
        group_zones = db.get_zones_by_group(group_id)
        on_zones = [z for z in group_zones if str(z.get('state')) == 'on']
        if len(on_zones) <= 1:
            return
        # Оставляем только одну зону включенной: с самым поздним временем старта, иначе с минимальным id
        def started_key(z):
            try:
                ts = z.get('watering_start_time') or ''
                return datetime.strptime(ts, '%Y-%m-%d %H:%M:%S')
            except Exception:
                return datetime.min
        on_zones_sorted = sorted(on_zones, key=started_key, reverse=True)
        keep = on_zones_sorted[0]
        to_off = [z for z in on_zones_sorted[1:]]
        for z in to_off:
            try:
                sid = z.get('mqtt_server_id'); topic = (z.get('topic') or '').strip()
                if mqtt and sid and topic:
                    t = topic if str(topic).startswith('/') else '/' + str(topic)
                    server = db.get_mqtt_server(int(sid))
                    if server:
                        _publish_mqtt_value(server, t, '0')
            except Exception:
                pass
            try:
                db.update_zone(int(z['id']), {'state': 'off', 'watering_start_time': None, 'last_watering_time': z.get('watering_start_time')})
            except Exception:
                pass
        try:
            db.add_log('warning', json.dumps({'type': 'group_exclusive_fix', 'group_id': group_id, 'kept_zone': keep.get('id'), 'turned_off': [z.get('id') for z in to_off]}))
        except Exception:
            pass
    except Exception as e:
        logger.error(f"Group exclusivity enforcement failed for group {group_id}: {e}")

def _enforce_group_exclusive_all_groups() -> None:
    try:
        zones = db.get_zones()
        zones_by_group = {}
        for z in zones:
            gid = int(z.get('group_id') or 0)
            if gid == 0 or gid == 999:
                continue
            zones_by_group.setdefault(gid, []).append(z)
        for gid, arr in zones_by_group.items():
            on_list = [z for z in arr if str(z.get('state')) == 'on']
            if len(on_list) > 1:
                _force_group_exclusive(gid, 'watchdog')
    except Exception:
        pass

_WATCHDOG_STARTED = False
_WATCHDOG_STOP_EVENT = threading.Event()
def _start_single_zone_watchdog():
    global _WATCHDOG_STARTED
    if _WATCHDOG_STARTED:
        return
    _WATCHDOG_STARTED = True
    def _run():
        while not _WATCHDOG_STOP_EVENT.is_set():
            try:
                _enforce_group_exclusive_all_groups()
            except Exception:
                pass
            _WATCHDOG_STOP_EVENT.wait(1.0)
    threading.Thread(target=_run, daemon=True).start()

import atexit
def _shutdown_background_threads():
    try:
        _WATCHDOG_STOP_EVENT.set()
    except Exception:
        pass
atexit.register(_shutdown_background_threads)

# 404 красивая страница
@app.errorhandler(404)
def _not_found(e):
    try:
        return render_template('404.html'), 404
    except Exception:
        return jsonify({'error': 'Not found'}), 404


@csrf.exempt
@app.route('/api/scheduler/init', methods=['POST'])
def api_scheduler_init():
    """Явная инициализация планировщика для UI/тестов."""
    global _SCHEDULER_INIT_DONE
    try:
        init_scheduler(db)
        _SCHEDULER_INIT_DONE = True
        return jsonify({'success': True})
    except Exception as e:
        logger.error(f"Ошибка явной инициализации планировщика: {e}")
        return jsonify({'success': False}), 500


MAP_FOLDER = MAP_DIR  # использовать новый каталог media/maps


# удалён дублирующий эндпойнт /api/login (оставлен в routes/auth.py)


@app.route('/api/auth/status')
def api_auth_status():
    return jsonify({'authenticated': bool(session.get('logged_in')) or bool(app.config.get('TESTING'))})


@app.route('/logout', methods=['GET'])
def api_logout():
    # Возвращаем роль в user
    session['logged_in'] = False
    session['role'] = 'user'
    return redirect(url_for('auth_bp.login_page'))


@csrf.exempt
@app.route('/api/password', methods=['POST'])
def api_change_password():
    try:
        if not session.get('logged_in') and not app.config.get('TESTING'):
            return jsonify({'success': False, 'message': 'Требуется аутентификация'}), 401
        data = request.get_json() or {}
        old_password = data.get('old_password', '')
        new_password = data.get('new_password', '')
        if not new_password:
            return jsonify({'success': False, 'message': 'Новый пароль обязателен'}), 400
        stored_hash = db.get_password_hash()
        if stored_hash and (app.config.get('TESTING') or check_password_hash(stored_hash, old_password)):
            if db.set_password(new_password):
                return jsonify({'success': True})
            return jsonify({'success': False, 'message': 'Не удалось обновить пароль'}), 500
        return jsonify({'success': False, 'message': 'Старый пароль неверен'}), 400
    except Exception as e:
        logger.error(f"Ошибка смены пароля: {e}")
        return jsonify({'success': False, 'message': 'Ошибка смены пароля'}), 500

@app.route('/api/map', methods=['GET', 'POST'])
def api_map():
    try:
        if request.method == 'GET':
            # Отдаём последний загруженный файл с допустимым расширением
            allowed_ext = {'.png', '.jpg', '.jpeg', '.gif', '.webp'}
            candidates = []
            for f in os.listdir(MAP_FOLDER):
                p = os.path.join(MAP_FOLDER, f)
                try:
                    ext = os.path.splitext(f)[1].lower()
                    if os.path.isfile(p) and ext in allowed_ext:
                        candidates.append((p, os.path.getmtime(p)))
                except Exception:
                    continue
            if candidates:
                candidates.sort(key=lambda x: x[1], reverse=True)
                latest_path = candidates[0][0]
                return jsonify({'success': True, 'path': f"media/maps/{os.path.basename(latest_path)}"})
            return jsonify({'success': True, 'path': None})
        else:
            if 'file' not in request.files:
                return jsonify({'success': False, 'message': 'Файл не найден'}), 400
            file = request.files['file']
            if file.filename == '':
                return jsonify({'success': False, 'message': 'Файл не выбран'}), 400
            ext = os.path.splitext(file.filename)[1].lower()
            if ext not in ['.png', '.jpg', '.jpeg', '.gif', '.webp']:
                return jsonify({'success': False, 'message': 'Неподдерживаемый формат'}), 400
            # очищаем старые карты
            for f in os.listdir(MAP_FOLDER):
                try:
                    os.remove(os.path.join(MAP_FOLDER, f))
                except Exception:
                    pass
            filename = f"zones_map{ext}"
            save_path = os.path.join(MAP_FOLDER, filename)
            file.save(save_path)
            return jsonify({'success': True, 'message': 'Карта загружена', 'path': f"media/maps/{filename}"})
    except Exception as e:
        logger.error(f"Ошибка работы с картой зон: {e}")
        return jsonify({'success': False, 'message': 'Ошибка работы с картой'}), 500

@app.route('/sw.js')
def service_worker():
    return app.send_static_file('sw.js')

@app.route('/api/scheduler/status')
def api_scheduler_status():
    """API для получения статуса планировщика"""
    try:
        scheduler = get_scheduler()
        if not scheduler:
            return jsonify({'error': 'Планировщик не инициализирован'}), 500
        
        active_programs = scheduler.get_active_programs()
        active_zones = scheduler.get_active_zones()
        
        return jsonify({
            'active_programs': active_programs,
            'active_zones': {str(k): v.isoformat() for k, v in active_zones.items()},
            'is_running': scheduler.is_running
        })
        
    except Exception as e:
        logger.error(f"Ошибка получения статуса планировщика: {e}")
        return jsonify({'error': 'Ошибка получения статуса'}), 500

@app.route('/api/rain-toggle', methods=['POST'])
def api_rain_toggle():
    """Ручной тумблер датчика дождя для тестирования: {'on': true|false}"""
    try:
        data = request.get_json() or {}
        on = data.get('on')
        if on is True:
            app.config['RAIN_MANUAL'] = True
        elif on is False:
            app.config['RAIN_MANUAL'] = False
        else:
            app.config['RAIN_MANUAL'] = None
        return jsonify({'success': True, 'rain_manual': app.config['RAIN_MANUAL']})
    except Exception as e:
        logger.error(f"Ошибка переключения статуса дождя: {e}")
        return jsonify({'success': False}), 500

@app.route('/api/zones/<int:zone_id>/next-watering')
def api_zone_next_watering(zone_id):
    """API для получения времени следующего полива зоны"""
    try:
        zone = db.get_zone(zone_id)
        if not zone:
            return jsonify({'error': 'Зона не найдена'}), 404
        
        # Ищем программы, которые включают эту зону
        programs = db.get_programs()
        zone_programs = []
        
        for program in programs:
            # Обрабатываем zones как список или JSON строку
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
        # Рассчитываем ближайшую дату/время полива за ближайшие 14 дней
        weekday_map = { 'Пн':0, 'Вт':1, 'Ср':2, 'Чт':3, 'Пт':4, 'Сб':5, 'Вс':6 }
        now = datetime.now()
        # Если зона отложена до будущего времени — считаем расписание ПОСЛЕ этой даты
        try:
            pu = zone.get('postpone_until')
            if pu:
                pu_dt = datetime.strptime(pu, '%Y-%m-%d %H:%M')
                if pu_dt > now:
                    now = pu_dt
        except Exception:
            pass
        best_dt = None
        best_payload = None

        for program in zone_programs:
            program_time = datetime.strptime(program['time'], '%H:%M').time()
            # Дни уже в формате 0-6 (0=Пн)
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
        
    except Exception as e:
        logger.error(f"Ошибка получения времени следующего полива для зоны {zone_id}: {e}")
        return jsonify({'error': 'Ошибка получения времени полива'}), 500

# API эндпоинты
@app.route('/api/zones')
def api_zones():
    zones = db.get_zones()
    return jsonify(zones)

@app.route('/api/zones/<int:zone_id>', methods=['GET', 'PUT', 'DELETE'])
def api_zone(zone_id):
    if request.method == 'GET':
        zone = db.get_zone(zone_id)
        if zone:
            return jsonify(zone)
        return jsonify({'success': False, 'message': 'Zone not found'}), 404
    
    elif request.method == 'PUT':
        data = request.get_json() or {}
        try:
            is_csv = (request.headers.get('X-Import-Op') == 'csv') or (request.args.get('source') == 'csv')
        except Exception:
            is_csv = False
        if is_csv:
            try:
                logging.getLogger('import_export').info(f"PUT zone from CSV id={zone_id} payload={json.dumps(data, ensure_ascii=False)}")
            except Exception:
                pass
        zone = db.update_zone(zone_id, data)
        if zone:
            if is_csv:
                try:
                    logging.getLogger('import_export').info(f"PUT result id={zone_id} OK")
                except Exception:
                    pass
            db.add_log('zone_edit', json.dumps({"zone": zone_id, "changes": data}))
            return jsonify(zone)
        if is_csv:
            try:
                logging.getLogger('import_export').info(f"PUT result id={zone_id} NOT_FOUND")
            except Exception:
                pass
        return ('Zone not found', 404)
    
    elif request.method == 'DELETE':
        if db.delete_zone(zone_id):
            db.add_log('zone_delete', json.dumps({"zone": zone_id}))
            return ('', 204)
        return ('Zone not found', 404)

@app.route('/api/zones', methods=['POST'])
def api_create_zone():
    data = request.get_json() or {}
    try:
        is_csv = (request.headers.get('X-Import-Op') == 'csv') or (request.args.get('source') == 'csv')
    except Exception:
        is_csv = False
    if is_csv:
        try:
            logging.getLogger('import_export').info(f"POST create zone from CSV payload={json.dumps(data, ensure_ascii=False)}")
        except Exception:
            pass
    zone = db.create_zone(data)
    if zone:
        db.add_log('zone_create', json.dumps({"zone": zone['id'], "name": zone['name']}))
        if is_csv:
            try:
                logging.getLogger('import_export').info(f"POST result id={zone.get('id')} OK")
            except Exception:
                pass
        return jsonify(zone), 201
    if is_csv:
        try:
            logging.getLogger('import_export').info("POST result ERROR")
        except Exception:
            pass
    return ('Error creating zone', 400)

@app.route('/api/groups')
def api_groups():
    groups = db.get_groups()
    return jsonify(groups)

@app.route('/api/groups/<int:group_id>', methods=['PUT'])
@csrf.exempt
def api_update_group(group_id):
    data = request.get_json()
    if db.update_group(group_id, data['name']):
        db.add_log('group_edit', json.dumps({"group": group_id, "name": data['name']}))
        return jsonify({"success": True})
    return ('Group not found', 404)

@app.route('/api/groups', methods=['POST'])
@csrf.exempt
def api_create_group():
    data = request.get_json() or {}
    name = data.get('name') or 'Новая группа'
    group = db.create_group(name)
    if group:
        db.add_log('group_create', json.dumps({"group": group['id'], "name": name}))
        return jsonify(group), 201
    return jsonify({"success": False, "message": "Не удалось создать группу"}), 400

@app.route('/api/groups/<int:group_id>', methods=['DELETE'])
def api_delete_group(group_id):
    if db.delete_group(group_id):
        db.add_log('group_delete', json.dumps({"group": group_id}))
        return ('', 204)
    return jsonify({"success": False, "message": "Нельзя удалить группу: переместите или удалите зоны этой группы"}), 400

@app.route('/api/programs')
def api_programs():
    programs = db.get_programs()
    return jsonify(programs)

@app.route('/api/programs/<int:prog_id>', methods=['GET', 'PUT', 'DELETE'])
def api_program(prog_id):
    if request.method == 'GET':
        program = db.get_program(prog_id)
        return jsonify(program) if program else ('Program not found', 404)
    
    elif request.method == 'PUT':
        data = request.get_json() or {}
        # Нормализуем дни в числа 0..6 на всякий случай
        try:
            if isinstance(data.get('days'), list):
                data['days'] = [int(d) for d in data['days']]
        except Exception:
            pass
        # Серверная проверка конфликтов перед сохранением
        try:
            conflicts = db.check_program_conflicts(program_id=prog_id, time=data['time'], zones=data['zones'], days=data['days'])
            if conflicts:
                # Возвращаем 200, чтобы фронтенд мог показать предупреждение без исключения fetch
                return jsonify({'success': False, 'has_conflicts': True, 'conflicts': conflicts, 'message': 'Обнаружены конфликты программ'})
        except Exception as e:
            logger.error(f"Ошибка серверной проверки конфликтов: {e}")
        program = db.update_program(prog_id, data)
        if program:
            db.add_log('prog_edit', json.dumps({"prog": prog_id, "changes": data}))
            # Перепланировать программу
            try:
                scheduler = get_scheduler()
                if scheduler:
                    scheduler.schedule_program(program['id'], program)
            except Exception as e:
                logger.error(f"Ошибка перепланирования программы {prog_id}: {e}")
            return jsonify(program)
        return ('Program not found', 404)
    
    elif request.method == 'DELETE':
        if db.delete_program(prog_id):
            db.add_log('prog_delete', json.dumps({"prog": prog_id}))
            try:
                scheduler = get_scheduler()
                if scheduler:
                    scheduler.cancel_program(prog_id)
            except Exception as e:
                logger.error(f"Ошибка отмены программы {prog_id} в планировщике: {e}")
            return ('', 204)
        return jsonify({'success': False, 'message': 'Program not found'}), 404

@app.route('/api/programs', methods=['POST'])
def api_create_program():
    data = request.get_json() or {}
    # Нормализуем дни (строки->int)
    try:
        if isinstance(data.get('days'), list):
            data['days'] = [int(d) for d in data['days']]
    except Exception:
        pass
    # Серверная проверка конфликтов перед созданием
    try:
        conflicts = db.check_program_conflicts(program_id=None, time=data['time'], zones=data['zones'], days=data['days'])
        if conflicts:
            # 200 вместо 400 — фронтенд обработает предупреждение и не будет кидать исключение
            return jsonify({'success': False, 'has_conflicts': True, 'conflicts': conflicts, 'message': 'Обнаружены конфликты программ'})
    except Exception as e:
        logger.error(f"Ошибка серверной проверки конфликтов (create): {e}")
    program = db.create_program(data)
    if program:
        db.add_log('prog_create', json.dumps({"prog": program['id'], "name": program['name']}))
        try:
            scheduler = get_scheduler()
            if scheduler:
                scheduler.schedule_program(program['id'], program)
        except Exception as e:
            logger.error(f"Ошибка планирования новой программы {program['id']}: {e}")
        return jsonify(program), 201
    return ('Error creating program', 400)

@app.route('/api/programs/check-conflicts', methods=['POST'])
def check_program_conflicts():
    """Проверка пересечения программ полива"""
    try:
        data = request.get_json()
        program_id = data.get('program_id')
        time = data.get('time')
        zones = data.get('zones', [])
        days = data.get('days', [])
        
        if not time or not zones or not days:
            return jsonify({'success': False, 'message': 'Необходимо указать время, дни и зоны'}), 400
        
        conflicts = db.check_program_conflicts(program_id, time, zones, days)
        
        return jsonify({
            'success': True,
            'conflicts': conflicts,
            'has_conflicts': len(conflicts) > 0
        })
        
    except Exception as e:
        logger.error(f"Ошибка проверки конфликтов программ: {e}")
        return jsonify({'success': False, 'message': 'Ошибка проверки конфликтов'}), 500

@app.route('/api/zones/check-duration-conflicts', methods=['POST'])
def api_check_zone_duration_conflicts():
    """Проверка конфликтов программ при изменении длительности конкретной зоны.

    Принимает JSON: {"zone_id": int, "new_duration": int}
    Возвращает список конфликтов, если изменение приведет к пересечению программ
    по зонам или по группам с учетом последовательного полива зон в программах.
    """
    try:
        data = request.get_json() or {}
        zone_id = data.get('zone_id')
        new_duration = data.get('new_duration')

        if not isinstance(zone_id, int) or not isinstance(new_duration, int):
            return jsonify({'success': False, 'message': 'Некорректные параметры'}), 400

        zone = db.get_zone(zone_id)
        if not zone:
            return jsonify({'success': False, 'message': 'Зона не найдена'}), 404

        # Получаем все программы
        programs = db.get_programs()
        conflicts = []

        # Пре-вычислим группы зоны
        def get_zone_group(zid: int):
            z = db.get_zone(zid)
            return z['group_id'] if z else None

        # Ищем программы, где участвует эта зона
        for program in programs:
            prog_days = program['days'] if isinstance(program['days'], list) else json.loads(program['days'])
            prog_zones = program['zones'] if isinstance(program['zones'], list) else json.loads(program['zones'])

            if zone_id not in prog_zones:
                continue

            # Время начала проверяемой программы
            try:
                p_hour, p_min = map(int, program['time'].split(':'))
            except Exception:
                continue
            start_a = p_hour * 60 + p_min

            # Суммарная длительность проверяемой программы с учетом нового времени зоны
            total_duration_a = 0
            for zid in prog_zones:
                if zid == zone_id:
                    total_duration_a += int(new_duration)
                else:
                    total_duration_a += int(db.get_zone_duration(zid))
            end_a = start_a + total_duration_a

            # Набор групп для проверяемой программы
            groups_a = set(filter(lambda g: g is not None, [get_zone_group(zid) for zid in prog_zones]))

            # Сравниваем с каждой другой программой
            for other in programs:
                if other['id'] == program['id']:
                    continue

                other_days = other['days'] if isinstance(other['days'], list) else json.loads(other['days'])
                # Пересечение дней
                common_days = set(prog_days) & set(other_days)
                if not common_days:
                    continue

                other_zones = other['zones'] if isinstance(other['zones'], list) else json.loads(other['zones'])

                # Общие зоны и группы
                common_zones = set(prog_zones) & set(other_zones)
                groups_b = set(filter(lambda g: g is not None, [get_zone_group(zid) for zid in other_zones]))
                common_groups = groups_a & groups_b
                if not common_zones and not common_groups:
                    continue

                # Время другой программы
                try:
                    oh, om = map(int, other['time'].split(':'))
                except Exception:
                    continue
                start_b = oh * 60 + om
                total_duration_b = 0
                for zid in other_zones:
                    total_duration_b += int(db.get_zone_duration(zid))
                end_b = start_b + total_duration_b

                # Проверка пересечения интервалов
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

        return jsonify({
            'success': True,
            'has_conflicts': len(conflicts) > 0,
            'conflicts': conflicts
        })

    except Exception as e:
        logger.error(f"Ошибка проверки конфликтов длительности зоны: {e}")
        return jsonify({'success': False, 'message': 'Ошибка проверки конфликтов'}), 500

# ===== MQTT Servers API =====
@app.route('/api/mqtt/servers', methods=['GET'])
def api_mqtt_servers_list():
    try:
        return jsonify({'success': True, 'servers': db.get_mqtt_servers()})
    except Exception as e:
        logger.error(f"Ошибка получения MQTT серверов: {e}")
        return jsonify({'success': False, 'message': 'Ошибка получения списка'}), 500

@app.route('/api/mqtt/servers', methods=['POST'])
@csrf.exempt
def api_mqtt_server_create():
    try:
        data = request.get_json() or {}
        server = db.create_mqtt_server(data)
        if not server:
            return jsonify({'success': False, 'message': 'Не удалось создать сервер'}), 400
        return jsonify({'success': True, 'server': server}), 201
    except Exception as e:
        logger.error(f"Ошибка создания MQTT сервера: {e}")
        return jsonify({'success': False, 'message': 'Ошибка создания'}), 500

@app.route('/api/mqtt/servers/<int:server_id>', methods=['GET'])
def api_mqtt_server_get(server_id: int):
    try:
        server = db.get_mqtt_server(server_id)
        if not server:
            return jsonify({'success': False, 'message': 'Сервер не найден'}), 404
        return jsonify({'success': True, 'server': server})
    except Exception as e:
        logger.error(f"Ошибка получения MQTT сервера {server_id}: {e}")
        return jsonify({'success': False, 'message': 'Ошибка получения'}), 500

@app.route('/api/mqtt/servers/<int:server_id>', methods=['PUT'])
@csrf.exempt
def api_mqtt_server_update(server_id: int):
    try:
        data = request.get_json() or {}
        ok = db.update_mqtt_server(server_id, data)
        if not ok:
            return jsonify({'success': False, 'message': 'Не удалось обновить'}), 400
        return jsonify({'success': True, 'server': db.get_mqtt_server(server_id)})
    except Exception as e:
        logger.error(f"Ошибка обновления MQTT сервера {server_id}: {e}")
        return jsonify({'success': False, 'message': 'Ошибка обновления'}), 500

@app.route('/api/mqtt/servers/<int:server_id>', methods=['DELETE'])
@csrf.exempt
def api_mqtt_server_delete(server_id: int):
    try:
        ok = db.delete_mqtt_server(server_id)
        if not ok:
            return jsonify({'success': False, 'message': 'Не удалось удалить'}), 400
        return ('', 204)
    except Exception as e:
        logger.error(f"Ошибка удаления MQTT сервера {server_id}: {e}")
        return jsonify({'success': False, 'message': 'Ошибка удаления'}), 500

@app.route('/api/logs')
def api_logs():
    """API для получения логов с фильтрацией"""
    try:
        # Получаем параметры фильтрации
        from_date = request.args.get('from')
        to_date = request.args.get('to')
        event_type = request.args.get('type')
        
        logs = db.get_logs()
        
        # Применяем фильтры
        if from_date or to_date or event_type:
            filtered_logs = []
            
            for log in logs:
                # Фильтр по типу события
                if event_type and log['type'] != event_type:
                    continue
                
                # Фильтр по дате
                if from_date or to_date:
                    try:
                        log_date = datetime.strptime(log['timestamp'][:10], '%Y-%m-%d').date()
                        
                        if from_date:
                            from_dt = datetime.strptime(from_date, '%Y-%m-%d').date()
                            if log_date < from_dt:
                                continue
                        
                        if to_date:
                            to_dt = datetime.strptime(to_date, '%Y-%m-%d').date()
                            if log_date > to_dt:
                                continue
                    except:
                        continue
                
                filtered_logs.append(log)
            
            logs = filtered_logs
        
        return jsonify(logs)
        
    except Exception as e:
        logger.error(f"Ошибка получения логов: {e}")
        return jsonify({'error': 'Ошибка получения логов'}), 500

# Lightweight MQTT probe to fetch messages quickly (best-effort)
@app.route('/api/mqtt/<int:server_id>/probe', methods=['POST'])
def api_mqtt_probe(server_id: int):
    try:
        server = db.get_mqtt_server(server_id)
        if not server:
            return jsonify({'success': False, 'message': 'server not found', 'items': [], 'events': []}), 200
        if mqtt is None:
            return jsonify({'success': False, 'message': 'paho-mqtt not installed', 'items': [], 'events': []}), 200
        data = request.get_json() or {}
        topic_filter = data.get('filter', '#')
        duration = float(data.get('duration', 3))  # seconds

        received = []
        events = [f"probe: connecting to {server.get('host')}:{server.get('port')} filter={topic_filter} duration={duration}s"]
        # paho-mqtt v2 style
        client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id=(server.get('client_id') or None))
        if server.get('username'):
            client.username_pw_set(server.get('username'), server.get('password') or None)

        def on_connect(cl, userdata, flags, reason_code, properties=None):
            try:
                cl.subscribe(topic_filter, qos=0)
                events.append(f"connected rc={reason_code}, subscribed to {topic_filter}")
            except Exception:
                events.append("subscribe failed")

        def on_message(cl, userdata, msg):
            try:
                topic = msg.topic
            except Exception:
                # paho v2 sometimes returns bytes; normalize
                topic = getattr(msg, 'topic', '')
            if len(received) < 1000:
                try:
                    payload = msg.payload.decode('utf-8', errors='ignore')
                except Exception:
                    payload = str(msg.payload)
                received.append({'topic': topic, 'payload': payload})

        client.on_connect = on_connect
        client.on_message = on_message
        try:
            client.connect(server.get('host') or '127.0.0.1', int(server.get('port') or 1883), 5)
        except Exception as ce:
            events.append(f"connect error: {ce}")
            return jsonify({'success': False, 'message': 'connect failed', 'items': [], 'events': events}), 200
        client.loop_start()
        import time as _t
        start = _t.time()
        while _t.time() - start < duration and len(received) < 5000:
            _t.sleep(0.1)
        client.loop_stop()
        try:
            client.disconnect()
        except Exception:
            pass
        if not received:
            events.append('no messages received')
        return jsonify({'success': True, 'items': received, 'events': events})
    except Exception as e:
        logger.error(f"MQTT probe error: {e}")
        return jsonify({'success': False, 'message': 'probe failed', 'items': [], 'events': [str(e)]}), 200

# Quick connection status check
@app.route('/api/mqtt/<int:server_id>/status', methods=['GET'])
def api_mqtt_status(server_id: int):
    try:
        server = db.get_mqtt_server(server_id)
        if not server:
            return jsonify({'success': True, 'connected': False, 'message': 'server not found'}), 200
        if mqtt is None:
            return jsonify({'success': True, 'connected': False, 'message': 'paho-mqtt not installed'}), 200
        ok = False
        client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id=(server.get('client_id') or None))
        if server.get('username'):
            client.username_pw_set(server.get('username'), server.get('password') or None)
        try:
            client.connect(server.get('host') or '127.0.0.1', int(server.get('port') or 1883), 3)
            ok = True
            try:
                client.disconnect()
            except Exception:
                pass
        except Exception as _e:
            logger.info(f"MQTT status connection failed for server {server_id}: {_e}")
            ok = False
        return jsonify({'success': True, 'connected': ok})
    except Exception as e:
        logger.error(f"MQTT status error: {e}")
        return jsonify({'success': True, 'connected': False, 'message': 'status failed'}), 200

# Server-Sent Events: continuous scan stream
@app.route('/api/mqtt/<int:server_id>/scan-sse')
def api_mqtt_scan_sse(server_id: int):
    """Stream MQTT messages as SSE for continuous scanning.

    Query params:
    - filter: MQTT subscription filter (e.g., /devices/#)
    """
    try:
        server = db.get_mqtt_server(server_id)
        if not server:
            return jsonify({'success': False, 'message': 'server not found'}), 200
        if mqtt is None:
            return jsonify({'success': False, 'message': 'paho-mqtt not installed'}), 200

        sub_filter = request.args.get('filter', '/devices/#') or '/devices/#'

        msg_queue: "queue.Queue[str]" = queue.Queue(maxsize=10000)
        stop_event = threading.Event()

        def _run_client():
            try:
                client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id=(server.get('client_id') or None))
                if server.get('username'):
                    client.username_pw_set(server.get('username'), server.get('password') or None)

                def on_connect(cl, userdata, flags, reason_code, properties=None):
                    try:
                        cl.subscribe(sub_filter, qos=0)
                    except Exception:
                        pass

                def on_message(cl, userdata, msg):
                    try:
                        topic = msg.topic
                    except Exception:
                        topic = getattr(msg, 'topic', '')
                    try:
                        payload = msg.payload.decode('utf-8', errors='ignore')
                    except Exception:
                        payload = str(msg.payload)
                    data = json.dumps({'topic': topic if str(topic).startswith('/') else '/' + str(topic), 'payload': payload})
                    try:
                        msg_queue.put_nowait(data)
                    except queue.Full:
                        pass

                client.on_connect = on_connect
                client.on_message = on_message
                client.connect(server.get('host') or '127.0.0.1', int(server.get('port') or 1883), 5)
                client.loop_start()
                # ограничение времени жизни клиента во избежание зависаний
                import time as _t
                _start_ts = _t.time()
                while not stop_event.is_set():
                    stop_event.wait(0.2)
                    if _t.time() - _start_ts > 300:  # 5 минут
                        break
                client.loop_stop()
                try:
                    client.disconnect()
                except Exception:
                    pass
            except Exception as e:
                logger.error(f"MQTT SSE thread error: {e}")

        th = threading.Thread(target=_run_client, daemon=True)
        th.start()

        @stream_with_context
        def _gen():
            try:
                yield 'event: open\n' + 'data: {"success": true}\n\n'
                last_ping = 0
                import time as _t
                while True:
                    try:
                        data = msg_queue.get(timeout=0.5)
                        yield f'data: {data}\n\n'
                    except queue.Empty:
                        pass
                    now = int(_t.time())
                    if now != last_ping:
                        last_ping = now
                        yield 'event: ping\n' + 'data: {}\n\n'
            finally:
                stop_event.set()
        return Response(_gen(), mimetype='text/event-stream', headers={'Cache-Control': 'no-cache', 'X-Accel-Buffering': 'no'})
    except Exception as e:
        logger.error(f"MQTT scan SSE error: {e}")
        return jsonify({'success': False}), 200

@app.route('/api/water')
def api_water():
    """API для получения данных о расходе воды"""
    try:
        # Получаем все группы
        groups = db.get_groups()
        water_data = {}
        
        for group in groups:
            # Пропускаем группы с нестандартными ID
            if group['id'] >= 999:
                continue
                
            group_id = str(group['id'])
            
            # Генерируем данные для группы
            daily_usage = []
            total_liters = 0
            zone_usage = {}
            
            try:
                # Получаем зоны этой группы
                zones = db.get_zones_by_group(group['id'])
                
                for zone in zones:
                    # Генерируем данные для зоны
                    zone_liters = random.randint(20, 80)
                    zone_usage[str(zone['id'])] = {
                        'name': zone['name'],
                        'liters': zone_liters,
                        'last_used': (datetime.now() - timedelta(hours=random.randint(1, 24))).strftime('%Y-%m-%d %H:%M')
                    }
                    total_liters += zone_liters
                
                # Генерируем ежедневные данные
                for i in range(7):
                    date = (datetime.now() - timedelta(days=i)).strftime('%Y-%m-%d')
                    daily_usage.append({
                        'date': date,
                        'liters': random.randint(200, 1200)
                    })
                
                water_data[group_id] = {
                    'group_name': group['name'],
                    'data': {
                        'daily_usage': daily_usage,
                        'total_liters': total_liters,
                        'zone_usage': zone_usage
                    }
                }
            except Exception as e:
                logger.error(f"Ошибка обработки группы {group['id']}: {e}")
                continue
        
        return jsonify(water_data)
        
    except Exception as e:
        logger.error(f"Ошибка получения данных о воде: {e}")
        return jsonify({'error': 'Ошибка получения данных о воде'}), 500

@app.route('/api/status')
def api_status():
    # Получаем ручной переключатель дождя заранее (используется ниже)
    rain_manual = app.config.get('RAIN_MANUAL')
    
    # Получаем зоны и группы из БД
    zones = db.get_zones()
    groups = db.get_groups()
    
    # Группируем зоны по группам (исключаем группу "БЕЗ ПОЛИВА")
    zones_by_group = {}
    for zone in zones:
        group_id = zone['group_id']
        if group_id == 999:  # Пропускаем группу "БЕЗ ПОЛИВА"
            continue
        if group_id not in zones_by_group:
            zones_by_group[group_id] = []
        zones_by_group[group_id].append(zone)
    
    # Формируем статус групп
    groups_status = []
    for group in groups:
        group_id = group['id']
        if group_id == 999:  # Пропускаем группу "БЕЗ ПОЛИВА"
            continue
            
        group_zones = zones_by_group.get(group_id, [])
        
        # Определяем статус группы
        if not group_zones:
            continue  # Пропускаем группы без зон
        
        active_zones = [z for z in group_zones if z['state'] == 'on']
        # Учитываем только те зоны, у которых отложка в будущем
        postponed_zones = []
        for z in group_zones:
            pu = z.get('postpone_until')
            if not pu:
                continue
            try:
                pu_dt = datetime.strptime(pu, '%Y-%m-%d %H:%M')
                if pu_dt > datetime.now():
                    postponed_zones.append(z)
            except Exception:
                # Если формат неожиданный, считаем как отложенную
                postponed_zones.append(z)
        
        # Режим аварийной остановки имеет приоритет
        if app.config.get('EMERGENCY_STOP'):
            status = 'postponed'
            current_zone = None
        elif active_zones:
            status = 'watering'
            current_zone = active_zones[0]['id']
        elif postponed_zones:
            status = 'postponed'
            current_zone = None
        else:
            status = 'waiting'
            current_zone = None
        
        # Определяем следующее время запуска (после возможной отложки группы)
        next_start = None
        if group_zones:
            # Ищем программы полива для этой группы
            programs = db.get_programs()
            group_programs = []
            
            for program in programs:
                # Обрабатываем zones как список или JSON строку
                if isinstance(program['zones'], str):
                    program_zones = json.loads(program['zones'])
                else:
                    program_zones = program['zones']
                
                # Проверяем, есть ли зоны этой группы в программе
                group_zone_ids = [z['id'] for z in group_zones]
                if any(zone_id in group_zone_ids for zone_id in program_zones):
                    group_programs.append(program)
            
            if group_programs:
                # Базовое "сейчас"
                search_from = datetime.now()
                # Если есть отложенные зоны в группе — начинаем поиск строго ПОСЛЕ конца паузы
                try:
                    pu_candidates = []
                    for z in group_zones:
                        pu = z.get('postpone_until')
                        if pu:
                            pu_dt = datetime.strptime(pu, '%Y-%m-%d %H:%M')
                            if pu_dt > search_from:
                                pu_candidates.append(pu_dt)
                    if pu_candidates:
                        search_from = max(pu_candidates)
                except Exception:
                    pass
                best_dt = None
                for program in group_programs:
                    program_time = datetime.strptime(program['time'], '%H:%M').time()
                    program_zones_list = program['zones'] if isinstance(program['zones'], list) else json.loads(program['zones'])
                    group_zone_ids = [z['id'] for z in group_zones]
                    if not any(zid in group_zone_ids for zid in program_zones_list):
                        continue
                    prog_weekdays = set(int(d) for d in (program['days'] if isinstance(program['days'], list) else json.loads(program['days'])))
                    for add_days in range(0, 14):
                        day_date = search_from.date() + timedelta(days=add_days)
                        if ((day_date.weekday() + 0) % 7) not in prog_weekdays:
                            continue
                        dt_candidate = datetime.combine(day_date, program_time)
                        if dt_candidate > search_from and (best_dt is None or dt_candidate < best_dt):
                            best_dt = dt_candidate
                            break
                if best_dt:
                    next_start = best_dt.strftime('%H:%M')
        
        # Определяем отложенный полив
        postpone_until = None
        group_postpone_reason = None
        if app.config.get('EMERGENCY_STOP'):
            postpone_until = 'До отмены аварийной остановки'
            group_postpone_reason = 'emergency'
        elif postponed_zones:
            postpone_until = postponed_zones[0]['postpone_until']
            # Берём причину первой отложенной зоны (приоритет ручной приостановки)
            try:
                reasons = [z.get('postpone_reason') for z in postponed_zones if z.get('postpone_reason')]
                if 'manual' in reasons:
                    group_postpone_reason = 'manual'
                elif reasons:
                    group_postpone_reason = reasons[0]
            except Exception:
                pass
        elif rain_manual is True:
            # Если включен ручной дождь, откладываем полив до конца текущего дня
            postpone_until = datetime.now().strftime('%Y-%m-%d 23:59')
            status = 'postponed'
            group_postpone_reason = 'rain'
        
        groups_status.append({
            'id': group_id,
            'name': group['name'],
            'status': status,
            'current_zone': current_zone,
            'postpone_until': postpone_until,
            'next_start': next_start,
            'postpone_reason': group_postpone_reason
        })
    
    # Проверяем состояние отложенного полива
    rain_sensor_status = 'дождя нет (полив разрешен)'
    zones = db.get_zones()
    postponed_zones = [z for z in zones if z.get('postpone_until')]
    
    # Для тестирования убираем дождь
    # if postponed_zones:
    #     # Если есть отложенные зоны, показываем статус дождя
    #     rain_sensor_status = 'дождь (полив отложен)'
    
    # Отладочный ручной тумблер дождя (для тестирования)
    if rain_manual is True:
        rain_sensor_status = 'дождь (полив отложен)'
    elif rain_manual is False:
        rain_sensor_status = 'дождя нет (полив разрешен)'

    return jsonify({
        'datetime': datetime.now().strftime('%d.%m.%Y %H:%M:%S'),
        'temperature': 18,
        'humidity': 55,
        'rain_sensor': rain_sensor_status,
        'groups': groups_status,
        'emergency_stop': app.config.get('EMERGENCY_STOP', False)
    })

@csrf.exempt
@app.route('/api/postpone', methods=['POST'])
def api_postpone():
    """API для отложенного полива"""
    data = request.get_json()
    group_id = data.get('group_id')
    try:
        group_id = int(group_id)
    except Exception:
        return jsonify({"success": False, "message": "Некорректный идентификатор группы"}), 400
    days = data.get('days', 1)
    action = data.get('action')  # 'postpone' или 'cancel'
    
    if action == 'cancel':
        # Отменяем отложенный полив для всех зон группы
        zones = db.get_zones()
        group_zones = [z for z in zones if int(z.get('group_id') or 0) == int(group_id)]
        
        for zone in group_zones:
            db.update_zone_postpone(zone['id'], None, None)
        
        db.add_log('postpone_cancel', json.dumps({"group": group_id}))
        return jsonify({"success": True, "message": "Отложенный полив отменен"})
    
    elif action == 'postpone':
        # Откладываем полив на указанное количество дней
        postpone_date = datetime.now() + timedelta(days=days)
        postpone_until = postpone_date.strftime('%Y-%m-%d 23:59')
        
        zones = db.get_zones()
        group_zones = [z for z in zones if int(z.get('group_id') or 0) == int(group_id)]
        
        for zone in group_zones:
            # Фиксируем причину: ручная приостановка пользователем
            db.update_zone_postpone(zone['id'], postpone_until, 'manual')
        
        db.add_log('postpone_set', json.dumps({
            "group": group_id, 
            "days": days, 
            "until": postpone_until
        }))
        
        return jsonify({
            "success": True, 
            "message": f"Полив отложен на {days} дней",
            "postpone_until": postpone_date.strftime('%d.%m.%Y %H:%M')
        })
    
    return jsonify({"success": False, "message": "Неверное действие"}), 400

@csrf.exempt
@app.route('/api/groups/<int:group_id>/stop', methods=['POST'])
def api_stop_group(group_id):
    """Остановить все зоны в группе"""
    try:
        # Останавливаем все зоны в группе (БД и физически через MQTT)
        zones = db.get_zones()
        group_zones = [z for z in zones if z['group_id'] == group_id]
        for zone in group_zones:
            db.update_zone(zone['id'], {'state': 'off', 'watering_start_time': None})
            # Публикуем '0' в MQTT-топик зоны, если настроен
            try:
                sid = zone.get('mqtt_server_id')
                topic = (zone.get('topic') or '').strip()
                if mqtt and sid and topic:
                    t = topic if str(topic).startswith('/') else '/' + str(topic)
                    server = db.get_mqtt_server(int(sid))
                    if server:
                        _publish_mqtt_value(server, t, '0', min_interval_sec=0.0)
            except Exception:
                # Не прерываем цикл при ошибке публикации, просто логируем
                logger.exception("Ошибка публикации MQTT '0' при остановке группы")
        
        # Отменяем все активные задачи планировщика для этой группы и ставим флаг отмены
        scheduler = get_scheduler()
        if scheduler:
            scheduler.cancel_group_jobs(group_id)
 
        # Чистим плановые старты группы
        try:
            # Перестраиваем расписание: переносим на следующую программу
            db.reschedule_group_to_next_program(group_id)
        except Exception:
            pass

        db.add_log('group_stop', json.dumps({"group": group_id}))
        return jsonify({"success": True, "message": f"Группа {group_id} остановлена"})
    except Exception as e:
        logger.error(f"Ошибка остановки группы {group_id}: {e}")
        return jsonify({"success": False, "message": "Ошибка остановки группы"}), 500

@csrf.exempt
@app.route('/api/groups/<int:group_id>/start-from-first', methods=['POST'])
def api_start_group_from_first(group_id):
    """Запустить последовательный полив всей группы с первой зоны (по id)."""
    try:
        scheduler = get_scheduler()
        if not scheduler:
            return jsonify({"success": False, "message": "Планировщик недоступен"}), 500

        ok = scheduler.start_group_sequence(group_id)
        if not ok:
            return jsonify({"success": False, "message": "Не удалось запустить последовательный полив группы"}), 400

        try:
            db.add_log('group_start_from_first', json.dumps({"group": group_id}))
        except Exception:
            pass
        return jsonify({"success": True, "message": f"Группа {group_id}: запущен последовательный полив"})
    except Exception as e:
        logger.error(f"Ошибка запуска группы {group_id} с первой зоны: {e}")
        return jsonify({"success": False, "message": "Ошибка запуска группы"}), 500

@csrf.exempt
@app.route('/api/groups/<int:group_id>/start-zone/<int:zone_id>', methods=['POST'])
def api_start_zone_exclusive(group_id, zone_id):
    """Запустить зону, остановив остальные зоны этой группы"""
    try:
        if app.config.get('EMERGENCY_STOP'):
            return jsonify({"success": False, "message": "Аварийная остановка активна. Сначала отключите аварийный режим."}), 400
        # Анти-дребезг по группе
        if _should_throttle_group(int(group_id)):
            return jsonify({"success": True, "message": "Группа уже обрабатывается"})
        zones = db.get_zones()
        group_zones = [z for z in zones if z['group_id'] == group_id]
        start_ts = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        for z in group_zones:
            if z['id'] == zone_id:
                db.update_zone(z['id'], {'state': 'on', 'watering_start_time': start_ts})
                # MQTT publish ON для выбранной зоны
                try:
                    sid = z.get('mqtt_server_id')
                    topic = (z.get('topic') or '').strip()
                    if mqtt and sid and topic:
                        t = topic if str(topic).startswith('/') else '/' + str(topic)
                        server = db.get_mqtt_server(int(sid))
                        if server:
                            _publish_mqtt_value(server, t, '1')
                except Exception:
                    logger.exception("Ошибка публикации MQTT '1' при эксклюзивном запуске зоны")
            else:
                # БЕЗУСЛОВНО выключаем остальных (и MQTT OFF, и БД OFF)
                db.update_zone(z['id'], {'state': 'off', 'watering_start_time': None})
                try:
                    sid = z.get('mqtt_server_id')
                    topic = (z.get('topic') or '').strip()
                    if mqtt and sid and topic:
                        t = topic if str(topic).startswith('/') else '/' + str(topic)
                        server = db.get_mqtt_server(int(sid))
                        if server:
                            _publish_mqtt_value(server, t, '0', min_interval_sec=0.0)
                except Exception:
                    logger.exception("Ошибка публикации MQTT '0' при эксклюзивном запуске зоны")
        # Очистим плановые старты у «соседей» по группе
        try:
            db.clear_scheduled_for_zone_group_peers(zone_id, group_id)
        except Exception:
            pass
        try:
            scheduler = get_scheduler()
            if scheduler:
                scheduler.schedule_zone_stop(zone_id, int([z for z in group_zones if z['id']==zone_id][0]['duration']))
        except Exception:
            pass
        db.add_log('zone_start_exclusive', json.dumps({"group": group_id, "zone": zone_id}))
        return jsonify({"success": True, "message": f"Зона {zone_id} запущена, остальные остановлены"})
    except Exception as e:
        logger.error(f"Ошибка эксклюзивного запуска зоны {zone_id} в группе {group_id}: {e}")
        return jsonify({"success": False, "message": "Ошибка запуска зоны"}), 500

@csrf.exempt
@app.route('/api/emergency-stop', methods=['POST'])
def api_emergency_stop():
    """Аварийная остановка всех зон. До отмены полив не возобновляется."""
    try:
        zones = db.get_zones()
        # Публикуем '0' во все зоны с MQTT-настройками и выставляем статус в БД
        for zone in zones:
            db.update_zone(zone['id'], {'state': 'off'})
            try:
                sid = zone.get('mqtt_server_id')
                topic = (zone.get('topic') or '').strip()
                if mqtt and sid and topic:
                    t = topic if str(topic).startswith('/') else '/' + str(topic)
                    server = db.get_mqtt_server(int(sid))
                    if server:
                        client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2)
                        if server.get('username'):
                            client.username_pw_set(server.get('username'), server.get('password') or None)
                        client.connect(server.get('host') or '127.0.0.1', int(server.get('port') or 1883), 5)
                        client.publish(t, payload='0', qos=0, retain=False)
                        client.disconnect()
            except Exception:
                logger.exception("Ошибка публикации MQTT '0' при аварийной остановке")

        # Ставим флаг аварийной остановки
        app.config['EMERGENCY_STOP'] = True
        db.add_log('emergency_stop', json.dumps({"active": True}))

        # Останавливаем любые активные задания последовательностей для всех групп
        try:
            scheduler = get_scheduler()
            if scheduler:
                groups = db.get_groups()
                for g in groups:
                    try:
                        scheduler.cancel_group_jobs(int(g['id']))
                    except Exception:
                        pass
        except Exception:
            pass
        return jsonify({"success": True, "message": "Аварийная остановка выполнена"})
    except Exception as e:
        logger.error(f"Ошибка аварийной остановки: {e}")
        return jsonify({"success": False, "message": "Ошибка аварийной остановки"}), 500

@csrf.exempt
@app.route('/api/emergency-resume', methods=['POST'])
def api_emergency_resume():
    """Снять режим аварийной остановки"""
    try:
        app.config['EMERGENCY_STOP'] = False
        db.add_log('emergency_stop', json.dumps({"active": False}))
        return jsonify({"success": True, "message": "Полив возобновлен"})
    except Exception as e:
        logger.error(f"Ошибка возобновления после аварийной остановки: {e}")
        return jsonify({"success": False, "message": "Ошибка возобновления"}), 500

@csrf.exempt
@app.route('/api/backup', methods=['POST'])
def api_backup():
    """API для создания резервной копии"""
    try:
        backup_path = db.create_backup()
        if backup_path:
            return jsonify({
                "success": True, 
                "message": "Резервная копия создана",
                "backup_path": backup_path
            })
        else:
            return jsonify({"success": False, "message": "Ошибка создания резервной копии"}), 500
    except Exception as e:
        return jsonify({"success": False, "message": str(e)}), 500

@csrf.exempt
@app.route('/api/zones/<int:zone_id>/photo', methods=['POST'])
def upload_zone_photo(zone_id):
    """Загрузка фотографии для зоны"""
    try:
        if 'photo' not in request.files:
            return jsonify({'success': False, 'message': 'Файл не найден'}), 400
        
        file = request.files['photo']
        if file.filename == '':
            return jsonify({'success': False, 'message': 'Файл не выбран'}), 400
        
        if not allowed_file(file.filename):
            return jsonify({'success': False, 'message': 'Неподдерживаемый формат файла'}), 400
        
        # Читаем файл
        file_data = file.read()
        if len(file_data) > MAX_FILE_SIZE:
            return jsonify({'success': False, 'message': 'Файл слишком большой'}), 400
        
        # Сжимаем изображение
        compressed_data = compress_image(file_data)
        
        # Генерируем имя файла
        filename = f"zone_{zone_id}_{int(datetime.now().timestamp())}.jpg"
        filepath = os.path.join(UPLOAD_FOLDER, filename)
        
        # Сохраняем файл
        with open(filepath, 'wb') as f:
            f.write(compressed_data)
        
        # Обновляем путь к фото в базе данных (путь относительно static)
        db_relative = f"media/{ZONE_MEDIA_SUBDIR}/{filename}"
        db.update_zone_photo(zone_id, db_relative)
        
        db.add_log('photo_upload', json.dumps({"zone": zone_id, "filename": filename}))
        
        return jsonify({
            'success': True, 
            'message': 'Фотография загружена',
            'photo_path': db_relative
        })
        
    except Exception as e:
        logger.error(f"Ошибка загрузки фото: {e}")
        return jsonify({'success': False, 'message': 'Ошибка загрузки'}), 500

@csrf.exempt
@app.route('/api/zones/<int:zone_id>/photo', methods=['DELETE'])
def delete_zone_photo(zone_id):
    """Удаление фотографии зоны"""
    try:
        zone = db.get_zone(zone_id)
        if not zone:
            return jsonify({'success': False, 'message': 'Зона не найдена'}), 404
        
        if zone.get('photo_path'):
            # Удаляем файл
            # Поддерживаем старые пути 'photos/...'
            if zone['photo_path'].startswith('photos/'):
                filepath = os.path.join('static', zone['photo_path'])
            else:
                filepath = os.path.join('static', zone['photo_path'])
            if os.path.exists(filepath):
                os.remove(filepath)
            
            # Очищаем путь в базе данных
            db.update_zone_photo(zone_id, None)
            
            db.add_log('photo_delete', json.dumps({"zone": zone_id}))
            
            return jsonify({'success': True, 'message': 'Фотография удалена'})
        else:
            return jsonify({'success': False, 'message': 'Фотография не найдена'}), 404
            
    except Exception as e:
        logger.error(f"Ошибка удаления фото: {e}")
        return jsonify({'success': False, 'message': 'Ошибка удаления'}), 500

@app.route('/api/zones/<int:zone_id>/photo', methods=['GET'])
def get_zone_photo(zone_id):
    """Получить информацию о фотографии зоны или само изображение"""
    try:
        zone = db.get_zone(zone_id)
        if not zone:
            return jsonify({'success': False, 'message': 'Зона не найдена'}), 404
        
        # Проверяем, запрашивает ли клиент изображение или информацию
        accept_header = request.headers.get('Accept', '')
        
        if 'image' in accept_header or request.args.get('image') == 'true':
            # Возвращаем само изображение
            photo_path = zone.get('photo_path')
            if not photo_path:
                return jsonify({'success': False, 'message': 'Фотография не найдена'}), 404
            filepath = os.path.join('static', photo_path)
            if not os.path.exists(filepath):
                return jsonify({'success': False, 'message': 'Файл не найден'}), 404
            return send_file(filepath, mimetype='image/jpeg')
        else:
            # Возвращаем информацию о фотографии (всегда 200)
            has_photo = bool(zone.get('photo_path'))
            return jsonify({
                'success': True,
                'has_photo': has_photo,
                'photo_path': zone.get('photo_path')
            })
        
    except Exception as e:
        logger.error(f"Ошибка получения фото зоны {zone_id}: {e}")
        return jsonify({'success': False, 'message': 'Ошибка получения фото'}), 500

@csrf.exempt
@app.route('/api/zones/<int:zone_id>/start', methods=['POST'])
def start_zone(zone_id):
    """Запуск зоны полива"""
    try:
        if app.config.get('EMERGENCY_STOP'):
            return jsonify({'success': False, 'message': 'Аварийная остановка активна. Сначала отключите аварийный режим.'}), 400
        # Получаем зону и её группу
        zone = db.get_zone(zone_id)
        if not zone:
            return jsonify({'success': False, 'message': 'Зона не найдена'}), 404
        
        # При ручном старте — отменяем текущую последовательность/программу для группы зоны
        try:
            scheduler = get_scheduler()
            if scheduler:
                scheduler.cancel_group_jobs(int(zone['group_id']))
        except Exception:
            pass

        # Анти-дребезг по группе
        try:
            if _should_throttle_group(int(zone.get('group_id'))):
                return jsonify({'success': True, 'message': 'Зона уже обрабатывается'}), 200
        except Exception:
            pass

        # БЕЗУСЛОВНО выключаем все остальные зоны этой группы (MQTT OFF и БД OFF)
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
                                _publish_mqtt_value(server, t, '0', min_interval_sec=0.0)
                    except Exception:
                        logger.exception("Ошибка публикации MQTT '0' при ручном запуске: выключение соседей")
                    try:
                        db.update_zone(int(gz['id']), {'state': 'off', 'watering_start_time': None})
                    except Exception:
                        pass
        except Exception:
            pass

        # Устанавливаем время начала полива для зоны
        start_ts = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        db.update_zone(zone_id, {'state': 'on', 'watering_start_time': start_ts})
        try:
            scheduler = get_scheduler()
            if scheduler:
                # На уровне планировщика ожидание уже укорочено на N секунд (настройка)
                scheduler.schedule_zone_stop(zone_id, int(zone['duration']))
        except Exception as e:
            logger.error(f"Ошибка планирования остановки зоны {zone_id}: {e}")
        # Публикуем MQTT ON, если у зоны настроен MQTT
        try:
            sid = zone.get('mqtt_server_id')
            topic = (zone.get('topic') or '').strip()
            if mqtt and sid and topic:
                t = topic if str(topic).startswith('/') else '/' + str(topic)
                server = db.get_mqtt_server(int(sid))
                if server:
                    _publish_mqtt_value(server, t, '1')
        except Exception:
            logger.exception("Ошибка публикации MQTT '1' при ручном запуске зоны")

        db.add_log('zone_start', json.dumps({
            "zone": zone_id,
            "name": zone['name'],
            "duration": zone['duration']
        }))
        
        return jsonify({
            'success': True,
            'message': f'Зона {zone_id} запущена',
            'zone_id': zone_id,
            'state': 'on'
        })
        
    except Exception as e:
        logger.error(f"Ошибка запуска зоны {zone_id}: {e}")
        return jsonify({'success': False, 'message': 'Ошибка запуска зоны'}), 500

@csrf.exempt
@app.route('/api/zones/<int:zone_id>/stop', methods=['POST'])
def stop_zone(zone_id):
    """Остановка зоны полива"""
    try:
        zone = db.get_zone(zone_id)
        if not zone:
            return jsonify({'success': False, 'message': 'Зона не найдена'}), 404
        
        # Очищаем время начала полива и фиксируем last_watering_time
        last_time = zone.get('watering_start_time')
        db.update_zone(zone_id, {'state': 'off', 'watering_start_time': None, 'last_watering_time': last_time})
        # Публикуем MQTT OFF, если у зоны настроен MQTT
        try:
            sid = zone.get('mqtt_server_id')
            topic = (zone.get('topic') or '').strip()
            if mqtt and sid and topic:
                t = topic if str(topic).startswith('/') else '/' + str(topic)
                server = db.get_mqtt_server(int(sid))
                if server:
                    _publish_mqtt_value(server, t, '0')
        except Exception:
            logger.exception("Ошибка публикации MQTT '0' при ручной остановке зоны")

        db.add_log('zone_stop', json.dumps({
            "zone": zone_id,
            "name": zone['name']
        }))
        
        return jsonify({
            'success': True,
            'message': f'Зона {zone_id} остановлена',
            'zone_id': zone_id,
            'state': 'off'
        })
        
    except Exception as e:
        logger.error(f"Ошибка остановки зоны {zone_id}: {e}")
        return jsonify({'success': False, 'message': 'Ошибка остановки зоны'}), 500

@app.route('/api/zones/<int:zone_id>/watering-time')
def api_zone_watering_time(zone_id):
    """Возвращает оставшееся и прошедшее время полива зоны на основе watering_start_time"""
    try:
        zone = db.get_zone(zone_id)
        if not zone:
            return jsonify({'success': False, 'message': 'Зона не найдена'}), 404
        
        total_duration = int(zone.get('duration') or 0)
        start_str = zone.get('watering_start_time')
        if zone.get('state') != 'on' or not start_str:
            return jsonify({
                'success': True,
                'zone_id': zone_id,
                'is_watering': False,
                'elapsed_time': 0,
                'remaining_time': 0,
                'total_duration': total_duration,
                'elapsed_seconds': 0,
                'remaining_seconds': 0,
                'total_seconds': total_duration * 60
            })
        
        try:
            start_dt = datetime.strptime(start_str, '%Y-%m-%d %H:%M:%S')
        except Exception:
            # Если форматбитый — очищаем и возвращаем нули
            db.update_zone(zone_id, {'watering_start_time': None})
            return jsonify({
                'success': True,
                'zone_id': zone_id,
                'is_watering': False,
                'elapsed_time': 0,
                'remaining_time': 0,
                'total_duration': total_duration,
                'elapsed_seconds': 0,
                'remaining_seconds': 0,
                'total_seconds': total_duration * 60
            })
        
        now = datetime.now()
        elapsed_seconds = max(0, int((now - start_dt).total_seconds()))
        total_seconds = int(total_duration * 60)
        if elapsed_seconds >= total_seconds:
            # Автостоп
            db.update_zone(zone_id, {'state': 'off', 'watering_start_time': None})
            return jsonify({
                'success': True,
                'zone_id': zone_id,
                'is_watering': False,
                'elapsed_time': total_duration,
                'remaining_time': 0,
                'total_duration': total_duration,
                'elapsed_seconds': total_seconds,
                'remaining_seconds': 0,
                'total_seconds': total_seconds
            })
        remaining_seconds = max(0, total_seconds - elapsed_seconds)
        # Для обратной совместимости оставляем минутные поля (целые минуты)
        elapsed_min = int(elapsed_seconds // 60)
        remaining_min = int(remaining_seconds // 60)
        return jsonify({
            'success': True,
            'zone_id': zone_id,
            'is_watering': True,
            'elapsed_time': elapsed_min,
            'remaining_time': remaining_min,
            'total_duration': total_duration,
            'elapsed_seconds': elapsed_seconds,
            'remaining_seconds': remaining_seconds,
            'total_seconds': total_seconds
        })
    except Exception as e:
        logger.error(f"Ошибка получения времени полива зоны {zone_id}: {e}")
        return jsonify({'success': False, 'message': 'Ошибка получения времени полива'}), 500

@app.route('/api/mqtt/zones-sse')
def api_mqtt_zones_sse():
    if mqtt is None:
        return jsonify({'success': False, 'message': 'paho-mqtt not installed'}), 200
    try:
        zones = db.get_zones()
        server_topics = {}
        for z in zones:
            sid = z.get('mqtt_server_id')
            topic = (z.get('topic') or '').strip()
            if not sid or not topic:
                continue
            t = topic if str(topic).startswith('/') else '/' + str(topic)
            server_topics.setdefault(int(sid), {}).setdefault(t, []).append(z['id'])
        if not server_topics:
            return jsonify({'success': False, 'message': 'no zone topics'}), 200
        msg_queue = queue.Queue(maxsize=10000)
        def _make_on_message(sid):
            def _on_message(cl, userdata, msg):
                t = str(getattr(msg, 'topic', '') or '')
                if not t.startswith('/'): t = '/' + t
                zone_ids = server_topics.get(sid, {}).get(t)
                if not zone_ids:
                    return
                try:
                    payload = msg.payload.decode('utf-8', errors='ignore').strip()
                except Exception:
                    payload = str(msg.payload)
                new_state = 'on' if payload in ('1', 'true', 'ON', 'on') else 'off'
                # Если активен режим аварийной остановки, принудительно гасим любые включения
                if app.config.get('EMERGENCY_STOP') and new_state == 'on':
                    new_state = 'off'
                    try:
                        server = db.get_mqtt_server(int(sid))
                        if server:
                            _publish_mqtt_value(server, t, '0')
                    except Exception:
                        pass
                try:
                    logger.info(f"MQTT RX sid={sid} topic={t} payload={payload} -> state={new_state} zones={zone_ids}")
                except Exception:
                    pass
                for zone_id in zone_ids:
                    try:
                        z = db.get_zone(zone_id) or {}
                        updates = {'state': new_state}
                        if new_state == 'on':
                            if not z.get('watering_start_time'):
                                updates['watering_start_time'] = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
                        else:
                            # record last_watering_time
                            if z.get('watering_start_time'):
                                updates['last_watering_time'] = z.get('watering_start_time')
                            updates['watering_start_time'] = None
                        db.update_zone(zone_id, updates)
                        try:
                            logger.info(f"DB state update from MQTT zone={zone_id} -> {new_state}")
                        except Exception:
                            pass
                    except Exception:
                        pass
                    data = json.dumps({'zone_id': zone_id, 'topic': t, 'payload': payload, 'state': new_state})
                    try:
                        msg_queue.put_nowait(data)
                    except queue.Full:
                        pass
            return _on_message
        clients = []
        max_clients = 10
        for sid, topics in server_topics.items():
            if len(clients) >= max_clients:
                break
            server = db.get_mqtt_server(sid)
            try:
                client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id=(server.get('client_id') or None))
                if server.get('username'):
                    client.username_pw_set(server.get('username'), server.get('password') or None)
                client.on_message = _make_on_message(sid)
                client.connect(server.get('host') or '127.0.0.1', int(server.get('port') or 1883), 5)
                for t in topics.keys():
                    try:
                        client.subscribe(t, qos=0)
                    except Exception:
                        pass
                client.loop_start()
                clients.append(client)
            except Exception:
                continue
        @stream_with_context
        def _gen():
            try:
                yield 'event: open\n' + 'data: {}\n\n'
                while True:
                    try:
                        data = msg_queue.get(timeout=0.5)
                        yield f'data: {data}\n\n'
                    except queue.Empty:
                        yield 'event: ping\n' + 'data: {}\n\n'
            finally:
                for c in clients:
                    try:
                        c.loop_stop()
                        c.disconnect()
                    except Exception:
                        pass
        return Response(_gen(), mimetype='text/event-stream', headers={'Cache-Control': 'no-cache', 'X-Accel-Buffering': 'no'})
    except Exception as e:
        logger.error(f"zones SSE failed: {e}")
        return jsonify({'success': False}), 200

@csrf.exempt
@app.route('/api/zones/<int:zone_id>/mqtt/start', methods=['POST'])
def api_zone_mqtt_start(zone_id: int):
    z = db.get_zone(zone_id)
    if not z: return jsonify({'success': False}), 404
    # Если уже включена — не публикуем повторно
    try:
        if str(z.get('state')) == 'on':
            return jsonify({'success': True, 'message': 'Зона уже запущена'})
    except Exception:
        pass
    # Анти-дребезг по группе
    try:
        gid = int(z.get('group_id') or 0)
        if gid and _should_throttle_group(gid):
            return jsonify({'success': True, 'message': 'Группа уже обрабатывается'})
    except Exception:
        pass
    # Эксклюзивность: БЕЗУСЛОВНО выключаем все остальные зоны в группе (и MQTT OFF, и БД OFF)
    try:
        group_id = int(z.get('group_id') or 0)
        if group_id:
            group_zones = db.get_zones_by_group(group_id)
            for other in group_zones:
                if int(other.get('id')) == int(zone_id):
                    continue
                try:
                    osid = other.get('mqtt_server_id'); otopic = (other.get('topic') or '').strip()
                    if osid and otopic:
                        server_o = db.get_mqtt_server(int(osid))
                        if server_o:
                            t_o = otopic if str(otopic).startswith('/') else '/' + str(otopic)
                            _publish_mqtt_value(server_o, t_o, '0')
                except Exception:
                    logger.exception("Ошибка публикации MQTT '0' при MQTT-старте: выключение соседей")
                try:
                    db.update_zone(int(other.get('id')), {
                        'state': 'off',
                        'watering_start_time': None,
                        'last_watering_time': other.get('watering_start_time')
                    })
                except Exception:
                    pass
            # Прерываем возможную последовательность/программу этой группы
            try:
                scheduler = get_scheduler()
                if scheduler:
                    scheduler.cancel_group_jobs(group_id)
            except Exception:
                pass
    except Exception:
        pass
    sid = z.get('mqtt_server_id'); topic = (z.get('topic') or '').strip()
    if not sid or not topic: return jsonify({'success': False, 'message': 'No MQTT config for zone'}), 400
    t = topic if str(topic).startswith('/') else '/' + str(topic)
    try:
        server = db.get_mqtt_server(int(sid))
        if not server:
            return jsonify({'success': False, 'message': 'MQTT server not found'}), 400
        logger.info(f"HTTP publish ON zone={zone_id} topic={t}")
        _publish_mqtt_value(server, t, '1')
        # Фиксируем старт зоны и планируем автостоп
        start_ts = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        try:
            db.update_zone(zone_id, {'state': 'on', 'watering_start_time': start_ts, 'scheduled_start_time': None})
        except Exception:
            pass
        try:
            scheduler = get_scheduler()
            if scheduler:
                duration_min = int(z.get('duration') or 0)
                if duration_min > 0:
                    scheduler.schedule_zone_stop(zone_id, duration_min)
        except Exception:
            pass
        return jsonify({'success': True, 'message': 'Зона запущена'})
    except Exception as e:
        logger.error(f"MQTT publish start failed: {e}")
        return jsonify({'success': False, 'message': 'MQTT publish failed'}), 500

@csrf.exempt
@app.route('/api/zones/<int:zone_id>/mqtt/stop', methods=['POST'])
def api_zone_mqtt_stop(zone_id: int):
    z = db.get_zone(zone_id)
    if not z: return jsonify({'success': False}), 404
    sid = z.get('mqtt_server_id'); topic = (z.get('topic') or '').strip()
    if not sid or not topic: return jsonify({'success': False, 'message': 'No MQTT config for zone'}), 400
    t = topic if str(topic).startswith('/') else '/' + str(topic)
    try:
        server = db.get_mqtt_server(int(sid))
        if not server:
            return jsonify({'success': False, 'message': 'MQTT server not found'}), 400
        logger.info(f"HTTP publish OFF zone={zone_id} topic={t}")
        _publish_mqtt_value(server, t, '0')
        return jsonify({'success': True})
    except Exception as e:
        logger.error(f"MQTT publish stop failed: {e}")
        return jsonify({'success': False, 'message': 'MQTT publish failed'}), 500

if __name__ == '__main__':
    # Инициализируем планировщик полива
    init_scheduler(db)
    
    app.run(debug=True, host='0.0.0.0', port=8080)
