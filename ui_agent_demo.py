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
    args = parser.parse_args()

    shots_dir = Path('ui_demo_screens')

    chrome_options = Options()
    if args.headless:
        chrome_options.add_argument('--headless=new')
    chrome_options.add_argument('--no-sandbox')
    chrome_options.add_argument('--disable-dev-shm-usage')
    chrome_options.add_argument('--window-size=1440,900')
    chrome_options.add_argument('--force-device-scale-factor=1')

    driver = webdriver.Chrome(service=Service(ChromeDriverManager().install()), options=chrome_options)
    driver.implicitly_wait(5)

    try:
        # Проверка, что сервер жив
        try:
            requests.get(args.base + '/api/status', timeout=3)
        except Exception:
            print('⚠️  Сервер недоступен по /api/status — продолжу попытку открыть UI')

        # 1) Статус: старт/стоп зоны, запуск/стоп группы, отложка, авария/возврат
        driver.get(args.base + '/')
        wait_ready(driver)
        if args.shots:
            screenshot(driver, shots_dir, 'status_loaded')

        # Найти и кликнуть первую кнопку зоны
        clicked = click_if_exists(driver, By.CSS_SELECTOR, 'button.zone-start-btn', args.slow, args.shots, shots_dir, 'zone_start')
        if clicked:
            # клик по той же кнопке снова (стоп)
            click_if_exists(driver, By.CSS_SELECTOR, 'button.zone-start-btn', args.slow, args.shots, shots_dir, 'zone_stop')

        # Запуск/стоп группы
        click_if_exists(driver, By.CSS_SELECTOR, 'button.continue-group', args.slow, args.shots, shots_dir, 'group_start')
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

        # 3) Программы
        driver.get(args.base + '/programs')
        wait_ready(driver)
        if args.shots:
            screenshot(driver, shots_dir, 'programs_loaded')

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


