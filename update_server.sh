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

# Archive working tree excluding heavy/volatile dirs
ARCHIVE="$BACKUP_DIR/repo_snapshot.tar.gz"
tar --exclude="./venv" --exclude="./.git" --exclude="./__pycache__" \
    --exclude="./backups" --exclude="./*.pid" -czf "$ARCHIVE" .
ok "Backup created: $ARCHIVE"

# 2) Update code
info "Fetching latest code..."
git fetch --all -q
info "Resetting to origin/$BRANCH"
git reset --hard "origin/$BRANCH" -q
ok "Code updated to $(git rev-parse --short HEAD)"

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
fi

ok "Update completed successfully. Backup stored at: $BACKUP_DIR"


