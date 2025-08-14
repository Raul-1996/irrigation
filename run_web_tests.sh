#!/bin/bash

echo "🧪 Запуск веб-тестов WB-Irrigation..."
echo "=================================================="

# Автоактивация venv при необходимости
if [[ "$VIRTUAL_ENV" == "" ]]; then
    if [[ -f "venv/bin/activate" ]]; then
        source venv/bin/activate
    fi
fi

# Устанавливаем зависимости для веб-тестирования
echo "📦 Установка зависимостей для веб-тестирования..."
pip install selenium==4.15.2 webdriver-manager==4.0.1 pytest==7.4.3 pytest-selenium==4.0.1

# Настраиваем docker selenium по умолчанию, если есть переменная
export BROWSER=${BROWSER:-chrome}
export SELENIUM_REMOTE_URL=${SELENIUM_REMOTE_URL:-http://localhost:4444/wd/hub}

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
