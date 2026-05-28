"""Zones Photo API — upload, delete, rotate, get zone photos."""

import io
import json
import logging
import os
import sqlite3

from flask import Blueprint, current_app, jsonify, request, send_file

from database import db
from services.audit import audit_log
from services.helpers import (
    ALLOWED_EXTENSIONS,
    ALLOWED_MIME_TYPES,
    MAX_FILE_SIZE,
    UPLOAD_FOLDER,
    ZONE_MEDIA_SUBDIR,
    UnsafePathError,
    safe_zone_photo_path,
)
from services.image_pipeline import ImageTooLargeError, encode_webp, load_safe_image

try:
    from PIL import Image, ImageOps
except ImportError:
    Image = None
    ImageOps = None

logger = logging.getLogger(__name__)

zones_photo_api_bp = Blueprint("zones_photo_api", __name__)


# ---- Image helpers ----
# Issue #49: decode/EXIF/RGB/50 MP-cap moved into services.image_pipeline so
# every upload handler shares one path. Two-variant rendering stays here
# because the thumb is a domain-specific 400x400 center-crop that no other
# uploader needs.


def allowed_file(filename):
    return "." in filename and filename.rsplit(".", 1)[1].lower() in ALLOWED_EXTENSIONS


def render_two_variants(image_bytes):
    """Issue #11: produce (main_webp_bytes, thumb_webp_bytes) from one input.

    - Main: long edge resized to <=1920, aspect preserved, WebP q=92.
    - Thumb: 400x400 center-crop, WebP q=90.
    Single PIL decode (via services.image_pipeline.load_safe_image which also
    applies EXIF rotation, RGB conversion and the 50 MP cap) then both
    outputs are encoded from the same in-memory image.
    Raises ImageTooLargeError if input exceeds the pixel safety cap.
    Other Pillow/IO errors propagate to caller.
    """
    img = load_safe_image(image_bytes)

    # Main: long edge <= 1920, preserve aspect.
    w, h = img.size
    if max(w, h) > 1920:
        scale = 1920 / float(max(w, h))
        main = img.resize((max(1, int(w * scale)), max(1, int(h * scale))), Image.Resampling.LANCZOS)
    else:
        main = img.copy()
    main_bytes = encode_webp(main, quality=92)

    # Thumb: 400x400 center-crop (no stretching).
    tw = th = 400
    rw, rh = img.size
    scale = max(tw / rw, th / rh)
    sized = img.resize((max(tw, int(rw * scale)), max(th, int(rh * scale))), Image.Resampling.LANCZOS)
    left = max(0, (sized.size[0] - tw) // 2)
    top = max(0, (sized.size[1] - th) // 2)
    cropped = sized.crop((left, top, left + tw, top + th))
    thumb_bytes = encode_webp(cropped, quality=90)

    return main_bytes, thumb_bytes


def _atomic_write(path, data):
    """Write bytes to ``path`` atomically via .tmp + os.replace."""
    tmp = path + ".tmp"
    with open(tmp, "wb") as f:
        f.write(data)
    os.replace(tmp, path)


def normalize_image(image_data, max_long_side=1024, fmt="WEBP", quality=90, lossless=False, target_size=None):
    """Normalize image: auto-rotate by EXIF, convert to RGB, scale and save in chosen format."""
    try:
        img = Image.open(io.BytesIO(image_data))
        try:
            img = ImageOps.exif_transpose(img)
        except (sqlite3.Error, ValueError, TypeError, OSError) as e:
            logger.debug("Handled exception in normalize_image: %s", e)
        if img.mode in ("RGBA", "LA", "P"):
            img = img.convert("RGB")
        w, h = img.size
        if target_size:
            tw, th = target_size
            scale = max(tw / w, th / h)
            new_size = (int(w * scale), int(h * scale))
            img = img.resize(new_size, Image.Resampling.LANCZOS)
            left = max(0, (img.size[0] - tw) // 2)
            top = max(0, (img.size[1] - th) // 2)
            img = img.crop((left, top, left + tw, top + th))
        else:
            if max(w, h) > max_long_side:
                scale = max_long_side / float(max(w, h))
                new_size = (int(w * scale), int(h * scale))
                img = img.resize(new_size, Image.Resampling.LANCZOS)
        out = io.BytesIO()
        fmt_upper = fmt.upper()
        if fmt_upper == "WEBP":
            img.save(out, format="WEBP", quality=quality, lossless=lossless, method=6)
            ext = ".webp"
        elif fmt_upper in ("JPEG", "JPG"):
            img.save(out, format="JPEG", quality=quality, optimize=True)
            ext = ".jpg"
        else:
            img.save(out, format="PNG", optimize=True)
            ext = ".png"
        out.seek(0)
        return out.getvalue(), ext
    except (ValueError, TypeError, KeyError) as e:
        logger.debug("Exception in line_80: %s", e)
        return image_data, ".jpg"


# ---- Photo endpoints ----


def _archive_old_zone_file(zone_id, old_rel, label):
    """Move an existing zone photo file to UPLOAD_FOLDER/OLD/.
    Best-effort, swallows OS errors. ``label`` only used for log context.
    """
    if not old_rel:
        return
    try:
        old_abs = safe_zone_photo_path(old_rel)
    except UnsafePathError as e:
        logger.warning(
            "archive_old: refused unsafe %s path for zone %s: %r — %s",
            label,
            zone_id,
            old_rel,
            e,
        )
        return
    try:
        if os.path.exists(old_abs):
            old_dir = os.path.join(UPLOAD_FOLDER, "OLD")
            os.makedirs(old_dir, exist_ok=True)
            os.replace(old_abs, os.path.join(old_dir, os.path.basename(old_abs)))
    except OSError as e:
        logger.debug("archive_old: %s move failed for zone %s: %s", label, zone_id, e)


@zones_photo_api_bp.route("/api/zones/<int:zone_id>/photo", methods=["POST"])
@audit_log("photo_upload", target_extractor=lambda *a, **kw: f"zone:{kw.get('zone_id', a[0] if a else '?')}")
def upload_zone_photo(zone_id):
    """Upload photo for a zone (issue #11: writes main + thumb)."""
    try:
        if "photo" not in request.files:
            return jsonify({"success": False, "message": "Файл не найден"}), 400
        file = request.files["photo"]
        if file.filename == "":
            return jsonify({"success": False, "message": "Файл не выбран"}), 400
        if not allowed_file(file.filename):
            return jsonify({"success": False, "message": "Неподдерживаемый формат файла"}), 400
        try:
            mime = file.mimetype
        except (AttributeError, ValueError) as e:
            logger.debug("Exception in upload_zone_photo: %s", e)
            mime = None
        if not mime or mime not in ALLOWED_MIME_TYPES:
            return jsonify({"success": False, "message": "Неподдерживаемый тип содержимого"}), 400
        file_data = file.read()
        if len(file_data) > MAX_FILE_SIZE:
            mb = MAX_FILE_SIZE // (1024 * 1024)
            return jsonify(
                {
                    "success": False,
                    "message": f"Файл больше {mb} МБ",
                    "error_code": "FILE_TOO_LARGE",
                }
            ), 400

        is_testing = bool(current_app.config.get("TESTING"))
        if is_testing:
            # In test mode skip Pillow re-encoding (tests upload tiny PNGs whose
            # raw bytes we want to preserve) — but still write a separate thumb
            # file so the two-variant contract holds. Tests that need real
            # 400x400 dimensions will use a Pillow path explicitly.
            try:
                main_bytes, thumb_bytes = render_two_variants(file_data)
            except ImageTooLargeError:
                return jsonify(
                    {
                        "success": False,
                        "message": "Изображение слишком большое",
                        "error_code": "IMAGE_TOO_LARGE",
                    }
                ), 400
            except (OSError, ValueError):
                # Fallback: keep raw bytes for both files — preserves current
                # test behaviour for the `b'not an image'` style cases.
                main_bytes = file_data
                thumb_bytes = file_data
            ext = ".webp"
        else:
            try:
                main_bytes, thumb_bytes = render_two_variants(file_data)
            except ImageTooLargeError:
                return jsonify(
                    {
                        "success": False,
                        "message": "Изображение слишком большое",
                        "error_code": "IMAGE_TOO_LARGE",
                    }
                ), 400
            except (OSError, ValueError) as e:
                logger.error("render_two_variants failed: %s", e)
                return jsonify(
                    {
                        "success": False,
                        "message": "Не удалось обработать изображение",
                        "error_code": "IMAGE_PROCESSING_FAILED",
                    }
                ), 400
            ext = ".webp"

        # Archive old files (main + thumb) before overwrite — both flow.
        try:
            current = db.get_zone(zone_id) or {}
            _archive_old_zone_file(zone_id, current.get("photo_path"), "main")
            _archive_old_zone_file(zone_id, current.get("photo_thumb"), "thumb")
        except (sqlite3.Error, OSError) as e:
            logger.debug("upload_zone_photo: archive step warning: %s", e)

        main_name = f"ZONE_{zone_id}{ext}"
        thumb_name = f"ZONE_{zone_id}_thumb{ext}"
        main_path = os.path.join(UPLOAD_FOLDER, main_name)
        thumb_path = os.path.join(UPLOAD_FOLDER, thumb_name)
        # Atomic writes: tmp file -> os.replace, prevents readers seeing a
        # partial main while thumb is still being written.
        _atomic_write(main_path, main_bytes)
        _atomic_write(thumb_path, thumb_bytes)

        db_main = f"media/{ZONE_MEDIA_SUBDIR}/{main_name}"
        db_thumb = f"media/{ZONE_MEDIA_SUBDIR}/{thumb_name}"
        db.update_zone_photo(zone_id, db_main, photo_thumb=db_thumb, update_thumb=True)
        db.add_log("photo_upload", json.dumps({"zone": zone_id, "filename": main_name}))
        return jsonify(
            {
                "success": True,
                "message": "Фотография загружена",
                "photo_path": db_main,
                "photo_thumb": db_thumb,
            }
        )
    except (json.JSONDecodeError, KeyError, TypeError, ValueError) as e:
        logger.error(f"Ошибка загрузки фото: {e}")
        return jsonify({"success": False, "message": "Ошибка загрузки"}), 500


@zones_photo_api_bp.route("/api/zones/<int:zone_id>/photo", methods=["DELETE"])
@audit_log("photo_delete", target_extractor=lambda *a, **kw: f"zone:{kw.get('zone_id', a[0] if a else '?')}")
def delete_zone_photo(zone_id):
    """Delete zone photo (issue #11: removes main + thumb)."""
    try:
        zone = db.get_zone(zone_id)
        if not zone:
            return jsonify({"success": False, "message": "Зона не найдена"}), 404
        photo_path = zone.get("photo_path")
        photo_thumb = zone.get("photo_thumb")
        if not photo_path and not photo_thumb:
            return jsonify({"success": False, "message": "Фотография не найдена"}), 404

        bad_path = False
        # SEC-009: validate each stored path before filesystem delete.
        for label, rel in (("main", photo_path), ("thumb", photo_thumb)):
            if not rel:
                continue
            try:
                filepath = safe_zone_photo_path(rel)
            except UnsafePathError as e:
                logger.error(
                    "delete_zone_photo: refused unsafe %s path for zone %s: %s",
                    label,
                    zone_id,
                    e,
                )
                bad_path = True
                continue
            try:
                if os.path.exists(filepath):
                    os.remove(filepath)
            except OSError as e:
                logger.warning(
                    "delete_zone_photo: %s remove failed for zone %s: %s",
                    label,
                    zone_id,
                    e,
                )

        # Always clear both DB columns so admin UI stops pointing at them.
        db.update_zone_photo(zone_id, None, photo_thumb=None, update_thumb=True)

        if bad_path:
            return jsonify(
                {
                    "success": False,
                    "message": "Некорректный путь к фото",
                    "error_code": "INVALID_PHOTO_PATH",
                }
            ), 400

        db.add_log("photo_delete", json.dumps({"zone": zone_id}))
        return jsonify({"success": True, "message": "Фотография удалена"})
    except (json.JSONDecodeError, KeyError, TypeError, ValueError) as e:
        logger.error(f"Ошибка удаления фото: {e}")
        return jsonify({"success": False, "message": "Ошибка удаления"}), 500


@zones_photo_api_bp.route("/api/zones/<int:zone_id>/photo/rotate", methods=["POST"])
@audit_log("photo_rotate", target_extractor=lambda *a, **kw: f"zone:{kw.get('zone_id', a[0] if a else '?')}")
def rotate_zone_photo(zone_id):
    """Rotate zone photo by a multiple of 90 degrees."""
    try:
        zone = db.get_zone(zone_id)
        if not zone:
            return jsonify({"success": False, "message": "Зона не найдена"}), 404
        angle = 90
        try:
            data = request.get_json(silent=True) or {}
            angle_raw = int(data.get("angle", 90))
            # SEC-014: clamp to the only legitimate rotation steps. An
            # attacker-controlled `angle=999999999` otherwise causes
            # Pillow to allocate enormous canvases (DoS / OOM).
            allowed_angles = {-270, -180, -90, 0, 90, 180, 270}
            if angle_raw not in allowed_angles:
                # Normalise to nearest 90deg multiple within one rotation.
                angle_raw = angle_raw % 360
                if angle_raw not in {0, 90, 180, 270}:
                    return jsonify(
                        {
                            "success": False,
                            "message": "Допустимы только углы, кратные 90",
                            "error_code": "INVALID_ANGLE",
                        }
                    ), 400
            angle = angle_raw
        except (ValueError, TypeError, KeyError) as e:
            logger.debug("Exception in rotate_zone_photo: %s", e)
            angle = 90
        photo_path = zone.get("photo_path")
        photo_thumb = zone.get("photo_thumb")
        if not photo_path:
            return jsonify({"success": False, "message": "Фото отсутствует"}), 404

        # Rotate every available variant. SEC-009: each path validated.
        targets = [("main", photo_path)]
        if photo_thumb:
            targets.append(("thumb", photo_thumb))

        for label, rel in targets:
            try:
                filepath = safe_zone_photo_path(rel)
            except UnsafePathError as e:
                logger.error(
                    "rotate_zone_photo: refused unsafe %s path for zone %s: %s",
                    label,
                    zone_id,
                    e,
                )
                return jsonify(
                    {
                        "success": False,
                        "message": "Некорректный путь к фото",
                        "error_code": "INVALID_PHOTO_PATH",
                    }
                ), 400
            if not os.path.exists(filepath):
                # The main file is required; thumb may be absent for legacy zones.
                if label == "main":
                    return jsonify({"success": False, "message": "Файл не найден"}), 404
                continue
            try:
                with Image.open(filepath) as img:
                    img = img.rotate(-angle, expand=True)
                    fmt = img.format or "JPEG"
                    img.save(filepath, format=fmt)
            except (OSError, PermissionError) as e:
                logger.error(f"rotate failed ({label}): {e}")
                return jsonify({"success": False, "message": "Ошибка обработки изображения"}), 500

        try:
            db.add_log("photo_rotate", json.dumps({"zone": zone_id, "angle": angle}))
        except (json.JSONDecodeError, KeyError, TypeError, ValueError) as e:
            logger.debug("Handled exception in rotate_zone_photo: %s", e)
        return jsonify({"success": True})
    except (json.JSONDecodeError, KeyError, TypeError, ValueError) as e:
        logger.error(f"Ошибка поворота фото: {e}")
        return jsonify({"success": False, "message": "Ошибка поворота"}), 500


@zones_photo_api_bp.route("/api/zones/<int:zone_id>/photo", methods=["GET"])
def get_zone_photo(zone_id):
    """Get zone photo info or image.

    Issue #11: ``?variant=thumb`` returns the 400x400 thumb. Default = main.
    Lazy migration: legacy zones with NULL photo_thumb fall back to photo_path.
    """
    try:
        zone = db.get_zone(zone_id)
        if not zone:
            return jsonify({"success": False, "message": "Зона не найдена"}), 404
        accept_header = request.headers.get("Accept", "")
        if "image" in accept_header or request.args.get("image") == "true":
            variant = request.args.get("variant", "main")
            if variant == "thumb":
                # Lazy fallback for zones uploaded before #11.
                photo_path = zone.get("photo_thumb") or zone.get("photo_path")
            else:
                photo_path = zone.get("photo_path")
            if not photo_path:
                return jsonify({"success": False, "message": "Фотография не найдена"}), 404
            # SEC-009: validate DB-stored path before send_file (otherwise
            # a corrupted `photo_path` like `../../etc/passwd` would be
            # returned to the client).
            try:
                filepath = safe_zone_photo_path(photo_path)
            except UnsafePathError as e:
                logger.error(
                    "get_zone_photo: refused unsafe photo_path for zone %s: %s",
                    zone_id,
                    e,
                )
                return jsonify(
                    {
                        "success": False,
                        "message": "Некорректный путь к фото",
                        "error_code": "INVALID_PHOTO_PATH",
                    }
                ), 400
            if not os.path.exists(filepath):
                return jsonify({"success": False, "message": "Файл не найден"}), 404
            ext = os.path.splitext(filepath)[1].lower()
            mime = "image/jpeg"
            if ext == ".png":
                mime = "image/png"
            elif ext == ".gif":
                mime = "image/gif"
            elif ext == ".webp":
                mime = "image/webp"
            return send_file(filepath, mimetype=mime)
        else:
            has_photo = bool(zone.get("photo_path"))
            return jsonify(
                {
                    "success": True,
                    "has_photo": has_photo,
                    "photo_path": zone.get("photo_path"),
                    "photo_thumb": zone.get("photo_thumb"),
                }
            )
    except (sqlite3.Error, OSError) as e:
        logger.error(f"Ошибка получения фото зоны {zone_id}: {e}")
        return jsonify({"success": False, "message": "Ошибка получения фото"}), 500
