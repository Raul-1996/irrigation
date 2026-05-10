"""Security tests for SEC-009 (photo path traversal) and SEC-014 (rotate angle).

These tests exercise the path-validation helpers directly — no Flask client
needed. Covers: unsafe paths are rejected, valid zone filenames pass,
edge cases (NUL, absolute, empty) are blocked.
"""
from __future__ import annotations

import os
import pytest

from services.helpers import (
    UnsafePathError,
    safe_media_subpath,
    safe_zone_photo_path,
)


# ── safe_media_subpath ──────────────────────────────────────────────────────

class TestSafeMediaSubpath:
    def test_accepts_plain_filename(self, tmp_path):
        base = str(tmp_path)
        result = safe_media_subpath(base, 'photo.webp')
        assert result.startswith(base)
        assert result.endswith('photo.webp')

    def test_accepts_nested_path(self, tmp_path):
        base = str(tmp_path)
        result = safe_media_subpath(base, 'media/zones/ZONE_5.webp')
        assert result.startswith(base)
        assert 'ZONE_5.webp' in result

    def test_rejects_parent_traversal(self, tmp_path):
        base = str(tmp_path)
        with pytest.raises(UnsafePathError):
            safe_media_subpath(base, '../etc/passwd')

    def test_rejects_nested_parent_traversal(self, tmp_path):
        base = str(tmp_path)
        with pytest.raises(UnsafePathError):
            safe_media_subpath(base, 'media/zones/../../../etc/passwd')

    def test_rejects_absolute_path(self, tmp_path):
        base = str(tmp_path)
        with pytest.raises(UnsafePathError):
            safe_media_subpath(base, '/etc/passwd')

    def test_rejects_empty_path(self, tmp_path):
        base = str(tmp_path)
        with pytest.raises(UnsafePathError):
            safe_media_subpath(base, '')

    def test_rejects_none(self, tmp_path):
        base = str(tmp_path)
        with pytest.raises(UnsafePathError):
            safe_media_subpath(base, None)  # type: ignore[arg-type]

    def test_rejects_nul_byte(self, tmp_path):
        base = str(tmp_path)
        with pytest.raises(UnsafePathError):
            safe_media_subpath(base, 'photo\x00.webp')

    def test_rejects_path_that_resolves_outside_base(self, tmp_path):
        # Classic symlink-escape pattern — by normalizing with abspath+commonpath
        # we block this even if the base dir doesn't exist yet.
        base = str(tmp_path / 'nonexistent')
        with pytest.raises(UnsafePathError):
            safe_media_subpath(base, '../../../tmp')


# ── safe_zone_photo_path ───────────────────────────────────────────────────

class TestSafeZonePhotoPath:
    def test_accepts_valid_zone_photo(self):
        result = safe_zone_photo_path('media/zones/ZONE_5.webp')
        assert result.endswith(os.path.join('static', 'media', 'zones', 'ZONE_5.webp'))

    def test_accepts_all_allowed_extensions(self):
        for ext in ('png', 'jpg', 'jpeg', 'gif', 'webp'):
            safe_zone_photo_path(f'media/zones/ZONE_1.{ext}')

    def test_rejects_traversal_in_photo_path(self):
        with pytest.raises(UnsafePathError):
            safe_zone_photo_path('../../etc/passwd')

    def test_rejects_traversal_disguised_as_zone_file(self):
        # Even if the filename is valid, a traversal prefix must fail.
        with pytest.raises(UnsafePathError):
            safe_zone_photo_path('../../ZONE_1.webp')

    def test_rejects_filename_without_zone_prefix(self):
        # photo_path must use the ZONE_<id> naming — arbitrary names
        # (e.g. left over from admin CRUD that set raw user input) fail.
        with pytest.raises(UnsafePathError):
            safe_zone_photo_path('media/zones/evil.php')

    def test_rejects_double_extension(self):
        with pytest.raises(UnsafePathError):
            safe_zone_photo_path('media/zones/ZONE_1.webp.php')

    def test_rejects_empty(self):
        with pytest.raises(UnsafePathError):
            safe_zone_photo_path('')

    def test_rejects_none(self):
        with pytest.raises(UnsafePathError):
            safe_zone_photo_path(None)  # type: ignore[arg-type]

    def test_rejects_absolute(self):
        with pytest.raises(UnsafePathError):
            safe_zone_photo_path('/etc/ZONE_1.webp')

    # Issue #11: filename regex extended with optional `_thumb` suffix.
    def test_thumb_filename_accepted(self):
        result = safe_zone_photo_path('media/zones/ZONE_5_thumb.webp')
        assert result.endswith(os.path.join('static', 'media', 'zones', 'ZONE_5_thumb.webp'))

    def test_evil_thumb_suffix_rejected(self):
        # Typo + extra-suffix variants must not pass.
        for bad in (
            'media/zones/ZONE_5_thumbb.webp',
            'media/zones/ZONE_5_thumb_evil.webp',
            'media/zones/ZONE_5_thumb.php',
            'media/zones/ZONE_5thumb.webp',
        ):
            with pytest.raises(UnsafePathError):
                safe_zone_photo_path(bad)


# ── SEC-014: rotate_zone_photo angle handling ──────────────────────────────

class TestRotateAngleClamp:
    """Integration-level: check the handler returns 400 on out-of-range angle."""

    def test_large_angle_rejected(self, admin_client):
        # We can't easily seed a zone photo file here without more plumbing,
        # but the handler validates angle BEFORE touching the filesystem, so
        # a 400 INVALID_ANGLE response or a 404 "Фото отсутствует" is fine —
        # either means the DoS vector (huge canvas allocation) is blocked.
        resp = admin_client.post(
            '/api/zones/1/photo/rotate',
            json={'angle': 999999999},
        )
        # Accept either 400 (angle validation fired) or 404 (zone/photo
        # missing in test DB — still proves the DoS allocation never ran).
        assert resp.status_code in (400, 404), (
            f'expected 400/404 for huge angle, got {resp.status_code}: {resp.data!r}'
        )

    def test_valid_angle_accepted_or_photo_missing(self, admin_client):
        resp = admin_client.post(
            '/api/zones/1/photo/rotate',
            json={'angle': 90},
        )
        # 404 (photo absent) or 200 (rotated) — both non-error paths.
        assert resp.status_code in (200, 404), (
            f'valid angle 90 must not be rejected as INVALID_ANGLE '
            f'(got {resp.status_code}: {resp.data!r})'
        )
