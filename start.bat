@echo off
REM Скрипт запуска WB-Irrigation Flask приложения (Windows)
REM Использование: start.bat

echo 🚀 Запуск WB-Irrigation Flask приложения...
echo ================================================

REM Проверка наличия виртуальной среды
if not exist venv (
    echo ❌ Виртуальная среда не найдена!
    echo Сначала выполните настройку:
    echo   install.bat
    pause
    exit /b 1
)

REM Проверка наличия app.py
if not exist app.py (
    echo ❌ Файл app.py не найден!
    pause
    exit /b 1
)

REM Активация виртуальной среды
echo 🔧 Активация виртуальной среды...
call venv\Scripts\activate.bat

REM Проверка установленных зависимостей
echo 📋 Проверка зависимостей...
python -c "import flask" >nul 2>&1
if errorlevel 1 (
    echo ❌ Flask не установлен! Выполните настройку:
    echo   install.bat
    pause
    exit /b 1
)

echo ✅ Все зависимости установлены

REM Проверка порта (Windows)
echo 🔍 Проверка порта 5000...
netstat -an | findstr :5000 >nul
if not errorlevel 1 (
    echo ⚠️  Порт 5000 уже занят!
    echo Остановите другое приложение или измените порт в app.py
    pause
    exit /b 1
)

echo.
echo 🌐 Запуск веб-сервера...
echo 📱 Откройте браузер: http://localhost:5000
echo ⏹️  Для остановки нажмите Ctrl+C
echo ================================================

REM Запуск приложения
python run.py
