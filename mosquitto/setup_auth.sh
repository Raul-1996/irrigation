#!/bin/bash
# Setup MQTT authentication for mosquitto
# Run this script once to generate the password file
#
# Usage:
#   ./mosquitto/setup_auth.sh [PASSWORD]
#
# If PASSWORD is not provided, a random one will be generated.

set -euo pipefail

PASSWD_FILE="$(dirname "$0")/passwd"
MQTT_USER="irrigation_app"

# Use provided password or generate a random one
if [ -n "${1:-}" ]; then
    MQTT_PASS="$1"
else
    MQTT_PASS=$(openssl rand -base64 24 | tr -d '/+=' | head -c 24)
    echo "Generated MQTT password: $MQTT_PASS"
    echo ""
    echo "Save this password! You will need it when configuring MQTT servers in the app."
fi

# Check if mosquitto_passwd is available
if command -v mosquitto_passwd &>/dev/null; then
    # Create password file using mosquitto_passwd
    mosquitto_passwd -c -b "$PASSWD_FILE" "$MQTT_USER" "$MQTT_PASS"
    echo "Password file created: $PASSWD_FILE"
elif command -v docker &>/dev/null; then
    # Use docker to run mosquitto_passwd
    docker run --rm -v "$(cd "$(dirname "$0")" && pwd):/mosquitto/config" \
        eclipse-mosquitto:2 \
        mosquitto_passwd -c -b /mosquitto/config/passwd "$MQTT_USER" "$MQTT_PASS"
    echo "Password file created via Docker: $PASSWD_FILE"
else
    echo "ERROR: Neither mosquitto_passwd nor docker found."
    echo "Install mosquitto-clients or Docker to generate the password file."
    exit 1
fi

chmod 600 "$PASSWD_FILE"
echo ""
echo "MQTT user:     $MQTT_USER"
echo "MQTT password: $MQTT_PASS"
echo ""
echo "Configure the MQTT server in the app with these credentials."
echo "Then restart the mosquitto container: docker compose restart mqtt"
