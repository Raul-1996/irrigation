import logging
import threading
from datetime import datetime
from typing import Optional

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
            # 3) Cancel today's scheduled program runs for affected groups
            try:
                progs = db.get_programs()
            except Exception:
                progs = []
            today_wd = _dt.now().weekday()
            today_date = _dt.now().strftime('%Y-%m-%d')
            for p in progs or []:
                try:
                    days = p.get('days') or []
                    zones_list = p.get('zones') or []
                except Exception:
                    days, zones_list = [], []
                if today_wd not in days:
                    continue
                # Determine groups that program affects
                try:
                    affected_groups = set()
                    for z in zones:
                        try:
                            if int(z.get('id')) in zones_list and int(z.get('group_id') or 0) in target_groups:
                                affected_groups.add(int(z.get('group_id') or 0))
                        except Exception:
                            continue
                    for gid in affected_groups:
                        try:
                            db.cancel_program_run_for_group(int(p.get('id')), today_date, int(gid))
                        except Exception:
                            logger.exception('RainMonitor: cancel_program_run_for_group failed')
                except Exception:
                    logger.exception('RainMonitor: computing affected groups failed')
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
