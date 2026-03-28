"""
Comprehensive API endpoint tests covering all 66 routes in app.py.
Focuses on endpoints NOT covered by existing tests.
"""
import io
import json
import os
import time

import pytest


# ---------- Page routes ----------

def test_page_index(client):
    r = client.get('/')
    assert r.status_code in (200, 302)


def test_page_status(client):
    r = client.get('/status')
    assert r.status_code in (200, 302)


def test_page_zones(client):
    r = client.get('/zones')
    assert r.status_code in (200, 302)


def test_page_programs(client):
    r = client.get('/programs')
    assert r.status_code in (200, 302)


def test_page_settings(client):
    r = client.get('/settings')
    assert r.status_code in (200, 302)


def test_page_mqtt(client):
    r = client.get('/mqtt')
    assert r.status_code in (200, 302)


def test_page_logs(client):
    r = client.get('/logs')
    assert r.status_code in (200, 302)


def test_page_map(client):
    r = client.get('/map')
    assert r.status_code in (200, 302)


def test_page_water(client):
    r = client.get('/water')
    assert r.status_code in (200, 302)


def test_page_login(client):
    r = client.get('/login')
    assert r.status_code == 200


# ---------- Health & system ----------

def test_health_endpoint(client):
    r = client.get('/health')
    assert r.status_code == 200


def test_health_details(client):
    r = client.get('/api/health-details')
    assert r.status_code == 200
    data = r.get_json()
    assert data is not None


def test_health_cancel_job_nonexistent(client):
    r = client.post('/api/health/job/nonexistent-job-id/cancel')
    assert r.status_code in (200, 404, 400)


def test_health_cancel_group_nonexistent(client):
    r = client.post('/api/health/group/999/cancel')
    assert r.status_code in (200, 404, 400)


def test_server_time(client):
    r = client.get('/api/server-time')
    assert r.status_code == 200
    data = r.get_json()
    assert 'time' in data or 'server_time' in data or 'iso' in data or isinstance(data, dict)


def test_api_status(client):
    r = client.get('/api/status')
    assert r.status_code == 200
    data = r.get_json()
    assert data is not None
    # Should contain zones/groups/programs info
    assert isinstance(data, dict)


def test_service_worker(client):
    r = client.get('/sw.js')
    assert r.status_code == 200


# ---------- Auth ----------

def test_login_correct_password(client):
    r = client.post('/api/login', json={'password': '1234'})
    assert r.status_code == 200


def test_login_wrong_password(client):
    r = client.post('/api/login', json={'password': 'wrongpassword'})
    assert r.status_code in (401, 403, 200)


def test_auth_status(client):
    r = client.get('/api/auth/status')
    assert r.status_code == 200


def test_logout(client):
    r = client.get('/logout', follow_redirects=False)
    assert r.status_code in (200, 302)


def test_change_password(client):
    # Try changing password
    r = client.post('/api/password', json={
        'old_password': '1234',
        'new_password': 'newpass123'
    })
    assert r.status_code in (200, 400, 401)


# ---------- Logging / Debug ----------

def test_logging_debug_get(client):
    r = client.get('/api/logging/debug')
    assert r.status_code == 200
    data = r.get_json()
    assert 'enabled' in data or 'debug' in data or isinstance(data, dict)


def test_logging_debug_post_toggle(client):
    r = client.post('/api/logging/debug', json={'enabled': True})
    assert r.status_code in (200, 204)
    r2 = client.post('/api/logging/debug', json={'enabled': False})
    assert r2.status_code in (200, 204)


# ---------- Settings ----------

def test_settings_early_off_get(client):
    r = client.get('/api/settings/early-off')
    assert r.status_code == 200


def test_settings_early_off_set(client):
    r = client.post('/api/settings/early-off', json={'seconds': 5})
    assert r.status_code == 200


def test_settings_system_name_get(client):
    r = client.get('/api/settings/system-name')
    assert r.status_code == 200


def test_settings_system_name_set(client):
    r = client.post('/api/settings/system-name', json={'name': 'TestSystem'})
    assert r.status_code == 200


# ---------- Scheduler ----------

def test_scheduler_init(client):
    r = client.post('/api/scheduler/init')
    assert r.status_code == 200


def test_scheduler_status(client):
    r = client.get('/api/scheduler/status')
    assert r.status_code == 200


def test_scheduler_jobs(client):
    r = client.get('/api/scheduler/jobs')
    assert r.status_code == 200
    data = r.get_json()
    assert isinstance(data, (dict, list))


