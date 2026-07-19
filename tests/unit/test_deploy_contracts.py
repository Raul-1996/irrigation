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
    assert 'sqlite3 "$DB_PATH" ".backup \'$DB_BACKUP\'"' in UPDATE
    assert 'PRAGMA quick_check' in UPDATE
    assert "DB_FILES=(irrigation.db irrigation.db-wal irrigation.db-shm)" not in UPDATE
    assert 'cp -v "$f" "$BACKUP_DIR/" || true' not in UPDATE
    assert 'tar ' in UPDATE and '-czf "$ARCHIVE"' in UPDATE
    assert '-czf "$ARCHIVE" . || true' not in UPDATE


def test_update_restores_previously_active_service_on_early_failure():
    assert "SERVICE_WAS_ACTIVE=0" in UPDATE
    assert "restore_service_on_failure" in UPDATE
    assert "trap restore_service_on_failure EXIT" in UPDATE


def test_update_rebuilds_invalid_venv_with_uv_python_311():
    assert "PY_VERSION=${PY_VERSION:-3.11}" in UPDATE
    assert 'uv python install "$PY_VERSION"' in UPDATE
    assert 'uv venv --python "$PY_VERSION" "$VENV_DIR"' in UPDATE
    assert "sys.version_info[:2]" in UPDATE
    assert "$PY_BIN -m venv venv" not in UPDATE
    assert "requirements-dev.txt" not in UPDATE


def test_update_backups_are_private_restorable_and_retained_on_data_partition():
    assert "umask 077" in UPDATE
    assert 'REPO_DIR_REAL=$(cd "$REPO_DIR" && pwd -P)' in UPDATE
    assert 'BACKUP_BASE=${BACKUP_BASE:-"${REPO_DIR_REAL}/backups"}' in UPDATE
    assert 'mkdir -p -m 700 "$BACKUP_BASE"' in UPDATE
    assert 'mkdir -m 700 "$BACKUP_DIR"' in UPDATE
    assert 'chmod 600 "$DB_BACKUP"' in UPDATE
    assert 'chmod 600 "$ARCHIVE"' in UPDATE
    assert "BACKUP_KEEP=${BACKUP_KEEP:-10}" in UPDATE
    assert "prune_old_backups" in UPDATE
    # Encryption keys remain inside the private snapshot and the external env
    # file is backed up explicitly so credentials are restorable.
    assert '--exclude="./.secret_key"' not in UPDATE
    assert '--exclude="./.irrig_secret_key"' not in UPDATE
    assert 'chmod 600 "$secret_file"' in UPDATE
    assert 'install -m 600 "$ENV_FILE" "$BACKUP_DIR/environment.env"' in UPDATE
    assert 'chmod 600 "$env_file"' in UPDATE
    assert 'chmod 644 "$ENV_FILE"' not in UPDATE


def test_deploy_workflow_uses_only_supported_update_path_and_propagates_failures():
    assert "set -euo pipefail" in DEPLOY
    assert "bash ./update_server.sh --yes" in DEPLOY
    assert "git pull" not in DEPLOY
    assert "pip install" not in DEPLOY
    assert "systemctl restart" not in DEPLOY
    assert "|| echo" not in DEPLOY


def test_install_migrates_legacy_tree_and_restarts_on_rerun():
    assert "migrate_legacy_app_tree" in INSTALL
    assert 'mv "$APP_LINK" "$DATA_DIR"' in INSTALL
    assert 'ln -s "$DATA_DIR" "$APP_LINK"' in INSTALL
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


def test_ci_runs_the_complete_default_selected_suite():
    assert "pytest --tb=short -q --cov --cov-report=xml" in CI
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
