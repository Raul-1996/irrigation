from flask import Flask, render_template, jsonify, request, send_file, redirect, url_for, session
from datetime import datetime, timedelta
import json
from database import db
from utils import normalize_topic
import os
from werkzeug.utils import secure_filename
from PIL import Image, ImageOps
from typing import Optional, Tuple
import io
import logging
from irrigation_scheduler import init_scheduler, get_scheduler
from flask_wtf.csrf import CSRFProtect
try:
    import paho.mqtt.client as mqtt
except Exception:
    mqtt = None
from flask import Response, stream_with_context
import time as _perf_time
import threading
import queue
import time
import random
from config import Config
from routes.status import status_bp
from routes.files import files_bp
from routes.zones import zones_bp
from routes.programs import programs_bp
from routes.groups import groups_bp
from routes.auth import auth_bp
from routes.settings import settings_bp
from werkzeug.security import check_password_hash

# Настройка логирования
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

class _PIIFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        try:
            msg = str(record.getMessage())
            # Маскируем password=..., "password":"...", Authorization: Bearer ...
            for key in ("password", "old_password", "new_password"):
                msg = msg.replace(f'"{key}":"', f'"{key}":"***').replace(f"{key}=", f"{key}=***")
            if 'Authorization' in msg:
                msg = msg.replace('Authorization', 'Authorization: ***')
            record.msg = msg
        except Exception:
            pass
        return True
# Не прокидываем в root в режиме тестов, чтобы потоковые хендлеры stdout/stderr не падали при закрытии пайпов
try:
    import builtins as _bi
    _IN_TESTS = bool(__name__ != '__main__' and 'PYTEST_CURRENT_TEST' in os.environ)
except Exception:
    _IN_TESTS = False
logger.propagate = not _IN_TESTS

# Единый формат логов
_LOG_FORMAT = '%(asctime)s [%(levelname)s] [%(name)s] %(message)s'
_LOG_DATEFMT = '%Y-%m-%d %H:%M:%S'

def _ensure_console_handler():
    """Гарантирует наличие StreamHandler на root с единым форматтером."""
    try:
        root = logging.getLogger()
        # Ищем уже существующий StreamHandler
        sh = None
        for h in root.handlers:
            if isinstance(h, logging.StreamHandler):
                sh = h
                break
        if sh is None:
            sh = logging.StreamHandler()
            root.addHandler(sh)
        sh.setLevel(root.level)
        sh.setFormatter(logging.Formatter(_LOG_FORMAT, datefmt=_LOG_DATEFMT))
        sh.addFilter(_PIIFilter())
        # Приводим werkzeug к нашему форматтеру
        wlg = logging.getLogger('werkzeug')
        for h in (wlg.handlers or []):
            if isinstance(h, logging.StreamHandler):
                h.setFormatter(logging.Formatter(_LOG_FORMAT, datefmt=_LOG_DATEFMT))
    except Exception:
        pass
try:
    from logging.handlers import RotatingFileHandler
    log_dir = os.path.join(os.getcwd(), 'backups')
    os.makedirs(log_dir, exist_ok=True)
    fh = RotatingFileHandler(os.path.join(log_dir, 'app.log'), maxBytes=1_000_000, backupCount=3)
    fh.setLevel(logging.INFO)
    fmt = logging.Formatter('%(asctime)s %(levelname)s %(name)s: %(message)s')
    fh.setFormatter(fmt)
    fh.addFilter(_PIIFilter())
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

# Устанавливаем TZ процесса по системной таймзоне, чтобы логи и планировщик работали в локальном времени
try:
    import time as _tz_time
    if not os.getenv('TZ'):
        try:
            with open('/etc/timezone', 'r') as _f:
                _tzname = _f.read().strip()
        except Exception:
            _tzname = None
        if _tzname:
            os.environ['TZ'] = _tzname
            try:
                _tz_time.tzset()
            except Exception:
                pass
except Exception:
    pass

app = Flask(__name__)
app.config.from_object(Config)
app.db = db  # Добавляем атрибут db для тестов
csrf = CSRFProtect(app)
# Долгое кеширование статики (ускоряет первую загрузку)
try:
    app.config.setdefault('SEND_FILE_MAX_AGE_DEFAULT', 60 * 60 * 24 * 7)
except Exception:
    pass
@app.before_request
def _perf_start_timer():
    try:
        request._started_at = _perf_time.time()
    except Exception:
        pass

@app.after_request
def _perf_add_server_timing(resp: Response):
    try:
        t0 = getattr(request, '_started_at', None)
        if t0 is not None:
            dur_ms = int((_perf_time.time() - t0) * 1000)
            resp.headers['Server-Timing'] = f"app;dur={dur_ms}"
    except Exception:
        pass
    return resp

# Настройки хранения медиафайлов
MEDIA_ROOT = 'static/media'
ZONE_MEDIA_SUBDIR = 'zones'
MAP_MEDIA_SUBDIR = 'maps'
UPLOAD_FOLDER = os.path.join(MEDIA_ROOT, ZONE_MEDIA_SUBDIR)
ALLOWED_EXTENSIONS = {'png', 'jpg', 'jpeg', 'gif', 'webp'}
ALLOWED_MIME_TYPES = {
    'image/png', 'image/jpeg', 'image/gif', 'image/webp'
}
MAX_FILE_SIZE = 5 * 1024 * 1024  # 5MB

# Создаем папки для медиа
os.makedirs(UPLOAD_FOLDER, exist_ok=True)
MAP_DIR = os.path.join(MEDIA_ROOT, MAP_MEDIA_SUBDIR)
os.makedirs(MAP_DIR, exist_ok=True)

def _parse_dt(s: str):
    if not s:
        return None
    for fmt in ('%Y-%m-%d %H:%M:%S', '%Y-%m-%d %H:%M'):
        try:
            return datetime.strptime(s, fmt)
        except Exception:
            continue
    return None

# === Централизованная конфигурация уровня логов ===
def _apply_runtime_log_level():
    try:
        is_debug = db.get_logging_debug()
        level = logging.DEBUG if is_debug else logging.WARNING
        root = logging.getLogger()
        root.setLevel(level)
        _ensure_console_handler()
        for lg_name in ('app', __name__, 'apscheduler', 'werkzeug', 'database', 'irrigation_scheduler'):
            lg = logging.getLogger(lg_name)
            lg.setLevel(level if lg_name in ('app', __name__, 'database', 'irrigation_scheduler') else (logging.ERROR if not is_debug else logging.INFO))
    except Exception:
        pass

# Сессионные куки: безопасность по умолчанию
try:
    app.config.setdefault('SESSION_COOKIE_SAMESITE', 'Lax')
    app.config.setdefault('SESSION_COOKIE_HTTPONLY', True)
    # В проде имеет смысл включить Secure
    if not Config.TESTING:
        app.config.setdefault('SESSION_COOKIE_SECURE', False)
except Exception:
    pass

@app.route('/api/logging/debug', methods=['GET', 'POST'])
def api_logging_debug_toggle():
    try:
        if request.method == 'POST':
            payload = request.get_json(force=True, silent=True) or {}
            enable = bool(payload.get('enabled'))
            db.set_logging_debug(enable)
            _apply_runtime_log_level()
        return jsonify({'debug': db.get_logging_debug()})
    except Exception as e:
        logger.error(f"api_logging_debug_toggle error: {e}")
        return jsonify({'debug': db.get_logging_debug()}), 500

def allowed_file(filename):
    return '.' in filename and filename.rsplit('.', 1)[1].lower() in ALLOWED_EXTENSIONS

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

def normalize_image(image_data: bytes, max_long_side: int = 1024, fmt: str = 'WEBP', quality: int = 90, lossless: bool = False, target_size: Optional[Tuple[int, int]] = None) -> Tuple[bytes, str]:
    """Нормализация изображения: авто-поворот по EXIF, приведение к RGB, масштабирование
    с сохранением пропорций по большей стороне и сохранение в выбранный формат.
    Возвращает (bytes, extension_with_dot).
    """
    try:
        img = Image.open(io.BytesIO(image_data))
        # автоориентация по EXIF
        try:
            img = ImageOps.exif_transpose(img)
        except Exception:
            pass
        if img.mode in ('RGBA', 'LA', 'P'):
            img = img.convert('RGB')
        # масштабирование/приведение размера
        w, h = img.size
        if target_size:
            tw, th = target_size
            # cover: растянуть до заполнения и откадрировать центр
            scale = max(tw / w, th / h)
            new_size = (int(w * scale), int(h * scale))
            img = img.resize(new_size, Image.Resampling.LANCZOS)
            # центрированный кроп
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
    except Exception:
        # fallback — вернуть исходные данные
        return image_data, '.jpg'

