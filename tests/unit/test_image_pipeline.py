"""Issue #49 — unit tests for the unified image optimization helper."""

from __future__ import annotations

import io

import pytest
from PIL import Image

from services.image_pipeline import (
    ImageTooLargeError,
    encode_webp,
    load_safe_image,
    optimize_uploaded_image,
)


def _png_bytes(size, color="red"):
    img = Image.new("RGB", size, color=color)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


class TestOptimizeUploadedImage:
    def test_large_png_becomes_smaller_webp(self):
        """AC#5 unit: large PNG -> smaller WebP, format == WEBP.

        Uses fine-grained per-pixel noise so PNG's deflate has nothing to
        latch on to — mirrors the real photo case from the issue (5.34 MB
        camera PNG -> 0.6 MB WebP). Without true noise PNG can beat WebP
        on synthetic banded gradients, which would say nothing about the
        production payload behaviour.
        """
        import random

        rng = random.Random(0)
        side = 1500  # 2.25 MP — large enough for a meaningful PNG->WebP test
        big = Image.new("RGB", (side, side))
        px = big.load()
        for y in range(side):
            for x in range(side):
                px[x, y] = (rng.randint(0, 255), rng.randint(0, 255), rng.randint(0, 255))
        buf = io.BytesIO()
        big.save(buf, format="PNG")
        src = buf.getvalue()

        # Use a tight max_dim so we also exercise the downscale path on
        # this expensive test (avoids building a 3000-side image just for
        # the resize assertion).
        out, ext = optimize_uploaded_image(src, max_dim=1024)

        assert ext == ".webp"
        assert len(out) < len(src), f"WebP {len(out)} not smaller than PNG {len(src)}"
        with Image.open(io.BytesIO(out)) as result:
            assert result.format == "WEBP"
            assert max(result.size) == 1024

    def test_small_image_not_upscaled(self):
        """Images already smaller than max_dim keep their dimensions."""
        src = _png_bytes((512, 384))
        out, ext = optimize_uploaded_image(src)
        with Image.open(io.BytesIO(out)) as r:
            assert r.size == (512, 384)
            assert r.format == "WEBP"
        assert ext == ".webp"

    def test_max_dim_override_honoured(self):
        """Caller can pick a tighter max_dim (e.g. 1920 for the zones main variant)."""
        src = _png_bytes((4000, 2000))
        out, _ = optimize_uploaded_image(src, max_dim=1920)
        with Image.open(io.BytesIO(out)) as r:
            assert max(r.size) == 1920

    def test_aspect_ratio_preserved_on_downscale(self):
        src = _png_bytes((4800, 2400))  # 2:1
        out, _ = optimize_uploaded_image(src, max_dim=2400)
        with Image.open(io.BytesIO(out)) as r:
            w, h = r.size
            assert (w, h) == (2400, 1200)

    def test_rgba_input_flattened_to_rgb(self):
        """PNGs with alpha must be encoded without crashing (WebP supports RGBA,
        but the pipeline flattens to RGB so the on-disk size stays small)."""
        img = Image.new("RGBA", (256, 256), (255, 0, 0, 128))
        buf = io.BytesIO()
        img.save(buf, format="PNG")
        out, _ = optimize_uploaded_image(buf.getvalue())
        with Image.open(io.BytesIO(out)) as r:
            assert r.mode == "RGB"

    def test_metadata_stripped(self):
        """EXIF block must not leak into the encoded WebP."""
        img = Image.new("RGB", (400, 400), color="green")
        exif = Image.Exif()
        exif[274] = 1  # Orientation
        exif[315] = "test-author"  # Artist — easy to detect on output
        buf = io.BytesIO()
        img.save(buf, format="JPEG", exif=exif.tobytes())

        out, _ = optimize_uploaded_image(buf.getvalue())

        with Image.open(io.BytesIO(out)) as r:
            # Pillow returns an empty Exif() object when no metadata is present.
            assert dict(r.getexif()) == {}

    def test_exif_orientation_applied(self):
        """Orientation=6 means the file pixels are 90° CW from the upright view.
        Pipeline must rotate before downscale so callers always see upright pixels.
        """
        img = Image.new("RGB", (100, 200), color="blue")  # tall portrait
        exif = Image.Exif()
        exif[274] = 6  # rotate-90 on display
        buf = io.BytesIO()
        img.save(buf, format="JPEG", exif=exif.tobytes())

        out, _ = optimize_uploaded_image(buf.getvalue())

        with Image.open(io.BytesIO(out)) as r:
            # After exif_transpose 100x200 -> 200x100 (landscape).
            assert r.size == (200, 100)

    def test_pixel_cap_rejects_over_50mp(self):
        """Decompression-bomb guard: 50 MP cap (matches zones_photo behaviour)."""
        # 8000x7000 = 56 MP — over the cap.
        huge = _png_bytes((8000, 7000), color="white")
        with pytest.raises(ImageTooLargeError):
            optimize_uploaded_image(huge)

    def test_pixel_cap_accepts_at_or_under_50mp(self):
        # 7000x7000 = 49 MP — under cap.
        ok = _png_bytes((7000, 7000), color="white")
        out, _ = optimize_uploaded_image(ok)
        assert len(out) > 0


class TestLoadSafeImage:
    def test_returns_rgb_image(self):
        img = load_safe_image(_png_bytes((200, 200)))
        assert img.mode == "RGB"
        assert img.size == (200, 200)

    def test_raises_on_oversize(self):
        with pytest.raises(ImageTooLargeError):
            load_safe_image(_png_bytes((8000, 7000)))

    def test_raises_on_garbage_input(self):
        # Pillow raises UnidentifiedImageError (subclass of OSError) — propagate.
        with pytest.raises(Exception):  # noqa: B017 — interface test, exact class is Pillow-private
            load_safe_image(b"definitely not an image")


class TestEncodeWebp:
    def test_round_trip(self):
        img = Image.new("RGB", (300, 200), color="cyan")
        data = encode_webp(img)
        with Image.open(io.BytesIO(data)) as r:
            assert r.format == "WEBP"
            assert r.size == (300, 200)

    def test_quality_param_affects_size(self):
        # Content with detail so quality actually matters.
        img = Image.new("RGB", (800, 600))
        px = img.load()
        for y in range(600):
            for x in range(800):
                px[x, y] = ((x * 13) % 255, (y * 17) % 255, ((x + y) * 5) % 255)
        small = encode_webp(img, quality=20)
        large = encode_webp(img, quality=95)
        assert len(small) < len(large)
