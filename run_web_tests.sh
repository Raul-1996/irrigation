#!/bin/bash

echo "🧪 Запуск веб-тестов WB-Irrigation..."
echo "=================================================="

# Проверяем, что виртуальная среда активирована
if [[ "$VIRTUAL_ENV" == "" ]]; then
    echo "❌ Виртуальная среда не активирована"
    echo "Запустите: source venv/bin/activate"
    exit 1
fi

# Устанавливаем зависимости для веб-тестирования
echo "📦 Установка зависимостей для веб-тестирования..."
pip install selenium==4.15.2 webdriver-manager==4.0.1 pytest==7.4.3 pytest-selenium==4.0.1

# Проверяем, что Chrome установлен
if ! command -v google-chrome &> /dev/null && ! command -v chromium-browser &> /dev/null; then
    echo "⚠️  Chrome не найден. Установите Chrome для запуска веб-тестов."
    echo "На macOS: brew install --cask google-chrome"
    echo "На Ubuntu: sudo apt install chromium-browser"
    exit 1
fi

# Останавливаем все запущенные процессы Flask
echo "🛑 Остановка существующих процессов Flask..."
pkill -f "python run.py" 2>/dev/null || true
sleep 2

# Запускаем веб-тесты
echo "🚀 Запуск веб-тестов..."
echo "=================================================="

python web_tests.py

# Проверяем результат
if [ $? -eq 0 ]; then
    echo "=================================================="
    echo "✅ Все веб-тесты прошли успешно!"
else
    echo "=================================================="
    echo "❌ Некоторые веб-тесты не прошли"
    exit 1
fi
