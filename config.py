import os
from dotenv import load_dotenv


load_dotenv()


class Config:
    SECRET_KEY = os.environ.get('SECRET_KEY', 'wb-irrigation-secret')
    WTF_CSRF_ENABLED = True
    WTF_CSRF_CHECK_DEFAULT = False  # по умолчанию не проверяем CSRF для API-запросов
    WTF_CSRF_TIME_LIMIT = None
    # Прочие настройки
    EMERGENCY_STOP = False
    TESTING = os.environ.get('TESTING') == '1'


class TestConfig(Config):
    TESTING = True
    WTF_CSRF_ENABLED = False


