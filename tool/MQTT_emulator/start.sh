#!/bin/zsh
set -euo pipefail

cd "$(dirname "$0")"

# Try start mosquitto via Docker if available and daemon is running
if command -v docker >/dev/null 2>&1 && docker info >/dev/null 2>&1; then
  if ! docker ps --format '{{.Names}}' | grep -q '^test-mosquitto$'; then
    docker rm -f test-mosquitto >/dev/null 2>&1 || true
    docker run -d --name test-mosquitto -p 1883:1883 \
      -v "$(pwd)/mosquitto.conf:/mosquitto/config/mosquitto.conf:ro" \
      eclipse-mosquitto:2 >/dev/null
    echo "Started docker mosquitto on 1883"
  fi
else
  echo "Docker unavailable or not running. Skipping docker broker startup."
fi

# If still no broker on 1883, try local mosquitto if present (no install)
if ! lsof -i :1883 >/dev/null 2>&1; then
  if command -v mosquitto >/dev/null 2>&1; then
    echo "Starting local mosquitto broker on 1883..."
    nohup mosquitto -c "$(pwd)/mosquitto.conf" > mosquitto.out 2>&1 &
    sleep 0.8
  else
    echo "No mosquitto binary found. You can install it or point to external broker via TEST_MQTT_HOST/PORT."
  fi
fi

# Activate venv one level up if exists, else use system python
if [ -x "../../venv/bin/python" ]; then
  PY="../../venv/bin/python"
else
  PY="python3"
fi

# Default envs
export TEST_MQTT_HOST=${TEST_MQTT_HOST:-127.0.0.1}
export TEST_MQTT_PORT=${TEST_MQTT_PORT:-1883}
export EMULATOR_HTTP_PORT=${EMULATOR_HTTP_PORT:-5055}

nohup "$PY" mqtt_relay_emulator.py > emulator.out 2>&1 &
EPID=$!

echo "Emulator started (tool): PID=$EPID, UI: http://localhost:${EMULATOR_HTTP_PORT}"
