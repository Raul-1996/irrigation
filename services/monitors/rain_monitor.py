import logging
import sqlite3

from database import db

try:
    import paho.mqtt.client as mqtt
except ImportError as e:
    mqtt = None

logger = logging.getLogger(__name__)


class RainMonitor:
    def __init__(self):
        self.client = None
        self.topic: str | None = None
        self.server_id: int | None = None
        self.is_rain: bool | None = None
        self._cfg: dict | None = None

    def stop(self):
        try:
            if self.client is not None:
                self.client.loop_stop()
                self.client.disconnect()
        except (ConnectionError, TimeoutError, OSError):
            logger.exception('RainMonitor stop failed')
        self.client = None

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
        except (ConnectionError, TimeoutError, OSError):
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
                    except (UnicodeDecodeError, AttributeError) as e:
                        logger.debug("Exception in _on_message: %s", e)
                        payload = str(payload)
                    self._handle_payload(str(payload))
                except (ValueError, TypeError, KeyError):
                    logger.exception('RainMonitor on_message failed')
            cl.on_message = _on_message
            cl.connect(host, port, 30)
            cl.subscribe(self.topic, qos=0)
            cl.loop_start()
            self.client = cl
        except (ConnectionError, TimeoutError, OSError):
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
        except (sqlite3.Error, OSError) as e:
            logger.debug("Exception in _handle_payload: %s", e)
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
                    except (ConnectionError, TimeoutError, OSError, sqlite3.Error):
                        logger.exception('RainMonitor: stop_all_in_group failed')
            except ImportError:
                logger.exception('RainMonitor: import stop_all_in_group failed')
            # 2) Set postpone for zones in affected groups (until end of day)
            from datetime import datetime as _dt
            postpone_until = _dt.now().strftime('%Y-%m-%d 23:59:59')
            zones = db.get_zones()
            for z in zones:
                if int(z.get('group_id') or 0) in target_groups:
                    try:
                        db.update_zone_postpone(int(z['id']), postpone_until, 'rain')
                    except (sqlite3.Error, OSError) as e:
                        logger.debug("Handled exception in _on_rain_start: %s", e)
            # Note: не отменяем сегодняшние запуски программ заранее.
            # Если дождь начался до времени старта — при отсутствии отложки к моменту старта программа отработает.
            # Если дождь начался во время выполнения — оставшиеся зоны пропустятся из-за отложки.
            try:
                db.add_log('rain_postpone', str({'groups': target_groups, 'until': postpone_until}))
            except (sqlite3.Error, OSError) as e:
                logger.debug("Handled exception in line_136: %s", e)
        except ImportError:
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
                except (sqlite3.Error, OSError) as e:
                    logger.debug("Handled exception in _on_rain_stop: %s", e)
            # После окончания дождя — уберём отмены программ на сегодня для этих групп, чтобы ближайшие вечерние сработали
            try:
                from datetime import datetime as _dt
                today = _dt.now().strftime('%Y-%m-%d')
                for gid in target_groups:
                    try:
                        db.clear_program_cancellations_for_group_on_date(int(gid), today)
                    except (sqlite3.Error, OSError) as e:
                        logger.debug("Handled exception in _on_rain_stop: %s", e)
            except ImportError as e:
                logger.debug("Handled exception in _on_rain_stop: %s", e)
            try:
                db.add_log('rain_resume', str({'groups': target_groups}))
            except (sqlite3.Error, OSError) as e:
                logger.debug("Handled exception in line_171: %s", e)
        except ImportError:
            logger.exception('RainMonitor on_rain_stop failed')


rain_monitor = RainMonitor()


def start_rain_monitor():
    try:
        cfg = db.get_rain_config()
        if cfg and bool(cfg.get('enabled')):
            rain_monitor.start(cfg)
    except (sqlite3.Error, OSError):
        logger.exception('start_rain_monitor failed')
