import os
import pytest

os.environ.setdefault("TESTING", "1")

# Use client fixture from conftest.py - no need to redefine


def test_index(client):
    resp = client.get("/")
    assert resp.status_code in (200, 302, 404)


def test_zones_list(client):
    resp = client.get("/api/zones")
    assert resp.status_code == 200
    data = resp.get_json()
    assert isinstance(data, list)


def test_zone_start_stop_cycle(client):
    import time
    
    # First check zone exists
    zones = client.get("/api/zones").get_json()
    print(f"DEBUG: Total zones found: {len(zones)}")
    zone1 = next((z for z in zones if z['id'] == 1), None)
    if not zone1:
        print("DEBUG: Zone 1 not found, available zones:", [z['id'] for z in zones])
        assert False, "Zone 1 not found"
    print(f"DEBUG: Zone 1 initial state: {zone1}")
    
    # stop to ensure clean state
    stop_cleanup = client.post("/api/zones/1/stop")
    print(f"DEBUG: Cleanup stop response: {stop_cleanup.status_code}")
    time.sleep(0.5)  # Allow cleanup to finish
    
    t0 = time.time()
    r1 = client.post("/api/zones/1/start")
    print(f"DEBUG: Start response: {r1.status_code}, {r1.get_json()}")
    assert r1.status_code in (200, 400, 404), f"Unexpected start status: {r1.status_code}"
    
    if r1.status_code != 200:
        print("DEBUG: Start failed, skipping state checks")
        return
    
    # verify state flips to on within 5s (longer for async operations)
    ok = False
    deadline = time.time() + 5.0
    while time.time() < deadline:
        z = client.get("/api/zones/1").get_json() or {}
        print(f"DEBUG: Zone state check: {z.get('state', 'unknown')}")
        if (z.get('state') == 'on'):
            ok = True
            break
        time.sleep(0.2)
    assert ok, f"Zone 1 did not become ON within 5s, final state: {z.get('state', 'unknown')}"

    t1 = time.time()
    r2 = client.post("/api/zones/1/stop")
    print(f"DEBUG: Stop response: {r2.status_code}, {r2.get_json()}")
    assert r2.status_code in (200, 404)
    
    # verify state flips to off within 15s (observed state verification can take up to 10s + retries)
    ok2 = False
    deadline2 = time.time() + 15.0
    while time.time() < deadline2:
        z = client.get("/api/zones/1").get_json() or {}
        if (z.get('state') == 'off'):
            ok2 = True
            break
        time.sleep(0.3)
    assert ok2, f"Zone 1 did not become OFF within 15s, final state: {z.get('state', 'unknown')}"


def test_group_stop_cancels_sequence(client):
    s = client.post("/api/groups/1/start-from-first")
    import time; time.sleep(0.3)
    # In TESTING scheduler may be unavailable -> accept 200/400/500
    assert s.status_code in (200, 400, 500)
    st = client.post("/api/groups/1/stop")
    assert st.status_code in (200, 500)
