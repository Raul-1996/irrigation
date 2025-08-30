import logging
from datetime import datetime
from typing import Optional

from database import db
from services.mqtt_pub import publish_mqtt_value
from utils import normalize_topic

logger = logging.getLogger(__name__)


def exclusive_start_zone(zone_id: int) -> bool:
    """Start zone and stop others in its group. Returns True on success."""
    try:
        z = db.get_zone(zone_id)
        if not z:
            return False
        group_id = int(z.get('group_id') or 0)
        group_zones = db.get_zones_by_group(group_id) if group_id else []
        start_ts = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        # Start current
        db.update_zone(zone_id, {'state': 'on', 'watering_start_time': start_ts})
        try:
            sid = z.get('mqtt_server_id'); topic = (z.get('topic') or '').strip()
            if sid and topic:
                server = db.get_mqtt_server(int(sid))
                if server:
                    publish_mqtt_value(server, normalize_topic(topic), '1')
        except Exception:
            logger.exception("exclusive_start_zone: mqtt on failed")
        # Stop others
        for other in group_zones:
            oid = int(other.get('id'))
            if oid == int(zone_id):
                continue
            db.update_zone(oid, {'state': 'off', 'watering_start_time': None})
            try:
                osid = other.get('mqtt_server_id'); otopic = (other.get('topic') or '').strip()
                if osid and otopic:
                    server_o = db.get_mqtt_server(int(osid))
                    if server_o:
                        publish_mqtt_value(server_o, normalize_topic(otopic), '0', min_interval_sec=0.0)
            except Exception:
                logger.exception("exclusive_start_zone: mqtt off peer failed")
        return True
    except Exception:
        logger.exception("exclusive_start_zone failed")
        return False


def stop_zone(zone_id: int) -> bool:
    try:
        z = db.get_zone(zone_id)
        if not z:
            return False
        last_time = z.get('watering_start_time')
        db.update_zone(zone_id, {'state': 'off', 'watering_start_time': None, 'last_watering_time': last_time})
        sid = z.get('mqtt_server_id'); topic = (z.get('topic') or '').strip()
        try:
            if sid and topic:
                server = db.get_mqtt_server(int(sid))
                if server:
                    publish_mqtt_value(server, normalize_topic(topic), '0')
        except Exception:
            logger.exception('stop_zone: mqtt off failed')
        return True
    except Exception:
        logger.exception('stop_zone failed')
        return False