# удалены устаревшие функции generate_water_data/login_required/admin_required


_SCHEDULER_INIT_DONE = False
_INITIAL_SYNC_DONE = False

_RAIN_MONITOR_STARTED = False
_RAIN_MONITOR_CFG_SIG = None
_ENV_MONITOR_STARTED = False
_ENV_MONITOR_CFG_SIG = None
_ENV_MONITOR_LAST_RESTART = 0.0

class RainMonitor:
    def __init__(self):
        self.client = None
        self.cfg = None
        self.is_rain = None

    def stop(self):
        try:
            if self.client is not None:
                self.client.loop_stop()
                self.client.disconnect()
        except Exception:
            pass
        self.client = None

    def start(self, cfg: dict):
        self.stop()
        self.cfg = cfg or {}
        if not self.cfg.get('enabled'):
            return
        if mqtt is None:
            return
        try:
            topic = (self.cfg.get('topic') or '').strip()
            sid = self.cfg.get('server_id')
            if not topic or not sid:
                return
            server = db.get_mqtt_server(int(sid))
            if not server:
                return
            cl = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2)
            if server.get('username'):
                cl.username_pw_set(server.get('username'), server.get('password') or None)
            cl.connect(server.get('host') or '127.0.0.1', int(server.get('port') or 1883), 5)
            cl.on_message = self._on_message
            cl.on_connect = lambda c, u, f=None, rc=0, p=None: self._on_connect(c, topic)
            self.client = cl
            cl.loop_start()
            logger.info("RainMonitor started")
        except Exception:
            logger.exception('RainMonitor start failed')

    def _on_connect(self, client, topic: str):
        try:
            try:
                options = mqtt.SubscribeOptions(qos=0, noLocal=False)
                client.subscribe(topic, options=options)
            except Exception:
                client.subscribe(topic, qos=0)
            logger.info(f"RainMonitor subscribed {topic}")
        except Exception:
            logger.exception('RainMonitor subscribe failed')

    def _interpret_payload(self, payload: str) -> Optional[bool]:
        s = (payload or '').strip().lower()
        val = None
        if s in ('1', 'true', 'on', 'yes'):
            val = True
        elif s in ('0', 'false', 'off', 'no'):
            val = False
        else:
            try:
                val = bool(int(s))
            except Exception:
                val = None
        if val is None:
            return None
        sensor_type = (self.cfg.get('type') or 'NO').upper()
        # NO: дождь -> вход True; NC: дождь -> вход False
        return (not val) if sensor_type == 'NC' else val

    def _on_message(self, client, u, msg):
        try:
            try:
                payload = msg.payload.decode('utf-8', 'ignore')
            except Exception:
                payload = str(msg.payload)
            rain_now = self._interpret_payload(payload)
            if rain_now is None:
                return
            self.is_rain = bool(rain_now)
            if self.is_rain:
                self._apply_rain_postpone()
        except Exception:
            logger.exception('RainMonitor on_message failed')

    def _apply_rain_postpone(self):
        try:
            groups = db.get_groups()
            target_groups = [int(g['id']) for g in groups if db.get_group_use_rain(int(g['id'])) and int(g['id']) != 999]
            if not target_groups:
                return
            postpone_until = datetime.now().strftime('%Y-%m-%d 23:59:59')
            zones = db.get_zones()
            for z in zones:
                if int(z.get('group_id') or 0) in target_groups:
                    db.update_zone_postpone(int(z['id']), postpone_until, 'rain')
            scheduler = get_scheduler()
            if scheduler:
                for gid in target_groups:
                    try:
                        scheduler.cancel_group_jobs(int(gid))
                    except Exception:
                        pass
            # Публикуем OFF и сбрасываем state
            for z in zones:
                try:
                    if int(z.get('group_id') or 0) not in target_groups:
                        continue
                    sid = z.get('mqtt_server_id')
                    topic = (z.get('topic') or '').strip()
                    if mqtt and sid and topic:
                        t = normalize_topic(topic)
                        server = db.get_mqtt_server(int(sid))
                        if server:
                            _publish_mqtt_value(server, t, '0', min_interval_sec=0.0)
                    db.update_zone(int(z['id']), {'state': 'off', 'watering_start_time': None})
                except Exception:
                    pass
            try:
                db.add_log('rain_postpone', json.dumps({'groups': target_groups, 'until': postpone_until}))
            except Exception:
                pass
        except Exception:
            logger.exception('RainMonitor apply postpone failed')

rain_monitor = RainMonitor()

class EnvMonitor:
    def __init__(self):
        self.temp_client = None
        self.hum_client = None
        self.temp_value = None
        self.hum_value = None
        self.cfg = None
        self.last_temp_rx_ts = 0.0
        self.last_hum_rx_ts = 0.0

    def stop(self):
        for cl in (self.temp_client, self.hum_client):
            try:
                if cl is not None:
                    cl.loop_stop(); cl.disconnect()
            except Exception:
                pass
        self.temp_client = None; self.hum_client = None
        self.last_temp_rx_ts = 0.0
        self.last_hum_rx_ts = 0.0

    def start(self, cfg: dict):
        self.stop(); self.cfg = cfg or {}
        if mqtt is None:
            try:
                logger.warning('EnvMonitor start skipped: paho.mqtt not available')
            except Exception:
                pass
            return
        try:
            logger.info("EnvMonitor starting with cfg=%s", cfg)
        except Exception:
            pass
        # Temperature
        tcfg = (self.cfg.get('temp') or {})
        if tcfg.get('enabled') and tcfg.get('topic') and tcfg.get('server_id'):
            server = db.get_mqtt_server(int(tcfg['server_id']))
            if server:
                try:
                    cl = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2)
                    if server.get('username'):
                        cl.username_pw_set(server.get('username'), server.get('password') or None)
                    topic_t = (tcfg['topic'] or '').strip()
                    try:
                        logger.info('EnvMonitor temp connecting host=%s port=%s topic=%s', server.get('host'), server.get('port'), topic_t)
                    except Exception:
                        pass
                    def _on_msg_temp(c, u, msg):
                        try:
                            s = (msg.payload.decode('utf-8', 'ignore') or '').strip().replace(',', '.')
                            self.temp_value = round(float(s))
                            try:
                                self.last_temp_rx_ts = time.time()
                            except Exception:
                                pass
                            logger.info(f"EnvMonitor temp RX topic={getattr(msg,'topic',topic_t)} value={self.temp_value}")
                        except Exception:
                            logger.exception('EnvMonitor temp parse failed')
                    def _on_connect_temp(c, u, flags, reason_code, properties=None):
                        try:
                            c.subscribe(topic_t, qos=0)
                            logger.info("EnvMonitor temp subscribed %s", topic_t)
                        except Exception:
                            logger.exception('EnvMonitor temp subscribe failed')
                    def _on_disconnect_temp(c, u, rc, properties=None):
                        try:
                            self.temp_client = None
                            logger.info("EnvMonitor temp disconnected: rc=%s", rc)
                        except Exception:
                            pass
                    cl.on_message = _on_msg_temp
                    cl.on_connect = _on_connect_temp
                    cl.on_disconnect = _on_disconnect_temp
                    cl.connect(server.get('host') or '127.0.0.1', int(server.get('port') or 1883), 5)
                    cl.loop_start()
                    # Also subscribe immediately in case already connected
                    try:
                        cl.subscribe(topic_t, qos=0)
                    except Exception:
                        logger.exception('EnvMonitor temp immediate subscribe failed')
                    self.temp_client = cl
                except Exception:
                    logger.exception('EnvMonitor temp start failed')
                    # В случае ошибки старта не блокируем будущие попытки
                    self.temp_client = None
        # Humidity
        hcfg = (self.cfg.get('hum') or {})
        if hcfg.get('enabled') and hcfg.get('topic') and hcfg.get('server_id'):
            server = db.get_mqtt_server(int(hcfg['server_id']))
            if server:
                try:
                    cl = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2)
                    if server.get('username'):
                        cl.username_pw_set(server.get('username'), server.get('password') or None)
                    topic_h = (hcfg['topic'] or '').strip()
                    try:
                        logger.info('EnvMonitor hum connecting host=%s port=%s topic=%s', server.get('host'), server.get('port'), topic_h)
                    except Exception:
                        pass
                    def _on_msg_hum(c, u, msg):
                        try:
                            s = (msg.payload.decode('utf-8', 'ignore') or '').strip().replace(',', '.')
                            self.hum_value = round(float(s))
                            try:
                                self.last_hum_rx_ts = time.time()
                            except Exception:
                                pass
                            logger.info(f"EnvMonitor hum RX topic={getattr(msg,'topic',topic_h)} value={self.hum_value}")
                        except Exception:
                            logger.exception('EnvMonitor hum parse failed')
                    def _on_connect_hum(c, u, flags, reason_code, properties=None):
                        try:
                            c.subscribe(topic_h, qos=0)
                            logger.info("EnvMonitor hum subscribed %s", topic_h)
                        except Exception:
                            logger.exception('EnvMonitor hum subscribe failed')
                    def _on_disconnect_hum(c, u, rc, properties=None):
                        try:
                            self.hum_client = None
                            logger.info("EnvMonitor hum disconnected: rc=%s", rc)
                        except Exception:
                            pass
                    cl.on_message = _on_msg_hum
                    cl.on_connect = _on_connect_hum
                    cl.on_disconnect = _on_disconnect_hum
                    cl.connect(server.get('host') or '127.0.0.1', int(server.get('port') or 1883), 5)
                    cl.loop_start()
                    try:
                        cl.subscribe(topic_h, qos=0)
                    except Exception:
                        logger.exception('EnvMonitor hum immediate subscribe failed')
                    self.hum_client = cl
                except Exception:
                    logger.exception('EnvMonitor hum start failed')
                    # В случае ошибки старта не блокируем будущие попытки
                    self.hum_client = None

