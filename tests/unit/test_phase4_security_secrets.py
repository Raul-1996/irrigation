"""Regression tests for local secret, password, and runtime-file hardening."""

from __future__ import annotations

import contextlib
import logging
import os
import sqlite3
import stat
import subprocess
import sys
import threading
from pathlib import Path

import pytest
from werkzeug.security import check_password_hash


@contextlib.contextmanager
def _umask(mask: int):
    previous = os.umask(mask)
    try:
        yield
    finally:
        os.umask(previous)


def _mode(path: Path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


def _settings_repository(db_path: Path):
    from db.settings import SettingsRepository

    repository = SettingsRepository(str(db_path))
    with sqlite3.connect(db_path) as connection:
        connection.execute("CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT)")
        connection.commit()
    return repository


def test_database_file_and_new_parent_are_private_independent_of_umask(tmp_path):
    db_path = tmp_path / "controller-state" / "irrigation.db"

    with _umask(0):
        repository = _settings_repository(db_path)
        repository.get_setting_value("missing")

    assert _mode(db_path.parent) == 0o700
    assert _mode(db_path) == 0o600
    for suffix in ("-wal", "-shm"):
        sidecar = Path(f"{db_path}{suffix}")
        if sidecar.exists():
            assert _mode(sidecar) == 0o600


def test_private_directories_do_not_require_linux_unsupported_path_chmod(tmp_path, monkeypatch):
    """Linux rejects chmod(path, ..., follow_symlinks=False); fd chmod must work."""

    import utils

    real_chmod = os.chmod

    def linux_like_chmod(path, mode, *, dir_fd=None, follow_symlinks=True):
        if follow_symlinks is False:
            raise NotImplementedError("follow_symlinks=False unavailable on Linux chmod")
        return real_chmod(path, mode, dir_fd=dir_fd, follow_symlinks=follow_symlinks)

    monkeypatch.setattr(utils.os, "chmod", linux_like_chmod)
    private_directory = tmp_path / "runtime" / "backups"

    with _umask(0):
        utils.ensure_private_directory(private_directory)

    assert _mode(private_directory.parent) == 0o700
    assert _mode(private_directory) == 0o700

    restrictive_directory = tmp_path / "umask-locked" / "backups"
    with _umask(0o777):
        utils.ensure_private_directory(restrictive_directory)
    assert _mode(restrictive_directory.parent) == 0o700
    assert _mode(restrictive_directory) == 0o700


def test_linux_like_chmod_limitation_does_not_break_secret_or_logging_startup(tmp_path, monkeypatch):
    import utils
    from config import _load_or_generate_secret
    from services.logging_setup import setup_logging

    real_chmod = os.chmod

    def linux_like_chmod(path, mode, *, dir_fd=None, follow_symlinks=True):
        if follow_symlinks is False:
            raise NotImplementedError("follow_symlinks=False unavailable on Linux chmod")
        return real_chmod(path, mode, dir_fd=dir_fd, follow_symlinks=follow_symlinks)

    monkeypatch.setattr(utils.os, "chmod", linux_like_chmod)
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("PHASE4_LINUX_FLASK_SECRET", raising=False)
    secret_path = tmp_path / "runtime" / ".secret_key"
    assert _load_or_generate_secret("PHASE4_LINUX_FLASK_SECRET", str(secret_path))

    root = logging.getLogger()
    import_logger = logging.getLogger("import_export")
    saved_root_handlers = root.handlers[:]
    saved_import_handlers = import_logger.handlers[:]
    saved_root_level = root.level
    root.handlers = [logging.NullHandler()]
    import_logger.handlers = []
    try:
        setup_logging(logging.getLogger("phase4.linux.startup"))
        assert (tmp_path / "backups" / "app.log").is_file()
    finally:
        for handler in [*root.handlers, *import_logger.handlers]:
            if handler not in saved_root_handlers and handler not in saved_import_handlers:
                with contextlib.suppress(Exception):
                    handler.close()
        root.handlers = saved_root_handlers
        root.setLevel(saved_root_level)
        import_logger.handlers = saved_import_handlers


def test_linux_like_chmod_limitation_does_not_break_app_import(tmp_path):
    project_root = Path(__file__).resolve().parents[2]
    environment = os.environ.copy()
    environment.update(
        {
            "PYTHONPATH": str(project_root),
            "SECRET_KEY": "phase4-subprocess-test-secret-key",
            "TESTING": "1",
        }
    )
    script = """
import logging
import os

real_chmod = os.chmod

def linux_like_chmod(path, mode, *, dir_fd=None, follow_symlinks=True):
    if follow_symlinks is False:
        raise NotImplementedError("follow_symlinks=False unavailable on Linux chmod")
    return real_chmod(path, mode, dir_fd=dir_fd, follow_symlinks=follow_symlinks)

os.chmod = linux_like_chmod
import app
from services.logging_setup import setup_logging
setup_logging(logging.getLogger("app"))
assert os.path.isfile("backups/app.log")
logging.shutdown()
"""

    completed = subprocess.run(
        [sys.executable, "-c", script],
        cwd=tmp_path,
        env=environment,
        capture_output=True,
        text=True,
        timeout=20,
        check=False,
    )

    assert completed.returncode == 0, completed.stderr


def test_missing_admin_hash_uses_private_recovery_file_not_logs(tmp_path, caplog):
    db_path = tmp_path / "irrigation.db"
    repository = _settings_repository(db_path)
    caplog.set_level(logging.WARNING, logger="db.settings")

    with _umask(0):
        repository.ensure_password_change_required()

    recovery_file = tmp_path / "backups" / ".initial_admin_password"
    assert recovery_file.is_file()
    assert _mode(recovery_file.parent) == 0o700
    assert _mode(recovery_file) == 0o600

    generated_password = recovery_file.read_text(encoding="utf-8").strip()
    stored_hash = repository.get_password_hash()
    assert generated_password
    assert stored_hash is not None
    assert check_password_hash(stored_hash, generated_password)
    assert generated_password not in caplog.text
    assert "initial admin password" in caplog.text.lower()

    assert repository.set_password("replacement-admin-password") is True
    assert not recovery_file.exists()


def test_password_storage_matches_login_whitespace_normalization(tmp_path):
    from db.settings import normalize_password

    repository = _settings_repository(tmp_path / "irrigation.db")

    assert normalize_password("  replacement-admin-password  ") == "replacement-admin-password"
    assert repository.set_password("  replacement-admin-password  ") is True
    stored_hash = repository.get_password_hash()
    assert stored_hash is not None
    assert check_password_hash(stored_hash, "replacement-admin-password")
    assert not check_password_hash(stored_hash, "  replacement-admin-password  ")


def test_password_change_with_edge_whitespace_remains_login_compatible(admin_client, client):
    changed = admin_client.post(
        "/api/password",
        json={"old_password": "1234", "new_password": "  replacement-admin-password  "},
    )
    assert changed.status_code == 200
    assert changed.get_json()["success"] is True

    login = client.post("/api/login", json={"password": "  replacement-admin-password  "})
    assert login.status_code == 200
    assert login.get_json()["success"] is True


@pytest.mark.parametrize("password", ["       x       ", "  password  "])
def test_password_normalization_cannot_bypass_strength_policy(tmp_path, password):
    repository = _settings_repository(tmp_path / "irrigation.db")

    assert repository.set_password(password) is False
    assert repository.get_password_hash() is None


def test_flask_secret_file_creation_and_repair_are_private(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("SECRET_KEY", "test-secret-key-for-testing-only")
    from config import _load_or_generate_secret

    monkeypatch.delenv("PHASE4_TEST_FLASK_SECRET", raising=False)
    key_path = tmp_path / "private-config" / ".secret_key"

    with _umask(0):
        generated = _load_or_generate_secret("PHASE4_TEST_FLASK_SECRET", str(key_path))

    assert generated
    assert _mode(key_path.parent) == 0o700
    assert _mode(key_path) == 0o600

    key_path.chmod(0o666)
    assert _load_or_generate_secret("PHASE4_TEST_FLASK_SECRET", str(key_path)) == generated
    assert _mode(key_path) == 0o600


def test_empty_flask_secret_file_is_not_silently_rotated(tmp_path, monkeypatch):
    monkeypatch.setenv("SECRET_KEY", "test-secret-key-for-testing-only")
    from config import _load_or_generate_secret

    key_path = tmp_path / ".secret_key"
    key_path.write_bytes(b"")
    monkeypatch.delenv("PHASE4_TEST_FLASK_SECRET", raising=False)

    with pytest.raises(RuntimeError, match=r"restore|empty|invalid"):
        _load_or_generate_secret("PHASE4_TEST_FLASK_SECRET", str(key_path))

    assert key_path.read_bytes() == b""


def test_concurrent_flask_secret_initialization_converges_on_one_complete_key(tmp_path, monkeypatch):
    monkeypatch.setenv("SECRET_KEY", "test-secret-key-for-testing-only")
    from config import _load_or_generate_secret

    monkeypatch.delenv("PHASE4_TEST_FLASK_SECRET", raising=False)
    key_path = tmp_path / ".secret_key"
    start = threading.Barrier(3)
    results: list[str] = []
    errors: list[Exception] = []

    def load_key() -> None:
        start.wait()
        try:
            results.append(_load_or_generate_secret("PHASE4_TEST_FLASK_SECRET", str(key_path)))
        except Exception as error:  # pragma: no cover - asserted below
            errors.append(error)

    workers = [threading.Thread(target=load_key) for _ in range(2)]
    for worker in workers:
        worker.start()
    start.wait()
    for worker in workers:
        worker.join(timeout=2)

    assert all(not worker.is_alive() for worker in workers)
    assert not errors
    assert len(results) == 2
    assert results[0] == results[1] == key_path.read_text(encoding="utf-8")
    assert _mode(key_path) == 0o600


def test_private_file_fsyncs_parent_after_publish_before_removing_temp(tmp_path, monkeypatch):
    import utils

    events: list[str] = []
    real_fsync = os.fsync
    real_link = os.link
    real_unlink = os.unlink

    def tracked_fsync(descriptor):
        kind = "directory" if stat.S_ISDIR(os.fstat(descriptor).st_mode) else "file"
        events.append(f"fsync:{kind}")
        return real_fsync(descriptor)

    def tracked_link(source, destination, *, src_dir_fd=None, dst_dir_fd=None, follow_symlinks=True):
        events.append("link:final")
        return real_link(
            source,
            destination,
            src_dir_fd=src_dir_fd,
            dst_dir_fd=dst_dir_fd,
            follow_symlinks=follow_symlinks,
        )

    def tracked_unlink(path, *, dir_fd=None):
        if str(path).endswith(".tmp"):
            events.append("unlink:temp")
        return real_unlink(path, dir_fd=dir_fd)

    monkeypatch.setattr(utils.os, "fsync", tracked_fsync)
    monkeypatch.setattr(utils.os, "link", tracked_link)
    monkeypatch.setattr(utils.os, "unlink", tracked_unlink)
    target = tmp_path / ".secret_key"

    utils.create_private_file(target, b"durable-secret")

    assert target.read_bytes() == b"durable-secret"
    assert events.index("fsync:file") < events.index("link:final")
    assert events.index("link:final") < events.index("fsync:directory")
    assert events.index("fsync:directory") < events.index("unlink:temp")


def test_parent_fsync_failure_keeps_private_recovery_link(tmp_path, monkeypatch):
    import utils

    real_fsync = os.fsync

    def fail_directory_fsync(descriptor):
        if stat.S_ISDIR(os.fstat(descriptor).st_mode):
            raise OSError("simulated directory fsync failure")
        return real_fsync(descriptor)

    monkeypatch.setattr(utils.os, "fsync", fail_directory_fsync)
    target = tmp_path / ".secret_key"

    with pytest.raises(OSError, match="directory fsync"):
        utils.create_private_file(target, b"recoverable-secret")

    temporary_links = list(tmp_path.glob("..secret_key.*.tmp"))
    assert target.read_bytes() == b"recoverable-secret"
    assert len(temporary_links) == 1
    assert temporary_links[0].read_bytes() == b"recoverable-secret"
    assert target.stat().st_ino == temporary_links[0].stat().st_ino
    assert _mode(target) == _mode(temporary_links[0]) == 0o600


def test_irrigation_secret_existing_file_is_hardened(tmp_path, monkeypatch):
    import utils

    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("IRRIG_SECRET_KEY", raising=False)
    key_path = tmp_path / ".irrig_secret_key"
    expected = b"K" * 32
    key_path.write_bytes(expected)
    key_path.chmod(0o666)

    assert utils._get_secret_key() == expected
    assert _mode(key_path) == 0o600


def test_invalid_irrigation_secret_file_is_not_silently_rotated(tmp_path, monkeypatch):
    import utils

    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("IRRIG_SECRET_KEY", raising=False)
    key_path = tmp_path / ".irrig_secret_key"
    damaged = b"truncated-key"
    key_path.write_bytes(damaged)

    with pytest.raises(RuntimeError, match=r"restore|32 bytes|invalid"):
        utils._get_secret_key()

    assert key_path.read_bytes() == damaged


def test_invalid_irrigation_secret_env_fails_closed_without_logging_material(tmp_path, monkeypatch, caplog):
    import utils

    monkeypatch.chdir(tmp_path)
    invalid_material = "not-base64-secret-material"
    monkeypatch.setenv("IRRIG_SECRET_KEY", invalid_material)
    caplog.set_level(logging.DEBUG, logger="utils")

    with pytest.raises(RuntimeError, match="IRRIG_SECRET_KEY"):
        utils._get_secret_key()

    assert invalid_material not in caplog.text
    assert not (tmp_path / ".irrig_secret_key").exists()


def test_logging_directory_and_rotating_files_are_private_under_permissive_umask(tmp_path, monkeypatch):
    from logging.handlers import TimedRotatingFileHandler

    from services.logging_setup import setup_logging

    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("TESTING", "1")
    root = logging.getLogger()
    app_logger = logging.getLogger("phase4.security.test")
    import_logger = logging.getLogger("import_export")
    saved_root_handlers = root.handlers[:]
    saved_import_handlers = import_logger.handlers[:]
    saved_root_level = root.level
    root.handlers = [logging.NullHandler()]
    import_logger.handlers = []
    try:
        with _umask(0):
            setup_logging(app_logger)
            app_logger.warning("security mode probe")
            import_logger.warning("import mode probe")

        log_dir = tmp_path / "backups"
        assert _mode(log_dir) == 0o700
        assert _mode(log_dir / "app.log") == 0o600
        assert _mode(log_dir / "import-export.log") == 0o600
        app_handlers = [handler for handler in root.handlers if isinstance(handler, TimedRotatingFileHandler)]
        assert app_handlers
        with _umask(0):
            app_handlers[0].doRollover()
        assert all(_mode(path) == 0o600 for path in log_dir.glob("app.log*"))
    finally:
        for handler in [*root.handlers, *import_logger.handlers]:
            if handler not in saved_root_handlers and handler not in saved_import_handlers:
                with contextlib.suppress(Exception):
                    handler.close()
        root.handlers = saved_root_handlers
        root.setLevel(saved_root_level)
        import_logger.handlers = saved_import_handlers


def test_pii_filter_redacts_legacy_bootstrap_password_message():
    from services.logging_setup import PIIMaskingFilter

    bootstrap_password = "bootstrap-material-must-not-leak"
    record = logging.LogRecord(
        name="db.settings",
        level=logging.WARNING,
        pathname="/db/settings.py",
        lineno=1,
        msg="Initial random password generated: %s (change it on first login!)",
        args=(bootstrap_password,),
        exc_info=None,
    )

    assert PIIMaskingFilter().filter(record) is True
    assert bootstrap_password not in record.getMessage()
    assert "[REDACTED]" in record.getMessage()
