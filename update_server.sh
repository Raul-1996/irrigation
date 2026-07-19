#!/usr/bin/env bash

# Supported native updater for Wirenboard.
# The application service is quiesced while a verified SQLite snapshot and
# restorable code/secret archive are created. Docker is intentionally not part
# of the production update path.

set -euo pipefail
umask 077

REPO_DIR=${REPO_DIR:-/opt/wb-irrigation/irrigation}
BRANCH=${BRANCH:-main}
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
Env vars: REPO_DIR, BRANCH, SERVICE, BACKUP_BASE, BACKUP_KEEP, PY_VERSION, UV_BIN, ENV_FILE, NONINTERACTIVE=1
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

confirm() {
  if [[ "$NONINTERACTIVE" == "1" ]]; then
    return 0
  fi
  read -r -p "$1 [y/N]: " ans || true
  [[ "${ans,,}" == "y" || "${ans,,}" == "yes" ]]
}

configured_port() {
  local env_file=$1
  local port=${PORT:-}

  if [[ -z "$port" && -f "$env_file" ]]; then
    port=$(sed -n 's/^[[:space:]]*PORT[[:space:]]*=[[:space:]]*//p' "$env_file" | tail -n 1)
    port=$(printf '%s' "$port" | tr -d "'\"[:space:]")
  fi
  port=${port:-8080}
  if [[ ! "$port" =~ ^[0-9]+$ ]] || ((port < 1 || port > 65535)); then
    err "Invalid PORT value: $port"
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

  # Validate the complete include tree, not only our file: duplicate targets
  # across package-owned configs otherwise fail later from cron/timer.
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
case "$BACKUP_BASE" in
  / | /opt | /mnt | /mnt/data | "$REPO_DIR_REAL")
    err "Unsafe backup directory: $BACKUP_BASE"
    exit 1
    ;;
esac

cd "$REPO_DIR_REAL"
info "Target repo: $REPO_DIR_REAL (branch: $BRANCH)"

# If anything fails after stopping a previously active service, make a best
# effort to return it to service. Preserve the original non-zero exit status.
SERVICE_WAS_ACTIVE=0
SERVICE_RESTORED=0
restore_service_on_failure() {
  local status=$?
  trap - EXIT
  if [[ "$status" -ne 0 && "$SERVICE_WAS_ACTIVE" == "1" && "$SERVICE_RESTORED" == "0" ]]; then
    warn "Update failed; restoring previously active service $SERVICE"
    if ! systemctl start "$SERVICE"; then
      err "Failed to restore $SERVICE; inspect journalctl -u $SERVICE"
    fi
  fi
  exit "$status"
}
trap restore_service_on_failure EXIT

if systemctl is-active "$SERVICE" >/dev/null 2>&1; then
  SERVICE_WAS_ACTIVE=1
  info "Stopping $SERVICE for a consistent backup"
  systemctl stop "$SERVICE"
  if systemctl is-active "$SERVICE" >/dev/null 2>&1; then
    err "Service did not stop: $SERVICE"
    exit 1
  fi
fi

# Create a private, self-contained backup on the same data partition as the
# physical repository. SQLite's backup API consolidates WAL state atomically.
mkdir -p -m 700 "$BACKUP_BASE"
chmod 700 "$BACKUP_BASE"
STAMP=$(date +"%Y%m%d_%H%M%S")
BACKUP_DIR="$BACKUP_BASE/$STAMP"
mkdir -m 700 "$BACKUP_DIR"
info "Creating verified backup at $BACKUP_DIR"

DB_PATH="$REPO_DIR_REAL/irrigation.db"
DB_BACKUP="$BACKUP_DIR/irrigation.db"
if [[ -f "$DB_PATH" ]]; then
  sqlite3 "$DB_PATH" ".backup '$DB_BACKUP'"
  DB_CHECK=$(sqlite3 "$DB_BACKUP" "PRAGMA quick_check;")
  if [[ "$DB_CHECK" != "ok" ]]; then
    err "SQLite backup verification failed: $DB_CHECK"
    exit 1
  fi
  chmod 600 "$DB_BACKUP"
else
  warn "Database does not exist yet; code and secrets will still be backed up"
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
  -czf "$ARCHIVE" .
