"""
Tests for zone_runs tracking in database — create, get, finish runs.
"""
import os
import sys
import time
import pytest
from datetime import datetime

_HERE = os.path.abspath(os.path.dirname(__file__))
_ROOT = os.path.abspath(os.path.join(_HERE, os.pardir, os.pardir, os.pardir))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from database import IrrigationDB


@pytest.fixture
def db(tmp_path):
    path = str(tmp_path / 'runs_test.db')
    d = IrrigationDB(db_path=path)
    d.init_database()
    # Seed a group and zone
    d.create_group('Pump')
    groups = d.get_groups()
    gid = groups[0]['id']
    d.create_zone({
        'name': 'Zone 1', 'icon': '🌱', 'duration': 5,
        'group_id': gid, 'topic': '/test/k1', 'mqtt_server_id': 0
    })
    return d


class TestZoneRuns:
    def test_create_zone_run(self, db):
        zones = db.get_zones()
        zid = zones[0]['id']
        groups = db.get_groups()
        gid = groups[0]['id']

        now = datetime.utcnow().isoformat()
        mono = time.monotonic()
        run_id = db.create_zone_run(zid, gid, now, mono, program_id=None, raw_pulses=0)
        assert run_id is not None
        assert isinstance(run_id, int)

    def test_get_open_zone_run(self, db):
        zones = db.get_zones()
        zid = zones[0]['id']
        groups = db.get_groups()
        gid = groups[0]['id']

        now = datetime.utcnow().isoformat()
        mono = time.monotonic()
        run_id = db.create_zone_run(zid, gid, now, mono, program_id=None, raw_pulses=0)

        run = db.get_open_zone_run(zid)
        assert run is not None
        assert run['id'] == run_id

    def test_finish_zone_run(self, db):
        zones = db.get_zones()
        zid = zones[0]['id']
        groups = db.get_groups()
        gid = groups[0]['id']

        now = datetime.utcnow().isoformat()
        mono = time.monotonic()
        run_id = db.create_zone_run(zid, gid, now, mono, program_id=None, raw_pulses=0)

        end_time = datetime.utcnow().isoformat()
        end_mono = time.monotonic()
        db.finish_zone_run(run_id, end_time, end_mono, end_raw_pulses=10, liters=5.0)

        # Should be closed now
        run = db.get_open_zone_run(zid)
        assert run is None

    def test_no_open_run(self, db):
        zones = db.get_zones()
        zid = zones[0]['id']
        run = db.get_open_zone_run(zid)
        assert run is None
