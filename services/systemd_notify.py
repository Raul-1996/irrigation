"""systemd sd_notify bridge (NOTIFY_SOCKET) - no C dependencies.

Protocol: datagram to $NOTIFY_SOCKET with ASCII payload.
  READY=1            - boot complete; systemd marks service active (Type=notify).
  STATUS=...         - human-readable status.
  WATCHDOG=1         - heartbeat (WatchdogSec required in unit).
  STOPPING=1         - graceful shutdown starting.

When $NOTIFY_SOCKET is not set (dev / not under systemd), every call is a no-op.
"""

from __future__ import annotations

import logging
import os
import socket
import threading

logger = logging.getLogger(__name__)

_HEARTBEAT_THREAD: threading.Thread | None = None
_HEARTBEAT_STOP = threading.Event()
_WATCHDOG_INTERVAL_SEC = 20  # WatchdogSec=60 in unit => send every 20s = 3x safety margin.


def _notify(message: str) -> bool:
    addr = os.environ.get("NOTIFY_SOCKET")
    if not addr:
        return False
    # Abstract socket convention: a leading '@' means abstract namespace -> replace with NUL byte.
    if addr.startswith("@"):
        addr = "\0" + addr[1:]
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as s:
            s.sendto(message.encode("utf-8"), addr)
        return True
    except OSError as e:
        logger.warning("sd_notify send failed: %s", e)
        return False


def notify_ready(status: str = "Application ready") -> bool:
    ok = _notify(f"READY=1\nSTATUS={status}\n")
    if ok:
        logger.info("sd_notify READY=1 sent", extra={"status": status})
    return ok


def notify_watchdog() -> bool:
    return _notify("WATCHDOG=1\n")


def notify_stopping() -> bool:
    return _notify("STOPPING=1\nSTATUS=Shutting down\n")


def _heartbeat_loop():
    logger.info("sd_notify heartbeat thread started (interval=%ds)", _WATCHDOG_INTERVAL_SEC)
    while not _HEARTBEAT_STOP.is_set():
        try:
            notify_watchdog()
        except Exception as e:
            logger.warning("sd_notify heartbeat error: %s", e)
        _HEARTBEAT_STOP.wait(_WATCHDOG_INTERVAL_SEC)
    logger.info("sd_notify heartbeat thread stopped")


def start_heartbeat() -> None:
    global _HEARTBEAT_THREAD
    if os.environ.get("WB_WATCHDOG_ENABLED", "1") != "1":
        logger.info("sd_notify heartbeat disabled via WB_WATCHDOG_ENABLED=0")
        return
    if _HEARTBEAT_THREAD is not None and _HEARTBEAT_THREAD.is_alive():
        return
    _HEARTBEAT_STOP.clear()
    _HEARTBEAT_THREAD = threading.Thread(
        target=_heartbeat_loop,
        name="sd-notify-heartbeat",
        daemon=True,
    )
    _HEARTBEAT_THREAD.start()


def stop_heartbeat(timeout: float = 5.0) -> None:
    _HEARTBEAT_STOP.set()
    if _HEARTBEAT_THREAD is not None:
        _HEARTBEAT_THREAD.join(timeout=timeout)