chmod 600 "$ARCHIVE"

# The EnvironmentFile is outside the repository. Preserve it explicitly;
# repository-local .secret_key/.irrig_secret_key remain in the private archive.
if [[ -f "$ENV_FILE" ]]; then
  chmod 600 "$ENV_FILE"
  install -m 600 "$ENV_FILE" "$BACKUP_DIR/environment.env"
fi
ok "Verified backup created: $BACKUP_DIR"

info "Fetching latest code"
git fetch --all -q
info "Resetting to origin/$BRANCH"
git reset --hard "origin/$BRANCH" -q
ok "Code updated to $(git rev-parse --short HEAD)"

info "Purging stale __pycache__"
find . -path ./venv -prune -o -path ./.git -prune -o -type d -name __pycache__ -print -exec rm -rf -- {} +

# The WB system Python is 3.9. Always provision and validate the application
# runtime through uv. A broken or wrong-version venv is rebuilt deterministically.
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
VENV_VERSION=""
if [[ -x "$VENV_DIR/bin/python" ]]; then
  VENV_VERSION=$(
    "$VENV_DIR/bin/python" -c 'import sys; print(".".join(map(str, sys.version_info[:2])))' 2>/dev/null || true
  )
fi
if [[ "$VENV_VERSION" != "$PY_VERSION" ]]; then
  warn "Rebuilding venv (found ${VENV_VERSION:-broken}, need $PY_VERSION)"
  rm -rf -- "$VENV_DIR"
  uv venv --python "$PY_VERSION" "$VENV_DIR"
fi
if [[ $("$VENV_DIR/bin/python" -c 'import sys; print(".".join(map(str, sys.version_info[:2])))') != "$PY_VERSION" ]]; then
  err "venv does not use Python $PY_VERSION"
  exit 1
fi
uv pip install --python "$VENV_DIR/bin/python" -r requirements.txt
ok "Dependencies installed with $($VENV_DIR/bin/python -V)"

SERVICE_FILE="/etc/systemd/system/${SERVICE}.service"
REPO_UNIT="$REPO_DIR_REAL/wb-irrigation.service"
if [[ ! -f "$REPO_UNIT" ]]; then
  err "Repository systemd unit is missing: $REPO_UNIT"
  exit 1
fi
if ! cmp -s "$REPO_UNIT" "$SERVICE_FILE" 2>/dev/null; then
  info "Updating systemd unit"
  install -m 0644 "$REPO_UNIT" "$SERVICE_FILE"
  systemctl daemon-reload
fi

LOGROTATE_SOURCE="$REPO_DIR_REAL/configs/logrotate.d/wb-irrigation"
LOGROTATE_TARGET="/etc/logrotate.d/wb-irrigation"
if [[ ! -f "$LOGROTATE_SOURCE" ]]; then
  err "Repository logrotate config is missing: $LOGROTATE_SOURCE"
  exit 1
fi
install -m 0644 "$LOGROTATE_SOURCE" "$LOGROTATE_TARGET"
ensure_logrotate_ready

CURRENT_COMMIT=$(git rev-parse HEAD)
upsert_env_value GIT_COMMIT "$CURRENT_COMMIT" "$ENV_FILE"
if [[ -n "${PORT:-}" ]]; then
  VALIDATED_PORT=$(configured_port "$ENV_FILE")
  upsert_env_value PORT "$VALIDATED_PORT" "$ENV_FILE"
fi
info "GIT_COMMIT updated in $ENV_FILE -> ${CURRENT_COMMIT:0:12}"

if ! systemctl is-enabled "$SERVICE" >/dev/null 2>&1; then
  err "Service is not enabled: $SERVICE"
  exit 1
fi
info "Restarting service: $SERVICE"
systemctl restart "$SERVICE"
if ! systemctl is-active "$SERVICE" >/dev/null 2>&1; then
  err "Service failed to start. Check: journalctl -u $SERVICE -n 200"
  exit 1
fi
SERVICE_RESTORED=1

APP_PORT=$(configured_port "$ENV_FILE")
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
ok "Update completed successfully. Backup stored at: $BACKUP_DIR"
