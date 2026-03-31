#!/bin/sh
# Health check for wb-irrigation — used by systemd ExecStartPost watchdog cron
# Returns 0 if healthy, 1 if not
HTTP_CODE=$(curl -s -o /dev/null -w "%{http_code}" --max-time 5 http://localhost:8080/api/status 2>/dev/null)
if [ "$HTTP_CODE" = "200" ]; then
    exit 0
else
    echo "wb-irrigation unhealthy: HTTP $HTTP_CODE"
    exit 1
fi
