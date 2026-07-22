#!/bin/sh
# Health check for wb-irrigation — used by systemd ExecStartPost watchdog cron
# Returns 0 if healthy, 1 if not
APP_DIR=${WB_IRRIGATION_APP_DIR:-/opt/wb-irrigation/irrigation}
STATE_ENV_FILE=${WB_IRRIGATION_STATE_ENV_FILE:-/mnt/data/wb-irrigation-state/.env}
ENV_FILE=${WB_IRRIGATION_ENV_FILE:-/opt/wb-irrigation/.env}
PYTHON_BIN=${WB_IRRIGATION_PYTHON:-"$APP_DIR/venv/bin/python"}

if ! PROBE_SETTINGS=$(
    cd "$APP_DIR" &&
        "$PYTHON_BIN" -c 'import os, sys; from dotenv import dotenv_values; from services.security import local_http_probe_url, resolve_http_probe_tls, resolve_http_transport; read=lambda path: {key: value for key, value in dotenv_values(path, interpolate=False).items() if value is not None}; env={**read(sys.argv[1]), **read(sys.argv[2]), **os.environ}; profile=resolve_http_transport(env); tls=resolve_http_probe_tls(profile, env); print(local_http_probe_url(profile, path="/readyz")); print(tls.ca_file or ""); print("1" if tls.insecure_tls else "0")' "$STATE_ENV_FILE" "$ENV_FILE"
); then
    echo "wb-irrigation unhealthy: invalid HTTP transport configuration"
    exit 1
fi

PROBE_URL=$(printf '%s\n' "$PROBE_SETTINGS" | sed -n '1p')
PROBE_CA_FILE=$(printf '%s\n' "$PROBE_SETTINGS" | sed -n '2p')
PROBE_INSECURE_TLS=$(printf '%s\n' "$PROBE_SETTINGS" | sed -n '3p')

if [ -n "$PROBE_CA_FILE" ]; then
    HTTP_CODE=$(curl --cacert "$PROBE_CA_FILE" -s -o /dev/null -w "%{http_code}" --max-time 5 "$PROBE_URL" 2>/dev/null)
elif [ "$PROBE_INSECURE_TLS" = "1" ]; then
    HTTP_CODE=$(curl --insecure -s -o /dev/null -w "%{http_code}" --max-time 5 "$PROBE_URL" 2>/dev/null)
else
    HTTP_CODE=$(curl -s -o /dev/null -w "%{http_code}" --max-time 5 "$PROBE_URL" 2>/dev/null)
fi
if [ "$HTTP_CODE" = "200" ]; then
    exit 0
else
    echo "wb-irrigation unhealthy: HTTP $HTTP_CODE"
    exit 1
fi
