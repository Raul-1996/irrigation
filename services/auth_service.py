from typing import Optional
import os
import logging
from database import db
from werkzeug.security import check_password_hash, generate_password_hash
import threading
import sqlite3

logger = logging.getLogger(__name__)


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
    # Проверка хэша пароля из БД
    try:
        stored_hash = db.get_password_hash()
        if stored_hash and check_password_hash(stored_hash, password):
            return True, 'admin'
    except (sqlite3.Error, OSError) as e:
        logger.debug("Password check failed: %s", e)

    return False, 'guest'


# Удалены неиспользуемые JWT-хелперы


