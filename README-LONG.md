WB-Irrigation — подробное описание (MVP)

Обзор
- Назначение: управление поливом через веб-интерфейс, интеграция с MQTT-реле, расписания, аварийные режимы.
- Технологии: Flask (REST + HTML), SQLite (без ORM), APScheduler (планировщик), paho-mqtt (интеграция), Flask-WTF CSRF.

Архитектура
- app.py: Flask-приложение, API-эндпойнты, регистрация страниц (Blueprints), интеграция с планировщиком, обработка MQTT.
- database.py: слой работы с SQLite, миграции (idempotent), CRUD зон/групп/программ/логов/MQTT-серверов, расчёт расписаний.
- irrigation_scheduler.py: APScheduler-планировщик (последовательный запуск зон, автостоп, отмены, раннее выключение).
- routes/: HTML-страницы (status, zones, programs, logs, mqtt, map) с ролевой защитой (services/security.py).
- templates/: Jinja2-шаблоны; base.html содержит helpers (api.*, уведомления) и регистрацию SW.
- static/: sw.js (кэширование), media/maps (карта), media/zones (фото зон).

Основные взаимодействия
1) Зоны и группы
   - CRUD зон /api/zones, группы /api/groups.
   - Ручной старт/стоп зоны: /api/zones/<id>/start|stop и MQTT-варианты /mqtt/start|stop.
   - Эксклюзивность в группе обеспечивается на уровне API и watchdog.

2) Программы
   - CRUD /api/programs.
   - Серверно проверяются конфликты времени/зон/групп /api/programs/check-conflicts.
   - Планировщик строит последовательные старты зон и автостопы.

3) Планировщик
   - Инициализация /api/scheduler/init, статус /api/scheduler/status.
   - Раннее выключение (early-off) настраивается /api/settings/early-off (0..15 сек).

4) MQTT
   - CRUD серверов /api/mqtt/servers.
   - Быстрый статус /api/mqtt/<id>/status, короткая подписка-проба /api/mqtt/<id>/probe.
   - SSE-поток статусов зон /api/mqtt/zones-sse (обновляет UI мгновенно).

5) Карта зон
   - HTML: /map. API: GET/POST /api/map.
   - POST очищает старые карты и сохраняет новый файл в static/media/maps/.
   - GET отдаёт путь последней валидной карты.

6) Фото зон
   - POST/DELETE/GET /api/zones/<id>/photo.

7) Аварийные режимы
   - POST /api/emergency-stop — OFF всем зонам + флаг EMERGENCY_STOP.
   - POST /api/emergency-resume — снимает флаг.
   - POST /api/postpone — отложка полива группы, cancel — отмена отложки.

Аутентификация и роли
- По умолчанию пароль 1234 (hash в таблице settings). Успешный вход — роль admin.
- Гостевой просмотр: /login?guest=1 (роль user) — доступ к статусу/карте, но не к админ-разделам.
- Смена пароля: /api/password (POST old/new).

Установка и запуск
1) Требования: Python 3.9+, virtualenv. MQTT брокер (опционально).
2) Установка зависимостей:
   python -m venv venv
   source venv/bin/activate
   pip install -r requirements.txt
3) Запуск:
   export TESTING=1  # для локальной отладки без MQTT
   python run.py
   Открыть http://localhost:8080

Настройка MQTT
- В разделе MQTT создайте сервер (host/port/user/pass/client_id).
- Для каждой зоны задайте topic и mqtt_server_id.
- Формат топиков свободный (например /devices/wb-mr6cv3_101/controls/K1).

База данных
- SQLite файл irrigation.db, режим WAL включается автоматически.
- Миграции выполняются при старте (ALTER TABLE IF NOT EXISTS и CREATE INDEX IF NOT EXISTS).
- Бэкап: POST /api/backup (копия файла и чистка старых).

Тесты
- Запуск: PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 TESTING=1 venv/bin/python -m pytest -q
- Тесты покрывают основные API, MQTT (по возможности), фото, отложку, таймеры.

Рекомендации по стабильности (MVP)
- Оставить включённым TESTING=1 в демо без реального MQTT — уменьшает тайминги и отключает лишние задержки.
- Для прод-запуска отключить debug и TESTING, настроить один надёжный брокер MQTT, проверить ping/TTL.
- Использовать systemd/pm2/докер для перезапуска сервиса при сбоях.
- Регулярные бэкапы БД (cron на /api/backup). Храните не менее 7 копий.
- Логи — ротация уже включена (backups/app.log). Отдельный лог импорт/экспорт.
- Мониторить нагрузку на SSE: ограничить число одновременных клиентов (Nginx proxy+timeouts).

Дорожная карта после MVP
- UI: вынести «Настройки» (early-off, rain-toggle) в отдельный экран.
- Alembic для версионирования миграций (даже с SQLite).
- Роли и пользователи (multi-user), аудит действий.
- Ретеншн логов и water_usage, реальные счётчики воды.
- Интеграционные тесты с реальным брокером в CI (фича-флаги).

## Управляющие MQTT-топики зон (WB)

Для максимальной совместимости с Wirenboard контроллерами публикация команд выполняется одновременно в два топика для каждой зоны:

- Базовый топик: `/devices/wb-mr6c_101/controls/Kx`
- Дополнительный управляющий топик: `/devices/wb-mr6c_101/controls/Kx/on`

Это справедливо как для включения (ON), так и для выключения (OFF). Клиент публикует одинаковое значение в оба топика:

- Включение зоны: значение `"1"`
- Выключение зоны: значение `"0"`

Пример для зоны K1:

- ON: публикуем `1` в `/devices/wb-mr6c_101/controls/K1` и `/devices/wb-mr6c_101/controls/K1/on`
- OFF: публикуем `0` в `/devices/wb-mr6c_101/controls/K1` и `/devices/wb-mr6c_101/controls/K1/on`

Дублирование публикаций реализовано в модуле `services/mqtt_pub.py` функцией `publish_mqtt_value(...)` и используется всеми местами, где выполняется управление зонами. Функция имеет анти-дребезг по топику и минимизирует лишние повторные отправки.


