import io
import os
from PIL import Image


def make_image_bytes(idx: int = 0):
    # Try to read preloaded zone images from tools/tests/images.
    here = os.path.abspath(os.path.dirname(__file__))
    images_dir = os.path.abspath(os.path.join(here, os.pardir, 'images'))
    try:
        files = os.listdir(images_dir)
    except Exception:
        files = []
    # Prefer exact known patterns first
    candidates = []
    for ext in ['jpg','jpeg','png','webp','gif']:
        candidates.append(f"zone_{idx}.{ext}")
        candidates.append(f"{idx}_zone.{ext}")
        candidates.append(f"{idx} zone.{ext}")
        candidates.append(f"zone {idx}.{ext}")
        candidates.append(f"Zone{idx}.{ext}")
    for name in candidates:
        p = os.path.join(images_dir, name)
        if os.path.exists(p):
            with open(p, 'rb') as f:
                return f.read()
    # Fallback: any file which contains both 'zone' and the idx in name
    for fname in files:
        low = fname.lower()
        if 'zone' in low and str(idx) in low:
            p = os.path.join(images_dir, fname)
            try:
                with open(p, 'rb') as f:
                    return f.read()
            except Exception:
                pass
    # Fallback synthetic image
    img = Image.new('RGB', (128, 128), color=(123, 20, 220))
    buf = io.BytesIO()
    img.save(buf, format='JPEG')
    buf.seek(0)
    return buf.getvalue()


def test_water_endpoint(client):
    r = client.get('/api/water')
    assert r.status_code == 200
    data = r.get_json()
    assert isinstance(data, dict)


def test_photo_lifecycle(client):
    # upload images for first three zones if available
    for zid in (1, 2, 3):
        data = {
            'photo': (io.BytesIO(make_image_bytes(zid)), f'zone_{zid}.jpg')
        }
        r = client.post(f'/api/zones/{zid}/photo', data=data, content_type='multipart/form-data')
        assert r.status_code in (200, 404)
        if r.status_code == 404:
            continue
        payload = r.get_json()
        assert payload.get('success') is True
        info = client.get(f'/api/zones/{zid}/photo')
        assert info.status_code == 200

    # Single-zone delete test for zone 1
    r = client.delete('/api/zones/1/photo')
    assert r.status_code in (200, 404)
