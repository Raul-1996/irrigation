#!/bin/sh
# Watchdog: restart wb-irrigation if health check fails 3 times in a row
# Run via cron every minute: * * * * * /opt/wb-irrigation/irrigation/scripts/watchdog.sh
FAIL_FILE="/tmp/wb-irrigation-fails"
MAX_FAILS=3

if /opt/wb-irrigation/irrigation/scripts/healthcheck.sh >/dev/null 2>&1; then
    # Healthy — reset counter
    rm -f "$FAIL_FILE"
    exit 0
fi

# Unhealthy — increment counter
FAILS=0
if [ -f "$FAIL_FILE" ]; then
    FAILS=$(cat "$FAIL_FILE" 2>/dev/null || echo 0)
fi
FAILS=$((FAILS + 1))
echo "$FAILS" > "$FAIL_FILE"

if [ "$FAILS" -ge "$MAX_FAILS" ]; then
    logger -t wb-irrigation-watchdog "Health check failed $FAILS times, restarting service"
    systemctl restart wb-irrigation
    rm -f "$FAIL_FILE"
fi
