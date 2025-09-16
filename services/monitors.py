import logging
import threading
from datetime import datetime
from typing import Optional, Dict, Deque, Tuple
from collections import deque

from database import db

try:
    import paho.mqtt.client as mqtt
except Exception:
    mqtt = None

logger = logging.getLogger(__name__)

class RainMonitor:
    def __init__(self):
        self.client = None
        self.topic: Optional[str] = None
        self.server_id: Optional[int] = None
        self.is_rain: Optional[bool] = None
        self._cfg: dict | None = None

    def start(self, cfg: dict):
        try:
            enabled = bool(cfg.get('enabled'))
            topic = (cfg.get('topic') or '').strip()
            server_id = cfg.get('server_id')
            if not enabled or not topic or not server_id or mqtt is None:
                return
            # keep config to evaluate NO/NC logic on RX
            self._cfg = dict(cfg or {})
            self.topic = topic
            self.server_id = int(server_id)
            self._ensure_client()
        except Exception:
            logger.exception('RainMonitor start failed')

    def _ensure_client(self):
        try:
            sid = self.server_id
            server = db.get_mqtt_server(int(sid))
            if not server:
                return
            cl = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2)
            if server.get('username'):
                cl.username_pw_set(server.get('username'), server.get('password') or None)
            host = server.get('host') or '127.0.0.1'
            port = int(server.get('port') or 1883)
            def _on_message(c, u, msg):
                try:
                    payload = getattr(msg, 'payload', b'')
                    try:
                        payload = payload.decode('utf-8', errors='ignore')
                    except Exception:
                        payload = str(payload)
                    self._handle_payload(str(payload))
                except Exception:
                    logger.exception('RainMonitor on_message failed')
            cl.on_message = _on_message
            cl.connect(host, port, 30)
            cl.subscribe(self.topic, qos=0)
            cl.loop_start()
            self.client = cl
        except Exception:
            logger.exception('RainMonitor client init failed')

    def _handle_payload(self, payload: str):
        p = (payload or '').strip().lower()
        # normalize primitive textual signals to boolean
        val = True if p in ('1', 'true', 'rain', 'yes', 'on') else False if p in ('0', 'false', 'no_rain', 'no', 'off') else None
        if val is None:
            return
        # Respect sensor type: NC means inverted logical value
        try:
            sensor_type = str((self._cfg or {}).get('type') or db.get_rain_config().get('type') or 'NO').upper()
        except Exception:
            sensor_type = 'NO'
        logical_rain = bool(val)
        if sensor_type == 'NC':
            logical_rain = not logical_rain
        self.is_rain = logical_rain
        if self.is_rain:
            self._on_rain_start()
        else:
            self._on_rain_stop()

    def _on_rain_start(self):
        """Rain started: for groups using rain sensor
        - stop ongoing watering
        - postpone watering until end of day (will be cleared on rain stop)
        - cancel today's scheduled program runs for those groups
        """
        try:
            groups = db.get_groups()
            target_groups = [int(g['id']) for g in groups if db.get_group_use_rain(int(g['id'])) and int(g['id']) != 999]
            if not target_groups:
                return
            # 1) Stop any watering in affected groups
            try:
                from services.zone_control import stop_all_in_group
                for gid in target_groups:
                    try:
                        stop_all_in_group(gid, reason='rain', force=True)
                    except Exception:
                        logger.exception('RainMonitor: stop_all_in_group failed')
            except Exception:
                logger.exception('RainMonitor: import stop_all_in_group failed')
            # 2) Set postpone for zones in affected groups (until end of day)
            from datetime import datetime as _dt
            postpone_until = _dt.now().strftime('%Y-%m-%d 23:59:59')
            zones = db.get_zones()
            for z in zones:
                if int(z.get('group_id') or 0) in target_groups:
                    try:
                        db.update_zone_postpone(int(z['id']), postpone_until, 'rain')
                    except Exception:
                        pass
            # Note: не отменяем сегодняшние запуски программ заранее.
            # Если дождь начался до времени старта — при отсутствии отложки к моменту старта программа отработает.
            # Если дождь начался во время выполнения — оставшиеся зоны пропустятся из-за отложки.
            try:
                db.add_log('rain_postpone', str({'groups': target_groups, 'until': postpone_until}))
            except Exception:
                pass
        except Exception:
            logger.exception('RainMonitor on_rain_start failed')

    def _on_rain_stop(self):
        """Rain stopped: clear only rain-related postpones for groups using rain sensor."""
        try:
            groups = db.get_groups()
            target_groups = [int(g['id']) for g in groups if db.get_group_use_rain(int(g['id'])) and int(g['id']) != 999]
            if not target_groups:
                return
            zones = db.get_zones()
            for z in zones:
                try:
                    if int(z.get('group_id') or 0) not in target_groups:
                        continue
                    # Clear postpone only if it was set due to rain
                    if (z.get('postpone_reason') or '') == 'rain':
                        db.update_zone_postpone(int(z['id']), None, None)
                except Exception:
                    pass
            # После окончания дождя — уберём отмены программ на сегодня для этих групп, чтобы ближайшие вечерние сработали
            try:
                from datetime import datetime as _dt
                today = _dt.now().strftime('%Y-%m-%d')
                for gid in target_groups:
                    try:
                        db.clear_program_cancellations_for_group_on_date(int(gid), today)
                    except Exception:
                        pass
            except Exception:
                pass
            try:
                db.add_log('rain_resume', str({'groups': target_groups}))
            except Exception:
                pass
        except Exception:
            logger.exception('RainMonitor on_rain_stop failed')

