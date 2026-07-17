# Сравнение тестов: main vs refactor/v2

**Дата:** 2026-03-29
**Метод:** 33 рабочих файла, каждый отдельным subprocess, timeout=25s

## Сводка

| Метрика | main | refactor/v2 |
|---------|------|-------------|
| Total files | 33 | 33 |
| ✅ All passed (exit=0) | 24 | 24 |
| ⚠️ Has failures (exit=1) | 9 | 9 |
| 🔴 Timeouts | 0 | 0 |
| Зависаний | **0** | **0** |

## Детальное сравнение

| # | Файл | main | v2 | Статус |
|---|------|------|-----|--------|
| 1 | test_api.py | ✅ 4 passed | ✅ 4 passed | ✅ OK |
| 2 | test_api_groups_crud.py | ⚠️ FAILED: test_create_group_empty_name | ⚠️ FAILED: test_create_group_empty_name | ✅ SAME |
| 3 | test_api_mqtt_servers.py | ⚠️ FAILED: test_get_servers | ✅ 9 passed | 🟢 **IMPROVED** |
| 4 | test_api_programs_crud.py | ✅ 10 passed | ⚠️ FAILED: test_create_program_missing_fields | 🔴 **REGRESSION** |
| 5 | test_auth_scheduler_misc.py | ✅ 7 passed | ✅ 7 passed | ✅ OK |
| 6 | test_db_migrations_backup.py | ✅ 2 passed | ✅ 2 passed | ✅ OK |
| 7 | test_env_mqtt_values.py | ✅ 1 passed 1 skip | ✅ 2 passed | 🟢 **IMPROVED** |
| 8 | test_group_cancel_immediate_off.py | ✅ 1 passed | ⚠️ FAILED: test_group_cancel_immediate_off | 🔴 **REGRESSION** |
| 9 | test_group_exclusivity.py | ✅ 1 passed | ✅ 1 passed | ✅ OK |
| 10 | test_logs_update.py | ✅ 1 passed | ✅ 1 passed | ✅ OK |
| 11 | test_manual_stop_mid_zone.py | ✅ 1 passed | ✅ 1 passed | ✅ OK |
| 12 | test_mqtt_devices_compare.py | ✅ 1 skip | ✅ 1 skip | ✅ OK |
| 13 | test_mqtt_emergency_block.py | ✅ 1 passed | ✅ 1 passed | ✅ OK |
| 14 | test_mqtt_end_to_end.py | ✅ 1 skip | ✅ 1 skip | ✅ OK |
| 15 | test_mqtt_mock.py | ⚠️ FAILED: test_delete_nonexistent_server | ⚠️ FAILED: test_delete_nonexistent_server | ✅ SAME |
| 16 | test_mqtt_probe.py | ✅ 1 passed | ✅ 1 passed | ✅ OK |
| 17 | test_mqtt_servers.py | ✅ 1 passed | ✅ 1 passed | ✅ OK |
| 18 | test_mqtt_zone_control.py | ⚠️ FAILED: test_zone_mqtt_endpoints_exist | ✅ 1 passed 3 skip | 🟢 **IMPROVED** |
| 19 | test_photos_and_water.py | ✅ 2 passed | ⚠️ FAILED: test_photo_lifecycle | 🔴 **REGRESSION** |
| 20 | test_program_conflicts.py | ✅ 1 passed | ✅ 1 passed | ✅ OK |
| 21 | test_programs_and_groups.py | ⚠️ FAILED: test_start_group_when_running | ✅ 5 passed | 🟢 **IMPROVED** |
| 22 | test_scheduler_cleanup.py | ⚠️ FAILED: test_scheduler_cancel_group_jobs | ⚠️ FAILED: test_scheduler_cancel_group_jobs | ✅ SAME |
| 23 | test_scheduler_operations.py | ✅ 8 passed | ✅ 8 passed | ✅ OK |
| 24 | test_services_zone_control.py | ⚠️ FAILED: test_emergency_stop | ✅ 9 passed | 🟢 **IMPROVED** |
| 25 | test_sse_zones.py | ✅ 1 passed | ✅ 1 passed | ✅ OK |
| 26 | test_telegram_bot.py | ⚠️ FAILED: test_telegram_webhook_auth_flow | ⚠️ FAILED: test_telegram_webhook_auth_flow | ✅ SAME |
| 27 | test_telegram_extended.py | ✅ 10 passed | ✅ 10 passed | ✅ OK |
| 28 | test_telegram_routes.py | ⚠️ FAILED: test_process_callback_groups | ⚠️ FAILED: test_process_callback_groups | ✅ SAME |
| 29 | test_utils_config.py | ⚠️ FAILED: test_api_error_format | ⚠️ FAILED: test_api_error_format | ✅ SAME |
| 30 | test_utils_encryption.py | ✅ 15 passed | ✅ 15 passed | ✅ OK |
| 31 | test_water_stats.py | ✅ 1 passed | ✅ 1 passed | ✅ OK |
| 32 | test_watering_time_and_postpone.py | ✅ 2 passed | ✅ 2 passed | ✅ OK |
| 33 | test_zone_runs.py | ✅ 4 passed | ✅ 4 passed | ✅ OK |

## Итоги

### 🟢 Улучшения (main ломался, v2 починил): 5 файлов
- `test_api_mqtt_servers.py` — test_get_servers
- `test_env_mqtt_values.py` — skip→pass
- `test_mqtt_zone_control.py` — test_zone_mqtt_endpoints_exist
- `test_programs_and_groups.py` — test_start_group_when_running
- `test_services_zone_control.py` — test_emergency_stop

### 🔴 Регрессии (main работал, v2 сломался): 3 файла
- `test_api_programs_crud.py` — test_create_program_missing_fields
- `test_group_cancel_immediate_off.py` — test_group_cancel_immediate_off
- `test_photos_and_water.py` — test_photo_lifecycle

### ✅ Одинаковые (не изменились): 25 файлов
- 19 полностью clean
- 6 с одинаковыми failures на обеих ветках

## Заключение

**Рефакторинг v2 больше починил, чем сломал:**
- **5 улучшений** vs **3 регрессии** (нетто: +2)
- 25 файлов стабильны на обеих ветках
- 3 регрессии — точечные, поддаются починке

**Рекомендация:** Починить 3 регрессии, после чего v2 будет объективно лучше main по тестам.
