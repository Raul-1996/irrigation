#!/usr/bin/env python3
"""
UI Agent Demo: наглядное кликанье по интерфейсу c подсветкой элементов, задержками и скриншотами.

Запуск (видимый браузер):
  python ui_agent_demo.py --base http://127.0.0.1:8080 --headless 0 --slow 0.8 --shots 1 --gif 1

Параметры:
  --base      Базовый URL приложения (по умолчанию http://127.0.0.1:8080)
  --headless  0/1 — видимый/фоновый режим Chrome (по умолчанию 0)
  --slow      Пауза между действиями в секундах (по умолчанию 0.8)
  --shots     0/1 — сохранять скриншоты после действий (по умолчанию 1)
  --gif       0/1 — собрать GIF из скриншотов (по умолчанию 0)
"""

import os
import time
import argparse
import datetime as dt
import requests
from pathlib import Path

from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from webdriver_manager.chrome import ChromeDriverManager

try:
    from PIL import Image
except Exception:
    Image = None


def highlight(driver, element, color="#ffeb3b"):
    try:
        driver.execute_script(
            "arguments[0].scrollIntoView({block: 'center'});"
            "arguments[0].style.outline='3px solid %s';"
            "arguments[0].style.transition='outline 0.2s ease';" % color,
            element,
        )
    except Exception:
        pass


def screenshot(driver, out_dir: Path, name: str):
    out_dir.mkdir(parents=True, exist_ok=True)
    ts = dt.datetime.now().strftime('%Y%m%d_%H%M%S_%f')
    path = out_dir / f"{ts}_{name}.png"
    try:
        driver.save_screenshot(str(path))
    except Exception:
        pass
    return path


def wait_ready(driver, timeout=10):
    WebDriverWait(driver, timeout).until(
        lambda d: d.execute_script("return document.readyState") == "complete"
    )

def wait_selector(driver, by, value, timeout=12):
    try:
        return WebDriverWait(driver, timeout).until(
            EC.presence_of_element_located((by, value))
        )
    except Exception:
        return None


def safe_navigate(driver, primary_url: str, fallback_url: str, timeout: int = 10):
    """Надёжная навигация: несколько попыток и альтернативные способы перехода."""
    def ok():
        try:
            cur = driver.current_url or ''
            return cur.startswith('http://127.0.0.1') or cur.startswith('http://localhost')
        except Exception:
            return False

    # Попытка 1: обычный get на основной URL
    try:
        driver.get('about:blank')
        driver.get(primary_url)
        wait_ready(driver, timeout)
        if ok():
            return True
    except Exception:
        pass

    # Попытка 2: JS-присвоение location
    try:
        driver.get('about:blank')
        driver.execute_script("window.location.assign(arguments[0])", primary_url)
        wait_ready(driver, timeout)
        if ok():
            return True
    except Exception:
        pass

    # Попытка 3: новая вкладка на запасной URL
    try:
        driver.switch_to.new_window('tab')
        driver.get(fallback_url)
        wait_ready(driver, timeout)
        if ok():
            return True
    except Exception:
        pass

    # Попытка 4: прямая загрузка запасного URL с JS
    try:
        driver.get('about:blank')
        driver.execute_script("window.location.href = arguments[0]", fallback_url)
        wait_ready(driver, timeout)
        if ok():
            return True
    except Exception:
        pass

    return False


def click_if_exists(driver, by, value, slow, shots, out_dir, shot_name):
    try:
        el = WebDriverWait(driver, 5).until(EC.element_to_be_clickable((by, value)))
        highlight(driver, el)
        time.sleep(slow)
        el.click()
        time.sleep(slow)
        if shots:
            screenshot(driver, out_dir, shot_name)
        return True
    except Exception:
        return False


