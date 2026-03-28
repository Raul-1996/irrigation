"""
One-time application initialisation extracted from before_request (TASK-016).

Call :func:`initialize_app` once at startup (e.g. inside ``create_app()`` or
at module level) instead of running heavy init on every HTTP request.
"""

import logging
import time
import threading

logger = logging.getLogger(__name__)

_INIT_DONE = False


def initialize_app(app, db):
    """Run once at boot: scheduler, watchdogs, boot-sync, monitors, MQTT warm-up.

    Safe to call multiple times — only the first invocation does real work.
    Skipped entirely when ``app.config['TESTING']`` is truthy.
    """
    global _INIT_DONE
    if _INIT_DONE:
        return
    _INIT_DONE = True

    if app.config.get('TESTING'):
        return

    # ── 1. Scheduler ────────────────────────────────────────────────
    try:
        from irrigation_scheduler import init_scheduler
        init_scheduler(db)
        logger.info('Scheduler initialised')
    except Exception as e:
        logger.error(f'Scheduler init failed: {e}')

    # ── 2. Single-zone exclusivity watchdog ─────────────────────────
    try:
        # _start_single_zone_watchdog is defined in app.py and imported below
        # at runtime to avoid circular imports. It is idempotent.
        from app import _start_single_zone_watchdog
        _start_single_zone_watchdog()
    except Exception:
        logger.exception('single-zone watchdog start failed')

    # ── 3. Cap-time watchdog (TASK-010) ─────────────────────────────
    try:
        from services.watchdog import start_watchdog as _start_cap_watchdog
        import services.zone_control as _zc_module
        _start_cap_watchdog(db, _zc_module, interval=30)
    except Exception:
        logger.exception('cap-time watchdog start failed')

    # ── 4. Boot sync: turn OFF all zones + master valves ────────────
    _boot_sync(app, db)

    # ── 5. Monitors (water, rain, env) ──────────────────────────────
    _start_monitors(app, db)

    # ── 6. MQTT publisher warm-up ───────────────────────────────────
    _warm_mqtt_clients(db)

    logger.info('Application initialisation complete')


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _boot_sync(app, db):
    """Ensure all zones and master-valves are OFF at controller start."""
    from utils import normalize_topic
    from services.mqtt_pub import publish_mqtt_value as _publish

    try:
        # Centralised OFF via zone_control (if available)
        try:
            from services.zone_control import stop_all_in_group as _stop_all
            groups = db.get_groups() or []
            for g in groups:
                try:
                    _stop_all(int(g['id']), reason='boot_sync', force=True)
                except Exception:
                    pass
        except Exception:
            pass

        # Close master-valves (mode-aware, retain)
        try:
            seen: set = set()
            for g in (db.get_groups() or []):
                try:
                    if int(g.get('use_master_valve') or 0) != 1:
                        continue
                except Exception:
                    continue
                mtopic = (g.get('master_mqtt_topic') or '').strip()
                msid = g.get('master_mqtt_server_id')
                if not mtopic or not msid:
                    continue
                key = (int(msid), mtopic)
                if key in seen:
                    continue
                seen.add(key)
                try:
                    server = db.get_mqtt_server(int(msid))
                    if server:
                        try:
                            mode = (g.get('master_mode') or 'NC').strip().upper()
                        except Exception:
                            mode = 'NC'
                        close_val = '1' if mode == 'NO' else '0'
                        logger.info(
                            f'Boot sync: closing master valve sid={msid} topic={mtopic} mode={mode} val={close_val}')
                        _publish(server, normalize_topic(mtopic), close_val, min_interval_sec=0.0, retain=True)
                except Exception:
                    logger.exception('Boot sync: master valve close failed')
        except Exception:
            logger.exception('Boot sync: master valve sweep failed')

        # Direct MQTT OFF publish per zone (secondary safety net with retries)
        try:
            zones = db.get_zones() or []
            for z in zones:
                try:
                    sid = z.get('mqtt_server_id')
                    t = (z.get('topic') or '').strip()
                    if sid and t:
                        server = db.get_mqtt_server(int(sid))
                        if server:
                            t_norm = normalize_topic(t)
                            for attempt in range(3):
                                ok = _publish(server, t_norm, '0', min_interval_sec=0.0, retain=True)
                                if ok:
                                    break
                                try:
                                    time.sleep(0.2 * (attempt + 1))
                                except Exception:
                                    pass
                            try:
                                time.sleep(0.01)
                            except Exception:
                                pass
                except Exception:
                    pass
        except Exception:
            pass

        # Close all configured master valves with retries (secondary safety net)
        try:
            seen2: set = set()
            for g in (db.get_groups() or []):
                try:
                    if int(g.get('use_master_valve') or 0) != 1:
                        continue
                except Exception:
                    continue
                mtopic = (g.get('master_mqtt_topic') or '').strip()
                msid = g.get('master_mqtt_server_id')
                if not mtopic or not msid:
                    continue
                key = (int(msid), mtopic)
                if key in seen2:
                    continue
                seen2.add(key)
                try:
                    server = db.get_mqtt_server(int(msid))
                    if server:
                        try:
                            mode = (g.get('master_mode') or 'NC').strip().upper()
                        except Exception:
                            mode = 'NC'
                        close_val = '1' if mode == 'NO' else '0'
                        t_norm = normalize_topic(mtopic)
                        for attempt in range(3):
                            ok = _publish(server, t_norm, close_val, min_interval_sec=0.0, retain=True)
                            if ok:
                                break
                            try:
                                time.sleep(0.2 * (attempt + 1))
                            except Exception:
                                pass
                        try:
                            time.sleep(0.01)
                        except Exception:
                            pass
                except Exception:
                    pass
        except Exception:
            logger.exception('boot sync OFF (secondary) failed')

        logger.info('Boot sync: all zones OFF, MQTT OFF published')
    except Exception as e:
        logger.error(f'Boot sync failed: {e}')


def _start_monitors(app, db):
    """Start water, rain, and environment monitors."""
    try:
        from services.monitors import (
            start_water_monitor,
            rain_monitor,
            env_monitor,
            probe_env_values,
        )
    except Exception:
        logger.exception('Failed to import monitors')
        return

    # Water monitor (idempotent)
    try:
        start_water_monitor()
    except Exception:
        logger.exception('WaterMonitor start failed')

    # Rain monitor
    try:
        cfg = db.get_rain_config()
        rain_monitor.start(cfg)
    except Exception:
        logger.exception('RainMonitor start failed')

    # Env monitor
    try:
        ecfg = db.get_env_config()
        env_monitor.start(ecfg)
        # Probe retained values so data appears immediately
        try:
            probe_env_values(ecfg)
        except Exception:
            logger.exception('EnvMonitor probe call failed')
    except Exception:
        logger.exception('EnvMonitor start failed')


def _warm_mqtt_clients(db):
    """Pre-connect all configured MQTT publisher clients."""
    try:
        from services.mqtt_pub import get_or_create_mqtt_client
        servers = db.get_mqtt_servers() or []
        for s in servers:
            try:
                if int(s.get('enabled') or 1) != 1:
                    continue
                get_or_create_mqtt_client(s)
            except Exception:
                pass
        logger.info(f'MQTT clients warmed: {len(servers)}')
    except Exception:
        logger.exception('MQTT warm-up failed')
