import requests
import threading
import time


def test_zones_sse_smoke(client):
    # SSE endpoint should either return JSON error (no mqtt) or an SSE stream
    r = client.get('/api/mqtt/zones-sse')
    assert r.status_code == 200

