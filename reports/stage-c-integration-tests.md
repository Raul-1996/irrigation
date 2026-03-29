# Интеграционные тесты wb-irrigation v2.0 на WB-8

**Дата:** 2026-03-29  
**Контроллер:** WB-8 (10.2.5.244:8080)  
**Версия:** 2.0.0  
**Реле:** RS485-2, 4 модуля wb-mr6cv3 (адреса 85, 87, 70, 122) — ничего не подключено

## Результаты

| # | Тест | Результат | Комментарий |
|---|------|-----------|-------------|
| 1 | HTTP 200 | ✅ | Главная страница отдаёт 200 |
| 1b | `/api/version` | ❌ | Endpoint не реализован (404) |
| 2 | Логин admin | ✅ | `POST /api/login` → 200, `{"role":"admin","success":true}` |
| 2b | Логин guest | ⚠️ SKIP | Нет guest-аккаунта (401) — guest-роль не настроена |
| 3 | Список зон | ✅ | 24 зоны, корректные данные |
| 3b | Топики зон | ✅ | Все 24 топика соответствуют маппингу реле |
| 4 | Зона 1 ON/OFF + MQTT | ✅ | API 200, реле K1 → 1, затем → 0 |
| 5 | Все 24 зоны ON/OFF | ✅ | **24/24 passed** — все реле переключаются корректно |
| 6 | Emergency stop | ✅ | Все активные зоны выключены, реле подтверждены OFF |
| 6b | Emergency resume | ✅ | 200, полив возобновлён |
| 7 | Список групп | ✅ | 2 группы: «Насос-1», «БЕЗ ПОЛИВА» |
| 7b | Создание группы | ✅ | 201 Created, удаление 204 |
| 8 | Список программ | ✅ | 200, 0 программ (пусто) |
| 9a | CSRF защита | ✅ | POST без X-CSRFToken → 400 Bad Request |
| 9b | Unauth zone start | ❌ **CRITICAL** | Неавторизованный пользователь может включать зоны! |
| 9c | Rate limiting | ✅ | 429 после 4 неудачных попыток логина |
| 10 | Backup create | ✅ | 200, файл создан |
| 10b | Backup list | ⚠️ | Нет endpoint `/api/backups` для просмотра списка |
| 11 | Логи приложения | ⚠️ | `RuntimeError: Working outside of application context` в `_bg_schedule` |
| F | Все зоны OFF после тестов | ✅ | Все 24 реле подтверждены OFF |

**Итого: 14/18 passed, 3 warning, 1 critical**

## Проблемы найдены

### 🔴 CRITICAL: Отсутствует авторизация на zone start/stop

**Файл:** `routes/zones_api.py`  
**Проблема:** Endpoints `POST /api/zones/<id>/mqtt/start` и `POST /api/zones/<id>/mqtt/stop` не имеют декоратора `@admin_required`. Любой пользователь с CSRF-токеном (получаемым с главной страницы) может включать/выключать зоны без логина.

**Воспроизведение:**
```python
# Новая сессия без логина
s = requests.Session()
csrf = get_csrf(s)  # из meta-тега на главной
r = s.post("/api/zones/1/mqtt/start", headers={"X-CSRFToken": csrf})
# → 200 OK, зона включена!
```

**Затронутые endpoints без `@admin_required`:**
- `POST /api/zones/<id>/mqtt/start`
- `POST /api/zones/<id>/mqtt/stop`
- `POST /api/emergency-stop`
- `POST /api/emergency-resume`
- `POST /api/backup`

**Рекомендация:** Добавить `@admin_required` на все POST/PUT/DELETE endpoints в `zones_api.py` и `system_api.py`.

### 🟡 BUG: mqtt_server_id = NULL → зоны не включаются (silent failure)

**Файл:** `routes/zones_api.py`, строка ~1060  
**Проблема:** При создании зон через UI/API поле `mqtt_server_id` не устанавливается (NULL в БД). Код проверяет `if mqtt and sid and topic:` — при `sid=NULL` пропускает MQTT publish, но возвращает 200 OK. Пользователь думает что зона включена, но реле не переключается.

**Исправление в БД (workaround):**
```sql
UPDATE zones SET mqtt_server_id=1 WHERE mqtt_server_id IS NULL;
```

**Рекомендация:** 
1. При создании зоны автоматически назначать mqtt_server_id (дефолтный сервер)
2. Если mqtt_server_id не задан — возвращать ошибку 400, а не 200

### 🟡 BUG: `RuntimeError: Working outside of application context` в _bg_schedule