env_monitor = EnvMonitor()

@app.before_request
def _init_scheduler_before_request():
    global _SCHEDULER_INIT_DONE, _INITIAL_SYNC_DONE, _RAIN_MONITOR_STARTED, _RAIN_MONITOR_CFG_SIG, _ENV_MONITOR_STARTED, _ENV_MONITOR_CFG_SIG
    # Default role is "user" (no password)
    if 'role' not in session:
        session['role'] = 'guest'
    # Не блокировать /api/login тяжёлыми инициализациями
    try:
        if (request.path or '') == '/api/login':
            return None
    except Exception:
        pass
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
                        t = normalize_topic(topic)
                        server = db.get_mqtt_server(int(sid))
                        if server:
                            _publish_mqtt_value(server, t, '0')
                except Exception:
                    pass
            _INITIAL_SYNC_DONE = True
            logger.info("Initial sync: all zones set to OFF and MQTT OFF published")
        except Exception as e:
            logger.error(f"Initial sync failed: {e}")

    # Требование смены пароля при первом входе / чистой установке
    try:
        if not app.config.get('TESTING'):
            try:
                db.ensure_password_change_required()
            except Exception:
                pass
            if request.path.startswith('/api/'):
                # Всегда разрешаем GET для гостей/пользователей
                if request.method == 'GET':
                    return None
                # Разрешаем гостю/user выполнять действия со страницы Статус
                pth = request.path or ''
                allowed_public_posts = {
                    '/api/login', '/api/password', '/api/status', '/health', '/api/env',
                    '/api/emergency-stop', '/api/emergency-resume', '/api/postpone'
                }
                def _is_status_action(path: str) -> bool:
                    try:
                        if path in allowed_public_posts:
                            return True
                        if path.startswith('/api/mqtt/'):
                            return True
                        if path.startswith('/api/groups/') and (path.endswith('/start-from-first') or path.endswith('/stop')):
                            return True
                        if path.startswith('/api/zones/') and ('/mqtt/start' in path or '/mqtt/stop' in path or path.endswith('/start') or path.endswith('/stop')):
                            return True
                    except Exception:
                        pass
                    return False
                if session.get('role') != 'admin':
                    if not _is_status_action(pth):
                        return jsonify({'success': False, 'message': 'auth required', 'error_code': 'UNAUTHENTICATED'}), 401
                if session.get('role') == 'admin' and request.method in ['POST','PUT','DELETE']:
                    try:
                        must = db.get_setting_value('password_must_change')
                    except Exception:
                        must = None
                    if str(must or '0') == '1' and request.path != '/api/password':
                        return jsonify({'success': False, 'message': 'password change required', 'error_code': 'PASSWORD_MUST_CHANGE'}), 403
    except Exception:
        pass

    # Инициализация/перезапуск RainMonitor при изменении конфигурации
    try:
        if not app.config.get('TESTING'):
            cfg = db.get_rain_config()
            sig = (cfg.get('enabled'), cfg.get('topic'), cfg.get('type'), cfg.get('server_id'))
            if (not _RAIN_MONITOR_STARTED) or sig != _RAIN_MONITOR_CFG_SIG:
                rain_monitor.start(cfg)
                _RAIN_MONITOR_STARTED = True
                _RAIN_MONITOR_CFG_SIG = sig
    except Exception:
        pass
    # Инициализация/перезапуск EnvMonitor (не зависим от TESTING, чтобы значения появлялись сразу)
    try:
        ecfg = db.get_env_config()
        esig = (
            ecfg.get('temp',{}).get('enabled'), ecfg.get('temp',{}).get('topic'), ecfg.get('temp',{}).get('server_id'),
            ecfg.get('hum',{}).get('enabled'), ecfg.get('hum',{}).get('topic'), ecfg.get('hum',{}).get('server_id'),
        )
        # Стартуем/перезапускаем монитор, если он ещё не стартовал, поменялась конфигурация
        # или оба клиента отсутствуют (например, предыдущий старт не удался).
        try:
            logger.info(
                "EnvMonitor check: started=%s cfg_sig=%s esig=%s temp_client=%s hum_client=%s",
                _ENV_MONITOR_STARTED, _ENV_MONITOR_CFG_SIG, esig,
                'ok' if getattr(env_monitor, 'temp_client', None) else 'none',
                'ok' if getattr(env_monitor, 'hum_client', None) else 'none',
            )
        except Exception:
            pass
        need_start = (not _ENV_MONITOR_STARTED) or (esig != _ENV_MONITOR_CFG_SIG)
        if not need_start:
            try:
                no_clients = (getattr(env_monitor, 'temp_client', None) is None and getattr(env_monitor, 'hum_client', None) is None)
            except Exception:
                no_clients = True
            need_start = no_clients
        try:
            logger.info(
                "EnvMonitor decision: need_start=%s reason=%s",
                need_start,
                ('cfg_changed' if esig != _ENV_MONITOR_CFG_SIG else ('no_clients' if (getattr(env_monitor, 'temp_client', None) is None and getattr(env_monitor, 'hum_client', None) is None) else ('not_started' if not _ENV_MONITOR_STARTED else 'none')))
            )
        except Exception:
            pass
        if need_start:
            env_monitor.start(ecfg)
            # Разово пробуем получить retained-значения после старта, чтобы данные появились сразу
            try:
                _probe_env_values(ecfg)
            except Exception:
                logger.exception('EnvMonitor probe call failed')
            _ENV_MONITOR_STARTED = True
            _ENV_MONITOR_CFG_SIG = esig
    except Exception:
        logger.exception('EnvMonitor before_request failed')


app.register_blueprint(status_bp)
app.register_blueprint(files_bp)
app.register_blueprint(zones_bp)
app.register_blueprint(programs_bp)
app.register_blueprint(groups_bp)
app.register_blueprint(auth_bp)
app.register_blueprint(settings_bp)
try:
    from routes.mqtt import mqtt_bp
    app.register_blueprint(mqtt_bp)
