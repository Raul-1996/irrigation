"""Observed-state verification after MQTT publish.

After publishing ON/OFF to a zone relay, we subscribe to the zone's MQTT topic
and wait for the relay to echo back the expected state. On failure we retry the
publish, and after exhausting retries we increment fault_count and send a
Telegram alert.
"""

import contextlib
import logging
import sqlite3
import threading
import time
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Callable

from constants import OBSERVED_STATE_MAX_RETRIES, OBSERVED_STATE_TIMEOUT_SEC
from services.locks import zone_lock

logger = logging.getLogger(__name__)

RELAY_ON_PAYLOADS = frozenset({"1", "on", "true"})
RELAY_OFF_PAYLOADS = frozenset({"0", "off", "false"})


@dataclass(slots=True)
class _PreparedVerification:
    zone_id: int
    expected: str
    generation: int
    server: dict
    topic: str
    expected_payloads: set[str]
    client: Any
    result: threading.Event


def canonical_relay_state(value: object) -> str | None:
    """Map only protocol-approved relay payloads to a physical state.

    Unknown/status payloads (``UNKNOWN``, ``offline``, numeric error codes,
    malformed bytes) carry no ON/OFF evidence and must never be interpreted as
    OFF merely because they are not an ON token.
    """
    normalized = str(value or "").strip().lower()
    if normalized in RELAY_ON_PAYLOADS:
        return "on"
    if normalized in RELAY_OFF_PAYLOADS:
        return "off"
    return None


try:
    import paho.mqtt.client as mqtt
except ImportError as e:
    logger.debug("Exception in line_18: %s", e)
    mqtt = None


