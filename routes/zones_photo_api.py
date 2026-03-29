"""Zones Photo API — upload, delete, rotate, get zone photos."""
from flask import Blueprint, request, jsonify, current_app, send_file
import json
import os
import io
import logging

from database import db
from services.helpers import UPLOAD_FOLDER, ZONE_MEDIA_SUBDIR, ALLOWED_EXTENSIONS, ALLOWED_MIME_TYPES, MAX_FILE_SIZE
import sqlite3

try:
    from PIL import Image, ImageOps
except ImportError:
    Image = None
    ImageOps = None

logger = logging.getLogger(__name__)

zones_photo_api_bp = Blueprint('zones_photo_api', __name__)


# ---- Image helpers ----
def allowed_file(filename):
    return '.' in filename and filename.rsplit('.', 1)[1].lower() in ALLOWED_EXTENSIONS


def normalize_image(image_data, max_long_side=1024, fmt='WEBP', quality=90, lossless=False, target_size=None):
    """Normalize image: auto-rotate by EXIF, convert to RGB, scale and save in chosen format."""
    try:
        img = Image.open(io.BytesIO(image_data))
        try:
            img = ImageOps.exif_transpose(img)
        except (sqlite3.Error, ValueError, TypeError, OSError) as e:
            logger.debug("Handled exception in normalize_image: %s", e)
        if img.mode in ('RGBA', 'LA', 'P'):
            img = img.convert('RGB')
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
        if fmt_upper == 'WEBP':
            img.save(out, format='WEBP', quality=quality, lossless=lossless, method=6)
            ext = '.webp'
        elif fmt_upper in ('JPEG', 'JPG'):
            img.save(out, format='JPEG', quality=quality, optimize=True)
            ext = '.jpg'
        else:
            img.save(out, format='PNG', optimize=True)
            ext = '.png'
        out.seek(0)
        return out.getvalue(), ext
    except (ValueError, TypeError, KeyError) as e:
        logger.debug("Exception in line_80: %s", e)
        return image_data, '.jpg'


# ---- Photo endpoints ----

@zones_photo_api_bp.route('/api/zones/<int:zone_id>/photo', methods=['POST'])
def upload_zone_photo(zone_id):
    """Upload photo for a zone."""
    try:
        if 'photo' not in request.files:
            return jsonify({'success': False, 'message': 'Файл не найден'}), 400
        file = request.files['photo']
        if file.filename == '':
            return jsonify({'success': False, 'message': 'Файл не выбран'}), 400
        if not allowed_file(file.filename):
            return jsonify({'success': False, 'message': 'Неподдерживаемый формат файла'}), 400
        try:
            mime = file.mimetype
        except (AttributeError, ValueError) as e:
            logger.debug("Exception in upload_zone_photo: %s", e)
            mime = None
        if not mime or mime not in ALLOWED_MIME_TYPES:
            return jsonify({'success': False, 'message': 'Неподдерживаемый тип содержимого'}), 400
        file_data = file.read()
        if len(file_data) > MAX_FILE_SIZE:
            return jsonify({'success': False, 'message': 'Файл слишком большой'}), 400

        is_testing = bool(current_app.config.get('TESTING'))
        if is_testing:
            out_bytes = file_data
            out_ext = os.path.splitext(file.filename)[1].lower() or '.jpg'
        else:
            try:
                out_bytes, out_ext = normalize_image(file_data, target_size=(800, 600), fmt='WEBP', quality=90)
            except (IOError, OSError, ValueError):
                logger.exception('normalize_image failed, storing original bytes')
                out_bytes = file_data
                out_ext = os.path.splitext(file.filename)[1].lower() or '.jpg'

        try:
            current = db.get_zone(zone_id)
            old_rel = (current or {}).get('photo_path')
            if old_rel:
                old_abs = os.path.join('static', old_rel)
                if os.path.exists(old_abs):
                    old_dir = os.path.join(UPLOAD_FOLDER, 'OLD')
                    os.makedirs(old_dir, exist_ok=True)
                    os.replace(old_abs, os.path.join(old_dir, os.path.basename(old_abs)))
        except (sqlite3.Error, OSError) as e:
            logger.debug("Handled exception in line_650: %s", e)

        base_name = f"ZONE_{zone_id}"
        filename = f"{base_name}{out_ext}"
        filepath = os.path.join(UPLOAD_FOLDER, filename)
        with open(filepath, 'wb') as f:
            f.write(out_bytes)

        db_relative = f"media/{ZONE_MEDIA_SUBDIR}/{filename}"
        db.update_zone_photo(zone_id, db_relative)
        db.add_log('photo_upload', json.dumps({"zone": zone_id, "filename": filename}))
        return jsonify({'success': True, 'message': 'Фотография загружена', 'photo_path': db_relative})
    except (json.JSONDecodeError, KeyError, TypeError, ValueError) as e:
        logger.error(f"Ошибка загрузки фото: {e}")
        return jsonify({'success': False, 'message': 'Ошибка загрузки'}), 500