def build_gif(out_dir: Path, gif_path: Path, duration_ms=500):
    if not Image:
        return
    files = sorted(out_dir.glob('*.png'))
    if not files:
        return
    frames = [Image.open(p) for p in files]
    frames[0].save(
        gif_path,
        save_all=True,
        append_images=frames[1:],
        duration=duration_ms,
        loop=0
    )


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--base', default=os.environ.get('BASE_URL', 'http://127.0.0.1:8080'))
    parser.add_argument('--headless', type=int, default=int(os.environ.get('CHROME_HEADLESS', '0')))
    parser.add_argument('--slow', type=float, default=0.8)
    parser.add_argument('--shots', type=int, default=1)
    parser.add_argument('--gif', type=int, default=0)
    parser.add_argument('--admin-pass', default=os.environ.get('ADMIN_PASS','8888'))
    args = parser.parse_args()

    shots_dir = Path('ui_demo_screens')

    chrome_options = Options()
    if args.headless:
        chrome_options.add_argument('--headless=new')
    chrome_options.add_argument('--no-sandbox')
    chrome_options.add_argument('--disable-dev-shm-usage')
    chrome_options.add_argument('--window-size=1440,900')
    chrome_options.add_argument('--force-device-scale-factor=1')

    # Сначала пробуем встроенный Selenium Manager (без webdriver-manager)
    try:
        driver = webdriver.Chrome(options=chrome_options)
    except Exception:
        # Фолбэк: webdriver-manager
        driver = webdriver.Chrome(service=Service(ChromeDriverManager().install()), options=chrome_options)
    driver.implicitly_wait(5)

    try:
        # Проверка, что сервер жив
        try:
            requests.get(args.base + '/api/status', timeout=3)
        except Exception:
            print('⚠️  Сервер недоступен по /api/status — продолжу попытку открыть UI')

        # 0) Авторизация (если требуется)
        # Пробуем открыть логин и войти админом
        try:
            driver.get(base_a + 'login')
            wait_ready(driver)
            pwd = wait_selector(driver, By.ID, 'password', timeout=5)
            if pwd:
                highlight(driver, pwd)
                pwd.clear(); pwd.send_keys(args.admin_pass)
                click_if_exists(driver, By.CSS_SELECTOR, 'button[type="submit"]', args.slow, args.shots, shots_dir, 'login_submit')
                time.sleep(args.slow)
        except Exception:
            pass

        # 0.5) Явно инициируем планировщик
        try:
            requests.post(args.base.rstrip('/') + '/api/scheduler/init', timeout=2)
        except Exception:
            pass

        # 1) Статус: старт/стоп зоны, запуск/стоп группы, отложка, авария/возврат
        base_a = (args.base or 'http://127.0.0.1:8080').rstrip('/') + '/'
        base_b = 'http://localhost:8080/'
        if not safe_navigate(driver, base_a, base_b, timeout=12):
            # Последняя попытка напрямую
            driver.get(base_a)
        wait_ready(driver)
        if args.shots:
            screenshot(driver, shots_dir, 'status_loaded')

        # Дождаться отрисовки таблицы с зонами и найти кнопку зоны
        wait_selector(driver, By.CSS_SELECTOR, '#zones-table-body', timeout=15)
        clicked = click_if_exists(driver, By.CSS_SELECTOR, 'button.zone-start-btn', args.slow, args.shots, shots_dir, 'zone_start')
        if clicked:
            # клик по той же кнопке снова (стоп)
            click_if_exists(driver, By.CSS_SELECTOR, 'button.zone-start-btn', args.slow, args.shots, shots_dir, 'zone_stop')

        # Запуск/стоп группы
        # Перед запуском проверяем, что планировщик доступен (навигация уже инициализировала его)
        click_if_exists(driver, By.CSS_SELECTOR, 'button.continue-group', args.slow, args.shots, shots_dir, 'group_start')
        time.sleep(args.slow)
        click_if_exists(driver, By.CSS_SELECTOR, 'button.stop-group', args.slow, args.shots, shots_dir, 'group_stop')

        # Отложить и отменить
        click_if_exists(driver, By.CSS_SELECTOR, 'button.delay', args.slow, args.shots, shots_dir, 'postpone')
        click_if_exists(driver, By.CSS_SELECTOR, 'button.cancel-postpone', args.slow, args.shots, shots_dir, 'postpone_cancel')

        # Аварийная остановка и продолжение
        try:
            driver.execute_script("window.confirm = ()=>true;")
        except Exception:
            pass
        click_if_exists(driver, By.ID, 'emergency-btn', args.slow, args.shots, shots_dir, 'emergency')
        click_if_exists(driver, By.ID, 'resume-btn', args.slow, args.shots, shots_dir, 'resume')

        # 2) Зоны страница
        driver.get(args.base + '/zones')
        wait_ready(driver)
        if args.shots:
            screenshot(driver, shots_dir, 'zones_loaded')
        # Попробуем первый toggle на карточке зоны, если есть (совместимость со старым UI)
        click_if_exists(driver, By.CLASS_NAME, 'toggle-btn', args.slow, args.shots, shots_dir, 'zones_toggle')

        # 3) Программы
        driver.get(args.base + '/programs')
        wait_ready(driver)
        if args.shots:
            screenshot(driver, shots_dir, 'programs_loaded')
        # Проверка CRUD: клик по кнопке добавления, если есть
        click_if_exists(driver, By.CLASS_NAME, 'add-program-btn', args.slow, args.shots, shots_dir, 'program_add_click')

        # 4) Логи
        driver.get(args.base + '/logs')
        wait_ready(driver)
        if args.shots:
            screenshot(driver, shots_dir, 'logs_loaded')

        # 5) Расход воды
        driver.get(args.base + '/water')
        wait_ready(driver)
        if args.shots:
            screenshot(driver, shots_dir, 'water_loaded')

        # 6) MQTT (если есть)
        try:
            driver.get(args.base + '/mqtt')
            wait_ready(driver)
            if args.shots:
                screenshot(driver, shots_dir, 'mqtt_loaded')
        except Exception:
            pass

        if args.gif:
            build_gif(shots_dir, Path('ui_demo_walkthrough.gif'), duration_ms=int(args.slow * 1000))

        print('✅ UI Agent Demo завершен. Скриншоты в', shots_dir)
    finally:
        driver.quit()


if __name__ == '__main__':
    main()


