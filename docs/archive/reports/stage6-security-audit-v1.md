# Stage 6c: Security Audit — wb-irrigation refactor/v2

**Дата:** 2026-03-28

## Проверка закрытия уязвимостей

### SEC-001: Anonymous MQTT → ✅ ЗАКРЫТО
```
mosquitto.conf: allow_anonymous false
mosquitto.conf: password_file /mosquitto/config/passwd
mosquitto.conf: acl_file /mosquitto/config/acl
```

### SEC-002: Hardcoded SECRET_KEY → ✅ ЗАКРЫТО
- `config.py` использует `_load_or_generate_secret()`:
  1. ENV переменная (отклоняет старый `wb-irrigation-secret`)
  2. Файл `.secret_key`
  3. Генерация `secrets.token_hex(32)` + persist с `chmod 0600`
- Упоминание `wb-irrigation-secret` в config.py — только как проверка "не использовать старый ключ"

### SEC-003: Plaintext MQTT passwords → ✅ ЗАКРЫТО
- `db/mqtt.py`: пароли шифруются с префиксом `ENC:` при сохранении
- `db/migrations.py:612-615`: миграция шифрует существующие plaintext пароли
- Расшифровка через `_decrypt_password()` при чтении
- Ключ шифрования: `utils.py` → `.mqtt_fernet.key` (Fernet symmetric)

### SEC-004: CSRF disabled → ✅ ЗАКРЫТО
```python
config.py: WTF_CSRF_ENABLED = True
config.py: WTF_CSRF_CHECK_DEFAULT = True
app.py: csrf = CSRFProtect(app)
```

### SEC-005: Guest full access → ✅ ЗАКРЫТО
- `app.py _auth_before_request`: viewer role → read-only (403 на POST/PUT/DELETE)
- `app.py _require_admin_for_mutations`: admin required для мутаций
- Guest (не залогиненный) → только GET + ограниченные POST (login, env, emergency)

### SEC-006: Session rate-limit → ✅ ЗАКРЫТО
- `services/rate_limiter.py`: `LoginRateLimiter` — IP-based
- `routes/auth.py`: интегрирован `login_limiter`
- Защита от brute-force на login endpoint

### SEC-007: Hostname key → ✅ ЗАКРЫТО
- Нет привязки к hostname в SECRET_KEY
- Ключ генерируется криптографически: `secrets.token_hex(32)`

### SEC-008: QoS 0 → ✅ ЗАКРЫТО (для управления)
- `services/zone_control.py`: все publish команды используют `qos=2, retain=True`
- `services/mqtt_pub.py`: поддержка QoS 0/1/2 с retry для QoS ≥ 1
- ⚠️ Subscribes в monitors.py используют QoS 0 — допустимо для мониторинга

## Дополнительные проверки

### Observed State → ✅ ЕСТЬ
- `services/observed_state.py` (257 строк): StateVerifier
- Интегрирован в `services/zone_control.py` — verify_async() после start/stop
- Проверяет что устройство подтвердило состояние через MQTT feedback

### Injection/RCE → ✅ ЧИСТО
- Нет `os.system()`, `subprocess`, `eval()`, `exec()`, `pickle`, `shell=True` в routes/services
- SQL через parameterized queries (sqlite3 `?` placeholders)

### Security Headers → ✅ ЕСТЬ
```python
app.py: X-Content-Type-Options: nosniff
app.py: X-Frame-Options: SAMEORIGIN
app.py: SESSION_COOKIE_SAMESITE: Lax
app.py: SESSION_COOKIE_HTTPONLY: True
```

### Docker → ✅ ЗАКРЫТО
- Dockerfile: `USER appuser` (не root)
- `docker-compose.yml`: `SECRET_KEY=${SECRET_KEY:-}` (пустой → auto-generate)

### Новые уязвимости в route-файлах → ✅ НЕ ОБНАРУЖЕНЫ
- Все inputs валидируются через `int()`, `str()`, `.strip()`
- File uploads (photo) проверяют content-type и размер
- SSE endpoints не утекают sensitive data

## Общая оценка: **B+ (GOOD)**
Все 8 критических уязвимостей закрыты. Рекомендации:
1. Добавить `Strict-Transport-Security` header (HSTS) для HTTPS
2. Добавить `Content-Security-Policy` header
3. Rate limiting на все API endpoints (не только login)
4. QoS 1 для subscribe в monitors.py (опционально)
