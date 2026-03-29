"""Global test configuration and fixtures for WB-Irrigation."""
import os
import sys
import pytest

# Ensure the project root is on sys.path
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

# Set TESTING env var early
os.environ['TESTING'] = '1'

# Re-export fixtures from fixtures/ modules
from tests.fixtures.database import test_db_path, test_db
from tests.fixtures.mqtt import mock_mqtt_client, mock_mqtt_module, patched_mqtt
from tests.fixtures.app import app, client, admin_client, viewer_client, guest_client


@pytest.fixture(autouse=True)
def _set_testing_env():
    """Ensure TESTING=1 is set for all tests."""
    old = os.environ.get('TESTING')
    os.environ['TESTING'] = '1'
    yield
    if old is None:
        os.environ.pop('TESTING', None)
    else:
        os.environ['TESTING'] = old


@pytest.fixture
def sample_zone_data():
    """Sample zone data for creating test zones."""
    return {
        'name': 'Тест Зона 1',
        'duration': 15,
        'group_id': 1,
        'icon': '🌱',
        'topic': '/devices/wb-mr6cv3_1/controls/K1',
    }


@pytest.fixture
def sample_program_data():
    """Sample program data for creating test programs."""
    return {
        'name': 'Утренний полив',
        'time': '06:00',
        'days': [0, 2, 4],  # Mon, Wed, Fri
        'zones': [1, 2],
    }


@pytest.fixture
def sample_mqtt_server_data():
    """Sample MQTT server data."""
    return {
        'name': 'Test MQTT',
        'host': '127.0.0.1',
        'port': 1883,
        'username': '',
        'password': '',
        'enabled': 1,
    }
