"""MQTT API blueprint — all /api/mqtt* endpoints (except zones-sse which is in zones_api)."""
from flask import Blueprint, request, jsonify, Response, stream_with_context
import json
import queue
import threading
import logging

from database import db
from utils import normalize_topic
from services.helpers import api_error, api_soft

try:
    import paho.mqtt.client as mqtt
except Exception as e:
    logger.debug("Exception in line_14: %s", e)
    mqtt = None

logger = logging.getLogger(__name__)

mqtt_api_bp = Blueprint('mqtt_api', __name__)


# ===== MQTT Servers CRUD =====

@mqtt_api_bp.route('/api/mqtt/servers', methods=['GET'])
def api_mqtt_servers_list():
    try:
        return jsonify({'success': True, 'servers': db.get_mqtt_servers()})
    except Exception as e:
        logger.error(f"Ошибка получения MQTT серверов: {e}")
        return jsonify({'success': False, 'message': 'Ошибка получения списка'}), 500


@mqtt_api_bp.route('/api/mqtt/servers', methods=['POST'])
def api_mqtt_server_create():
    try:
        data = request.get_json() or {}
        server = db.create_mqtt_server(data)
        if not server:
            return jsonify({'success': False, 'message': 'Не удалось создать сервер'}), 400
        return jsonify({'success': True, 'server': server}), 201
    except Exception as e:
        logger.error(f"Ошибка создания MQTT сервера: {e}")
        return jsonify({'success': False, 'message': 'Ошибка создания'}), 500


@mqtt_api_bp.route('/api/mqtt/servers/<int:server_id>', methods=['GET'])
def api_mqtt_server_get(server_id: int):
    try:
        server = db.get_mqtt_server(server_id)
        if not server:
            return jsonify({'success': False, 'message': 'Сервер не найден'}), 404
        return jsonify({'success': True, 'server': server})
    except Exception as e:
        logger.error(f"Ошибка получения MQTT сервера {server_id}: {e}")
        return jsonify({'success': False, 'message': 'Ошибка получения'}), 500


@mqtt_api_bp.route('/api/mqtt/servers/<int:server_id>', methods=['PUT'])
def api_mqtt_server_update(server_id: int):
    try:
        data = request.get_json() or {}
        ok = db.update_mqtt_server(server_id, data)
        if not ok:
            return jsonify({'success': False, 'message': 'Не удалось обновить'}), 400
        return jsonify({'success': True, 'server': db.get_mqtt_server(server_id)})
    except Exception as e:
        logger.error(f"Ошибка обновления MQTT сервера {server_id}: {e}")
        return jsonify({'success': False, 'message': 'Ошибка обновления'}), 500


@mqtt_api_bp.route('/api/mqtt/servers/<int:server_id>', methods=['DELETE'])
def api_mqtt_server_delete(server_id: int):
    try:
        ok = db.delete_mqtt_server(server_id)
        if not ok:
            return jsonify({'success': False, 'message': 'Не удалось удалить'}), 400
        return ('', 204)
    except Exception as e:
        logger.error(f"Ошибка удаления MQTT сервера {server_id}: {e}")
        return jsonify({'success': False, 'message': 'Ошибка удаления'}), 500


# ===== MQTT Probe =====

@mqtt_api_bp.route('/api/mqtt/<int:server_id>/probe', methods=['POST'])
def api_mqtt_probe(server_id: int):
    try:
        server = db.get_mqtt_server(server_id)
        if not server:
            return api_soft('MQTT_SERVER_NOT_FOUND', 'server not found', {'items': [], 'events': []})
        if mqtt is None:
            return api_soft('PAHO_NOT_INSTALLED', 'paho-mqtt not installed', {'items': [], 'events': []})
        data = request.get_json() or {}
        topic_filter = data.get('filter', '#')
        duration = float(data.get('duration', 3))

        received = []
        events = [f"probe: connecting to {server.get('host')}:{server.get('port')} filter={topic_filter} duration={duration}s"]
        client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id=(server.get('client_id') or None))
        if server.get('username'):
            client.username_pw_set(server.get('username'), server.get('password') or None)

        def on_connect(cl, userdata, flags, reason_code, properties=None):
            try:
                cl.subscribe(topic_filter, qos=0)
                events.append(f"connected rc={reason_code}, subscribed to {topic_filter}")
            except Exception as e:
                logger.debug("Exception in on_connect: %s", e)
                events.append("subscribe failed")

        def on_message(cl, userdata, msg):
            try:
                topic = msg.topic
            except Exception as e:
                logger.debug("Exception in on_message: %s", e)
                topic = getattr(msg, 'topic', '')
            if len(received) < 1000:
                try:
                    payload = msg.payload.decode('utf-8', errors='ignore')
                except Exception as e:
                    logger.debug("Exception in on_message: %s", e)
                    payload = str(msg.payload)
                received.append({'topic': topic, 'payload': payload})

        client.on_connect = on_connect
        client.on_message = on_message
        try:
            client.connect(server.get('host') or '127.0.0.1', int(server.get('port') or 1883), 5)
        except Exception as ce:
            logger.debug("Exception in on_message: %s", ce)
            events.append(f"connect error: {ce}")
            return api_soft('MQTT_CONNECT_FAILED', 'connect failed', {'items': [], 'events': events})
        client.loop_start()
        import time as _t
        start = _t.time()
        while _t.time() - start < duration and len(received) < 5000:
            _t.sleep(0.1)
        client.loop_stop()
        try:
            client.disconnect()
        except Exception as e:
            logger.debug("Handled exception in line_142: %s", e)
        if not received:
            events.append('no messages received')
        return jsonify({'success': True, 'items': received, 'events': events})
    except Exception as e:
        logger.error(f"MQTT probe error: {e}")
        return api_soft('PROBE_FAILED', 'probe failed', {'items': [], 'events': [str(e)]})


