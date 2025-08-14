#!/usr/bin/env python3
"""
Скрипт для установки и настройки Selenium для веб-тестирования
"""

import subprocess
import sys
import os
import platform

def run_command(command, description):
    """Запуск команды с выводом"""
    print(f"🔧 {description}...")
    try:
        result = subprocess.run(command, shell=True, capture_output=True, text=True)
        if result.returncode == 0:
            print(f"✅ {description} выполнено успешно")
            return True
        else:
            print(f"❌ {description} не выполнено")
            print(f"Ошибка: {result.stderr}")
            return False
    except Exception as e:
        print(f"❌ Ошибка при {description}: {e}")
        return False

def check_python_package(package_name):
    """Проверка установки Python пакета"""
    try:
        __import__(package_name)
        return True
    except ImportError:
        return False

def install_selenium():
    """Установка Selenium"""
    if check_python_package('selenium'):
        print("✅ Selenium уже установлен")
        return True
    
    return run_command(
        "pip install selenium",
        "Установка Selenium"
    )

def install_webdriver_manager():
    """Установка webdriver-manager для автоматического управления драйверами"""
    if check_python_package('webdriver_manager'):
        print("✅ webdriver-manager уже установлен")
        return True
    
    return run_command(
        "pip install webdriver-manager",
        "Установка webdriver-manager"
    )

def check_chrome():
    """Проверка наличия Chrome"""
    system = platform.system().lower()
    
    if system == "darwin":  # macOS
        chrome_paths = [
            "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
            "/Applications/Chromium.app/Contents/MacOS/Chromium"
        ]
    elif system == "linux":
        chrome_paths = [
            "/usr/bin/google-chrome",
            "/usr/bin/chromium-browser",
            "/usr/bin/chromium"
        ]
    elif system == "windows":
        chrome_paths = [
            "C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe",
            "C:\\Program Files (x86)\\Google\\Chrome\\Application\\chrome.exe"
        ]
    else:
        print("⚠️  Неизвестная операционная система")
        return False
    
    for path in chrome_paths:
        if os.path.exists(path):
            print(f"✅ Chrome найден: {path}")
            return True
    
    print("❌ Chrome не найден")
    print("📋 Рекомендации по установке Chrome:")
    
    if system == "darwin":
        print("   - Скачайте Chrome с https://www.google.com/chrome/")
        print("   - Или установите через Homebrew: brew install --cask google-chrome")
    elif system == "linux":
        print("   - Ubuntu/Debian: sudo apt install google-chrome-stable")
        print("   - CentOS/RHEL: sudo yum install google-chrome-stable")
    elif system == "windows":
        print("   - Скачайте Chrome с https://www.google.com/chrome/")
    
    return False

def test_selenium_setup():
    """Тестирование настройки Selenium"""
    print("🧪 Тестирование настройки Selenium...")
    
    try:
        from selenium import webdriver
        from selenium.webdriver.chrome.service import Service
        from selenium.webdriver.chrome.options import Options
        from webdriver_manager.chrome import ChromeDriverManager
        
        # Настройка Chrome
        chrome_options = Options()
        chrome_options.add_argument("--headless")
        chrome_options.add_argument("--no-sandbox")
        chrome_options.add_argument("--disable-dev-shm-usage")
        
        # Автоматическая установка ChromeDriver
        service = Service(ChromeDriverManager().install())
        
        # Создание драйвера
        driver = webdriver.Chrome(service=service, options=chrome_options)
        
        # Простой тест
        driver.get("https://www.google.com")
        title = driver.title
        driver.quit()
        
        print("✅ Selenium настроен и работает корректно")
        print(f"✅ Тестовый запрос выполнен, заголовок: {title}")
        return True
        
    except Exception as e:
        print(f"❌ Ошибка при тестировании Selenium: {e}")
        return False

def main():
    """Основная функция"""
    print("🚀 Настройка Selenium для веб-тестирования WB-Irrigation")
    print("=" * 60)
    
    # Проверяем Python
    print(f"🐍 Python версия: {sys.version}")
    
    # Устанавливаем необходимые пакеты
    success = True
    
    success &= install_selenium()
    success &= install_webdriver_manager()
    
    # Проверяем Chrome
    chrome_available = check_chrome()
    
    if not chrome_available:
        print("⚠️  Chrome не найден, но можно продолжить с другими браузерами")
    
    # Тестируем настройку
    if success:
        selenium_works = test_selenium_setup()
        if selenium_works:
            print("\n🎉 Настройка Selenium завершена успешно!")
            print("✅ Теперь можно запускать реалистичные веб-тесты")
        else:
            print("\n⚠️  Selenium настроен, но есть проблемы с драйверами")
            print("🔧 Попробуйте установить Chrome или другой браузер")
    else:
        print("\n❌ Настройка Selenium не завершена")
        print("🔧 Проверьте ошибки выше и попробуйте снова")
    
    print("\n📋 Следующие шаги:")
    print("1. Убедитесь, что Chrome установлен")
    print("2. Запустите реалистичные тесты: python web_tests_realistic.py")
    print("3. Если Chrome недоступен, используйте простые тесты: python web_tests_simple.py")

if __name__ == '__main__':
    main()
