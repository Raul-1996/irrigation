"""Executable failure-injection checks for native deployment shell helpers."""

import os
import shlex
import shutil
import sqlite3
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
UPDATE = ROOT / "update_server.sh"
INSTALL = ROOT / "install_wb.sh"
UNINSTALL = ROOT / "uninstall_wb.sh"


def _bash(script: str, *, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    run_env = os.environ.copy()
    run_env.pop("PORT", None)
    if env:
        run_env.update(env)
    return subprocess.run(
        ["bash", "-c", script],
        cwd=ROOT,
        env=run_env,
        check=True,
        capture_output=True,
        text=True,
    )


def test_port_resolution_matches_runtime_environment_precedence(tmp_path):
    environment_file = tmp_path / "systemd.env"
    repository_file = tmp_path / "repo.env"
    repository_file.write_text("PORT=8123\n", encoding="utf-8")
    update = shlex.quote(str(UPDATE))
    external = shlex.quote(str(environment_file))
    repository = shlex.quote(str(repository_file))

    _bash(
        f"""
        source {update}
        [[ $(configured_port {external} {repository}) == 8123 ]]
        printf 'PORT=8234\n' > {external}
        [[ $(configured_port {external} {repository}) == 8234 ]]
        PORT=8345
        [[ $(configured_port {external} {repository}) == 8345 ]]
        """
    )


def test_http_transport_contract_defaults_safe_and_rejects_implicit_external_plaintext(tmp_path):
    environment_file = tmp_path / "systemd.env"
    repository_file = tmp_path / "repo.env"
    external = shlex.quote(str(environment_file))
    repository = shlex.quote(str(repository_file))

    for deploy_script in (UPDATE, INSTALL):
        _bash(
            f"""
            source {shlex.quote(str(deploy_script))}
            [[ $(configured_http_scheme {external} {repository}) == http ]]

            printf '%s\n' \
              'WB_HTTP_BIND_HOST=0.0.0.0' \
              'WB_HTTP_TLS_CERTFILE=/etc/wb-irrigation/tls.crt' \
              'WB_HTTP_TLS_KEYFILE=/etc/wb-irrigation/tls.key' > {external}
            validate_http_transport_contract {external} {repository}
            [[ $(configured_http_scheme {external} {repository}) == https ]]

            printf '%s\n' 'WB_HTTP_BIND_HOST=0.0.0.0' > {external}
            if validate_http_transport_contract {external} {repository}; then
              exit 107
            fi

            printf '%s\n' \
              'WB_HTTP_BIND_HOST=0.0.0.0' \
              'WB_HTTP_ALLOW_INSECURE_EXTERNAL=1' > {external}
            validate_http_transport_contract {external} {repository}
            [[ $(configured_http_scheme {external} {repository}) == http ]]

            printf '%s\n' \
              'WB_HTTP_BIND_HOST=127.0.0.1' \
              'WB_HTTP_TLS_CERTFILE=/etc/wb-irrigation/tls.crt' > {external}
            if validate_http_transport_contract {external} {repository}; then
              exit 108
            fi
            """
        )


def test_ready_probe_derives_safe_host_and_requires_explicit_tls_verification_override(tmp_path):
    environment_file = tmp_path / "systemd.env"
    repository_file = tmp_path / "state.env"
    ca_file = tmp_path / "ca.pem"
    ca_file.write_text("test CA\n", encoding="utf-8")
    external = shlex.quote(str(environment_file))
    repository = shlex.quote(str(repository_file))
    for deploy_script in (UPDATE, INSTALL):
        _bash(
            f"""
            source {shlex.quote(str(deploy_script))}
            APP_PORT=8443
            printf '%s\n' \
              'WB_HTTP_BIND_HOST=0.0.0.0' \
              'WB_HTTP_TLS_CERTFILE=/etc/wb-irrigation/tls.crt' \
              'WB_HTTP_TLS_KEYFILE=/etc/wb-irrigation/tls.key' \
              'WB_HTTP_PROBE_HOST=wb.local' \
              'WB_HTTP_PROBE_CA_FILE={ca_file}' > {external}
            validate_http_transport_contract {external} {repository}
            configure_ready_probe {external} {repository}
            [[ "$APP_READY_URL" == 'https://wb.local:8443/readyz' ]]
            [[ " ${{READY_CURL_ARGS[*]}} " == *' --cacert {ca_file} '* ]]
            [[ " ${{READY_CURL_ARGS[*]}} " != *' --insecure '* ]]

            printf '%s\n' \
              'WB_HTTP_BIND_HOST=::' \
              'WB_HTTP_TLS_CERTFILE=/etc/wb-irrigation/tls.crt' \
              'WB_HTTP_TLS_KEYFILE=/etc/wb-irrigation/tls.key' \
              'WB_HTTP_PROBE_INSECURE_TLS=1' > {external}
            validate_http_transport_contract {external} {repository}
            configure_ready_probe {external} {repository}
            [[ "$APP_READY_URL" == 'https://[::1]:8443/readyz' ]]
            [[ " ${{READY_CURL_ARGS[*]}} " == *' --insecure '* ]]

            printf '%s\n' \
              'WB_HTTP_BIND_HOST=0.0.0.0' \
              'WB_HTTP_TLS_CERTFILE=/etc/wb-irrigation/tls.crt' \
              'WB_HTTP_TLS_KEYFILE=/etc/wb-irrigation/tls.key' \
              'WB_HTTP_PROBE_HOST=127.0.0.1/path' > {external}
            if validate_http_transport_contract {external} {repository}; then
              exit 110
            fi

            printf '%s\n' \
              'WB_HTTP_BIND_HOST=0.0.0.0' \
              'WB_HTTP_TLS_CERTFILE=/etc/wb-irrigation/tls.crt' \
              'WB_HTTP_TLS_KEYFILE=/etc/wb-irrigation/tls.key' \
              'WB_HTTP_PROBE_CA_FILE={ca_file}' \
              'WB_HTTP_PROBE_INSECURE_TLS=1' > {external}
            if validate_http_transport_contract {external} {repository}; then
              exit 111
            fi
            """
        )


def test_backup_base_rejects_broad_relative_noncanonical_and_symlink_paths(tmp_path):
    state_dir = tmp_path / "state"
    outside = tmp_path / "outside"
    state_dir.mkdir()
    outside.mkdir()
    update = shlex.quote(str(UPDATE))
    state = shlex.quote(str(state_dir))
    valid = shlex.quote(str(state_dir / "deploy-backups"))
    wrong_child = shlex.quote(str(state_dir / "snapshots"))

    _bash(
        f"""
        source {update}
        STATE_DIR={state}
        BACKUP_BASE={valid}
        canonicalize_backup_base
        [[ "$BACKUP_BASE" == {valid} ]]
        for bad in . /mnt/data {wrong_child}; do
          if (BACKUP_BASE="$bad"; canonicalize_backup_base); then
            exit 91
          fi
        done
        ln -s {shlex.quote(str(outside))} {valid}
        if (BACKUP_BASE={valid}; canonicalize_backup_base); then
          exit 92
        fi
        """
    )


def test_mutable_state_sync_copies_databases_secrets_backups_and_media(tmp_path):
    source = tmp_path / "source"
    target = tmp_path / "target"
    (source / "backups").mkdir(parents=True)
    (source / "static" / "media" / "zones").mkdir(parents=True)
    (source / "backups" / "app.log").write_text("log\n", encoding="utf-8")
    (source / "static" / "media" / "zones" / "ZONE_1.webp").write_bytes(b"image")
    (source / ".secret_key").write_text("session-key\n", encoding="utf-8")
    (source / ".irrig_secret_key").write_bytes(b"x" * 32)
    for database_name in ("irrigation.db", "jobs.db"):
        with sqlite3.connect(source / database_name) as connection:
            connection.execute("CREATE TABLE marker (value TEXT NOT NULL)")
            connection.execute("INSERT INTO marker VALUES ('state')")

    _bash(
        f"""
        source {shlex.quote(str(UPDATE))}
        chown() {{ :; }}
        sync_mutable_state_tree \
          {shlex.quote(str(source))} {shlex.quote(str(target))} root root
        """
    )

    assert (target / ".secret_key").read_text(encoding="utf-8") == "session-key\n"
    assert (target / ".irrig_secret_key").read_bytes() == b"x" * 32
    assert (target / "backups" / "app.log").read_text(encoding="utf-8") == "log\n"
    assert (target / "static" / "media" / "zones" / "ZONE_1.webp").read_bytes() == b"image"
    for database_name in ("irrigation.db", "jobs.db"):
        with sqlite3.connect(target / database_name) as connection:
            assert connection.execute("SELECT value FROM marker").fetchone() == ("state",)


def test_mutable_state_sync_refuses_symlinked_source_tree(tmp_path):
    source = tmp_path / "source"
    target = tmp_path / "target"
    outside = tmp_path / "outside"
    source.mkdir()
    outside.mkdir()
    (source / "backups").symlink_to(outside, target_is_directory=True)

    _bash(
        f"""
        source {shlex.quote(str(UPDATE))}
        chown() {{ :; }}
        if sync_mutable_state_tree \
          {shlex.quote(str(source))} {shlex.quote(str(target))} root root; then
          exit 109
        fi
        [[ ! -e {shlex.quote(str(target / "backups"))} ]]
        """
    )


def test_full_commit_validation_rejects_floating_and_unauthorized_revisions(tmp_path):
    repository = tmp_path / "repo"
    repository.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repository, check=True)
    subprocess.run(["git", "config", "user.email", "deploy-test@example.invalid"], cwd=repository, check=True)
    subprocess.run(["git", "config", "user.name", "Deploy Test"], cwd=repository, check=True)
    tracked = repository / "tracked.txt"
    tracked.write_text("authorized\n", encoding="utf-8")
    subprocess.run(["git", "add", "tracked.txt"], cwd=repository, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "authorized"], cwd=repository, check=True)
    authorized = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=repository, text=True).strip()
    subprocess.run(
        ["git", "update-ref", "refs/remotes/origin/main", authorized],
        cwd=repository,
        check=True,
    )
    subprocess.run(["git", "checkout", "-q", "--orphan", "untrusted"], cwd=repository, check=True)
    subprocess.run(["git", "rm", "-q", "-f", "tracked.txt"], cwd=repository, check=True)
    tracked.write_text("untrusted\n", encoding="utf-8")
    subprocess.run(["git", "add", "tracked.txt"], cwd=repository, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "untrusted"], cwd=repository, check=True)
    untrusted = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=repository, text=True).strip()

    for deploy_script in (UPDATE, INSTALL):
        _bash(
            f"""
            source {shlex.quote(str(deploy_script))}
            BRANCH=main
            require_full_commit_sha {shlex.quote(authorized)}
            [[ $(resolve_authorized_target_commit \
              {shlex.quote(str(repository))} {shlex.quote(authorized)}) == {shlex.quote(authorized)} ]]
            for invalid in '' main HEAD {shlex.quote(authorized[:12])} {shlex.quote(authorized.upper())}; do
              if require_full_commit_sha "$invalid"; then
                exit 102
              fi
            done
            if resolve_authorized_target_commit \
              {shlex.quote(str(repository))} {shlex.quote(untrusted)}; then
              exit 103
            fi
            """
        )


