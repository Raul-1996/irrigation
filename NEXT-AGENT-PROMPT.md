# Оркестратор: Финальная верификация и деплой wb-irrigation v2.0

## РОЛЬ
Ты — агент-оркестратор. Управляешь финальными этапами: тесты → деплой на контроллер → полное тестирование на реальном железе.
Каждый крупный этап = отдельный саб-агент (`sessions_spawn`, `model="anthropic/claude-opus-4-6"`).

---

## КОНТЕКСТ ПРОЕКТА

### Что было сделано (предыдущая сессия)
Полный цикл аудита и рефакторинга проекта wb-irrigation:
- **35/37 задач выполнены** за 6 этапов пайплайна
- **Ветка:** `refactor/v2` (37 коммитов поверх main)
- **НЕ мержить в main** до полной верификации на контроллере

### Ключевые изменения (refactor/v2 vs main)
| Метрика | main (до) | refactor/v2 (после) |
|---------|-----------|---------------------|
| app.py | 4411 строк | 361 строк (−92%) |
| database.py | 2359 строк | 306 строк фасад + 9 модулей в db/ |
| Модули Python | ~5 | 52 файла |
| Security | CRITICAL | B+ (все 8 уязвимостей закрыты) |
| catch-all except | 356+ без лога | 742 except, все с логированием |
| MQTT QoS | 0 | 2 + retain |
| SECRET_KEY | hardcoded `wb-irrigation-secret` | auto-generated |
| MQTT auth | anonymous | username + password + ACL |
| CSRF | disabled | enabled |
| Guest | full control | viewer (read-only) |
| Password | 1234 | random + force change |
| Docker | root | appuser |

### Новая архитектура
```
wb-irrigation/
├── app.py              # 361 строк — Flask core, middleware, blueprint registration
├── config.py           # Config + auto-generated SECRET_KEY
├── constants.py        # Все магические числа
├── database.py         # 306 строк — фасад (proxy → db/)
├── db/                 # Repository pattern (9 модулей)
│   ├── base.py, zones.py, programs.py, groups.py
│   ├── mqtt.py, settings.py, telegram.py, logs.py, migrations.py
├── routes/             # Blueprints (12 файлов)
│   ├── zones_api.py, groups_api.py, programs_api.py
│   ├── mqtt_api.py, system_api.py, auth.py, ...
├── services/           # Business logic (16 файлов)
│   ├── zone_control.py   # QoS 2 + retain
│   ├── mqtt_pub.py       # Retry + graceful shutdown
│   ├── observed_state.py # Верификация реле
│   ├── sse_hub.py        # SSE real-time
│   ├── rate_limiter.py   # IP-based brute-force protection
│   ├── watchdog.py       # Cap-time enforcement
│   ├── monitors.py, app_init.py, helpers.py, ...
├── mosquitto/          # Auth config (passwd, acl, setup_auth.sh)
├── migrations/         # reencrypt_secrets.py
├── VERSION             # 2.0.0
└── .env.example        # Все переменные задокументированы
```

### Репозиторий
- **GitHub:** `Raul-1996/irrigation`
- **Ветка для работы:** `refactor/v2`
- **Клон:** `/workspace/wb-irrigation/` (ветка refactor/v2)

### Доступ к GitHub
```bash
export GITHUB_TOKEN=$(python3 -c "import json; print(json.load(open('/config/secrets/services.json'))['github']['token'])")
```

### SSH доступ
- **Docker-хост (rauls-ubunte):** `ssh -i /config/secrets/ssh/botops_key botops@172.30.0.1`
- **Контроллер WB-8 (.244):** через jump host:
  ```bash
  ssh -i /config/secrets/ssh/botops_key botops@172.30.0.1
  # затем:
  sshpass -p '123_grom' ssh root@10.2.5.244
  ```
- **⚠️ НЕ ТРОГАТЬ** контейнер `b24-migrator` и другие контейнеры на Docker-хосте!

### Контроллер WB-8 (.244)
- **IP:** 10.2.5.244
- **OS:** Wirenboard (Debian-based ARM64)
- **Назначение:** Тех. помещения — управление поливом
- **Root пароль:** 123_grom
- **Текущая версия:** старый wb-irrigation в /opt/wb-irrigation/irrigation/
- **Порт приложения:** 8080
- **Реле (RS485-2):**
  - Зоны 1–6: wb-mr6cv3_85 K1–K6
  - Зоны 7–12: wb-mr6cv3_87 K1–K6
  - Зоны 13–18: wb-mr6cv3_70 K1–K6
  - Зоны 19–24: wb-mr6cv3_122 K1–K6
  - Топики: `/devices/wb-mr6cv3_{addr}/controls/K{N}`
  - **К реле пока ничего не подключено** — безопасно тестировать ON/OFF

