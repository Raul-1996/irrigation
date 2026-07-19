#!/usr/bin/env bash

# Supported native updater for Wirenboard.
# A verified backup is followed by a staged runtime activation. Any failure
# restores the previous Git revision, venv and external configuration before
# returning the previously active service to operation.

set -euo pipefail
umask 077

REPO_DIR=${REPO_DIR:-/opt/wb-irrigation/irrigation}
BRANCH=${BRANCH:-main}
TARGET_COMMIT=${TARGET_COMMIT:-}
SERVICE=${SERVICE:-wb-irrigation}
PY_VERSION=${PY_VERSION:-3.11}
UV_BIN=${UV_BIN:-/root/.local/bin/uv}
BACKUP_BASE=${BACKUP_BASE:-}
BACKUP_KEEP=${BACKUP_KEEP:-10}
ENV_FILE=${ENV_FILE:-/opt/wb-irrigation/.env}
NONINTERACTIVE=${NONINTERACTIVE:-0}

usage() {
  cat <<EOF
Usage: $(basename "$0") [--repo-dir DIR] [--branch BRANCH] [--service NAME] [--backup-dir DIR] [--yes]
Env vars: REPO_DIR, BRANCH, TARGET_COMMIT, SERVICE, BACKUP_BASE, BACKUP_KEEP, PY_VERSION, UV_BIN, ENV_FILE
Examples:
  sudo bash $0 --yes
  BRANCH=main REPO_DIR=/opt/wb-irrigation/irrigation bash $0 -y
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    -h | --help)
      usage
      exit 0
      ;;
    --repo-dir)
      REPO_DIR="$2"
      shift 2
      ;;
    --branch)
      BRANCH="$2"
      shift 2
      ;;
    --service)
      SERVICE="$2"
      shift 2
      ;;
    --backup-dir)
      BACKUP_BASE="$2"
      shift 2
      ;;
    -y | --yes)
      NONINTERACTIVE=1
      shift
      ;;
    *)
      echo "Unknown arg: $1"
      usage
      exit 1
      ;;
  esac
done

ts() { date +"%Y-%m-%d %H:%M:%S"; }
info() { echo -e "\033[1;34m[$(ts)]\033[0m $*"; }
ok() { echo -e "\033[1;32m[$(ts)] OK\033[0m $*"; }
warn() { echo -e "\033[1;33m[$(ts)] WARN\033[0m $*"; }
err() { echo -e "\033[1;31m[$(ts)] ERROR\033[0m $*" >&2; }

read_env_port() {
  local env_file=$1
  local value

  [[ -f "$env_file" ]] || return 1
  grep -q '^[[:space:]]*PORT[[:space:]]*=' "$env_file" || return 1
  value=$(sed -n 's/^[[:space:]]*PORT[[:space:]]*=[[:space:]]*//p' "$env_file" | tail -n 1)
  printf '%s' "$value" | tr -d "'\"[:space:]"
}

configured_port() {
  local environment_file=$1
  local repository_file=${2:-}
  local port=""
  local found=0

  if [[ ${PORT+x} ]]; then
    port=$PORT
    found=1
  elif port=$(read_env_port "$environment_file"); then
    found=1
  elif [[ -n "$repository_file" ]] && port=$(read_env_port "$repository_file"); then
    found=1
  fi
  if [[ "$found" == "0" ]]; then
    port=8080
  fi
  if [[ ! "$port" =~ ^[0-9]+$ ]] || ((port < 1 || port > 65535)); then
    err "Invalid PORT value: ${port:-<empty>}"
    return 1
  fi
  printf '%s\n' "$port"
}

upsert_env_value() {
  local key=$1
  local value=$2
  local env_file=$3

  mkdir -p -m 700 "$(dirname "$env_file")"
  touch "$env_file"
  chmod 600 "$env_file"
  if grep -q "^${key}=" "$env_file"; then
    sed -i.bak "s|^${key}=.*|${key}=${value}|" "$env_file"
    rm -f "${env_file}.bak"
  else
    printf '%s=%s\n' "$key" "$value" >>"$env_file"
  fi
  chmod 600 "$env_file"
}

