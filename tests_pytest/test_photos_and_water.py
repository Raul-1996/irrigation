import io
from PIL import Image


def make_image_bytes():
    img = Image.new('RGB', (64, 64), color=(123, 20, 220))
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
    # upload
    data = {
        'photo': (io.BytesIO(make_image_bytes()), 'test.jpg')
    }
    r = client.post('/api/zones/1/photo', data=data, content_type='multipart/form-data')
    assert r.status_code in (200, 404)
    if r.status_code == 404:
        return

    payload = r.get_json()
    assert payload.get('success') is True
    # info
    info = client.get('/api/zones/1/photo')
    assert info.status_code == 200
    # delete
    d = client.delete('/api/zones/1/photo')
    assert d.status_code in (200, 404)
