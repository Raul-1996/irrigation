#!/bin/bash

# WB-Irrigation updater for Wirenboard
# - Creates dated backup (code + DB)
# - Updates repo to latest commit on branch
# - Ensures venv and installs requirements
# - Restarts systemd service

set -euo pipefail

# Defaults (override via env or flags)
REPO_DIR=${REPO_DIR:-/opt/wb-irrigation/irrigation}
BRANCH=${BRANCH:-main}
SERVICE=${SERVICE:-wb-irrigation}
BACKUP_BASE=${BACKUP_BASE:-/opt/wb-irrigation/backups}
PY_BIN=${PY_BIN:-python3}
NONINTERACTIVE=${NONINTERACTIVE:-0}

usage(){
  cat <<EOF
Usage: $(basename "$0") [--repo-dir DIR] [--branch BRANCH] [--service NAME] [--backup-dir DIR] [--yes]
Env vars: REPO_DIR, BRANCH, SERVICE, BACKUP_BASE, PY_BIN, NONINTERACTIVE=1
Examples:
  sudo bash $0 --yes
  BRANCH=main REPO_DIR=/opt/wb-irrigation/irrigation bash $0 -y
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    -h|--help) usage; exit 0;;
    --repo-dir) REPO_DIR="$2"; shift 2;;
    --branch) BRANCH="$2"; shift 2;;
    --service) SERVICE="$2"; shift 2;;
    --backup-dir) BACKUP_BASE="$2"; shift 2;;
    -y|--yes) NONINTERACTIVE=1; shift;;
    *) echo "Unknown arg: $1"; usage; exit 1;;
  esac
done

ts(){ date +"%Y-%m-%d %H:%M:%S"; }
info(){ echo -e "\033[1;34m[$(ts)]\033[0m $*"; }
ok(){ echo -e "\033[1;32m[$(ts)] OK\033[0m $*"; }
warn(){ echo -e "\033[1;33m[$(ts)] WARN\033[0m $*"; }
err(){ echo -e "\033[1;31m[$(ts)] ERROR\033[0m $*"; }

confirm(){
  if [[ "$NONINTERACTIVE" == "1" ]]; then return 0; fi
  read -r -p "$1 [y/N]: " ans || true
  [[ "${ans,,}" == "y" || "${ans,,}" == "yes" ]]
}

if [[ ! -d "$REPO_DIR" ]]; then
  err "Repo dir not found: $REPO_DIR"
  exit 1
fi

cd "$REPO_DIR"

info "Target repo: $REPO_DIR (branch: $BRANCH)"

# 1) Backup
mkdir -p "$BACKUP_BASE"
STAMP=$(date +"%Y%m%d_%H%M%S")
BACKUP_DIR="$BACKUP_BASE/$STAMP"
mkdir -p "$BACKUP_DIR"

info "Creating backup at $BACKUP_DIR"

# Copy DB files (if exist)
shopt -s nullglob
DB_FILES=(irrigation.db irrigation.db-wal irrigation.db-shm)
for f in "${DB_FILES[@]}"; do
  if [[ -f "$f" ]]; then
    cp -v "$f" "$BACKUP_DIR/" || true
  fi
done

# Archive working tree excluding heavy/volatile dirs. The live SQLite DB
# (irrigation.db + WAL/SHM) is excluded: it is backed up separately by the cp
# loop above, and tarring it while the service writes the WAL makes tar exit 1
# ("file changed as we read it"), which under `set -e` aborts the whole deploy
# before git reset. The code snapshot is best-effort (`|| true`) — code is
# always recoverable from git; the authoritative DB backup is the cp above.
ARCHIVE="$BACKUP_DIR/repo_snapshot.tar.gz"
tar --exclude="./venv" --exclude="./.git" --exclude="./__pycache__" \
    --exclude="./backups" --exclude="./*.pid" \
    --exclude="./irrigation.db" --exclude="./irrigation.db-wal" --exclude="./irrigation.db-shm" \
    -czf "$ARCHIVE" . || true
ok "Backup created: $ARCHIVE"

# 2) Update code
info "Fetching latest code..."
git fetch --all -q
info "Resetting to origin/$BRANCH"
git reset --hard "origin/$BRANCH" -q
ok "Code updated to $(git rev-parse --short HEAD)"

# 2.5) Purge stale __pycache__ (not removed by git reset; can shadow fresh .py)
info "Purging stale __pycache__..."
find . -path ./venv -prune -o -path ./.git -prune -o -type d -name __pycache__ -print -exec rm -rf {} + 2>/dev/null || true
ok "__pycache__ purged"

# 3) Ensure venv and requirements
if [[ ! -d "venv" ]]; then
  info "Creating virtualenv..."
  $PY_BIN -m venv venv