canonicalize_backup_base() {
  local backup_parent
  local backup_name

  if [[ -z "${REPO_DIR_REAL:-}" || "$REPO_DIR_REAL" != /* ]]; then
    err "Repository path must be canonical before validating backups"
    return 1
  fi
  if [[ -z "${BACKUP_BASE:-}" || "$BACKUP_BASE" != /* ]]; then
    err "BACKUP_BASE must be an absolute dedicated directory"
    return 1
  fi
  if [[ -L "$BACKUP_BASE" ]]; then
    err "BACKUP_BASE must not be a symlink: $BACKUP_BASE"
    return 1
  fi

  backup_parent=$(dirname -- "$BACKUP_BASE")
  backup_name=$(basename -- "$BACKUP_BASE")
  if [[ "$backup_name" != "backups" || "$backup_parent" != "$REPO_DIR_REAL" ]]; then
    err "BACKUP_BASE must be the dedicated child $REPO_DIR_REAL/backups"
    return 1
  fi
  if [[ -e "$BACKUP_BASE" && ! -d "$BACKUP_BASE" ]]; then
    err "BACKUP_BASE exists but is not a directory: $BACKUP_BASE"
    return 1
  fi

  BACKUP_BASE="$REPO_DIR_REAL/backups"
}

ensure_logrotate_ready() {
  if ! command -v logrotate >/dev/null 2>&1; then
    info "Installing required logrotate package"
    apt-get update -qq
    DEBIAN_FRONTEND=noninteractive apt-get install -y logrotate
  fi
  if ! command -v logrotate >/dev/null 2>&1 || [[ ! -f /etc/logrotate.conf ]]; then
    err "logrotate runtime is incomplete"
    return 1
  fi

  logrotate -d "/etc/logrotate.conf"
  if systemctl cat logrotate.timer >/dev/null 2>&1; then
    systemctl enable --now logrotate.timer
    if ! systemctl is-enabled logrotate.timer >/dev/null 2>&1 \
      || ! systemctl is-active logrotate.timer >/dev/null 2>&1; then
      err "logrotate.timer is installed but not active"
      return 1
    fi
    info "logrotate.timer is enabled and active"
  elif [[ -x /etc/cron.daily/logrotate ]] && systemctl is-active cron >/dev/null 2>&1; then
    info "logrotate is scheduled by active cron"
  else
    err "No active logrotate.timer or cron execution path found"
    return 1
  fi
}

require_retired_telegram_runtime_commit() {
  local repository=$1
  local target_commit=$2
  local matches
  local grep_status

  if matches=$(git -C "$repository" grep -n -E \
    -e 'logging[.]FileHandler' -e 'telegram[.]txt' \
    "$target_commit" -- services/telegram_bot.py); then
    err "Target commit still creates the retired Telegram file log"
    err "$matches"
    return 1
  else
    grep_status=$?
  fi
  if [[ "$grep_status" != "1" ]]; then
    err "Could not verify Telegram logging in target commit $target_commit"
    return 1
  fi
}

retire_telegram_file_logs() {
  local application_root=$1
  local application_root_real
  local expected_logs_dir
  local logs_dir_real
  local listing_file
  local enumeration_sentinel
  local enumeration_complete=0
  local iteration_failed=0
  local candidate
  local candidate_name

  application_root_real=$(cd "$application_root" && pwd -P) || return 1
  expected_logs_dir="$application_root_real/services/logs"
  if [[ ! -e "$expected_logs_dir" && ! -L "$expected_logs_dir" ]]; then
    return 0
  fi
  if [[ -L "$application_root_real/services" || -L "$expected_logs_dir" || ! -d "$expected_logs_dir" ]]; then
    err "Refusing to traverse a non-canonical application logs directory: $expected_logs_dir"
    return 1
  fi
  logs_dir_real=$(cd "$expected_logs_dir" && pwd -P) || return 1
  if [[ "$logs_dir_real" != "$expected_logs_dir" ]]; then
    err "Application logs directory escapes the canonical repository: $logs_dir_real"
    return 1
  fi

  if ! listing_file=$(mktemp /tmp/wb-irrigation-telegram-logs.XXXXXX); then
    err "Could not create a private Telegram log enumeration file"
    return 1
  fi
  if ! chmod 600 "$listing_file"; then
    rm -f -- "$listing_file" || err "Could not remove insecure enumeration file: $listing_file"
    err "Could not make the Telegram log enumeration file private"
    return 1
  fi
  enumeration_sentinel="WB_IRRIGATION_ENUMERATION_COMPLETE_${listing_file##*/}"
  if ! find -P "$logs_dir_real" -mindepth 1 -maxdepth 1 -print0 >"$listing_file"; then
    rm -f -- "$listing_file" || err "Could not remove failed enumeration file: $listing_file"
    err "Could not enumerate retired Telegram file logs"
    return 1
  fi
  if ! printf '%s\0' "$enumeration_sentinel" >>"$listing_file"; then
    rm -f -- "$listing_file" || err "Could not remove incomplete enumeration file: $listing_file"
    err "Could not finalize retired Telegram log enumeration"
    return 1
  fi

  if ! while IFS= read -r -d '' candidate; do
    if [[ "$candidate" == "$enumeration_sentinel" ]]; then
      enumeration_complete=1
      continue
    fi
    candidate_name=${candidate##*/}
    if [[ "$candidate_name" == "telegram.txt" \
      || "$candidate_name" =~ ^telegram[.]txt([.][0-9]+|-[0-9]{8})([.]gz)?$ ]]; then
      if [[ -f "$candidate" || -L "$candidate" ]]; then
        if ! rm -f -- "$candidate"; then
          err "Could not remove retired Telegram file log: $candidate"
          iteration_failed=1
          break
        fi
        info "Removed retired Telegram file log: $candidate"
      elif [[ -e "$candidate" ]]; then
        err "Refusing to remove non-file historical log path: $candidate"
        iteration_failed=1
        break
      fi
    fi
  done <"$listing_file"; then
    iteration_failed=1
  fi

  if ! rm -f -- "$listing_file"; then
    err "Could not remove Telegram log enumeration file: $listing_file"
    return 1
  fi
  if [[ "$iteration_failed" == "1" || "$enumeration_complete" != "1" ]]; then
    err "Retired Telegram log enumeration was not processed completely"
    return 1
  fi
}

prune_old_backups() {
  local -a backups=()
  local old_backup

  while IFS= read -r -d '' old_backup; do
    backups+=("$old_backup")
  done < <(find "$BACKUP_BASE" -mindepth 1 -maxdepth 1 -type d -name '????????_??????' -print0 | sort -z)

  while ((${#backups[@]} > BACKUP_KEEP)); do
    old_backup=${backups[0]}
    info "Removing expired backup: $old_backup"
    rm -rf -- "$old_backup"
    backups=("${backups[@]:1}")
  done
}

backup_sqlite_database() {
  local database_path=$1
  local backup_path=$2
  local database_name=$3
  local check_result

  if ! sqlite3 "$database_path" ".backup '$backup_path'"; then
    err "Could not back up $database_name"
    return 1
  fi
  if ! check_result=$(sqlite3 "$backup_path" "PRAGMA quick_check;"); then
    err "Could not verify $database_name backup"
    return 1
  fi
  if [[ "$check_result" != "ok" ]]; then
    err "$database_name backup verification failed: $check_result"
    return 1
  fi
  chmod 600 "$backup_path"
}

restore_sqlite_database() {
  local database_path=$1
  local backup_path=$2
  local existed_before=$3
  local database_name=$4
  local check_result

  if ! rm -f -- "${database_path}-wal" "${database_path}-shm"; then
    err "Could not remove stale $database_name WAL files"
    return 1
  fi
  if [[ "$existed_before" == "0" ]]; then
    if ! rm -f -- "$database_path"; then
      err "Could not remove $database_name created by the failed update"
      return 1
    fi
    return 0
  fi
  if [[ ! -f "$backup_path" ]]; then
    err "$database_name rollback backup is missing: $backup_path"
    return 1
  fi
  if ! rm -f -- "$database_path"; then
    err "Could not remove the failed-update copy of $database_name"
    return 1
  fi
  if ! sqlite3 "$database_path" ".restore '$backup_path'"; then
    err "Could not restore $database_name"
    return 1
  fi
  if ! check_result=$(sqlite3 "$database_path" "PRAGMA quick_check;"); then
    err "Could not verify restored $database_name"
    return 1
  fi
  if [[ "$check_result" != "ok" ]]; then
    err "$database_name rollback verification failed: $check_result"
    return 1
  fi
  chmod 600 "$database_path"
}

rollback_update() {
  local rollback_failed=0

  warn "Update failed; restoring previous code and runtime"
  if [[ "${CODE_UPDATED:-0}" == "1" || "${SERVICE_WAS_ACTIVE:-0}" == "1" ]]; then
    if ! systemctl stop "${SERVICE:-wb-irrigation}"; then
      err "Rollback could not stop ${SERVICE:-wb-irrigation}"
      rollback_failed=1
    fi
  fi

  if [[ "${VENV_SWAPPED:-0}" == "1" ]]; then
    if [[ -e "${VENV_DIR:-}" ]] && ! rm -rf -- "$VENV_DIR"; then
      err "Rollback could not remove staged live venv"
      rollback_failed=1
    fi
    if [[ "${HAD_OLD_VENV:-0}" == "1" && -e "${OLD_VENV_BACKUP:-}" ]]; then
      if ! mv "$OLD_VENV_BACKUP" "$VENV_DIR"; then
        err "Rollback could not restore previous venv"
        rollback_failed=1
      fi
    fi
  fi
  if [[ -n "${STAGED_VENV:-}" && -e "${STAGED_VENV:-}" ]] && ! rm -rf -- "$STAGED_VENV"; then
    err "Rollback could not remove incomplete staged venv"
    rollback_failed=1
  fi

  if [[ "${CODE_UPDATED:-0}" == "1" ]]; then
    if ! git -C "$REPO_DIR_REAL" reset --hard "$ORIGINAL_COMMIT" -q; then
      err "Rollback could not restore Git revision $ORIGINAL_COMMIT"
      rollback_failed=1
    fi
    if ! restore_sqlite_database "$DB_PATH" "$DB_BACKUP" "${HAD_DB:-0}" "irrigation.db"; then
      rollback_failed=1
    fi
    if ! restore_sqlite_database "$JOBS_DB_PATH" "$JOBS_DB_BACKUP" "${HAD_JOBS_DB:-0}" "jobs.db"; then
      rollback_failed=1
    fi
  fi

  if [[ "${UNIT_CHANGED:-0}" == "1" ]]; then
    if [[ "${HAD_SERVICE_FILE:-0}" == "1" ]]; then
      install -m 0644 "$SERVICE_FILE_BACKUP" "$SERVICE_FILE" || rollback_failed=1
    else
      rm -f -- "$SERVICE_FILE" || rollback_failed=1
    fi
    systemctl daemon-reload || rollback_failed=1
  fi
  if [[ "${LOGROTATE_CHANGED:-0}" == "1" ]]; then
    if [[ "${HAD_LOGROTATE_FILE:-0}" == "1" ]]; then
      install -m 0644 "$LOGROTATE_FILE_BACKUP" "$LOGROTATE_TARGET" || rollback_failed=1
    else
      rm -f -- "$LOGROTATE_TARGET" || rollback_failed=1
    fi
  fi
  if [[ "${ENV_CHANGED:-0}" == "1" ]]; then
    if [[ "${HAD_ENV_FILE:-0}" == "1" ]]; then
      install -m 0600 "$ENV_FILE_BACKUP" "$ENV_FILE" || rollback_failed=1
    else
      rm -f -- "$ENV_FILE" || rollback_failed=1
    fi
  fi

  if [[ "${SERVICE_WAS_ACTIVE:-0}" == "1" ]]; then
    if ! systemctl start "${SERVICE:-wb-irrigation}"; then
      err "Rollback could not restart ${SERVICE:-wb-irrigation}"
      rollback_failed=1
    fi
  fi
  if [[ "$rollback_failed" == "1" ]]; then
    err "Rollback completed with errors; manual recovery from ${BACKUP_DIR:-<unknown>} is required"
    return 1
  fi
  ok "Previous code and runtime restored"
}

finish_update() {
  local status=$?
  trap - EXIT
  if [[ "$status" -ne 0 && "${UPDATE_SUCCEEDED:-0}" != "1" ]]; then
    rollback_update || true
  fi
  exit "$status"
}

# Unit tests source the real helper functions and inject failures without
# executing the privileged deployment body.
if [[ "${BASH_SOURCE[0]}" != "$0" ]]; then
  return 0
fi

if [[ "$(id -u)" -ne 0 ]]; then
  err "Run as root (sudo bash $0)"
  exit 1
fi
if [[ ! -d "$REPO_DIR/.git" ]]; then
  err "Git repository not found: $REPO_DIR"
  exit 1
fi
if [[ ! "$BACKUP_KEEP" =~ ^[0-9]+$ ]] || ((BACKUP_KEEP < 1)); then
  err "BACKUP_KEEP must be a positive integer"
  exit 1
fi

REPO_DIR_REAL=$(cd "$REPO_DIR" && pwd -P)
BACKUP_BASE=${BACKUP_BASE:-"${REPO_DIR_REAL}/backups"}
canonicalize_backup_base
cd "$REPO_DIR_REAL"
info "Target repo: $REPO_DIR_REAL (branch: $BRANCH)"

if [[ -n $(git status --porcelain --untracked-files=normal) ]]; then
  err "Deployment checkout has local changes; refusing an update that cannot be rolled back exactly"
  exit 1
fi
ORIGINAL_COMMIT=$(git rev-parse --verify HEAD)
info "Fetching target branch origin/$BRANCH"
git fetch --quiet origin "$BRANCH"
UPDATE_REF=${TARGET_COMMIT:-origin/$BRANCH}
RESOLVED_TARGET=$(git rev-parse --verify "${UPDATE_REF}^{commit}")
if [[ -n "$TARGET_COMMIT" && "$RESOLVED_TARGET" != "$TARGET_COMMIT" ]]; then
  err "Fetched target does not match requested TARGET_COMMIT=$TARGET_COMMIT"
  exit 1
fi
# Integration ordering contract: the Telegram runtime must stop creating the
# legacy file before this deploy migration is allowed to purge it.
require_retired_telegram_runtime_commit "$REPO_DIR_REAL" "$RESOLVED_TARGET"

UPDATE_SUCCEEDED=0
SERVICE_WAS_ACTIVE=0
CODE_UPDATED=0
VENV_SWAPPED=0
HAD_OLD_VENV=0
UNIT_CHANGED=0
LOGROTATE_CHANGED=0
ENV_CHANGED=0
HAD_SERVICE_FILE=0
HAD_LOGROTATE_FILE=0
HAD_ENV_FILE=0
HAD_DB=0
HAD_JOBS_DB=0
STAGED_VENV=""
OLD_VENV_BACKUP=""
BACKUP_DIR=""
trap finish_update EXIT

if systemctl is-active "$SERVICE" >/dev/null 2>&1; then
  SERVICE_WAS_ACTIVE=1
  info "Stopping $SERVICE for a consistent backup"
  systemctl stop "$SERVICE"
  if systemctl is-active "$SERVICE" >/dev/null 2>&1; then
    err "Service did not stop: $SERVICE"
    exit 1
  fi
fi

retire_telegram_file_logs "$REPO_DIR_REAL"

mkdir -p -m 700 "$BACKUP_BASE"
chmod 700 "$BACKUP_BASE"
STAMP=$(date +"%Y%m%d_%H%M%S")
BACKUP_DIR="$BACKUP_BASE/$STAMP"
mkdir -m 700 "$BACKUP_DIR"
info "Creating verified backup at $BACKUP_DIR"

DB_PATH="$REPO_DIR_REAL/irrigation.db"
DB_BACKUP="$BACKUP_DIR/irrigation.db"
JOBS_DB_PATH="$REPO_DIR_REAL/jobs.db"
JOBS_DB_BACKUP="$BACKUP_DIR/jobs.db"
if [[ -f "$DB_PATH" ]]; then
  backup_sqlite_database "$DB_PATH" "$DB_BACKUP" "irrigation.db"
  HAD_DB=1
else
  warn "Database does not exist yet; code and secrets will still be backed up"
fi
if [[ -f "$JOBS_DB_PATH" ]]; then
  backup_sqlite_database "$JOBS_DB_PATH" "$JOBS_DB_BACKUP" "jobs.db"
  HAD_JOBS_DB=1
fi

for secret_file in "$REPO_DIR_REAL/.secret_key" "$REPO_DIR_REAL/.irrig_secret_key" "$REPO_DIR_REAL/.env"; do
  if [[ -f "$secret_file" ]]; then
    chmod 600 "$secret_file"
  fi
done

ARCHIVE="$BACKUP_DIR/repo_snapshot.tar.gz"
tar --exclude="./venv" --exclude="./.git" --exclude="./__pycache__" \
  --exclude="./backups" --exclude="./*.pid" \
  --exclude="./irrigation.db" --exclude="./irrigation.db-wal" --exclude="./irrigation.db-shm" \
  --exclude="./jobs.db" --exclude="./jobs.db-wal" --exclude="./jobs.db-shm" \
  -czf "$ARCHIVE" .
chmod 600 "$ARCHIVE"

SERVICE_FILE="/etc/systemd/system/${SERVICE}.service"
LOGROTATE_TARGET="/etc/logrotate.d/wb-irrigation"
SERVICE_FILE_BACKUP="$BACKUP_DIR/systemd-unit.service"
LOGROTATE_FILE_BACKUP="$BACKUP_DIR/logrotate.conf"
ENV_FILE_BACKUP="$BACKUP_DIR/environment.env"
if [[ -f "$SERVICE_FILE" ]]; then
  HAD_SERVICE_FILE=1
  install -m 0600 "$SERVICE_FILE" "$SERVICE_FILE_BACKUP"
fi
if [[ -f "$LOGROTATE_TARGET" ]]; then
  HAD_LOGROTATE_FILE=1
  install -m 0600 "$LOGROTATE_TARGET" "$LOGROTATE_FILE_BACKUP"
fi
if [[ -f "$ENV_FILE" ]]; then
  HAD_ENV_FILE=1
  chmod 600 "$ENV_FILE"
  install -m 0600 "$ENV_FILE" "$ENV_FILE_BACKUP"
fi
ok "Verified backup created: $BACKUP_DIR"

info "Activating target code $RESOLVED_TARGET"
CODE_UPDATED=1
git reset --hard "$RESOLVED_TARGET" -q
ok "Code updated to $(git rev-parse --short HEAD)"

find . -path ./venv -prune -o -path ./.git -prune -o -type d -name __pycache__ -print -exec rm -rf -- {} +

if [[ ! -x "$UV_BIN" ]]; then
  if command -v uv >/dev/null 2>&1; then
    UV_BIN=$(command -v uv)
  else
    info "Installing uv"
    curl -LsSf https://astral.sh/uv/install.sh | sh
  fi
fi
if [[ ! -x "$UV_BIN" ]]; then
  err "uv not found at $UV_BIN"
  exit 1
fi
export PATH="$(dirname "$UV_BIN"):$PATH"
uv python install "$PY_VERSION"

VENV_DIR="$REPO_DIR_REAL/venv"
STAGED_VENV="$REPO_DIR_REAL/.venv-staged-$STAMP-$$"
OLD_VENV_BACKUP="$REPO_DIR_REAL/.venv-rollback-$STAMP-$$"
uv venv --python "$PY_VERSION" "$STAGED_VENV"
uv pip install --python "$STAGED_VENV/bin/python" -r requirements.txt
STAGED_VERSION=$("$STAGED_VENV/bin/python" -c 'import sys; print(".".join(map(str, sys.version_info[:2])))')
if [[ "$STAGED_VERSION" != "$PY_VERSION" ]]; then
  err "Staged venv uses Python $STAGED_VERSION instead of $PY_VERSION"
  exit 1
fi

if [[ -e "$VENV_DIR" ]]; then
  mv "$VENV_DIR" "$OLD_VENV_BACKUP"
  HAD_OLD_VENV=1
fi
VENV_SWAPPED=1
mv "$STAGED_VENV" "$VENV_DIR"
ok "Staged runtime activated with $($VENV_DIR/bin/python -V)"

REPO_UNIT="$REPO_DIR_REAL/wb-irrigation.service"
if [[ ! -f "$REPO_UNIT" ]]; then
  err "Repository systemd unit is missing: $REPO_UNIT"
  exit 1
fi
if ! cmp -s "$REPO_UNIT" "$SERVICE_FILE" 2>/dev/null; then
  UNIT_CHANGED=1
  install -m 0644 "$REPO_UNIT" "$SERVICE_FILE"
  systemctl daemon-reload
fi

LOGROTATE_SOURCE="$REPO_DIR_REAL/configs/logrotate.d/wb-irrigation"
if [[ ! -f "$LOGROTATE_SOURCE" ]]; then
  err "Repository logrotate config is missing: $LOGROTATE_SOURCE"
  exit 1
fi
if ! cmp -s "$LOGROTATE_SOURCE" "$LOGROTATE_TARGET" 2>/dev/null; then
  LOGROTATE_CHANGED=1
  install -m 0644 "$LOGROTATE_SOURCE" "$LOGROTATE_TARGET"
fi
ensure_logrotate_ready

CURRENT_COMMIT=$(git rev-parse HEAD)
ENV_CHANGED=1
upsert_env_value GIT_COMMIT "$CURRENT_COMMIT" "$ENV_FILE"
if [[ ${PORT+x} ]]; then
  VALIDATED_PORT=$(configured_port "$ENV_FILE" "$REPO_DIR_REAL/.env")
  upsert_env_value PORT "$VALIDATED_PORT" "$ENV_FILE"
fi
info "GIT_COMMIT updated in $ENV_FILE -> ${CURRENT_COMMIT:0:12}"

if ! systemctl is-enabled "$SERVICE" >/dev/null 2>&1; then
  err "Service is not enabled: $SERVICE"
  exit 1
fi
systemctl restart "$SERVICE"
if ! systemctl is-active "$SERVICE" >/dev/null 2>&1; then
  err "Service failed to start. Check: journalctl -u $SERVICE -n 200"
  exit 1
fi

APP_PORT=$(configured_port "$ENV_FILE" "$REPO_DIR_REAL/.env")
info "Waiting up to 30s for /readyz on 127.0.0.1:${APP_PORT}"
READYZ_BODY=""
for attempt in $(seq 1 15); do
  if READYZ_BODY=$(curl -fsS --max-time 3 "http://127.0.0.1:${APP_PORT}/readyz" 2>/dev/null); then
    SCHED_STATUS=$(
      printf '%s' "$READYZ_BODY" | "$VENV_DIR/bin/python" -c \
        "import json,sys; print(json.load(sys.stdin).get('checks', {}).get('scheduler', {}).get('status', 'missing'))"
    )
    if [[ "$SCHED_STATUS" == "ok" ]]; then
      ok "/readyz scheduler check: ok (attempt $attempt)"
      break
    fi
  fi
  if [[ "$attempt" -eq 15 ]]; then
    err "/readyz failed on configured PORT=$APP_PORT"
    err "Body: ${READYZ_BODY:-<no response>}"
    exit 1
  fi
  sleep 2
done

prune_old_backups
UPDATE_SUCCEEDED=1
VENV_SWAPPED=0
if [[ "$HAD_OLD_VENV" == "1" ]] && ! rm -rf -- "$OLD_VENV_BACKUP"; then
  warn "Update succeeded, but old venv cleanup failed: $OLD_VENV_BACKUP"
fi
ok "Update completed successfully. Backup stored at: $BACKUP_DIR"