def test_install_update_and_uninstall_contend_on_the_same_process_lock(tmp_path):
    lock_file = tmp_path / "deploy.lock"
    quoted_lock = shlex.quote(str(lock_file))
    quoted_install = shlex.quote(str(INSTALL))
    quoted_update = shlex.quote(str(UPDATE))
    quoted_uninstall = shlex.quote(str(UNINSTALL))

    _bash(
        f"""
        if ! command -v flock >/dev/null 2>&1; then
          flock() {{
            local descriptor=$2
            python3 - "$descriptor" <<'PY'
import fcntl
import sys

try:
    fcntl.flock(int(sys.argv[1]), fcntl.LOCK_EX | fcntl.LOCK_NB)
except BlockingIOError:
    raise SystemExit(1)
PY
          }}
          export -f flock
        fi
        source {quoted_update}
        DEPLOY_LOCK_FILE={quoted_lock}
        acquire_deploy_lock
        for contender in {quoted_install} {quoted_update} {quoted_uninstall}; do
          if env -u WB_IRRIGATION_DEPLOY_LOCK_FD DEPLOY_LOCK_FILE={quoted_lock} \
            bash -c 'deploy_script=$1; set --; source "$deploy_script"; acquire_deploy_lock' \
            _ "$contender"; then
            exit 104
          fi
        done
        WB_IRRIGATION_DEPLOY_LOCK_FD=9 DEPLOY_LOCK_FILE={quoted_lock} \
          bash -c 'deploy_script=$1; set --; source "$deploy_script"; acquire_deploy_lock' \
          _ {quoted_update}
        """
    )