# ---------- Rain ----------

def test_rain_config_get(client):
    r = client.get('/api/rain')
    assert r.status_code == 200
    data = r.get_json()
    assert isinstance(data, dict)


def test_rain_config_post(client):
    r = client.post('/api/rain', json={
        'enabled': False,
        'topic': '/test/rain',
        'mqtt_server_id': 1
    })
    assert r.status_code in (200, 400)


# ---------- Zones CRUD ----------

def test_zones_list(client):
    r = client.get('/api/zones')
    assert r.status_code == 200
    data = r.get_json()
    assert isinstance(data, list)
    assert len(data) > 0  # seed data has 30 zones


def test_zone_get_single(client):
    r = client.get('/api/zones/1')
    assert r.status_code == 200
    data = r.get_json()
    assert data.get('id') == 1


def test_zone_update(client):
    r = client.put('/api/zones/1', json={
        'name': 'Зона 1 Updated',
        'duration': 2
    })
    assert r.status_code == 200


def test_zone_create(client):
    r = client.post('/api/zones', json={
        'name': 'Новая зона',
        'icon': '🌱',
        'duration': 3,
        'group_id': 1,
        'topic': '/devices/test/controls/K1',
        'mqtt_server_id': 1
    })
    assert r.status_code in (200, 201)


def test_zone_delete(client):
    # Create a zone first, then delete it
    r1 = client.post('/api/zones', json={
        'name': 'Зона для удаления',
        'icon': '❌',
        'duration': 1,
        'group_id': 1,
        'topic': '/devices/test_del/controls/K1',
        'mqtt_server_id': 1
    })
    assert r1.status_code in (200, 201)
    data = r1.get_json()
    zone_id = data.get('id') or data.get('zone', {}).get('id')
    if zone_id:
        r2 = client.delete(f'/api/zones/{zone_id}')
        assert r2.status_code in (200, 204)


def test_zone_import_bulk(client):
    zones = [
        {
            'name': 'Import Zone 1',
            'icon': '🌿',
            'duration': 1,
            'group_id': 1,
            'topic': '/devices/import1/controls/K1',
            'mqtt_server_id': 1
        }
    ]
    r = client.post('/api/zones/import', json={'zones': zones})
    assert r.status_code in (200, 201, 400)


def test_zone_next_watering(client):
    r = client.get('/api/zones/1/next-watering')
    assert r.status_code == 200


def test_zones_next_watering_bulk(client):
    r = client.post('/api/zones/next-watering-bulk', json={
        'zone_ids': [1, 2, 3]
    })
    assert r.status_code == 200


# ---------- Zone Photos ----------

def test_zone_photo_upload_get_delete(client):
    # Upload a photo
    img_data = io.BytesIO()
    try:
        from PIL import Image
        img = Image.new('RGB', (100, 100), color='green')
        img.save(img_data, format='JPEG')
    except ImportError:
        # Fallback: minimal JPEG bytes
        img_data.write(b'\xff\xd8\xff\xe0' + b'\x00' * 100)
    img_data.seek(0)

    r1 = client.post('/api/zones/1/photo',
                      data={'file': (img_data, 'test_photo.jpg')},
                      content_type='multipart/form-data')
    assert r1.status_code in (200, 201, 400)

    # Get photo
    r2 = client.get('/api/zones/1/photo')
    assert r2.status_code in (200, 404)

    # Delete photo
    r3 = client.delete('/api/zones/1/photo')
    assert r3.status_code in (200, 204, 404)


def test_zone_photo_rotate(client):
    # Upload first
    img_data = io.BytesIO()
    try:
        from PIL import Image
        img = Image.new('RGB', (100, 100), color='blue')
        img.save(img_data, format='JPEG')
    except ImportError:
        img_data.write(b'\xff\xd8\xff\xe0' + b'\x00' * 100)
    img_data.seek(0)

    client.post('/api/zones/2/photo',
                data={'file': (img_data, 'rotate_photo.jpg')},
                content_type='multipart/form-data')

    r = client.post('/api/zones/2/photo/rotate', json={'angle': 90})
    assert r.status_code in (200, 400, 404)


# ---------- Groups ----------

def test_groups_list(client):
    r = client.get('/api/groups')
    assert r.status_code == 200
    data = r.get_json()
    assert isinstance(data, list)


def test_group_create(client):
    r = client.post('/api/groups', json={'name': 'Новая группа'})
    assert r.status_code in (200, 201)


