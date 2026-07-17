"""Единый postpone-сервис: откладывание полива группы.

Полный набор шагов для любого канала (HTTP API, Telegram):
postpone_until всем зонам группы, аудируемая остановка активных зон
с публикацией MQTT '0', снятие заданий планировщика (cancel_group_jobs
ставит cancel-event, снимает zone/group_seq-job'ы и перестраивает
расписание группы) и запись postpone_set в журнал.
"""

import json
import logging
import sqlite3
from datetime import datetime, timedelta

from database import db
from services.mqtt_pub import publish_mqtt_value
from utils import normalize_topic

try:
    import paho.mqtt.client as mqtt
except ImportError:
    mqtt = None

logger = logging.getLogger(__name__)


def postpone_group(group_id: int, days: int, source: str = "api") -> dict:
    """Отложить полив всех зон группы на ``days`` дней (до конца дня).

    Возвращает {"postpone_until": str-до-конца-дня, "postpone_date": datetime}.
    """
    group_id = int(group_id)
    days = int(days)
    postpone_date = datetime.now() + timedelta(days=days)
    postpone_until = postpone_date.strftime("%Y-%m-%d 23:59:59")
    zones = db.get_zones() or []
    group_zones = [z for z in zones if int(z.get("group_id") or 0) == group_id]
    for zone in group_zones:
        db.update_zone_postpone(zone["id"], postpone_until, "manual")
    try:
        for zone in group_zones:
            try:
                if (zone.get("state") == "on") or zone.get("watering_start_time"):
                    # Postpone applies to a group — each zone going OFF
                    # is an audited transition (operator action) that we
                    # want to see in audit_log.
                    try:
                        from services.zones_state import update_zone_state as _uzs

                        _uzs(
                            zone["id"],
                            {"state": "off", "watering_start_time": None},
                            audit_reason="postpone_action",
                        )
                    except (sqlite3.Error, OSError, ImportError):
                        logger.exception(
                            "postpone_group: audited path failed zone=%s — falling back to raw update_zone",
                            zone.get("id"),
                        )
                        db.update_zone(zone["id"], {"state": "off", "watering_start_time": None})
                    sid = zone.get("mqtt_server_id")
                    topic = (zone.get("topic") or "").strip()
                    if mqtt and sid and topic:
                        t = normalize_topic(topic)
                        server = db.get_mqtt_server(int(sid))
                        if server:
                            publish_mqtt_value(server, t, "0", min_interval_sec=0.0, qos=2, retain=True)
            except (ConnectionError, TimeoutError, OSError):
                logger.exception("Ошибка остановки зоны при установке отложенного полива")
        try:
            from irrigation_scheduler import get_scheduler

            scheduler = get_scheduler()
            if scheduler:
                scheduler.cancel_group_jobs(group_id)
        except (ImportError, ValueError, KeyError, RuntimeError):
            logger.exception("Ошибка отмены заданий планировщика при отложенном поливе группы")
    except (ConnectionError, TimeoutError, OSError):
        logger.exception("Ошибка массовой остановки зон при отложенном поливе группы")
    db.add_log(
        "postpone_set", json.dumps({"group": group_id, "days": days, "until": postpone_until, "source": source})
    )
    return {"postpone_until": postpone_until, "postpone_date": postpone_date}
