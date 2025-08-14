from typing import Optional
from database import db
from werkzeug.security import check_password_hash
import jwt
from datetime import datetime, timedelta
from flask import current_app


def verify_admin(password: str) -> bool:
    stored_hash = db.get_password_hash()
    return bool(stored_hash and check_password_hash(stored_hash, password))


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