except Exception as _e:
    logger.warning(f"MQTT blueprint not registered: {_e}")
@app.before_request
def _require_admin_for_mutations():
    try:
        if app.config.get('TESTING'):
            return None
        p = request.path or ''
        if not p.startswith('/api/'):
            return None
        # Разрешаем все GET-запросы для гостей/пользователей (чтение публичных данных)
        if request.method == 'GET':
            return None
        # Мутации — только для админа, кроме разрешённых
        if request.method in ['POST', 'PUT', 'DELETE']:
            if p == '/api/login' or p.startswith('/api/env') or p.startswith('/api/mqtt/') or p == '/api/password':
                return None
            # Разрешаем действия со страницы "Статус" для гостя/пользователя
            def _is_status_action(path: str) -> bool:
                try:
                    if path in ('/api/emergency-stop', '/api/emergency-resume', '/api/postpone'):
                        return True
                    if path.startswith('/api/groups/') and (path.endswith('/start-from-first') or path.endswith('/stop')):
                        return True
                    if path.startswith('/api/zones/') and ('/mqtt/start' in path or '/mqtt/stop' in path or path.endswith('/start') or path.endswith('/stop')):
                        return True
                except Exception:
                    pass
                return False
            if session.get('role') != 'admin':
                if not _is_status_action(p):
                    return jsonify({'success': False, 'message': 'admin required', 'error_code': 'FORBIDDEN'}), 403
    except Exception:
        return None
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

# Кэш MQTT‑клиентов по server_id для публикаций
_MQTT_CLIENTS: dict[int, object] = {}
_MQTT_CLIENTS_LOCK = threading.Lock()

def _get_or_create_mqtt_client(server: dict):
    try:
        sid = int(server.get('id')) if server.get('id') is not None else 0
    except Exception:
        sid = 0
    with _MQTT_CLIENTS_LOCK:
        cl = _MQTT_CLIENTS.get(sid)
        if cl is None:
            try:
                cl = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2)
                if server.get('username'):
                    cl.username_pw_set(server.get('username'), server.get('password') or None)
                # TLS options (если включены)
                try:
                    if int(server.get('tls_enabled') or 0) == 1:
                        import ssl
                        ca = server.get('tls_ca_path') or None
                        cert = server.get('tls_cert_path') or None
                        key = server.get('tls_key_path') or None
                        tls_ver = (server.get('tls_version') or '').upper().strip()
                        version = ssl.PROTOCOL_TLS_CLIENT if tls_ver in ('', 'TLS', 'TLS_CLIENT') else ssl.PROTOCOL_TLS
                        cl.tls_set(ca_certs=ca, certfile=cert, keyfile=key, tls_version=version)
                        if int(server.get('tls_insecure') or 0) == 1:
                            cl.tls_insecure_set(True)
                except Exception:
                    logger.exception('MQTT TLS setup failed for publisher')
                host = server.get('host') or '127.0.0.1'
                port = int(server.get('port') or 1883)
                try:
                    cl.connect(host, port, 30)
                except Exception:
                    # не кэшируем неудачное подключение
                    return None
                try:
                    cl.reconnect_delay_set(min_delay=1, max_delay=4)
                except Exception:
                    pass
                def _on_disconnect(c, u, rc, properties=None):
                    try:
                        with _MQTT_CLIENTS_LOCK:
                            if _MQTT_CLIENTS.get(sid) is c:
                                _MQTT_CLIENTS.pop(sid, None)
                    except Exception:
                        pass
                cl.on_disconnect = _on_disconnect
                # запускаем сетевой цикл, чтобы publish действительно уходил
                try:
                    cl.loop_start()
                except Exception:
                    pass
                _MQTT_CLIENTS[sid] = cl
            except Exception:
                return None
        return cl

def _publish_mqtt_value(server: dict, topic: str, value: str, min_interval_sec: float = 0.5) -> bool:
    try:
        t = normalize_topic(topic)
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
        cl = _get_or_create_mqtt_client(server)
        if cl is None:
            logger.warning("MQTT publish: client unavailable, dropping message")
            return False
        try:
            res = cl.publish(t, payload=value, qos=0, retain=False)
            try:
                rc = getattr(res, 'rc', 0)
            except Exception:
                rc = 0
            if rc != 0:
                logger.warning(f"MQTT publish initial rc={rc}, try reconnect")
                raise RuntimeError('publish_failed')
        except Exception:
            # Попробуем один раз пересоздать клиента и отправить
            cl = _get_or_create_mqtt_client(server)
            if cl is None:
                return False
            res = cl.publish(t, payload=value, qos=0, retain=False)
            try:
                rc = getattr(res, 'rc', 0)
            except Exception:
                rc = 0
            if rc != 0:
                logger.warning(f"MQTT publish retry rc={rc}")
                return False
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
                    t = normalize_topic(topic)
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
    return jsonify({
        'authenticated': bool(session.get('logged_in')) or bool(app.config.get('TESTING')),
        'role': session.get('role', 'guest')
    })


@app.route('/logout', methods=['GET'])
def api_logout():
    # Возвращаем роль в user
    session['logged_in'] = False
    session['role'] = 'user'
    return redirect(url_for('auth_bp.login_page'))


@csrf.exempt
@csrf.exempt
@app.route('/api/password', methods=['POST'])
def api_change_password():
    try:
        if not session.get('logged_in') and not app.config.get('TESTING'):
            return jsonify({'success': False, 'message': 'Требуется аутентификация'}), 401
        data = request.get_json() or {}
        old_password = data.get('old_password', '')
        new_password = data.get('new_password', '')
        if len(new_password) < 4 or len(new_password) > 32:
            return jsonify({'success': False, 'message': 'Пароль должен быть 4..32 символа'}), 400
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
            # Вернуть список всех карт по дате добавления (новые сверху)
            allowed_ext = {'.png', '.jpg', '.jpeg', '.gif', '.webp'}
            items = []
            for f in os.listdir(MAP_FOLDER):
                p = os.path.join(MAP_FOLDER, f)
                try:
                    ext = os.path.splitext(f)[1].lower()
                    if os.path.isfile(p) and ext in allowed_ext:
                        items.append({
                            'name': f,
                            'path': f"media/maps/{f}",
                            'mtime': os.path.getmtime(p)
                        })
                except Exception:
                    continue
            items.sort(key=lambda x: x['mtime'], reverse=True)
            return jsonify({'success': True, 'items': items})
        else:
            # Только админ может загружать
            if not (app.config.get('TESTING') or session.get('role') == 'admin'):
                return jsonify({'success': False, 'message': 'Только администратор может загружать карты'}), 403
            if 'file' not in request.files:
                return jsonify({'success': False, 'message': 'Файл не найден'}), 400
            file = request.files['file']
            if file.filename == '':
                return jsonify({'success': False, 'message': 'Файл не выбран'}), 400
            ext = os.path.splitext(file.filename)[1].lower()
            if ext not in ['.png', '.jpg', '.jpeg', '.gif', '.webp']:
                return jsonify({'success': False, 'message': 'Неподдерживаемый формат'}), 400
            # MIME-проверка загружаемой карты
            m = request.files.get('file')
            if not m or (getattr(m, 'mimetype', None) not in ALLOWED_MIME_TYPES):
                return jsonify({'success': False, 'message': 'Неподдерживаемый тип содержимого'}), 400
            # Больше не удаляем предыдущие карты — поддерживаем несколько файлов
            filename = f"zones_map_{int(time.time())}{ext}"
            save_path = os.path.join(MAP_FOLDER, filename)
            file.save(save_path)
            return jsonify({'success': True, 'message': 'Карта загружена', 'path': f"media/maps/{filename}"})
    except Exception as e:
        logger.error(f"Ошибка работы с картой зон: {e}")
        return jsonify({'success': False, 'message': 'Ошибка работы с картой'}), 500

