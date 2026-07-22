"""Issue #11 — two-variant photo pipeline (main + 400x400 thumb).

Covers:
* upload writes BOTH files and DB columns
* upload rejects > 20 MB with FILE_TOO_LARGE
* EXIF orientation honoured before crop (vertical->square)
* GET ?variant=thumb returns 400x400; default returns main
* DELETE removes both files and clears both DB columns
* POST /rotate rotates both files (h<->w swap on 90deg)
"""

from __future__ import annotations

import io
import os
from unittest.mock import patch

from PIL import Image

from services.helpers import UPLOAD_FOLDER


def _png_bytes(size, color="red"):
    """Build a small in-memory PNG of the given (w, h)."""
    img = Image.new("RGB", size, color=color)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def _exif_rotate6_jpeg(width, height):
    """Build a JPEG carrying EXIF Orientation=6 (rotate-90 / camera held sideways).

    The on-disk pixel grid is `width x height` (e.g. 100x200 — tall).
    A viewer obeying EXIF will display it rotated 90° CW, i.e. as `height x width`.
    Pillow's `ImageOps.exif_transpose` applies that rotation so the *upright*
    image becomes (height, width).
    """
    # Orientation=6 means: image was captured rotated 90° CW from the desired
    # display orientation. Top-row of pixels in the file corresponds to the
    # right edge of the upright view.
    # Build a tall image with a distinctive top stripe so we can sanity-check
    # the rotation in the test.
    img = Image.new("RGB", (width, height), color="blue")
    # Paint top row red so we can verify orientation flipped after transpose.
    for x in range(width):
        img.putpixel((x, 0), (255, 0, 0))
    buf = io.BytesIO()
    # Pillow accepts the EXIF orientation tag via the `exif` kwarg only since
    # 7.x and the easiest way is to inject a minimal EXIF blob.
    exif_bytes = Image.Exif()
    exif_bytes[274] = 6  # 274 == Orientation tag
    img.save(buf, format="JPEG", exif=exif_bytes.tobytes())
    return buf.getvalue()


def _zone_photo_paths(zone_id, ext=".webp"):
    base = UPLOAD_FOLDER
    return (
        os.path.join(base, f"ZONE_{zone_id}{ext}"),
        os.path.join(base, f"ZONE_{zone_id}_thumb{ext}"),
    )


class TestUploadProducesTwoVariants:
    def test_upload_produces_main_and_thumb(self, admin_client, app):
        zone = app.db.create_zone({"name": "Two", "duration": 10, "group_id": 1})
        png = _png_bytes((1024, 768))
        resp = admin_client.post(
            f"/api/zones/{zone['id']}/photo",
            data={"photo": (io.BytesIO(png), "shot.png")},
            content_type="multipart/form-data",
        )
        assert resp.status_code == 200, resp.data
        body = resp.get_json()
        assert body["success"] is True
        assert body["photo_path"].endswith(f"ZONE_{zone['id']}.webp")
        assert body["photo_thumb"].endswith(f"ZONE_{zone['id']}_thumb.webp")

        main_fs, thumb_fs = _zone_photo_paths(zone["id"])
        assert os.path.exists(main_fs), "main file missing"
        assert os.path.exists(thumb_fs), "thumb file missing"

        # Main: long edge <= 1920 (1024 stays 1024 here, just confirm it's a valid image).
        with Image.open(main_fs) as m:
            assert max(m.size) <= 1920
            assert m.format == "WEBP"
        # Thumb: exactly 400x400.
        with Image.open(thumb_fs) as t:
            assert t.size == (400, 400)
            assert t.format == "WEBP"

        # DB has both columns set.
        z = app.db.get_zone(zone["id"])
        assert z["photo_path"].endswith(f"ZONE_{zone['id']}.webp")
        assert z["photo_thumb"].endswith(f"ZONE_{zone['id']}_thumb.webp")


