# Docker deploy for WB-Irrigation

## One-time install

Run:

```
./install_docker.sh
```

It builds the app image and starts two services:
- app: Flask app on http://localhost:8080
- mqtt: Eclipse Mosquitto on tcp://localhost:1883

Persistent data:
- ./static/media  /app/static/media
- ./backups  /app/backups
- ./irrigation.db  /app/irrigation.db
- ./mosquitto_data, ./mosquitto_log

## Update

```
./update_docker.sh
```
- Creates DB backup into backups/
- Pulls/builds images
- Restarts services

## Environment

- SECRET_KEY (optional; default used if missing)
- UI_THEME (auto|light|dark)
- TESTING (0|1)

## Health

- Endpoint: /health (returns 200 OK JSON)

## Notes

- Ensure mosquitto.conf exists (repo includes a default). Adjust ports/paths if needed.
- For production behind a reverse proxy, bind 8080 to localhost and proxy via nginx or caddy.