def test_fixed_unit_contract_rejects_inconsistent_runtime_overrides():
    _bash(
        f"""
        source {shlex.quote(str(UPDATE))}
        validate_fixed_deployment_contract
        for assignment in \
          'REPO_DIR=/srv/irrigation' \
          'BRANCH=release' \
          'SERVICE=other-service' \
          'ENV_FILE=/tmp/irrigation.env' \
          'PY_VERSION=3.11.14' \
          'UV_BIN=/usr/local/bin/uv'; do
          if (eval "$assignment"; validate_fixed_deployment_contract); then
            exit 105
          fi
        done
        """
    )
    _bash(
        f"""
        source {shlex.quote(str(INSTALL))}
        validate_fixed_deployment_contract
        for assignment in \
          'DATA_DIR=/srv/irrigation' \
          'BRANCH=release' \
          'APP_LINK=/srv/irrigation-link' \
          'SERVICE_NAME=other-service' \
          'ENV_FILE=/tmp/irrigation.env' \
          'PY_VERSION=3.11.14' \
          'UV_BIN=/usr/local/bin/uv'; do
          if (eval "$assignment"; validate_fixed_deployment_contract); then
            exit 106
          fi
        done
        """
    )


def test_rollback_restores_previous_commit_and_venv_after_activation_failure(tmp_path):
    repository = tmp_path / "repo"
    repository.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repository, check=True)
    subprocess.run(["git", "config", "user.email", "deploy-test@example.invalid"], cwd=repository, check=True)
    subprocess.run(["git", "config", "user.name", "Deploy Test"], cwd=repository, check=True)
    tracked = repository / "tracked.txt"
    tracked.write_text("old\n", encoding="utf-8")
    subprocess.run(["git", "add", "tracked.txt"], cwd=repository, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "old"], cwd=repository, check=True)
    original_commit = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=repository, text=True).strip()
    tracked.write_text("new\n", encoding="utf-8")
    subprocess.run(["git", "add", "tracked.txt"], cwd=repository, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "new"], cwd=repository, check=True)

    live_venv = repository / "venv"
    old_venv = repository / ".venv-rollback-test"
    live_venv.mkdir()
    old_venv.mkdir()
    (live_venv / "marker").write_text("new", encoding="utf-8")
    (old_venv / "marker").write_text("old", encoding="utf-8")
    backup_dir = tmp_path / "backup"
    backup_dir.mkdir()
    live_db = repository / "irrigation.db"
    backup_db = backup_dir / "irrigation.db"
    live_jobs_db = repository / "jobs.db"
    backup_jobs_db = backup_dir / "jobs.db"
    for live_database, backup_database in ((live_db, backup_db), (live_jobs_db, backup_jobs_db)):
        with sqlite3.connect(live_database) as connection:
            connection.execute("CREATE TABLE marker (value TEXT NOT NULL)")
            connection.execute("INSERT INTO marker VALUES ('old')")
            connection.commit()
            with sqlite3.connect(backup_database) as backup_connection:
                connection.backup(backup_connection)
            connection.execute("UPDATE marker SET value = 'new'")
            connection.commit()
    service_log = tmp_path / "systemctl.log"

    _bash(
        f"""
        source {shlex.quote(str(UPDATE))}
        systemctl() {{
          printf '%s\n' "$*" >> {shlex.quote(str(service_log))}
          return 0
        }}
        REPO_DIR_REAL={shlex.quote(str(repository))}
        ORIGINAL_COMMIT={shlex.quote(original_commit)}
        CODE_UPDATED=1
        VENV_DIR={shlex.quote(str(live_venv))}
        OLD_VENV_BACKUP={shlex.quote(str(old_venv))}
        STAGED_VENV={shlex.quote(str(repository / ".venv-staged-test"))}
        VENV_SWAPPED=1
        HAD_OLD_VENV=1
        DB_PATH={shlex.quote(str(live_db))}
        DB_BACKUP={shlex.quote(str(backup_db))}
        JOBS_DB_PATH={shlex.quote(str(live_jobs_db))}
        JOBS_DB_BACKUP={shlex.quote(str(backup_jobs_db))}
        HAD_DB=1
        HAD_JOBS_DB=1
        SERVICE_WAS_ACTIVE=1
        SERVICE=wb-irrigation
        UNIT_CHANGED=0
        LOGROTATE_CHANGED=0
        ENV_CHANGED=0
        rollback_update
        """
    )

    assert tracked.read_text(encoding="utf-8") == "old\n"
    assert (live_venv / "marker").read_text(encoding="utf-8") == "old"
    for restored_database in (live_db, live_jobs_db):
        with sqlite3.connect(restored_database) as connection:
            assert connection.execute("SELECT value FROM marker").fetchone() == ("old",)
    calls = service_log.read_text(encoding="utf-8")
    assert "stop wb-irrigation" in calls
    assert "start wb-irrigation" in calls


