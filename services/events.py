import logging
import threading
import time
from typing import Any, Callable

from constants import DEDUP_SET_MAX_SIZE, DEDUP_TTL_SEC

logger = logging.getLogger(__name__)

_BUS_LOCK = threading.Lock()
_SUBS = []  # list[Callable[[dict], None]]
_DEDUP: dict[str, float] = {}  # key -> ts первой публикации
_DEDUP_TTL = float(DEDUP_TTL_SEC)


def publish(event: dict[str, Any]) -> None:
    try:
        key = f"{event.get('type')}:{event.get('id') or event.get('event_id') or event.get('ts')}"
        now = time.time()
        with _BUS_LOCK:
            # Simple TTL-based dedup
            _cleanup(now)
            if key in _DEDUP:
                return
            _DEDUP[key] = now
            subs = list(_SUBS)
        for cb in subs:
            try:
                cb(dict(event))
            except (KeyError, TypeError, ValueError) as e:
                logger.debug("Handled exception in publish: %s", e)
    except (ConnectionError, TimeoutError, OSError) as e:
        logger.debug("Handled exception in publish: %s", e)


def subscribe(callback: Callable[[dict[str, Any]], None]) -> None:
    with _BUS_LOCK:
        _SUBS.append(callback)


def _cleanup(now: float) -> None:
    expired = [k for k, ts in _DEDUP.items() if now - ts > _DEDUP_TTL]
    for k in expired:
        del _DEDUP[k]
    if len(_DEDUP) > DEDUP_SET_MAX_SIZE:
        _DEDUP.clear()