### Отчёты предыдущей сессии
- `/workspace/wb-irrigation/reports/stage1-testing.md`
- `/workspace/wb-irrigation/reports/stage2-code-review.md`
- `/workspace/wb-irrigation/reports/stage3-security-audit.md`
- `/workspace/wb-irrigation/reports/stage6-code-review-v1.md`
- `/workspace/wb-irrigation/reports/stage6-security-audit-v1.md`
- `/workspace/wb-irrigation/reports/final-summary.md`
- `/workspace/wb-irrigation/specs/wb-irrigation/spec.md`
- `/workspace/wb-irrigation/specs/wb-irrigation/plan.md`
- `/workspace/wb-irrigation/specs/wb-irrigation/tasks.md`

### Что не завершено
1. **TASK-012 (тесты):** Саб-агент `irrigation-fix-tests` работал над починкой тестовой инфраструктуры — conftest.py, зависающие тесты. Проверь результат: есть ли коммит, обновлён ли отчёт.
2. **TASK-034 (dataclasses):** Пропущен как nice-to-have.
3. **TASK-037 (CI/CD):** .github/workflows/ci.yml создан, но push заблокирован (PAT без workflow scope). Файл мог быть удалён при верификации.

---

## ПАЙПЛАЙН

### Этап A: Проверить и завершить тесты

**Задача для саб-агента:**
1. `cd /workspace/wb-irrigation && git pull origin refactor/v2`
2. Проверить: был ли коммит от irrigation-fix-tests. `git log --oneline -5`
3. Запустить тесты:
   ```bash
   cd /workspace/wb-irrigation
   pip install -r requirements.txt pytest pytest-timeout -q 2>/dev/null
   TESTING=1 python3 -m pytest tools/tests/tests_pytest/ --timeout=10 -q --tb=short 2>&1 | tail -30
   ```
4. **Если тесты зависают:** проблема в conftest.py (поднимает реальный сервер). Решение:
   - Переписать conftest.py: использовать `app.test_client()` вместо werkzeug сервера
   - Mock MQTT и SSE
   - Глобальный timeout 10с в pytest.ini
5. **Если тесты падают:** починить. Известные причины:
   - Устаревшие имена методов DB (`add_group` → `create_group`)
   - API validation в TESTING mode
   - Import/setup issues
6. **Цель:** 0 зависаний, максимум passed, точные числа

**Критерий:** `pytest --timeout=10` завершается за < 5 минут, 0 зависаний.

**Выход:** обновлённый `reports/stage6-tests-v1.md` с реальными числами, коммит + push

---

### Этап B: Деплой на контроллер WB-8 (.244)

**ВАЖНО: Осторожно! Это реальный контроллер.**

**Задача для саб-агента:**
1. **Бэкап старой версии:**
   ```bash
   # На контроллере .244:
   cd /opt/wb-irrigation
   tar czf /root/irrigation-backup-$(date +%Y%m%d).tar.gz irrigation/
   ```
2. **Остановить старую версию:**
   - Узнать как запущена (systemd? screen? docker? напрямую python?):
     ```bash
     ps aux | grep irrigation
     systemctl list-units | grep irrigation
     ```
   - Остановить корректно (НЕ kill -9)
   - Убедиться что все зоны OFF
3. **Очистить (НЕ удалять сразу — переименовать):**
   ```bash
   mv /opt/wb-irrigation/irrigation /opt/wb-irrigation/irrigation-old
   ```
4. **Залить новую версию:**
   - Из ветки refactor/v2, НЕ из main
   - Варианты:
     a) git clone на контроллер (если есть git)
     b) tar архив через scp
   ```bash
   # Локально:
   cd /workspace/wb-irrigation
   tar czf /tmp/wb-irrigation-v2.tar.gz --exclude='.git' --exclude='__pycache__' --exclude='*.pyc' --exclude='irrigation.db*' --exclude='backups/*' --exclude='mosquitto_data' --exclude='mosquitto_log' .
   # Копируем через jump host
   scp -i /config/secrets/ssh/botops_key /tmp/wb-irrigation-v2.tar.gz botops@172.30.0.1:/tmp/
   # Далее с Docker-хоста на контроллер
   sshpass -p '123_grom' scp /tmp/wb-irrigation-v2.tar.gz root@10.2.5.244:/tmp/
   # На контроллере:
   mkdir -p /opt/wb-irrigation/irrigation
   cd /opt/wb-irrigation/irrigation
   tar xzf /tmp/wb-irrigation-v2.tar.gz
   ```
