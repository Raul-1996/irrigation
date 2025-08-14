#!/usr/bin/env python3
"""
Скрипт для запуска WB-Irrigation Flask приложения
"""

import os
import sys
from app import app

if __name__ == '__main__':
    # Проверяем наличие необходимых файлов
    if not os.path.exists('app.py'):
        print("Ошибка: файл app.py не найден!")
        sys.exit(1)
    
    if not os.path.exists('templates'):
        print("Ошибка: папка templates не найдена!")
        sys.exit(1)
    
    print("🚀 Запуск WB-Irrigation...")
    print("📱 Откройте браузер и перейдите по адресу: http://localhost:8080")
    print("⏹️  Для остановки нажмите Ctrl+C")
    print("-" * 50)
    
    try:
        app.run(
            debug=True,
            host='0.0.0.0',
            port=8080,
            use_reloader=True
        )
    except KeyboardInterrupt:
        print("\n👋 Приложение остановлено")
    except Exception as e:
        print(f"❌ Ошибка запуска: {e}")
        sys.exit(1)
