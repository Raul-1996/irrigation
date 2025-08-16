#!/bin/zsh
set -euo pipefail

# Kill any process listening on 1883 and 5055 (MQTT + emulator HTTP)
if lsof -i :1883 >/dev/null 2>&1; then
  echo "Port 1883 is busy, killing listeners..."
  lsof -ti :1883 | xargs -r kill -9 || true
fi
if lsof -i :5055 >/dev/null 2>&1; then
  echo "Port 5055 is busy, killing listeners..."
  lsof -ti :5055 | xargs -r kill -9 || true
fi

# Ensure docker mosquitto is running with our config (if Docker daemon is available)
if command -v docker >/dev/null 2>&1; then
  if docker info >/dev/null 2>&1; then
    if ! docker ps --format '{{.Names}}' | grep -q '^test-mosquitto$'; then
      docker rm -f test-mosquitto >/dev/null 2>&1 || true
      docker run -d --name test-mosquitto -p 1883:1883 \
        -v "$(pwd)/mosquitto.conf:/mosquitto/config/mosquitto.conf:ro" \
        eclipse-mosquitto:2 >/dev/null
      echo "Started docker mosquitto on 1883"
    fi
  else
    echo "Docker daemon is not running. Skipping broker startup via Docker."
  fi
else
  echo "Docker is not installed. Skipping broker startup via Docker."
fi

# Activate venv and install deps if needed
if [ ! -x "venv/bin/python" ]; then
  python3 -m venv venv
fi
source venv/bin/activate
pip -q install -r requirements.txt

# Export defaults if not provided
export TEST_MQTT_HOST=${TEST_MQTT_HOST:-127.0.0.1}
export TEST_MQTT_PORT=${TEST_MQTT_PORT:-1883}
export EMULATOR_HTTP_PORT=${EMULATOR_HTTP_PORT:-5055}

# Start emulator
nohup python mqtt_relay_emulator.py > emulator.out 2>&1 &
EMUPID=$!

echo "Emulator started: PID=$EMUPID, UI: http://localhost:${EMULATOR_HTTP_PORT}"
