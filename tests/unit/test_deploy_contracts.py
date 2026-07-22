"""Static contracts for the supported native Wirenboard deployment path.

The deploy scripts require root/systemd and cannot be executed safely in CI.
These tests pin the safety-critical shell and workflow invariants instead.
"""

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
UPDATE = (ROOT / "update_server.sh").read_text(encoding="utf-8")
INSTALL = (ROOT / "install_wb.sh").read_text(encoding="utf-8")
DEPLOY = (ROOT / ".github" / "workflows" / "deploy.yml").read_text(encoding="utf-8")
CI = (ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
SERVICE = (ROOT / "wb-irrigation.service").read_text(encoding="utf-8")
UNINSTALL = (ROOT / "uninstall_wb.sh").read_text(encoding="utf-8")


def test_update_quiesces_service_and_uses_verified_sqlite_backup():
    assert 'systemctl stop "$SERVICE"' in UPDATE
    assert 'sqlite3 "$database_path" ".backup \'$backup_path\'"' in UPDATE
    assert 'backup_sqlite_database "$DB_PATH" "$DB_BACKUP" "irrigation.db"' in UPDATE
    assert 'backup_sqlite_database "$JOBS_DB_PATH" "$JOBS_DB_BACKUP" "jobs.db"' in UPDATE
    assert 'DB_PATH="$STATE_DIR/irrigation.db"' in UPDATE
    assert 'JOBS_DB_PATH="$STATE_DIR/jobs.db"' in UPDATE
    assert "PRAGMA quick_check" in UPDATE
    assert "DB_FILES=(irrigation.db irrigation.db-wal irrigation.db-shm)" not in UPDATE
    assert 'cp -v "$f" "$BACKUP_DIR/" || true' not in UPDATE
    assert "tar " in UPDATE and '-czf "$ARCHIVE"' in UPDATE
    assert '-czf "$ARCHIVE" . || true' not in UPDATE


def test_update_rolls_back_code_runtime_and_service_on_every_failure():
    assert "SERVICE_WAS_ACTIVE=$HANDOFF_SERVICE_WAS_ACTIVE" in UPDATE
    assert "rollback_update" in UPDATE
    assert "trap finish_update EXIT" in UPDATE
    assert 'git -C "$REPO_DIR_REAL" reset --hard "$ORIGINAL_COMMIT"' in UPDATE
    assert 'mv "$OLD_VENV_BACKUP" "$VENV_DIR"' in UPDATE
    assert 'restore_sqlite_database "$DB_PATH" "$DB_BACKUP"' in UPDATE
    assert 'restore_sqlite_database "$JOBS_DB_PATH" "$JOBS_DB_BACKUP"' in UPDATE
    assert "UPDATE_SUCCEEDED=0" in UPDATE
    assert "git status --porcelain --untracked-files=normal" in UPDATE
    assert "restore_state_snapshot" in UPDATE
    assert "quarantine_legacy_runtime_state" in UPDATE
    assert "restore_legacy_runtime_state" in UPDATE
    assert '"${DEFER_ROLLBACK_RESTART:-0}" != "1"' in UPDATE


def test_update_rebuilds_invalid_venv_with_uv_python_311():
    assert "PY_VERSION=${PY_VERSION:-3.11.15}" in UPDATE
    assert '"$UV_BIN" python install "$PY_VERSION"' in UPDATE
    assert "sys.version_info[:3]" in UPDATE
    assert "$PY_BIN -m venv venv" not in UPDATE
    assert "requirements-dev.txt" not in UPDATE
    assert 'STAGED_VENV="$REPO_DIR_REAL/.venv-staged-$STAMP-$$"' in UPDATE
    assert '"$UV_BIN" venv --python "$PY_BIN" "$STAGED_VENV"' in UPDATE
    assert '"$UV_BIN" pip install --python "$STAGED_VENV/bin/python"' in UPDATE


def test_native_deploy_bootstraps_only_hash_verified_pinned_uv():
    for script in (INSTALL, UPDATE):
        assert "UV_VERSION=0.11.31" in script
        assert (
            "https://releases.astral.sh/github/uv/releases/download/0.11.31/uv-aarch64-unknown-linux-gnu.tar.gz"
        ) in script
        assert "d74f23949fd07be4970f293d06ca99d87cd2a78a341c3d7b7fc0df7bc2d8a145" in script
        assert "sha256sum -c -" in script
        assert "curl --proto '=https' --tlsv1.2" in script
        assert "install_pinned_uv" in script
        assert '[[ "$architecture" != "aarch64" ]]' in script
        assert "astral.sh/uv/install" not in script
        assert "install.sh | sh" not in script
        assert "PY_VERSION=${PY_VERSION:-3.11.15}" in script
        assert "FIXED_PY_VERSION=3.11.15" in script


def test_update_backups_are_private_restorable_and_retained_on_data_partition():
    assert "umask 077" in UPDATE
    assert 'REPO_DIR_REAL=$(cd "$REPO_DIR" && pwd -P)' in UPDATE
    assert '[[ "$REPO_DIR_REAL" != "$FIXED_REPO_DIR_REAL" ]]' in UPDATE
    assert "STATE_DIR=${STATE_DIR:-/mnt/data/wb-irrigation-state}" in UPDATE
    assert 'BACKUP_BASE=${BACKUP_BASE:-"${DEPLOY_CONTROL_DIR}/backups"}' in UPDATE
    assert "canonicalize_backup_base" in UPDATE
    assert '[[ -L "$BACKUP_BASE" ]]' in UPDATE
    assert '[[ "$backup_name" != "backups"' in UPDATE
    assert 'mkdir -p -m 700 "$BACKUP_BASE"' in UPDATE
    assert 'mkdir -m 700 "$BACKUP_DIR"' in UPDATE
    assert 'chmod 600 "$backup_path"' in UPDATE
    assert 'chmod 600 "$ARCHIVE"' in UPDATE
    assert 'chmod 600 "$STATE_ARCHIVE"' in UPDATE
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
    assert "commit_sha:" in DEPLOY
    assert "environment: production" in DEPLOY
    assert "TARGET_COMMIT: ${{ inputs.commit_sha }}" in DEPLOY
    assert '[[ "$TARGET_COMMIT" =~ ^[0-9a-f]{40}$ ]]' in DEPLOY
    assert "git fetch --quiet origin" in DEPLOY
    assert 'git merge-base --is-ancestor "$TARGET_COMMIT" "refs/remotes/origin/main"' in DEPLOY
    assert 'git show "${TARGET_COMMIT}:update_server.sh"' in DEPLOY
    assert 'bash "$STAGED_UPDATER" --yes --branch main --commit "$TARGET_COMMIT"' in DEPLOY
    assert "TARGET_COMMIT=$(git rev-parse" not in DEPLOY
    assert "bash ./update_server.sh --yes" not in DEPLOY
    assert "git pull" not in DEPLOY
    assert "pip install" not in DEPLOY
    assert "systemctl restart" not in DEPLOY
    assert "|| echo" not in DEPLOY


def test_deploy_workflow_has_native_host_key_pinned_transport():
    assert "appleboy/ssh-action" not in DEPLOY
    assert "drone-ssh" not in DEPLOY
    assert "ssh-keyscan" in DEPLOY
    assert "ssh-keygen -lf -" in DEPLOY
    assert "StrictHostKeyChecking=yes" in DEPLOY
    assert "github.ref == 'refs/heads/main'" in DEPLOY
    assert '[[ -n "$WB_SSH_FINGERPRINT" ]]' in DEPLOY
    assert '[[ -n "$JUMP_SSH_FINGERPRINT" ]]' in DEPLOY


def test_install_and_update_require_authorized_full_commit_sha():
    for script in (INSTALL, UPDATE):
        assert "TARGET_COMMIT=${TARGET_COMMIT:-}" in script
        assert "require_full_commit_sha" in script
        assert "resolve_authorized_target_commit" in script
        assert 'merge-base --is-ancestor "$target_commit" "refs/remotes/origin/$BRANCH"' in script
        assert "UPDATE_REF=${TARGET_COMMIT:-origin/$BRANCH}" not in script


def test_native_installers_require_the_hash_locked_production_dependencies():
    for script in (INSTALL, UPDATE):
        assert "requirements.lock" in script
        assert '"$UV_BIN" pip install' in script
        assert "--require-hashes" in script
        assert "requirements.txt" not in "\n".join(
            line for line in script.splitlines() if not line.lstrip().startswith("#")
        )


def test_native_mutators_share_a_nonblocking_process_lock():
    for script in (INSTALL, UPDATE, UNINSTALL):
        assert "/run/lock/wb-irrigation/deploy.lock" in script
        assert 'chmod 0700 "$DEPLOY_LOCK_DIR"' in script
        assert "descriptor_identity" in script
        assert "acquire_deploy_lock" in script
        assert "flock -n" in script


def test_fixed_systemd_contract_rejects_inconsistent_overrides_before_mutation():
    assert "validate_fixed_deployment_contract" in INSTALL
    assert "validate_fixed_deployment_contract" in UPDATE
    install_validation = INSTALL.rindex("validate_fixed_deployment_contract")
    update_validation = UPDATE.rindex("validate_fixed_deployment_contract")
    assert install_validation < INSTALL.rindex("apt-get update -qq")
    assert update_validation < UPDATE.index("git status --porcelain")
    for fixed_value in (
        "/opt/wb-irrigation/irrigation",
        "/mnt/data/wb-irrigation",
        "/opt/wb-irrigation/.env",
        "/mnt/data/wb-irrigation-state",
        "wb-irrigation",
    ):
        assert fixed_value in INSTALL
    for fixed_value in (
        "/opt/wb-irrigation/irrigation",
        "/opt/wb-irrigation/.env",
        "/mnt/data/wb-irrigation-state",
        "wb-irrigation",
    ):
        assert fixed_value in UPDATE


def test_repeat_install_uses_transactional_updater_and_migrates_legacy_code_first():
    assert "redirect_existing_install_to_updater" in INSTALL
    handoff = INSTALL.rindex("redirect_existing_install_to_updater")
    assert handoff < INSTALL.rindex("apt-get update -qq")
    assert 'TARGET_COMMIT="$TARGET_COMMIT"' in INSTALL
    assert 'git -C "$existing_repo" show "${resolved_target}:update_server.sh"' in INSTALL
    assert 'bash "$existing_updater" --yes --branch "$BRANCH" --commit "$TARGET_COMMIT"' in INSTALL
    assert 'grep -Fq "trap finish_update EXIT" "$existing_updater"' in INSTALL
    assert "rollback_legacy_app_tree_migration" in INSTALL
    assert 'WB_IRRIGATION_HANDOFF_SERVICE_WAS_ACTIVE="$service_was_active"' in INSTALL
    assert 'WB_IRRIGATION_DEFER_ROLLBACK_RESTART="$service_was_active"' in INSTALL


def test_systemd_restart_limits_are_unit_scoped_and_service_is_unprivileged():
    unit_section, service_and_install = SERVICE.split("[Service]", maxsplit=1)
    service_section = service_and_install.split("[Install]", maxsplit=1)[0]
    assert "StartLimitBurst=5" in unit_section
    assert "StartLimitIntervalSec=300" in unit_section
    assert "StartLimitBurst" not in service_section
    assert "StartLimitIntervalSec" not in service_section
    assert "RequiresMountsFor=/mnt/data" in unit_section
    for directive in (
        "User=wb-irrigation",
        "Group=wb-irrigation",
        "WorkingDirectory=/mnt/data/wb-irrigation-state",
        "ReadWritePaths=/mnt/data/wb-irrigation-state",
        "BindPaths=/mnt/data/wb-irrigation-state/static/media/maps:/mnt/data/wb-irrigation/static/media/maps",
        "BindPaths=/mnt/data/wb-irrigation-state/static/media/zones:/mnt/data/wb-irrigation/static/media/zones",
        "EnvironmentFile=-/opt/wb-irrigation/.env",
        "EnvironmentFile=/opt/wb-irrigation/release.env",
        "Environment=PYTHONDONTWRITEBYTECODE=1",
        "ExecStart=/opt/wb-irrigation/irrigation/venv/bin/python /opt/wb-irrigation/irrigation/run.py",
    ):
        assert directive in service_section
    for directive in (
        "NoNewPrivileges=true",
        "PrivateTmp=true",
        "ProtectSystem=strict",
        "ProtectKernelTunables=true",
        "ProtectKernelModules=true",
        "ProtectControlGroups=true",
        "RestrictSUIDSGID=true",
        "LockPersonality=true",
        "RestrictRealtime=true",
        "UMask=0077",
    ):
        assert directive in service_section


def test_install_and_update_manage_dedicated_account_state_and_code_ownership():
    for script in (INSTALL, UPDATE):
        assert "ensure_service_account" in script
        assert "prepare_state_layout" in script
        assert "sync_mutable_state_tree" in script
        assert "secure_code_tree" in script
        assert 'chown root:"$SERVICE_GROUP" "$ENV_FILE"' in script
        assert 'chmod 0640 "$ENV_FILE"' in script
        assert "STATE_DIR=${STATE_DIR:-/mnt/data/wb-irrigation-state}" in script
        assert "PYTHON_INSTALL_DIR=${PYTHON_INSTALL_DIR:-/mnt/data/wb-irrigation-python}" in script
        assert 'UV_PYTHON_INSTALL_DIR="$PYTHON_INSTALL_DIR"' in script
        assert "verify_service_runtime_access" in script
        assert '[[ "$uid" == "0" || "$gid" == "0" ]]' in script
        assert script.index("prepare_state_layout") < script.rindex("systemctl restart")


def test_uninstall_removes_runtime_config_but_preserves_data_without_explicit_purge():
    assert "LOGROTATE_TARGET=/etc/logrotate.d/wb-irrigation" in UNINSTALL
    assert 'rm -f -- "$LOGROTATE_TARGET"' in UNINSTALL
    assert "--purge-data" in UNINSTALL
    assert '[[ "$PURGE_DATA" == "1" ]]' in UNINSTALL
    assert 'rm -rf -- "$DATA_DIR"' in UNINSTALL
    assert 'rm -rf -- "$STATE_DIR"' in UNINSTALL
    assert 'rm -rf -- "$PYTHON_INSTALL_DIR"' in UNINSTALL
    assert 'userdel "$SERVICE_USER"' in UNINSTALL
    assert "No leftovers should remain" not in UNINSTALL
    assert "данные сохранены" in UNINSTALL


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
    assert "EnvironmentFile=-/mnt/data/wb-irrigation-state/.env" not in SERVICE
    assert "EnvironmentFile=-/opt/wb-irrigation/.env" in SERVICE
    assert "configured_port" in INSTALL
    assert "configured_port" in UPDATE
    assert "SMOKE_PORT" not in INSTALL
    assert "SMOKE_PORT" not in UPDATE
    assert 'upsert_env_value PORT "$VALIDATED_PORT" "$ENV_FILE"' in UPDATE
    assert 'configured_port "$ENV_FILE"' in UPDATE
    assert 'configured_port "$ENV_FILE"' in INSTALL
    assert 'APP_READY_URL="${APP_SCHEME}://${url_host}:${APP_PORT}/readyz"' in INSTALL
    assert 'APP_READY_URL="${APP_SCHEME}://${url_host}:${APP_PORT}/readyz"' in UPDATE
    assert "checks', {}).get('scheduler'" not in UPDATE


def test_native_deploy_enforces_safe_http_transport_and_scheme_aware_smoke_check():
    assert "EnvironmentFile=-/opt/wb-irrigation/.env" in SERVICE
    for script in (INSTALL, UPDATE):
        assert "configured_http_scheme" in script
        assert "validate_http_transport_contract" in script
        assert "WB_HTTP_TLS_CERTFILE" in script
        assert "WB_HTTP_TLS_KEYFILE" in script
        assert "WB_HTTP_PROBE_CA_FILE" in script
        assert "WB_HTTP_PROBE_HOST" in script
        assert "WB_HTTP_PROBE_INSECURE_TLS" in script
        assert "WB_HTTP_ALLOW_INSECURE_EXTERNAL" in script
        assert 'ensure_env_default WB_HTTP_BIND_HOST 127.0.0.1 "$env_file"' in script
        assert "probe_ready_endpoint" in script
        assert "--write-out '%{http_code}'" in script
        assert '[[ "$http_status" == "200" ]]' in script
        assert '"${APP_SCHEME}://127.0.0.1:${APP_PORT}/readyz"' not in script
        assert "--insecure" in script


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
    assert 'retire_telegram_file_logs "$APP_DIR"' in INSTALL


def test_deploy_requires_telegram_runtime_retirement_before_pii_cleanup():
    update_guard = 'require_retired_telegram_runtime_commit "$REPO_DIR_REAL" "$RESOLVED_TARGET"'
    install_preflight_guard = 'require_retired_telegram_runtime_commit "$DATA_DIR" "$INSTALL_TARGET_COMMIT"'
    install_guard = 'require_retired_telegram_runtime_tree "$APP_DIR"'
    assert update_guard in UPDATE
    assert install_preflight_guard in INSTALL
    assert install_guard in INSTALL
    assert UPDATE.index(update_guard) < UPDATE.rindex("initialize_rollback_state")
    assert INSTALL.index(install_preflight_guard) < INSTALL.index('retire_telegram_file_logs "$APP_DIR"')
    assert INSTALL.index(install_guard) < INSTALL.index('retire_telegram_file_logs "$APP_DIR"')
    assert INSTALL.index(install_guard) < INSTALL.index('systemctl restart "$SERVICE_NAME"')
    for script in (INSTALL, UPDATE):
        assert "TELEGRAM_LOG_RETIREMENT_BLOB=e4fc7b282236aac7675c879b892cbf1192dc66f0" in script
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


def test_update_rollback_gates_databases_and_stages_restores_next_to_live_files():
    assert UPDATE.index("DB_EXISTED_BEFORE=1") < UPDATE.index(
        'backup_sqlite_database "$DB_PATH" "$DB_BACKUP" "irrigation.db"'
    )
    assert UPDATE.index("JOBS_DB_EXISTED_BEFORE=1") < UPDATE.index(
        'backup_sqlite_database "$JOBS_DB_PATH" "$JOBS_DB_BACKUP" "jobs.db"'
    )
    assert 'staged_path=$(mktemp "$database_dir/.${database_name}.rollback-stage.XXXXXX")' in UPDATE
    assert 'mv -f -- "$staged_path" "$database_path"' in UPDATE
    assert '"${DB_BACKUP_VERIFIED:-0}"' in UPDATE
    assert UPDATE.index("DATABASES_MAY_HAVE_MUTATED=1") < UPDATE.rindex('systemctl restart "$SERVICE"')


def test_native_state_trust_boundary_is_root_owned_and_external_to_service_state():
    for script in (INSTALL, UPDATE):
        assert "/mnt/data/wb-irrigation-deploy" in script
        assert "merge_legacy_env_defaults" in script
        assert 'configured_port "$ENV_FILE"' in script
        assert 'configured_port "$ENV_FILE" "$STATE_DIR/.env"' not in script
        assert "publish_release_environment" in script
        assert "WB_APP_VERSION=" in script
        assert "migrate_mqtt_tls_paths" in script
    assert "EnvironmentFile=-/mnt/data/wb-irrigation-state/.env" not in SERVICE
    assert "EnvironmentFile=/opt/wb-irrigation/release.env" in SERVICE
    assert 'install -m 0600 /dev/null "$DEPLOY_CONTROL_DIR/layout-v1"' in INSTALL
