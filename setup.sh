#!/bin/bash

# Скрипт настройки виртуальной среды для WB-Irrigation
# Использование: ./setup.sh

set -e  # Остановка при ошибке

echo "🚀 Настройка WB-Irrigation Flask приложения..."
echo "================================================"

# Проверка наличия Python
if ! command -v python3 &> /dev/null; then
    echo "❌ Python 3 не найден! Установите Python 3.8+"
    exit 1
fi

PYTHON_VERSION=$(python3 --version | cut -d' ' -f2 | cut -d'.' -f1,2)
echo "✅ Python версия: $PYTHON_VERSION"

# Проверка версии Python
if [[ $(echo "$PYTHON_VERSION >= 3.8" | bc -l) -eq 0 ]]; then
    echo "❌ Требуется Python 3.8 или выше!"
    exit 1
fi

# Создание виртуальной среды
echo "📦 Создание виртуальной среды..."
if [ -d "venv" ]; then
    echo "⚠️  Виртуальная среда уже существует. Удаляем..."
    rm -rf venv
fi

python3 -m venv venv
echo "✅ Виртуальная среда создана"

# Активация виртуальной среды
echo "🔧 Активация виртуальной среды..."
source venv/bin/activate

# Обновление pip
echo "⬆️  Обновление pip..."
pip install --upgrade pip

# Установка зависимостей
echo "📚 Установка зависимостей..."
pip install -r requirements.txt

echo ""
echo "🎉 Настройка завершена!"
echo "================================================"
echo "Для запуска приложения выполните:"
echo "  source venv/bin/activate"
echo "  python run.py"
echo ""
echo "Или используйте скрипт запуска:"
echo "  ./start.sh"
echo ""
echo "Для остановки: Ctrl+C"
echo "Для деактивации виртуальной среды: deactivate"