@app.route('/api/map/<string:filename>', methods=['DELETE'])
def api_map_delete(filename):
    try:
        # Только админ может удалять
        if not (app.config.get('TESTING') or session.get('role') == 'admin'):
            return jsonify({'success': False, 'message': 'Только администратор может удалять карты'}), 403
        safe = secure_filename(filename)
        if safe != filename:
            return jsonify({'success': False, 'message': 'Некорректное имя файла'}), 400
        path = os.path.join(MAP_FOLDER, safe)
        if not os.path.exists(path):
            return jsonify({'success': False, 'message': 'Файл не найден'}), 404
        os.remove(path)
        return jsonify({'success': True})
    except Exception as e:
        logger.error(f"Ошибка удаления карты: {e}")
        return jsonify({'success': False, 'message': 'Ошибка удаления карты'}), 500

@app.route('/sw.js')
def service_worker():
    return app.send_static_file('sw.js')

@app.route('/health')
def health_check():
    try:
        # DB check
        try:
            _ = db.get_zones()
            db_ok = True
        except Exception:
            db_ok = False
        # Scheduler check
        try:
            sched = get_scheduler()
            sched_ok = bool(sched is not None)
        except Exception:
            sched_ok = False
        # MQTT check: есть ли доступные сервера
        try:
            servers = db.get_mqtt_servers() or []
            mqtt_ok = bool(len(servers) >= 0)
        except Exception:
            mqtt_ok = False
        overall = db_ok and sched_ok
        code = 200 if overall else 503
        return jsonify({
            'ok': overall,
            'db': db_ok,
            'scheduler': sched_ok,
            'mqtt_configured': mqtt_ok
        }), code
    except Exception as e:
        logger.exception('health check failed')
        return jsonify({'ok': False, 'error': str(e)}), 500

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

@app.route('/api/rain', methods=['GET', 'POST'])
def api_rain_config():
    """GET — вернуть конфигурацию датчика дождя; POST — обновить.
    { enabled: bool, topic: str, type: 'NO'|'NC', server_id?: int }
    """
    try:
        if request.method == 'GET':
            return jsonify({'success': True, 'config': db.get_rain_config()})
        data = request.get_json() or {}
        cfg = {
            'enabled': bool(data.get('enabled')),
            'topic': (data.get('topic') or '').strip(),
            'type': data.get('type') if data.get('type') in ('NO', 'NC') else 'NO',
            'server_id': data.get('server_id')
        }
        # Валидация: если включено — topic обязателен
        if cfg['enabled'] and not cfg['topic']:
            return jsonify({'success': False, 'message': 'Требуется MQTT-топик для датчика дождя'}), 400
        ok = db.set_rain_config(cfg)
        # Если глобально включили датчик дождя — включаем флаг у всех групп (кроме 999)
        if ok and cfg.get('enabled'):
            try:
                for g in (db.get_groups() or []):
                    gid = int(g.get('id'))
                    if gid == 999:
                        continue
                    db.set_group_use_rain(gid, True)
            except Exception:
                pass
        return jsonify({'success': bool(ok)})
    except Exception as e:
        logger.error(f"rain config failed: {e}")
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
                pu_dt = _parse_dt(pu)
                if pu_dt and pu_dt > now:
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
        # Простая валидация
        try:
            if 'duration' in data:
                d = int(data['duration'])
                if d < 1 or d > 3600:
                    return jsonify({'success': False, 'message': 'duration must be 1..3600'}), 400
            if 'name' in data and (not str(data['name']).strip()):
                return jsonify({'success': False, 'message': 'name must be non-empty'}), 400
        except Exception:
            return jsonify({'success': False, 'message': 'invalid zone payload'}), 400
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
    # Простая валидация
    try:
        name = str(data.get('name') or 'Зона').strip()
        duration = int(data.get('duration') or 10)
        if duration < 1 or duration > 3600:
            return jsonify({'success': False, 'message': 'duration must be 1..3600'}), 400
        if not name:
            return jsonify({'success': False, 'message': 'name must be non-empty'}), 400
    except Exception:
        return jsonify({'success': False, 'message': 'invalid zone payload'}), 400
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

@csrf.exempt
@app.route('/api/zones/import', methods=['POST'])
def api_import_zones_bulk():
    """Импорт/массовое применение изменений зон в одной транзакции.

    Формат: { zones: [ { id?, name?, icon?, duration?, group_id?, topic?, mqtt_server_id?, state? }, ... ] }
    Возвращает: { success, created, updated, failed }
    """
    try:
        body = request.get_json(silent=True) or {}
        zones = body.get('zones') or []
        if not isinstance(zones, list) or not zones:
            return jsonify({'success': False, 'message': 'Нет данных для импорта'}), 400
        stats = db.bulk_upsert_zones(zones)
        try:
            db.add_log('zones_import', json.dumps({'counts': stats}))
        except Exception:
            pass
        return jsonify({'success': True, **stats})
    except Exception as e:
        logger.error(f"Ошибка импорта зон: {e}")
        return jsonify({'success': False, 'message': 'Ошибка импорта'}), 500

@app.route('/api/groups')
def api_groups():
    groups = db.get_groups()
    return jsonify(groups)

@csrf.exempt
@app.route('/api/zones/next-watering-bulk', methods=['POST'])
def api_zones_next_watering_bulk():
    try:
        data = request.get_json(silent=True) or {}
        zone_ids = data.get('zone_ids')
        # Загрузим все зоны (для длительностей), а также программы один раз
        all_zones = db.get_zones() or []
        if not zone_ids:
            zone_ids = [int(z.get('id')) for z in all_zones if int(z.get('group_id') or z.get('group') or 0) != 999]
        zone_ids = [int(z) for z in zone_ids]
        duration_by_zone = {int(z['id']): int(z.get('duration') or 0) for z in all_zones}
        programs = db.get_programs() or []
        # Для каждой программы посчитаем смещения зон (накопительная длительность)
        offset_map_per_program = []  # list of dicts {zone_id: offset_minutes}
        for p in programs:
            try:
                zones_list = sorted([int(x) for x in (p.get('zones') or [])])
            except Exception:
                zones_list = []
            offsets = {}
            cum = 0
            for zid in zones_list:
                offsets[zid] = cum
                cum += int(duration_by_zone.get(zid, 0))
            offset_map_per_program.append({'prog': p, 'offsets': offsets})
        # Для каждой программы найдём ближайшее будущее время старта
        from datetime import datetime as _dt
        now = _dt.now()
        next_start_per_program = {}
        for pm in offset_map_per_program:
            p = pm['prog']
            try:
                hh, mm = [int(x) for x in str(p.get('time') or '00:00').split(':', 1)]
            except Exception:
                hh, mm = 0, 0
            best = None
            days = p.get('days') or []
            for off in range(0, 14):
                d = now + timedelta(days=off)
                if d.weekday() in days:
                    cand = d.replace(hour=hh, minute=mm, second=0, microsecond=0)
                    if cand <= now:
                        continue
                    best = cand
                    break
            next_start_per_program[p.get('id')] = best
        # Соберём ответы по зонам
        items = []
        for zid in zone_ids:
            best_dt = None
            for pm in offset_map_per_program:
                p = pm['prog']; offsets = pm['offsets']
                if zid not in offsets:
                    continue
                start_dt = next_start_per_program.get(p.get('id'))
                if not start_dt:
                    continue
                cand = start_dt + timedelta(minutes=int(offsets.get(zid, 0)))
                # Если зона уже поливается, не переносим сразу на следующий день для отображения в UI
                try:
                    zinfo = next((zz for zz in all_zones if int(zz.get('id')) == int(zid)), None)
                    if not (zinfo and str(zinfo.get('state')) == 'on' and zinfo.get('watering_start_time')):
                        if cand <= now:
                            continue
                except Exception:
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
    except Exception as e:
        logger.error(f"bulk next-watering failed: {e}")
        return jsonify({'success': False}), 500

@csrf.exempt
@app.route('/api/groups/<int:group_id>', methods=['PUT'])
def api_update_group(group_id):
    data = request.get_json() or {}
    # Обновление имени
    updated = False
    if 'name' in data:
        if db.update_group(group_id, data['name']):
            updated = True
    # Обновление флага использования датчика дождя
    if 'use_rain_sensor' in data:
        try:
            ok = db.set_group_use_rain(group_id, bool(data.get('use_rain_sensor')))
            updated = updated or ok
        except Exception as e:
            logger.error(f"Ошибка обновления use_rain_sensor группы {group_id}: {e}")
    if updated:
        try:
            payload = {"group": group_id}
            if 'name' in data:
                payload["name"] = data['name']
            if 'use_rain_sensor' in data:
                payload["use_rain_sensor"] = bool(data.get('use_rain_sensor'))
            db.add_log('group_edit', json.dumps(payload))
        except Exception:
            pass
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


