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

# Проверка и освобождение порта
PORT=8080
PIDS=$(lsof -ti :$PORT || true)
if [ -n "$PIDS" ]; then
    echo "⚠️  Порт $PORT занят процессами: $PIDS"
    echo "🔪 Завершаю процессы на порту $PORT..."
    kill -9 $PIDS || true
    sleep 1
fi

echo ""
echo "🌐 Запуск веб-сервера..."
echo "📱 Откройте браузер: http://localhost:$PORT"
echo "⏹️  Для остановки нажмите Ctrl+C"
echo "================================================"

# Запуск приложения
python run.py