5. **Настройка:**
   - Скопировать БД из старой версии (если нужна — зоны, программы, настройки):
     ```bash
     cp /opt/wb-irrigation/irrigation-old/irrigation.db /opt/wb-irrigation/irrigation/
     ```
   - Запустить миграцию секретов (если нужна):
     ```bash
     cd /opt/wb-irrigation/irrigation
     python3 migrations/reencrypt_secrets.py --dry-run
     ```
   - Настроить MQTT auth (mosquitto на WB уже есть системный):
     - Проверить: `systemctl status wb-mqtt-serial`
     - Приложение должно подключаться к localhost:1883 (системный MQTT WB)
     - **НЕ поднимать** свой mosquitto — использовать штатный WB брокер
   - pip install зависимостей:
     ```bash
     pip3 install -r requirements.txt
     ```
6. **Запустить новую версию:**
   - Так же как была запущена старая (systemd/screen/etc.)
   - Проверить логи: нет ошибок при старте
   - Проверить веб-интерфейс: http://10.2.5.244:8080/

**Критерий:** приложение запущено, веб доступен, логи чистые.

**Выход:** `reports/stage-deploy-244.md`

---

### Этап C: Полное тестирование на реальном контроллере

#### C1: Написать спеку тестирования

**Перед тестированием — создать документ с чёткими тест-кейсами:**

Файл: `/workspace/wb-irrigation/reports/integration-test-spec.md`

```markdown
# Спецификация интеграционного тестирования wb-irrigation v2.0
# Контроллер: WB-8 (10.2.5.244)

## 1. Базовый запуск
| # | Проверка | Как | Ожидаемый результат |
|---|----------|-----|---------------------|
| 1.1 | Приложение стартует | curl http://10.2.5.244:8080/ | HTTP 200, HTML страница |
| 1.2 | Логин | POST /api/login с паролем | 200, session cookie |
| 1.3 | Авто-генерация SECRET_KEY | ls -la .secret_key | Файл существует, 64 символа |
| 1.4 | Авто-генерация IRRIG_KEY | ls -la .irrig_secret_key | Файл существует, 32 байта |

## 2. Зоны (24 шт)
| # | Проверка | Как | Ожидаемый результат |
|---|----------|-----|---------------------|
| 2.1 | Список зон | GET /api/zones | JSON, 24 зоны |
| 2.2 | Включить зону 1 | POST /api/zones/1/mqtt/start | 200, реле K1 на wb-mr6cv3_85 = ON |
| 2.3 | MQTT QoS 2 | mosquitto_sub -v -t '#' на WB | Сообщение с QoS 2 |
| 2.4 | Retain | mosquitto_sub при подключении | Последнее состояние видно |
| 2.5 | Выключить зону 1 | POST /api/zones/1/mqtt/stop | 200, реле = OFF |
| 2.6 | observed_state | Проверить в логах | "State verified: zone 1 = on/off" |
| 2.7 | Все 24 зоны ON/OFF | Поочерёдно включить-выключить | Все реле откликаются |

## 3. Группы
| # | Проверка | Как | Ожидаемый результат |
|---|----------|-----|---------------------|
| 3.1 | Создать группу | POST /api/groups | 201 |
| 3.2 | Запустить группу | POST /api/groups/{id}/start | Зоны группы включаются последовательно |
| 3.3 | Остановить группу | POST /api/groups/{id}/stop | Все зоны группы OFF |

## 4. Программы (расписание)
| # | Проверка | Как | Ожидаемый результат |
|---|----------|-----|---------------------|
| 4.1 | Создать программу | POST /api/programs | 201 |
| 4.2 | Scheduler видит | GET /api/scheduler/jobs | Программа в списке |
| 4.3 | Ручной запуск | POST /api/programs/{id}/start (если есть) | Зоны запускаются |

## 5. Security
| # | Проверка | Как | Ожидаемый результат |
|---|----------|-----|---------------------|
| 5.1 | CSRF | POST без X-CSRFToken | 400 Bad Request |
| 5.2 | CSRF с токеном | POST с meta csrf-token | 200 OK |
| 5.3 | Guest = viewer | Логин guest → POST start zone | 403 Forbidden |
| 5.4 | Rate limiting | 6 неудачных логинов подряд | 429 Too Many Requests |
| 5.5 | Пароль '1234' | Попытка установить | 400 "Слишком простой" |

## 6. MQTT
| # | Проверка | Как | Ожидаемый результат |
|---|----------|-----|---------------------|
| 6.1 | QoS 2 доставка | Включить зону, проверить MQTT лог | QoS 2 PUBREC/PUBCOMP |
| 6.2 | Retain | Переподключить sub → видно состояние | Retained message |
| 6.3 | Fault detection | Отключить реле (если возможно) | fault_count +1, алерт |

## 7. Safety
| # | Проверка | Как | Ожидаемый результат |
|---|----------|-----|---------------------|
| 7.1 | Emergency stop | POST /api/emergency-stop | ВСЕ зоны OFF |
| 7.2 | Watchdog cap | Включить зону, ждать > cap | Авто-выключение |
| 7.3 | Emergency resume | POST /api/emergency-resume | Система разблокирована |

## 8. UI (ручная проверка через браузер)
| # | Проверка | Ожидаемый результат |
|---|----------|---------------------|
| 8.1 | Страница логина | Форма отображается |
| 8.2 | Дашборд зон | 24 зоны с статусами |
| 8.3 | SSE обновления | При включении зоны — UI обновляется real-time |
| 8.4 | Карта | Карта зон отображается |
| 8.5 | Настройки | Все разделы доступны |
| 8.6 | Логи | История действий видна |

## 9. Мониторы
| # | Проверка | Как | Ожидаемый результат |
|---|----------|-----|---------------------|
| 9.1 | Rain delay | Установить rain delay | Зоны не запускаются |
| 9.2 | Env monitor | Проверить /api/env/values | Данные датчиков |

## 10. Бэкап
| # | Проверка | Как | Ожидаемый результат |
|---|----------|-----|---------------------|
| 10.1 | Создать бэкап | POST /api/backup | Файл создан в backups/ |
| 10.2 | Скачать бэкап | GET /api/backup/{file} | Файл скачивается |
```

