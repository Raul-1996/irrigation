import threading
from typing import Dict

_group_locks: Dict[int, threading.RLock] = {}
_zone_locks: Dict[int, threading.RLock] = {}
_gl_lock = threading.Lock()

def group_lock(group_id: int) -> threading.RLock:
    with _gl_lock:
        lk = _group_locks.get(int(group_id))
        if lk is None:
            lk = threading.RLock()
            _group_locks[int(group_id)] = lk
        return lk

def zone_lock(zone_id: int) -> threading.RLock:
    with _gl_lock:
        lk = _zone_locks.get(int(zone_id))
        if lk is None:
            lk = threading.RLock()
            _zone_locks[int(zone_id)] = lk
        return lk



def _is_locked(lock: threading.RLock) -> bool:
    try:
        # Если не удаётся захватить немедленно — значит, удерживается другим потоком
        acquired = lock.acquire(blocking=False)
        if acquired:
            # Мы его не удерживали — отпускаем
            try:
                lock.release()
            except Exception:
                pass
            return False
        return True
    except Exception:
        return False


def snapshot_group_locks() -> Dict[int, bool]:
    """Возвращает {group_id: locked_bool}."""
    with _gl_lock:
        return {gid: _is_locked(lk) for gid, lk in _group_locks.items()}


def snapshot_zone_locks() -> Dict[int, bool]:
    """Возвращает {zone_id: locked_bool}."""
    with _gl_lock:
        return {zid: _is_locked(lk) for zid, lk in _zone_locks.items()}


def snapshot_all_locks() -> Dict[str, Dict[int, bool]]:
    return {
        'groups': snapshot_group_locks(),
        'zones': snapshot_zone_locks(),
    }
