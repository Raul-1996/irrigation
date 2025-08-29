#!/bin/sh
set -eu

# Simple idempotent installer for Wirenboard without Docker.
# Installs into /opt/wb-irrigation/irrigation and creates a systemd service.

REPO_URL="https://github.com/Raul-1996/irrigation.git"
BASE_DIR="/opt/wb-irrigation"
APP_DIR="${BASE_DIR}/irrigation"
SERVICE_NAME="wb-irrigation"
SERVICE_FILE="/etc/systemd/system/${SERVICE_NAME}.service"

require_root() {
  if [ "$(id -u)" -ne 0 ]; then
    echo "Please run as root (sudo -i)" >&2
    exit 1
  fi
}

install_packages() {
  apt-get update
  DEBIAN_FRONTEND=noninteractive apt-get install -y \
    python3-venv python3-pip git sqlite3 build-essential \
    libjpeg-dev zlib1g-dev libpng-dev curl
}

sync_repo() {
  mkdir -p "${BASE_DIR}"
  if [ -d "${APP_DIR}/.git" ]; then
    echo "Repository exists in ${APP_DIR}, pulling updates..."
    git -C "${APP_DIR}" pull --rebase || true
  else
    echo "Cloning repository to ${APP_DIR}..."
    git clone "${REPO_URL}" "${APP_DIR}"
  fi
}

setup_venv() {
  cd "${APP_DIR}"
  if [ ! -d venv ]; then
    python3 -m venv venv
  fi
  . venv/bin/activate
  pip install -U pip wheel
  pip install -r requirements.txt
}

prepare_data_dirs() {
  cd "${APP_DIR}"
  mkdir -p static/media backups
  [ -f irrigation.db ] || touch irrigation.db
}

seed_mqtt_row() {
  cd "${APP_DIR}"
  # Create table if app hasn’t bootstrapped DB yet (idempotent)
  sqlite3 irrigation.db "CREATE TABLE IF NOT EXISTS mqtt_servers (id INTEGER PRIMARY KEY, name TEXT NOT NULL, host TEXT NOT NULL, port INTEGER DEFAULT 1883, username TEXT, password TEXT, client_id TEXT, enabled INTEGER DEFAULT 1, created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP, updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP);"
  # TLS columns (ignore if already exist)
  sqlite3 irrigation.db "ALTER TABLE mqtt_servers ADD COLUMN tls_enabled INTEGER DEFAULT 0;" 2>/dev/null || true
  sqlite3 irrigation.db "ALTER TABLE mqtt_servers ADD COLUMN tls_ca_path TEXT;" 2>/dev/null || true
  sqlite3 irrigation.db "ALTER TABLE mqtt_servers ADD COLUMN tls_cert_path TEXT;" 2>/dev/null || true
  sqlite3 irrigation.db "ALTER TABLE mqtt_servers ADD COLUMN tls_key_path TEXT;" 2>/dev/null || true
  sqlite3 irrigation.db "ALTER TABLE mqtt_servers ADD COLUMN tls_insecure INTEGER DEFAULT 0;" 2>/dev/null || true
  sqlite3 irrigation.db "ALTER TABLE mqtt_servers ADD COLUMN tls_version TEXT;" 2>/dev/null || true
  # Upsert local broker row
  sqlite3 irrigation.db "INSERT OR REPLACE INTO mqtt_servers (id,name,host,port,enabled,tls_enabled) VALUES (1,'wirenboard','127.0.0.1',1883,1,0);"
}

write_service() {
  cat >"${SERVICE_FILE}" <<EOF
[Unit]
Description=WB-Irrigation Flask app
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
WorkingDirectory=${APP_DIR}
Environment=TESTING=0
Environment=UI_THEME=auto
ExecStart=${APP_DIR}/venv/bin/python run.py
Restart=on-failure
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF
}

enable_service() {
  systemctl daemon-reload
  systemctl enable --now "${SERVICE_NAME}"
}

health_check() {
  sleep 3 || true
  if command -v curl >/dev/null 2>&1; then
    set +e
    curl -I http://127.0.0.1:8080/ | head -n 1 || true
    set -e
  fi
  systemctl status "${SERVICE_NAME}" --no-pager | sed -n '1,40p' || true
}

require_root
install_packages
sync_repo
setup_venv
prepare_data_dirs
seed_mqtt_row
write_service
enable_service
health_check

echo "Installation finished. Open http://<wirenboard-ip>:8080"


