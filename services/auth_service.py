from typing import Optional
from database import db
from werkzeug.security import check_password_hash, generate_password_hash
import threading


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
            # Если хэш слишком тяжёлый — пере-хэшируем в фоне, чтобы не задерживать ответ
            def _rehash_bg(old_hash: str, plain: str):
                try:
                    if ':sha256:' in old_hash and 'pbkdf2' in old_hash and ('260000' in old_hash or '200000' in old_hash or '180000' in old_hash or '120000' in old_hash or '90000' in old_hash):
                        new_hash = generate_password_hash(plain, method='pbkdf2:sha256:60000')
                        db.set_setting_value('password_hash', new_hash)
                except Exception:
                    pass
            try:
                threading.Thread(target=_rehash_bg, args=(stored_hash, password), daemon=True).start()
            except Exception:
                pass
            return True, 'admin'
    except Exception:
        pass
    # Дополнительно: если хэш слишком «тяжёлый», можно пере-хэшировать при успешном входе
    try:
        if stored_hash and check_password_hash(stored_hash, password):
            # Normalize hash strength for weak CPUs (e.g., Wirenboard)
            if ':sha256:' in stored_hash and 'pbkdf2' in stored_hash and ('260000' in stored_hash or '200000' in stored_hash or '180000' in stored_hash):
                try:
                    new_hash = generate_password_hash(password, method='pbkdf2:sha256:90000')
                    db.set_setting_value('password_hash', new_hash)
                except Exception:
                    pass
    except Exception:
        pass
    # Backward-compat отключён по умолчанию
    return False, 'guest'


# Удалены неиспользуемые JWT-хелперы