#### C2: Запуск внутренних тестов на контроллере
```bash
# На контроллере (если pytest установлен):
cd /opt/wb-irrigation/irrigation
TESTING=1 python3 -m pytest tools/tests/tests_pytest/ --timeout=10 -q
```

#### C3: Выполнение интеграционных тестов
Пройти спеку по порядку, автоматизировать что можно через curl/API.

**Для реальных реле:**
```bash
# Проверить состояние реле через MQTT (с контроллера):
mosquitto_sub -t '/devices/wb-mr6cv3_85/controls/K1' -C 1
# Включить зону через API:
curl -X POST http://10.2.5.244:8080/api/zones/1/mqtt/start -H 'Cookie: session=...'
# Проверить что реле переключилось:
mosquitto_sub -t '/devices/wb-mr6cv3_85/controls/K1' -C 1
```

**Критерий:** все тесты из спеки пройдены, критических проблем нет.

**Выход:**
- `reports/integration-test-spec.md` — спека (написать ДО тестов)
- `reports/integration-test-results.md` — результаты с pass/fail каждого теста

---

## ПРАВИЛА ОРКЕСТРАТОРА

1. **Модель:** каждый саб-агент = `sessions_spawn(model="anthropic/claude-opus-4-6")`
2. **Порядок:** A → B → C (строго последовательно)
3. **Ветка:** работаем на `refactor/v2`, НЕ мержим в main
4. **Контроллер:**
   - Бэкап старой версии ПЕРЕД любыми изменениями
   - Старую версию НЕ удалять — переименовать (irrigation → irrigation-old)
   - Если что-то пошло не так → `mv irrigation-old irrigation` и перезапуск
5. **MQTT на WB:**
   - Использовать ШТАТНЫЙ брокер WB (wb-mqtt-serial, порт 1883)
   - НЕ поднимать свой mosquitto на контроллере
   - MQTT auth из refactor/v2 касается только Docker-версии, на WB — адаптировать
6. **Реле:**
   - К реле пока ничего не подключено — безопасно тестировать ON/OFF
   - Всё равно: после тестов убедиться что ВСЕ зоны OFF
7. **Статус в чат:** после каждого этапа — краткий статус
8. **Git:** все изменения коммитить в refactor/v2 с push
9. **Docker-хост:** НЕ трогать b24-migrator и другие контейнеры
10. **Откат:** если деплой на .244 сломался → восстановить из irrigation-old, сообщить