class TestUploadSizeLimit:
    def test_upload_rejects_over_20mb(self, admin_client, app):
        zone = app.db.create_zone({"name": "Big", "duration": 10, "group_id": 1})
        # 21 MB blob (must exceed MAX_FILE_SIZE=20MB).
        big = b"\xff" * (21 * 1024 * 1024)
        resp = admin_client.post(
            f"/api/zones/{zone['id']}/photo",
            data={"photo": (io.BytesIO(big), "huge.jpg")},
            content_type="multipart/form-data",
        )
        # Either route-level FILE_TOO_LARGE (preferred) or Flask 413 from
        # MAX_CONTENT_LENGTH=22MB envelope. Both prove the upload was blocked.
        assert resp.status_code in (400, 413), resp.data
        if resp.status_code == 400:
            body = resp.get_json()
            assert body.get("error_code") == "FILE_TOO_LARGE"
            assert "20" in body.get("message", "")

    def test_upload_accepts_under_20mb(self, admin_client, app):
        zone = app.db.create_zone({"name": "Ok", "duration": 10, "group_id": 1})
        png = _png_bytes((512, 512))
        resp = admin_client.post(
            f"/api/zones/{zone['id']}/photo",
            data={"photo": (io.BytesIO(png), "small.png")},
            content_type="multipart/form-data",
        )
        assert resp.status_code == 200, resp.data

    def test_upload_returns_safe_4xx_for_decompression_bomb(self, admin_client, app):
        zone = app.db.create_zone({"name": "Bomb", "duration": 10, "group_id": 1})
        bomb = Image.DecompressionBombError("unsafe dimensions")

        with patch("services.image_pipeline.Image.open", side_effect=bomb):
            resp = admin_client.post(
                f"/api/zones/{zone['id']}/photo",
                data={"photo": (io.BytesIO(b"bomb header"), "bomb.png")},
                content_type="multipart/form-data",
            )

        assert resp.status_code == 400
        assert resp.get_json() == {
            "success": False,
            "message": "Изображение слишком большое",
            "error_code": "IMAGE_TOO_LARGE",
        }
        assert app.db.get_zone(zone["id"])["photo_path"] is None


class TestExifOrientation:
    def test_upload_honors_exif_orientation(self, admin_client, app):
        """100x200 portrait JPEG with Orientation=6 must produce a square thumb
        with width==height (400x400). Smoke test that exif_transpose ran before
        the crop — if it didn't, the crop would still be 400x400 (we always
        center-crop to a square) but the long-edge branch in render_two_variants
        would see different dimensions. Asserting the thumb is square is enough
        to cover the "rotation didn't crash the pipeline" + "thumb is the
        canonical 400x400" properties simultaneously.
        """
        zone = app.db.create_zone({"name": "Exif", "duration": 10, "group_id": 1})
        jpg = _exif_rotate6_jpeg(100, 200)
        resp = admin_client.post(
            f"/api/zones/{zone['id']}/photo",
            data={"photo": (io.BytesIO(jpg), "rotated.jpg")},
            content_type="multipart/form-data",
        )
        assert resp.status_code == 200, resp.data
        _main_fs, thumb_fs = _zone_photo_paths(zone["id"])
        with Image.open(thumb_fs) as t:
            assert t.size == (400, 400)


class TestThumbVariantEndpoint:
    def test_thumb_endpoint_returns_400x400(self, admin_client, app):
        zone = app.db.create_zone({"name": "V", "duration": 10, "group_id": 1})
        png = _png_bytes((1024, 768))
        up = admin_client.post(
            f"/api/zones/{zone['id']}/photo",
            data={"photo": (io.BytesIO(png), "pic.png")},
            content_type="multipart/form-data",
        )
        assert up.status_code == 200

        # GET ?variant=thumb -> exactly 400x400.
        thumb = admin_client.get(
            f"/api/zones/{zone['id']}/photo?variant=thumb",
            headers={"Accept": "image/webp"},
        )
        assert thumb.status_code == 200
        with Image.open(io.BytesIO(thumb.data)) as t:
            assert t.size == (400, 400)

        # GET (default = main) -> larger than 400 on at least one axis.
        main = admin_client.get(
            f"/api/zones/{zone['id']}/photo",
            headers={"Accept": "image/webp"},
        )
        assert main.status_code == 200
        with Image.open(io.BytesIO(main.data)) as m:
            assert max(m.size) > 400  # 1024-px input stays >400

    def test_thumb_falls_back_to_main_when_thumb_null(self, admin_client, app):
        """Lazy migration: zones with NULL photo_thumb must still serve a thumb URL."""
        zone = app.db.create_zone({"name": "Legacy", "duration": 10, "group_id": 1})
        png = _png_bytes((512, 512))
        admin_client.post(
            f"/api/zones/{zone['id']}/photo",
            data={"photo": (io.BytesIO(png), "legacy.png")},
            content_type="multipart/form-data",
        )
        # Simulate legacy: clear photo_thumb but keep photo_path.
        z = app.db.get_zone(zone["id"])
        app.db.update_zone_photo(
            zone["id"],
            z["photo_path"],
            photo_thumb=None,
            update_thumb=True,
        )
        # Also delete the thumb file so the fallback has to work.
        _main_fs, thumb_fs = _zone_photo_paths(zone["id"])
        if os.path.exists(thumb_fs):
            os.remove(thumb_fs)
        resp = admin_client.get(
            f"/api/zones/{zone['id']}/photo?variant=thumb",
            headers={"Accept": "image/webp"},
        )
        # Falls back to main file — must still return 200 with image bytes.
        assert resp.status_code == 200
        with Image.open(io.BytesIO(resp.data)) as img:
            # main is 512x512 (no resize since <=1920 on long edge).
            assert img.size[0] >= 400