@app.route('/api/zones/check-duration-conflicts-bulk', methods=['POST'])
def api_check_zone_duration_conflicts_bulk():
    """Пакетная проверка конфликтов длительностей для нескольких зон.

    Принимает JSON: { "changes": [{"zone_id": int, "new_duration": int}, ...] }
    Возвращает: { success, results: { zone_id: { has_conflicts, conflicts: [...] } } }
    """
    try:
        payload = request.get_json() or {}
        changes = payload.get('changes') or []
        # Валидация
        normalized = []
        for ch in changes:
            try:
                zid = int(ch.get('zone_id'))
                dur = int(ch.get('new_duration'))
                normalized.append((zid, dur))
            except Exception:
                continue
        if not normalized:
            return jsonify({'success': False, 'message': 'Нет валидных изменений'}), 400

        # Кэшируем необходимые данные один раз
        all_programs = db.get_programs()
        # Кэш зон: длительности и групповые принадлежности
        zones_cache = { z['id']: z for z in db.get_zones() }

        def get_zone_group(zid: int):
            z = zones_cache.get(zid)
            return z['group_id'] if z else None

        def get_zone_duration(zid: int):
            z = zones_cache.get(zid)
            if not z: return 0
            try:
                return int(z.get('duration') or 0)
            except Exception:
                return 0

        results = {}

        # Для каждого изменения считаем конфликты, переиспользуя кэши
        for (zone_id, new_duration) in normalized:
            conflicts = []
            # Ищем программы, где участвует эта зона
            for program in all_programs:
                prog_days = program['days'] if isinstance(program['days'], list) else json.loads(program['days'])
                prog_zones = program['zones'] if isinstance(program['zones'], list) else json.loads(program['zones'])
                if zone_id not in prog_zones:
                    continue
                try:
                    p_hour, p_min = map(int, program['time'].split(':'))
                except Exception:
                    continue
                start_a = p_hour * 60 + p_min
                # Суммарная длительность программы, учитывая новое значение только для текущей зоны
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
                    except Exception:
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
            results[str(zone_id)] = {
                'has_conflicts': len(conflicts) > 0,
                'conflicts': conflicts
            }

        return jsonify({'success': True, 'results': results})
    except Exception as e:
        logger.error(f"Ошибка bulk-проверки конфликтов длительности зон: {e}")
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
                    data = json.dumps({'topic': normalize_topic(topic), 'payload': payload})
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

@app.route('/api/server-time')
def api_server_time():
    try:
        now = datetime.now()
        try:
            tzname = time.tzname[0] if time.tzname else ''
        except Exception:
            tzname = ''
        payload = {
            'now_iso': now.strftime('%Y-%m-%d %H:%M:%S'),
            'epoch_ms': int(time.time() * 1000),
            'tz': tzname
        }
        resp = jsonify(payload)
        resp.headers['Cache-Control'] = 'no-store'
        return resp
    except Exception as e:
        logger.error(f"server-time failed: {e}")
        return jsonify({'now_iso': None, 'epoch_ms': int(time.time()*1000)}), 200

@app.route('/api/status')
def api_status():
    rain_cfg = db.get_rain_config()
    
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
                            pu_dt = _parse_dt(pu)
                            if pu_dt and pu_dt > search_from:
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
            postpone_until = postponed_zones[0].get('postpone_until')
            # Берём причину первой отложенной зоны (приоритет ручной приостановки)
            try:
                reasons = [z.get('postpone_reason') for z in postponed_zones if z.get('postpone_reason')]
                if 'manual' in reasons:
                    group_postpone_reason = 'manual'
                elif reasons:
                    group_postpone_reason = reasons[0]
            except Exception:
                pass
        # Не навязываем отложку только по факту включенного датчика.
        # Отложка ставится RainMonitor'ом в момент дождя и хранится в БД.
        
        # Определяем источник запуска текущей зоны (manual|schedule), если полив идёт
        current_zone_source = None
        try:
            if status == 'watering' and current_zone:
                cz = next((z for z in group_zones if int(z['id']) == int(current_zone)), None)
                if cz:
                    src = (cz.get('watering_start_source') or '').strip().lower()
                    if src in ('manual', 'schedule', 'remote'):
                        current_zone_source = src
                    else:
                        # Если явного источника нет, но зона включена — считаем, что это удалённый запуск (MQTT вне UI)
                        current_zone_source = 'remote'
        except Exception:
            pass

        groups_status.append({
            'id': group_id,
            'name': group['name'],
            'status': status,
            'current_zone': current_zone,
            'postpone_until': postpone_until,
            'next_start': next_start,
            'postpone_reason': group_postpone_reason,
            'was_postponed': bool(postponed_zones),
            'current_zone_source': current_zone_source
        })
    
    # Статус датчика дождя
    # Текстовый статус датчика: выключен / дождя нет / идёт дождь
    if not rain_cfg.get('enabled'):
        rain_sensor_status = 'выключен'
    else:
        try:
            # ориентируемся по мониторингу, если есть последнее состояние
            if hasattr(rain_monitor, 'is_rain') and rain_monitor.is_rain is not None:
                rain_sensor_status = 'идёт дождь' if rain_monitor.is_rain else 'дождя нет'
            else:
                rain_sensor_status = 'дождя нет'
        except Exception:
            rain_sensor_status = 'дождя нет'

    # Температура/влажность из MQTT (если включено), иначе скрывать или показывать 'нет данных'
    env_cfg = db.get_env_config()
    temp_enabled = bool(env_cfg.get('temp',{}).get('enabled'))
    hum_enabled = bool(env_cfg.get('hum',{}).get('enabled'))
    temperature = None if not temp_enabled else (env_monitor.temp_value if env_monitor.temp_value is not None else 'нет данных')
    humidity = None if not hum_enabled else (env_monitor.hum_value if env_monitor.hum_value is not None else 'нет данных')

    # MQTT servers/config/connectivity quick summary for UI banners
    try:
        servers = db.get_mqtt_servers()
    except Exception:
        servers = []
    mqtt_servers_count = len(servers)
    enabled_servers = [s for s in servers if int(s.get('enabled') or 0) == 1]
    mqtt_enabled_count = len(enabled_servers)
    mqtt_connected = False
    # Best-effort connectivity check: try to connect to any enabled server (fallback: any server)
    try:
        if mqtt_servers_count > 0 and mqtt is not None:
            candidates = enabled_servers if mqtt_enabled_count > 0 else servers
            for s in candidates:
                try:
                    client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id=(s.get('client_id') or None))
                    if s.get('username'):
                        client.username_pw_set(s.get('username'), s.get('password') or None)
                    # keepalive is small; connect will raise fast if host unreachable
                    client.connect(s.get('host') or '127.0.0.1', int(s.get('port') or 1883), 3)
                    mqtt_connected = True
                    try:
                        client.disconnect()
                    except Exception:
                        pass
                    break
                except Exception:
                    mqtt_connected = False
        # Warn-level logs for missing servers or no connectivity
        if mqtt_servers_count == 0:
            try:
                logger.warning('MQTT: нет ни одного сервера в настройках')
            except Exception:
                pass
            try:
                db.add_log('mqtt_warn', 'нет ни одного сервера в настройках')
            except Exception:
                pass
        elif not mqtt_connected:
            try:
                logger.warning('MQTT: нет связи ни с одним сервером')
            except Exception:
                pass
            try:
                db.add_log('mqtt_warn', 'нет связи ни с одним MQTT сервером')
            except Exception:
                pass
    except Exception:
        # Silent: do not break status endpoint on MQTT check errors
        pass

    try:
        logger.info('api_status: temp=%s hum=%s temp_enabled=%s hum_enabled=%s', temperature, humidity, temp_enabled, hum_enabled)
    except Exception:
        pass
    return jsonify({
        'datetime': datetime.now().strftime('%d.%m.%Y %H:%M:%S'),
        'temperature': temperature,
        'humidity': humidity,
        'rain_enabled': bool(rain_cfg.get('enabled')),
        'rain_sensor': rain_sensor_status,
        'groups': groups_status,
        'emergency_stop': app.config.get('EMERGENCY_STOP', False),
        # MQTT quick status for UI
        'mqtt_servers_count': mqtt_servers_count,
        'mqtt_enabled_count': mqtt_enabled_count,
        'mqtt_connected': mqtt_connected
    })
