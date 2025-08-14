#!/usr/bin/env python3
"""
Альтернативные реалистичные тесты веб-интерфейса WB-Irrigation
Использует requests + BeautifulSoup для эмуляции действий пользователя
"""

import unittest
import time
import tempfile
import os
import shutil
import subprocess
import requests
import json
from PIL import Image
import io
from bs4 import BeautifulSoup
import random
import re

class AlternativeRealisticWebInterfaceTest(unittest.TestCase):
    """Альтернативные реалистичные тесты веб-интерфейса WB-Irrigation"""
    
    @classmethod
    def setUpClass(cls):
        """Настройка перед всеми тестами"""
        # Создаем временные директории
        cls.test_db_path = tempfile.mktemp(suffix='.db')
        cls.test_backup_dir = tempfile.mkdtemp()
        cls.test_photos_dir = tempfile.mkdtemp()
        
        # Настройка сессии requests
        cls.session = requests.Session()
        cls.session.headers.update({
            'User-Agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36'
        })
        
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
                response = cls.session.get('http://localhost:8080/api/status', timeout=1)
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
    
    def simulate_human_delay(self, min_delay=0.5, max_delay=2.0):
        """Симуляция человеческой задержки"""
        delay = random.uniform(min_delay, max_delay)
        time.sleep(delay)
    
    def get_page_content(self, url):
        """Получение содержимого страницы"""
        try:
            response = self.session.get(f'http://localhost:8080{url}')
            response.raise_for_status()
            return response.text, response.status_code
        except requests.exceptions.RequestException as e:
            return str(e), 500
    
    def parse_html(self, html_content):
        """Парсинг HTML контента"""
        return BeautifulSoup(html_content, 'html.parser')
    
    def test_01_realistic_home_page_navigation(self):
        """Реалистичная навигация по главной странице"""
        print("🧪 Тест реалистичной навигации по главной странице...")
        
        # Получаем главную страницу
        html_content, status_code = self.get_page_content('/')
        self.assertEqual(status_code, 200, "Главная страница недоступна")
        
        # Парсим HTML
        soup = self.parse_html(html_content)
        
        # Проверяем заголовок страницы
        title = soup.find('title')
        self.assertIsNotNone(title, "Заголовок страницы не найден")
        # Проверяем, что заголовок содержит ожидаемый текст
        title_text = title.text.lower()
        self.assertTrue('статус' in title_text or 'wb-irrigation' in title_text, f"Неожиданный заголовок: {title.text}")
        print("✅ Заголовок страницы корректен")
        
        # Ищем основные элементы интерфейса
        nav_elements = soup.find_all('nav')
        self.assertGreater(len(nav_elements), 0, "Навигация не найдена")
        print("✅ Навигация найдена")
        
        # Проверяем наличие кнопок управления
        buttons = soup.find_all('button')
        self.assertGreater(len(buttons), 0, "Кнопки не найдены")
        print(f"✅ Найдено {len(buttons)} кнопок")
        
        print("✅ Навигация по главной странице работает корректно")
    
    def test_02_realistic_api_interaction(self):
        """Реалистичное взаимодействие с API"""
        print("🧪 Тест реалистичного взаимодействия с API...")
        
        # Тестируем API статуса
        response = self.session.get('http://localhost:8080/api/status')
        self.assertEqual(response.status_code, 200, "API статуса недоступен")
        status_data = response.json()
        # Проверяем, что API возвращает ожидаемые поля
        expected_fields = ['datetime', 'groups', 'humidity', 'rain_sensor', 'temperature']
        for field in expected_fields:
            self.assertIn(field, status_data, f"Поле {field} отсутствует в API статуса")
        print("✅ API статуса работает")
        
        # Тестируем API зон
        response = self.session.get('http://localhost:8080/api/zones')
        self.assertEqual(response.status_code, 200, "API зон недоступен")
        zones_data = response.json()
        self.assertIsInstance(zones_data, list)
        print(f"✅ API зон работает, получено {len(zones_data)} зон")
        
        # Тестируем API групп
        response = self.session.get('http://localhost:8080/api/groups')
        self.assertEqual(response.status_code, 200, "API групп недоступен")
        groups_data = response.json()
        self.assertIsInstance(groups_data, list)
        print(f"✅ API групп работает, получено {len(groups_data)} групп")
        
        print("✅ Взаимодействие с API работает корректно")
    
    def test_03_realistic_performance_testing(self):
        """Реалистичное тестирование производительности"""
        print("🧪 Тест реалистичного тестирования производительности...")
        
        # Тестируем время загрузки страниц
        pages = ['/', '/zones', '/programs', '/logs', '/water']
        
        for page in pages:
            start_time = time.time()
            html_content, status_code = self.get_page_content(page)
            load_time = time.time() - start_time
            
            self.assertEqual(status_code, 200, f"Страница {page} недоступна")
            self.assertLess(load_time, 5.0, f"Страница {page} загружается слишком медленно")
            print(f"✅ Страница {page} загрузилась за {load_time:.2f} секунд")
            
            self.simulate_human_delay(0.5, 1.0)
        
        print("✅ Производительность страниц приемлема")

if __name__ == '__main__':
    print("🧪 Запуск альтернативных реалистичных веб-тестов WB-Irrigation...")
    print("=" * 60)
    
    # Запуск тестов
    unittest.main(verbosity=2, exit=False)
    
    print("=" * 60)
    print("🎉 Альтернативные реалистичные веб-тесты завершены!")
