"""System Config API — auth, password, rain, env, map, postpone, settings."""

import contextlib
import fcntl
import json
import logging
import os
import secrets
import sqlite3
import stat
import threading
import time
from datetime import datetime, timedelta

from flask import Blueprint, abort, current_app, jsonify, redirect, request, send_file, session, url_for
from werkzeug.security import check_password_hash
from werkzeug.utils import secure_filename

from constants import MIN_PASSWORD_LENGTH
from database import db
from irrigation_scheduler import get_scheduler
from services.api_rate_limiter import rate_limit
from services.audit import audit_log
from services.helpers import ALLOWED_MIME_TYPES
from services.image_pipeline import ImageTooLargeError, optimize_uploaded_image
from services.monitors import env_monitor, probe_env_values
from services.mqtt_pub import publish_mqtt_value as _publish_mqtt_value
from utils import normalize_topic

try:
    import paho.mqtt.client as mqtt
except ImportError:
    mqtt = None

logger = logging.getLogger(__name__)

system_config_api_bp = Blueprint("system_config_api", __name__)

# Password blocklist (TASK-013)
_PASSWORD_BLOCKLIST = {"1234", "12345678", "0000", "password", "admin", "qwerty"}

# Maps are full-size images stored on the controller's limited flash.  Keep
# both the API response and the on-disk collection bounded to the same newest
# set so a long-running installation cannot grow this directory indefinitely.
MAX_MAP_FILES = 20
MAP_TEMP_STALE_SECONDS = 60 * 60
_MAP_EXTENSIONS = frozenset({".png", ".jpg", ".jpeg", ".gif", ".webp"})
_MAP_MIME_TYPES = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".webp": "image/webp",
}
_TRUSTED_MAP_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "static", "media", "maps"))
_MAP_STORAGE_LOCK = threading.RLock()


# ===== Auth / Password =====


@system_config_api_bp.route("/api/auth/status")
def api_auth_status():
    return jsonify(
        {
            "authenticated": bool(session.get("logged_in")) or bool(current_app.config.get("TESTING")),
            "role": session.get("role", "guest"),
        }
    )


@system_config_api_bp.route("/logout", methods=["GET", "POST"])
@audit_log("logout", target_extractor=lambda *a, **kw: "session")
def api_logout():
    """Terminate the current session.

    Security fixes:
      * SEC-007: `session.clear()` fully destroys the server-side session
        payload AND forces Flask to rotate the signed cookie. The previous
        implementation left `logged_in=False` but kept role='user' which
        (via the `_is_status_action` whitelist) still allowed mutating
        calls on zone/group control endpoints.
      * SEC-008: GET-based logout was CSRF-able (e.g. `<img src=/logout>`
        in email/IM would forcibly log admin out). The GET variant is
        preserved for backward compatibility with the existing link in
        the sidebar template, but mutating side-effects are now identical
        and the cookie is rotated. New integrations should POST to
        `/logout`.
    """
    # Capture whether the session had any sign-in so audit logs make
    # sense, but never log the role/user because that is PII-adjacent.
    was_logged_in = bool(session.get("logged_in"))
    session.clear()
    # Force-invalidate any Flask-Session server-side entry too, if present.
    with contextlib.suppress(AttributeError, TypeError):
        session.modified = True
    logger.info("logout: session cleared (was_logged_in=%s)", was_logged_in)
    return redirect(url_for("auth_bp.login_page"))


@system_config_api_bp.route("/api/password", methods=["POST"])
@rate_limit("password_change", max_requests=3, window_sec=300)
@audit_log("password_change", target_extractor=lambda *a, **kw: "admin", payload_filter=lambda p: {"changed": True})
def api_change_password():
    try:
        if not session.get("logged_in") and not current_app.config.get("TESTING"):
            return jsonify({"success": False, "message": "Требуется аутентификация"}), 401
        data = request.get_json() or {}
        old_password = data.get("old_password", "")
        new_password = data.get("new_password", "")
        if len(new_password) < MIN_PASSWORD_LENGTH:
            return jsonify(
                {"success": False, "message": f"Пароль должен быть не менее {MIN_PASSWORD_LENGTH} символов"}
            ), 400
        if len(new_password) > 32:
            return jsonify({"success": False, "message": "Пароль не может быть длиннее 32 символов"}), 400
        if not new_password:
            return jsonify({"success": False, "message": "Новый пароль обязателен"}), 400
        if new_password.lower() in _PASSWORD_BLOCKLIST:
            return jsonify({"success": False, "message": "Этот пароль слишком простой. Выберите другой."}), 400
        stored_hash = db.get_password_hash()
        if stored_hash and (current_app.config.get("TESTING") or check_password_hash(stored_hash, old_password)):
            if db.set_password(new_password):
                return jsonify({"success": True})
            return jsonify({"success": False, "message": "Не удалось обновить пароль"}), 500
        return jsonify({"success": False, "message": "Старый пароль неверен"}), 400
    except (sqlite3.Error, OSError) as e:
        logger.error(f"Ошибка смены пароля: {e}")
        return jsonify({"success": False, "message": "Ошибка смены пароля"}), 500