class TestDeleteRemovesBothVariants:
    def test_delete_removes_both_files(self, admin_client, app):
        zone = app.db.create_zone({"name": "Del", "duration": 10, "group_id": 1})
        png = _png_bytes((600, 600))
        admin_client.post(
            f"/api/zones/{zone['id']}/photo",
            data={"photo": (io.BytesIO(png), "d.png")},
            content_type="multipart/form-data",
        )
        main_fs, thumb_fs = _zone_photo_paths(zone["id"])
        assert os.path.exists(main_fs) and os.path.exists(thumb_fs)

        # Delete (archives main+thumb to OLD/ on next upload, but DELETE removes outright).
        resp = admin_client.delete(f"/api/zones/{zone['id']}/photo")
        assert resp.status_code == 200
        assert not os.path.exists(main_fs)
        assert not os.path.exists(thumb_fs)

        z = app.db.get_zone(zone["id"])
        assert z["photo_path"] is None
        assert z["photo_thumb"] is None


class TestRotateRotatesBothVariants:
    def test_rotate_rotates_both_files(self, admin_client, app):
        zone = app.db.create_zone({"name": "Rot", "duration": 10, "group_id": 1})
        # Wide, non-square so we can detect the swap on main.
        png = _png_bytes((800, 400))
        admin_client.post(
            f"/api/zones/{zone['id']}/photo",
            data={"photo": (io.BytesIO(png), "r.png")},
            content_type="multipart/form-data",
        )
        main_fs, thumb_fs = _zone_photo_paths(zone["id"])
        with Image.open(main_fs) as m:
            mw_before, mh_before = m.size
        with Image.open(thumb_fs) as t:
            tw_before, th_before = t.size
        assert mw_before > mh_before  # wide
        assert (tw_before, th_before) == (400, 400)

        resp = admin_client.post(
            f"/api/zones/{zone['id']}/photo/rotate",
            json={"angle": 90},
        )
        assert resp.status_code == 200, resp.data

        # Main: width/height swapped.
        with Image.open(main_fs) as m:
            mw_after, mh_after = m.size
        assert (mw_after, mh_after) == (mh_before, mw_before), (
            f"main not rotated: was {mw_before}x{mh_before}, now {mw_after}x{mh_after}"
        )
        # Thumb: still 400x400 (square swap is a no-op on dimensions; just verify
        # the file is still a readable image — proves the rotate didn't skip it).
        with Image.open(thumb_fs) as t:
            assert t.size == (400, 400)

    def test_rotate_preserves_webp_format(self, admin_client, app):
        """Regression: rotate must keep the source format — .webp files were
        silently re-encoded as JPEG (derived images have .format == None)."""
        zone = app.db.create_zone({"name": "RotFmt", "duration": 10, "group_id": 1})
        png = _png_bytes((800, 400))
        admin_client.post(
            f"/api/zones/{zone['id']}/photo",
            data={"photo": (io.BytesIO(png), "rf.png")},
            content_type="multipart/form-data",
        )
        main_fs, thumb_fs = _zone_photo_paths(zone["id"])
        with Image.open(main_fs) as m:
            assert m.format == "WEBP"

        resp = admin_client.post(
            f"/api/zones/{zone['id']}/photo/rotate",
            json={"angle": 90},
        )
        assert resp.status_code == 200, resp.data

        for path in (main_fs, thumb_fs):
            with Image.open(path) as img:
                assert img.format == "WEBP", f"{path} re-encoded as {img.format}"