class EnvMonitor:
    def __init__(self):
        self.temp_client = None
        self.hum_client = None
        self.temp_value: Optional[float] = None
        self.hum_value: Optional[float] = None
        self._lock = threading.Lock()

    def start(self, cfg: dict):
        try:
            if mqtt is None:
                logger.warning('EnvMonitor start skipped: paho-mqtt not available')
                return
            tcfg = cfg.get('temp') or {}
            hcfg = cfg.get('hum') or {}
            if tcfg.get('enabled') and tcfg.get('topic') and tcfg.get('server_id'):
                self._start_temp(int(tcfg['server_id']), str(tcfg['topic']))
            if hcfg.get('enabled') and hcfg.get('topic') and hcfg.get('server_id'):
                self._start_hum(int(hcfg['server_id']), str(hcfg['topic']))
        except Exception:
            logger.exception('EnvMonitor start failed')

    def _start_temp(self, server_id: int, topic: str):
        try:
            server = db.get_mqtt_server(server_id)
            if not server:
                return
            cl = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2)
            if server.get('username'):
                cl.username_pw_set(server.get('username'), server.get('password') or None)
            def _on_message(c, u, msg):
                try:
                    p = msg.payload.decode('utf-8', errors='ignore').strip()
                    try:
                        val = float(p)
                        with self._lock:
                            self.temp_value = val
                    except Exception:
                        logger.exception('EnvMonitor temp parse failed')
                except Exception:
                    logger.exception('EnvMonitor temp RX failed')
            cl.on_message = _on_message
            cl.connect(server.get('host') or '127.0.0.1', int(server.get('port') or 1883), 10)
            cl.subscribe(topic, qos=0)
            cl.loop_start()
            self.temp_client = cl
        except Exception:
            logger.exception('EnvMonitor temp start failed')

    def _start_hum(self, server_id: int, topic: str):
        try:
            server = db.get_mqtt_server(server_id)
            if not server:
                return
            cl = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2)
            if server.get('username'):
                cl.username_pw_set(server.get('username'), server.get('password') or None)
            def _on_message(c, u, msg):
                try:
                    p = msg.payload.decode('utf-8', errors='ignore').strip()
                    try:
                        val = float(p)
                        with self._lock:
                            self.hum_value = val
                    except Exception:
                        logger.exception('EnvMonitor hum parse failed')
                except Exception:
                    logger.exception('EnvMonitor hum RX failed')
            cl.on_message = _on_message
            cl.connect(server.get('host') or '127.0.0.1', int(server.get('port') or 1883), 10)
            cl.subscribe(topic, qos=0)
            cl.loop_start()
            self.hum_client = cl
        except Exception:
            logger.exception('EnvMonitor hum start failed')

