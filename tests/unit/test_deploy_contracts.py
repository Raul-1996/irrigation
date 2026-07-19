"""Static contracts for the supported native Wirenboard deployment path.

The deploy scripts require root/systemd and cannot be executed safely in CI.
These tests pin the safety-critical shell and workflow invariants instead.
"""

from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
UPDATE = (ROOT / "update_server.sh").read_text(encoding="utf-8")
INSTALL = (ROOT / "install_wb.sh").read_text(encoding="utf-8")
DEPLOY = (ROOT / ".github" / "workflows" / "deploy.yml").read_text(encoding="utf-8")
CI = (ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
SERVICE = (ROOT / "wb-irrigation.service").read_text(encoding="utf-8")


def test_update_quiesces_service_and_uses_verified_sqlite_backup():
    assert 'systemctl stop "$SERVICE"' in UPDATE
    assert 'sqlite3 "$database_path" ".backup \'$backup_path\'"' in UPDATE
    assert 'backup_sqlite_database "$DB_PATH" "$DB_BACKUP" "irrigation.db"' in UPDATE
    assert 'backup_sqlite_database "$JOBS_DB_PATH" "$JOBS_DB_BACKUP" "jobs.db"' in UPDATE
    assert 'PRAGMA quick_check' in UPDATE
    assert "DB_FILES=(irrigation.db irrigation.db-wal irrigation.db-shm)" not in UPDATE
    assert 'cp -v "$f" "$BACKUP_DIR/" || true' not in UPDATE
    assert 'tar ' in UPDATE and '-czf "$ARCHIVE"' in UPDATE
    assert '-czf "$ARCHIVE" . || true' not in UPDATE


def test_update_rolls_back_code_runtime_and_service_on_every_failure():
    assert "SERVICE_WAS_ACTIVE=0" in UPDATE
    assert "rollback_update" in UPDATE
    assert "trap finish_update EXIT" in UPDATE
    assert 'git -C "$REPO_DIR_REAL" reset --hard "$ORIGINAL_COMMIT"' in UPDATE
    assert 'mv "$OLD_VENV_BACKUP" "$VENV_DIR"' in UPDATE
    assert 'restore_sqlite_database "$DB_PATH" "$DB_BACKUP"' in UPDATE
    assert 'restore_sqlite_database "$JOBS_DB_PATH" "$JOBS_DB_BACKUP"' in UPDATE
    assert "UPDATE_SUCCEEDED=0" in UPDATE
    assert "git status --porcelain --untracked-files=normal" in UPDATE


def test_update_rebuilds_invalid_venv_with_uv_python_311():
    assert "PY_VERSION=${PY_VERSION:-3.11}" in UPDATE
    assert 'uv python install "$PY_VERSION"' in UPDATE
    assert "sys.version_info[:2]" in UPDATE
    assert "$PY_BIN -m venv venv" not in UPDATE
    assert "requirements-dev.txt" not in UPDATE
    assert 'STAGED_VENV="$REPO_DIR_REAL/.venv-staged-$STAMP-$$"' in UPDATE
    assert 'uv venv --python "$PY_VERSION" "$STAGED_VENV"' in UPDATE
    assert 'uv pip install --python "$STAGED_VENV/bin/python"' in UPDATE


def test_update_backups_are_private_restorable_and_retained_on_data_partition():
    assert "umask 077" in UPDATE
    assert 'REPO_DIR_REAL=$(cd "$REPO_DIR" && pwd -P)' in UPDATE
    assert 'BACKUP_BASE=${BACKUP_BASE:-"${REPO_DIR_REAL}/backups"}' in UPDATE
    assert "canonicalize_backup_base" in UPDATE
    assert '[[ -L "$BACKUP_BASE" ]]' in UPDATE
    assert '[[ "$backup_name" != "backups"' in UPDATE
    assert 'mkdir -p -m 700 "$BACKUP_BASE"' in UPDATE
    assert 'mkdir -m 700 "$BACKUP_DIR"' in UPDATE
    assert 'chmod 600 "$backup_path"' in UPDATE
    assert 'chmod 600 "$ARCHIVE"' in UPDATE
    assert "BACKUP_KEEP=${BACKUP_KEEP:-10}" in UPDATE
    assert "prune_old_backups" in UPDATE
    # Encryption keys remain inside the private snapshot and the external env
    # file is backed up explicitly so credentials are restorable.
    assert '--exclude="./.secret_key"' not in UPDATE
    assert '--exclude="./.irrig_secret_key"' not in UPDATE
    assert 'chmod 600 "$secret_file"' in UPDATE
    assert 'install -m 0600 "$ENV_FILE" "$ENV_FILE_BACKUP"' in UPDATE
    assert 'chmod 600 "$env_file"' in UPDATE
    assert 'chmod 644 "$ENV_FILE"' not in UPDATE


def test_deploy_workflow_uses_only_supported_update_path_and_propagates_failures():
    assert "set -euo pipefail" in DEPLOY
    assert "git fetch --quiet origin main" in DEPLOY
    assert 'git show "${TARGET_COMMIT}:update_server.sh"' in DEPLOY
    assert 'bash "$STAGED_UPDATER" --yes' in DEPLOY
    assert "export TARGET_COMMIT" in DEPLOY
    assert "bash ./update_server.sh --yes" not in DEPLOY
    assert "git pull" not in DEPLOY
    assert "pip install" not in DEPLOY
    assert "systemctl restart" not in DEPLOY
    assert "|| echo" not in DEPLOY


def test_install_migrates_legacy_tree_and_restarts_on_rerun():
    assert "migrate_legacy_app_tree" in INSTALL
    assert 'cp -a "$current/." "$migration_stage/"' in INSTALL
    assert 'atomic_switch_app_link "$legacy_backup"' in INSTALL
    assert 'ln -s "$DATA_DIR" "$link_stage"' in INSTALL
    assert 'rm "$APP_LINK"' not in INSTALL
    assert 'systemctl enable "$SERVICE_NAME"' in INSTALL
    assert 'systemctl restart "$SERVICE_NAME"' in INSTALL
    assert 'systemctl enable --now "$SERVICE_NAME"' not in INSTALL


def test_port_contract_is_shared_by_service_and_both_smoke_checks():
    assert "EnvironmentFile=-/opt/wb-irrigation/.env" in SERVICE
    assert "configured_port" in INSTALL
    assert "configured_port" in UPDATE
    assert "SMOKE_PORT" not in INSTALL
    assert "SMOKE_PORT" not in UPDATE
    assert 'upsert_env_value PORT "$VALIDATED_PORT" "$ENV_FILE"' in UPDATE
    assert 'configured_port "$ENV_FILE" "$REPO_DIR_REAL/.env"' in UPDATE
    assert 'configured_port "$ENV_FILE" "$APP_DIR/.env"' in INSTALL
    assert '127.0.0.1:${APP_PORT}/readyz' in INSTALL
    assert '127.0.0.1:${APP_PORT}/readyz' in UPDATE


def test_install_and_update_activate_repo_logrotate_config():
    expected = 'install -m 0644 "$LOGROTATE_SOURCE" "$LOGROTATE_TARGET"'
    assert expected in INSTALL
    assert expected in UPDATE
    assert "/etc/logrotate.d/wb-irrigation" in INSTALL
    assert "/etc/logrotate.d/wb-irrigation" in UPDATE
    for script in (INSTALL, UPDATE):
        assert "ensure_logrotate_ready" in script
        assert 'logrotate -d "/etc/logrotate.conf"' in script
        assert "logrotate.timer" in script
        assert "/etc/cron.daily/logrotate" in script
    assert "apt-get install -y logrotate" in UPDATE


def test_install_and_update_purge_retired_telegram_file_logs_before_activation():
    for script in (INSTALL, UPDATE):
        assert "retire_telegram_file_logs" in script
        assert "find -P" in script
        assert "telegram[.]txt" in script
        assert 'rm -f -- "$candidate"' in script
    assert 'retire_telegram_file_logs "$REPO_DIR_REAL"' in UPDATE
    assert 'retire_telegram_file_logs "$ACTIVE_APP_DIR"' in INSTALL
    assert 'retire_telegram_file_logs "$ACTIVE_DATA_DIR"' in INSTALL
    assert 'retire_telegram_file_logs "$APP_DIR"' in INSTALL


def test_deploy_requires_telegram_runtime_retirement_before_pii_cleanup():
    update_guard = 'require_retired_telegram_runtime_commit "$REPO_DIR_REAL" "$RESOLVED_TARGET"'
    install_preflight_guard = (
        'require_retired_telegram_runtime_commit "$TARGET_VALIDATION_REPO" "$INSTALL_TARGET_COMMIT"'
    )
    install_guard = 'require_retired_telegram_runtime_tree "$APP_DIR"'
    assert update_guard in UPDATE
    assert install_preflight_guard in INSTALL
    assert install_guard in INSTALL
    assert UPDATE.index(update_guard) < UPDATE.index("UPDATE_SUCCEEDED=0")
    assert INSTALL.index(install_preflight_guard) < INSTALL.index('retire_telegram_file_logs "$ACTIVE_APP_DIR"')
    assert INSTALL.index(install_preflight_guard) < INSTALL.index('retire_telegram_file_logs "$ACTIVE_DATA_DIR"')
    assert INSTALL.index(install_guard) < INSTALL.index('retire_telegram_file_logs "$APP_DIR"')
    assert INSTALL.index(install_guard) < INSTALL.index('systemctl restart "$SERVICE_NAME"')
    for script in (INSTALL, UPDATE):
        assert "TELEGRAM_LOG_RETIREMENT_BLOB=a686879844b5293a943c0804f79950ff6984ec8c" in script
        assert 'ls-tree "$target_commit" -- services' in script
        assert 'ls-tree "$target_commit" -- services/telegram_bot.py' in script
        assert "logging[.]FileHandler" not in script


def test_ci_runs_the_complete_default_selected_suite():
    assert "pytest --tb=short -q --cov --cov-report=xml" in CI
    assert "-W error::pytest.PytestUnraisableExceptionWarning" in CI
    assert "-W error::pytest.PytestUnhandledThreadExceptionWarning" in CI
    assert "pytest tests/unit tests/db tests/api" not in CI
    # Selection remains centralized in pytest.ini; production CI must not add
    # Docker or bespoke category lists that silently omit new test folders.
    assert "docker" not in CI.lower()


def test_production_deploy_path_does_not_invoke_docker():
    shell_commands = "\n".join(
        line for text in (UPDATE, INSTALL) for line in text.splitlines() if not line.lstrip().startswith("#")
    )
    assert "docker" not in shell_commands.lower()
    assert "docker" not in DEPLOY.lower()
    assert "docker" not in SERVICE.lower()
