import logging
import threading
from datetime import datetime, timedelta
import json
from typing import Optional

from database import db
from utils import normalize_topic

try:
    import paho.mqtt.client as mqtt
except Exception:
    mqtt = None

logger = logging.getLogger(__name__)

class RainMonitor:
    def __init__(self):
        self.client = None
        self.topic = None
        self.server_id = None
        self.is_rain: Optional[bool] = None

    def start(self, cfg: dict):
        try:
            enabled = bool(cfg.get('enabled'))
            topic = (cfg.get('topic') or '').strip()
            server_id = cfg.get('server_id')
            if not enabled or not topic or not server_id or mqtt is None:
                return
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
        val = self._interpret_payload(payload)
        if val is None:
            return
        self.is_rain = bool(val)
        if self.is_rain:
            self._apply_rain_postpone()

    def _interpret_payload(self, payload: str) -> Optional[bool]:
        p = (payload or '').strip().lower()
        if p in ('1', 'true', 'rain', 'yes', 'on'):
            return True
        if p in ('0', 'false', 'no_rain', 'no', 'off'):
            return False
        return None

    def _apply_rain_postpone(self):
        try:
            groups = db.get_groups()
            target_groups = [int(g['id']) for g in groups if db.get_group_use_rain(int(g['id'])) and int(g['id']) != 999]
            if not target_groups:
                return
            postpone_until = (datetime.now() + timedelta(hours=1)).strftime('%Y-%m-%d %H:%M:%S')
            zones = db.get_zones()
            for z in zones:
                if int(z.get('group_id') or 0) in target_groups:
                    try:
                        db.update_zone_postpone(int(z['id']), postpone_until, 'rain')
                    except Exception:
                        pass
            try:
                db.add_log('rain_postpone', json.dumps({'groups': target_groups, 'until': postpone_until}))
            except Exception:
                pass
        except Exception:
            logger.exception('RainMonitor apply_postpone failed')

rain_monitor = RainMonitor()


def start_rain_monitor():
    try:
        cfg = db.get_rain_config()
        if bool(cfg.get('enabled')):
            rain_monitor.start(cfg)
    except Exception:
        logger.exception('start_rain_monitor failed')
