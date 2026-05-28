"""Issue #49 — POST /api/map runs uploads through the shared image pipeline.

Verifies that:
* a big PNG map upload lands on disk as ``.webp``,
* the saved file is smaller than the source,
* the response path points at the new ``.webp`` filename,
* a small image still works (regression on the happy path),
* undecodable bytes return a structured 400 (no garbage on disk),
* GET /api/map surfaces the re-encoded ``.webp`` in its listing.
"""

from __future__ import annotations

import io
import os
import random

from PIL import Image

from services.helpers import MAP_DIR


def _random_png_bytes(size):
    """Build a PNG full of fine per-pixel noise — incompressible for deflate,
    so WebP q=95 + downscale visibly wins.
    """
    rng = random.Random(0)
    img = Image.new("RGB", size)
    px = img.load()
    for y in range(size[1]):
        for x in range(size[0]):
            px[x, y] = (rng.randint(0, 255), rng.randint(0, 255), rng.randint(0, 255))
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def _small_png_bytes(size, color="red"):
    img = Image.new("RGB", size, color=color)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def _list_map_files():
    return [f for f in os.listdir(MAP_DIR) if f.startswith("zones_map_")]


class TestMapUploadCompressed:
    def test_large_png_saved_as_smaller_webp(self, admin_client):
        """AC#2 + AC#5 API: POST big PNG -> server stores .webp smaller than input."""
        before = set(_list_map_files())
        # ~1500x1500 random noise ≈ several hundred KB PNG. Small enough for a
        # 30 s pytest timeout but real enough to compress meaningfully.
        src = _random_png_bytes((1500, 1500))
        resp = admin_client.post(
            "/api/map",
            data={"file": (io.BytesIO(src), "big_map.png")},
            content_type="multipart/form-data",
        )
        assert resp.status_code == 200, resp.data
        body = resp.get_json()
        assert body["success"] is True
        assert body["path"].endswith(".webp"), f"expected .webp path, got {body['path']}"

        # Exactly one new file in MAP_DIR, and it ends with .webp.
        after = set(_list_map_files())
        new = after - before
        assert len(new) == 1
        new_name = next(iter(new))
        assert new_name.endswith(".webp")

        # On-disk size strictly less than the source PNG.
        on_disk = os.path.getsize(os.path.join(MAP_DIR, new_name))
        assert on_disk < len(src), f"webp on disk {on_disk} >= source {len(src)}"

        # Response path matches the actual file.
        assert body["path"] == f"media/maps/{new_name}"

    def test_oversized_png_kept_within_max_dim(self, admin_client):
        """Upload a 3000x2000 image — server must downscale to <=2400 long edge."""
        # Solid colour so the PNG itself is tiny; we only care about pixel dims.
        src = _small_png_bytes((3000, 2000), color="orange")
        resp = admin_client.post(
            "/api/map",
            data={"file": (io.BytesIO(src), "wide_map.png")},
            content_type="multipart/form-data",
        )
        assert resp.status_code == 200, resp.data
        body = resp.get_json()
        new_name = os.path.basename(body["path"])
        with Image.open(os.path.join(MAP_DIR, new_name)) as img:
            assert img.format == "WEBP"
            assert max(img.size) == 2400, f"long edge not clamped: {img.size}"

    def test_small_jpeg_still_uploads(self, admin_client):
        """Regression: a tiny image must still succeed (no false-positive size guard)."""
        img = Image.new("RGB", (200, 200), color="blue")
        buf = io.BytesIO()
        img.save(buf, format="JPEG")
        resp = admin_client.post(
            "/api/map",
            data={"file": (io.BytesIO(buf.getvalue()), "tiny.jpg")},
            content_type="multipart/form-data",
        )
        assert resp.status_code == 200, resp.data
        body = resp.get_json()
        # Even a tiny JPEG gets re-encoded to .webp because the pipeline is uniform.
        assert body["path"].endswith(".webp")


class TestMapUploadInvalid:
    def test_garbage_bytes_return_400(self, admin_client):
        """Bytes that pass extension+MIME guard but Pillow cannot decode must
        produce a structured 400 (not 500, not silent save of garbage)."""
        before = set(_list_map_files())
        resp = admin_client.post(
            "/api/map",
            data={"file": (io.BytesIO(b"this is not an image at all"), "fake.png")},
            content_type="multipart/form-data",
        )
        assert resp.status_code == 400, resp.data
        body = resp.get_json()
        assert body.get("success") is False
        assert body.get("error_code") == "IMAGE_PROCESSING_FAILED"
        # And critically: nothing got persisted to disk.
        assert set(_list_map_files()) == before


class TestMapListIncludesWebp:
    def test_listing_returns_uploaded_webp(self, admin_client):
        """GET /api/map must surface the re-encoded .webp filename so the UI
        can render the freshly uploaded map without a manual refresh path."""
        src = _small_png_bytes((300, 300))
        up = admin_client.post(
            "/api/map",
            data={"file": (io.BytesIO(src), "list-me.png")},
            content_type="multipart/form-data",
        )
        assert up.status_code == 200, up.data
        uploaded_name = os.path.basename(up.get_json()["path"])
        assert uploaded_name.endswith(".webp")

        resp = admin_client.get("/api/map")
        assert resp.status_code == 200
        names = [it["name"] for it in resp.get_json()["items"]]
        assert uploaded_name in names