def test_legacy_migration_enospc_keeps_live_tree_and_link_untouched(tmp_path):
    app_link = tmp_path / "legacy-app"
    data_dir = tmp_path / "data-app"
    app_link.mkdir()
    (app_link / ".git").mkdir()
    (app_link / "marker").write_text("live", encoding="utf-8")

    _bash(
        f"""
        source {shlex.quote(str(INSTALL))}
        APP_LINK={shlex.quote(str(app_link))}
        DATA_DIR={shlex.quote(str(data_dir))}
        cp() {{ return 28; }}
        if migrate_legacy_app_tree; then
          exit 93
        fi
        [[ -d "$APP_LINK" && ! -L "$APP_LINK" ]]
        [[ $(<"$APP_LINK/marker") == live ]]
        [[ ! -e "$DATA_DIR" ]]
        if compgen -G "${{DATA_DIR}}.migration-*" >/dev/null; then
          exit 94
        fi
        """
    )


def test_prelayout_real_opt_tree_migration_can_be_fully_rolled_back(tmp_path):
    app_link = tmp_path / "opt" / "wb-irrigation" / "irrigation"
    data_dir = tmp_path / "mnt" / "data" / "wb-irrigation"
    app_link.mkdir(parents=True)
    (app_link / ".git").mkdir()
    (app_link / "marker").write_text("original\n", encoding="utf-8")

    _bash(
        f"""
        source {shlex.quote(str(INSTALL))}
        APP_LINK={shlex.quote(str(app_link))}
        DATA_DIR={shlex.quote(str(data_dir))}
        reset_legacy_migration_tracking
        migrate_legacy_app_tree
        [[ -L "$APP_LINK" ]]
        [[ $(readlink -f "$APP_LINK") == $(readlink -f "$DATA_DIR") ]]
        [[ $(<"$DATA_DIR/marker") == original ]]
        [[ -d "$LEGACY_MIGRATION_APP_BACKUP" ]]

        printf '%s\n' changed > "$DATA_DIR/marker"
        rollback_legacy_app_tree_migration
        [[ -d "$APP_LINK" && ! -L "$APP_LINK" ]]
        [[ $(<"$APP_LINK/marker") == original ]]
        [[ ! -e "$DATA_DIR" ]]
        """
    )