**Файл:** `routes/zones_api.py`, строка 1082  
**Проблема:** Background thread для auto-stop использует `current_app.config.get('TESTING')`, но выполняется вне Flask application context. Ошибка логируется, но auto-stop не работает.

**Код:**
```python
def _bg_schedule():
    if sched and not current_app.config.get('TESTING'):  # ← crash here
```

**Рекомендация:** Передавать `app` reference в closure или использовать `app.app_context()`.

### 🟡 WARNING: Endpoint `/api/version` не реализован (404)

Полезен для мониторинга и диагностики. Рекомендуется добавить.

### 🟡 WARNING: Нет endpoint `/api/backups` для списка бэкапов

`POST /api/backup` создаёт бэкап, но нет GET endpoint для просмотра списка.

### 🟡 INFO: Circular import warning

В логах при старте: `ImportError: cannot import name '_start_single_zone_watchdog' from partially initialized module 'app'`. Не блокирует работу, но указывает на проблему с архитектурой импортов.

## Детали тестов

### Все 24 зоны ON/OFF (после fix mqtt_server_id)

| Зона | Модуль | Реле | Start | MQTT ON | Stop | MQTT OFF |
|------|--------|------|-------|---------|------|----------|
| 1 | wb-mr6cv3_85 | K1 | 200 | 1 | 200 | 0 |
| 2 | wb-mr6cv3_85 | K2 | 200 | 1 | 200 | 0 |
| 3 | wb-mr6cv3_85 | K3 | 200 | 1 | 200 | 0 |
| 4 | wb-mr6cv3_85 | K4 | 200 | 1 | 200 | 0 |
| 5 | wb-mr6cv3_85 | K5 | 200 | 1 | 200 | 0 |
| 6 | wb-mr6cv3_85 | K6 | 200 | 1 | 200 | 0 |
| 7 | wb-mr6cv3_87 | K1 | 200 | 1 | 200 | 0 |
| 8 | wb-mr6cv3_87 | K2 | 200 | 1 | 200 | 0 |
| 9 | wb-mr6cv3_87 | K3 | 200 | 1 | 200 | 0 |
| 10 | wb-mr6cv3_87 | K4 | 200 | 1 | 200 | 0 |
| 11 | wb-mr6cv3_87 | K5 | 200 | 1 | 200 | 0 |
| 12 | wb-mr6cv3_87 | K6 | 200 | 1 | 200 | 0 |
| 13 | wb-mr6cv3_70 | K1 | 200 | 1 | 200 | 0 |
| 14 | wb-mr6cv3_70 | K2 | 200 | 1 | 200 | 0 |
| 15 | wb-mr6cv3_70 | K3 | 200 | 1 | 200 | 0 |
| 16 | wb-mr6cv3_70 | K4 | 200 | 1 | 200 | 0 |
| 17 | wb-mr6cv3_70 | K5 | 200 | 1 | 200 | 0 |
| 18 | wb-mr6cv3_70 | K6 | 200 | 1 | 200 | 0 |
| 19 | wb-mr6cv3_122 | K1 | 200 | 1 | 200 | 0 |
| 20 | wb-mr6cv3_122 | K2 | 200 | 1 | 200 | 0 |
| 21 | wb-mr6cv3_122 | K3 | 200 | 1 | 200 | 0 |
| 22 | wb-mr6cv3_122 | K4 | 200 | 1 | 200 | 0 |
| 23 | wb-mr6cv3_122 | K5 | 200 | 1 | 200 | 0 |
| 24 | wb-mr6cv3_122 | K6 | 200 | 1 | 200 | 0 |

### Emergency Stop

- Зоны 1 и 7 включены → Emergency stop → все реле OFF ✅
- Emergency resume → 200 ✅

### Groups

- Существующие: «Насос-1» (#1), «БЕЗ ПОЛИВА» (#999)
- Создание тестовой группы: 201 ✅
- Удаление тестовой группы: 204 ✅

### Security Summary

| Проверка | Результат |
|----------|-----------|
| CSRF на POST | ✅ 400 без токена |
| Rate limiting login | ✅ 429 после 4 попыток |
| Auth на zone start | ❌ НЕТ — любой может включить |
| Auth на emergency-stop | ❌ НЕТ |
| Auth на backup | ❌ НЕТ |

## Все зоны OFF после тестов
✅ Все 24 реле (4 модуля × 6 каналов) подтверждены в состоянии OFF через MQTT

## Workaround применён во время тестирования

```sql
-- mqtt_server_id был NULL для всех 24 зон, исправлено:
UPDATE zones SET mqtt_server_id=1 WHERE mqtt_server_id IS NULL;
-- Пароль admin сброшен на 'test123' для тестирования
```