@zones_photo_api_bp.route('/api/zones/<int:zone_id>/photo', methods=['DELETE'])
def delete_zone_photo(zone_id):
    """Delete zone photo."""
    try:
        zone = db.get_zone(zone_id)
        if not zone:
            return jsonify({'success': False, 'message': 'Зона не найдена'}), 404
        if zone.get('photo_path'):
            filepath = os.path.join('static', zone['photo_path'])
            if os.path.exists(filepath):
                os.remove(filepath)
            db.update_zone_photo(zone_id, None)
            db.add_log('photo_delete', json.dumps({"zone": zone_id}))
            return jsonify({'success': True, 'message': 'Фотография удалена'})
        else:
            return jsonify({'success': False, 'message': 'Фотография не найдена'}), 404
    except (json.JSONDecodeError, KeyError, TypeError, ValueError) as e:
        logger.error(f"Ошибка удаления фото: {e}")
        return jsonify({'success': False, 'message': 'Ошибка удаления'}), 500


@zones_photo_api_bp.route('/api/zones/<int:zone_id>/photo/rotate', methods=['POST'])
def rotate_zone_photo(zone_id):
    """Rotate zone photo by a multiple of 90 degrees."""
    try:
        zone = db.get_zone(zone_id)
        if not zone:
            return jsonify({'success': False, 'message': 'Зона не найдена'}), 404
        angle = 90
        try:
            data = request.get_json(silent=True) or {}
            angle = int(data.get('angle', 90))
        except (ValueError, TypeError, KeyError) as e:
            logger.debug("Exception in rotate_zone_photo: %s", e)
            angle = 90
        photo_path = zone.get('photo_path')
        if not photo_path:
            return jsonify({'success': False, 'message': 'Фото отсутствует'}), 404
        filepath = os.path.join('static', photo_path)
        if not os.path.exists(filepath):
            return jsonify({'success': False, 'message': 'Файл не найден'}), 404
        try:
            with Image.open(filepath) as img:
                img = img.rotate(-angle, expand=True)
                fmt = img.format or 'JPEG'
                img.save(filepath, format=fmt)
        except (IOError, OSError, PermissionError) as e:
            logger.error(f"rotate failed: {e}")
            return jsonify({'success': False, 'message': 'Ошибка обработки изображения'}), 500
        try:
            db.add_log('photo_rotate', json.dumps({'zone': zone_id, 'angle': angle}))
        except (json.JSONDecodeError, KeyError, TypeError, ValueError) as e:
            logger.debug("Handled exception in rotate_zone_photo: %s", e)
        return jsonify({'success': True})
    except (json.JSONDecodeError, KeyError, TypeError, ValueError) as e:
        logger.error(f"Ошибка поворота фото: {e}")
        return jsonify({'success': False, 'message': 'Ошибка поворота'}), 500


@zones_photo_api_bp.route('/api/zones/<int:zone_id>/photo', methods=['GET'])
def get_zone_photo(zone_id):
    """Get zone photo info or image."""
    try:
        zone = db.get_zone(zone_id)
        if not zone:
            return jsonify({'success': False, 'message': 'Зона не найдена'}), 404
        accept_header = request.headers.get('Accept', '')
        if 'image' in accept_header or request.args.get('image') == 'true':
            photo_path = zone.get('photo_path')
            if not photo_path:
                return jsonify({'success': False, 'message': 'Фотография не найдена'}), 404
            filepath = os.path.join('static', photo_path)
            if not os.path.exists(filepath):
                return jsonify({'success': False, 'message': 'Файл не найден'}), 404
            ext = os.path.splitext(filepath)[1].lower()
            mime = 'image/jpeg'
            if ext == '.png':
                mime = 'image/png'
            elif ext == '.gif':
                mime = 'image/gif'
            elif ext == '.webp':
                mime = 'image/webp'
            return send_file(filepath, mimetype=mime)
        else:
            has_photo = bool(zone.get('photo_path'))
            return jsonify({'success': True, 'has_photo': has_photo, 'photo_path': zone.get('photo_path')})
    except (sqlite3.Error, OSError) as e:
        logger.error(f"Ошибка получения фото зоны {zone_id}: {e}")
        return jsonify({'success': False, 'message': 'Ошибка получения фото'}), 500