def test_upgrade_purges_world_readable_retired_telegram_logs_and_rotations(tmp_path):
    retired_names = (
        "telegram.txt",
        "telegram.txt.1",
        "telegram.txt.2.gz",
        "telegram.txt-20260718",
        "telegram.txt-20260717.gz",
    )

    for script_name, deploy_script in (("update", UPDATE), ("install", INSTALL)):
        application_root = tmp_path / script_name / "app"
        logs_dir = application_root / "services" / "logs"
        logs_dir.mkdir(parents=True)
        for retired_name in retired_names:
            retired_log = logs_dir / retired_name
            retired_log.write_text("PII: chat_id=123456\n", encoding="utf-8")
            retired_log.chmod(0o644)

        outside_target = tmp_path / f"{script_name}-outside.txt"
        outside_target.write_text("must survive\n", encoding="utf-8")
        symlinked_rotation = logs_dir / "telegram.txt-20260716.gz"
        symlinked_rotation.symlink_to(outside_target)
        unrelated_file = logs_dir / "telegram.txt.notes"
        unrelated_file.write_text("not a rotation\n", encoding="utf-8")

        _bash(
            f"""
            source {shlex.quote(str(deploy_script))}
            retire_telegram_file_logs {shlex.quote(str(application_root))}
            """
        )

        for retired_name in retired_names:
            assert not (logs_dir / retired_name).exists()
        assert not symlinked_rotation.exists()
        assert outside_target.read_text(encoding="utf-8") == "must survive\n"
        assert unrelated_file.read_text(encoding="utf-8") == "not a rotation\n"