class StateVerifier:
    """Verify that a zone relay acknowledged a state change."""

    def __init__(self):
        self._db = None
        self._notifier = None
        self._generation_lock = threading.Lock()
        self._generations: dict[int, int] = {}
        self._generation_registered_at: dict[int, float] = {}
        self._generation_invalidated_at: dict[int, float] = {}

    @property
    def db(self):
        if self._db is None:
            try:
                from database import db

                self._db = db
            except ImportError as e:
                logger.debug("Handled exception in db: %s", e)
        return self._db

    @property
    def notifier(self):
        if self._notifier is None:
            try:
                from services.telegram_bot import notifier

                self._notifier = notifier
            except ImportError as e:
                logger.debug("Handled exception in notifier: %s", e)
        return self._notifier

    # ------------------------------------------------------------------
    def register_command(self, zone_id: int, expected: str) -> int:
        """Return a new per-zone verifier generation for a relay command.

        A newer command invalidates every older verifier for that zone.  This
        token is deliberately public so command paths outside zone_control can
        participate in the same ordering contract instead of spawning an
        ownerless retry loop.
        """
        del expected  # The generation orders commands; DB commanded_state carries the value.
        zid = int(zone_id)
        # The same activation lock guards confirmation recheck+apply below.
        # A new generation therefore cannot slip between a stale verifier's
        # final check and its mutation of token/jobs.
        with zone_lock(zid), self._generation_lock:
            generation = self._generations.get(zid, 0) + 1
            self._generations[zid] = generation
            self._generation_registered_at[zid] = time.time()
            self._generation_invalidated_at.pop(zid, None)
        return generation

    def command_registered_at(self, zone_id: int) -> float | None:
        """Return the receipt-time fence for the latest zone command."""
        with self._generation_lock:
            registered_at = self._generation_registered_at.get(int(zone_id))
        return float(registered_at) if registered_at is not None else None

    def command_invalidated_at(self, zone_id: int) -> float | None:
        """Return when the latest real command generation was invalidated."""
        with self._generation_lock:
            invalidated_at = self._generation_invalidated_at.get(int(zone_id))
        return float(invalidated_at) if invalidated_at is not None else None

    def invalidate_verifiers(self, zone_id: int) -> int:
        """Invalidate in-flight verifiers without inventing a new command.

        Fault handling must retire a verifier that may otherwise retry or
        apply stale state.  It must not, however, advance the MQTT receipt
        fence: no relay command is sent by the invalidation itself, and a
        physical report already received for the latest real command remains
        valid evidence.
        """
        zid = int(zone_id)
        with zone_lock(zid), self._generation_lock:
            generation = self._generations.get(zid, 0) + 1
            self._generations[zid] = generation
            # Keep the real command's receipt fence intact: a report already
            # received before this invalidation remains valid physical
            # evidence.  The separate boundary lets queued consumers reject
            # reports received only after that command lost ownership.
            self._generation_invalidated_at[zid] = time.time()
        return generation

    @staticmethod
    def _canonical_state(value: object) -> str | None:
        return canonical_relay_state(value)

    def _is_current(self, zone_id: int, expected: str, generation: int) -> bool:
        """Check both in-process generation and persisted command truth."""
        zid = int(zone_id)
        with self._generation_lock:
            if self._generations.get(zid) != int(generation):
                return False
        db = self.db
        if db is None:
            return False
        try:
            current = db.get_zone(zid)
        except (sqlite3.Error, OSError, ValueError, TypeError):
            logger.exception("StateVerifier: failed to refresh zone=%s generation=%s", zid, generation)
            return False
        if not current:
            return False
        commanded = self._canonical_state(current.get("commanded_state"))
        wanted = self._canonical_state(expected)
        return commanded is None or commanded == wanted

    @staticmethod
    def _close_client(client: Any) -> None:
        with contextlib.suppress(ConnectionError, TimeoutError, OSError, RuntimeError, AttributeError):
            client.loop_stop()
        with contextlib.suppress(ConnectionError, TimeoutError, OSError, RuntimeError, AttributeError):
            client.disconnect()

    @classmethod
    def _close_prepared(cls, prepared: _PreparedVerification) -> None:
        cls._close_client(prepared.client)

    def cancel_verification(self, prepared: _PreparedVerification | None) -> None:
        """Close a prepared verifier when the associated publish failed."""
        if isinstance(prepared, _PreparedVerification):
            self._close_prepared(prepared)

    def _prepare_report_subscription(
        self,
        server: dict,
        topic: str,
        expected_payloads: set[str],
        timeout: float,
        *,
        log_target: str,
    ) -> tuple[Any, threading.Event] | None:
        """Open one fresh-report subscription and wait until SUBACK."""
        if mqtt is None:
            return None
        result = threading.Event()
        suback_ready = threading.Event()
        subscription_state: dict[str, object] = {"mid": None, "accepted": False}
        subscription_lock = threading.Lock()
        import uuid

        client_id = f"verifier_{uuid.uuid4().hex[:8]}"
        try:
            client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id=client_id)
        except (ConnectionError, TimeoutError, OSError):
            client = mqtt.Client(client_id=client_id)
        if server.get("username"):
            client.username_pw_set(server.get("username"), server.get("password") or None)
        if int(server.get("tls_enabled") or 0) == 1:
            try:
                ca = server.get("tls_ca_path") or None
                cert = server.get("tls_cert_path") or None
                key = server.get("tls_key_path") or None
                client.tls_set(ca_certs=ca, certfile=cert, keyfile=key)
                if int(server.get("tls_insecure") or 0) == 1:
                    client.tls_insecure_set(True)
            except (ImportError, OSError, ValueError, RuntimeError):
                logger.exception("StateVerifier: prepared TLS setup failed; refusing plaintext fallback")
                self._close_client(client)
                return None

        def reject_subscription(message: str) -> None:
            logger.error("StateVerifier: %s target=%s topic=%s", message, log_target, topic)
            with subscription_lock:
                subscription_state["accepted"] = False
            suback_ready.set()

        def on_connect(cl, _userdata, _flags, reason_code, _properties=None):
            try:
                connect_failed = bool(getattr(reason_code, "is_failure", False)) or (
                    isinstance(reason_code, int) and reason_code != 0
                )
            except (TypeError, ValueError, AttributeError):
                connect_failed = True
            if connect_failed:
                reject_subscription(f"connect rejected reason={reason_code!r}")
                return
            try:
                subscribe_result = cl.subscribe(topic, qos=1)
                if not isinstance(subscribe_result, (tuple, list)) or len(subscribe_result) < 2:
                    reject_subscription("subscribe returned no matchable MID")
                    return
                if int(subscribe_result[0]) != 0:
                    reject_subscription(f"subscribe rejected rc={subscribe_result[0]!r}")
                    return
                with subscription_lock:
                    subscription_state["mid"] = int(subscribe_result[1])
            except (ConnectionError, TimeoutError, OSError, RuntimeError, TypeError, ValueError, AttributeError):
                logger.exception("StateVerifier: prepared subscribe failed topic=%s", topic)
                reject_subscription("subscribe raised")

        def on_subscribe(_cl, _userdata, mid, reason_codes, _properties=None):
            with subscription_lock:
                expected_mid = subscription_state.get("mid")
            if expected_mid is None or int(mid) != int(expected_mid):
                logger.warning(
                    "StateVerifier: unrelated SUBACK ignored target=%s topic=%s expected_mid=%s actual_mid=%s",
                    log_target,
                    topic,
                    expected_mid,
                    mid,
                )
                return
            try:
                codes = list(reason_codes or [])
            except TypeError:
                codes = []
            failed = not codes
            for code in codes:
                try:
                    if bool(getattr(code, "is_failure", False)) or int(code) >= 128:
                        failed = True
                        break
                except (TypeError, ValueError, AttributeError):
                    failed = True
                    break
            with subscription_lock:
                subscription_state["accepted"] = not failed
            if failed:
                logger.error(
                    "StateVerifier: SUBACK rejected target=%s topic=%s mid=%s codes=%r",
                    log_target,
                    topic,
                    mid,
                    reason_codes,
                )
            suback_ready.set()

        def on_disconnect(_cl, _userdata, *_args):
            if not suback_ready.is_set():
                reject_subscription("disconnected before successful SUBACK")

        def on_message(_cl, _userdata, msg):
            try:
                payload = msg.payload.decode("utf-8", errors="replace").strip()
                if is_command_confirmation_message(msg) and payload in expected_payloads:
                    result.set()
            except (ValueError, TypeError, KeyError, AttributeError) as e:
                logger.debug("StateVerifier: prepared message parse failed: %s", e)

        client.on_connect = on_connect
        client.on_subscribe = on_subscribe
        client.on_disconnect = on_disconnect
        client.on_message = on_message
        try:
            client.connect(
                server.get("host") or "127.0.0.1",
                int(server.get("port") or 1883),
                keepalive=max(5, int(timeout) + 5),
            )
            client.loop_start()
            if not suback_ready.wait(timeout=max(0.001, float(timeout))):
                logger.error("StateVerifier: SUBACK timeout target=%s topic=%s", log_target, topic)
                self._close_client(client)
                return None
            with subscription_lock:
                accepted = bool(subscription_state.get("accepted"))
            if not accepted:
                self._close_client(client)
                return None
            return client, result
        except (ConnectionError, TimeoutError, OSError, RuntimeError, ValueError):
            logger.exception("StateVerifier: prepared MQTT connection failed target=%s", log_target)
            self._close_client(client)
            return None

    def prepare_verification(
        self,
        zone_id: int,
        expected: str,
        *,
        generation: int | None = None,
        timeout: float = OBSERVED_STATE_TIMEOUT_SEC,
    ) -> _PreparedVerification | None:
        """Subscribe and receive SUBACK before the actuator command is sent."""
        if mqtt is None or self.db is None:
            return None
        zid = int(zone_id)
        if generation is None:
            generation = self.register_command(zid, expected)
        if not self._is_current(zid, expected, generation):
            return None
        zone = self.db.get_zone(zid)
        if not zone:
            return None
        topic = (zone.get("topic") or "").strip()
        server_id = zone.get("mqtt_server_id")
        if not topic or not server_id:
            return None
        server = self.db.get_mqtt_server(int(server_id))
        if not server:
            return None

        from utils import normalize_topic

        norm_topic = normalize_topic(topic)
        expected_payloads = self._expected_payloads(expected)
        if not norm_topic or not expected_payloads:
            return None
        opened = self._prepare_report_subscription(
            server,
            norm_topic,
            expected_payloads,
            timeout,
            log_target=f"zone:{zid}",
        )
        if opened is None:
            return None
        client, result = opened
        return _PreparedVerification(
            zone_id=zid,
            expected=str(expected),
            generation=int(generation),
            server=server,
            topic=norm_topic,
            expected_payloads=expected_payloads,
            client=client,
            result=result,
        )

    def verify_master_command(
        self,
        server_id: int,
        topic: str,
        expected_payload: str,
        publish_command: Callable[[], bool],
        timeout: float = OBSERVED_STATE_TIMEOUT_SEC,
    ) -> bool:
        """Subscribe first, publish a master command, then await fresh echo."""
        db = self.db
        if mqtt is None or db is None:
            return False
        try:
            server = db.get_mqtt_server(int(server_id))
            if not server:
                return False
            from utils import normalize_topic

            norm_topic = normalize_topic(topic)
            expected_payloads = self._expected_payloads(expected_payload)
            if not norm_topic or not expected_payloads:
                return False
            opened = self._prepare_report_subscription(
                server,
                norm_topic,
                expected_payloads,
                timeout,
                log_target=f"master:{int(server_id)}",
            )
            if opened is None:
                return False
            client, result = opened
            try:
                for _attempt in range(OBSERVED_STATE_MAX_RETRIES):
                    if not bool(publish_command()):
                        continue
                    if result.wait(timeout=max(0.001, float(timeout))):
                        return True
                return False
            finally:
                self._close_client(client)
        except Exception:  # External DB/MQTT/callback boundary: fail closed.
            logger.exception("StateVerifier: master command verification failed server=%s topic=%s", server_id, topic)
            return False

    def verify_async(
        self,
        zone_id: int,
        expected: str,
        *,
        generation: int | None = None,
        prepared: _PreparedVerification | None = None,
    ) -> None:
        """Fire-and-forget: launch verification in a background thread."""
        from config import TESTING

        if generation is None:
            generation = prepared.generation if prepared is not None else self.register_command(zone_id, expected)
        if TESTING:
            if prepared is not None:
                self.cancel_verification(prepared)
            return  # Skip async verification in tests
        t = threading.Thread(
            target=self._safe_verify,
            args=(zone_id, expected, generation, prepared),
            daemon=True,
        )
        try:
            t.start()
        except RuntimeError:
            if prepared is not None:
                self.cancel_verification(prepared)
            raise

    def _safe_verify(
        self,
        zone_id: int,
        expected: str,
        generation: int | None = None,
        prepared: _PreparedVerification | None = None,
    ) -> None:
        try:
            if generation is None:
                generation = self.register_command(zone_id, expected)
            self.verify(zone_id, expected, generation=generation, prepared=prepared)
        except (ConnectionError, TimeoutError, OSError, ValueError, RuntimeError):  # catch-all: intentional
            logger.exception("StateVerifier._safe_verify failed zone=%s expected=%s", zone_id, expected)

    def _apply_confirmation(
        self,
        zone_id: int,
        expected: str,
        *,
        clear_fault: bool = False,
        db_instance=None,
        scheduler_getter=None,
    ) -> bool:
        """Persist fresh physical truth before any generation-bound side effect.

        A broker report can race a newer command or topology edit.  The CAS is
        therefore the commit point: run history, safety-job cleanup and events
        are touched only after that exact relay generation was accepted.
        """
        db = self.db if db_instance is None else db_instance
        if db is None:
            return False
        state = self._canonical_state(expected)
        if state is None:
            return False
        snapshot = db.get_zone(int(zone_id))
        if not snapshot:
            return False
        snapshot_state = str(snapshot.get("state") or "").lower()
        already_observed = str(snapshot.get("observed_state") or "").lower() == state
        on_run = None
        if state == "on" and (snapshot.get("command_id") or snapshot.get("watering_start_time")):
            try:
                on_run = db.get_open_zone_run(int(zone_id))
            except (sqlite3.Error, OSError, AttributeError):
                logger.exception("StateVerifier: open-run confirmation read failed zone=%s", zone_id)
                return False
            if not on_run:
                logger.error("StateVerifier: active ON confirmation has no open run zone=%s", zone_id)
                return False
        if state == "on" and already_observed and snapshot_state in {"on", "fault"}:
            if on_run and int(on_run.get("confirmed") or 0) == 1:
                return True
            if not on_run:
                return True
        if (
            state == "off"
            and already_observed
            and snapshot_state in {"off", "fault"}
            and not clear_fault
            and not snapshot.get("command_id")
            and not snapshot.get("watering_start_time")
        ):
            return True
        fields: dict[str, object] = {"observed_state": state}
        if state == "off":
            fields["planned_end_time"] = None
            # Fault is sticky until an explicit repair action, but a confirmed
            # physical OFF still completes ordinary STARTING/ON/STOPPING rows.
            if snapshot_state != "fault" or clear_fault:
                fields["state"] = "off"
        elif snapshot_state != "fault":
            fields["state"] = "on"
        from services.zones_state import update_zone_state_internal

        needs_persist = not already_observed or any(snapshot.get(key) != value for key, value in fields.items())
        if needs_persist:
            try:
                applied, _current = update_zone_state_internal(
                    int(zone_id),
                    fields,
                    snapshot=snapshot,
                    audit_reason=f"mqtt_verified_{state}",
                    db=db,
                )
                if not applied:
                    logger.warning("StateVerifier: confirmation CAS conflicted zone=%s", zone_id)
                    return False
            except (sqlite3.Error, OSError, ImportError):
                logger.exception("StateVerifier: audited confirmation persist failed zone=%s", zone_id)
                return False

        if state == "on":
            try:
                confirmed = db.mark_zone_run_confirmed(zone_id)
            except (sqlite3.Error, OSError, AttributeError):
                logger.exception("StateVerifier: mark_zone_run_confirmed failed zone=%s", zone_id)
                return False
            if confirmed is not True:
                logger.error("StateVerifier: mark_zone_run_confirmed rejected zone=%s", zone_id)
                return False
            return True

        # A normal stop is only history-successful after this fresh OFF.  The
        # command path intentionally leaves the run open while confirmation is
        # pending; close it now, while the generation lock is still held.
        try:
            from services.zone_control import _finalize_water_stats, _finish_open_zone_run_failed

            if snapshot_state == "fault" and not clear_fault:
                history_ok = _finish_open_zone_run_failed(int(zone_id), db_instance=db)
            else:
                history_ok = _finalize_water_stats(
                    int(zone_id),
                    snapshot,
                    snapshot.get("watering_start_time"),
                    log_label="observed OFF",
                    db_instance=db,
                )
        except (ImportError, AttributeError, RuntimeError, ValueError, TypeError):
            logger.exception("StateVerifier: confirmed OFF run finalization failed zone=%s", zone_id)
            return False
        if history_ok is not True:
            # Keep activation-bound safety jobs and ownership tokens armed.
            # A later fresh OFF/reconciliation pass may retry the still-open
            # history row without ever claiming a successful stop early.
            logger.critical("StateVerifier: confirmed OFF history close rejected zone=%s", zone_id)
            return False

        cleanup_ok = True
        try:
            if scheduler_getter is None:
                from irrigation_scheduler import get_scheduler

                scheduler = get_scheduler()
            else:
                scheduler = scheduler_getter()
            if scheduler is not None:
                try:
                    scheduler.cancel_zone_jobs(int(zone_id), include_cap=True)
                except TypeError:
                    scheduler.cancel_zone_jobs(int(zone_id))
                    scheduler.cancel_zone_cap(int(zone_id))
        except (ImportError, AttributeError, RuntimeError, ValueError, TypeError):
            cleanup_ok = False
            logger.exception("StateVerifier: cancel confirmed OFF safety jobs failed zone=%s", zone_id)

        if not cleanup_ok:
            return False
        latest = db.get_zone(int(zone_id))
        if not (
            latest
            and str(latest.get("state") or "").lower() in {"off", "fault"}
            and str(latest.get("commanded_state") or "").lower() == "off"
            and latest.get("command_id") == snapshot.get("command_id")
            and latest.get("watering_start_time") == snapshot.get("watering_start_time")
        ):
            logger.warning("StateVerifier: confirmed OFF cleanup owner changed zone=%s", zone_id)
            return False
        try:
            cleaned, _current = update_zone_state_internal(
                int(zone_id),
                {"watering_start_time": None, "command_id": None},
                snapshot=latest,
                audit_reason="mqtt_verified_off_cleanup",
                db=db,
            )
            if not cleaned:
                logger.warning("StateVerifier: confirmed OFF cleanup CAS conflicted zone=%s", zone_id)
                return False
        except (sqlite3.Error, OSError):
            logger.exception("StateVerifier: confirmed OFF cleanup failed zone=%s", zone_id)
            return False

        try:
            db.add_log("zone_stop", f"observed_state: zone={int(zone_id)}")
        except (sqlite3.Error, OSError, AttributeError):
            logger.debug("StateVerifier: confirmed OFF log failed zone=%s", zone_id)
        try:
            from services import events

            events.publish({"type": "zone_stop", "id": int(zone_id), "by": "observed_state"})
        except (ImportError, AttributeError):
            logger.debug("StateVerifier: confirmed OFF event failed zone=%s", zone_id)
        return True

    def apply_live_confirmation(
        self,
        zone_id: int,
        expected: str,
        *,
        received_at: float | None = None,
        db_instance=None,
        scheduler_getter=None,
    ) -> bool:
        """Apply an SSE/live report through the verifier's idempotent commit."""
        zid = int(zone_id)
        state = self._canonical_state(expected)
        if state is None:
            return False
        with zone_lock(zid):
            db = self.db if db_instance is None else db_instance
            current = db.get_zone(zid) if db is not None else None
            if not current:
                return False
            commanded = self._canonical_state(current.get("commanded_state"))
            if commanded is not None and commanded != state:
                return False
            registered_at = self.command_registered_at(zid)
            if received_at is not None and registered_at is not None and float(received_at) < registered_at:
                return False
            invalidated_at = self.command_invalidated_at(zid)
            clear_fault = bool(
                state == "off"
                and str(current.get("state") or "").lower() == "fault"
                and received_at is not None
                and invalidated_at is not None
                and float(received_at) < invalidated_at
            )
            return self._apply_confirmation(
                zid,
                state,
                clear_fault=clear_fault,
                db_instance=db,
                scheduler_getter=scheduler_getter,
            )

    def _apply_confirmation_if_current(self, zone_id: int, expected: str, generation: int) -> bool:
        """Atomically recheck generation and mutate physical truth/jobs."""
        with zone_lock(int(zone_id)):
            if not self._is_current(zone_id, expected, generation):
                return False
            return self._apply_confirmation(zone_id, expected)

    # ------------------------------------------------------------------
    def verify(
        self,
        zone_id: int,
        expected: str,
        timeout: float = OBSERVED_STATE_TIMEOUT_SEC,
        retries: int = OBSERVED_STATE_MAX_RETRIES,
        *,
        generation: int | None = None,
        prepared: _PreparedVerification | None = None,
    ) -> bool:
        """Subscribe to the zone MQTT topic, wait for observed_state == expected.

        On timeout → retry publish.
        After *retries* failures → fault_count += 1, Telegram alert.

        Returns True if state confirmed, False otherwise.
        """
        if generation is None:
            generation = prepared.generation if prepared is not None else self.register_command(zone_id, expected)

        if mqtt is None:
            logger.warning("StateVerifier: paho-mqtt not available, skipping")
            return False

        db = self.db
        if db is None:
            logger.warning("StateVerifier: database not available")
            return False

        zone = db.get_zone(zone_id)
        if not zone:
            logger.warning("StateVerifier: zone %s not found", zone_id)
            return False

        topic = (zone.get("topic") or "").strip()
        server_id = zone.get("mqtt_server_id")
        if not topic or not server_id:
            logger.debug("StateVerifier: zone %s has no topic/server, skipping", zone_id)
            return True  # nothing to verify

        server = db.get_mqtt_server(int(server_id))
        if not server:
            logger.warning("StateVerifier: mqtt server %s not found", server_id)
            return False

        from utils import normalize_topic

        norm_topic = normalize_topic(topic)
        if not norm_topic:
            logger.error("StateVerifier: invalid/command-channel topic zone=%s topic=%r", zone_id, topic)
            return False

        expected_payloads = self._expected_payloads(expected)
        if prepared is not None:
            prepared_matches = (
                prepared.zone_id == int(zone_id)
                and prepared.generation == int(generation)
                and self._canonical_state(prepared.expected) == self._canonical_state(expected)
                and prepared.topic == norm_topic
            )
            if not prepared_matches:
                self._close_prepared(prepared)
                return False

        try:
            for attempt in range(1, retries + 1):
                if not self._is_current(zone_id, expected, generation):
                    logger.info(
                        "StateVerifier: stale generation abandoned zone=%s expected=%s generation=%s",
                        zone_id,
                        expected,
                        generation,
                    )
                    return False
                confirmed = (
                    prepared.result.wait(timeout=timeout)
                    if prepared is not None
                    else self._subscribe_and_wait(server, norm_topic, expected_payloads, timeout)
                )
                if confirmed:
                    if not self._apply_confirmation_if_current(zone_id, expected, generation):
                        logger.info(
                            "StateVerifier: stale confirmation ignored zone=%s expected=%s generation=%s",
                            zone_id,
                            expected,
                            generation,
                        )
                        return False
                    logger.info("StateVerifier: zone %s confirmed '%s' on attempt %d", zone_id, expected, attempt)
                    return True

                if not self._is_current(zone_id, expected, generation):
                    logger.info(
                        "StateVerifier: newer command superseded retry zone=%s expected=%s generation=%s",
                        zone_id,
                        expected,
                        generation,
                    )
                    return False

                logger.warning(
                    "StateVerifier: zone %s timeout waiting for '%s' (attempt %d/%d)",
                    zone_id,
                    expected,
                    attempt,
                    retries,
                )

                # Retry on the publisher connection while a prepared verifier
                # keeps its one report subscription alive across all attempts.
                if attempt < retries:
                    try:
                        from services.mqtt_pub import publish_mqtt_value

                        value = "1" if expected.lower() in ("on", "1") else "0"
                        publish_mqtt_value(server, norm_topic, value, min_interval_sec=0.0, qos=2, retain=True)
                    except ImportError:
                        logger.exception("StateVerifier: retry publish failed zone=%s", zone_id)

            # All retries exhausted → record fault
            if self._is_current(zone_id, expected, generation):
                self._record_fault(zone_id, zone, expected, generation=generation)
            return False
        finally:
            if prepared is not None:
                self._close_prepared(prepared)

    # ------------------------------------------------------------------
    @staticmethod
    def _expected_payloads(expected: str) -> set[str]:
        """Return set of MQTT payloads that satisfy the expected state."""
        state = canonical_relay_state(expected)
        if state == "on":
            return {"1", "on", "ON", "true", "True", "TRUE"}
        if state == "off":
            return {"0", "off", "OFF", "false", "False", "FALSE"}
        return set()

    def _subscribe_and_wait(self, server: dict, topic: str, expected_payloads: set[str], timeout: float) -> bool:
        """Create a temporary MQTT client, subscribe, and wait for matching payload."""
        result = threading.Event()
        confirmed = [False]

        import uuid

        client_id = f"verifier_{uuid.uuid4().hex[:8]}"

        try:
            cl = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id=client_id)
        except (ConnectionError, TimeoutError, OSError) as e:
            logger.debug("Exception in _subscribe_and_wait: %s", e)
            cl = mqtt.Client(client_id=client_id)

        if server.get("username"):
            cl.username_pw_set(server.get("username"), server.get("password") or None)

        # TLS is a strict transport requirement.  If its setup fails, never
        # continue to ``connect`` because that silently downgrades a configured
        # secure verifier connection to plaintext.
        if int(server.get("tls_enabled") or 0) == 1:
            try:
                import ssl

                ca = server.get("tls_ca_path") or None
                cert = server.get("tls_cert_path") or None
                key = server.get("tls_key_path") or None
                cl.tls_set(ca_certs=ca, certfile=cert, keyfile=key)
                if int(server.get("tls_insecure") or 0) == 1:
                    cl.tls_insecure_set(True)
            except (ImportError, OSError, ValueError, RuntimeError):
                logger.exception("StateVerifier: TLS setup failed; refusing plaintext fallback")
                with contextlib.suppress(ConnectionError, TimeoutError, OSError, RuntimeError):
                    cl.disconnect()
                return False

        def on_connect(client, userdata, flags, rc, *args):
            try:
                client.subscribe(topic, qos=1)
            except (ConnectionError, TimeoutError, OSError):
                logger.exception("StateVerifier: subscribe failed")

        def on_message(client, userdata, msg):
            try:
                payload = msg.payload.decode("utf-8", errors="replace").strip()
                if is_command_confirmation_message(msg) and payload in expected_payloads:
                    confirmed[0] = True
                    result.set()
            except (ValueError, TypeError, KeyError) as e:
                logger.debug("Handled exception in on_message: %s", e)

        cl.on_connect = on_connect
        cl.on_message = on_message

        try:
            host = server.get("host") or "127.0.0.1"
            port = int(server.get("port") or 1883)
            cl.connect(host, port, keepalive=int(timeout) + 5)
            cl.loop_start()

            result.wait(timeout=timeout)

            cl.loop_stop()
            cl.disconnect()
        except (ConnectionError, TimeoutError, OSError):
            logger.exception("StateVerifier: MQTT connection failed")
            try:
                cl.loop_stop()
                cl.disconnect()
            except (ConnectionError, TimeoutError, OSError) as e:
                logger.debug("Handled exception in on_message: %s", e)

        return confirmed[0]

    def _record_fault(
        self,
        zone_id: int,
        zone: dict,
        expected: str,
        *,
        generation: int | None = None,
    ) -> bool:
        """Serialize stale-fault suppression with new command registration."""
        with zone_lock(int(zone_id)):
            return self._record_fault_locked(
                zone_id,
                zone,
                expected,
                generation=generation,
            )

    def _record_fault_locked(
        self,
        zone_id: int,
        zone: dict,
        expected: str,
        *,
        generation: int | None = None,
    ) -> bool:
        """Mark zone as FAULT, increment fault_count, send Telegram alert.

        PHYS-1 reconciliation: when the observed-state contract fails
        (OBSERVED_STATE_MAX_RETRIES exhausted) we flip the zone's `state`
        to 'fault' so the state machine stops trusting what the app thinks
        the valve is doing. Downstream consumers (watchdog, UI badge,
        emergency-stop) must treat state=='fault' as "physically unknown,
        do NOT schedule new irrigation on this zone until an operator
        clears it".

        Fields updated on the zone row:
            state        = 'fault'            # blocks further commands
            fault_count += 1                  # observability counter
            last_fault   = now (ISO)          # last failure timestamp
            observed_state = expected+'?'     # mark the observation as
                                              # failed so reconciler knows
                                              # the last command was not
                                              # confirmed physically
        """
        db = self.db
        if not db:
            return False
        if generation is not None and not self._is_current(zone_id, expected, generation):
            logger.info(
                "StateVerifier: stale fault suppressed zone=%s expected=%s generation=%s",
                zone_id,
                expected,
                generation,
            )
            return False

        current = db.get_zone(int(zone_id))
        if not current:
            return False
        zone = current

        now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        current_faults = int(zone.get("fault_count") or 0)
        fields = {
            "state": "fault",
            "fault_count": current_faults + 1,
            "last_fault": now_str,
        }
        # observed_state is nullable — only set if schema has the column
        try:
            if "observed_state" in (zone.keys() if hasattr(zone, "keys") else {}):
                fields["observed_state"] = "unconfirmed"
        except (AttributeError, TypeError):
            pass

        # Audit-critical: a fault transition flips state→'fault' which blocks
        # all further irrigation on this zone until an operator clears it.
        # Operators MUST be able to see 'who/when/why' from audit_log without
        # crawling app.log.  We pass ``self.db`` explicitly so unit tests
        # that inject a test_db via ``sv._db = test_db`` see the write on
        # the same instance they later read from.
        db_inst = self.db
        try:
            from services.zones_state import update_zone_state_internal as _uzs

            applied, _current = _uzs(
                zone_id,
                fields,
                snapshot=zone,
                audit_reason="fault_detected",
                db=db_inst,
            )
            if not applied:
                logger.warning("StateVerifier: fault CAS conflicted zone=%s", zone_id)
                return False
        except (sqlite3.Error, OSError, ImportError):
            logger.exception("StateVerifier: audited fault CAS failed zone=%s", zone_id)
            return False

        # Retire the exact timed-out command only after FAULT is durable.  The
        # separate timestamp preserves already-received relay evidence while
        # allowing the SSE queue to reject reports arriving after invalidation.
        self.invalidate_verifiers(int(zone_id))

        zone_name = zone.get("name") or f"#{zone_id}"
        alert_text = (
            f"⚠️ Зона «{zone_name}»: реле не подтвердило переключение в '{expected}'\n"
            f"Попыток: {OBSERVED_STATE_MAX_RETRIES}, fault_count: {current_faults + 1}\n"
            f"Зона переведена в state=fault и исключена из расписания до вмешательства оператора.\n"
            f"Время: {now_str}"
        )
        logger.critical("StateVerifier FAULT: %s", alert_text)

        # Publish event for Telegram
        try:
            from services import events

            events.publish(
                {
                    "type": "critical_error",
                    "code": "observed_state_fault",
                    "message": alert_text,
                }
            )
        except ImportError as e:
            logger.debug("Handled exception in line_240: %s", e)

        # Direct Telegram alert
        try:
            notifier = self.notifier
            if notifier and db:
                admin_chat = db.get_setting_value("telegram_admin_chat_id")
                if admin_chat:
                    notifier.send_text(int(admin_chat), alert_text)
        except (sqlite3.Error, OSError):
            logger.exception("StateVerifier: Telegram alert failed")
        return True


def is_command_confirmation_message(message: object) -> bool:
    """Return whether an MQTT report is fresh evidence for a command.

    Retained replay has no freshness proof and can predate the current command
    (or have been written by an older application version that still wrote the
    report channel).  Only a live relay report can confirm this command.

    Current publishers write desired state exclusively to ``<topic>/on``;
    this filter remains as a rolling-upgrade and stale-broker safeguard.
    """
    return not bool(getattr(message, "retain", False))


# Module-level singleton
state_verifier = StateVerifier()


def verify_master_command(
    server_id: int,
    topic: str,
    expected_payload: str,
    publish_command: Callable[[], bool],
    timeout: float = OBSERVED_STATE_TIMEOUT_SEC,
) -> bool:
    """Public broker-aware master verification boundary; all failures are False."""
    return state_verifier.verify_master_command(
        server_id,
        topic,
        expected_payload,
        publish_command,
        timeout,
    )
