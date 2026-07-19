"""Shared master-valve helpers — единая логика перебора и закрытия
всех настроенных master-клапанов (boot sync, graceful shutdown).
"""

import logging
import time

from utils import normalize_topic

logger = logging.getLogger(__name__)


def iter_master_close_targets(groups):
    """Yield ``(server_id, topic, close_val)`` для каждого уникального master-клапана.

    ``topic`` нормализован; dedup по (server_id, topic) — один физический
    клапан может быть расшарен между группами. ``close_val`` mode-aware:
    NO → '1' (запитать = закрыть), иначе '0' (обесточить = закрыть).
    Кривые записи групп молча пропускаются (log-and-skip) — перебор не
    должен ронять ни boot sync, ни shutdown.
    """
    seen: set = set()
    for g in groups or []:
        try:
            if int(g.get("use_master_valve") or 0) != 1:
                continue
            mtopic = (g.get("master_mqtt_topic") or "").strip()
            msid = g.get("master_mqtt_server_id")
            if not mtopic or not msid:
                continue
            topic = normalize_topic(mtopic)
            key = (int(msid), topic)
            if key in seen:
                continue
            seen.add(key)
            try:
                mode = (g.get("master_mode") or "NC").strip().upper()
            except (ValueError, TypeError, KeyError, AttributeError) as e:
                logger.debug("iter_master_close_targets: bad master_mode: %s", e)
                mode = "NC"
            yield int(msid), topic, ("1" if mode == "NO" else "0")
        except (ValueError, TypeError, KeyError, AttributeError) as e:
            logger.debug("iter_master_close_targets: skip group: %s", e)
            continue


def close_all_master_valves(db, publish, retries: int = 3) -> int:
    """Закрыть все настроенные master-клапаны (mode-aware, retain, QoS 2).

    ``publish`` — callable с сигнатурой ``publish_mqtt_value``; публикация
    ретраится до ``retries`` раз. Возвращает число клапанов, для которых
    публикация подтверждена publisher-ом. Ошибки логируются, не пробрасываются.
    """
    closed = 0
    try:
        groups = db.get_groups() or []
    except (ConnectionError, TimeoutError, OSError) as e:
        logger.debug("close_all_master_valves: get_groups failed: %s", e)
        return 0
    for msid, topic, close_val in iter_master_close_targets(groups):
        try:
            server = db.get_mqtt_server(msid)
            if not server:
                logger.error("master valve close skipped: server unavailable sid=%s topic=%s", msid, topic)
                continue
            logger.info("Closing master valve sid=%s topic=%s val=%s", msid, topic, close_val)
            delivered = False
            for attempt in range(retries):
                ok = publish(server, topic, close_val, min_interval_sec=0.0, retain=True, qos=2)
                if ok:
                    delivered = True
                    break
                time.sleep(0.2 * (attempt + 1))
            if not delivered:
                logger.error("master valve close unresolved sid=%s topic=%s after %s attempts", msid, topic, retries)
                continue
            time.sleep(0.01)
            closed += 1
        except (ConnectionError, TimeoutError, OSError, RuntimeError, ValueError, TypeError):
            logger.exception("master valve close failed sid=%s topic=%s", msid, topic)
    return closed