def test_retired_log_cleanup_refuses_symlinked_logs_directory(tmp_path):
    outside_logs = tmp_path / "outside-logs"
    outside_logs.mkdir()
    outside_log = outside_logs / "telegram.txt"
    outside_log.write_text("PII must not be traversed\n", encoding="utf-8")

    for script_name, deploy_script in (("update", UPDATE), ("install", INSTALL)):
        application_root = tmp_path / script_name / "app"
        services_dir = application_root / "services"
        services_dir.mkdir(parents=True)
        (services_dir / "logs").symlink_to(outside_logs, target_is_directory=True)

        _bash(
            f"""
            source {shlex.quote(str(deploy_script))}
            if retire_telegram_file_logs {shlex.quote(str(application_root))}; then
              exit 95
            fi
            """
        )

    assert outside_log.read_text(encoding="utf-8") == "PII must not be traversed\n"


def test_retired_log_cleanup_fails_closed_on_enumeration_io_error(tmp_path):
    for script_name, deploy_script in (("update", UPDATE), ("install", INSTALL)):
        application_root = tmp_path / script_name / "app"
        logs_dir = application_root / "services" / "logs"
        logs_dir.mkdir(parents=True)
        retired_log = logs_dir / "telegram.txt"
        retired_log.write_text("PII must survive a failed enumeration\n", encoding="utf-8")
        retired_log.chmod(0o644)
        controlled_listing = tmp_path / f"{script_name}-enumeration.bin"

        _bash(
            f"""
            source {shlex.quote(str(deploy_script))}
            CONTROLLED_LISTING={shlex.quote(str(controlled_listing))}
            RETIRED_LOG={shlex.quote(str(retired_log))}
            mktemp() {{
              : > "$CONTROLLED_LISTING"
              printf '%s\n' "$CONTROLLED_LISTING"
            }}
            find() {{
              printf '%s\\0' "$RETIRED_LOG"
              return 42
            }}
            if retire_telegram_file_logs {shlex.quote(str(application_root))}; then
              exit 96
            fi
            [[ -f "$RETIRED_LOG" ]]
            [[ ! -e "$CONTROLLED_LISTING" ]]
            """
        )

        assert retired_log.read_text(encoding="utf-8") == "PII must survive a failed enumeration\n"


