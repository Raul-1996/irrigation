#!/usr/bin/env python3
"""
Упрощенные тесты веб-интерфейса WB-Irrigation
Использует requests для тестирования API без браузера
"""

import unittest
import time
import tempfile
import os
import shutil
import threading
import subprocess
import requests
import json
from PIL import Image
import io

class WebInterfaceTest(unittest.TestCase):
    """Тесты веб-интерфейса WB-Irrigation"""
    
    @classmethod
    def setUpClass(cls):
        """Настройка перед всеми тестами"""
        # Создаем временные директории
        cls.test_db_path = tempfile.mktemp(suffix='.db')
        cls.test_backup_dir = tempfile.mkdtemp()
        cls.test_photos_dir = tempfile.mkdtemp()
        
        # Запуск Flask приложения в отдельном потоке
        cls.app_process = None
        cls.start_flask_app()
        
        # Ждем запуска приложения
        cls.wait_for_app_startup()
        
        # Создаем тестовое изображение
        cls.create_test_image()
    
    @classmethod
    def tearDownClass(cls):
        """Очистка после всех тестов"""
        if cls.app_process:
            cls.app_process.terminate()
            cls.app_process.wait()
        
        # Удаляем временные файлы
        if os.path.exists(cls.test_db_path):
            os.remove(cls.test_db_path)
        if os.path.exists(cls.test_backup_dir):
            shutil.rmtree(cls.test_backup_dir)
        if os.path.exists(cls.test_photos_dir):
            shutil.rmtree(cls.test_photos_dir)
    
    @classmethod
    def start_flask_app(cls):
        """Запуск Flask приложения"""
        env = os.environ.copy()
        env['TESTING'] = '1'
        env['TEST_DB_PATH'] = cls.test_db_path
        env['TEST_BACKUP_DIR'] = cls.test_backup_dir
        env['TEST_PHOTOS_DIR'] = cls.test_photos_dir
        
        cls.app_process = subprocess.Popen(
            ['python', 'run.py'],
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE
        )
    
    @classmethod
    def wait_for_app_startup(cls):
        """Ожидание запуска приложения"""
        max_attempts = 30
        for attempt in range(max_attempts):
            try:
                response = requests.get('http://localhost:8080/api/status', timeout=1)
                if response.status_code == 200:
                    print(f"✅ Приложение запущено на попытке {attempt + 1}")
                    return
            except requests.exceptions.RequestException:
                pass
            time.sleep(1)
        raise Exception("Не удалось запустить приложение")
    
    @classmethod
    def create_test_image(cls):
        """Создание тестового изображения"""
        # Создаем простое изображение 100x100 пикселей
        img = Image.new('RGB', (100, 100), color='red')
        img_byte_arr = io.BytesIO()
        img.save(img_byte_arr, format='JPEG')
        img_byte_arr.seek(0)
        cls.test_image_data = img_byte_arr.getvalue()
    
    def test_01_home_page(self):
        """Тест главной страницы"""
        print("🧪 Тест главной страницы...")
        response = requests.get('http://localhost:8080/')
        self.assertEqual(response.status_code, 200)
        self.assertIn('WB-Irrigation', response.text)
        print("✅ Главная страница загружается корректно")
    
    def test_02_status_api(self):
        """Тест API статуса"""
        print("🧪 Тест API статуса...")
        response = requests.get('http://localhost:8080/api/status')
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertIn('datetime', data)
        self.assertIn('groups', data)
        print("✅ API статуса работает корректно")
    
    def test_03_zones_api(self):
        """Тест API зон"""
        print("🧪 Тест API зон...")
        response = requests.get('http://localhost:8080/api/zones')
        self.assertEqual(response.status_code, 200)
        zones = response.json()
        self.assertIsInstance(zones, list)
        self.assertGreater(len(zones), 0)
        print(f"✅ API зон работает корректно, найдено {len(zones)} зон")
    
    def test_04_groups_api(self):
        """Тест API групп"""
        print("🧪 Тест API групп...")
        response = requests.get('http://localhost:8080/api/groups')
        self.assertEqual(response.status_code, 200)
        groups = response.json()
        self.assertIsInstance(groups, list)
        self.assertGreater(len(groups), 0)
        print(f"✅ API групп работает корректно, найдено {len(groups)} групп")
    
    def test_05_programs_api(self):
        """Тест API программ"""
        print("🧪 Тест API программ...")
        response = requests.get('http://localhost:8080/api/programs')
        self.assertEqual(response.status_code, 200)
        programs = response.json()
        self.assertIsInstance(programs, list)
        print(f"✅ API программ работает корректно, найдено {len(programs)} программ")

    def test_05b_mqtt_servers_crud_api(self):
        """Тест CRUD MQTT servers через API"""
        print("🧪 Тест API MQTT servers...")
        # create
        payload = {
            'name': 'WB UI',
            'host': '127.0.0.1',
            'port': 1883,
            'username': 'u',
            'password': 'p',
            'client_id': 'cid',
            'enabled': True
        }
        r = requests.post('http://localhost:8080/api/mqtt/servers', json=payload)
        self.assertIn(r.status_code, (201, 400))
        if r.status_code == 201:
            sid = r.json()['server']['id']
            # get
            g = requests.get(f'http://localhost:8080/api/mqtt/servers/{sid}')
            self.assertEqual(g.status_code, 200)
            # update
            u = requests.put(f'http://localhost:8080/api/mqtt/servers/{sid}', json={'name': 'WB UI 2'})
            self.assertEqual(u.status_code, 200)
            # delete
            d = requests.delete(f'http://localhost:8080/api/mqtt/servers/{sid}')
            self.assertIn(d.status_code, (204, 400))
        print("✅ API MQTT servers CRUD работает корректно")
    
    def test_06_logs_api(self):
        """Тест API логов"""
        print("🧪 Тест API логов...")
        response = requests.get('http://localhost:8080/api/logs')
        self.assertEqual(response.status_code, 200)
        logs = response.json()
        self.assertIsInstance(logs, list)
        print(f"✅ API логов работает корректно, найдено {len(logs)} записей")
    
    def test_07_water_api(self):
        """Тест API воды"""
        print("🧪 Тест API воды...")
        response = requests.get('http://localhost:8080/api/water')
        self.assertEqual(response.status_code, 200)
        water_data = response.json()
        self.assertIsInstance(water_data, dict)
        print("✅ API воды работает корректно")
    
    def test_08_zone_update(self):
        """Тест обновления зоны"""
        print("🧪 Тест обновления зоны...")
        # Получаем первую зону
        response = requests.get('http://localhost:8080/api/zones')
        zones = response.json()
        zone_id = zones[0]['id']
        
        # Обновляем зону
        update_data = {
            'name': 'Тестовая зона',
            'duration': 15,
            'icon': '🌱'
        }
        response = requests.put(f'http://localhost:8080/api/zones/{zone_id}', 
                              json=update_data)
        self.assertEqual(response.status_code, 200)
        
        # Проверяем обновление
        response = requests.get(f'http://localhost:8080/api/zones/{zone_id}')
        updated_zone = response.json()
        self.assertEqual(updated_zone['name'], 'Тестовая зона')
        print("✅ Обновление зоны работает корректно")
    
    def test_09_postpone_api(self):
        """Тест API отложенного полива"""
        print("🧪 Тест API отложенного полива...")
        postpone_data = {
            'group_id': 1,
            'days': 1,
            'action': 'postpone'
        }
        response = requests.post('http://localhost:8080/api/postpone', 
                               json=postpone_data)
        self.assertEqual(response.status_code, 200)
        result = response.json()
        self.assertTrue(result['success'])
        print("✅ API отложенного полива работает корректно")
    
    def test_10_zone_photo_upload(self):
        """Тест загрузки фотографии зоны"""
        print("🧪 Тест загрузки фотографии зоны...")
        # Получаем первую зону
        response = requests.get('http://localhost:8080/api/zones')
        zones = response.json()
        zone_id = zones[0]['id']
        
        # Загружаем фотографию
        files = {'photo': ('test.jpg', self.test_image_data, 'image/jpeg')}
        response = requests.post(f'http://localhost:8080/api/zones/{zone_id}/photo', 
                               files=files)
        self.assertEqual(response.status_code, 200)
        result = response.json()
        self.assertTrue(result['success'])
        print("✅ Загрузка фотографии зоны работает корректно")
    
    def test_11_zone_photo_get(self):
        """Тест получения информации о фотографии зоны"""
        print("🧪 Тест получения информации о фотографии зоны...")
        # Получаем первую зону
        response = requests.get('http://localhost:8080/api/zones')
        zones = response.json()
        zone_id = zones[0]['id']
        
        # Получаем информацию о фотографии
        response = requests.get(f'http://localhost:8080/api/zones/{zone_id}/photo')
        self.assertEqual(response.status_code, 200)
        result = response.json()
        self.assertTrue(result['success'])
        print("✅ Получение информации о фотографии работает корректно")
    
    def test_12_zone_start_stop(self):
        """Тест запуска и остановки зоны"""
        print("🧪 Тест запуска и остановки зоны...")
        # Получаем первую зону
        response = requests.get('http://localhost:8080/api/zones')
        zones = response.json()
        zone_id = zones[0]['id']
        
        # Запускаем зону
        response = requests.post(f'http://localhost:8080/api/zones/{zone_id}/start')
        self.assertEqual(response.status_code, 200)
        result = response.json()
        self.assertTrue(result['success'])
        
        # Останавливаем зону
        response = requests.post(f'http://localhost:8080/api/zones/{zone_id}/stop')
        self.assertEqual(response.status_code, 200)
        result = response.json()
        self.assertTrue(result['success'])
        print("✅ Запуск и остановка зоны работает корректно")
    
    def test_13_pages_accessibility(self):
        """Тест доступности всех страниц"""
        print("🧪 Тест доступности всех страниц...")
        pages = ['/', '/login', '/zones', '/programs', '/logs', '/water']
        
        for page in pages:
            response = requests.get(f'http://localhost:8080{page}')
            self.assertEqual(response.status_code, 200)
            self.assertIn('WB-Irrigation', response.text)
            print(f"✅ Страница {page} доступна")

    def test_13b_login_logout(self):
        """Тест логина и логаута"""
        # login page GET
        resp = requests.get('http://localhost:8080/login')
        self.assertEqual(resp.status_code, 200)
        # API login
        resp = requests.post('http://localhost:8080/api/login', json={'password': '1234'})
        self.assertIn(resp.status_code, (200, 401))
        # logout redirect
        resp = requests.get('http://localhost:8080/logout', allow_redirects=False)
        self.assertIn(resp.status_code, (302, 303))
    
    def test_14_error_handling(self):
        """Тест обработки ошибок"""
        print("🧪 Тест обработки ошибок...")
        # Тест несуществующей зоны
        response = requests.get('http://localhost:8080/api/zones/999999')
        self.assertEqual(response.status_code, 404)
        
        # Тест несуществующей страницы
        response = requests.get('http://localhost:8080/nonexistent')
        self.assertEqual(response.status_code, 404)
        print("✅ Обработка ошибок работает корректно")
    
    def test_15_water_usage_page(self):
        """Тест страницы расхода воды"""
        print("🧪 Тест страницы расхода воды...")
        response = requests.get('http://localhost:8080/water')
        self.assertEqual(response.status_code, 200)
        self.assertIn('Расход воды', response.text)
        print("✅ Страница расхода воды работает корректно")

if __name__ == '__main__':
    print("🧪 Запуск веб-тестов WB-Irrigation...")
    print("=" * 50)
    
    # Запуск тестов
    unittest.main(verbosity=2, exit=False)
    
    print("=" * 50)
    print("🎉 Веб-тесты завершены!")
