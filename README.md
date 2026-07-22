# WB‑Irrigation

Лёгкий веб‑интерфейс и планировщик полива для небольших/средних участков на базе Python/Flask и SQLite, с управлением по MQTT (совместимо с Wirenboard).

## 🚀 Быстрый запуск

### Требования
- **Python 3.11+** (минимум). Причины:
  - В коде используется PEP 604 union-синтаксис (`X | None`) — требует 3.10+;
  - Используется `datetime.UTC` — требует 3.11+.
  - Не пытайтесь запустить на 3.9 или 3.10 — упадёт на импортах.
- pip (менеджер пакетов Python)

На Wirenboard (Debian 11, системный `python3.9`) изолированный Python 3.11.15
ставится автоматически через `uv` — см. раздел [Wirenboard](#-wirenboard-установка-без-docker) ниже.

## 📋 Инструкции по установке и запуску

### 🐧 Linux/macOS

#### Автоматическая установка (рекомендуется)
```bash
# 1. Сделать скрипты исполняемыми
chmod +x setup.sh start.sh start_tests.sh start_mqtt.sh

# 2. Настройка виртуальной среды и установка зависимостей
./setup.sh

# 3. Запуск приложения
./start.sh

# 4. Запуск автотестов (опционально)
./start_tests.sh

# 5. Конфигурация окружения (опционально)
# Используйте переменные окружения для настройки. Примеры:
# export UI_THEME=auto   # auto | light | dark
# export TESTING=0       # 1 для режима тестов
```

#### Ручная установка
```bash
# 1. Создание виртуальной среды
python3 -m venv venv

# 2. Активация виртуальной среды
source venv/bin/activate

# 3. Обновление pip
pip install --upgrade pip

# 4. Установка зависимостей
# Прод: только runtime
pip install -r requirements.txt
# Dev/тесты: дополнительно
pip install -r requirements-dev.txt

# 5. Запуск приложения
python run.py

# 6. Запуск тестов
pytest -q
```

### 🧰 Wirenboard (установка без Docker)

Целевая платформа: Wirenboard на Debian 11/12, архитектура aarch64.
Системный Python там — 3.9.2; его не трогаем. Изолированный Python 3.11.15
ставится через hash-verified `uv 0.11.31` (Astral, prebuilt
`python-build-standalone` — без компиляции).

#### Свежий контроллер: полный bootstrap
```bash
# На устройстве Wirenboard укажите заранее проверенный commit из main.
# Короткие SHA, ветки и теги production-скрипты намеренно не принимают.
RELEASE_SHA=0123456789abcdef0123456789abcdef01234567
curl -fsSL "https://raw.githubusercontent.com/Raul-1996/irrigation/${RELEASE_SHA}/install_wb.sh" \
  -o /tmp/install_wb.sh
sudo bash /tmp/install_wb.sh --yes --branch main --commit "$RELEASE_SHA"
# По умолчанию endpoint доступен только локально на 127.0.0.1:8080.
```

Что делает `install_wb.sh`:
1. Ставит системные пакеты: `curl git build-essential libssl-dev sqlite3 mosquitto`.
2. Ставит pinned `uv 0.11.31` из immutable aarch64 archive с проверкой SHA-256
   и через него — Python 3.11.15 в общем read-only runtime
   `/mnt/data/wb-irrigation-python` (prebuilt aarch64-бинарь, ~30 секунд).
3. Проверяет, что указанный полный SHA принадлежит `origin/main`, разворачивает
   именно этот commit в `/mnt/data/wb-irrigation` и делает симлинк
   `/opt/wb-irrigation/irrigation` → `/mnt/data/wb-irrigation`
   (корневой раздел WB маленький, `/mnt/data` большой).
4. Создаёт venv на Python 3.11.15 в `venv/`, ставит только зафиксированный
   hash-locked production closure из `requirements.lock`.
5. Создаёт системного пользователя `wb-irrigation`, приватный state-каталог
   `/mnt/data/wb-irrigation-state`, устанавливает unit и стартует сервис без root.
6. Smoke check: ждёт ответа `/readyz` до 30 секунд и только после успеха
   фиксирует завершённую state-layout миграцию.

При обнаружении существующей canonical установки bootstrap до установки пакетов
передаёт выбранный SHA transactional `update_server.sh`. Для старой установки с
реальным code tree в `/opt` он сначала останавливает сервис, копирует дерево в
staging на `/mnt/data`, атомарно переключает symlink и проверяет SQLite; updater
получает stopped-service handoff. При любой ошибке updater оставляет сервис
остановленным, bootstrap восстанавливает исходный `/opt` layout и лишь затем
запускает прежний сервис. Параллельные install/update/uninstall блокируются общим lock.
Production paths и имя сервиса фиксированы unit-файлом; несовместимые override'ы
завершаются ошибкой до изменения установки.

#### Обновление существующей установки
```bash
RELEASE_SHA=0123456789abcdef0123456789abcdef01234567
sudo bash /opt/wb-irrigation/irrigation/update_server.sh \
  --yes --branch main --commit "$RELEASE_SHA"
```

Updater принимает только полный immutable SHA, проверяет его принадлежность
`origin/main`, делает верифицированный backup на data-разделе и откатывает
code, venv, state, базы, unit и env при неуспешной активации. Первый update
старой root-установки копирует DB/jobs/ключи/backup/media в staging, проверяет
SQLite и лишь затем переключает unit на пользователя `wb-irrigation`. Старые
runtime-файлы убираются из code tree только после успешного `/readyz`; при
ошибке они восстанавливаются, а неуспешный state сохраняется с суффиксом
`.failed-*` для диагностики.

GitHub Actions deploy требует тот же SHA как ручной input, использует защищённое
environment `production` и отказывается подключаться без secrets
`WB_SSH_FINGERPRINT` и `JUMP_SSH_FINGERPRINT`. В настройках репозитория для
environment `production` должны быть включены required reviewers.

Unit сначала загружает мигрированный `/mnt/data/wb-irrigation-state/.env`, затем
`/opt/wb-irrigation/.env`; внешний файл имеет приоритет и остаётся основным
местом для администраторских override'ов. Install/update добавляют туда
безопасный bind, только если ключ ещё отсутствует:

```dotenv
WB_HTTP_BIND_HOST=127.0.0.1
```

Для внешнего bind нужно задать оба файла native TLS:

```dotenv
WB_HTTP_BIND_HOST=0.0.0.0
WB_HTTP_TLS_CERTFILE=/etc/wb-irrigation/tls.crt
WB_HTTP_TLS_KEYFILE=/etc/wb-irrigation/tls.key
```

При native TLS runtime принудительно включает secure cookie, а deploy smoke
проверяет `/readyz` по адресу, выведенному из bind host. Для wildcard bind
используются `127.0.0.1`/`::1`. Если TLS certificate выписан на DNS-имя,
задайте `WB_HTTP_PROBE_HOST=wb.local`. По умолчанию curl использует системное
хранилище CA; отдельный обычный non-symlink CA-файл задаётся через
`WB_HTTP_PROBE_CA_FILE`. Только для readiness probe можно явно отключить проверку
сертификата с `WB_HTTP_PROBE_INSECURE_TLS=1` — deploy выведет предупреждение.
Временная legacy-схема без TLS возможна только с явным
`WB_HTTP_ALLOW_INSECURE_EXTERNAL=1`; без неё внешний plaintext bind завершается
ошибкой. Пока bind остаётся безопасным значением по умолчанию `127.0.0.1`,
installer намеренно не рекламирует LAN URL.

#### Удаление
```bash
sudo bash /opt/wb-irrigation/irrigation/uninstall_wb.sh --yes
```

По умолчанию удаляются service unit и logrotate-конфиг, а код, `.env`, state,
базы, backup'ы и общий Python runtime явно сохраняются. Необратимая очистка
всех этих данных и dedicated service account требует отдельной опции (в
интерактивном режиме скрипт дополнительно попросит подтверждение):

```bash
sudo bash /opt/wb-irrigation/irrigation/uninstall_wb.sh --yes --purge-data
```

Примечания:
- На WB уровень логов по умолчанию WARNING; включить подробные логи можно через Настройки.
- Для HTTPS куки выставьте `SESSION_COOKIE_SECURE=1` в окружении сервиса.
- Docker на Wirenboard **не используется**. Скрипты `install_docker.sh` /
  `update_docker.sh` были удалены как мёртвый код.

Production unit запускается как `wb-irrigation:wb-irrigation`. Относительные
DB/jobs/keys/backups живут в `/mnt/data/wb-irrigation-state`; maps/zones
подключены writable bind-mount'ами из state в read-only code tree. Код, venv и
общий Python принадлежат root и недоступны сервису для записи. Unit дополнительно
использует `ProtectSystem=strict`, `NoNewPrivileges`, приватный `/tmp` и защиту
system/kernel деревьев.

### 🪟 Windows

#### Автоматическая установка (рекомендуется)
```cmd
# 1. Настройка виртуальной среды и установка зависимостей
install.bat

# 2. Запуск приложения
start.bat

# 3. Запуск автотестов (опционально)
pytest -q
```

#### Ручная установка
```cmd
# 1. Создание виртуальной среды
python -m venv venv

# 2. Активация виртуальной среды
venv\Scripts\activate.bat

# 3. Обновление pip
python -m pip install --upgrade pip

# 4. Установка зависимостей
# Прод: только runtime
pip install -r requirements.txt
# Dev/тесты: дополнительно
pip install -r requirements-dev.txt

# 5. Запуск приложения
python run.py

# 6. Запуск тестов
pytest -q
```

### 🌐 Доступ к приложению
После запуска откройте браузер и перейдите по адресу:
- **http://localhost:8080** - главная страница (Статус)

## 🆕 Новые функции

### 💾 База данных SQLite
- **Автоматическое создание** таблиц при первом запуске
- **Резервное копирование** каждые 7 дней с автоматической очисткой
- **Целостность данных** с проверкой связей между таблицами
- **Логирование** всех операций для аудита

### ⏰ Отложенный полив
- **Кнопки +1/+2/+3 дня** для отложки полива групп
- **Автоматическое суммирование** дней при повторном нажатии
- **Кнопка отмены** "Отменить отложенный полив"
- **Визуальная индикация** статуса отложенного полива
- **Кнопки всегда видны** для удобства управления

### 🏷️ Управление группами
- **Динамическое отображение** групп только с зонами
- **Редактирование названий** групп через веб-интерфейс
- **Переименование страницы** в "Зоны и группы"
- **Счетчик зон** в каждой группе

### 🧪 Автотестирование
- **Полный набор тестов** для всех функций
- **Тестирование API** эндпоинтов
- **Тестирование базы данных** операций
- **Проверка целостности** данных
- **Обработка ошибок** и граничных случаев

### 🎨 Улучшенный UI/UX
- **Легенда цветов** для статусов групп
- **Индикатор связи с сервером** (красное уведомление при потере связи)
- **Темная тема** (auto/light/dark через переменную окружения `UI_THEME`)
- **Улучшенные уведомления** с анимациями и иконками
- **Адаптивный дизайн** для всех устройств
- **Анимации и переходы** для лучшего UX
- **Favicon** с иконкой капли воды
- **Sticky header** для удобной навигации

### 🔧 Технические улучшения
- **CSS переменные** для легкой кастомизации
- **Service Worker** для офлайн поддержки (если `static/sw.js` доступен)
- **Performance monitoring** с уведомлениями о медленной загрузке
- **Global error handling** с автоматическими уведомлениями
- **Accessibility** поддержка (focus styles, reduced motion)
- **Print styles** для печати
- **SEO оптимизация** с мета-тегами

## 📱 Страницы приложения

### 🏠 Статус (`/`)
- Обзор состояния всех групп и зон
- Быстрые действия (отложить, запустить, остановить)
- Аварийная остановка
- Таблица всех зон с индикаторами состояния
- Автообновление каждые 30 секунд
- **Новое**: Отложенный полив с кнопками отмены

### 🎯 Зоны и группы (`/zones`)
- **Новое**: Управление группами с редактированием названий
- Список всех зон с возможностью редактирования
- Создание новых зон через модальное окно
- Массовые изменения выбранных зон
- Инлайн-редактирование полей
- Удаление зон с подтверждением
- **Новое**: Счетчик зон в каждой группе

### 📅 Программы (`/programs`)
- Список программ полива
- Мастер создания/редактирования (2 шага)
- Выбор зон по группам
- Настройка времени и дней недели
- Удаление программ

### 📊 Логи (`/logs`)
- Журнал всех событий системы
- Фильтрация по датам и типу событий
- Экспорт в CSV с защитой от формульной инъекции
- Цветовая индикация типов событий

### 🗺️ Карта (`/map`)
- Карта участка с расположением зон

### 📡 MQTT (`/mqtt`)
- Управление MQTT-серверами (создание, проверка соединения, сканирование топиков)

### ⚙️ Настройки (`/settings`)
- Системные настройки, Telegram-уведомления, уровень логов

## 🔧 API эндпоинты (основные)

### Зоны
- `GET /api/zones` - список всех зон
- `GET /api/zones/<id>` - получить зону
- `POST /api/zones` - создать зону
- `PUT /api/zones/<id>` - обновить зону
- `DELETE /api/zones/<id>` - удалить зону

### Группы
- `GET /api/groups` - список групп с количеством зон
- `PUT /api/groups/<id>` - обновить название группы

### Программы
- `GET /api/programs` - список программ
- `GET /api/programs/<id>` - получить программу
- `POST /api/programs` - создать программу
- `PUT /api/programs/<id>` - обновить программу
- `DELETE /api/programs/<id>` - удалить программу

### Логи
- `GET /api/logs` - список логов
- `GET /api/logs?type=zone_on&from=2024-01-01&to=2024-01-31` - фильтрация

### Расход воды
- `GET /api/water?days=7&zone=all` - данные расхода

### Статус
- `GET /api/status` - текущий статус системы

### Отложенный полив
- `POST /api/postpone` - отложить/отменить полив группы

### MQTT

- `GET /api/mqtt/servers` — список серверов
- `POST /api/mqtt/servers` — создать сервер `{name, host, port, username?, password?, enabled?}`
- `PUT /api/mqtt/servers/<id>` — обновить сервер
- `DELETE /api/mqtt/servers/<id>` — удалить сервер
- `GET /api/mqtt/<id>/status` — быстрая проверка TCP-соединения
- `POST /api/mqtt/<id>/probe` — короткая подписка (filter, duration секунд), возвращает события и сообщения
- `GET /api/mqtt/<id>/scan-sse?filter=/devices/#` — потоковое сканирование (SSE)

Коды ошибок в ответах: `error_code` (например: `MQTT_SERVER_NOT_FOUND`, `MQTT_LIB_MISSING`, `MQTT_CONNECT_FAILED`).

### Резервное копирование
- `POST /api/backup` - создать резервную копию БД

### 🔐 Аутентификация
- Простая авторизация по паролю (сессии). По умолчанию пароль: `1234`.
- Изменение пароля: `POST /api/password` с телом `{old_password, new_password}`.
- Статус авторизации: `GET /api/auth/status`.
- Страницы UI требуют входа; API остаётся открытым (можно закрыть по необходимости).

### ⏱ Планировщик (APScheduler)
- Используется APScheduler (BackgroundScheduler) вместо schedule.
- Формат дней недели единый: 0–6, где 0=Понедельник.
- При создании/редактировании/удалении программы задачи планировщика автоматически пересоздаются.

## 📡 Управляющие MQTT-топики зон (WB)

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

## 🛠️ Структура проекта

```
wb-irrigation/
├── app.py                   # Основное Flask приложение (фабрика, auth, SSE)
├── run.py                   # Скрипт запуска (Hypercorn; порт через PORT, по умолчанию 8080)
├── config.py                # Конфигурация
├── constants.py             # Константы
├── database.py              # Фасад работы с SQLite БД
├── db/                      # Модули БД (zones, groups, programs, mqtt, ...)
├── routes/                  # Flask blueprints: страницы и API
├── services/                # Сервисы (MQTT, погода, Telegram, события, ...)
├── irrigation_scheduler.py  # Планировщик полива (APScheduler)
├── scheduler/               # Вспомогательные модули планировщика
├── migrations/              # Миграции БД
├── templates/               # HTML шаблоны (status, zones, programs, logs, map, mqtt, settings)
├── static/                  # Статические файлы
│   ├── js/, css/, icons/   # Фронтенд
│   ├── media/               # Фото/карты (в .gitignore, остаются .gitkeep)
│   └── photos/              # Фото зон (в .gitignore, .gitkeep)
├── tests/                   # Автотесты (pytest)
├── configs/                 # Примеры конфигов (nginx и т.п.)
├── scripts/, tools/         # Вспомогательные скрипты
├── requirements.txt         # Исходный список зависимостей
├── requirements.lock        # Точный production closure для install/update
├── requirements-dev.txt     # Зависимости для разработки/тестов
├── wb-irrigation.service    # systemd unit для Wirenboard
├── install_wb.sh            # Установка на Wirenboard
├── update_server.sh         # Обновление установки на Wirenboard
├── uninstall_wb.sh          # Удаление с Wirenboard
├── setup.sh / start.sh / start_tests.sh / start_mqtt.sh  # Linux/macOS
├── install.bat / start.bat  # Windows
├── venv/                    # Виртуальная среда (создается автоматически)
├── irrigation.db            # SQLite база данных (создается автоматически, в .gitignore)
└── backups/                 # Резервные копии БД (создается автоматически, в .gitignore)
```

## 🎨 Особенности UI/UX

### Современный дизайн
- Адаптивная верстка (мобильные устройства)
- Material Design цвета и тени
- Плавные анимации и переходы
- Интуитивная навигация

### Интерактивность
- Уведомления о действиях пользователя
- Подтверждения для критических операций
- Автообновление данных
- Загрузочные состояния

### Безопасность
- Защита от XSS (безопасный рендер данных)
- Защита от формульной инъекции в CSV
- Валидация данных на клиенте и сервере
- Логирование всех операций

## 🧪 Автотестирование

### Запуск тестов
```bash
# Автоматический запуск
./start_tests.sh

# Ручной запуск
pytest -q
```

### Покрытие тестами
- ✅ **База данных**: инициализация, CRUD операции, резервное копирование
- ✅ **API эндпоинты**: все GET, POST, PUT, DELETE операции
- ✅ **Отложенный полив**: установка, отмена, валидация
- ✅ **Целостность данных**: проверка связей между таблицами
- ✅ **Обработка ошибок**: некорректные запросы, несуществующие ресурсы
- ✅ **Логирование**: добавление и фильтрация логов

## 🔮 Планы развития

Реализовано: Flask API и UI, SQLite с резервным копированием, MQTT-управление
(Wirenboard), планировщик (APScheduler), аутентификация, live-обновления (SSE),
интеграция с погодой, Telegram-уведомления.

### Долгосрочные
- [ ] Мобильное приложение
- [ ] Машинное обучение для оптимизации полива
- [ ] Масштабирование на несколько участков

## 🐛 Отладка

### Проблемы с виртуальной средой
```bash
# Удалить и пересоздать виртуальную среду
rm -rf venv
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

### Проблемы с базой данных
```bash
# Удалить и пересоздать БД
rm irrigation.db
python run.py  # БД создастся автоматически
```

### Логи Flask
```bash
# Включить подробные логи
export FLASK_DEBUG=1
python app.py
```

### Проверка API
```bash
# Тест API эндпоинтов
curl http://localhost:8080/api/zones
curl http://localhost:8080/api/status
```

### Проблемы с зависимостями
```bash
# Обновить pip
python -m pip install --upgrade pip

# Переустановить зависимости
pip uninstall -r requirements.txt
pip install -r requirements.txt
```

### Проблемы с портом
```bash
# Проверить, что занят порт 8080
lsof -i :8080

# Запустить на другом порту
PORT=8081 python run.py
```

### Запуск тестов
```bash
# Запуск всех тестов
./start_tests.sh

# Запуск конкретного файла тестов
pytest tests/unit/test_scheduler_v2.py

# Подробный вывод
pytest -v
```

## 🔄 Перенос и деплой

### Подготовка к переносу
1. **Убедитесь, что приложение работает в виртуальной среде**
2. **Запустите автотесты**: `./start_tests.sh`
3. **Скопируйте проект на целевой сервер (или Wiren Board)**
4. **На Wiren Board выполните те же команды установки**

### На Wiren Board
Установка и автозапуск на WB описаны выше — см. [Wirenboard (установка без Docker)](#-wirenboard-установка-без-docker).
Systemd unit — единственный источник правды: файл [`wb-irrigation.service`](wb-irrigation.service) в корне репо; `install_wb.sh` копирует его в `/etc/systemd/system/`.

## 📄 Лицензия

MIT License - свободное использование и модификация.

## 🤝 Вклад в проект

1. Форкните репозиторий
2. Создайте ветку для новой функции
3. Внесите изменения
4. Запустите тесты: `./start_tests.sh`
5. Создайте Pull Request

## 📞 Поддержка

При возникновении проблем:
1. Проверьте логи в консоли
2. Убедитесь, что виртуальная среда активирована
3. Проверьте, что все зависимости установлены
4. Запустите автотесты: `./start_tests.sh`
5. Проверьте, что порт 8080 свободен
6. Создайте Issue с описанием проблемы

## Testing

### All tests

```bash
source venv/bin/activate
pytest -q
```

Маркеры `e2e` и `mqtt_real` исключены из дефолтного прогона (см. `addopts` в `pytest.ini`).

### E2E и тесты с реальным брокером (opt-in)

```bash
# E2E против живого контроллера
WB_E2E_URL=http://<controller>:8080 WB_E2E_PASSWORD=<password> pytest -m e2e

# Интеграция с реальным MQTT-брокером
WB_MQTT_HOST=<broker> pytest -m mqtt_real
```
