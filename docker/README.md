# docker/ — локальная dev-сборка

Эта папка нужна для запуска WB-Irrigation **локально на desktop** (Linux/macOS) без Wirenboard — для разработки и проверки изменений.

**Это НЕ путь деплоя на прод.** На Wirenboard приложение работает нативно через systemd, без Docker (см. раздел "Wirenboard" в корневом `README.md`, скрипт `install_wb.sh`). Docker на WB не используется и не планируется.

## Запуск

Из корня репо:

```bash
docker compose -f docker/docker-compose.yml up --build
```

Откроется:
- App: http://localhost:8080
- MQTT broker: tcp://localhost:1884 (на хосте 1884, внутри контейнера 1883)

Детали (volumes, env, healthcheck) — в `DEPLOY-DOCKER.md` в этой же папке.
