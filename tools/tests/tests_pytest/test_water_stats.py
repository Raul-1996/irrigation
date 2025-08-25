from database import db


def test_water_stats_non_crashing(client):
    # Add sample water usage and verify API shape
    zones = db.get_zones() or []
    if zones:
        db.add_water_usage(zones[0]['id'], 1.23)
    r = client.get('/api/water')
    assert r.status_code == 200
    data = r.get_json()
    assert isinstance(data, dict)

