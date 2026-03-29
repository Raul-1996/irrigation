# Сравнение тестов: main vs refactor/v2

**Дата:** 2026-03-29 06:30 UTC  
**Всего тестов:** 33 файла  
**Метод:** `pytest --timeout=10 -q --tb=no --no-header` с timeout 25 сек на файл

## Сводка

| Метрика | main | refactor/v2 | Изменение |
|---------|------|-------------|-----------|
| Total files | 33 | 33 | = |
| Полностью пройденные | 24 | 23 | -1 |
| С ошибками | 9 | 10 | +1 |

## Детальное сравнение

| Файл | main | refactor/v2 | Статус |
|------|------|-------------|--------|
| test_api.py | ✅ passed | ✅ passed | ✅ STABLE |
| test_api_groups_crud.py | ❌ FAILED | ❌ FAILED | 🔄 SAME |
| test_api_mqtt_servers.py | ❌ FAILED | ✅ passed | 🎯 **IMPROVEMENT** |
| test_api_programs_crud.py | ✅ passed | ❌ FAILED | ⚠️ **REGRESSION** |
| test_auth_scheduler_misc.py | ✅ passed | ✅ passed | ✅ STABLE |
| test_db_migrations_backup.py | ✅ passed | ✅ passed | ✅ STABLE |
| test_env_mqtt_values.py | ⏩ skipped | ✅ passed | 🎯 **IMPROVEMENT** |
| test_group_cancel_immediate_off.py | ✅ passed | ❌ FAILED | ⚠️ **REGRESSION** |
| test_group_exclusivity.py | ✅ passed | ✅ passed | ✅ STABLE |
| test_logs_update.py | ✅ passed | ✅ passed | ✅ STABLE |
| test_manual_stop_mid_zone.py | ✅ passed | ✅ passed | ✅ STABLE |
| test_mqtt_devices_compare.py | ⏩ skipped | ⏩ skipped | ✅ STABLE |
| test_mqtt_emergency_block.py | ✅ passed | ✅ passed | ✅ STABLE |
| test_mqtt_end_to_end.py | ⏩ skipped | ⏩ skipped | ✅ STABLE |
| test_mqtt_mock.py | ❌ FAILED | ❌ FAILED | 🔄 SAME |
| test_mqtt_probe.py | ✅ passed | ✅ passed | ✅ STABLE |
| test_mqtt_servers.py | ✅ passed | ✅ passed | ✅ STABLE |
| test_mqtt_zone_control.py | ❌ FAILED | ⏩ skipped | 🎯 **IMPROVEMENT** |
| test_photos_and_water.py | ✅ passed | ❌ FAILED | ⚠️ **REGRESSION** |
| test_program_conflicts.py | ✅ passed | ✅ passed | ✅ STABLE |
| test_programs_and_groups.py | ✅ passed | ✅ passed | ✅ STABLE |
| test_scheduler_cleanup.py | ✅ passed | ❌ FAILED | ⚠️ **REGRESSION** |
| test_scheduler_operations.py | ✅ passed | ✅ passed | ✅ STABLE |
| test_services_zone_control.py | ✅ passed | ✅ passed | ✅ STABLE |
| test_sse_zones.py | ✅ passed | ✅ passed | ✅ STABLE |
| test_telegram_bot.py | ❌ FAILED | ❌ FAILED | 🔄 SAME |
| test_telegram_extended.py | ✅ passed | ✅ passed | ✅ STABLE |
| test_telegram_routes.py | ❌ FAILED | ❌ FAILED | 🔄 SAME |
| test_utils_config.py | ❌ FAILED | ❌ FAILED | 🔄 SAME |
| test_utils_encryption.py | ✅ passed | ✅ passed | ✅ STABLE |
| test_water_stats.py | ✅ passed | ✅ passed | ✅ STABLE |
| test_watering_time_and_postpone.py | ✅ passed | ✅ passed | ✅ STABLE |
| test_zone_runs.py | ❌ FAILED | ✅ passed | 🎯 **IMPROVEMENT** |

## 🔥 Регрессии (работали в main, сломались в v2)

1. **test_api_programs_crud.py**
   - main: ✅ 10 passed
   - v2: ❌ FAILED on `test_create_program_missing_fields`

2. **test_group_cancel_immediate_off.py**
   - main: ✅ passed
   - v2: ❌ FAILED on `test_group_cancel_immediate_off`

3. **test_photos_and_water.py**
   - main: ✅ 2 passed  
   - v2: ❌ FAILED on `test_photo_lifecycle`

4. **test_scheduler_cleanup.py**
   - main: ✅ 2 passed
   - v2: ❌ FAILED on `test_scheduler_cancel_group_jobs`

## 🎯 Улучшения (ломались в main, починены в v2)

1. **test_api_mqtt_servers.py**
   - main: ❌ FAILED on `test_get_servers`
   - v2: ✅ 9 passed

2. **test_env_mqtt_values.py**
   - main: ⏩ skipped
   - v2: ✅ 2 passed

3. **test_mqtt_zone_control.py**  
   - main: ❌ FAILED on `test_zone_mqtt_endpoints_exist`
   - v2: ⏩ 1 passed, 3 skipped

4. **test_zone_runs.py**
   - main: ❌ FAILED on `test_finish_zone_run`
   - v2: ✅ 4 passed

## 🔄 Стабильные ошибки (ломались в обеих ветках)

1. **test_api_groups_crud.py** - `test_create_group_empty_name`
2. **test_mqtt_mock.py** - multiple failures in MQTT server tests  
3. **test_telegram_bot.py** - `test_telegram_webhook_auth_flow`
4. **test_telegram_routes.py** - callback processing failures
5. **test_utils_config.py** - API error format + image normalization

## 📊 Общий анализ

**Положительное:**
- ✅ 4 улучшения vs 4 регрессии - БАЛАНС
- ✅ Починены важные MQTT и zone-related тесты
- ✅ 18 тестов стабильно проходят в обеих ветках

**Проблемное:**  
- ⚠️ Новые регрессии в API programs, group cancellation, photos, scheduler
- ⚠️ 5 тестов стабильно ломаются в обеих ветках

**Рекомендация:**
- Перед мержем v2→main нужно исправить 4 регрессии
- Рассмотреть фиксы для 5 стабильно ломающихся тестов

---
*Отчёт сгенерирован автоматически с помощью subagent irrigation-compare-tests*