def test_deploy_guards_require_runtime_file_handler_removal(tmp_path):
    safe_blob = "e4fc7b282236aac7675c879b892cbf1192dc66f0"
    current_blob = subprocess.check_output(
        ["git", "hash-object", "services/telegram_bot.py"],
        cwd=ROOT,
        text=True,
    ).strip()
    assert current_blob == safe_blob
    safe_source = subprocess.check_output(
        ["git", "cat-file", "blob", safe_blob],
        cwd=ROOT,
    )
    assert b"handler previously created services/logs/telegram.txt" in safe_source

    repository = tmp_path / "repo"
    telegram_source = repository / "services" / "telegram_bot.py"
    telegram_source.parent.mkdir(parents=True)
    subprocess.run(["git", "init", "-q"], cwd=repository, check=True)
    subprocess.run(["git", "config", "user.email", "deploy-test@example.invalid"], cwd=repository, check=True)
    subprocess.run(["git", "config", "user.name", "Deploy Test"], cwd=repository, check=True)

    def commit_runtime(message: str) -> str:
        subprocess.run(["git", "add", "-A"], cwd=repository, check=True)
        subprocess.run(["git", "commit", "-q", "-m", message], cwd=repository, check=True)
        return subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=repository, text=True).strip()

    telegram_source.write_bytes(safe_source)
    subprocess.run(["git", "add", "services/telegram_bot.py"], cwd=repository, check=True)
    safe_commit = commit_runtime("versioned Telegram retirement runtime")
    tracked_blob = subprocess.check_output(
        ["git", "rev-parse", f"{safe_commit}:services/telegram_bot.py"],
        cwd=repository,
        text=True,
    ).strip()
    assert tracked_blob == safe_blob

    telegram_source.write_text(
        'import logging\nlogging.FileHandler("services/logs/telegram.txt")\n',
        encoding="utf-8",
    )
    actual_handler_commit = commit_runtime("actual legacy handler")

    telegram_source.write_text(
        "from logging import FileHandler as Handler\n"
        "from os.path import join\n"
        'name = "".join(("telegram", ".txt"))\n'
        'Handler(join("services", "logs", name))\n',
        encoding="utf-8",
    )
    aliased_handler_commit = commit_runtime("aliased composed legacy handler")

    telegram_source.unlink()
    (repository / "services" / "placeholder.py").write_text("# runtime absent\n", encoding="utf-8")
    absent_runtime_commit = commit_runtime("missing Telegram runtime")

    subprocess.run(["git", "rm", "-qr", "services"], cwd=repository, check=True)
    (repository / "services").symlink_to("outside-services", target_is_directory=True)
    tracked_services_symlink_commit = commit_runtime("tracked services symlink")

    subprocess.run(["git", "rm", "-q", "services"], cwd=repository, check=True)
    (repository / "services").mkdir()
    outside_runtime = tmp_path / "outside-telegram.py"
    outside_runtime.write_bytes(safe_source)
    (repository / "services" / "telegram_bot.py").symlink_to(outside_runtime)
    tracked_runtime_symlink_commit = commit_runtime("tracked runtime symlink")

    _bash(
        f"""
        source {shlex.quote(str(UPDATE))}
        require_retired_telegram_runtime_commit \
          {shlex.quote(str(repository))} {shlex.quote(safe_commit)}
        for unsafe_commit in \
          {shlex.quote(actual_handler_commit)} \
          {shlex.quote(aliased_handler_commit)} \
          {shlex.quote(absent_runtime_commit)} \
          {shlex.quote(tracked_services_symlink_commit)} \
          {shlex.quote(tracked_runtime_symlink_commit)}; do
          if require_retired_telegram_runtime_commit \
            {shlex.quote(str(repository))} "$unsafe_commit"; then
            exit 97
          fi
        done
        """
    )

    subprocess.run(["git", "reset", "--hard", "-q", safe_commit], cwd=repository, check=True)
    logs_dir = repository / "services" / "logs"
    logs_dir.mkdir()
    retired_log = logs_dir / "telegram.txt"
    retired_log.write_text("PII must remain when the target guard fails\n", encoding="utf-8")
    _bash(
        f"""
        source {shlex.quote(str(INSTALL))}
        require_retired_telegram_runtime_tree {shlex.quote(str(repository))}
        if require_retired_telegram_runtime_commit \
          {shlex.quote(str(repository))} {shlex.quote(actual_handler_commit)}; then
          retire_telegram_file_logs {shlex.quote(str(repository))}
          exit 98
        fi
        [[ -f {shlex.quote(str(retired_log))} ]]
        """
    )

    relocated_services = tmp_path / "relocated-services"
    shutil.move(str(repository / "services"), relocated_services)
    (repository / "services").symlink_to(relocated_services, target_is_directory=True)
    _bash(
        f"""
        source {shlex.quote(str(INSTALL))}
        if require_retired_telegram_runtime_tree {shlex.quote(str(repository))}; then
          exit 99
        fi
        """
    )
    (repository / "services").unlink()
    shutil.move(str(relocated_services), repository / "services")

    telegram_source = repository / "services" / "telegram_bot.py"
    telegram_source.unlink()
    telegram_source.symlink_to(outside_runtime)
    _bash(
        f"""
        source {shlex.quote(str(INSTALL))}
        if require_retired_telegram_runtime_tree {shlex.quote(str(repository))}; then
          exit 100
        fi
        """
    )
    telegram_source.unlink()
    _bash(
        f"""
        source {shlex.quote(str(INSTALL))}
        if require_retired_telegram_runtime_tree {shlex.quote(str(repository))}; then
          exit 101
        fi
        """
    )