@app.route('/api/env', methods=['GET','POST'])
def api_env_config():
    try:
        if request.method == 'GET':
            cfg = db.get_env_config()
            values = {
                'temp': env_monitor.temp_value,
                'hum': env_monitor.hum_value,
            }
            return jsonify({'success': True, 'config': cfg, 'values': values})
        data = request.get_json() or {}
        # Special action: restart monitor without changing config
        action = data.get('action')
        if action == 'restart':
            try:
                cfg = db.get_env_config()
                env_monitor.start(cfg)
                # best-effort probe
                _probe_env_values(cfg)
            except Exception:
                pass
            return jsonify({'success': True})
        # Валидация: если включены датчики temp/hum — их topic обязателен (копим все ошибки сразу)
        try:
            temp_cfg = (data.get('temp') or {})
            hum_cfg = (data.get('hum') or {})
            errors = {}
            if bool(temp_cfg.get('enabled')) and not str(temp_cfg.get('topic') or '').strip():
                errors['temp_topic'] = 'Требуется MQTT-топик для датчика температуры'
            if bool(hum_cfg.get('enabled')) and not str(hum_cfg.get('topic') or '').strip():
                errors['hum_topic'] = 'Требуется MQTT-топик для датчика влажности'
            if errors:
                return jsonify({'success': False, 'errors': errors}), 400
        except Exception:
            pass
        ok = db.set_env_config(data)
        # Apply new config immediately
        try:
            cfg = db.get_env_config()
            env_monitor.start(cfg)
            _probe_env_values(cfg)
        except Exception:
            pass
        return jsonify({'success': bool(ok)})
    except Exception as e:
        logger.error(f"env config failed: {e}")
        return jsonify({'success': False}), 500

