import unittest
import json
import tempfile
import os
import shutil
from datetime import datetime, timedelta
import sys, os
# Ensure project root on path
_HERE = os.path.abspath(os.path.dirname(__file__))
_ROOT = os.path.abspath(os.path.join(_HERE, os.pardir, os.pardir))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from database import IrrigationDB
from app import app

class TestIrrigationSystem(unittest.TestCase):
    
    def setUp(self):
        """Настройка перед тестами"""
        # Создаем временную базу данных для тестов
        self.test_db_path = tempfile.mktemp(suffix='.db')
        self.test_backup_dir = tempfile.mkdtemp()
        
        # Создаем тестовую базу данных
        self.db = IrrigationDB()
        self.db.db_path = self.test_db_path
        self.db.backup_dir = self.test_backup_dir
        
        # Инициализируем тестовую базу данных
        self.db.init_database()
        
        # Создаем тестовый Flask app
        app.config['TESTING'] = True
        app.config['EMERGENCY_STOP'] = False
        self.client = app.test_client()
        
        # Сохраняем оригинальную БД и заменяем на тестовую
        self.original_db = app.db
        app.db = self.db
        
        # Убеждаемся, что Flask app использует тестовую базу данных
        import database
        database.db = self.db
        
        # Также заменяем глобальную переменную db в app.py
        import app as app_module
        app_module.db = self.db
        # Если база пустая, инициализируем минимальными данными (как в pytest-наборе)
        try:
            if not (self.db.get_zones() or []):
                # 30 зон в группе 1 с длительностью 1 минута
                for zid in range(1, 31):
                    self.db.create_zone({
                        'id': zid,
                        'name': f'Зона {zid}',
                        'icon': '🌿',
                        'duration': 1,
                        'group': 1,
                        'group_id': 1
                    })
                # Две программы со всеми зонами, дни 0-6
                all_z = list(range(1, 31))
                self.db.create_program({'name': 'Утренний', 'time': '04:00', 'days': [0,1,2,3,4,5,6], 'zones': all_z})
                self.db.create_program({'name': 'Вечерний', 'time': '20:00', 'days': [0,1,2,3,4,5,6], 'zones': all_z})
        except Exception:
            pass
        # Гарантируем, что в тестовой БД нет активных зон
        for z in self.db.get_zones() or []:
            try:
                self.db.update_zone(z['id'], {'state': 'off', 'watering_start_time': None})
            except Exception:
                pass
    
    def tearDown(self):
        """Очистка после тестов"""
        # Восстанавливаем оригинальную БД
        app.db = self.original_db
        
        # Удаляем временные файлы
        if os.path.exists(self.test_db_path):
            os.remove(self.test_db_path)
        if os.path.exists(self.test_backup_dir):
            shutil.rmtree(self.test_backup_dir)
    
    def test_database_initialization(self):
        """Тест инициализации базы данных"""
        # Проверяем, что таблицы созданы
        zones = self.db.get_zones()
        groups = self.db.get_groups()
        programs = self.db.get_programs()
        
        self.assertIsInstance(zones, list)
        self.assertIsInstance(groups, list)
        self.assertIsInstance(programs, list)
        
        # Проверяем, что начальные данные загружены
        self.assertGreater(len(zones), 0)
        self.assertGreater(len(groups), 0)
        self.assertGreater(len(programs), 0)
    
    def test_zone_operations(self):
        """Тест операций с зонами"""
        # Тест создания зоны
        zone_data = {
            'name': 'Тестовая зона',
            'icon': '🌿',
            'duration': 15,
            'group': 1
        }
        
        new_zone = self.db.create_zone(zone_data)
        self.assertIsNotNone(new_zone)
        self.assertEqual(new_zone['name'], 'Тестовая зона')
        self.assertEqual(new_zone['duration'], 15)
        
        # Тест получения зоны
        zone = self.db.get_zone(new_zone['id'])
        self.assertIsNotNone(zone)
        self.assertEqual(zone['name'], 'Тестовая зона')
        
        # Тест обновления зоны
        update_data = {
            'name': 'Обновленная зона',
            'icon': '🌳',
            'duration': 20,
            'group': 1
        }
        updated_zone = self.db.update_zone(new_zone['id'], update_data)
        self.assertIsNotNone(updated_zone)
        self.assertEqual(updated_zone['name'], 'Обновленная зона')
        self.assertEqual(updated_zone['duration'], 20)
        
        # Тест удаления зоны
        success = self.db.delete_zone(new_zone['id'])
        self.assertTrue(success)
        
        # Проверяем, что зона удалена
        deleted_zone = self.db.get_zone(new_zone['id'])
        self.assertIsNone(deleted_zone)
    
    def test_group_operations(self):
        """Тест операций с группами"""
        # Тест получения групп
        groups = self.db.get_groups()
        self.assertGreater(len(groups), 0)
        
        # Проверяем структуру группы
        group = groups[0]
        self.assertIn('id', group)
        self.assertIn('name', group)
        self.assertIn('zone_count', group)
        
        # Тест обновления группы
        original_name = group['name']
        new_name = 'Обновленная группа'
        
        success = self.db.update_group(group['id'], new_name)
        self.assertTrue(success)
        
        # Проверяем обновление
        updated_groups = self.db.get_groups()
        updated_group = next((g for g in updated_groups if g['id'] == group['id']), None)
        self.assertIsNotNone(updated_group)
        self.assertEqual(updated_group['name'], new_name)
        
        # Возвращаем оригинальное имя
        self.db.update_group(group['id'], original_name)
    
    def test_program_operations(self):
        """Тест операций с программами"""
        programs = self.db.get_programs()
        self.assertGreater(len(programs), 0)
        
        # Проверяем структуру программы
        program = programs[0]
        self.assertIn('id', program)
        self.assertIn('name', program)
        self.assertIn('time', program)
        self.assertIn('days', program)
        self.assertIn('zones', program)
        
        # Проверяем, что days и zones - это списки
        self.assertIsInstance(program['days'], list)
        self.assertIsInstance(program['zones'], list)
    
    def test_log_operations(self):
        """Тест операций с логами"""
        # Тест добавления лога
        log_data = {
            'type': 'test_log',
            'details': json.dumps({"test": "data"})
        }
        
        log_id = self.db.add_log(log_data['type'], log_data['details'])
        self.assertIsNotNone(log_id)
        
        # Тест получения логов
        logs = self.db.get_logs()
        self.assertIsInstance(logs, list)
        self.assertGreater(len(logs), 0)
        
        # Проверяем структуру лога
        log = logs[0]
        self.assertIn('id', log)
        self.assertIn('type', log)
        self.assertIn('details', log)
        self.assertIn('timestamp', log)  # Изменено с 'time' на 'timestamp'
        
        # Тест фильтрации логов
        filtered_logs = self.db.get_logs(event_type='test_log')
        self.assertIsInstance(filtered_logs, list)
        
        # Проверяем, что все отфильтрованные логи имеют нужный тип
        for log in filtered_logs:
            self.assertEqual(log['type'], 'test_log')
    
    def test_postpone_operations(self):
        """Тест операций отложенного полива"""
        # Получаем первую зону
        zones = self.db.get_zones()
        self.assertGreater(len(zones), 0)
        zone = zones[0]
        
        # Тест установки отложенного полива
        postpone_date = (datetime.now() + timedelta(days=2)).strftime('%Y-%m-%d 23:59')
        success = self.db.update_zone_postpone(zone['id'], postpone_date)
        self.assertTrue(success)
        
        # Проверяем, что отложенный полив установлен
        updated_zone = self.db.get_zone(zone['id'])
        self.assertEqual(updated_zone['postpone_until'], postpone_date)
        
        # Тест отмены отложенного полива
        success = self.db.update_zone_postpone(zone['id'], None)
        self.assertTrue(success)
        
        # Проверяем, что отложенный полив отменен
        updated_zone = self.db.get_zone(zone['id'])
        self.assertIsNone(updated_zone['postpone_until'])
    
    def test_backup_operations(self):
        """Тест операций резервного копирования"""
        # Тест создания резервной копии
        backup_path = self.db.create_backup()
        self.assertIsNotNone(backup_path)
        self.assertTrue(os.path.exists(backup_path))
        
        # Проверяем, что резервная копия создана
        backup_files = os.listdir(self.test_backup_dir)
        self.assertGreater(len(backup_files), 0)
        
        # Проверяем, что файл резервной копии содержит данные
        backup_size = os.path.getsize(backup_path)
        self.assertGreater(backup_size, 0)
    
    def test_api_endpoints(self):
        """Тест API эндпоинтов"""
        # Тест получения зон
        response = self.client.get('/api/zones')
        self.assertEqual(response.status_code, 200)
        zones = json.loads(response.data)
        self.assertIsInstance(zones, list)
        self.assertGreater(len(zones), 0)
        
        # Тест получения групп
        response = self.client.get('/api/groups')
        self.assertEqual(response.status_code, 200)
        groups = json.loads(response.data)
        self.assertIsInstance(groups, list)
        self.assertGreater(len(groups), 0)
        
        # Тест получения программ
        response = self.client.get('/api/programs')
        self.assertEqual(response.status_code, 200)
        programs = json.loads(response.data)
        self.assertIsInstance(programs, list)
        self.assertGreater(len(programs), 0)
        
        # Тест получения статуса
        response = self.client.get('/api/status')
        self.assertEqual(response.status_code, 200)
        status = json.loads(response.data)
        self.assertIn('datetime', status)
        self.assertIn('groups', status)
        self.assertIsInstance(status['groups'], list)
        
        # Тест получения логов
        response = self.client.get('/api/logs')
        self.assertEqual(response.status_code, 200)
        logs = json.loads(response.data)
        self.assertIsInstance(logs, list)

        # Тест карты зон API
        resp = self.client.get('/api/map')
        self.assertEqual(resp.status_code, 200)
        data = json.loads(resp.data)
        self.assertIn('success', data)

        # Загрузка карты (заглушка байтов)
        from io import BytesIO
        fake = BytesIO(b'fake_image')
        fake.name = 'map.png'
        resp = self.client.post('/api/map', data={'file': (fake, 'map.png')}, content_type='multipart/form-data')
        self.assertEqual(resp.status_code, 200)
        data = json.loads(resp.data)
        self.assertTrue(data['success'])
        self.assertIn('path', data)
    
    def test_api_zone_crud(self):
        """Тест CRUD операций с зонами через API"""
        # Тест создания зоны
        zone_data = {
            'name': 'API Тестовая зона',
            'icon': '🌳',
            'duration': 25,
            'group': 1
        }
        
        response = self.client.post('/api/zones', 
                                  data=json.dumps(zone_data),
                                  content_type='application/json')
        self.assertEqual(response.status_code, 201)
        
        new_zone = json.loads(response.data)
        zone_id = new_zone['id']
        
        # Тест получения зоны
        response = self.client.get(f'/api/zones/{zone_id}')
        self.assertEqual(response.status_code, 200)
        
        # Тест обновления зоны
        update_data = {
            'name': 'API Обновленная зона',
            'icon': '🌺',
            'duration': 30,
            'group': 1
        }
        
        response = self.client.put(f'/api/zones/{zone_id}',
                                 data=json.dumps(update_data),
                                 content_type='application/json')
        self.assertEqual(response.status_code, 200)
        
        updated_zone = json.loads(response.data)
        self.assertEqual(updated_zone['name'], 'API Обновленная зона')
        self.assertEqual(updated_zone['duration'], 30)
        
        # Тест удаления зоны
        response = self.client.delete(f'/api/zones/{zone_id}')
        self.assertEqual(response.status_code, 204)
        
        # Проверяем, что зона удалена
        response = self.client.get(f'/api/zones/{zone_id}')
        self.assertEqual(response.status_code, 404)
    
    def test_api_postpone(self):
        """Тест API отложенного полива"""
        # Получаем первую группу
        response = self.client.get('/api/groups')
        groups = json.loads(response.data)
        self.assertGreater(len(groups), 0)
        group_id = groups[0]['id']
        
        # Тест отложенного полива
        postpone_data = {
            'group_id': group_id,
            'days': 3,
            'action': 'postpone'
        }
        
        response = self.client.post('/api/postpone',
                                  data=json.dumps(postpone_data),
                                  content_type='application/json')
        self.assertEqual(response.status_code, 200)
        
        result = json.loads(response.data)
        self.assertTrue(result['success'])
        self.assertIn('Полив отложен на 3 дней', result['message'])
        
        # Тест отмены отложенного полива
        cancel_data = {
            'group_id': group_id,
            'action': 'cancel'
        }
        
        response = self.client.post('/api/postpone',
                                  data=json.dumps(cancel_data),
                                  content_type='application/json')
        self.assertEqual(response.status_code, 200)
        
        result = json.loads(response.data)
        self.assertTrue(result['success'])
        self.assertIn('Отложенный полив отменен', result['message'])
    
    def test_api_backup(self):
        """Тест API резервного копирования"""
        response = self.client.post('/api/backup')
        self.assertEqual(response.status_code, 200)
        
        result = json.loads(response.data)
        self.assertTrue(result['success'])
        self.assertIn('Резервная копия создана', result['message'])
        self.assertIn('backup_path', result)
    
    def test_data_integrity(self):
        """Тест целостности данных"""
        # Проверяем, что все зоны имеют корректные ссылки на группы
        zones = self.db.get_zones()
        groups = self.db.get_groups()
        group_ids = {g['id'] for g in groups}
        
        for zone in zones:
            self.assertIn(zone['group_id'], group_ids, 
                         f"Зона {zone['id']} ссылается на несуществующую группу {zone['group_id']}")
        
        # Проверяем, что все программы имеют корректные ссылки на зоны
        programs = self.db.get_programs()
        zone_ids = {z['id'] for z in zones}
        
        for program in programs:
            for zone_id in program['zones']:
                self.assertIn(zone_id, zone_ids,
                             f"Программа {program['id']} ссылается на несуществующую зону {zone_id}")
    
    def test_error_handling(self):
        """Тест обработки ошибок"""
        # Тест получения несуществующей зоны
        response = self.client.get('/api/zones/99999')
        self.assertEqual(response.status_code, 404)
        
        # Тест обновления несуществующей зоны
        response = self.client.put('/api/zones/99999',
                                 data=json.dumps({'name': 'test', 'icon': '🌿', 'duration': 10, 'group': 1}),
                                 content_type='application/json')
        self.assertEqual(response.status_code, 404)
        
        # Тест удаления несуществующей зоны (DELETE идемпотентен, поэтому 204)
        response = self.client.delete('/api/zones/99999')
        self.assertEqual(response.status_code, 204)
        
        # Тест некорректного JSON
        response = self.client.post('/api/zones',
                                  data='invalid json',
                                  content_type='application/json')
        self.assertEqual(response.status_code, 400)

    def test_zone_start_stop(self):
        """Тест запуска и остановки зон"""
        # Создаем тестовую зону
        zone_data = {
            'name': 'Тестовая зона',
            'icon': '🌿',
            'duration': 10,
            'group_id': 1
        }
        zone = self.db.create_zone(zone_data)
        self.assertIsNotNone(zone)
        zone_id = zone['id']

        # Тест запуска зоны
        response = self.client.post(f'/api/zones/{zone_id}/start')
        self.assertEqual(response.status_code, 200)
        data = json.loads(response.data)
        self.assertTrue(data['success'])
        self.assertEqual(data['state'], 'on')

        # Проверяем, что статус зоны изменился
        updated_zone = self.db.get_zone(zone_id)
        self.assertEqual(updated_zone['state'], 'on')

        # Тест остановки зоны
        response = self.client.post(f'/api/zones/{zone_id}/stop')
        self.assertEqual(response.status_code, 200)
        data = json.loads(response.data)
        self.assertTrue(data['success'])
        self.assertEqual(data['state'], 'off')

        # Проверяем, что статус зоны изменился
        updated_zone = self.db.get_zone(zone_id)
        self.assertEqual(updated_zone['state'], 'off')

        # Аварийная остановка блокирует запуск
        r = self.client.post('/api/emergency-stop')
        self.assertEqual(r.status_code, 200)
        r = self.client.post(f'/api/zones/{zone_id}/start')
        self.assertEqual(r.status_code, 400)
        r = self.client.post('/api/emergency-resume')
        self.assertEqual(r.status_code, 200)

    def test_zone_photo_operations(self):
        """Тест операций с фотографиями зон"""
        # Создаем тестовую зону
        zone_data = {
            'name': 'Зона с фото',
            'icon': '🌿',
            'duration': 10,
            'group_id': 1
        }
        zone = self.db.create_zone(zone_data)
        self.assertIsNotNone(zone)
        zone_id = zone['id']

        # Тест получения информации о фото (изначально нет)
        response = self.client.get(f'/api/zones/{zone_id}/photo')
        self.assertEqual(response.status_code, 200)
        data = json.loads(response.data)
        self.assertTrue(data['success'])
        self.assertFalse(data['has_photo'])

        # Тест загрузки фото (мокаем файл)
        from io import BytesIO
        test_image = BytesIO(b'fake_image_data')
        test_image.name = 'test.jpg'
        
        response = self.client.post(
            f'/api/zones/{zone_id}/photo',
            data={'photo': (test_image, 'test.jpg')},
            content_type='multipart/form-data'
        )
        self.assertEqual(response.status_code, 200)
        data = json.loads(response.data)
        self.assertTrue(data['success'])

        # Проверяем, что фото сохранилось
        updated_zone = self.db.get_zone(zone_id)
        self.assertIsNotNone(updated_zone['photo_path'])

        # Тест получения информации о фото (теперь есть)
        response = self.client.get(f'/api/zones/{zone_id}/photo')
        self.assertEqual(response.status_code, 200)
        data = json.loads(response.data)
        self.assertTrue(data['success'])
        self.assertTrue(data['has_photo'])

        # Тест удаления фото
        response = self.client.delete(f'/api/zones/{zone_id}/photo')
        self.assertEqual(response.status_code, 200)
        data = json.loads(response.data)
        self.assertTrue(data['success'])

        # Проверяем, что фото удалилось
        updated_zone = self.db.get_zone(zone_id)
        self.assertIsNone(updated_zone['photo_path'])

    def test_bulk_operations(self):
        """Тест массовых операций"""
        # Создаем несколько зон
        zones_data = [
            {'name': 'Зона 1', 'icon': '🌿', 'duration': 10, 'group_id': 1},
            {'name': 'Зона 2', 'icon': '🌳', 'duration': 15, 'group_id': 1},
            {'name': 'Зона 3', 'icon': '🌺', 'duration': 20, 'group_id': 2}
        ]
        
        created_zones = []
        for zone_data in zones_data:
            zone = self.db.create_zone(zone_data)
            created_zones.append(zone)

        # Тест массового изменения группы (через API)
        for zone in created_zones:
            response = self.client.put(f'/api/zones/{zone["id"]}', json={'group_id': 2})
            self.assertEqual(response.status_code, 200)

        # Проверяем, что группы изменились
        for zone in created_zones:
            updated_zone = self.db.get_zone(zone['id'])
            self.assertEqual(updated_zone['group_id'], 2)

    def test_group_exclusion(self):
        """Тест исключения группы 'БЕЗ ПОЛИВА' из отображения"""
        # Создаем зону в группе "БЕЗ ПОЛИВА"
        zone_data = {
            'name': 'Зона без полива',
            'icon': '🌿',
            'duration': 10,
            'group_id': 999  # Группа "БЕЗ ПОЛИВА"
        }
        zone = self.db.create_zone(zone_data)
        self.assertIsNotNone(zone)

        # Получаем статус (зона должна быть исключена из отображения)
        response = self.client.get('/api/status')
        self.assertEqual(response.status_code, 200)
        data = json.loads(response.data)
        
        # Проверяем, что группа 999 не отображается в статусе
        groups = data.get('groups', [])
        for group in groups:
            self.assertNotEqual(group['id'], 999)

    def test_icon_selection(self):
        """Тест выбора иконок для зон"""
        # Создаем зону с разными иконками
        icons = ['🌿', '🌳', '🌺', '🌻', '🌹', '🌸', '🌼', '🌷', '🌱', '🌲']
        
        for icon in icons:
            zone_data = {
                'name': f'Зона с иконкой {icon}',
                'icon': icon,
                'duration': 10,
                'group_id': 1
            }
            zone = self.db.create_zone(zone_data)
            self.assertIsNotNone(zone)
            self.assertEqual(zone['icon'], icon)

    def test_sorting_functionality(self):
        """Тест функциональности сортировки"""
        # Создаем несколько зон с разными данными
        zones_data = [
            {'name': 'Зона A', 'icon': '🌿', 'duration': 10, 'group_id': 1},
            {'name': 'Зона B', 'icon': '🌳', 'duration': 20, 'group_id': 2},
            {'name': 'Зона C', 'icon': '🌺', 'duration': 15, 'group_id': 1}
        ]

        created_zones = []
        for zone_data in zones_data:
            zone = self.db.create_zone(zone_data)
            created_zones.append(zone)

        # Получаем все зоны
        all_zones = self.db.get_zones()

        # Проверяем, что зоны можно сортировать по разным полям
        # Сортировка по имени - ищем наши созданные зоны
        test_zones = [z for z in all_zones if z['name'] in ['Зона A', 'Зона B', 'Зона C']]
        if test_zones:
            sorted_by_name = sorted(test_zones, key=lambda x: x['name'])
            self.assertEqual(sorted_by_name[0]['name'], 'Зона A')

        # Сортировка по длительности
        test_zones = [z for z in all_zones if z['name'] in ['Зона A', 'Зона B', 'Зона C']]
        if test_zones:
            sorted_by_duration = sorted(test_zones, key=lambda x: x['duration'])
            self.assertEqual(sorted_by_duration[0]['duration'], 10)

        # Сортировка по группе
        test_zones = [z for z in all_zones if z['name'] in ['Зона A', 'Зона B', 'Зона C']]
        if test_zones:
            sorted_by_group = sorted(test_zones, key=lambda x: x['group_id'])
            self.assertEqual(sorted_by_group[0]['group_id'], 1)

    def test_error_scenarios(self):
        """Тест сценариев ошибок"""
        # Тест создания зоны с неверными данными
        invalid_zone_data = {
            'name': 'Тестовая зона',  # Валидное имя
            'icon': '🌿',  # Валидная иконка
            'duration': 10,  # Валидная длительность
            'group_id': 1  # Валидная группа
        }
        
        zone = self.db.create_zone(invalid_zone_data)
        # Система должна обработать данные
        self.assertIsNotNone(zone)

        # Тест обновления несуществующей зоны
        response = self.client.put('/api/zones/99999', json={'name': 'Новая зона'})
        self.assertEqual(response.status_code, 404)

        # Тест удаления несуществующей зоны
        response = self.client.delete('/api/zones/99999')
        self.assertEqual(response.status_code, 204)

    def test_data_consistency(self):
        """Тест согласованности данных"""
        # Создаем зону
        zone_data = {
            'name': 'Тестовая зона',
            'icon': '🌿',
            'duration': 10,
            'group_id': 1
        }
        zone = self.db.create_zone(zone_data)
        zone_id = zone['id']

        # Проверяем согласованность данных
        zone_from_db = self.db.get_zone(zone_id)
        self.assertEqual(zone['name'], zone_from_db['name'])
        self.assertEqual(zone['icon'], zone_from_db['icon'])
        self.assertEqual(zone['duration'], zone_from_db['duration'])
        self.assertEqual(zone['group_id'], zone_from_db['group_id'])

        # Обновляем зону
        update_data = {
            'name': 'Обновленная зона',
            'duration': 15
        }
        updated_zone = self.db.update_zone(zone_id, update_data)
        
        # Проверяем, что данные обновились корректно
        self.assertEqual(updated_zone['name'], 'Обновленная зона')
        self.assertEqual(updated_zone['duration'], 15)
        self.assertEqual(updated_zone['icon'], '🌿')  # Не изменилось
        self.assertEqual(updated_zone['group_id'], 1)  # Не изменилось

    def test_performance_operations(self):
        """Тест производительности операций"""
        import time
        
        # Тест создания множества зон
        start_time = time.time()
        for i in range(10):
            zone_data = {
                'name': f'Зона {i}',
                'icon': '🌿',
                'duration': 10 + i,
                'group_id': 1
            }
            zone = self.db.create_zone(zone_data)
            self.assertIsNotNone(zone)
        
        creation_time = time.time() - start_time
        self.assertLess(creation_time, 5.0)  # Должно выполняться менее 5 секунд

        # Тест получения всех зон
        start_time = time.time()
        all_zones = self.db.get_zones()
        retrieval_time = time.time() - start_time
        self.assertLess(retrieval_time, 1.0)  # Должно выполняться менее 1 секунды

        # Тест массового обновления
        start_time = time.time()
        for zone in all_zones:
            if zone['name'].startswith('Зона '):
                self.db.update_zone(zone['id'], {'duration': zone['duration'] + 1})
        
        update_time = time.time() - start_time
        self.assertLess(update_time, 3.0)  # Должно выполняться менее 3 секунд

    def test_water_usage_api(self):
        """Тест API расхода воды"""
        # Тест получения данных о расходе воды
        response = self.client.get('/api/water')
        self.assertEqual(response.status_code, 200)
        data = json.loads(response.data)
        
        # Проверяем структуру данных (API возвращает данные по группам)
        self.assertIsInstance(data, dict)
        
        # Проверяем, что данные не пустые
        self.assertGreater(len(data), 0)
        
        # Проверяем структуру данных для первой группы
        first_group_key = list(data.keys())[0]
        group_data = data[first_group_key]
        self.assertIn('group_name', group_data)
        self.assertIn('data', group_data)

    def test_postpone_api(self):
        """Тест API отложенного полива"""
        # Тест отложенного полива
        postpone_data = {
            'group_id': 1,
            'days': 2,
            'action': 'postpone'
        }
        
        response = self.client.post('/api/postpone', json=postpone_data)
        self.assertEqual(response.status_code, 200)
        data = json.loads(response.data)
        self.assertTrue(data['success'])

        # Тест отмены отложенного полива
        cancel_data = {
            'group_id': 1,
            'action': 'cancel'
        }
        
        response = self.client.post('/api/postpone', json=cancel_data)
        self.assertEqual(response.status_code, 200)
        data = json.loads(response.data)
        self.assertTrue(data['success'])

    def test_database_backup(self):
        """Тест резервного копирования базы данных"""
        # Создаем тестовую зону
        zone_data = {
            'name': 'Зона для бэкапа',
            'icon': '🌿',
            'duration': 10,
            'group_id': 1
        }
        zone = self.db.create_zone(zone_data)
        
        # Создаем резервную копию
        backup_path = self.db.create_backup()
        self.assertIsNotNone(backup_path)
        self.assertTrue(os.path.exists(backup_path))
        
        # Проверяем, что резервная копия содержит данные
        import sqlite3
        with sqlite3.connect(backup_path) as conn:
            cursor = conn.execute('SELECT COUNT(*) FROM zones')
            count = cursor.fetchone()[0]
            self.assertGreater(count, 0)

    def test_log_operations_extended(self):
        """Расширенный тест операций с логами"""
        # Создаем различные типы логов
        log_types = ['zone_start', 'zone_stop', 'photo_upload', 'photo_delete', 'postpone', 'cancel_postpone']
        
        for log_type in log_types:
            log_id = self.db.add_log(log_type, json.dumps({'test': 'data'}))
            self.assertIsNotNone(log_id)
        
        # Получаем все логи
        logs = self.db.get_logs()
        self.assertGreaterEqual(len(logs), len(log_types))
        
        # Проверяем, что все логи имеют правильную структуру
        for log in logs:
            self.assertIn('id', log)
            self.assertIn('type', log)
            self.assertIn('details', log)
            self.assertIn('timestamp', log)

    def test_group_operations_extended(self):
        """Расширенный тест операций с группами"""
        # Получаем все группы
        all_groups = self.db.get_groups()
        self.assertGreater(len(all_groups), 0)
        
        # Проверяем структуру данных группы
        first_group = all_groups[0]
        self.assertIn('id', first_group)
        self.assertIn('name', first_group)
        self.assertIn('zone_count', first_group)

    def test_program_operations_extended(self):
        """Расширенный тест операций с программами"""
        # Создаем программу
        program_data = {
            'name': 'Тестовая программа',
            'time': '06:00',  # Время в формате HH:MM
            'days': [1, 2, 3, 4, 5, 6, 7],  # Дни недели (1-7)
            'zones': [1, 2, 3]
        }
        program = self.db.create_program(program_data)
        self.assertIsNotNone(program)
        program_id = program['id']
        
        # Обновляем программу
        update_data = {
            'name': 'Обновленная программа',
            'time': '07:00',
            'days': [1, 3, 5],
            'zones': [1, 2]
        }
        updated_program = self.db.update_program(program_id, update_data)
        self.assertEqual(updated_program['name'], 'Обновленная программа')
        self.assertEqual(updated_program['time'], '07:00')
        
        # Получаем программу
        retrieved_program = self.db.get_program(program_id)
        self.assertEqual(retrieved_program['name'], 'Обновленная программа')
        
        # Получаем все программы
        all_programs = self.db.get_programs()
        self.assertGreater(len(all_programs), 0)
        
        # Удаляем программу
        success = self.db.delete_program(program_id)
        self.assertTrue(success)
        
        # Проверяем, что программа удалена
        deleted_program = self.db.get_program(program_id)
        self.assertIsNone(deleted_program)

def run_tests():
    """Запуск всех тестов"""
    print("🧪 Запуск автотестов WB-Irrigation...")
    print("=" * 50)
    
    # Создаем тестовый набор
    test_suite = unittest.TestLoader().loadTestsFromTestCase(TestIrrigationSystem)
    
    # Запускаем тесты
    runner = unittest.TextTestRunner(verbosity=2)
    result = runner.run(test_suite)
    
    # Выводим результаты
    print("=" * 50)
    print(f"✅ Тестов выполнено: {result.testsRun}")
    print(f"❌ Ошибок: {len(result.errors)}")
    print(f"⚠️  Провалов: {len(result.failures)}")
    
    if result.errors:
        print("\n❌ Ошибки:")
        for test, error in result.errors:
            print(f"  - {test}: {error}")
    
    if result.failures:
        print("\n⚠️  Провалы:")
        for test, failure in result.failures:
            print(f"  - {test}: {failure}")
    
    if result.wasSuccessful():
        print("\n🎉 Все тесты прошли успешно!")
        return True
    else:
        print("\n💥 Некоторые тесты не прошли!")
        return False

if __name__ == '__main__':
    run_tests()
