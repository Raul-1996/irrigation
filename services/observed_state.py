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
from datetime import datetime

from constants import OBSERVED_STATE_MAX_RETRIES, OBSERVED_STATE_TIMEOUT_SEC

logger = logging.getLogger(__name__)

RELAY_ON_PAYLOADS = frozenset({"1", "on", "true"})
RELAY_OFF_PAYLOADS = frozenset({"0", "off", "false"})


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
        with self._generation_lock:
            generation = self._generations.get(zid, 0) + 1
            self._generations[zid] = generation
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

    def verify_async(self, zone_id: int, expected: str) -> None:
        """Fire-and-forget: launch verification in a background thread."""
        from config import TESTING

        generation = self.register_command(zone_id, expected)
        if TESTING:
            return  # Skip async verification in tests
        t = threading.Thread(
            target=self._safe_verify,
            args=(zone_id, expected, generation),
            daemon=True,
        )
        t.start()

    def _safe_verify(self, zone_id: int, expected: str, generation: int | None = None) -> None:
        try:
            if generation is None:
                generation = self.register_command(zone_id, expected)
            self.verify(zone_id, expected, generation=generation)
        except (ConnectionError, TimeoutError, OSError, ValueError, RuntimeError):  # catch-all: intentional
            logger.exception("StateVerifier._safe_verify failed zone=%s expected=%s", zone_id, expected)

    # ------------------------------------------------------------------
    def verify(
        self,
        zone_id: int,
        expected: str,
        timeout: float = OBSERVED_STATE_TIMEOUT_SEC,
        retries: int = OBSERVED_STATE_MAX_RETRIES,
        *,
        generation: int | None = None,
    ) -> bool:
        """Subscribe to the zone MQTT topic, wait for observed_state == expected.

        On timeout → retry publish.
        After *retries* failures → fault_count += 1, Telegram alert.

        Returns True if state confirmed, False otherwise.
        """
        if generation is None:
            generation = self.register_command(zone_id, expected)

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
            return True

        expected_payloads = self._expected_payloads(expected)

        for attempt in range(1, retries + 1):
            if not self._is_current(zone_id, expected, generation):
                logger.info(
                    "StateVerifier: stale generation abandoned zone=%s expected=%s generation=%s",
                    zone_id,
                    expected,
                    generation,
                )
                return False
            confirmed = self._subscribe_and_wait(server, norm_topic, expected_payloads, timeout)
            if confirmed:
                if not self._is_current(zone_id, expected, generation):
                    logger.info(
                        "StateVerifier: stale confirmation ignored zone=%s expected=%s generation=%s",
                        zone_id,
                        expected,
                        generation,
                    )
                    return False
                logger.info("StateVerifier: zone %s confirmed '%s' on attempt %d", zone_id, expected, attempt)
                # Second, independent source of run-confirmation. The verifier
                # actively waits for a fresh relay report, so it confirms even
                # when the SSE hub's subscription is down. Retained state is
                # intentionally ineligible because the app writes it itself.
                # Only an 'on' confirmation means the zone physically watered.
                if str(expected).lower() in ("on", "1"):
                    try:
                        db.mark_zone_run_confirmed(zone_id)
                    except (sqlite3.Error, OSError, AttributeError):
                        logger.debug("StateVerifier: mark_zone_run_confirmed failed zone=%s", zone_id)
                else:
                    # Broker ACK is not physical OFF.  Keep the activation cap
                    # armed until this fresh relay report arrives; only now is
                    # it safe to disarm the activation-bound retry.
                    try:
                        from irrigation_scheduler import get_scheduler

                        scheduler = get_scheduler()
                        if scheduler is not None:
                            scheduler.cancel_zone_cap(int(zone_id))
                    except (ImportError, AttributeError, RuntimeError, ValueError, TypeError):
                        logger.exception("StateVerifier: cancel confirmed OFF cap failed zone=%s", zone_id)
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
                "StateVerifier: zone %s timeout waiting for '%s' (attempt %d/%d)", zone_id, expected, attempt, retries
            )

            # Retry publish (except on last attempt)
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
    ) -> None:
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
            return
        if generation is not None and not self._is_current(zone_id, expected, generation):
            logger.info(
                "StateVerifier: stale fault suppressed zone=%s expected=%s generation=%s",
                zone_id,
                expected,
                generation,
            )
            return

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
            from services.zones_state import update_zone_state as _uzs

            _uzs(zone_id, fields, audit_reason="fault_detected", db=db_inst)
        except (sqlite3.Error, OSError, ImportError):
            logger.exception(
                "StateVerifier: audited fault persist failed zone=%s — falling back to raw update_zone",
                zone_id,
            )
            try:
                if db_inst is not None:
                    db_inst.update_zone(zone_id, fields)
            except (sqlite3.Error, OSError):
                logger.exception("StateVerifier: failed to persist fault state zone=%s", zone_id)

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
