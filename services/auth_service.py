from typing import Optional
from database import db
from werkzeug.security import check_password_hash
import jwt
from datetime import datetime, timedelta
from flask import current_app


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
    if verify_admin(password):
        return True, 'admin'
    elif verify_user(password):
        return True, 'user'
    else:
        return False, 'guest'


def create_jwt(role: str = 'admin', expires_minutes: int = 60) -> str:
    payload = {
        'role': role,
        'exp': datetime.utcnow() + timedelta(minutes=expires_minutes),
        'iat': datetime.utcnow(),
    }
    return jwt.encode(payload, current_app.config['SECRET_KEY'], algorithm='HS256')


def decode_jwt(token: str) -> Optional[dict]:
    try:
        return jwt.decode(token, current_app.config['SECRET_KEY'], algorithms=['HS256'])
    except Exception:
        return None


