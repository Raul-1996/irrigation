import os
import secrets
import stat
import logging
from dotenv import load_dotenv


load_dotenv()


def _load_or_generate_secret(env_var: str = 'SECRET_KEY',
                              file_path: str = '.secret_key') -> str:
    """Load SECRET_KEY from env, file, or generate a new one.

    Priority:
    1. Environment variable (if set and not the old hardcoded default)
    2. File on disk (.secret_key)
    3. Generate new random key, persist to file
    """
    env_val = os.environ.get(env_var, '').strip()
    if env_val and env_val != 'wb-irrigation-secret':
        return env_val

    # Try reading from file
    try:
        with open(file_path, 'r') as f:
            key = f.read().strip()
        if key:
            return key
    except FileNotFoundError:
        logging.getLogger(__name__).debug("No secret key file found, generating new one")

    # Generate new key and persist
    key = secrets.token_hex(32)
    with open(file_path, 'w') as f:
        f.write(key)
    os.chmod(file_path, stat.S_IRUSR | stat.S_IWUSR)  # 0o600
    return key


class Config:
    SECRET_KEY = _load_or_generate_secret()
    WTF_CSRF_ENABLED = True
    WTF_CSRF_CHECK_DEFAULT = True  # CSRF проверка включена для всех POST/PUT/DELETE
    WTF_CSRF_TIME_LIMIT = None
    # Прочие настройки
    EMERGENCY_STOP = False
    TESTING = os.environ.get('TESTING') == '1'


class TestConfig(Config):
    TESTING = True
    WTF_CSRF_ENABLED = False


