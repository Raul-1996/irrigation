"""Phase 2 regressions for env configuration and settings-page contracts."""

from __future__ import annotations

import re
import sqlite3
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parents[2]


def _enable_native_security(app) -> None:
    """Enable the production auth/CSRF hooks disabled by the shared fixture."""
    app.config.update(TESTING=False, WTF_CSRF_ENABLED=True)
    app.db.set_setting_value("password_hash", "test-password-hash")
    app.db.set_setting_value("password_must_change", "0")


def _csrf_token(response) -> str:
    match = re.search(r'<meta name="csrf-token" content="([^"]+)">', response.get_data(as_text=True))
    assert match is not None
    return match.group(1)


def _env_config(*, temp_topic: str, temp_server_id: object, hum_topic: str = "/hum") -> dict:
    return {
        "temp": {"enabled": True, "topic": temp_topic, "server_id": temp_server_id},
        "hum": {"enabled": True, "topic": hum_topic, "server_id": 2},
    }


def _create_env_servers(db) -> None:
    first = db.create_mqtt_server({"name": "Temperature", "host": "temp-broker", "port": 1883})
    second = db.create_mqtt_server({"name": "Humidity", "host": "hum-broker", "port": 1883})
    assert (first["id"], second["id"]) == (1, 2)


def test_env_get_and_post_require_explicit_login_and_admin_csrf(app, guest_client, admin_client):
    _create_env_servers(app.db)
    _enable_native_security(app)

    assert guest_client.get("/api/env").status_code == 401
    assert guest_client.get("/").status_code == 302

    app.config["WTF_CSRF_ENABLED"] = False
    try:
        guest_response = guest_client.post(
            "/api/env",
            json=_env_config(temp_topic="/guest", temp_server_id=1),
        )
    finally:
        app.config["WTF_CSRF_ENABLED"] = True
    assert guest_response.status_code == 401
    assert guest_response.get_json()["error_code"] == "UNAUTHENTICATED"

    missing_csrf = admin_client.post(
        "/api/env",
        json=_env_config(temp_topic="/admin", temp_server_id=1),
    )
    assert missing_csrf.status_code == 400

    admin_token = _csrf_token(admin_client.get("/"))
    accepted = admin_client.post(
        "/api/env",
        json=_env_config(temp_topic="/admin", temp_server_id=1),
        headers={"X-CSRFToken": admin_token},
    )
    assert accepted.status_code == 200
    assert accepted.get_json()["success"] is True


def test_set_env_config_rejects_invalid_server_id_without_partial_write(test_db):
    _create_env_servers(test_db)
    original = _env_config(temp_topic="/original/temp", temp_server_id=1, hum_topic="/original/hum")
    assert test_db.set_env_config(original) is True

    invalid = _env_config(temp_topic="/changed/temp", temp_server_id="not-an-id", hum_topic="/changed/hum")
    assert test_db.set_env_config(invalid) is False
    assert test_db.get_env_config() == original


def test_set_env_config_rolls_back_every_key_when_one_write_fails(test_db):
    _create_env_servers(test_db)
    original = _env_config(temp_topic="/original/temp", temp_server_id=1, hum_topic="/original/hum")
    assert test_db.set_env_config(original) is True

    with sqlite3.connect(test_db.db_path) as conn:
        conn.execute(
            """
            CREATE TRIGGER reject_env_temp_server
            BEFORE INSERT ON settings
            WHEN NEW.key = 'env.temp.server_id'
            BEGIN
                SELECT RAISE(ABORT, 'rejected env temp server');
            END
            """
        )

    changed = _env_config(temp_topic="/changed/temp", temp_server_id=9, hum_topic="/changed/hum")
    assert test_db.set_env_config(changed) is False
    assert test_db.get_env_config() == original


def test_settings_template_matches_backend_password_minimum():
    source = (_PROJECT_ROOT / "templates" / "settings.html").read_text(encoding="utf-8")

    assert "Минимальная длина пароля — 8 символов." in source
    assert re.search(r'id="new_password"[^>]*minlength="8"', source)
    assert re.search(r'id="new_password2"[^>]*minlength="8"', source)
    assert "new_password.length < 8" in source
    assert "Пароль должен быть не короче 8 символов." in source


def test_weather_location_save_validates_locale_numbers_and_server_result():
    source = (_PROJECT_ROOT / "templates" / "settings.html").read_text(encoding="utf-8")

    assert "function parseLocaleCoordinate(value)" in source
    assert ".replace(',', '.')" in source
    assert "Number.isFinite(parsed)" in source
    assert "latitude < -90 || latitude > 90" in source
    assert "longitude < -180 || longitude > 180" in source
    assert "if (!locationResponse.ok || !locationResult.success)" in source
    assert "if (!r.ok || !j.success)" in source
