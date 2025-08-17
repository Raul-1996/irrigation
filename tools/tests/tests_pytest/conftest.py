import os
import sys
import pytest

os.environ.setdefault("TESTING", "1")

# Ensure project root on path (…/irrigation)
_HERE = os.path.abspath(os.path.dirname(__file__))
_ROOT = os.path.abspath(os.path.join(_HERE, os.pardir, os.pardir, os.pardir))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from app import app  # noqa: E402
from database import db  # noqa: E402
import sqlite3
import json

def _reset_seed_data():
    # Drop and recreate DB schema with desired defaults
    conn = sqlite3.connect('irrigation.db')
    c = conn.cursor()
    for tbl in ['zones','groups','programs','logs','water_usage','mqtt_servers','settings']:
        try:
            c.execute(f'DELETE FROM {tbl}')
        except Exception:
            pass
    # One group
    c.execute("INSERT INTO groups(id,name) VALUES(1,'носос 1')")
    # MQTT server
    c.execute("INSERT INTO mqtt_servers(id,name,host,port,enabled) VALUES(1,'local','127.0.0.1',1883,1)")
    # 30 zones, duration 1, topics 101..105/K1..K6
    zones = []
    for zid in range(1,31):
        dev = 101 + (zid-1)//6
        ch = 1 + (zid-1)%6
        topic = f"/devices/wb-mr6cv3_{dev}/controls/K{ch}"
        zones.append((zid,'off',f'Зона {zid}','🌿',1,1,topic,1))
    c.executemany("INSERT INTO zones(id,state,name,icon,duration,group_id,topic,mqtt_server_id) VALUES(?,?,?,?,?,?,?,?)", zones)
    # two programs 04:00 and 20:00 with all zones
    all_z = json.dumps(list(range(1,31)))
    days = json.dumps([0,1,2,3,4,5,6])
    c.execute("INSERT INTO programs(id,name,time,days,zones) VALUES(1,'Утренний','04:00',?,?)", (days, all_z))
    c.execute("INSERT INTO programs(id,name,time,days,zones) VALUES(2,'Вечерний','20:00',?,?)", (days, all_z))
    # password default
    try:
        from werkzeug.security import generate_password_hash
        c.execute("INSERT OR REPLACE INTO settings(key,value) VALUES('password_hash',?)", (generate_password_hash('1234', method='pbkdf2:sha256'),))
    except Exception:
        pass
    conn.commit()
    conn.close()

@pytest.fixture(autouse=True)
def ensure_db():
    # Force initialization by accessing DB
    db.get_zones()
    _reset_seed_data()
    yield

@pytest.fixture()
def client():
    app.config.update(TESTING=True)
    with app.test_client() as c:
        yield c
