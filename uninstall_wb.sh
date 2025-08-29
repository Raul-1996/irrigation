#!/bin/bash
set -euo pipefail

# Uninstaller for Wirenboard deployment created by install_wb.sh

SERVICE_NAME="wb-irrigation"
SERVICE_FILE="/etc/systemd/system/${SERVICE_NAME}.service"
APP_ROOT="/opt/wb-irrigation"

require_root() {
  if [ "${EUID}" -ne 0 ]; then
    echo "Please run as root (sudo -i)" >&2
    exit 1
  fi
}

stop_service() {
  systemctl stop "${SERVICE_NAME}" 2>/dev/null || true
  systemctl disable "${SERVICE_NAME}" 2>/dev/null || true
  rm -f "${SERVICE_FILE}" || true
  systemctl daemon-reload || true
}

remove_files() {
  rm -rf "${APP_ROOT}" || true
}

require_root
stop_service
remove_files

echo "WB-Irrigation removed. No leftovers should remain."


