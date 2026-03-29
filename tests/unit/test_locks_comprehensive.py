"""Comprehensive tests for services/locks.py."""
import pytest
import os
import threading

os.environ['TESTING'] = '1'


class TestGroupLock:
    def test_get_group_lock(self):
        from services.locks import group_lock
        lk = group_lock(1)
        assert lk is not None
        assert hasattr(lk, 'acquire')

    def test_same_group_same_lock(self):
        from services.locks import group_lock
        lk1 = group_lock(100)
        lk2 = group_lock(100)
        assert lk1 is lk2

    def test_different_groups_different_locks(self):
        from services.locks import group_lock
        lk1 = group_lock(200)
        lk2 = group_lock(201)
        assert lk1 is not lk2


class TestZoneLock:
    def test_get_zone_lock(self):
        from services.locks import zone_lock
        lk = zone_lock(1)
        assert lk is not None
        assert hasattr(lk, 'acquire')

    def test_same_zone_same_lock(self):
        from services.locks import zone_lock
        lk1 = zone_lock(300)
        lk2 = zone_lock(300)
        assert lk1 is lk2


class TestSnapshots:
    def test_snapshot_group_locks(self):
        from services.locks import snapshot_group_locks
        result = snapshot_group_locks()
        assert isinstance(result, dict)

    def test_snapshot_zone_locks(self):
        from services.locks import snapshot_zone_locks
        result = snapshot_zone_locks()
        assert isinstance(result, dict)

    def test_snapshot_all_locks(self):
        from services.locks import snapshot_all_locks
        result = snapshot_all_locks()
        assert 'groups' in result
        assert 'zones' in result

    def test_locked_status(self):
        import threading
        from services.locks import group_lock, snapshot_group_locks
        lk = group_lock(500)
        acquired = threading.Event()
        done = threading.Event()
        def hold_lock():
            lk.acquire()
            acquired.set()
            done.wait(timeout=5)
            lk.release()
        t = threading.Thread(target=hold_lock, daemon=True)
        t.start()
        acquired.wait(timeout=5)
        snap = snapshot_group_locks()
        assert snap.get(500) is True
        done.set()
        t.join(timeout=5)
