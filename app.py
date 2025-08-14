from flask import Flask, render_template, jsonify, request, send_file, redirect, url_for, session
from datetime import datetime, timedelta
import random
import json
from database import db
import os
from werkzeug.utils import secure_filename
from PIL import Image
import io
import logging
from irrigation_scheduler import init_scheduler, get_scheduler
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
except Exception:
    pass

app = Flask(__name__)
app.db = db  # Добавляем атрибут db для тестов
app.config['EMERGENCY_STOP'] = False
app.secret_key = os.environ.get('SECRET_KEY', 'wb-irrigation-secret')
if os.environ.get('TESTING'):
    app.config['TESTING'] = True

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

# Генерация демо-данных расхода воды
def generate_water_data(days, zone):
    labels, data, log_rows = [], [], []
    for i in range(days - 1, -1, -1):
        d = datetime.now() - timedelta(days=i)
        day = d.strftime('%Y-%m-%d')
        labels.append(f"{d.day:02d}.{d.month:02d}")
        usage = random.randint(10, 60)
        data.append(usage)
        log_rows.append({
            'date': day,
            'zone': 'all' if zone == 'all' else zone,
            'usage': usage
        })
    return {'labels': labels, 'data': data, 'logRows': log_rows}

def login_required(view):
    def wrapper(*args, **kwargs):
        if app.config.get('TESTING'):
            return view(*args, **kwargs)
        if not session.get('logged_in'):
            return redirect(url_for('login'))
        return view(*args, **kwargs)
    wrapper.__name__ = view.__name__
    return wrapper


_SCHEDULER_INIT_DONE = False

@app.before_request
def _init_scheduler_before_request():
    global _SCHEDULER_INIT_DONE
    if not _SCHEDULER_INIT_DONE and not app.config.get('TESTING'):
        try:
            init_scheduler(db)
            _SCHEDULER_INIT_DONE = True
        except Exception as e:
            logger.error(f"Ошибка инициализации планировщика: {e}")


@app.route('/login', methods=['GET'])
def login():
    return render_template('login.html')


@app.route('/')
@login_required
def index():
    return render_template('status.html')

@app.route('/status')
@login_required
def status():
    return render_template('status.html')

@app.route('/zones')
@login_required
def zones_page():
    return render_template('zones.html')

@app.route('/programs')
@login_required
def programs_page():
    return render_template('programs.html')

@app.route('/logs')
@login_required
def logs_page():
    return render_template('logs.html')

@app.route('/water')
@login_required
def water_page():
    return render_template('water.html')

# Карта зон
MAP_FOLDER = MAP_DIR  # использовать новый каталог media/maps

@app.route('/map')
@login_required
def map_page():
    return render_template('map.html')


@app.route('/api/login', methods=['POST'])
def api_login():
    try:
        data = request.get_json() or {}
        password = data.get('password', '')
        stored_hash = db.get_password_hash()
        if stored_hash and check_password_hash(stored_hash, password):
            session['logged_in'] = True
            return jsonify({'success': True})
        return jsonify({'success': False, 'message': 'Неверный пароль'}), 401
    except Exception as e:
        logger.error(f"Ошибка входа: {e}")
        return jsonify({'success': False, 'message': 'Ошибка входа'}), 500


@app.route('/api/auth/status')
def api_auth_status():
    return jsonify({'authenticated': bool(session.get('logged_in')) or bool(app.config.get('TESTING'))})


@app.route('/logout', methods=['GET'])
def api_logout():
    session.clear()
    return redirect(url_for('login'))


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
            files = [f for f in os.listdir(MAP_FOLDER) if os.path.isfile(os.path.join(MAP_FOLDER, f))]
            if files:
                return jsonify({'success': True, 'path': f"media/maps/{files[0]}"})
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
        data = request.get_json()
        zone = db.update_zone(zone_id, data)
        if zone:
            db.add_log('zone_edit', json.dumps({"zone": zone_id, "changes": data}))
            return jsonify(zone)
        return ('Zone not found', 404)
    
    elif request.method == 'DELETE':
        if db.delete_zone(zone_id):
            db.add_log('zone_delete', json.dumps({"zone": zone_id}))
            return ('', 204)
        return ('Zone not found', 404)

@app.route('/api/zones', methods=['POST'])
def api_create_zone():
    data = request.get_json()
    zone = db.create_zone(data)
    if zone:
        db.add_log('zone_create', json.dumps({"zone": zone['id'], "name": zone['name']}))
        return jsonify(zone), 201
    return ('Error creating zone', 400)

@app.route('/api/groups')
def api_groups():
    groups = db.get_groups()
    return jsonify(groups)

@app.route('/api/groups/<int:group_id>', methods=['PUT'])
def api_update_group(group_id):
    data = request.get_json()
    if db.update_group(group_id, data['name']):
        db.add_log('group_edit', json.dumps({"group": group_id, "name": data['name']}))
        return jsonify({"success": True})
    return ('Group not found', 404)

