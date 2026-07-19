"""Executable failure-injection checks for native deployment shell helpers."""

import os
import shlex
import sqlite3
import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
UPDATE = ROOT / "update_server.sh"
INSTALL = ROOT / "install_wb.sh"


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


def test_backup_base_rejects_broad_relative_noncanonical_and_symlink_paths(tmp_path):
    repository = tmp_path / "repo"
    outside = tmp_path / "outside"
    repository.mkdir()
    outside.mkdir()
    update = shlex.quote(str(UPDATE))
    repo = shlex.quote(str(repository))
    valid = shlex.quote(str(repository / "backups"))
    wrong_child = shlex.quote(str(repository / "snapshots"))

    _bash(
        f"""
        source {update}
        REPO_DIR_REAL={repo}
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
        STAGED_VENV={shlex.quote(str(repository / '.venv-staged-test'))}
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
    repository = tmp_path / "repo"
    telegram_source = repository / "services" / "telegram_bot.py"
    telegram_source.parent.mkdir(parents=True)
    subprocess.run(["git", "init", "-q"], cwd=repository, check=True)
    subprocess.run(["git", "config", "user.email", "deploy-test@example.invalid"], cwd=repository, check=True)
    subprocess.run(["git", "config", "user.name", "Deploy Test"], cwd=repository, check=True)

    telegram_source.write_text(
        'import logging\nlogging.FileHandler("services/logs/telegram.txt")\n',
        encoding="utf-8",
    )
    subprocess.run(["git", "add", "services/telegram_bot.py"], cwd=repository, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "legacy file log"], cwd=repository, check=True)
    legacy_commit = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=repository, text=True).strip()

    telegram_source.write_text(
        'import logging\nlogger = logging.getLogger("telegram")\n',
        encoding="utf-8",
    )
    subprocess.run(["git", "add", "services/telegram_bot.py"], cwd=repository, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "retire file log"], cwd=repository, check=True)
    retired_commit = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=repository, text=True).strip()

    legacy_tree = tmp_path / "legacy-tree"
    retired_tree = tmp_path / "retired-tree"
    for tree, source in (
        (legacy_tree, 'import logging\nlogging.FileHandler("services/logs/telegram.txt")\n'),
        (retired_tree, 'import logging\nlogger = logging.getLogger("telegram")\n'),
    ):
        tree_source = tree / "services" / "telegram_bot.py"
        tree_source.parent.mkdir(parents=True)
        tree_source.write_text(source, encoding="utf-8")

    _bash(
        f"""
        source {shlex.quote(str(UPDATE))}
        if require_retired_telegram_runtime_commit \
          {shlex.quote(str(repository))} {shlex.quote(legacy_commit)}; then
          exit 97
        fi
        require_retired_telegram_runtime_commit \
          {shlex.quote(str(repository))} {shlex.quote(retired_commit)}
        """
    )
    _bash(
        f"""
        source {shlex.quote(str(INSTALL))}
        if require_retired_telegram_runtime_tree {shlex.quote(str(legacy_tree))}; then
          exit 98
        fi
        require_retired_telegram_runtime_tree {shlex.quote(str(retired_tree))}
        """
    )
