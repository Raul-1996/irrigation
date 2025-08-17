#!/bin/zsh
set -euo pipefail

# Kill processes occupying emulator HTTP port (default 5055 or EMULATOR_HTTP_PORT)
PORT_TO_KILL=${EMULATOR_HTTP_PORT:-5055}
if lsof -i :${PORT_TO_KILL} >/dev/null 2>&1; then
  echo "Port ${PORT_TO_KILL} is busy, killing listeners..."
  for pid in $(lsof -ti :${PORT_TO_KILL}); do
    kill -9 "$pid" || true
  done
fi

# Ensure docker mosquitto is running with our config (if Docker daemon is available)
CFG_SRC="$(pwd)/tools/MQTT_emulator/mosquitto.conf"
if [ -f "$CFG_SRC" ]; then
  if command -v docker >/dev/null 2>&1; then
    if docker info >/dev/null 2>&1; then
      if ! docker ps --format '{{.Names}}' | grep -q '^test-mosquitto$'; then
        docker rm -f test-mosquitto >/dev/null 2>&1 || true
        docker run -d --name test-mosquitto -p 1883:1883 \
          -v "$CFG_SRC:/mosquitto/config/mosquitto.conf:ro" \
          eclipse-mosquitto:2 >/dev/null
        echo "Started docker mosquitto on 1883"
      fi
    else
      echo "Docker daemon is not running. Skipping broker startup via Docker."
    fi
  else
    echo "Docker is not installed. Skipping broker startup via Docker."
  fi
else
  echo "Mosquitto config not found at $CFG_SRC. Skipping docker broker startup."
fi

# If port 1883 is still free, try to start a local mosquitto binary
if ! lsof -i :1883 >/dev/null 2>&1; then
  MOSQ_BIN=""
  # Try PATH first
  if command -v mosquitto >/dev/null 2>&1; then
    MOSQ_BIN="$(command -v mosquitto)"
  fi
  # Try common Homebrew locations
  [ -z "$MOSQ_BIN" ] && [ -x "/opt/homebrew/sbin/mosquitto" ] && MOSQ_BIN="/opt/homebrew/sbin/mosquitto"
  [ -z "$MOSQ_BIN" ] && [ -x "/usr/local/sbin/mosquitto" ] && MOSQ_BIN="/usr/local/sbin/mosquitto"

  # Try to install via Homebrew if not found
  if [ -z "$MOSQ_BIN" ]; then
    BREW_BIN=""
    if command -v brew >/dev/null 2>&1; then
      BREW_BIN="$(command -v brew)"
    elif [ -x "/opt/homebrew/bin/brew" ]; then
      BREW_BIN="/opt/homebrew/bin/brew"
    elif [ -x "/usr/local/bin/brew" ]; then
      BREW_BIN="/usr/local/bin/brew"
    fi
    if [ -n "$BREW_BIN" ]; then
      echo "Installing mosquitto via Homebrew..."
      "$BREW_BIN" list mosquitto >/dev/null 2>&1 || "$BREW_BIN" install -q mosquitto
      # After install, try to locate mosquitto again
      [ -x "/opt/homebrew/sbin/mosquitto" ] && MOSQ_BIN="/opt/homebrew/sbin/mosquitto"
      [ -z "$MOSQ_BIN" ] && [ -x "/usr/local/sbin/mosquitto" ] && MOSQ_BIN="/usr/local/sbin/mosquitto"
      [ -z "$MOSQ_BIN" ] && MOSQ_BIN="$(command -v mosquitto 2>/dev/null || true)"
    fi
  fi

  if [ -n "$MOSQ_BIN" ]; then
    echo "Starting local mosquitto broker on 1883 using $MOSQ_BIN..."
    nohup "$MOSQ_BIN" -c "$CFG_SRC" > mosquitto.out 2>&1 &
    sleep 0.8
  else
    echo "No broker found and Docker unavailable. Please install Mosquitto or start any MQTT broker on port 1883."
  fi
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

# Start emulator from tools folder if present, else from root
if [ -f "tools/MQTT_emulator/mqtt_relay_emulator.py" ]; then
  (cd tools/MQTT_emulator && nohup ${PYTHON:-python3} mqtt_relay_emulator.py > ../../emulator.out 2>&1 & echo $! > ../../emulator.pid)
else
  nohup python mqtt_relay_emulator.py > emulator.out 2>&1 & echo $! > emulator.pid
fi
EMUPID=$(cat emulator.pid 2>/dev/null || true)
echo "Emulator started: PID=${EMUPID:-unknown}, UI: http://localhost:${EMULATOR_HTTP_PORT}"
