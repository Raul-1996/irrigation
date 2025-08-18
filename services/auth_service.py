from typing import Optional
from database import db
from werkzeug.security import check_password_hash


def verify_admin(password: str) -> bool:
    """Проверка пароля администратора (8888)"""
    return password == '8888'


def verify_user(password: str) -> bool:
    """Проверка пароля обычного пользователя (1234)"""
    return password == '1234'


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
    # Backward-compat: поддержка старых фиксированных паролей в тестовых сценариях
    if verify_admin(password):
        return True, 'admin'
    if verify_user(password):
        return True, 'user'
    return False, 'guest'


# Удалены неиспользуемые JWT-хелперы


