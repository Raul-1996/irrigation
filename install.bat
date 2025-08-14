@echo off
REM Скрипт настройки виртуальной среды для WB-Irrigation (Windows)
REM Использование: install.bat

echo 🚀 Настройка WB-Irrigation Flask приложения...
echo ================================================

REM Проверка наличия Python
python --version >nul 2>&1
if errorlevel 1 (
    echo ❌ Python не найден! Установите Python 3.8+
    pause
    exit /b 1
)

echo ✅ Python найден

REM Создание виртуальной среды
echo 📦 Создание виртуальной среды...
if exist venv (
    echo ⚠️  Виртуальная среда уже существует. Удаляем...
    rmdir /s /q venv
)

python -m venv venv
if errorlevel 1 (
    echo ❌ Ошибка создания виртуальной среды!
    pause
    exit /b 1
)
echo ✅ Виртуальная среда создана

REM Активация виртуальной среды
echo 🔧 Активация виртуальной среды...
call venv\Scripts\activate.bat

REM Обновление pip
echo ⬆️  Обновление pip...
python -m pip install --upgrade pip

REM Установка зависимостей
echo 📚 Установка зависимостей...
pip install -r requirements.txt
if errorlevel 1 (
    echo ❌ Ошибка установки зависимостей!
    pause
    exit /b 1
)

echo.
echo 🎉 Настройка завершена!
echo ================================================
echo Для запуска приложения выполните:
echo   venv\Scripts\activate.bat
echo   python run.py
echo.
echo Или используйте скрипт запуска:
echo   start.bat
echo.
echo Для остановки: Ctrl+C
echo Для деактивации виртуальной среды: deactivate
pause