# ===== Map =====


def _map_open_flags(*, directory: bool = False) -> int:
    flags = os.O_RDONLY
    if directory:
        flags |= getattr(os, "O_DIRECTORY", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    flags |= getattr(os, "O_CLOEXEC", 0)
    return flags


@contextlib.contextmanager
def _open_map_directory(*, create: bool) -> tuple[str, int]:
    """Open the fixed map directory without following directory symlinks.

    The in-process lock protects threads.  ``flock`` on the already verified
    directory descriptor extends that serialization to multiple application
    processes while every operation keeps using ``dir_fd`` relative syscalls.
    """

    with _MAP_STORAGE_LOCK:
        map_directory = os.path.abspath(_TRUSTED_MAP_DIR)
        if os.path.realpath(map_directory) != map_directory:
            raise OSError("map directory or one of its parents is a symlink")
        if create:
            os.makedirs(map_directory, mode=0o755, exist_ok=True)
        directory_info = os.lstat(map_directory)
        if stat.S_ISLNK(directory_info.st_mode) or not stat.S_ISDIR(directory_info.st_mode):
            raise OSError("map storage path is not a trusted directory")
        directory_fd = os.open(map_directory, _map_open_flags(directory=True))
        try:
            opened_info = os.fstat(directory_fd)
            if not stat.S_ISDIR(opened_info.st_mode):
                raise OSError("map storage descriptor is not a directory")
            fcntl.flock(directory_fd, fcntl.LOCK_EX)
            try:
                yield map_directory, directory_fd
            finally:
                fcntl.flock(directory_fd, fcntl.LOCK_UN)
        finally:
            os.close(directory_fd)


def _is_safe_map_filename(filename: str) -> bool:
    return (
        bool(filename)
        and secure_filename(filename) == filename
        and os.path.splitext(filename)[1].lower() in _MAP_EXTENSIONS
    )


def _entry_stat(directory_fd: int, filename: str) -> os.stat_result:
    return os.stat(filename, dir_fd=directory_fd, follow_symlinks=False)


def _remove_unsafe_entries_locked(directory_fd: int) -> None:
    removed = False
    try:
        for filename in os.listdir(directory_fd):
            try:
                item_stat = _entry_stat(directory_fd, filename)
            except FileNotFoundError:
                continue
            if stat.S_ISLNK(item_stat.st_mode):
                os.unlink(filename, dir_fd=directory_fd)
                removed = True
    finally:
        if removed:
            os.fsync(directory_fd)


def _list_map_items_locked(directory_fd: int) -> list[dict[str, object]]:
    items: list[dict[str, object]] = []
    for filename in os.listdir(directory_fd):
        if not _is_safe_map_filename(filename):
            continue
        try:
            item_stat = _entry_stat(directory_fd, filename)
        except FileNotFoundError:
            continue
        if not stat.S_ISREG(item_stat.st_mode):
            continue
        items.append(
            {
                "name": filename,
                "path": f"media/maps/{filename}",
                "mtime": item_stat.st_mtime,
            }
        )
    items.sort(key=lambda item: (float(item["mtime"]), str(item["name"])), reverse=True)
    return items


def _cleanup_stale_map_temps_locked(directory_fd: int) -> None:
    cutoff = time.time() - MAP_TEMP_STALE_SECONDS
    removed = False
    try:
        for filename in os.listdir(directory_fd):
            if not (filename.startswith(".zones_map_") and filename.endswith(".tmp")):
                continue
            try:
                item_stat = _entry_stat(directory_fd, filename)
            except FileNotFoundError:
                continue
            if stat.S_ISREG(item_stat.st_mode) and item_stat.st_mtime < cutoff:
                os.unlink(filename, dir_fd=directory_fd)
                removed = True
    finally:
        if removed:
            os.fsync(directory_fd)


def _prune_map_items_locked(directory_fd: int, *, new_filename: str | None = None) -> None:
    """Prune oldest maps, always protecting a just-published map."""

    items = _list_map_items_locked(directory_fd)
    if new_filename is None:
        keep_names = {str(item["name"]) for item in items[:MAX_MAP_FILES]}
    else:
        keep_names = {new_filename}
        prior_items = [item for item in items if item["name"] != new_filename]
        keep_names.update(str(item["name"]) for item in prior_items[: MAX_MAP_FILES - 1])
    # Oldest first makes a failed prune conservative and cannot remove the
    # newest item, including the map that triggered this retention pass.
    victims = [str(item["name"]) for item in reversed(items) if item["name"] not in keep_names]
    removed = False
    try:
        for filename in victims:
            os.unlink(filename, dir_fd=directory_fd)
            removed = True
    finally:
        if removed:
            os.fsync(directory_fd)


def _atomic_publish_map_locked(directory_fd: int, out_bytes: bytes, out_ext: str) -> str:
    extension = str(out_ext).lower()
    if extension not in _MAP_EXTENSIONS:
        raise ValueError("unsupported optimized map extension")
    unique = f"{time.time_ns()}_{secrets.token_hex(6)}"
    filename = f"zones_map_{unique}{extension}"
    temporary_name = f".zones_map_{unique}.tmp"
    temporary_fd: int | None = None
    try:
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
        flags |= getattr(os, "O_NOFOLLOW", 0)
        flags |= getattr(os, "O_CLOEXEC", 0)
        temporary_fd = os.open(temporary_name, flags, 0o644, dir_fd=directory_fd)
        with os.fdopen(temporary_fd, "wb") as temporary:
            temporary_fd = None
            temporary.write(out_bytes)
            temporary.flush()
            os.fchmod(temporary.fileno(), 0o644)
            os.fsync(temporary.fileno())
        os.replace(temporary_name, filename, src_dir_fd=directory_fd, dst_dir_fd=directory_fd)
        temporary_name = ""
        # Persist the new directory entry before any retention deletion.
        os.fsync(directory_fd)
        return filename
    finally:
        if temporary_fd is not None:
            os.close(temporary_fd)
        if temporary_name:
            with contextlib.suppress(FileNotFoundError):
                os.unlink(temporary_name, dir_fd=directory_fd)
                os.fsync(directory_fd)


def _store_map_bytes(out_bytes: bytes, out_ext: str) -> str:
    with _open_map_directory(create=True) as (_, directory_fd):
        _remove_unsafe_entries_locked(directory_fd)
        _cleanup_stale_map_temps_locked(directory_fd)
        filename = _atomic_publish_map_locked(directory_fd, out_bytes, out_ext)
        _prune_map_items_locked(directory_fd, new_filename=filename)
        return filename


def _open_safe_map_file(filename: str) -> tuple[object, str]:
    if not _is_safe_map_filename(filename):
        raise FileNotFoundError(filename)
    with _open_map_directory(create=False) as (_, directory_fd):
        try:
            item_stat = _entry_stat(directory_fd, filename)
        except FileNotFoundError:
            raise
        if stat.S_ISLNK(item_stat.st_mode):
            os.unlink(filename, dir_fd=directory_fd)
            os.fsync(directory_fd)
            raise FileNotFoundError(filename)
        if not stat.S_ISREG(item_stat.st_mode):
            raise FileNotFoundError(filename)
        file_fd = os.open(filename, _map_open_flags(), dir_fd=directory_fd)
        try:
            if not stat.S_ISREG(os.fstat(file_fd).st_mode):
                raise FileNotFoundError(filename)
        except BaseException:
            os.close(file_fd)
            raise
    extension = os.path.splitext(filename)[1].lower()
    return os.fdopen(file_fd, "rb"), _MAP_MIME_TYPES[extension]


def _serve_map_file(filename: str):
    try:
        file_handle, mimetype = _open_safe_map_file(filename)
    except FileNotFoundError:
        abort(404)
    try:
        response = send_file(
            file_handle,
            mimetype=mimetype,
            download_name=filename,
            conditional=False,
            max_age=60 * 60 * 24 * 7,
        )
    except BaseException:
        file_handle.close()
        raise
    response.call_on_close(file_handle.close)
    return response


@system_config_api_bp.before_app_request
def _serve_legacy_map_static_path():
    """Keep legacy map URLs without letting Flask follow map symlinks."""

    prefix = "/static/media/maps/"
    if not request.path.startswith(prefix):
        return None
    filename = request.path[len(prefix) :]
    if "/" in filename:
        abort(404)
    try:
        return _serve_map_file(filename)
    except OSError as exc:
        logger.error("map serving failed: %s", exc)
        abort(500)


@system_config_api_bp.route("/api/map/file/<string:filename>", methods=["GET"])
def api_map_file(filename: str):
    try:
        return _serve_map_file(filename)
    except OSError as exc:
        logger.error("map serving failed: %s", exc)
        return jsonify({"success": False, "message": "Ошибка работы с картой"}), 500


@system_config_api_bp.route("/api/map", methods=["GET", "POST"])
@audit_log("map_upload", target_extractor=lambda *a, **kw: "map")
def api_map():
    try:
        if request.method == "GET":
            with _open_map_directory(create=True) as (_, directory_fd):
                _remove_unsafe_entries_locked(directory_fd)
                _cleanup_stale_map_temps_locked(directory_fd)
                _prune_map_items_locked(directory_fd)
                items = _list_map_items_locked(directory_fd)
            return jsonify({"success": True, "items": items})
        else:
            if not (current_app.config.get("TESTING") or session.get("role") == "admin"):
                return jsonify({"success": False, "message": "Только администратор может загружать карты"}), 403
            if "file" not in request.files:
                return jsonify({"success": False, "message": "Файл не найден"}), 400
            file = request.files["file"]
            if file.filename == "":
                return jsonify({"success": False, "message": "Файл не выбран"}), 400
            ext = os.path.splitext(file.filename)[1].lower()
            if ext not in _MAP_EXTENSIONS:
                return jsonify({"success": False, "message": "Неподдерживаемый формат"}), 400
            m = request.files.get("file")
            if not m or (getattr(m, "mimetype", None) not in ALLOWED_MIME_TYPES):
                return jsonify({"success": False, "message": "Неподдерживаемый тип содержимого"}), 400
            # Issue #49: route every upload through the shared image pipeline
            # so big PNG maps (the 5.34 MB camera dump that triggered the
            # ticket) land on disk as WebP q=95 with the long edge clamped.
            file_data = file.read()
            try:
                out_bytes, out_ext = optimize_uploaded_image(file_data)
            except ImageTooLargeError:
                return jsonify(
                    {
                        "success": False,
                        "message": "Изображение слишком большое",
                        "error_code": "IMAGE_TOO_LARGE",
                    }
                ), 400
            except (OSError, ValueError) as e:
                logger.error("map upload: optimize failed: %s", e)
                return jsonify(
                    {
                        "success": False,
                        "message": "Не удалось обработать изображение",
                        "error_code": "IMAGE_PROCESSING_FAILED",
                    }
                ), 400
            filename = _store_map_bytes(out_bytes, out_ext)
            return jsonify({"success": True, "message": "Карта загружена", "path": f"media/maps/{filename}"})
    except (OSError, PermissionError) as e:
        logger.error(f"Ошибка работы с картой зон: {e}")
        return jsonify({"success": False, "message": "Ошибка работы с картой"}), 500


@system_config_api_bp.route("/api/map/<string:filename>", methods=["DELETE"])
@audit_log("map_delete", target_extractor=lambda *a, **kw: f"map:{kw.get('filename', a[0] if a else '?')}")
def api_map_delete(filename):
    try:
        if not (current_app.config.get("TESTING") or session.get("role") == "admin"):
            return jsonify({"success": False, "message": "Только администратор может удалять карты"}), 403
        safe = secure_filename(filename)
        if safe != filename:
            return jsonify({"success": False, "message": "Некорректное имя файла"}), 400
        if not _is_safe_map_filename(safe):
            return jsonify({"success": False, "message": "Некорректное имя файла"}), 400
        with _open_map_directory(create=False) as (_, directory_fd):
            try:
                item_stat = _entry_stat(directory_fd, safe)
            except FileNotFoundError:
                return jsonify({"success": False, "message": "Файл не найден"}), 404
            if stat.S_ISLNK(item_stat.st_mode):
                os.unlink(safe, dir_fd=directory_fd)
                os.fsync(directory_fd)
                return jsonify({"success": False, "message": "Файл не найден"}), 404
            if not stat.S_ISREG(item_stat.st_mode):
                return jsonify({"success": False, "message": "Файл не найден"}), 404
            os.unlink(safe, dir_fd=directory_fd)
            os.fsync(directory_fd)
        return jsonify({"success": True})
    except (OSError, PermissionError) as e:
        logger.error(f"Ошибка удаления карты: {e}")
        return jsonify({"success": False, "message": "Ошибка удаления карты"}), 500


# ===== Rain config =====


@system_config_api_bp.route("/api/rain", methods=["GET", "POST"])
@audit_log("rain_config_save", target_extractor=lambda *a, **kw: "rain_config")
def api_rain_config():
    try:
        if request.method == "GET":
            return jsonify({"success": True, "config": db.get_rain_config()})
        data = request.get_json() or {}
        cfg = {
            "enabled": bool(data.get("enabled")),
            "topic": (data.get("topic") or "").strip(),
            "type": data.get("type") if data.get("type") in ("NO", "NC") else "NO",
            "server_id": data.get("server_id"),
        }
        if cfg["enabled"] and not cfg["topic"]:
            return jsonify({"success": False, "message": "Требуется MQTT-топик для датчика дождя"}), 400
        ok = db.set_rain_config(cfg)
        if ok and cfg.get("enabled"):
            try:
                for g in db.get_groups() or []:
                    gid = int(g.get("id"))
                    if gid == 999:
                        continue
                    db.set_group_use_rain(gid, True)
            except (sqlite3.Error, OSError) as e:
                logger.debug("Handled exception in api_rain_config: %s", e)
        return jsonify({"success": bool(ok)})
    except (ConnectionError, TimeoutError, OSError) as e:
        logger.error(f"rain config failed: {e}")
        return jsonify({"success": False}), 500


# ===== Env config =====


@system_config_api_bp.route("/api/env", methods=["GET", "POST"])
@audit_log("env_config_save", target_extractor=lambda *a, **kw: "env_config")
def api_env_config():
    try:
        if request.method == "GET":
            cfg = db.get_env_config()
            values = {"temp": env_monitor.temp_value, "hum": env_monitor.hum_value}
            return jsonify({"success": True, "config": cfg, "values": values})
        data = request.get_json() or {}
        action = data.get("action")
        if action == "restart":
            try:
                cfg = db.get_env_config()
                env_monitor.start(cfg)
                probe_env_values(cfg)
            except (sqlite3.Error, OSError) as e:
                logger.debug("Handled exception in api_env_config: %s", e)
            return jsonify({"success": True})
        try:
            temp_cfg = data.get("temp") or {}
            hum_cfg = data.get("hum") or {}
            errors = {}
            if bool(temp_cfg.get("enabled")) and not str(temp_cfg.get("topic") or "").strip():
                errors["temp_topic"] = "Требуется MQTT-топик для датчика температуры"
            if bool(hum_cfg.get("enabled")) and not str(hum_cfg.get("topic") or "").strip():
                errors["hum_topic"] = "Требуется MQTT-топик для датчика влажности"
            if errors:
                return jsonify({"success": False, "errors": errors}), 400
        except (ConnectionError, TimeoutError, OSError) as e:
            logger.debug("Handled exception in api_env_config: %s", e)
        ok = db.set_env_config(data)
        try:
            cfg = db.get_env_config()
            env_monitor.start(cfg)
            probe_env_values(cfg)
        except (sqlite3.Error, OSError) as e:
            logger.debug("Handled exception in line_416: %s", e)
        return jsonify({"success": bool(ok)})
    except (ConnectionError, TimeoutError, OSError) as e:
        logger.error(f"env config failed: {e}")
        return jsonify({"success": False}), 500


@system_config_api_bp.route("/api/env/values", methods=["GET"])
def api_env_values():
    try:
        cfg = db.get_env_config()
        temp_enabled = bool((cfg.get("temp") or {}).get("enabled"))
        hum_enabled = bool((cfg.get("hum") or {}).get("enabled"))
        temperature = (
            None
            if not temp_enabled
            else (env_monitor.temp_value if env_monitor.temp_value is not None else "нет данных")
        )
        humidity = (
            None if not hum_enabled else (env_monitor.hum_value if env_monitor.hum_value is not None else "нет данных")
        )
        return jsonify(
            {
                "success": True,
                "temperature": temperature,
                "humidity": humidity,
                "enabled": {"temp": temp_enabled, "hum": hum_enabled},
            }
        )
    except (sqlite3.Error, OSError) as e:
        logger.error(f"env values failed: {e}")
        return jsonify({"success": False}), 500


# ===== Postpone =====


@system_config_api_bp.route("/api/postpone", methods=["POST"])
@audit_log("postpone_action", target_extractor=lambda *a, **kw: "group")
def api_postpone():
    """Postpone watering."""
    data = request.get_json()
    group_id = data.get("group_id")
    try:
        group_id = int(group_id)
    except (ValueError, TypeError, KeyError) as e:
        logger.debug("Exception in api_postpone: %s", e)
        return jsonify({"success": False, "message": "Некорректный идентификатор группы"}), 400
    days = data.get("days", 1)
    action = data.get("action")

    if action == "cancel":
        zones = db.get_zones()
        group_zones = [z for z in zones if int(z.get("group_id") or 0) == int(group_id)]
        for zone in group_zones:
            db.update_zone_postpone(zone["id"], None, None)
        db.add_log("postpone_cancel", json.dumps({"group": group_id}))
        return jsonify({"success": True, "message": "Отложенный полив отменен"})

    elif action == "postpone":
        from services.postpone import postpone_group

        res = postpone_group(group_id, days, source="api")
        return jsonify(
            {
                "success": True,
                "message": f"Полив отложен на {days} дней",
                "postpone_until": res["postpone_date"].strftime("%Y-%m-%d %H:%M:%S"),
            }
        )

    return jsonify({"success": False, "message": "Неверное действие"}), 400


# ===== Settings =====


@system_config_api_bp.route("/api/settings/early-off", methods=["GET", "POST"])
@audit_log("setting_early_off", target_extractor=lambda *a, **kw: "setting:early_off")
def api_setting_early_off():
    try:
        if request.method == "GET":
            seconds = db.get_early_off_seconds()
            return jsonify({"success": True, "seconds": seconds})
        data = request.get_json(silent=True) or {}
        seconds = int(data.get("seconds", 3))
        if seconds < 0 or seconds > 15:
            return jsonify({"success": False, "message": "seconds must be within 0..15"}), 400
        ok = db.set_early_off_seconds(seconds)
        return jsonify({"success": bool(ok), "seconds": seconds})
    except (sqlite3.Error, OSError) as e:
        logger.error(f"early-off setting failed: {e}")
        from services.helpers import api_error

        return api_error("INTERNAL_ERROR", "internal error", 500)


@system_config_api_bp.route("/api/settings/system-name", methods=["GET", "POST"])
@audit_log("setting_system_name", target_extractor=lambda *a, **kw: "setting:system_name")
def api_setting_system_name():
    try:
        if request.method == "GET":
            name = db.get_setting_value("system_name") or ""
            return jsonify({"success": True, "name": name})
        if not (current_app.config.get("TESTING") or session.get("role") == "admin"):
            return jsonify({"success": False, "message": "admin required"}), 403
        data = request.get_json(silent=True) or {}
        name = (data.get("name") or "").strip()
        ok = db.set_setting_value("system_name", name if name else None)
        return jsonify({"success": bool(ok), "name": name})
    except (sqlite3.Error, OSError) as e:
        logger.error(f"system-name setting failed: {e}")
        from services.helpers import api_error

        return api_error("INTERNAL_ERROR", "internal error", 500)


# ===== Logging debug toggle =====


def _disable_debug_logging_job():
    """APScheduler job: turn off DEBUG mode automatically.

    Persists 'logging.debug=0' to settings, drops root logger to WARNING,
    records an audit event so operators can see the auto-off in /logs.
    Best-effort — never raises.
    """
    try:
        db.set_logging_debug(False)
        # Invalidate the debug_audit() TTL cache so the flip takes effect
        # immediately for high-volume diagnostic emits (mqtt_publish, scheduler
        # timers) instead of waiting up to ~5s for the cache to expire.
        try:
            from services.audit import invalidate_debug_audit_cache

            invalidate_debug_audit_cache()
        except (ImportError, RuntimeError) as e:
            logger.debug("auto-off: invalidate_debug_audit_cache failed: %s", e)
        try:
            logging.getLogger().setLevel(logging.WARNING)
        except (TypeError, ValueError) as e:
            logger.debug("auto-off: setLevel failed: %s", e)
        try:
            from services.audit import record_audit

            record_audit(
                action_type="debug_log_auto_off",
                source="scheduler",
                target="logging:debug",
                payload={"auto_off": True},
                actor="system",
            )
        except (ImportError, RuntimeError) as e:
            logger.debug("auto-off: record_audit failed: %s", e)
        logger.info("debug logging auto-off triggered (job=debug_auto_off)")
    except (sqlite3.Error, OSError) as e:
        logger.warning("debug_auto_off job failed: %s", e)


@system_config_api_bp.route("/api/logging/debug", methods=["GET", "POST"])
@audit_log("debug_log_toggle", target_extractor=lambda *a, **kw: "logging:debug")
def api_logging_debug_toggle():
    """Toggle DEBUG-level logging (Level 2 — operational debug).

    POST body:
        {"enabled": true|false, "auto_off_minutes": 60}

    `auto_off_minutes` is optional (1..720 = 12h). When supplied with
    enabled=true, schedules a one-shot APScheduler DateTrigger to flip
    the flag back off — protects against operators forgetting to disable
    debug mode and filling the disk with logs.
    """
    try:
        if request.method == "POST":
            payload = request.get_json(force=True, silent=True) or {}
            enable = bool(payload.get("enabled"))
            try:
                auto_off_min = payload.get("auto_off_minutes")
                auto_off_min = int(auto_off_min) if auto_off_min is not None else None
                if auto_off_min is not None and (auto_off_min < 1 or auto_off_min > 720):
                    auto_off_min = max(1, min(720, auto_off_min))
            except (TypeError, ValueError):
                auto_off_min = None
            db.set_logging_debug(enable)
            # Invalidate debug_audit() TTL cache so manual toggle takes effect
            # immediately instead of waiting for the ~5s cache to expire.
            try:
                from services.audit import invalidate_debug_audit_cache

                invalidate_debug_audit_cache()
            except (ImportError, RuntimeError) as e:
                logger.debug("debug toggle: invalidate_debug_audit_cache failed: %s", e)
            # Apply runtime log level
            try:
                is_debug = db.get_logging_debug()
                level = logging.DEBUG if is_debug else logging.WARNING
                root = logging.getLogger()
                root.setLevel(level)
            except (sqlite3.Error, OSError) as e:
                logger.debug("Handled exception in api_logging_debug_toggle: %s", e)
            # Manage the auto-off job
            try:
                from apscheduler.triggers.date import DateTrigger

                sched = get_scheduler()
                if sched and getattr(sched, "scheduler", None):
                    # Remove any pending auto-off first
                    try:
                        sched.scheduler.remove_job("debug_auto_off")
                    except (ValueError, KeyError):
                        pass  # not scheduled — fine
                    if enable and auto_off_min:
                        run_at = datetime.now() + timedelta(minutes=auto_off_min)
                        sched.scheduler.add_job(
                            _disable_debug_logging_job,
                            trigger=DateTrigger(run_date=run_at),
                            id="debug_auto_off",
                            replace_existing=True,
                            coalesce=True,
                            max_instances=1,
                        )
                        logger.info(
                            "debug logging auto-off scheduled for %s (in %d min)",
                            run_at.isoformat(timespec="seconds"),
                            auto_off_min,
                        )
            except (ImportError, RuntimeError, KeyError, ValueError) as e:
                logger.warning("Failed to (re)schedule debug auto-off: %s", e)
        # GET (and POST response): include auto_off info if known
        info = {"debug": db.get_logging_debug()}
        try:
            sched = get_scheduler()
            if sched and getattr(sched, "scheduler", None):
                job = sched.scheduler.get_job("debug_auto_off")
                if job and job.next_run_time:
                    info["auto_off_at"] = job.next_run_time.isoformat(timespec="seconds")
        except (ImportError, RuntimeError, AttributeError):
            pass
        return jsonify(info)
    except (sqlite3.Error, OSError) as e:
        logger.error(f"api_logging_debug_toggle error: {e}")
        return jsonify({"debug": db.get_logging_debug()}), 500
