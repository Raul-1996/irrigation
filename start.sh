#!/bin/bash

# Скрипт запуска WB-Irrigation Flask приложения
# Использование: ./start.sh

set -e  # Остановка при ошибке

echo "🚀 Запуск WB-Irrigation Flask приложения..."
echo "================================================"

# Проверка наличия виртуальной среды
if [ ! -d "venv" ]; then
    echo "❌ Виртуальная среда не найдена!"
    echo "Сначала выполните настройку:"
    echo "  ./setup.sh"
    exit 1
fi

# Проверка наличия app.py
if [ ! -f "app.py" ]; then
    echo "❌ Файл app.py не найден!"
    exit 1
fi

# Активация виртуальной среды
echo "🔧 Активация виртуальной среды..."
source venv/bin/activate

# Проверка установленных зависимостей
echo "📋 Проверка зависимостей..."
if ! python -c "import flask" 2>/dev/null; then
    echo "❌ Flask не установлен! Выполните настройку:"
    echo "  ./setup.sh"
    exit 1
fi

echo "✅ Все зависимости установлены"

# Проверка порта
PORT=8080
if lsof -Pi :$PORT -sTCP:LISTEN -t >/dev/null 2>&1; then
    echo "⚠️  Порт $PORT уже занят!"
    echo "Остановите другое приложение или измените порт в app.py"
    exit 1
fi

echo ""
echo "🌐 Запуск веб-сервера..."
echo "📱 Откройте браузер: http://localhost:$PORT"
echo "⏹️  Для остановки нажмите Ctrl+C"
echo "================================================"

# Запуск приложения
python run.py
