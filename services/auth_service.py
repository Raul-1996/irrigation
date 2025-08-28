from typing import Optional
from database import db
from werkzeug.security import check_password_hash


def verify_admin(password: str) -> bool:
    """Deprecated: фиксированный пароль администратора (для старых тестов)."""
    return False


def verify_user(password: str) -> bool:
    """Deprecated: фиксированный пароль пользователя (для старых тестов)."""
    return False


def verify_password(password: str) -> tuple[bool, str]:
    """
    Проверка пароля и возврат роли пользователя
    Возвращает: (успех, роль)
    """
    # Сначала пытаемся проверить хэш в настройках (основной путь)
    try:
        stored_hash = db.get_password_hash()
        if stored_hash and check_password_hash(stored_hash, password):
            return True, 'admin'
    except Exception:
        pass
    # Backward-compat отключён по умолчанию
    return False, 'guest'


# Удалены неиспользуемые JWT-хелперы


