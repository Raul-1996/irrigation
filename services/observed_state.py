"""Observed-state verification after MQTT publish.

After publishing ON/OFF to a zone relay, we subscribe to the zone's MQTT topic
and wait for the relay to echo back the expected state. On failure we retry the
publish, and after exhausting retries we increment fault_count and send a
Telegram alert.
"""
import logging
import threading
import time
from datetime import datetime
from typing import Optional

logger = logging.getLogger(__name__)

try:
    import paho.mqtt.client as mqtt
except Exception as e:
    logger.debug("Exception in line_18: %s", e)
    mqtt = None


class StateVerifier:
    """Verify that a zone relay acknowledged a state change."""

    def __init__(self):
        self._db = None
        self._notifier = None

    @property
    def db(self):
        if self._db is None:
            try:
                from database import db
                self._db = db
            except Exception as e:
                logger.debug("Handled exception in db: %s", e)
        return self._db

    @property
    def notifier(self):
        if self._notifier is None:
            try:
                from services.telegram_bot import notifier
                self._notifier = notifier
            except Exception as e:
                logger.debug("Handled exception in notifier: %s", e)
        return self._notifier

    # ------------------------------------------------------------------
    def verify_async(self, zone_id: int, expected: str) -> None:
        """Fire-and-forget: launch verification in a background thread."""
        t = threading.Thread(
            target=self._safe_verify,
            args=(zone_id, expected),
            daemon=True,
        )
        t.start()

    def _safe_verify(self, zone_id: int, expected: str) -> None:
        try:
            self.verify(zone_id, expected)
        except Exception:
            logger.exception("StateVerifier._safe_verify failed zone=%s expected=%s", zone_id, expected)

    # ------------------------------------------------------------------
    def verify(self, zone_id: int, expected: str, timeout: float = 10.0, retries: int = 3) -> bool:
        """Subscribe to the zone MQTT topic, wait for observed_state == expected.

        On timeout → retry publish.
        After *retries* failures → fault_count += 1, Telegram alert.

        Returns True if state confirmed, False otherwise.
        """
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

        topic = (zone.get('topic') or '').strip()
        server_id = zone.get('mqtt_server_id')
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
            confirmed = self._subscribe_and_wait(server, norm_topic, expected_payloads, timeout)
            if confirmed:
                logger.info("StateVerifier: zone %s confirmed '%s' on attempt %d", zone_id, expected, attempt)
                return True

            logger.warning("StateVerifier: zone %s timeout waiting for '%s' (attempt %d/%d)",
                           zone_id, expected, attempt, retries)

            # Retry publish (except on last attempt)
            if attempt < retries:
                try:
                    from services.mqtt_pub import publish_mqtt_value
                    value = '1' if expected.lower() in ('on', '1') else '0'
                    publish_mqtt_value(server, norm_topic, value, min_interval_sec=0.0, qos=2, retain=True)
                except Exception:
                    logger.exception("StateVerifier: retry publish failed zone=%s", zone_id)

        # All retries exhausted → record fault
        self._record_fault(zone_id, zone, expected)
        return False

    # ------------------------------------------------------------------
    @staticmethod
    def _expected_payloads(expected: str) -> set:
        """Return set of MQTT payloads that satisfy the expected state."""
        e = expected.lower().strip()
        if e in ('on', '1'):
            return {'1', 'on', 'ON', 'true', 'True', 'TRUE'}
        else:
            return {'0', 'off', 'OFF', 'false', 'False', 'FALSE'}

    def _subscribe_and_wait(self, server: dict, topic: str, expected_payloads: set,
                            timeout: float) -> bool:
        """Create a temporary MQTT client, subscribe, and wait for matching payload."""
        result = threading.Event()
        confirmed = [False]

        import uuid
        client_id = f"verifier_{uuid.uuid4().hex[:8]}"

        try:
            cl = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id=client_id)
        except Exception as e:
            logger.debug("Exception in _subscribe_and_wait: %s", e)
            cl = mqtt.Client(client_id=client_id)

        if server.get('username'):
            cl.username_pw_set(server.get('username'), server.get('password') or None)

        # TLS if needed
        try:
            if int(server.get('tls_enabled') or 0) == 1:
                import ssl
                ca = server.get('tls_ca_path') or None
                cert = server.get('tls_cert_path') or None
                key = server.get('tls_key_path') or None
                cl.tls_set(ca_certs=ca, certfile=cert, keyfile=key)
                if int(server.get('tls_insecure') or 0) == 1:
                    cl.tls_insecure_set(True)
        except Exception:
            logger.exception("StateVerifier: TLS setup failed")

        def on_connect(client, userdata, flags, rc, *args):
            try:
                client.subscribe(topic, qos=1)
            except Exception:
                logger.exception("StateVerifier: subscribe failed")

        def on_message(client, userdata, msg):
            try:
                payload = msg.payload.decode('utf-8', errors='replace').strip()
                if payload in expected_payloads:
                    confirmed[0] = True
                    result.set()
            except Exception as e:
                logger.debug("Handled exception in on_message: %s", e)

        cl.on_connect = on_connect
        cl.on_message = on_message

        try:
            host = server.get('host') or '127.0.0.1'
            port = int(server.get('port') or 1883)
            cl.connect(host, port, keepalive=int(timeout) + 5)
            cl.loop_start()

            result.wait(timeout=timeout)

            cl.loop_stop()
            cl.disconnect()
        except Exception:
            logger.exception("StateVerifier: MQTT connection failed")
            try:
                cl.loop_stop()
                cl.disconnect()
            except Exception as e:
                logger.debug("Handled exception in on_message: %s", e)

        return confirmed[0]

    def _record_fault(self, zone_id: int, zone: dict, expected: str) -> None:
        """Increment fault_count and send Telegram alert."""
        db = self.db
        if not db:
            return

        now_str = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        current_faults = int(zone.get('fault_count') or 0)
        try:
            db.update_zone(zone_id, {
                'fault_count': current_faults + 1,
                'last_fault': now_str,
            })
        except Exception:
            logger.exception("StateVerifier: failed to update fault_count zone=%s", zone_id)

        zone_name = zone.get('name') or f'#{zone_id}'
        alert_text = (
            f"⚠️ Зона «{zone_name}»: реле не подтвердило переключение в '{expected}'\n"
            f"Попыток: 3, fault_count: {current_faults + 1}\n"
            f"Время: {now_str}"
        )
        logger.critical("StateVerifier FAULT: %s", alert_text)

        # Publish event for Telegram
        try:
            from services import events
            events.publish({
                'type': 'critical_error',
                'code': 'observed_state_fault',
                'message': alert_text,
            })
        except Exception as e:
            logger.debug("Handled exception in line_240: %s", e)

        # Direct Telegram alert
        try:
            notifier = self.notifier
            if notifier and db:
                admin_chat = db.get_setting_value('telegram_admin_chat_id')
                if admin_chat:
                    notifier.send_text(int(admin_chat), alert_text)
        except Exception:
            logger.exception("StateVerifier: Telegram alert failed")


# Module-level singleton
state_verifier = StateVerifier()