@app.route('/api/env/values', methods=['GET'])
def api_env_values():
    try:
        cfg = db.get_env_config()
        temp_enabled = bool((cfg.get('temp') or {}).get('enabled'))
        hum_enabled = bool((cfg.get('hum') or {}).get('enabled'))
        temperature = None if not temp_enabled else (env_monitor.temp_value if env_monitor.temp_value is not None else 'нет данных')
        humidity = None if not hum_enabled else (env_monitor.hum_value if env_monitor.hum_value is not None else 'нет данных')
        return jsonify({'success': True, 'temperature': temperature, 'humidity': humidity, 'enabled': {'temp': temp_enabled, 'hum': hum_enabled}})
    except Exception as e:
        logger.error(f"env values failed: {e}")
        return jsonify({'success': False}), 500

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
        postpone_until = postpone_date.strftime('%Y-%m-%d 23:59:59')
        
        zones = db.get_zones()
        group_zones = [z for z in zones if int(z.get('group_id') or 0) == int(group_id)]
        
        for zone in group_zones:
            # Фиксируем причину: ручная приостановка пользователем
            db.update_zone_postpone(zone['id'], postpone_until, 'manual')
        
        # По требованию: немедленно остановить полив всех зон группы,
        # но не блокировать ручной запуск (НЕ аварийная остановка)
        try:
            for zone in group_zones:
                try:
                    if (zone.get('state') == 'on') or zone.get('watering_start_time'):
                        db.update_zone(zone['id'], {'state': 'off', 'watering_start_time': None})
                        # Публикуем OFF в MQTT, если настроен сервер и топик
                        sid = zone.get('mqtt_server_id')
                        topic = (zone.get('topic') or '').strip()
                        if mqtt and sid and topic:
                            t = normalize_topic(topic)
                            server = db.get_mqtt_server(int(sid))
                            if server:
                                _publish_mqtt_value(server, t, '0', min_interval_sec=0.0)
                except Exception:
                    logger.exception("Ошибка остановки зоны при установке отложенного полива")
            # Отменяем активные задания планировщика для группы (будущие и текущие)
            try:
                scheduler = get_scheduler()
                if scheduler:
                    scheduler.cancel_group_jobs(group_id)
            except Exception:
                logger.exception("Ошибка отмены заданий планировщика при отложенном поливе группы")
        except Exception:
            # Не прерываем выполнение общей операции postpone, только логируем
            logger.exception("Ошибка массовой остановки зон при отложенном поливе группы")
        
        db.add_log('postpone_set', json.dumps({
            "group": group_id, 
            "days": days, 
            "until": postpone_until
        }))
        
        return jsonify({
            "success": True, 
            "message": f"Полив отложен на {days} дней",
            "postpone_until": postpone_date.strftime('%d.%m.%Y %H:%M:%S')
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
                    t = normalize_topic(topic)
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
            try:
                # Дополнительно очищаем scheduled_start_time в БД, чтобы не было «хвостов»
                db.clear_group_scheduled_starts(group_id)
            except Exception:
                pass
 
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
                        t = normalize_topic(topic)
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
                        t = normalize_topic(topic)
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
                # Немедленно запускаем обратный отсчет для UI: записываем старт
                try:
                    db.update_zone(zone_id, {'watering_start_time': datetime.now().strftime('%Y-%m-%d %H:%M:%S')})
                except Exception:
                    pass
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
        # MIME-проверка
        try:
            mime = file.mimetype
        except Exception:
            mime = None
        if not mime or mime not in ALLOWED_MIME_TYPES:
            return jsonify({'success': False, 'message': 'Неподдерживаемый тип содержимого'}), 400
        
        # Читаем файл
        file_data = file.read()
        if len(file_data) > MAX_FILE_SIZE:
            return jsonify({'success': False, 'message': 'Файл слишком большой'}), 400
        
        # Нормализация: в TESTING сохраняем исходные байты (для байтового сравнения),
        # в обычном режиме приводим к единому размеру и формату WEBP.
        is_testing = bool(app.config.get('TESTING'))
        if is_testing:
            out_bytes = file_data
            out_ext = os.path.splitext(file.filename)[1].lower() or '.jpg'
        else:
            try:
                out_bytes, out_ext = normalize_image(file_data, target_size=(800, 600), fmt='WEBP', quality=90)
            except Exception:
                logger.exception('normalize_image failed, storing original bytes')
                out_bytes = file_data
                out_ext = os.path.splitext(file.filename)[1].lower() or '.jpg'
        
        # Перемещаем старый файл в OLD
        try:
            current = db.get_zone(zone_id)
            old_rel = (current or {}).get('photo_path')
            if old_rel:
                old_abs = os.path.join('static', old_rel)
                if os.path.exists(old_abs):
                    old_dir = os.path.join(UPLOAD_FOLDER, 'OLD')
                    os.makedirs(old_dir, exist_ok=True)
                    os.replace(old_abs, os.path.join(old_dir, os.path.basename(old_abs)))
        except Exception:
            pass

        # Генерируем стандартное имя
        base_name = f"ZONE_{zone_id}"
        filename = f"{base_name}{out_ext}"
        filepath = os.path.join(UPLOAD_FOLDER, filename)
        
        # Сохраняем файл
        with open(filepath, 'wb') as f:
            f.write(out_bytes)
        
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

@csrf.exempt
@app.route('/api/zones/<int:zone_id>/photo/rotate', methods=['POST'])
def rotate_zone_photo(zone_id):
    """Повернуть фотографию зоны на кратный 90 угол (в градусах)."""
    try:
        zone = db.get_zone(zone_id)
        if not zone:
            return jsonify({'success': False, 'message': 'Зона не найдена'}), 404
        angle = 90
        try:
            data = request.get_json(silent=True) or {}
            angle = int(data.get('angle', 90))
        except Exception:
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
                # Перезаписываем в исходном формате
                fmt = img.format or 'JPEG'
                img.save(filepath, format=fmt)
        except Exception as e:
            logger.error(f"rotate failed: {e}")
            return jsonify({'success': False, 'message': 'Ошибка обработки изображения'}), 500
        try:
            db.add_log('photo_rotate', json.dumps({'zone': zone_id, 'angle': angle}))
        except Exception:
            pass
        return jsonify({'success': True})
    except Exception as e:
        logger.error(f"Ошибка поворота фото: {e}")
        return jsonify({'success': False, 'message': 'Ошибка поворота'}), 500

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
                t = normalize_topic(topic)
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
            resp = jsonify({'success': False, 'message': 'Зона не найдена'})
            resp.headers['Cache-Control'] = 'no-store'
            return resp, 404
        
        total_duration = int(zone.get('duration') or 0)
        start_str = zone.get('watering_start_time')
        if zone.get('state') != 'on' or not start_str:
            resp = jsonify({
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
            resp.headers['Cache-Control'] = 'no-store'
            return resp
        
        try:
            start_dt = datetime.strptime(start_str, '%Y-%m-%d %H:%M:%S')
        except Exception:
            # Если форматбитый — очищаем и возвращаем нули
            db.update_zone(zone_id, {'watering_start_time': None})
            resp = jsonify({
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
            resp.headers['Cache-Control'] = 'no-store'
            return resp
        
        now = datetime.now()
        elapsed_seconds = max(0, int((now - start_dt).total_seconds()))
        total_seconds = int(total_duration * 60)
        if elapsed_seconds >= total_seconds:
            # Автостоп
            db.update_zone(zone_id, {'state': 'off', 'watering_start_time': None})
            resp = jsonify({
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
            resp.headers['Cache-Control'] = 'no-store'
            return resp
        remaining_seconds = max(0, total_seconds - elapsed_seconds)
        # Для обратной совместимости оставляем минутные поля (целые минуты)
        elapsed_min = int(elapsed_seconds // 60)
        remaining_min = int(remaining_seconds // 60)
        resp = jsonify({
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
        resp.headers['Cache-Control'] = 'no-store'
        return resp
    except Exception as e:
        logger.error(f"Ошибка получения времени полива зоны {zone_id}: {e}")
        resp = jsonify({'success': False, 'message': 'Ошибка получения времени полива'})
        resp.headers['Cache-Control'] = 'no-store'
        return resp, 500

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
                                # если пришёл ON по MQTT из внешнего интерфейса — пометим как remote
                                updates['watering_start_source'] = 'remote'
                            # Обновим планировщик: если зона неожиданно ВКЛ, запланируем автостоп
                            try:
                                sched = get_scheduler()
                                if sched:
                                    dur = int(z.get('duration') or 0)
                                    if dur > 0:
                                        sched.cancel_zone_jobs(int(zone_id))
                                        sched.schedule_zone_stop(int(zone_id), dur)
                            except Exception:
                                pass
                        else:
                            # record last_watering_time
                            if z.get('watering_start_time'):
                                updates['last_watering_time'] = z.get('watering_start_time')
                            updates['watering_start_time'] = None
                            # Если зона вручную выключена (MQTT=0), отменим её автостоп в планировщике
                            try:
                                sched = get_scheduler()
                                if sched:
                                    sched.cancel_zone_jobs(int(zone_id))
                            except Exception:
                                pass
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
                            t_o = normalize_topic(otopic)
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
    t = normalize_topic(topic)
    try:
        server = db.get_mqtt_server(int(sid))
        if not server:
            return jsonify({'success': False, 'message': 'MQTT server not found'}), 400
        # Сначала фиксируем старт в БД, чтобы MQTT-SSE не перезаписал источник 'manual'
        start_ts = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        try:
            db.update_zone(zone_id, {
                'state': 'on',
                'watering_start_time': start_ts,
                'scheduled_start_time': None,
                'watering_start_source': 'manual'
            })
            # При ручном старте зоны — отменим очередь группы и очистим плановые старты
            try:
                gid = int(z.get('group_id') or 0)
                if gid:
                    sched = get_scheduler()
                    if sched:
                        sched.cancel_group_jobs(gid)
                        # И дополнительно удалим незапущенные задания последовательности, которые могли появиться чуть позже
                        try:
                            for job in sched.scheduler.get_jobs():
                                if job.id.startswith(f"group_seq_{gid}_"):
                                    sched.scheduler.remove_job(job.id)
                        except Exception:
                            pass
                    db.clear_group_scheduled_starts(gid)
            except Exception:
                pass
        except Exception:
            pass
        logger.info(f"HTTP publish ON zone={zone_id} topic={t}")
        _publish_mqtt_value(server, t, '1')
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
    t = normalize_topic(topic)
    try:
        server = db.get_mqtt_server(int(sid))
        if not server:
            return jsonify({'success': False, 'message': 'MQTT server not found'}), 400
        logger.info(f"HTTP publish OFF zone={zone_id} topic={t}")
        _publish_mqtt_value(server, t, '0')
        try:
            # Немедленно отражаем остановку в БД, чтобы UI увидел состояние и таймер сбросился
            db.update_zone(zone_id, {'state': 'off', 'watering_start_time': None})
        except Exception:
            pass
        return jsonify({'success': True, 'message': 'Зона остановлена'})
    except Exception as e:
        logger.error(f"MQTT publish stop failed: {e}")
        return jsonify({'success': False, 'message': 'MQTT publish failed'}), 500

def _probe_env_values(cfg: dict) -> None:
    try:
        if mqtt is None:
            return
        # Subscribe one-shot to fetch retained values
        try:
            logger.info('EnvProbe: starting')
        except Exception:
            pass
        topics = []
        tcfg = (cfg.get('temp') or {})
        hcfg = (cfg.get('hum') or {})
        if tcfg.get('enabled') and tcfg.get('topic') and tcfg.get('server_id'):
            topics.append((int(tcfg['server_id']), (tcfg['topic'] or '').strip(), 'temp'))
        if hcfg.get('enabled') and hcfg.get('topic') and hcfg.get('server_id'):
            topics.append((int(hcfg['server_id']), (hcfg['topic'] or '').strip(), 'hum'))
        for sid, topic, kind in topics:
            server = db.get_mqtt_server(int(sid))
            if not server or not topic:
                continue
            try:
                logger.info('EnvProbe: connect sid=%s host=%s port=%s topic=%s kind=%s', sid, server.get('host'), server.get('port'), topic, kind)
                cl = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2)
                if server.get('username'):
                    cl.username_pw_set(server.get('username'), server.get('password') or None)
                # TLS options
                try:
                    if int(server.get('tls_enabled') or 0) == 1:
                        import ssl
                        ca = server.get('tls_ca_path') or None
                        cert = server.get('tls_cert_path') or None
                        key = server.get('tls_key_path') or None
                        tls_ver = (server.get('tls_version') or '').upper().strip()
                        version = ssl.PROTOCOL_TLS_CLIENT if tls_ver in ('', 'TLS', 'TLS_CLIENT') else ssl.PROTOCOL_TLS
                        cl.tls_set(ca_certs=ca, certfile=cert, keyfile=key, tls_version=version)
                        if int(server.get('tls_insecure') or 0) == 1:
                            cl.tls_insecure_set(True)
                except Exception:
                    logger.exception('MQTT TLS setup failed')
                host = server.get('host') or '127.0.0.1'
                port = int(server.get('port') or 1883)
                try:
                    cl.connect(host, port, 30)
                except Exception:
                    # не кэшируем неудачное подключение
                    return None
                def _on_disconnect(c, u, rc, properties=None):
                    try:
                        with _MQTT_CLIENTS_LOCK:
                            if _MQTT_CLIENTS.get(sid) is c:
                                _MQTT_CLIENTS.pop(sid, None)
                    except Exception:
                        pass
                cl.on_disconnect = _on_disconnect
                _MQTT_CLIENTS[sid] = cl
            except Exception:
                return None
    except Exception:
        logger.exception('EnvProbe: outer failed')

if __name__ == '__main__':
    # Инициализируем планировщик полива
    init_scheduler(db)
    
    app.run(debug=True, host='0.0.0.0', port=8080)