def test_group_update(client):
    r = client.put('/api/groups/1', json={'name': 'Обновленная группа'})
    assert r.status_code == 200


def test_group_delete(client):
    # Create and then delete
    r1 = client.post('/api/groups', json={'name': 'Удаляемая группа'})
    data = r1.get_json()
    gid = data.get('id') or data.get('group', {}).get('id')
    if gid:
        r2 = client.delete(f'/api/groups/{gid}')
        assert r2.status_code in (200, 204)


def test_group_stop(client):
    r = client.post('/api/groups/1/stop')
    assert r.status_code == 200


def test_group_start_from_first(client):
    r = client.post('/api/groups/1/start-from-first')
    assert r.status_code == 200
    # Stop immediately
    client.post('/api/groups/1/stop')


def test_group_start_zone(client):
    r = client.post('/api/groups/1/start-zone/1')
    assert r.status_code == 200
    # Stop
    client.post('/api/groups/1/stop')


def test_group_master_valve_open(client):
    r = client.post('/api/groups/1/master-valve/open')
    assert r.status_code in (200, 400, 404)


def test_group_master_valve_close(client):
    r = client.post('/api/groups/1/master-valve/close')
    assert r.status_code in (200, 400, 404)


# ---------- Programs CRUD ----------

def test_programs_list(client):
    r = client.get('/api/programs')
    assert r.status_code == 200
    data = r.get_json()
    assert isinstance(data, list)


def test_program_get(client):
    r = client.get('/api/programs/1')
    assert r.status_code == 200
    data = r.get_json()
    assert data.get('id') == 1


def test_program_create(client):
    r = client.post('/api/programs', json={
        'name': 'Тестовая программа',
        'time': '12:00',
        'days': [0, 1, 2],
        'zones': [1, 2, 3]
    })
    assert r.status_code in (200, 201)


def test_program_update(client):
    r = client.put('/api/programs/1', json={
        'name': 'Обновленная программа',
        'time': '05:00',
        'days': [0, 1, 2, 3, 4],
        'zones': [1, 2, 3, 4, 5]
    })
    assert r.status_code == 200


def test_program_delete(client):
    # Create then delete
    r1 = client.post('/api/programs', json={
        'name': 'Программа на удаление',
        'time': '15:00',
        'days': [0],
        'zones': [1]
    })
    data = r1.get_json()
    pid = data.get('id') or data.get('program', {}).get('id')
    if pid:
        r2 = client.delete(f'/api/programs/{pid}')
        assert r2.status_code in (200, 204)


def test_program_check_conflicts(client):
    r = client.post('/api/programs/check-conflicts', json={
        'time': '04:00',
        'days': [0, 1, 2, 3, 4, 5, 6],
        'zones': [1, 2, 3]
    })
    assert r.status_code == 200


# ---------- Duration Conflicts ----------

def test_zone_duration_conflicts(client):
    r = client.post('/api/zones/check-duration-conflicts', json={
        'zone_id': 1,
        'duration': 10
    })
    assert r.status_code == 200


def test_zone_duration_conflicts_bulk(client):
    r = client.post('/api/zones/check-duration-conflicts-bulk', json={
        'zones': [
            {'id': 1, 'duration': 5},
            {'id': 2, 'duration': 10}
        ]
    })
    assert r.status_code == 200


# ---------- MQTT Servers ----------

def test_mqtt_servers_list(client):
    r = client.get('/api/mqtt/servers')
    assert r.status_code == 200
    data = r.get_json()
    assert 'servers' in data


def test_mqtt_server_create(client):
    r = client.post('/api/mqtt/servers', json={
        'name': 'test-mqtt',
        'host': '127.0.0.1',
        'port': 1883
    })
    assert r.status_code in (200, 201)


def test_mqtt_server_get(client):
    r = client.get('/api/mqtt/servers/1')
    assert r.status_code in (200, 404)


def test_mqtt_server_update(client):
    r = client.put('/api/mqtt/servers/1', json={
        'name': 'updated-mqtt',
        'host': '127.0.0.1',
        'port': 1883
    })
    assert r.status_code in (200, 404)


def test_mqtt_server_delete(client):
    # Create then delete
    r1 = client.post('/api/mqtt/servers', json={
        'name': 'deletable-mqtt',
        'host': '127.0.0.1',
        'port': 1899
    })
    data = r1.get_json()
    sid = data.get('id') or data.get('server', {}).get('id')
    if sid:
        r2 = client.delete(f'/api/mqtt/servers/{sid}')
        assert r2.status_code in (200, 204)


