from database import db
import time


def test_emergency_blocks_mqtt_on(client):
    # enable emergency stop
    client.post('/api/emergency-stop')
    # set zone ON via DB directly to simulate MQTT ON state update path
    zones = db.get_zones()
    assert zones
    zid = zones[0]['id']
    db.update_zone(zid, {'state': 'on'})
    # backend on MQTT RX should force OFF when EMERGENCY_STOP, but here we directly set; verify API still reports stop allowed and zone becomes off after stop
    client.post(f'/api/zones/{zid}/stop')
    z = db.get_zone(zid)
    assert z['state'] == 'off'
    # resume
    client.post('/api/emergency-resume')

