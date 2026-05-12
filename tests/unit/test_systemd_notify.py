"""Unit tests for services.systemd_notify (Wave 2 F4 — systemd watchdog)."""

import contextlib
import os
import socket
import threading
import time

import pytest


@pytest.fixture(autouse=True)
def _reset_heartbeat_state():
    """Make sure each test starts with a clean heartbeat thread state."""
    from services import systemd_notify as sn

    sn._HEARTBEAT_STOP.set()
    if sn._HEARTBEAT_THREAD is not None:
        sn._HEARTBEAT_THREAD.join(timeout=1.0)
    sn._HEARTBEAT_THREAD = None
    sn._HEARTBEAT_STOP = threading.Event()
    yield
    sn._HEARTBEAT_STOP.set()
    if sn._HEARTBEAT_THREAD is not None:
        sn._HEARTBEAT_THREAD.join(timeout=1.0)
    sn._HEARTBEAT_THREAD = None


def test_notify_noop_without_env(monkeypatch):
    """Test 1: when NOTIFY_SOCKET is unset, _notify returns False quietly."""
    from services import systemd_notify as sn

    monkeypatch.delenv("NOTIFY_SOCKET", raising=False)
    assert sn._notify("READY=1\n") is False
    assert sn.notify_ready() is False
    assert sn.notify_watchdog() is False
    assert sn.notify_stopping() is False


def test_notify_abstract_socket_prefix(monkeypatch):
    """Test 2: leading '@' in NOTIFY_SOCKET is converted to NUL byte."""
    from services import systemd_notify as sn

    captured = {}

    class _FakeSock:
        def __init__(self, *a, **kw):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def sendto(self, data, addr):
            captured["data"] = data
            captured["addr"] = addr

    monkeypatch.setenv("NOTIFY_SOCKET", "@fake-abstract")
    monkeypatch.setattr(sn.socket, "socket", lambda *a, **kw: _FakeSock())

    assert sn._notify("READY=1\n") is True
    assert captured["addr"].startswith("\0")
    assert captured["addr"] == "\0fake-abstract"
    assert captured["data"] == b"READY=1\n"


def test_notify_ready_sends_proper_payload(monkeypatch, tmp_path):
    """Test 3: real AF_UNIX datagram socket roundtrip — READY=1 + STATUS line."""
    from services import systemd_notify as sn

    sock_path = str(tmp_path / "notify.sock")
    srv = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
    srv.bind(sock_path)
    srv.settimeout(2.0)

    monkeypatch.setenv("NOTIFY_SOCKET", sock_path)
    try:
        assert sn.notify_ready(status="Application ready") is True
        data, _ = srv.recvfrom(4096)
        assert data == b"READY=1\nSTATUS=Application ready\n"
    finally:
        srv.close()
        with contextlib.suppress(OSError):
            os.unlink(sock_path)


def test_heartbeat_thread_starts_and_stops(monkeypatch):
    """Test 4: heartbeat thread sends multiple WATCHDOG=1 and stops cleanly."""
    from services import systemd_notify as sn

    calls = []

    def _fake_notify(message):
        calls.append(message)
        return True

    monkeypatch.setattr(sn, "_notify", _fake_notify)
    monkeypatch.setattr(sn, "_WATCHDOG_INTERVAL_SEC", 0.05)
    monkeypatch.setenv("WB_WATCHDOG_ENABLED", "1")

    sn.start_heartbeat()
    time.sleep(0.2)
    sn.stop_heartbeat(timeout=1.0)

    watchdog_calls = [c for c in calls if c == "WATCHDOG=1\n"]
    assert len(watchdog_calls) >= 3, f"expected >=3 WATCHDOG pings, got {len(watchdog_calls)}"
    assert sn._HEARTBEAT_THREAD is None or not sn._HEARTBEAT_THREAD.is_alive()


def test_heartbeat_disabled_via_env(monkeypatch):
    """Test 5: WB_WATCHDOG_ENABLED=0 causes start_heartbeat() to be a no-op."""
    from services import systemd_notify as sn

    monkeypatch.setenv("WB_WATCHDOG_ENABLED", "0")
    sn.start_heartbeat()
    assert sn._HEARTBEAT_THREAD is None


def test_notify_survives_socket_error(monkeypatch):
    """Test 6: if socket.sendto raises OSError, _notify returns False cleanly."""
    from services import systemd_notify as sn

    class _BrokenSock:
        def __init__(self, *a, **kw):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def sendto(self, data, addr):
            raise OSError("simulated broken socket")

    monkeypatch.setenv("NOTIFY_SOCKET", "/tmp/does-not-matter")
    monkeypatch.setattr(sn.socket, "socket", lambda *a, **kw: _BrokenSock())

    # No exception should bubble; result is False.
    assert sn._notify("WATCHDOG=1\n") is False
    assert sn.notify_watchdog() is False