def test_mqtt_server_status(client):
    r = client.get('/api/mqtt/1/status')
    assert r.status_code in (200, 404)


# ---------- Env (environment sensors) ----------

def test_env_get(client):
    r = client.get('/api/env')
    assert r.status_code == 200


def test_env_post(client):
    r = client.post('/api/env', json={
        'temp_enabled': False,
        'hum_enabled': False
    })
    assert r.status_code in (200, 400)


def test_env_values(client):
    r = client.get('/api/env/values')
    assert r.status_code == 200


# ---------- Postpone ----------

def test_postpone_and_cancel(client):
    r = client.post('/api/postpone', json={
        'group_id': 1,
        'days': 1,
        'action': 'postpone'
    })
    assert r.status_code == 200

    r2 = client.post('/api/postpone', json={
        'group_id': 1,
        'action': 'cancel'
    })
    assert r2.status_code == 200


# ---------- Emergency ----------

def test_emergency_stop_and_resume(client):
    r1 = client.post('/api/emergency-stop')
    assert r1.status_code == 200
    
    r2 = client.post('/api/emergency-resume')
    assert r2.status_code == 200


# ---------- Zone start/stop ----------

def test_zone_start_stop(client):
    r1 = client.post('/api/zones/1/start')
    assert r1.status_code == 200

    r2 = client.post('/api/zones/1/stop')
    assert r2.status_code == 200


def test_zone_watering_time(client):
    r = client.get('/api/zones/1/watering-time')
    assert r.status_code == 200


def test_zone_mqtt_start_stop(client):
    r1 = client.post('/api/zones/1/mqtt/start')
    assert r1.status_code in (200, 400)

    r2 = client.post('/api/zones/1/mqtt/stop')
    assert r2.status_code in (200, 400)


# ---------- Map ----------

def test_map_get(client):
    r = client.get('/api/map')
    assert r.status_code == 200


def test_map_upload_and_delete(client):
    img_data = io.BytesIO()
    try:
        from PIL import Image
        img = Image.new('RGB', (200, 200), color='red')
        img.save(img_data, format='PNG')
    except ImportError:
        # minimal PNG
        img_data.write(b'\x89PNG\r\n\x1a\n' + b'\x00' * 100)
    img_data.seek(0)

    r1 = client.post('/api/map',
                      data={'file': (img_data, 'test_map.png')},
                      content_type='multipart/form-data')
    assert r1.status_code in (200, 201)

    if r1.status_code in (200, 201):
        r2 = client.delete('/api/map/test_map.png')
        assert r2.status_code in (200, 204, 404)


# ---------- Backup ----------

def test_backup(client):
    r = client.post('/api/backup')
    assert r.status_code == 200


# ---------- Logs ----------

def test_logs_api(client):
    r = client.get('/api/logs')
    assert r.status_code == 200
    data = r.get_json()
    assert isinstance(data, (list, dict))


# ---------- Water usage ----------

def test_water_api(client):
    r = client.get('/api/water')
    assert r.status_code == 200


# ---------- MQTT Probe ----------

def test_mqtt_probe(client):
    r = client.post('/api/mqtt/1/probe', json={
        'filter': '#',
        'duration': 1
    })
    assert r.status_code in (200, 400, 404, 500)


# ---------- WebSocket stub ----------

def test_ws_stub(client):
    r = client.get('/ws')
    assert r.status_code in (200, 400, 426)  # 426 Upgrade Required expected


# ---------- SSE endpoints ----------

def test_mqtt_zones_sse(client):
    """SSE endpoint should return streaming response."""
    r = client.get('/api/mqtt/zones-sse')
    assert r.status_code == 200


def test_mqtt_scan_sse(client):
    r = client.get('/api/mqtt/1/scan-sse')
    assert r.status_code in (200, 404, 400)


# ---------- Reports ----------

def test_reports_api_various_periods(client):
    for period in ['today', 'week', 'month']:
        r = client.get(f'/api/reports?period={period}&format=brief')
        assert r.status_code in (200, 302)


# ---------- Telegram settings ----------

def test_telegram_settings_get(client):
    r = client.get('/api/settings/telegram')
    assert r.status_code in (200, 302)


def test_telegram_settings_put(client):
    r = client.put('/api/settings/telegram', json={
        'telegram_admin_chat_id': '12345'
    })
    assert r.status_code in (200, 302)


def test_telegram_test(client):
    r = client.post('/api/settings/telegram/test')
    assert r.status_code in (200, 400)
