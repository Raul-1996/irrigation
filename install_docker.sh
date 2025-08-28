#!/usr/bin/env bash
set -euo pipefail

echo "[+] Installing Docker and Compose if missing..."
if ! command -v docker >/dev/null 2>&1; then
  curl -fsSL https://get.docker.com | sh
fi
if ! command -v docker compose >/dev/null 2>&1; then
  DOCKER_COMPOSE_BIN=$(command -v docker-compose || true)
  if [ -z "$DOCKER_COMPOSE_BIN" ]; then
    echo "Please install docker compose plugin or docker-compose manually." >&2
    exit 1
  fi
fi

echo "[+] Building images..."
docker compose build

echo "[+] Creating default SECRET_KEY if absent..."
export SECRET_KEY=${SECRET_KEY:-wb-irrigation-secret}

echo "[+] Starting services..."
docker compose up -d

echo "[+] Done. Open http://localhost:8080"


