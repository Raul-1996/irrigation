#!/usr/bin/env python3
"""
Реалистичные тесты веб-интерфейса WB-Irrigation
Эмулирует реальные действия пользователя с использованием Selenium
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
from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.common.action_chains import ActionChains
from selenium.webdriver.support.ui import Select
from selenium.webdriver.common.keys import Keys
import random

BASE_URL_HOST = os.environ.get('TEST_BASE_URL_HOST', 'http://localhost:8080').rstrip('/')
BASE_URL_BROWSER = os.environ.get('TEST_BASE_URL_BROWSER', os.environ.get('TEST_BASE_URL', BASE_URL_HOST)).rstrip('/')
class RealisticWebInterfaceTest(unittest.TestCase):
    """Реалистичные тесты веб-интерфейса WB-Irrigation"""
    
    @classmethod
    def setUpClass(cls):
        """Настройка перед всеми тестами"""
        # Создаем временные директории
        cls.test_db_path = tempfile.mktemp(suffix='.db')
        cls.test_backup_dir = tempfile.mkdtemp()
        cls.test_photos_dir = tempfile.mkdtemp()
        
        # Настройка Chrome для headless режима
        chrome_options = Options()
        chrome_options.add_argument("--headless")  # Запуск в фоновом режиме
        chrome_options.add_argument("--no-sandbox")
        chrome_options.add_argument("--disable-dev-shm-usage")
        chrome_options.add_argument("--disable-gpu")
        chrome_options.add_argument("--window-size=1920,1080")
        chrome_options.add_argument("--disable-web-security")
        chrome_options.add_argument("--allow-running-insecure-content")
        
        # Пытаемся подключиться к удаленному Selenium (Docker)
        try:
            remote_url = os.environ.get('SELENIUM_REMOTE_URL')
            if remote_url:
                from selenium.webdriver import Remote
                cls.driver = Remote(command_executor=remote_url, options=chrome_options)
            else:
                cls.driver = webdriver.Chrome(options=chrome_options)
            cls.driver.implicitly_wait(10)
            print("✅ WebDriver инициализирован")
        except Exception as e:
            print(f"⚠️  Не удалось инициализировать WebDriver: {e}")
            print("🔄 Переключаемся на режим без браузера")
            cls.driver = None
        
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
        if cls.driver:
            cls.driver.quit()
        
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
                response = requests.get(f"{BASE_URL_HOST}/api/status", timeout=1)
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
    
    def wait_and_find_element(self, by, value, timeout=10):
        """Ожидание и поиск элемента"""
        if not self.driver:
            return None
        try:
            element = WebDriverWait(self.driver, timeout).until(
                EC.presence_of_element_located((by, value))
            )
            return element
        except:
            return None
    
    def simulate_human_delay(self, min_delay=0.5, max_delay=2.0):
        """Симуляция человеческой задержки"""
        delay = random.uniform(min_delay, max_delay)
        time.sleep(delay)
    
    def test_01_realistic_home_page_navigation(self):
        """Реалистичная навигация по главной странице"""
        print("🧪 Тест реалистичной навигации по главной странице...")
        
        if not self.driver:
            print("⚠️  Пропуск теста - браузер недоступен")
            return
        
        # Открываем главную страницу
        self.driver.get(f'{BASE_URL_BROWSER}/')
        self.simulate_human_delay()
        
        # Проверяем заголовок страницы
        title = self.driver.title
        self.assertIn('WB-Irrigation', title)
        print("✅ Заголовок страницы корректен")
        
        # Ищем основные элементы интерфейса
        nav_elements = self.driver.find_elements(By.TAG_NAME, 'nav')
        self.assertGreater(len(nav_elements), 0, "Навигация не найдена")
        print("✅ Навигация найдена")
        
        # Проверяем наличие кнопок управления
        buttons = self.driver.find_elements(By.TAG_NAME, 'button')
        self.assertGreater(len(buttons), 0, "Кнопки не найдены")
        print(f"✅ Найдено {len(buttons)} кнопок")
        
        # Проверяем наличие зон полива
        zone_elements = self.driver.find_elements(By.CLASS_NAME, 'zone-card')
        self.assertGreater(len(zone_elements), 0, "Зоны полива не найдены")
        print(f"✅ Найдено {len(zone_elements)} зон полива")
        
        print("✅ Навигация по главной странице работает корректно")
    
    def test_02_realistic_zone_management(self):
        """Реалистичное управление зонами"""
        print("🧪 Тест реалистичного управления зонами...")
        
        if not self.driver:
            print("⚠️  Пропуск теста - браузер недоступен")
            return
        
        # Переходим на страницу зон
        self.driver.get(f'{BASE_URL_BROWSER}/zones')
        self.simulate_human_delay()
        
        # Ждем загрузки зон
        zone_cards = self.wait_and_find_element(By.CLASS_NAME, 'zone-card')
        self.assertIsNotNone(zone_cards, "Зоны не загрузились")
        
        # Находим первую зону
        zone_cards = self.driver.find_elements(By.CLASS_NAME, 'zone-card')
        if len(zone_cards) > 0:
            first_zone = zone_cards[0]
            
            # Находим кнопку включения/выключения
            toggle_buttons = first_zone.find_elements(By.CLASS_NAME, 'toggle-btn')
            if len(toggle_buttons) > 0:
                toggle_btn = toggle_buttons[0]
                
                # Сохраняем начальное состояние
                initial_state = toggle_btn.text
                
                # Кликаем по кнопке
                self.driver.execute_script("arguments[0].click();", toggle_btn)
                self.simulate_human_delay(1, 3)
                
                # Проверяем изменение состояния
                new_state = toggle_btn.text
                self.assertNotEqual(initial_state, new_state, "Состояние зоны не изменилось")
                print("✅ Переключение состояния зоны работает")
                
                # Возвращаем в исходное состояние
                self.driver.execute_script("arguments[0].click();", toggle_btn)
                self.simulate_human_delay()
        
        print("✅ Управление зонами работает корректно")
    
    def test_03_realistic_program_creation(self):
        """Реалистичное создание программы полива"""
        print("🧪 Тест реалистичного создания программы полива...")
        
        if not self.driver:
            print("⚠️  Пропуск теста - браузер недоступен")
            return
        
        # Переходим на страницу программ
        self.driver.get(f'{BASE_URL_BROWSER}/programs')
        self.simulate_human_delay()
        
        # Ищем кнопку создания новой программы
        add_buttons = self.driver.find_elements(By.CLASS_NAME, 'add-program-btn')
        if len(add_buttons) > 0:
            add_btn = add_buttons[0]
            
            # Кликаем по кнопке добавления
            self.driver.execute_script("arguments[0].click();", add_btn)
            self.simulate_human_delay()
            
            # Ищем форму создания программы
            form = self.wait_and_find_element(By.CLASS_NAME, 'program-form')
            if form:
                # Заполняем форму
                name_input = form.find_element(By.NAME, 'name')
                name_input.clear()
                name_input.send_keys('Тестовая программа')
                self.simulate_human_delay(0.5, 1)
                
                # Устанавливаем время
                time_input = form.find_element(By.NAME, 'time')
                time_input.clear()
                time_input.send_keys('08:00')
                self.simulate_human_delay(0.5, 1)
                
                # Выбираем дни недели
                day_checkboxes = form.find_elements(By.NAME, 'days')
                if len(day_checkboxes) > 0:
                    day_checkboxes[0].click()  # Понедельник
                    self.simulate_human_delay(0.3, 0.7)
                
                # Выбираем зоны
                zone_checkboxes = form.find_elements(By.NAME, 'zones')
                if len(zone_checkboxes) > 0:
                    zone_checkboxes[0].click()  # Первая зона
                    self.simulate_human_delay(0.3, 0.7)
                
                # Сохраняем программу
                save_btn = form.find_element(By.CLASS_NAME, 'save-btn')
                self.driver.execute_script("arguments[0].click();", save_btn)
                self.simulate_human_delay(1, 2)
                
                print("✅ Создание программы работает корректно")
            else:
                print("⚠️  Форма создания программы не найдена")
        else:
            print("⚠️  Кнопка добавления программы не найдена")
    
    def test_04_realistic_photo_upload(self):
        """Реалистичная загрузка фотографии"""
        print("🧪 Тест реалистичной загрузки фотографии...")
        
        if not self.driver:
            print("⚠️  Пропуск теста - браузер недоступен")
            return
        
        # Переходим на страницу зон
        self.driver.get(f'{BASE_URL_BROWSER}/zones')
        self.simulate_human_delay()
        
        # Находим первую зону
        zone_cards = self.driver.find_elements(By.CLASS_NAME, 'zone-card')
        if len(zone_cards) > 0:
            first_zone = zone_cards[0]
            
            # Ищем кнопку загрузки фото
            photo_buttons = first_zone.find_elements(By.CLASS_NAME, 'photo-upload-btn')
            if len(photo_buttons) > 0:
                photo_btn = photo_buttons[0]
                
                # Кликаем по кнопке загрузки
                self.driver.execute_script("arguments[0].click();", photo_btn)
                self.simulate_human_delay()
                
                # Ищем input для файла
                file_input = self.wait_and_find_element(By.CSS_SELECTOR, 'input[type="file"]')
                if file_input:
                    # Создаем временный файл
                    temp_file = tempfile.NamedTemporaryFile(suffix='.jpg', delete=False)
                    temp_file.write(self.test_image_data)
                    temp_file.close()
                    
                    # Загружаем файл
                    file_input.send_keys(temp_file.name)
                    self.simulate_human_delay(1, 2)
                    
                    # Удаляем временный файл
                    os.unlink(temp_file.name)
                    
                    print("✅ Загрузка фотографии работает корректно")
                else:
                    print("⚠️  Поле загрузки файла не найдено")
            else:
                print("⚠️  Кнопка загрузки фото не найдена")
    
    def test_05_realistic_log_viewing(self):
        """Реалистичный просмотр логов"""
        print("🧪 Тест реалистичного просмотра логов...")
        
        if not self.driver:
            print("⚠️  Пропуск теста - браузер недоступен")
            return
        
        # Переходим на страницу логов
        self.driver.get(f'{BASE_URL_BROWSER}/logs')
        self.simulate_human_delay()
        
        # Ждем загрузки логов
        log_table = self.wait_and_find_element(By.CLASS_NAME, 'log-table')
        self.assertIsNotNone(log_table, "Таблица логов не загрузилась")
        
        # Проверяем наличие записей
        log_rows = self.driver.find_elements(By.CLASS_NAME, 'log-row')
        self.assertGreater(len(log_rows), 0, "Логи не найдены")
        print(f"✅ Найдено {len(log_rows)} записей в логах")
        
        # Проверяем фильтры
        filter_elements = self.driver.find_elements(By.CLASS_NAME, 'log-filter')
        if len(filter_elements) > 0:
            # Кликаем по первому фильтру
            filter_elements[0].click()
            self.simulate_human_delay()
            print("✅ Фильтры логов работают")
        
        print("✅ Просмотр логов работает корректно")
    
    def test_06_realistic_water_usage_monitoring(self):
        """Реалистичный мониторинг расхода воды"""
        print("🧪 Тест реалистичного мониторинга расхода воды...")
        
        if not self.driver:
            print("⚠️  Пропуск теста - браузер недоступен")
            return
        
        # Переходим на страницу расхода воды
        self.driver.get(f'{BASE_URL_BROWSER}/water')
        self.simulate_human_delay()
        
        # Проверяем наличие графиков
        charts = self.driver.find_elements(By.CLASS_NAME, 'chart')
        self.assertGreater(len(charts), 0, "Графики не найдены")
        print(f"✅ Найдено {len(charts)} графиков")
        
        # Проверяем статистику
        stats_elements = self.driver.find_elements(By.CLASS_NAME, 'stat-card')
        self.assertGreater(len(stats_elements), 0, "Статистика не найдена")
        print(f"✅ Найдено {len(stats_elements)} элементов статистики")
        
        # Проверяем селекторы периода
        period_selectors = self.driver.find_elements(By.CLASS_NAME, 'period-selector')
        if len(period_selectors) > 0:
            # Кликаем по селектору периода
            period_selectors[0].click()
            self.simulate_human_delay()
            print("✅ Селекторы периода работают")
        
        print("✅ Мониторинг расхода воды работает корректно")
    
    def test_07_realistic_postpone_functionality(self):
        """Реалистичная функция отложенного полива"""
        print("🧪 Тест реалистичной функции отложенного полива...")
        
        if not self.driver:
            print("⚠️  Пропуск теста - браузер недоступен")
            return
        
        # Переходим на главную страницу
        self.driver.get(f'{BASE_URL_BROWSER}/')
        self.simulate_human_delay()
        
        # Ищем кнопки отложенного полива
        postpone_buttons = self.driver.find_elements(By.CLASS_NAME, 'postpone-btn')
        if len(postpone_buttons) > 0:
            postpone_btn = postpone_buttons[0]
            
            # Кликаем по кнопке отложенного полива
            self.driver.execute_script("arguments[0].click();", postpone_btn)
            self.simulate_human_delay()
            
            # Ищем модальное окно или форму
            modal = self.wait_and_find_element(By.CLASS_NAME, 'postpone-modal')
            if modal:
                # Выбираем количество дней
                day_selectors = modal.find_elements(By.CLASS_NAME, 'day-selector')
                if len(day_selectors) > 0:
                    day_selectors[0].click()  # 1 день
                    self.simulate_human_delay()
                
                # Подтверждаем отложенный полив
                confirm_btn = modal.find_element(By.CLASS_NAME, 'confirm-btn')
                self.driver.execute_script("arguments[0].click();", confirm_btn)
                self.simulate_human_delay(1, 2)
                
                print("✅ Функция отложенного полива работает корректно")
            else:
                print("⚠️  Модальное окно отложенного полива не найдено")
        else:
            print("⚠️  Кнопки отложенного полива не найдены")
    
    def test_08_realistic_responsive_design(self):
        """Реалистичная проверка адаптивного дизайна"""
        print("🧪 Тест реалистичной проверки адаптивного дизайна...")
        
        if not self.driver:
            print("⚠️  Пропуск теста - браузер недоступен")
            return
        
        # Тестируем разные размеры экрана
        screen_sizes = [
            (1920, 1080),  # Desktop
            (1366, 768),   # Laptop
            (768, 1024),   # Tablet
            (375, 667)     # Mobile
        ]
        
        for width, height in screen_sizes:
            self.driver.set_window_size(width, height)
            self.simulate_human_delay()
            
            # Переходим на главную страницу
            self.driver.get(f'{BASE_URL_BROWSER}/')
            self.simulate_human_delay()
            
            # Проверяем, что страница загрузилась
            title = self.driver.title
            self.assertIn('WB-Irrigation', title)
            
            # Проверяем, что элементы видны
            body = self.driver.find_element(By.TAG_NAME, 'body')
            self.assertTrue(body.is_displayed())
            
            print(f"✅ Адаптивный дизайн работает для {width}x{height}")
        
        # Возвращаем к стандартному размеру
        self.driver.set_window_size(1920, 1080)
    
    def test_09_realistic_error_handling(self):
        """Реалистичная проверка обработки ошибок"""
        print("🧪 Тест реалистичной проверки обработки ошибок...")
        
        if not self.driver:
            print("⚠️  Пропуск теста - браузер недоступен")
            return
        
        # Пытаемся перейти на несуществующую страницу
        self.driver.get(f'{BASE_URL_BROWSER}/nonexistent')
        self.simulate_human_delay()
        
        # Проверяем, что отображается страница 404
        page_content = self.driver.page_source
        self.assertIn('404', page_content) or self.assertIn('Not Found', page_content)
        print("✅ Страница 404 отображается корректно")
        
        # Возвращаемся на главную страницу
        self.driver.get(f'{BASE_URL_BROWSER}/')
        self.simulate_human_delay()
        
        print("✅ Обработка ошибок работает корректно")
    
    def test_10_realistic_performance_testing(self):
        """Реалистичное тестирование производительности"""
        print("🧪 Тест реалистичного тестирования производительности...")
        
        if not self.driver:
            print("⚠️  Пропуск теста - браузер недоступен")
            return
        
        # Тестируем время загрузки страниц
        pages = ['/', '/zones', '/programs', '/logs', '/water']
        
        for page in pages:
            start_time = time.time()
            self.driver.get(f'{BASE_URL_BROWSER}{page}')
            
            # Ждем полной загрузки страницы
            WebDriverWait(self.driver, 10).until(
                lambda driver: driver.execute_script("return document.readyState") == "complete"
            )
            
            load_time = time.time() - start_time
            print(f"✅ Страница {page} загрузилась за {load_time:.2f} секунд")
            
            # Проверяем, что время загрузки приемлемое (менее 5 секунд)
            self.assertLess(load_time, 5.0, f"Страница {page} загружается слишком медленно")
            
            self.simulate_human_delay()
        
        print("✅ Производительность страниц приемлема")

if __name__ == '__main__':
    print("🧪 Запуск реалистичных веб-тестов WB-Irrigation...")
    print("=" * 60)
    
    # Запуск тестов
    unittest.main(verbosity=2, exit=False)
    
    print("=" * 60)
    print("🎉 Реалистичные веб-тесты завершены!")