fi
source venv/bin/activate
info "Upgrading pip/wheel..."
pip -q install -U pip wheel
if [[ -f requirements.txt ]]; then
  info "Installing requirements.txt..."
  pip -q install -r requirements.txt
else
  warn "requirements.txt not found, skipping"
fi
if [[ -f requirements-dev.txt ]]; then
  warn "Installing requirements-dev.txt (optional)"
  pip -q install -r requirements-dev.txt || true
fi
ok "Dependencies installed"

# 3.5) Update systemd unit if changed
SERVICE_FILE="/etc/systemd/system/${SERVICE}.service"
REPO_UNIT="${REPO_DIR}/wb-irrigation.service"
if [[ -f "$REPO_UNIT" ]]; then
  if ! cmp -s "$REPO_UNIT" "$SERVICE_FILE" 2>/dev/null; then
    info "Updating systemd unit file..."
    cp "$REPO_UNIT" "$SERVICE_FILE"
    systemctl daemon-reload
    ok "Systemd unit updated and daemon-reloaded"
  else
    info "Systemd unit file unchanged, skipping"
  fi
fi

# 3.6) Write current GIT_COMMIT into systemd EnvironmentFile (Wave 2 F2).
# The wb_build_info Prometheus metric reads os.environ['GIT_COMMIT']; this
# lets the deployed process advertise the exact sha it is running.  File
# lives at /opt/wb-irrigation/.env (one level above $REPO_DIR).
ENV_FILE=${ENV_FILE:-/opt/wb-irrigation/.env}
CURRENT_COMMIT=$(git rev-parse HEAD)
if [[ -d "$(dirname "$ENV_FILE")" ]]; then
  touch "$ENV_FILE"
  if grep -q '^GIT_COMMIT=' "$ENV_FILE" 2>/dev/null; then
    # Update existing line in-place (portable sed: backup suffix then remove).
    sed -i.bak "s|^GIT_COMMIT=.*|GIT_COMMIT=${CURRENT_COMMIT}|" "$ENV_FILE"
    rm -f "${ENV_FILE}.bak"
    info "GIT_COMMIT updated in $ENV_FILE -> ${CURRENT_COMMIT:0:12}"
  else
    echo "GIT_COMMIT=${CURRENT_COMMIT}" >> "$ENV_FILE"
    info "GIT_COMMIT appended to $ENV_FILE -> ${CURRENT_COMMIT:0:12}"
  fi
  chmod 644 "$ENV_FILE" 2>/dev/null || true
else
  warn "GIT_COMMIT env not updated: $(dirname "$ENV_FILE") does not exist"
fi

# 4) Restart service
info "Restarting service: $SERVICE"
if systemctl is-enabled "$SERVICE" >/dev/null 2>&1; then
  systemctl restart "$SERVICE"
  sleep 1
  if systemctl is-active "$SERVICE" >/dev/null 2>&1; then
    ok "Service is active"
  else
    err "Service failed to start. Check: journalctl -u $SERVICE -n 200"
    exit 1
  fi
else
  warn "Service $SERVICE not enabled. Launching app manually is required."
  exit 0
fi

# 5) Smoke check — wait for sd_notify READY, then probe /readyz
SMOKE_PORT=${SMOKE_PORT:-8080}
info "Waiting up to 30s for service to settle..."
for i in $(seq 1 15); do
  sleep 2
  if journalctl -u "$SERVICE" --since "1 minute ago" --no-pager 2>/dev/null | grep -q "sd_notify READY=1 sent"; then
    ok "sd_notify READY=1 observed in journal"
    break
  fi
  if [[ $i -eq 15 ]]; then
    warn "sd_notify READY=1 not seen in journal within 30s (continuing)"
  fi
done

info "Probing /readyz on 127.0.0.1:${SMOKE_PORT}..."
READYZ_BODY=$(curl -fsS --max-time 5 "http://127.0.0.1:${SMOKE_PORT}/readyz" 2>/dev/null || true)
if [[ -z "$READYZ_BODY" ]]; then
  err "/readyz did not respond. Service: $(systemctl is-active "$SERVICE")"
  err "Last 30 log lines: journalctl -u $SERVICE -n 30"
  exit 1
fi

SCHED_STATUS=$(echo "$READYZ_BODY" | python3 -c "import sys,json
try:
    d = json.load(sys.stdin)
    print(d.get('checks', {}).get('scheduler', {}).get('status', 'missing'))
except Exception as e:
    print(f'parse_error:{e}')
" 2>/dev/null || echo "parse_failed")

if [[ "$SCHED_STATUS" == "ok" ]]; then
  ok "/readyz scheduler check: ok"
else
  err "/readyz scheduler check: $SCHED_STATUS"
  err "Body: $READYZ_BODY"
  exit 1
fi

ok "Update completed successfully. Backup stored at: $BACKUP_DIR"


