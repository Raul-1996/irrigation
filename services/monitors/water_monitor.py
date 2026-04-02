import logging
import sqlite3
import threading
from datetime import datetime
from typing import Optional, Dict, Deque, Tuple
from collections import deque

from database import db

try:
    import paho.mqtt.client as mqtt
except ImportError:
    mqtt = None

logger = logging.getLogger(__name__)


class WaterMonitor:
    """Подписывается на топики счётчиков воды по группам, хранит последние импульсы и рассчитывает поток."""
    def __init__(self):
        self._clients: Dict[int, mqtt.Client] = {}  # key: group_id
        self._topics: Dict[int, str] = {}
        self._server_ids: Dict[int, int] = {}
        self._pulse_liters: Dict[int, int] = {}  # 1|10|100
        self._samples: Dict[int, Deque[Tuple[float, int]]] = {}  # ts, pulses
        self._lock = threading.Lock()

    def start(self):
        try:
            if mqtt is None:
                return
            groups = db.get_groups() or []
            for g in groups:
                try:
                    gid = int(g.get('id'))
                    if gid == 999:
                        continue
                    if int(g.get('use_water_meter') or 0) != 1:
                        continue
                    topic = (g.get('water_mqtt_topic') or '').strip()
                    sid = g.get('water_mqtt_server_id')
                    if not topic or not sid:
                        continue
                    pulse = str(g.get('water_pulse_size') or '1l')
                    liters = 100 if pulse == '100l' else 10 if pulse == '10l' else 1
                    # already started?
                    if gid in self._clients:
                        # update settings
                        self._topics[gid] = topic
                        self._server_ids[gid] = int(sid)
                        self._pulse_liters[gid] = liters
                        continue
                    server = db.get_mqtt_server(int(sid))
                    if not server:
                        continue
                    cl = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2)
                    if server.get('username'):
                        cl.username_pw_set(server.get('username'), server.get('password') or None)
                    def _on_message(c, u, msg, _gid=gid):
                        try:
                            p = msg.payload.decode('utf-8', errors='ignore').strip()
                            pulses = int(''.join([ch for ch in p if (ch.isdigit() or ch=='-')]))
                            ts = datetime.now().timestamp()
                            with self._lock:
                                dq = self._samples.setdefault(_gid, deque(maxlen=256))
                                dq.append((ts, pulses))
                        except (ValueError, TypeError, KeyError):
                            logger.exception('WaterMonitor on_message failed')
                    cl.on_message = _on_message
                    cl.connect(server.get('host') or '127.0.0.1', int(server.get('port') or 1883), 10)
                    cl.subscribe(topic, qos=0)
                    cl.loop_start()
                    with self._lock:
                        self._clients[gid] = cl
                        self._topics[gid] = topic
                        self._server_ids[gid] = int(sid)
                        self._pulse_liters[gid] = liters
                        self._samples.setdefault(gid, deque(maxlen=256))
                except (ConnectionError, TimeoutError, OSError):
                    logger.exception('WaterMonitor start group failed')
        except (ConnectionError, TimeoutError, OSError):
            logger.exception('WaterMonitor start failed')

    def get_current_reading_m3(self, group_id: int) -> Optional[float]:
        try:
            g = next((gg for gg in (db.get_groups() or []) if int(gg.get('id')) == int(group_id)), None)
            if not g:
                return None
            base_m3 = float(g.get('water_base_value_m3') or 0.0)
            base_p = int(g.get('water_base_pulses') or 0)
            liters = self._pulse_liters.get(int(group_id), 1)
            with self._lock:
                dq = self._samples.get(int(group_id)) or deque()
                cur_p = dq[-1][1] if dq else base_p
            delta_p = max(0, cur_p - base_p)
            val = base_m3 + (delta_p * liters) / 1000.0
            return round(val, 3)
        except (sqlite3.Error, OSError) as e:
            logger.debug("Exception in get_current_reading_m3: %s", e)
            return None

    def get_flow_lpm(self, group_id: int, since_iso: Optional[str]) -> Optional[float]:
        try:
            if not since_iso:
                return None
            try:
                since_ts = datetime.strptime(since_iso, '%Y-%m-%d %H:%M:%S').timestamp()
            except (ValueError, TypeError, KeyError) as e:
                logger.debug("Exception in get_flow_lpm: %s", e)
                return None
            liters = self._pulse_liters.get(int(group_id), 1)
            with self._lock:
                dq = list(self._samples.get(int(group_id)) or [])
            # filter samples after start
            samples = [(ts, p) for ts, p in dq if ts >= since_ts]
            if len(samples) < 2:
                return None
            # Prefer at least 5 increments if available
            p0 = samples[0][1]
            idx = 0
            target_delta = 5
            for i in range(1, len(samples)):
                if samples[i][1] - p0 >= target_delta:
                    idx = i
                    break
            if idx == 0:
                idx = len(samples) - 1
            ts0, p_start = samples[0]
            ts1, p_end = samples[idx]
            dp = max(0, p_end - p_start)
            dt_sec = max(1.0, ts1 - ts0)
            lpm = (dp * liters) / (dt_sec / 60.0)
            return round(lpm, 2)
        except (ValueError, TypeError, KeyError) as e:
            logger.debug("Exception in line_430: %s", e)
            return None

    def summarize_run(self, group_id: int, since_iso: Optional[str]) -> Tuple[Optional[float], Optional[float]]:
        """Возвращает (total_liters, avg_flow_lpm) за интервал с момента старта до последнего сэмпла."""
        try:
            if not since_iso:
                return (None, None)
            try:
                since_ts = datetime.strptime(since_iso, '%Y-%m-%d %H:%M:%S').timestamp()
            except (ValueError, TypeError, KeyError) as e:
                logger.debug("Exception in summarize_run: %s", e)
                return (None, None)
            liters_per_pulse = self._pulse_liters.get(int(group_id), 1)
            with self._lock:
                dq = list(self._samples.get(int(group_id)) or [])
            samples = [(ts, p) for ts, p in dq if ts >= since_ts]
            if len(samples) < 2:
                return (0.0, 0.0)
            ts0, p0 = samples[0]
            ts1, p1 = samples[-1]
            total_p = max(0, p1 - p0)
            total_l = total_p * liters_per_pulse
            dt_min = max(0.001, (ts1 - ts0) / 60.0)
            avg_lpm = total_l / dt_min
            return (round(total_l, 2), round(avg_lpm, 2))
        except (ValueError, TypeError, KeyError) as e:
            logger.debug("Exception in summarize_run: %s", e)
            return (None, None)

    def get_raw_pulses(self, group_id: int) -> Optional[int]:
        """Возвращает последние сырые импульсы для группы (или None, если нет сэмплов)."""
        try:
            with self._lock:
                dq = self._samples.get(int(group_id))
                if not dq or len(dq) == 0:
                    return None
                return int(dq[-1][1])
        except (ValueError, TypeError, KeyError) as e:
            logger.debug("Exception in get_raw_pulses: %s", e)
            return None

    def get_pulses_at_or_before(self, group_id: int, ts: float) -> Optional[int]:
        """Пульсы на момент ts (берём последний сэмпл с ts' <= заданного)."""
        try:
            with self._lock:
                arr = list(self._samples.get(int(group_id)) or [])
            if not arr:
                return None
            best = None
            for t, p in arr:
                if t <= ts:
                    best = p
                else:
                    break
            return int(best) if best is not None else int(arr[0][1]) if arr else None
        except (ValueError, TypeError, KeyError) as e:
            logger.debug("Exception in get_pulses_at_or_before: %s", e)
            return None

    def get_pulses_at_or_after(self, group_id: int, ts: float) -> Optional[int]:
        """Пульсы после/на момент ts (берём первый сэмпл с ts' >= заданного)."""
        try:
            with self._lock:
                arr = list(self._samples.get(int(group_id)) or [])
            if not arr:
                return None
            for t, p in arr:
                if t >= ts:
                    return int(p)
            return int(arr[-1][1]) if arr else None
        except (ValueError, TypeError, KeyError) as e:
            logger.debug("Exception in get_pulses_at_or_after: %s", e)
            return None


water_monitor = WaterMonitor()


def start_water_monitor():
    try:
        water_monitor.start()
    except (ConnectionError, TimeoutError, OSError, ValueError):
        logger.exception('start_water_monitor failed')
