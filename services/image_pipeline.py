"""Single image-optimization pipeline used by every upload handler.

Issue #49: every uploaded image (zone photo, zones map, future uploaders)
goes through ``optimize_uploaded_image`` so we always:

* honour EXIF orientation,
* enforce a 50 MP decompression-bomb cap,
* convert to RGB,
* downscale the long edge to ``max_dim`` (default 2400 px),
* encode as WebP lossy q=95 / method=6,
* strip metadata (Pillow does not copy EXIF when ``exif`` kwarg is absent).

Heavy/varargs handlers (e.g. zones photo that needs main + 400x400 thumb)
share the decode-and-guard step via :func:`load_safe_image`; the encode
step is :func:`encode_webp`.
"""

from __future__ import annotations

import io
import logging

from PIL import Image, ImageOps

logger = logging.getLogger(__name__)

# Decompression-bomb guard (also enforced in zones_photo_api for the
# two-variant path — keep the cap consistent across uploaders).
MAX_INPUT_PIXELS = 50_000_000

DEFAULT_MAX_DIM = 2400
DEFAULT_WEBP_QUALITY = 95
DEFAULT_WEBP_METHOD = 6


class ImageTooLargeError(ValueError):
    """Raised when an input image exceeds the pixel-count safety cap."""


def load_safe_image(file_data: bytes) -> Image.Image:
    """Decode bytes -> Pillow Image, applying EXIF rotation and pixel cap.

    Returns an RGB image (mode == "RGB"). Raises ImageTooLargeError if
    the input would exceed MAX_INPUT_PIXELS pixels. Other Pillow/IO
    errors propagate to the caller.
    """
    img = Image.open(io.BytesIO(file_data))
    img.load()  # force decode so PIL raises here, not later
    w0, h0 = img.size
    if w0 * h0 > MAX_INPUT_PIXELS:
        raise ImageTooLargeError(f"image too large: {w0}x{h0} ({w0 * h0} px) exceeds {MAX_INPUT_PIXELS}")
    try:
        img = ImageOps.exif_transpose(img)
    except (ValueError, TypeError, OSError) as e:
        logger.debug("load_safe_image: exif_transpose ignored: %s", e)
    if img.mode in ("RGBA", "LA", "P") or img.mode != "RGB":
        img = img.convert("RGB")
    return img


def encode_webp(
    img: Image.Image,
    *,
    quality: int = DEFAULT_WEBP_QUALITY,
    method: int = DEFAULT_WEBP_METHOD,
) -> bytes:
    """Encode an RGB Pillow Image to WebP lossy bytes (no metadata)."""
    out = io.BytesIO()
    img.save(out, format="WEBP", quality=quality, method=method)
    return out.getvalue()


def optimize_uploaded_image(
    file_data: bytes,
    *,
    max_dim: int = DEFAULT_MAX_DIM,
) -> tuple[bytes, str]:
    """Optimize any uploaded image to WebP, downscaled to ``max_dim``.

    Returns ``(webp_bytes, ".webp")`` so callers can write to disk with
    the canonical extension. Raises ImageTooLargeError on >50 MP input;
    other Pillow/IO errors propagate.
    """
    img = load_safe_image(file_data)
    w, h = img.size
    if max(w, h) > max_dim:
        scale = max_dim / float(max(w, h))
        img = img.resize(
            (max(1, int(w * scale)), max(1, int(h * scale))),
            Image.Resampling.LANCZOS,
        )
    return encode_webp(img), ".webp"
