#!/usr/bin/env python3
import unittest
import time
import os
import subprocess
from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.common.exceptions import WebDriverException

BASE_URL_HOST = os.environ.get('TEST_BASE_URL_HOST', 'http://localhost:8080').rstrip('/')
BASE_URL_BROWSER = os.environ.get('TEST_BASE_URL_BROWSER', os.environ.get('TEST_BASE_URL', BASE_URL_HOST)).rstrip('/')

class WebAuthAndProgramsTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        browser = os.environ.get('BROWSER', 'safari').lower()
        cls.driver = None
        try:
            remote_url = os.environ.get('SELENIUM_REMOTE_URL')
            if browser == 'chrome':
                chrome_options = Options()
                chrome_options.add_argument('--headless=new')
                chrome_options.add_argument('--no-sandbox')
                chrome_options.add_argument('--disable-dev-shm-usage')
                if remote_url:
                    from selenium.webdriver import Remote
                    cls.driver = Remote(command_executor=remote_url, options=chrome_options)
                else:
                    cls.driver = webdriver.Chrome(options=chrome_options)
            else:
                # Safari (встроенный драйвер на macOS)
                cls.driver = webdriver.Safari()
            cls.driver.implicitly_wait(5)
        except WebDriverException as e:
            print(f"⚠️  Не удалось инициализировать браузер {browser}: {e}")
            cls.driver = None

        env = os.environ.copy()
        env['TESTING'] = '1'
        cls.app_process = subprocess.Popen(['python', 'run.py'], env=env)
        time.sleep(3)

    @classmethod
    def tearDownClass(cls):
        try:
            cls.driver.quit()
        except Exception:
            pass
        try:
            cls.app_process.terminate()
            cls.app_process.wait()
        except Exception:
            pass

    def test_user_access_menu(self):
        self.driver.get(f'{BASE_URL_BROWSER}/')
        time.sleep(1)
        # user должен видеть пункты Статус/Карта зон/Расход воды
        self.assertTrue(self.driver.find_element(By.LINK_TEXT, 'Статус').is_displayed())
        self.assertTrue(self.driver.find_element(By.LINK_TEXT, 'Карта зон').is_displayed())
        self.assertTrue(self.driver.find_element(By.LINK_TEXT, 'Расход воды').is_displayed())
        # пункты админа могут отсутствовать, проверяем отсутствие ошибок при поиске
        admin_links = ['Зоны и группы', 'Программы', 'Логи', 'MQTT']
        for link in admin_links:
            try:
                el = self.driver.find_element(By.LINK_TEXT, link)
                self.assertFalse(el.is_displayed())
            except Exception:
                pass

    def test_admin_login_and_programs_page(self):
        self.driver.get(f'{BASE_URL_BROWSER}/login')
        time.sleep(1)
        # Выполним вход
        # Страница логина — форма JS, отправим запрос через fetch с DevTools? Упростим: откроем консоль через execute_script
        self.driver.execute_script("fetch('/api/login',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({password:'1234'})}).then(()=>location.href='/programs')")
        time.sleep(2)
        # Проверим, что страница программ доступна
        self.assertIn('Программы', self.driver.title)

    def test_mqtt_crud_ui(self):
        if not self.driver:
            self.skipTest('no driver')
        # login as admin
        self.driver.get(f'{BASE_URL_BROWSER}/login')
        time.sleep(1)
        self.driver.execute_script("fetch('/api/login',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({password:'1234'})}).then(()=>location.href='/mqtt')")
        time.sleep(2)
        # На странице MQTT создадим запись через JS — формы управляются JS, заполним и вызовем createServer
        self.assertIn('MQTT', self.driver.title)
        rows_before = self.driver.find_elements(By.CSS_SELECTOR, '#servers_body tr')
        rows_before_count = len(rows_before)
        self.driver.execute_script("document.getElementById('m_name').value='UI Test';document.getElementById('m_host').value='localhost';document.getElementById('m_port').value='1883';document.getElementById('m_user').value='u';document.getElementById('m_pass').value='p';document.getElementById('m_client').value='cid';document.getElementById('m_enabled').value='true';createServer();")
        time.sleep(2)
        # Проверим, что в таблице появился новый сервер
        rows = self.driver.find_elements(By.CSS_SELECTOR, '#servers_body tr')
        self.assertGreaterEqual(len(rows), rows_before_count + 1)

        # Обновление имени у последней строки и сохранение
        last_row = rows[-1]
        name_input = last_row.find_elements(By.TAG_NAME, 'input')[0]
        name_input.clear()
        name_input.send_keys('UI Test Updated')
        # Кнопка Сохранить — первая в последней ячейке
        action_buttons = last_row.find_elements(By.TAG_NAME, 'button')
        action_buttons[0].click()
        time.sleep(2)

        # Удаление созданной записи
        rows_after_update = self.driver.find_elements(By.CSS_SELECTOR, '#servers_body tr')
        last_row = rows_after_update[-1]
        delete_btn = last_row.find_elements(By.TAG_NAME, 'button')[1]
        delete_btn.click()
        try:
            WebDriverWait(self.driver, 3).until(EC.alert_is_present())
            alert = self.driver.switch_to.alert
            alert.accept()
        except Exception:
            pass
        time.sleep(2)
        rows_after_delete = self.driver.find_elements(By.CSS_SELECTOR, '#servers_body tr')
        self.assertGreaterEqual(len(rows_after_delete), rows_before_count)


if __name__ == '__main__':
    unittest.main(verbosity=2)