# ===== MQTT Status =====

@mqtt_api_bp.route('/api/mqtt/<int:server_id>/status', methods=['GET'])
def api_mqtt_status(server_id: int):
    try:
        server = db.get_mqtt_server(server_id)
        if not server:
            return jsonify({'success': True, 'connected': False, 'message': 'server not found'}), 200
        if mqtt is None:
            return jsonify({'success': True, 'connected': False, 'message': 'paho-mqtt not installed'}), 200
        ok = False
        client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id=(server.get('client_id') or None))
        if server.get('username'):
            client.username_pw_set(server.get('username'), server.get('password') or None)
        try:
            client.connect(server.get('host') or '127.0.0.1', int(server.get('port') or 1883), 3)
            ok = True
            try:
                client.disconnect()
            except Exception as e:
                logger.debug("Handled exception in api_mqtt_status: %s", e)
        except Exception as _e:
            logger.info(f"MQTT status connection failed for server {server_id}: {_e}")
            ok = False
        return jsonify({'success': True, 'connected': ok})
    except Exception as e:
        logger.error(f"MQTT status error: {e}")
        return jsonify({'success': True, 'connected': False, 'message': 'status failed'}), 200


# ===== MQTT Scan SSE =====

@mqtt_api_bp.route('/api/mqtt/<int:server_id>/scan-sse')
def api_mqtt_scan_sse(server_id: int):
    """Stream MQTT messages as SSE for continuous scanning."""
    try:
        server = db.get_mqtt_server(server_id)
        if not server:
            return api_error('MQTT_SERVER_NOT_FOUND', 'server not found', 404)
        if mqtt is None:
            return api_error('MQTT_LIB_MISSING', 'paho-mqtt not installed', 500)

        sub_filter = request.args.get('filter', '/devices/#') or '/devices/#'
        msg_queue: "queue.Queue[str]" = queue.Queue(maxsize=10000)
        stop_event = threading.Event()

        def _run_client():
            try:
                client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id=(server.get('client_id') or None))
                if server.get('username'):
                    client.username_pw_set(server.get('username'), server.get('password') or None)

                def on_connect(cl, userdata, flags, reason_code, properties=None):
                    try:
                        cl.subscribe(sub_filter, qos=0)
                    except Exception as e:
                        logger.debug("Handled exception in on_connect: %s", e)

                def on_message(cl, userdata, msg):
                    try:
                        topic = msg.topic
                    except Exception as e:
                        logger.debug("Exception in on_message: %s", e)
                        topic = getattr(msg, 'topic', '')
                    try:
                        payload = msg.payload.decode('utf-8', errors='ignore')
                    except Exception as e:
                        logger.debug("Exception in on_message: %s", e)
                        payload = str(msg.payload)
                    data = json.dumps({'topic': normalize_topic(topic), 'payload': payload})
                    try:
                        msg_queue.put_nowait(data)
                    except queue.Full:
                        logger.debug("scan-sse msg_queue full, dropping message for topic %s", topic)

                client.on_connect = on_connect
                client.on_message = on_message
                client.connect(server.get('host') or '127.0.0.1', int(server.get('port') or 1883), 5)
                client.loop_start()
                import time as _t
                _start_ts = _t.time()
                while not stop_event.is_set():
                    stop_event.wait(0.2)
                    if _t.time() - _start_ts > 300:
                        break
                client.loop_stop()
                try:
                    client.disconnect()
                except Exception as e:
                    logger.debug("Handled exception in line_240: %s", e)
            except Exception as e:
                logger.error(f"MQTT SSE thread error: {e}")

        th = threading.Thread(target=_run_client, daemon=True)
        th.start()

        @stream_with_context
        def _gen():
            try:
                yield 'event: open\n' + 'data: {"success": true}\n\n'
                last_ping = 0
                import time as _t
                while True:
                    try:
                        data = msg_queue.get(timeout=0.5)
                        yield f'data: {data}\n\n'
                    except queue.Empty:
                        pass  # Expected: poll timeout, no data yet
                    now = int(_t.time())
                    if now != last_ping:
                        last_ping = now
                        yield 'event: ping\n' + 'data: {}\n\n'
            finally:
                stop_event.set()
        return Response(_gen(), mimetype='text/event-stream', headers={'Cache-Control': 'no-cache', 'X-Accel-Buffering': 'no'})
    except Exception as e:
        logger.error(f"MQTT scan SSE error: {e}")
        return api_error('SSE_FAILED', 'sse init failed', 500)
