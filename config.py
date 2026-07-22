import os
import secrets

from dotenv import load_dotenv

from utils import SecretKeyConfigurationError, create_private_file, read_private_file

load_dotenv()


def _load_or_generate_secret(env_var: str = "SECRET_KEY", file_path: str = ".secret_key") -> str:
    """Load SECRET_KEY from env, file, or generate a new one.

    Priority:
    1. Environment variable (if set and not the old hardcoded default)
    2. File on disk (.secret_key)
    3. Generate new random key, persist to file
    """
    env_val = os.environ.get(env_var, "").strip()
    if env_val and env_val != "wb-irrigation-secret":
        return env_val

    # Try reading from file.  An existing but empty/damaged file must never be
    # replaced silently: that would rotate the session key without an operator
    # recovery decision and invalidate every current session.
    try:
        raw_key = read_private_file(file_path)
    except FileNotFoundError:
        raw_key = None

    if raw_key is not None:
        try:
            key = raw_key.decode("utf-8").strip()
        except UnicodeDecodeError as error:
            raise SecretKeyConfigurationError("Flask secret key file is invalid; restore the original key") from error
        if not key:
            raise SecretKeyConfigurationError("Flask secret key file is empty; restore the original key")
        return key

    # Generate new key and persist
    key = secrets.token_hex(32)
    try:
        create_private_file(file_path, key.encode("utf-8"))
        return key
    except FileExistsError:
        # Another worker completed first-start initialisation.  Re-read its
        # material instead of rotating it with this worker's generated value.
        try:
            persisted = read_private_file(file_path).decode("utf-8").strip()
        except UnicodeDecodeError as error:
            raise SecretKeyConfigurationError("Flask secret key file is invalid; restore the original key") from error
        if not persisted:
            raise SecretKeyConfigurationError("Flask secret key file is empty; restore the original key") from None
        return persisted


# Module-level TESTING flag — read ONCE at import time so every site that
# does ``from config import TESTING`` sees the same boolean.  Centralised so
# we don't have 19+ different ``os.environ.get('TESTING') == '1'`` snippets
# spread across the codebase (each one is one inconsistency away from a
# subtle behavioural drift between modules).  Tests that need to flip
# TESTING after import time should use ``monkeypatch.setattr('config.TESTING',
# True)`` — that is what tests/conftest.py does.
TESTING: bool = os.environ.get("TESTING") == "1"


class Config:
    SECRET_KEY = _load_or_generate_secret()
    WTF_CSRF_ENABLED = True
    WTF_CSRF_CHECK_DEFAULT = True  # CSRF проверка включена для всех POST/PUT/DELETE
    WTF_CSRF_TIME_LIMIT = None
    # Прочие настройки
    EMERGENCY_STOP = False
    TESTING = TESTING
    # Optional GitHub relay channel for weather data, used when the live
    # ``weather.source_mode`` setting is ``relay`` (sites where Open-Meteo is
    # network-blocked). URL = raw file URL (public repo) or contents-API URL
    # (private repo); token only needed for a private repo. Read once at import
    # time — changing them needs a service restart.
    # NOTE: the relay only covers the forecast path (WeatherService._fetch_api).
    # The H2 water-balance history fetch (services/weather/balance.py::fetch_history)
    # still calls Open-Meteo directly, so on a relay-only site keep
    # weather.balance.enabled OFF — otherwise balance silently freezes.
    OPEN_METEO_RELAY_URL = os.environ.get("OPEN_METEO_RELAY_URL", "").strip()
    OPEN_METEO_RELAY_TOKEN = os.environ.get("OPEN_METEO_RELAY_TOKEN", "").strip()


class TestConfig(Config):
    TESTING = True
    WTF_CSRF_ENABLED = False
