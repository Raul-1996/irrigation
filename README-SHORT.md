WB-Irrigation (MVP)

Кратко
- Веб-приложение для управления поливом: зоны, группы, расписания, MQTT, аварийная остановка.
- Бэкенд: Flask + SQLite + APScheduler. MQTT (paho-mqtt).
- UI: простые страницы (Статус, Зоны/Программы, Карта зон, MQTT, Логи). Карта загружается как изображение.

Установка (локально)
1) Python 3.9+ и virtualenv.
2) Создать окружение и установить зависимости:
   python -m venv venv
   source venv/bin/activate
   pip install -r requirements.txt
3) Запуск:
   export TESTING=1  # для локального режима без MQTT
   python run.py
   Открыть http://localhost:8080

Вход/Роли
- По умолчанию пароль: 1234 (хранится как hash в БД). Роль admin.
- Кнопка «Войти» на главной. Гостевой режим: /login?guest=1 (роль user).

MQTT (опционально)
- Добавьте серверы в разделе MQTT.
- Укажите для зон topic и mqtt_server_id, чтобы управлять устройствами.

Тесты
- Запустить: PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 TESTING=1 venv/bin/python -m pytest -q