@app.route('/api/groups', methods=['POST'])
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
        data = request.get_json()
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
    data = request.get_json()
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
        
        # Определяем следующее время запуска
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
                # Вычислим ближайшее стартовое время среди программ на 14 дней
                now = datetime.now()
                best_dt = None
                for program in group_programs:
                    program_time = datetime.strptime(program['time'], '%H:%M').time()
                    program_zones_list = program['zones'] if isinstance(program['zones'], list) else json.loads(program['zones'])
                    group_zone_ids = [z['id'] for z in group_zones]
                    if not any(zid in group_zone_ids for zid in program_zones_list):
                        continue
                    prog_weekdays = set(int(d) for d in (program['days'] if isinstance(program['days'], list) else json.loads(program['days'])))
                    for add_days in range(0, 14):
                        day_date = now.date() + timedelta(days=add_days)
                        if ((day_date.weekday() + 0) % 7) not in prog_weekdays:
                            continue
                        dt_candidate = datetime.combine(day_date, program_time)
                        if dt_candidate > now and (best_dt is None or dt_candidate < best_dt):
                            best_dt = dt_candidate
                            break
                if best_dt:
                    next_start = best_dt.strftime('%H:%M')
        
        # Определяем отложенный полив
        postpone_until = None
        if app.config.get('EMERGENCY_STOP'):
            postpone_until = 'До отмены аварийной остановки'
        elif postponed_zones:
            postpone_until = postponed_zones[0]['postpone_until']
        elif rain_manual is True:
            # Если включен ручной дождь, откладываем полив до конца текущего дня
            postpone_until = datetime.now().strftime('%Y-%m-%d 23:59')
            status = 'postponed'
        
        groups_status.append({
            'id': group_id,
            'name': group['name'],
            'status': status,
            'current_zone': current_zone,
            'postpone_until': postpone_until,
            'next_start': next_start
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

@app.route('/api/postpone', methods=['POST'])
def api_postpone():
    """API для отложенного полива"""
    data = request.get_json()
    group_id = data.get('group_id')
    days = data.get('days', 1)
    action = data.get('action')  # 'postpone' или 'cancel'
    
    if action == 'cancel':
        # Отменяем отложенный полив для всех зон группы
        zones = db.get_zones()
        group_zones = [z for z in zones if z['group_id'] == group_id]
        
        for zone in group_zones:
            db.update_zone_postpone(zone['id'], None)
        
        db.add_log('postpone_cancel', json.dumps({"group": group_id}))
        return jsonify({"success": True, "message": "Отложенный полив отменен"})
    
    elif action == 'postpone':
        # Откладываем полив на указанное количество дней
        postpone_date = datetime.now() + timedelta(days=days)
        postpone_until = postpone_date.strftime('%Y-%m-%d 23:59')
        
        zones = db.get_zones()
        group_zones = [z for z in zones if z['group_id'] == group_id]
        
        for zone in group_zones:
            db.update_zone_postpone(zone['id'], postpone_until)
        
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

@app.route('/api/groups/<int:group_id>/stop', methods=['POST'])
def api_stop_group(group_id):
    """Остановить все зоны в группе"""
    try:
        zones = db.get_zones()
        group_zones = [z for z in zones if z['group_id'] == group_id]
        for zone in group_zones:
            db.update_zone(zone['id'], {'state': 'off'})
        db.add_log('group_stop', json.dumps({"group": group_id}))
        return jsonify({"success": True, "message": f"Группа {group_id} остановлена"})
    except Exception as e:
        logger.error(f"Ошибка остановки группы {group_id}: {e}")
        return jsonify({"success": False, "message": "Ошибка остановки группы"}), 500

@app.route('/api/groups/<int:group_id>/start-zone/<int:zone_id>', methods=['POST'])
def api_start_zone_exclusive(group_id, zone_id):
    """Запустить зону, остановив остальные зоны этой группы"""
    try:
        if app.config.get('EMERGENCY_STOP'):
            return jsonify({"success": False, "message": "Аварийная остановка активна. Сначала отключите аварийный режим."}), 400
        zones = db.get_zones()
        group_zones = [z for z in zones if z['group_id'] == group_id]
        for z in group_zones:
            new_state = 'on' if z['id'] == zone_id else 'off'
            db.update_zone(z['id'], {'state': new_state})
        db.add_log('zone_start_exclusive', json.dumps({"group": group_id, "zone": zone_id}))
        return jsonify({"success": True, "message": f"Зона {zone_id} запущена, остальные остановлены"})
    except Exception as e:
        logger.error(f"Ошибка эксклюзивного запуска зоны {zone_id} в группе {group_id}: {e}")
        return jsonify({"success": False, "message": "Ошибка запуска зоны"}), 500

@app.route('/api/emergency-stop', methods=['POST'])
def api_emergency_stop():
    """Аварийная остановка всех зон. До отмены полив не возобновляется."""
    try:
        zones = db.get_zones()
        for zone in zones:
            db.update_zone(zone['id'], {'state': 'off'})
        app.config['EMERGENCY_STOP'] = True
        db.add_log('emergency_stop', json.dumps({"active": True}))
        return jsonify({"success": True, "message": "Аварийная остановка выполнена"})
    except Exception as e:
        logger.error(f"Ошибка аварийной остановки: {e}")
        return jsonify({"success": False, "message": "Ошибка аварийной остановки"}), 500

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

@app.route('/api/zones/<int:zone_id>/start', methods=['POST'])
def start_zone(zone_id):
    """Запуск зоны полива"""
    try:
        if app.config.get('EMERGENCY_STOP'):
            return jsonify({'success': False, 'message': 'Аварийная остановка активна. Сначала отключите аварийный режим.'}), 400
        zone = db.get_zone(zone_id)
        if not zone:
            return jsonify({'success': False, 'message': 'Зона не найдена'}), 404
        
        db.update_zone(zone_id, {'state': 'on'})
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

@app.route('/api/zones/<int:zone_id>/stop', methods=['POST'])
def stop_zone(zone_id):
    """Остановка зоны полива"""
    try:
        zone = db.get_zone(zone_id)
        if not zone:
            return jsonify({'success': False, 'message': 'Зона не найдена'}), 404
        
        db.update_zone(zone_id, {'state': 'off'})
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

if __name__ == '__main__':
    # Инициализируем планировщик полива
    init_scheduler(db)
    
    app.run(debug=True, host='0.0.0.0', port=8080)