rain_monitor = RainMonitor()
env_monitor = EnvMonitor()

class WaterMonitor:
    """Подписывается на топики счётчиков воды по группам, хранит последние импульсы и рассчитывает поток."""
    def __init__(self):
        self._clients: Dict[int, mqtt.Client] = {}  # key: group_id
        self._topics: Dict[int, str] = {}
        self._server_ids: Dict[int, int] = {}
        self._pulse_liters: Dict[int, int] = {}  # 1|10|100
        self._samples: Dict[int, Deque[Tuple[float, int]]] = {}  # ts, pulses
        self._lock = threading.Lock()

    def start(self):
        try:
            if mqtt is None:
                return
            groups = db.get_groups() or []
            for g in groups:
                try:
                    gid = int(g.get('id'))
                    if gid == 999:
                        continue
                    if int(g.get('use_water_meter') or 0) != 1:
                        continue
                    topic = (g.get('water_mqtt_topic') or '').strip()
                    sid = g.get('water_mqtt_server_id')
                    if not topic or not sid:
                        continue
                    pulse = str(g.get('water_pulse_size') or '1l')
                    liters = 100 if pulse == '100l' else 10 if pulse == '10l' else 1
                    # already started?
                    if gid in self._clients:
                        # update settings
                        self._topics[gid] = topic
                        self._server_ids[gid] = int(sid)
                        self._pulse_liters[gid] = liters
                        continue
                    server = db.get_mqtt_server(int(sid))
                    if not server:
                        continue
                    cl = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2)
                    if server.get('username'):
                        cl.username_pw_set(server.get('username'), server.get('password') or None)
                    def _on_message(c, u, msg, _gid=gid):
                        try:
                            p = msg.payload.decode('utf-8', errors='ignore').strip()
                            pulses = int(''.join([ch for ch in p if (ch.isdigit() or ch=='-')]))
                            ts = datetime.now().timestamp()
                            with self._lock:
                                dq = self._samples.setdefault(_gid, deque(maxlen=256))
                                dq.append((ts, pulses))
                        except Exception:
                            logger.exception('WaterMonitor on_message failed')
                    cl.on_message = _on_message
                    cl.connect(server.get('host') or '127.0.0.1', int(server.get('port') or 1883), 10)
                    cl.subscribe(topic, qos=0)
                    cl.loop_start()
                    with self._lock:
                        self._clients[gid] = cl
                        self._topics[gid] = topic
                        self._server_ids[gid] = int(sid)
                        self._pulse_liters[gid] = liters
                        self._samples.setdefault(gid, deque(maxlen=256))
                except Exception:
                    logger.exception('WaterMonitor start group failed')
        except Exception:
            logger.exception('WaterMonitor start failed')

    def get_current_reading_m3(self, group_id: int) -> Optional[float]:
        try:
            g = next((gg for gg in (db.get_groups() or []) if int(gg.get('id')) == int(group_id)), None)
            if not g:
                return None
            base_m3 = float(g.get('water_base_value_m3') or 0.0)
            base_p = int(g.get('water_base_pulses') or 0)
            liters = self._pulse_liters.get(int(group_id), 1)
            with self._lock:
                dq = self._samples.get(int(group_id)) or deque()
                cur_p = dq[-1][1] if dq else base_p
            delta_p = max(0, cur_p - base_p)
            val = base_m3 + (delta_p * liters) / 1000.0
            return round(val, 3)
        except Exception:
            return None

    def get_flow_lpm(self, group_id: int, since_iso: Optional[str]) -> Optional[float]:
        try:
            if not since_iso:
                return None
            try:
                since_ts = datetime.strptime(since_iso, '%Y-%m-%d %H:%M:%S').timestamp()
            except Exception:
                return None
            liters = self._pulse_liters.get(int(group_id), 1)
            with self._lock:
                dq = list(self._samples.get(int(group_id)) or [])
            # filter samples after start
            samples = [(ts, p) for ts, p in dq if ts >= since_ts]
            if len(samples) < 2:
                return None
            # Prefer at least 5 increments if available
            p0 = samples[0][1]
            idx = 0
            target_delta = 5
            for i in range(1, len(samples)):
                if samples[i][1] - p0 >= target_delta:
                    idx = i
                    break
            if idx == 0:
                idx = len(samples) - 1
            ts0, p_start = samples[0]
            ts1, p_end = samples[idx]
            dp = max(0, p_end - p_start)
            dt_sec = max(1.0, ts1 - ts0)
            lpm = (dp * liters) / (dt_sec / 60.0)
            return round(lpm, 2)
        except Exception:
            return None

    def summarize_run(self, group_id: int, since_iso: Optional[str]) -> Tuple[Optional[float], Optional[float]]:
        """Возвращает (total_liters, avg_flow_lpm) за интервал с момента старта до последнего сэмпла."""
        try:
            if not since_iso:
                return (None, None)
            try:
                since_ts = datetime.strptime(since_iso, '%Y-%m-%d %H:%M:%S').timestamp()
            except Exception:
                return (None, None)
            liters_per_pulse = self._pulse_liters.get(int(group_id), 1)
            with self._lock:
                dq = list(self._samples.get(int(group_id)) or [])
            samples = [(ts, p) for ts, p in dq if ts >= since_ts]
            if len(samples) < 2:
                return (0.0, 0.0)
            ts0, p0 = samples[0]
            ts1, p1 = samples[-1]
            total_p = max(0, p1 - p0)
            total_l = total_p * liters_per_pulse
            dt_min = max(0.001, (ts1 - ts0) / 60.0)
            avg_lpm = total_l / dt_min
            return (round(total_l, 2), round(avg_lpm, 2))
        except Exception:
            return (None, None)

    def get_raw_pulses(self, group_id: int) -> Optional[int]:
        """Возвращает последние сырые импульсы для группы (или None, если нет сэмплов)."""
        try:
            with self._lock:
                dq = self._samples.get(int(group_id))
                if not dq or len(dq) == 0:
                    return None
                return int(dq[-1][1])
        except Exception:
            return None

    def get_pulses_at_or_before(self, group_id: int, ts: float) -> Optional[int]:
        """Пульсы на момент ts (берём последний сэмпл с ts' <= заданного)."""
        try:
            with self._lock:
                arr = list(self._samples.get(int(group_id)) or [])
            if not arr:
                return None
            best = None
            for t, p in arr:
                if t <= ts:
                    best = p
                else:
                    break
            return int(best) if best is not None else int(arr[0][1]) if arr else None
        except Exception:
            return None

    def get_pulses_at_or_after(self, group_id: int, ts: float) -> Optional[int]:
        """Пульсы после/на момент ts (берём первый сэмпл с ts' >= заданного)."""
        try:
            with self._lock:
                arr = list(self._samples.get(int(group_id)) or [])
            if not arr:
                return None
            for t, p in arr:
                if t >= ts:
                    return int(p)
            return int(arr[-1][1]) if arr else None
        except Exception:
            return None

water_monitor = WaterMonitor()

def start_rain_monitor():
    try:
        cfg = db.get_rain_config()
        if cfg and bool(cfg.get('enabled')):
            rain_monitor.start(cfg)
    except Exception:
        logger.exception('start_rain_monitor failed')

def start_env_monitor(cfg: dict):
    try:
        env_monitor.start(cfg or {})
    except Exception:
        logger.exception('start_env_monitor failed')

def start_water_monitor():
    try:
        water_monitor.start()
    except Exception:
        logger.exception('start_water_monitor failed')
