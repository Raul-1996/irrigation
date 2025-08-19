#!/usr/bin/env python3
"""
Скрипт для запуска всех тестов WB-Irrigation
Включает модульные тесты и веб-тесты
"""

import subprocess
import sys
import time
import os
from datetime import datetime

def run_command(command, description):
    """Запуск команды с выводом"""
    print(f"\n{'='*60}")
    print(f"🧪 {description}")
    print(f"{'='*60}")
    
    start_time = time.time()
    try:
        result = subprocess.run(command, shell=True, capture_output=True, text=True)
        end_time = time.time()
        duration = end_time - start_time
        
        print(f"⏱️  Время выполнения: {duration:.2f} секунд")
        
        if result.returncode == 0:
            print("✅ Успешно выполнено")
            print("\n📋 Вывод:")
            print(result.stdout)
            return True, result.stdout
        else:
            print("❌ Ошибка выполнения")
            print("\n📋 Вывод:")
            print(result.stdout)
            print("\n🚨 Ошибки:")
            print(result.stderr)
            return False, result.stderr
            
    except Exception as e:
        print(f"❌ Исключение: {e}")
        return False, str(e)

def main():
    """Основная функция"""
    print("🚀 Запуск комплексного тестирования WB-Irrigation")
    print(f"📅 Дата и время: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print("="*80)
    
    # Активация виртуальной среды
    if not os.path.exists('venv'):
        print("❌ Виртуальная среда не найдена!")
        return
    
    # Результаты тестов
    test_results = []
    
    # 1. Модульные тесты
    success, output = run_command(
        "source venv/bin/activate && python tools/tests/tests.py",
        "Модульные тесты (tests.py)"
    )
    test_results.append(("Модульные тесты", success, output))
    
    # 2. Веб-тесты (простые)
    success, output = run_command(
        "source venv/bin/activate && python tools/tests/web_tests_simple.py",
        "Веб-тесты (простые)"
    )
    test_results.append(("Веб-тесты (простые)", success, output))
    
    # 3. Проверяем доступность Selenium для реалистичных тестов
    try:
        from selenium import webdriver
        from webdriver_manager.chrome import ChromeDriverManager
        # 3. Веб-тесты (реалистичные)
        success, output = run_command(
            "source venv/bin/activate && python tools/tests/web_tests_realistic.py",
            "Веб-тесты (реалистичные)"
        )
        test_results.append(("Веб-тесты (реалистичные)", success, output))
        print("✅ Selenium доступен - добавлены реалистичные тесты")
    except ImportError:
        print("⚠️  Selenium недоступен - пропускаем реалистичные тесты")
        print("💡 Для установки Selenium запустите: python setup_selenium.py")
    
    # 5. Проверка синтаксиса
    success, output = run_command(
        "source venv/bin/activate && python -m py_compile app.py database.py run.py",
        "Проверка синтаксиса Python файлов"
    )
    test_results.append(("Проверка синтаксиса", success, output))
    
    # 6. Проверка импортов
    success, output = run_command(
        "source venv/bin/activate && python -c 'import app; import database; print(\"✅ Все модули импортируются корректно\")'",
        "Проверка импортов модулей"
    )
    test_results.append(("Проверка импортов", success, output))

    # 7. Загрузка тестовых изображений в живой сервер (если доступен)
    try:
        import requests
        base = os.environ.get('WB_BASE_URL', 'http://127.0.0.1:8080')
        # map image
        images_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), 'images'))
        def pick(first_names):
            for name in first_names:
                p = os.path.join(images_dir, name)
                if os.path.exists(p):
                    return p
            return None
        map_path = pick(['map.jpg','map.jpeg','map.png','map.webp','map.gif'])
        if map_path:
            with open(map_path, 'rb') as f:
                r = requests.post(f"{base}/api/map", files={'file': (os.path.basename(map_path), f)})
                print('📎 Upload map ->', r.status_code, r.text[:120])
        # zone photos 1..3
        for zid in (1,2,3):
            zp = pick([f'zone_{zid}.jpg', f'zone_{zid}.jpeg', f'zone_{zid}.png', f'zone_{zid}.webp', f'zone_{zid}.gif'])
            if not zp:
                continue
            with open(zp, 'rb') as f:
                r = requests.post(f"{base}/api/zones/{zid}/photo", files={'photo': (os.path.basename(zp), f)})
                print(f'📎 Upload zone {zid} photo ->', r.status_code)
            # quick verify
            inf = requests.get(f"{base}/api/zones/{zid}/photo")
            try:
                print('ℹ️ ', zid, inf.json())
            except Exception:
                print('ℹ️ ', zid, inf.status_code)
    except Exception as e:
        print('⚠️  Не удалось загрузить тестовые изображения на живой сервер:', e)
    
    # Создание отчета
    print(f"\n{'='*80}")
    print("📊 ОТЧЕТ О ТЕСТИРОВАНИИ")
    print(f"{'='*80}")
    
    total_tests = len(test_results)
    passed_tests = sum(1 for _, success, _ in test_results if success)
    failed_tests = total_tests - passed_tests
    
    print(f"📈 Всего тестов: {total_tests}")
    print(f"✅ Успешно: {passed_tests}")
    print(f"❌ Провалено: {failed_tests}")
    print(f"📊 Процент успеха: {(passed_tests/total_tests)*100:.1f}%")
    
    print(f"\n📋 Детальные результаты:")
    for test_name, success, output in test_results:
        status = "✅ ПРОЙДЕН" if success else "❌ ПРОВАЛЕН"
        print(f"  {status} - {test_name}")
    
    # Общий результат
    if failed_tests == 0:
        print(f"\n🎉 ВСЕ ТЕСТЫ ПРОЙДЕНЫ УСПЕШНО!")
        print("🚀 Проект готов к использованию")
        return 0
    else:
        print(f"\n⚠️  НАЙДЕНЫ ПРОБЛЕМЫ: {failed_tests} тест(ов) провалено")
        print("🔧 Рекомендуется исправить проблемы перед использованием")
        return 1

if __name__ == '__main__':
    exit_code = main()
    sys.exit(exit_code)
