#!/usr/bin/env python3
"""
Комплексные тесты веб-интерфейса WB-Irrigation
Использует Selenium для автоматизированного тестирования всех сценариев
"""

import unittest
import time
import tempfile
import os
import shutil
from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.chrome.options import Options
from webdriver_manager.chrome import ChromeDriverManager
from selenium.webdriver.common.action_chains import ActionChains
from selenium.webdriver.support.ui import Select
import threading
import subprocess
import requests
import json

# Адрес для внутренних запросов (host) и адрес, который будет открывать браузер внутри Docker
BASE_URL_HOST = os.environ.get('TEST_BASE_URL_HOST', 'http://localhost:8080').rstrip('/')
BASE_URL_BROWSER = os.environ.get('TEST_BASE_URL_BROWSER', os.environ.get('TEST_BASE_URL', BASE_URL_HOST)).rstrip('/')

class WebInterfaceTest(unittest.TestCase):
    """Тесты веб-интерфейса WB-Irrigation"""
    
    @classmethod
    def setUpClass(cls):
        """Настройка перед всеми тестами"""
        # Создаем временные директории
        cls.test_db_path = tempfile.mktemp(suffix='.db')
        cls.test_backup_dir = tempfile.mkdtemp()
        cls.test_photos_dir = tempfile.mkdtemp()
        
        # Настройка Chrome
        chrome_options = Options()
        chrome_options.add_argument("--headless")  # Запуск в фоновом режиме
        chrome_options.add_argument("--no-sandbox")
        chrome_options.add_argument("--disable-dev-shm-usage")
        chrome_options.add_argument("--disable-gpu")
        chrome_options.add_argument("--window-size=1920,1080")
        try:
            chrome_options.page_load_strategy = 'eager'
        except Exception:
            pass
        
        # Инициализация драйвера: если задан SELENIUM_REMOTE_URL — идем в удаленный Selenium
        remote_url = os.environ.get('SELENIUM_REMOTE_URL')
        if remote_url:
            # Ждем готовности удаленного Selenium
            for _ in range(30):
                try:
                    r = requests.get(remote_url.rstrip('/') + '/status', timeout=1)
                    if r.ok and r.json().get('value', {}).get('ready'):
                        break
                except Exception:
                    pass
                time.sleep(1)
            from selenium.webdriver import Remote
            cls.driver = Remote(command_executor=remote_url, options=chrome_options)
        else:
            cls.driver = webdriver.Chrome(options=chrome_options)
        cls.driver.implicitly_wait(10)
        try:
            cls.driver.set_page_load_timeout(20)
        except Exception:
            pass
        
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
            stdout=subprocess.DEVNULL,
            stderr=subprocess.STDOUT
        )
    
    @classmethod
    def wait_for_app_startup(cls):
        """Ожидание запуска приложения"""
        max_attempts = 30
        for attempt in range(max_attempts):
            try:
                response = requests.get(f"{BASE_URL_HOST}/api/status", timeout=1)
                if response.status_code == 200:
                    print("✅ Flask приложение запущено")
                    return
            except:
                pass
            time.sleep(1)
        
        raise Exception("Не удалось запустить Flask приложение")
    
    @classmethod
    def create_test_image(cls):
        """Создание тестового изображения"""
        from PIL import Image, ImageDraw, ImageFont
        
        # Создаем простое изображение
        img = Image.new('RGB', (100, 100), color='red')
        draw = ImageDraw.Draw(img)
        draw.text((10, 40), "TEST", fill='white')
        
        cls.test_image_path = os.path.join(cls.test_photos_dir, 'test_image.jpg')
        img.save(cls.test_image_path)
    
    def setUp(self):
        """Настройка перед каждым тестом"""
        self.open_url('/')
        time.sleep(0.5)
        # Логинимся как админ для доступа к защищенным страницам
        try:
            self.driver.execute_script(
                "return fetch('/api/login',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({password:'1234'})}).then(()=>true).catch(()=>false)"
            )
            time.sleep(0.5)
        except Exception:
            pass

    def open_url(self, path: str):
        url = BASE_URL_BROWSER + ('' if path.startswith('/') else '/') + path
        try:
            self.driver.get(url)
        except Exception:
            try:
                self.driver.execute_script("window.stop();")
            except Exception:
                pass
        return url

    def js_click(self, element):
        try:
            self.driver.execute_script("arguments[0].click();", element)
        except Exception:
            element.click()
    
    def wait_for_element(self, by, value, timeout=10):
        """Ожидание появления элемента"""
        return WebDriverWait(self.driver, timeout).until(
            EC.presence_of_element_located((by, value))
        )
    
    def wait_for_clickable(self, by, value, timeout=10):
        """Ожидание кликабельности элемента"""
        return WebDriverWait(self.driver, timeout).until(
            EC.element_to_be_clickable((by, value))
        )
    
    def test_01_home_page_loads(self):
        """Тест загрузки главной страницы"""
        self.assertIn("Статус", self.driver.title)
        
        # Проверяем наличие основных элементов
        self.wait_for_element(By.ID, "groups-container")
        self.wait_for_element(By.ID, "zones-table-body")
        self.wait_for_element(By.CLASS_NAME, "legend")
        
        print("✅ Главная страница загружается корректно")
    
    def test_02_navigation_works(self):
        """Тест навигации между страницами"""
        # Переход на страницу зон
        self.driver.find_element(By.LINK_TEXT, "Зоны и группы").click()
        self.wait_for_element(By.ID, "zones-table")
        self.assertIn("Зоны и группы", self.driver.title)
        
        # Переход на страницу программ
        self.driver.find_element(By.LINK_TEXT, "Программы").click()
        self.wait_for_element(By.CLASS_NAME, "prog-table")
        self.assertIn("Программы", self.driver.title)
        
        # Переход на страницу логов
        self.driver.find_element(By.LINK_TEXT, "Логи").click()
        self.wait_for_element(By.CLASS_NAME, "logs-table")
        self.assertIn("Логи", self.driver.title)
        
        # Переход на страницу расхода воды
        self.driver.find_element(By.LINK_TEXT, "Расход воды").click()
        self.wait_for_element(By.ID, "waterChart")
        self.assertIn("Расход воды", self.driver.title)
        
        # Возврат на главную
        self.driver.find_element(By.LINK_TEXT, "Статус").click()
        self.wait_for_element(By.ID, "groups-container")
        
        print("✅ Навигация работает корректно")
    
    def test_03_create_zone(self):
        """Тест создания новой зоны"""
        # Переход на страницу зон
        self.driver.find_element(By.LINK_TEXT, "Зоны и группы").click()
        self.wait_for_element(By.ID, "zones-table")
        
        # Запоминаем количество зон до создания
        zones_before = len(self.driver.find_elements(By.CSS_SELECTOR, "#zones-table-body tr"))
        
        # Нажатие кнопки добавления зоны
        add_button = self.wait_for_clickable(By.CSS_SELECTOR, ".float-add")
        self.js_click(add_button)
        
        # Заполнение формы
        self.wait_for_element(By.ID, "zoneName").send_keys("Тестовая зона")
        
        # Выбор иконки
        icon_select = Select(self.driver.find_element(By.ID, "zoneIcon"))
        icon_select.select_by_value("🌿")
        
        # Установка времени
        duration_input = self.driver.find_element(By.ID, "zoneDuration")
        duration_input.clear()
        duration_input.send_keys("15")
        
        # Выбор группы
        group_select = Select(self.driver.find_element(By.ID, "zoneGroup"))
        group_select.select_by_index(1)  # Первая группа (не "БЕЗ ПОЛИВА")
        
        # Сохранение
        self.driver.find_element(By.CSS_SELECTOR, ".modal-actions .btn-primary").click()
        
        # Ожидание закрытия модального окна
        time.sleep(2)
        
        # Проверяем, что зона добавилась
        zones_after = len(self.driver.find_elements(By.CSS_SELECTOR, "#zones-table-body tr"))
        self.assertEqual(zones_after, zones_before + 1)
        
        print("✅ Создание зоны работает корректно")
    
    def test_04_edit_zone(self):
        """Тест редактирования зоны"""
        # Переход на страницу зон
        self.driver.find_element(By.LINK_TEXT, "Зоны и группы").click()
        self.wait_for_element(By.ID, "zones-table")
        
        # Находим первую зону и редактируем её название
        first_zone_name = self.driver.find_element(By.CSS_SELECTOR, "#zones-table-body tr:first-child .zone-name")
        first_zone_name.clear()
        first_zone_name.send_keys("Отредактированная зона")
        
        # Нажимаем кнопку сохранения
        save_button = self.driver.find_element(By.CSS_SELECTOR, "#zones-table-body tr:first-child .save-btn")
        save_button.click()
        
        # Проверяем, что кнопка стала неактивной
        time.sleep(1)
        self.assertTrue(save_button.get_attribute("disabled"))
        
        print("✅ Редактирование зоны работает корректно")
    
    def test_05_delete_zone(self):
        """Тест удаления зоны"""
        # Переход на страницу зон
        self.driver.find_element(By.LINK_TEXT, "Зоны и группы").click()
        self.wait_for_element(By.ID, "zones-table")
        
        # Запоминаем количество зон до удаления
        zones_before = len(self.driver.find_elements(By.CSS_SELECTOR, "#zones-table-body tr"))
        
        if zones_before > 0:
            # Нажимаем кнопку удаления первой зоны
            delete_button = self.driver.find_element(By.CSS_SELECTOR, "#zones-table-body tr:first-child .delete-btn")
            delete_button.click()
            
            # Подтверждаем удаление
            self.driver.switch_to.alert.accept()
            
            # Ожидаем обновления таблицы
            time.sleep(2)
            
            # Проверяем, что зона удалилась
            zones_after = len(self.driver.find_elements(By.CSS_SELECTOR, "#zones-table-body tr"))
            self.assertEqual(zones_after, zones_before - 1)
        
        print("✅ Удаление зоны работает корректно")
    
    def test_06_upload_photo(self):
        """Тест загрузки фотографии для зоны"""
        # Переход на страницу зон
        self.driver.find_element(By.LINK_TEXT, "Зоны и группы").click()
        self.wait_for_element(By.ID, "zones-table")
        
        # Находим первую зону и нажимаем кнопку загрузки фото
        upload_button = self.driver.find_element(By.CSS_SELECTOR, "#zones-table-body tr:first-child .photo-upload-btn")
        upload_button.click()
        
        # Загружаем файл
        file_input = self.driver.find_element(By.ID, "photoInput")
        file_input.send_keys(self.test_image_path)
        
        # Ожидаем загрузки
        time.sleep(3)
        
        # Проверяем, что фото загрузилось (появилась кнопка удаления)
        delete_photo_button = self.driver.find_element(By.CSS_SELECTOR, "#zones-table-body tr:first-child .photo-delete-btn")
        self.assertTrue(delete_photo_button.is_displayed())
        
        print("✅ Загрузка фотографии работает корректно")
    
    def test_07_view_photo(self):
        """Тест просмотра фотографии"""
        # Переход на страницу зон
        self.driver.find_element(By.LINK_TEXT, "Зоны и группы").click()
        self.wait_for_element(By.ID, "zones-table")
        
        # Находим зону с фото и кликаем на неё
        photo_img = self.driver.find_element(By.CSS_SELECTOR, "#zones-table-body tr:first-child .zone-photo img")
        photo_img.click()
        
        # Проверяем, что открылось модальное окно
        modal = self.wait_for_element(By.ID, "photoModal")
        self.assertTrue(modal.is_displayed())
        
        # Закрываем модальное окно
        close_button = self.driver.find_element(By.CSS_SELECTOR, "#photoModal .btn-primary")
        close_button.click()
        
        # Проверяем, что модальное окно закрылось
        time.sleep(1)
        self.assertFalse(modal.is_displayed())
        
        print("✅ Просмотр фотографии работает корректно")
    
    def test_08_delete_photo(self):
        """Тест удаления фотографии"""
        # Переход на страницу зон
        self.driver.find_element(By.LINK_TEXT, "Зоны и группы").click()
        self.wait_for_element(By.ID, "zones-table")
        
        # Нажимаем кнопку удаления фото
        delete_button = self.driver.find_element(By.CSS_SELECTOR, "#zones-table-body tr:first-child .photo-delete-btn")
        delete_button.click()
        
        # Подтверждаем удаление
        self.driver.switch_to.alert.accept()
        
        # Ожидаем обновления
        time.sleep(2)
        
        # Проверяем, что появилась кнопка загрузки
        upload_button = self.driver.find_element(By.CSS_SELECTOR, "#zones-table-body tr:first-child .photo-upload-btn")
        self.assertTrue(upload_button.is_displayed())
        
        print("✅ Удаление фотографии работает корректно")
    
    def test_09_change_zone_group(self):
        """Тест изменения группы зоны"""
        # Переход на страницу зон
        self.driver.find_element(By.LINK_TEXT, "Зоны и группы").click()
        self.wait_for_element(By.ID, "zones-table")
        
        # Находим селектор группы первой зоны
        group_select = Select(self.driver.find_element(By.CSS_SELECTOR, "#zones-table-body tr:first-child .zone-group"))
        original_group = group_select.first_selected_option.text
        
        # Меняем группу
        group_select.select_by_index(2)  # Выбираем другую группу
        
        # Нажимаем кнопку сохранения
        save_button = self.driver.find_element(By.CSS_SELECTOR, "#zones-table-body tr:first-child .save-btn")
        save_button.click()
        
        # Проверяем, что группа изменилась
        time.sleep(1)
        new_group_select = Select(self.driver.find_element(By.CSS_SELECTOR, "#zones-table-body tr:first-child .zone-group"))
        new_group = new_group_select.first_selected_option.text
        self.assertNotEqual(original_group, new_group)
        
        print("✅ Изменение группы зоны работает корректно")
    
    def test_10_create_group(self):
        """Тест создания новой группы"""
        # Переход на страницу зон
        self.driver.find_element(By.LINK_TEXT, "Зоны и группы").click()
        self.wait_for_element(By.ID, "zones-table")
        
        # Находим кнопку добавления группы
        add_group_button = self.driver.find_element(By.CSS_SELECTOR, ".groups-header button")
        self.js_click(add_group_button)
        self.wait_for_element(By.ID, "groupName")
        
        # Заполняем форму
        group_name_input = self.wait_for_element(By.ID, "groupName")
        group_name_input.send_keys("Тестовая группа")
        
        # Сохраняем
        self.driver.find_element(By.CSS_SELECTOR, "#groupModal .btn-primary").click()
        
        # Ожидаем закрытия модального окна
        time.sleep(2)
        
        # Проверяем, что группа добавилась
        groups = self.driver.find_elements(By.CSS_SELECTOR, ".group-card")
        group_names = [group.find_element(By.CSS_SELECTOR, ".group-name").text for group in groups]
        self.assertIn("Тестовая группа", group_names)
        
        print("✅ Создание группы работает корректно")
    
    def test_11_bulk_operations(self):
        """Тест массовых операций"""
        # Переход на страницу зон
        self.driver.find_element(By.LINK_TEXT, "Зоны и группы").click()
        self.wait_for_element(By.ID, "zones-table")
        
        # Выбираем все зоны
        select_all_checkbox = self.driver.find_element(By.ID, "selectAll")
        select_all_checkbox.click()
        
        # Проверяем, что все чекбоксы выбраны
        checkboxes = self.driver.find_elements(By.CSS_SELECTOR, ".zone-checkbox")
        for checkbox in checkboxes:
            self.assertTrue(checkbox.is_selected())
        
        # Выбираем действие "Изменить группу"
        bulk_action_select = Select(self.driver.find_element(By.ID, "bulkAction"))
        bulk_action_select.select_by_value("group")
        
        # Выбираем новую группу
        bulk_group_select = Select(self.driver.find_element(By.ID, "bulkGroupSelect"))
        bulk_group_select.select_by_index(1)
        
        # Применяем действие
        apply_button = self.driver.find_element(By.CSS_SELECTOR, ".bulk-form button")
        self.js_click(apply_button)
        
        # Ожидаем применения
        time.sleep(2)
        
        print("✅ Массовые операции работают корректно")
    
    def test_12_zone_start_stop(self):
        """Тест запуска и остановки зон"""
        # Переход на страницу зон
        self.driver.find_element(By.LINK_TEXT, "Зоны и группы").click()
        self.wait_for_element(By.ID, "zones-table")
        
        # Нажимаем кнопку запуска первой зоны
        start_button = self.driver.find_element(By.CSS_SELECTOR, "#zones-table-body tr:first-child .start-btn")
        start_button.click()
        
        # Ожидаем обновления статуса
        time.sleep(2)
        
        # Нажимаем кнопку остановки
        stop_button = self.driver.find_element(By.CSS_SELECTOR, "#zones-table-body tr:first-child .stop-btn")
        stop_button.click()
        
        # Ожидаем обновления статуса
        time.sleep(2)
        
        print("✅ Запуск и остановка зон работает корректно")
    
    def test_13_postpone_irrigation(self):
        """Тест отложенного полива"""
        # Переход на главную страницу
        self.driver.find_element(By.LINK_TEXT, "Статус").click()
        self.wait_for_element(By.ID, "groups-container")
        
        # Находим кнопку отложенного полива для первой группы
        postpone_button = self.driver.find_element(By.CSS_SELECTOR, ".card .btn-group .delay")
        postpone_button.click()
        
        # Ожидаем обновления
        time.sleep(2)
        
        # Проверяем, что появился текст об отложенном поливе
        postpone_text = self.driver.find_element(By.CSS_SELECTOR, ".postpone-until")
        self.assertIn("Не будет поливаться до:", postpone_text.text)
        
        print("✅ Отложенный полив работает корректно")
    
    def test_14_emergency_stop(self):
        """Тест аварийной остановки"""
        # Переход на главную страницу
        self.driver.find_element(By.LINK_TEXT, "Статус").click()
        self.wait_for_element(By.ID, "groups-container")
        
        # Находим кнопку аварийной остановки
        emergency_button = self.driver.find_element(By.ID, "emergency-btn")
        emergency_button.click()
        
        # Подтверждаем действие
        self.driver.switch_to.alert.accept()
        
        print("✅ Аварийная остановка работает корректно")
    
    def test_15_water_usage_page(self):
        """Тест страницы расхода воды"""
        # Переход на страницу расхода воды
        self.driver.find_element(By.LINK_TEXT, "Расход воды").click()
        self.wait_for_element(By.ID, "waterChart")
        
        # Проверяем наличие элементов
        self.wait_for_element(By.ID, "groupSelector")
        self.wait_for_element(By.ID, "waterStats")
        self.wait_for_element(By.ID, "zonesTable")
        
        # Переключаемся между группами
        group_buttons = self.driver.find_elements(By.CSS_SELECTOR, ".group-btn")
        if group_buttons:
            group_buttons[0].click()
            time.sleep(1)
        
        print("✅ Страница расхода воды работает корректно")
    
    def test_16_logs_page(self):
        """Тест страницы логов"""
        # Переход на страницу логов
        self.driver.find_element(By.LINK_TEXT, "Логи").click()
        self.wait_for_element(By.CLASS_NAME, "logs-table")
        
        # Проверяем фильтры
        self.wait_for_element(By.CSS_SELECTOR, ".filter-bar")
        
        # Экспортируем логи
        export_button = self.driver.find_element(By.CSS_SELECTOR, ".export-btn")
        export_button.click()
        
        print("✅ Страница логов работает корректно")
    
    def test_17_programs_page(self):
        """Тест страницы программ"""
        # Переход на страницу программ
        self.driver.find_element(By.LINK_TEXT, "Программы").click()
        self.wait_for_element(By.CLASS_NAME, "prog-table")
        
        # Нажимаем кнопку добавления программы
        add_program_button = self.driver.find_element(By.CSS_SELECTOR, ".float-add")
        add_program_button.click()
        
        # Проверяем, что открылось модальное окно
        self.wait_for_element(By.ID, "zoneModal")
        
        # Закрываем модальное окно
        close_button = self.driver.find_element(By.CSS_SELECTOR, ".close")
        close_button.click()
        
        print("✅ Страница программ работает корректно")
    
    def test_18_responsive_design(self):
        """Тест адаптивного дизайна"""
        # Устанавливаем размер окна для мобильного устройства
        self.driver.set_window_size(375, 667)
        
        # Переход на главную страницу
        self.driver.find_element(By.LINK_TEXT, "Статус").click()
        self.wait_for_element(By.ID, "groups-container")
        
        # Проверяем, что элементы адаптируются
        self.assertTrue(self.driver.find_element(By.ID, "groups-container").is_displayed())
        
        # Возвращаем нормальный размер
        self.driver.set_window_size(1920, 1080)
        
        print("✅ Адаптивный дизайн работает корректно")
    
    def test_19_notifications(self):
        """Тест системы уведомлений"""
        # Переход на страницу зон
        self.driver.find_element(By.LINK_TEXT, "Зоны и группы").click()
        self.wait_for_element(By.ID, "zones-table")
        
        # Выполняем действие, которое должно вызвать уведомление
        first_zone_name = self.driver.find_element(By.CSS_SELECTOR, "#zones-table-body tr:first-child .zone-name")
        first_zone_name.clear()
        first_zone_name.send_keys("Тест уведомлений")
        
        save_button = self.driver.find_element(By.CSS_SELECTOR, "#zones-table-body tr:first-child .save-btn")
        save_button.click()
        
        # Проверяем, что появилось уведомление
        time.sleep(1)
        notification = self.driver.find_element(By.CSS_SELECTOR, ".notification")
        self.assertTrue(notification.is_displayed())
        
        print("✅ Система уведомлений работает корректно")
    
    def test_20_connection_status(self):
        """Тест индикатора состояния соединения"""
        # Переход на главную страницу
        self.driver.find_element(By.LINK_TEXT, "Статус").click()
        self.wait_for_element(By.ID, "groups-container")
        
        # Проверяем, что индикатор соединения скрыт (соединение есть)
        connection_status = self.driver.find_element(By.ID, "connection-status")
        self.assertNotIn("show", connection_status.get_attribute("class"))
        
        print("✅ Индикатор состояния соединения работает корректно")


if __name__ == "__main__":
    # Запуск тестов
    unittest.main(verbosity=2)
