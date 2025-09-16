import threading
import queue
import time
import json
from typing import Callable, Dict, Any

_BUS_LOCK = threading.Lock()
_SUBS = []  # list[Callable[[dict], None]]
_DEDUP = set()
_DEDUP_TTL = 300.0

def publish(event: Dict[str, Any]) -> None:
    try:
        key = f"{event.get('type')}:{event.get('id') or event.get('event_id') or event.get('ts')}"
        now = time.time()
        with _BUS_LOCK:
            # Simple TTL-based dedup
            _cleanup(now)
            if key in _DEDUP:
                return
            _DEDUP.add(key)
            subs = list(_SUBS)
        for cb in subs:
            try:
                cb(dict(event))
            except Exception:
                pass
    except Exception:
        pass

def subscribe(callback: Callable[[Dict[str, Any]], None]) -> None:
    with _BUS_LOCK:
        _SUBS.append(callback)

def _cleanup(now: float) -> None:
    if len(_DEDUP) > 4096:
        _DEDUP.clear()

