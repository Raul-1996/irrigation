#!/usr/bin/env bash
set -euo pipefail

echo "[+] Creating DB backup..."
ts=$(date +%Y%m%d_%H%M%S)
mkdir -p backups
if [ -f irrigation.db ]; then
  cp irrigation.db backups/irrigation_backup_${ts}.db
fi

echo "[+] Pulling new images..."
docker compose pull || true

echo "[+] Rebuilding app image..."
docker compose build app

echo "[+] Restarting services..."
docker compose up -d

echo "[+] Update complete. Health check:"
sleep 2
if command -v curl >/dev/null 2>&1; then
  curl -fsS http://localhost:8080/health || true
fi


