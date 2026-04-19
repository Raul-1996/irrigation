# DEPLOY INSTRUCTIONS — POST-CONSOLIDATION (refactor/v2 -> main)

**Owner of action:** репозитория и прод-сервера 10.2.5.244 (WB-Techpom, Wirenboard controller)
**Author (prep):** DevOps Automator (Tony's team)
**Date drafted:** 2026-04-19
**Related:** консолидация истории `refactor/v2` в `main` через force-push (выполняет git-workflow-master).

Этот документ — runbook для владельца. Он описывает:
1. Что делать ПЕРЕД force-push (pre-flight на прод-сервере).
2. Как выкатить новый `main` на 10.2.5.244.
3. Smoke-тесты после деплоя.
4. Rollback приложения (если деплой сломал прод).
5. Rollback CI/конфигов (если force-push на main оказался ошибочным).

---

## 0. Контекст / Инвентаризация (TL;DR)

Хорошая новость: CI/CD в будущем `main` (т.е. в refactor/v2 сейчас) **уже** настроен корректно:

| Артефакт | Где | Состояние |
|---|---|---|
| `.github/workflows/ci.yml` | триггеры `push/pull_request` на `[main, master]` | OK |
| `.github/workflows/deploy.yml` | `workflow_dispatch` + `git pull origin main` | OK |
| `update_server.sh` | default `BRANCH=main` | OK |
| `install_wb.sh` | `git pull --rebase` (без явной ветки, follows HEAD) | OK |
| `scripts/watchdog.sh` | health + systemctl restart, **не делает git pull** | OK |
| cron (10.2.5.244) | `* * * * * scripts/watchdog.sh` + daily backup 03:00 | OK |

Других CI/CD мест, где была бы ветка `refactor/v2` в триггерах или deploy-командах — не найдено. В документации (`NEXT-AGENT-PROMPT.md`, `reports/stage-deploy-244.md`) упоминания есть, но они исторические и на работу pipeline не влияют.

Вывод: **патчить CI-триггеры не нужно**. После force-push `refactor/v2 -> main` pipeline сразу заработает на main.

Прод-сервер 10.2.5.244 (из devops graph):
- OS: Debian 11 (bullseye), kernel 6.8-wb153, aarch64
- App path: `/opt/wb-irrigation/irrigation`
- Service: `wb-irrigation.service` (systemd, Type=simple, venv/bin/python run.py)
- Port: 8080 (listen 0.0.0.0) — Flask
- DB: SQLite `irrigation.db` в корне репо
- Экспозиция наружу: Cloudflare Tunnel `cloudflared-poliv` (localhost:20241)
- MQTT: mosquitto на 1883 (plain) и 18883 (TLS)

---

## 1. PRE-FLIGHT (за 5-10 минут ДО force-push)

Выполнять владельцем на сервере 10.2.5.244 под root (SSH). Цель — точка отката.

```bash
# 1.1 Снимок текущего коммита в local репо и remote pointer (до force-push)
cd /opt/wb-irrigation/irrigation
git rev-parse HEAD | tee /root/pre-consolidation.HEAD.txt
git rev-parse --abbrev-ref HEAD | tee /root/pre-consolidation.BRANCH.txt
git remote -v | tee /root/pre-consolidation.REMOTE.txt

# 1.2 Версия приложения
cat VERSION 2>/dev/null || echo "no VERSION file" | tee /root/pre-consolidation.VERSION.txt

# 1.3 Остановить watchdog cron временно (чтобы не рестартовал сервис во время деплоя)
#     Способ 1: комментируем строку в crontab
crontab -l > /root/crontab.pre-consolidation.bak
crontab -l | sed 's|^\(\* \* \* \* \* /opt/wb-irrigation/irrigation/scripts/watchdog.sh\)|# \1|' | crontab -

# 1.4 Бэкап кода + БД
STAMP=$(date +%Y%m%d_%H%M%S)
BACKUP=/root/irrigation-backup-${STAMP}
mkdir -p "$BACKUP"
cp /opt/wb-irrigation/irrigation/irrigation.db "$BACKUP/" 2>/dev/null || true
cp /opt/wb-irrigation/irrigation/irrigation.db-wal "$BACKUP/" 2>/dev/null || true
cp /opt/wb-irrigation/irrigation/irrigation.db-shm "$BACKUP/" 2>/dev/null || true
tar --exclude=venv --exclude=.git --exclude=__pycache__ --exclude=backups \
    -czf "$BACKUP/repo_snapshot.tar.gz" -C /opt/wb-irrigation/irrigation .
echo "Backup ready: $BACKUP"
ls -la "$BACKUP"

# 1.5 Проверить, что сервис сейчас здоров (не деплоим на уже-сломанное)
systemctl is-active wb-irrigation
curl -sS -o /dev/null -w "HTTP %{http_code}\n" http://localhost:8080/api/status
# Ожидаем: active  и HTTP 200
```

**Stop-gate:** если 1.5 не даёт `active` + `HTTP 200` — **не продолжать**, разбираться сначала с текущим инцидентом.

---

## 2. DEPLOY на 10.2.5.244 (ПОСЛЕ force-push в origin/main)

Предварительное условие: git-workflow-master сделал force-push `refactor/v2` -> `origin/main` и создал тег `pre-consolidation-main-2026-04-19` на старом коммите main.

```bash
# 2.1 На сервере 10.2.5.244 (root)
cd /opt/wb-irrigation/irrigation

# 2.2 Остановить сервис корректно (graceful stop, TimeoutStopSec=20 в unit-файле)
systemctl stop wb-irrigation
sleep 3
systemctl is-active wb-irrigation || echo "stopped OK"
# Дополнительно убедиться, что процесса python run.py нет
pgrep -f "venv/bin/python run.py" && echo "WARN: still running" || echo "no process OK"

# 2.3 Запустить штатный updater (он сам сделает второй backup, fetch, reset --hard origin/main, venv, pip)
bash /opt/wb-irrigation/irrigation/update_server.sh --yes --branch main
# Скрипт:
#   - создаст свой backup в /opt/wb-irrigation/backups/<timestamp>/
#   - git fetch --all && git reset --hard origin/main
#   - venv + pip install -r requirements.txt
#   - при необходимости обновит /etc/systemd/system/wb-irrigation.service
#   - systemctl restart wb-irrigation

# 2.4 Если update_server.sh отработал — перейти к smoke (шаг 3).
#     Если упал — НЕ делать rollback сразу, прочитать вывод, возможно хватит починить deps и повторить шаг 2.3.
```

Альтернатива (ручной путь, если `update_server.sh` не устраивает):
```bash
cd /opt/wb-irrigation/irrigation
git fetch origin
git checkout main                 # локально перейти на main (если были на другой ветке)
git reset --hard origin/main
source venv/bin/activate
pip install -r requirements.txt
systemctl restart wb-irrigation
```

---

## 3. SMOKE TESTS (сразу после рестарта, до возврата watchdog cron)

```bash
# 3.1 Systemd: сервис активен и не падает в рестарт-луп
systemctl status wb-irrigation --no-pager | head -20
journalctl -u wb-irrigation -n 100 --no-pager | tail -50
# Не должно быть: ERROR/Traceback в последних 30 строках после "Started WB-Irrigation"

# 3.2 HTTP endpoints (local)
curl -sS -o /dev/null -w "status  %{http_code}\n" http://localhost:8080/api/status
curl -sS -o /dev/null -w "root    %{http_code}\n" http://localhost:8080/
curl -sS -o /dev/null -w "zones   %{http_code}\n" http://localhost:8080/api/zones
curl -sS -o /dev/null -w "groups  %{http_code}\n" http://localhost:8080/api/groups
# Ожидаем: status=200, root=200 (или 302 на /login — тоже OK), zones=200, groups=200

# 3.3 MQTT брокер жив
systemctl is-active mosquitto
# Ожидаем: active

# 3.4 Cloudflare tunnel (внешняя доступность)
systemctl is-active cloudflared-poliv
# Ожидаем: active. При желании: curl -sS -o /dev/null -w "%{http_code}\n" https://<cf-domain>/api/status

# 3.5 Версия приложения обновилась
cat /opt/wb-irrigation/irrigation/VERSION
git -C /opt/wb-irrigation/irrigation rev-parse --short HEAD
# Сверить с тем, что ожидается после force-push

# 3.6 БД не деградировала (количество зон не уменьшилось)
sqlite3 /opt/wb-irrigation/irrigation/irrigation.db "SELECT COUNT(*) FROM zones;"
sqlite3 /opt/wb-irrigation/irrigation/irrigation.db "SELECT COUNT(*) FROM groups;"
# Сверить с числами до деплоя (желательно знать их заранее)

# 3.7 Включить watchdog обратно
crontab /root/crontab.pre-consolidation.bak
crontab -l | grep watchdog
```

**Приёмка:** все 3.1-3.7 должны пройти зелёными. Особенно 3.2 (/api/status=200) — это то, за чем watchdog следит каждую минуту. Если 3.2 красный больше 3 минут — watchdog сам рестартанёт сервис (см. `scripts/watchdog.sh`). Отдельно проверить через 5 минут, что сервис не ушёл в restart-loop: `systemctl status wb-irrigation` → поле `Restart:` в `on-failure`, но счётчик рестартов в logs должен быть 0.

---

## 4. APPLICATION ROLLBACK (если деплой сломал прод)

Два пути — быстрый (git reset на предыдущий коммит) и надёжный (restore from backup).

### 4.1 Быстрый — git reset на прошлый HEAD
```bash
systemctl stop wb-irrigation
cd /opt/wb-irrigation/irrigation
PREV_HEAD=$(cat /root/pre-consolidation.HEAD.txt)
git fetch origin --tags
git reset --hard "$PREV_HEAD"      # или: git reset --hard pre-consolidation-main-2026-04-19
# Если venv-зависимости тоже откатить надо:
source venv/bin/activate
pip install -r requirements.txt
systemctl start wb-irrigation
sleep 3
curl -sS -o /dev/null -w "%{http_code}\n" http://localhost:8080/api/status
```

Замечание: если force-push уже удалил старый коммит из origin, `git fetch` его не достанет. Тогда — путь 4.2.

### 4.2 Надёжный — restore из pre-flight backup (раздел 1.4)
```bash
systemctl stop wb-irrigation
BACKUP=/root/irrigation-backup-<STAMP>   # подставить реальный
cd /opt/wb-irrigation
mv irrigation irrigation.broken.$(date +%s)
mkdir -p irrigation && cd irrigation
tar xzf "$BACKUP/repo_snapshot.tar.gz"
# Вернуть БД (если она была испорчена миграциями нового кода)
cp "$BACKUP/irrigation.db"    . 2>/dev/null || true
cp "$BACKUP/irrigation.db-wal" . 2>/dev/null || true
cp "$BACKUP/irrigation.db-shm" . 2>/dev/null || true
# Восстановить venv
if [ ! -d venv ]; then python3 -m venv venv; fi
source venv/bin/activate
pip install -r requirements.txt
# Вернуть systemd unit если файл менялся
if [ -f wb-irrigation.service ]; then
  cp wb-irrigation.service /etc/systemd/system/wb-irrigation.service
  systemctl daemon-reload
fi
systemctl start wb-irrigation
sleep 3
systemctl is-active wb-irrigation && curl -sS -w "%{http_code}\n" http://localhost:8080/api/status
```

Если путь 4.2 прошёл — дальше **не** возвращать `refactor/v2 -> main`, а сначала разобраться почему прод сломался, поправить код, и только потом повторять деплой.

---

## 5. CI ROLLBACK (если консолидация force-push оказалась ошибочной)

Этот блок — про git-историю в origin, а не про прод-сервер.

Пред-условие: git-workflow-master перед force-push создал тег на **прежнем** состоянии main:
```
pre-consolidation-main-2026-04-19  →  <старый HEAD origin/main>
```

### 5.1 Откат origin/main на предыдущее состояние
Выполняет владелец с правами push (локально, не из CI):
```bash
# В любой актуальной рабочей копии
git fetch origin --tags
git checkout -B main-rollback pre-consolidation-main-2026-04-19
# Проверить, что это тот самый коммит
git log -1 --oneline
# Опасная операция — force push обратно. Делать только если консолидация действительно провалилась.
git push origin main-rollback:main --force-with-lease
```

Флаг `--force-with-lease` безопаснее, чем `--force`: если с момента тега на origin/main кто-то успел пушнуть (маловероятно в окне консолидации) — команда остановится вместо перезаписи.

### 5.2 После CI-rollback — развернуть старый main на проде
Повторить раздел 2, но на восстановленном main. `update_server.sh --branch main --yes` сам сделает `git reset --hard origin/main`. Если БД к этому моменту мигрировалась к новой схеме — нужен **restore БД из pre-flight backup (раздел 1.4)**, потому что old-код не умеет читать new-схему.

### 5.3 После CI-rollback — восстановить ветку refactor/v2 (чтобы не потерять наработки)
Force-push ветки в main **не удаляет** саму ветку `refactor/v2` на origin (она остаётся отдельным ref). Достаточно проверить:
```bash
git fetch origin
git branch -r | grep refactor/v2
# Если ветки нет (кто-то удалил) — восстановить:
git push origin refactor/v2:refactor/v2
```

---

## 6. POST-DEPLOY (в течение 24 часов после успешного деплоя)

- Следить за `journalctl -u wb-irrigation --since "1 hour ago"` — ищем Traceback / repeated restarts.
- Следить за `/tmp/wb-irrigation-fails` (счётчик watchdog). Нормально — файла нет (значит healthcheck ОК).
- Проверить, что GitHub Actions CI на push в main отработал: `gh run list --branch main --limit 5` (локально).
- Через сутки убрать директории `irrigation.broken.*` и старые `/opt/wb-irrigation/backups/*` старше недели — освободить /opt (2.0G total, used 59% по инвентаризации).

---

## 7. Open questions / Assumptions

Эти вопросы я (DevOps Automator) не смог проверить без write-SSH и оставляю владельцу:

1. **Тег `pre-consolidation-main-2026-04-19`** — делает git-workflow-master. Проверить перед force-push, что он создан (`git ls-remote origin refs/tags/pre-consolidation-main-2026-04-19`).
2. **GitHub Actions secrets** для `deploy.yml` (`WB_HOST`, `WB_USER`, `WB_PASSWORD`, `JUMP_HOST`, `JUMP_USER`, `JUMP_SSH_KEY`) — проверить, что они настроены в repo settings. Сейчас repo `Raul-1996/irrigation` — **требует проверки владельцем**.
3. **Branch protection на main** — после консолидации стоит включить (require PR, require status checks = `lint`, `test`, `security` из ci.yml). Не делается этим runbook, но рекомендуется.
4. **`install_wb.sh`** делает `git pull --rebase` без имени ветки — он следует текущей локальной ветке. Если на устройстве раньше руками чекаутили `refactor/v2`, то после force-push `git pull --rebase` упадёт на расходящейся истории. Мягкое лекарство: в шаге 2.3 использовать `update_server.sh` (он делает `reset --hard`, а не `pull --rebase`).
5. **БД-миграции**: V2-код автоматически добавляет поля (`last_fault`, `fault_count` в zones) — это видно в `reports/stage-deploy-244.md`. После v2 уже в проде, но если в новом main есть дополнительные миграции — проверить `migrations/` директорию перед рестартом.
6. **Другие Wirenboard-контроллеры** (в графе упомянут `10.2.5.242` / WB-Dom как conceptually_related) — деплоятся ли они тоже? Этот runbook — только для `10.2.5.244`. Если есть ещё — сделать копию runbook и пройти его для каждого.

---

## Checklist (короткая версия runbook)

Pre-flight (шаг 1):
- [ ] `git rev-parse HEAD` → `/root/pre-consolidation.HEAD.txt`
- [ ] Watchdog cron закомментирован
- [ ] Backup в `/root/irrigation-backup-<STAMP>/` создан (repo_snapshot.tar.gz + *.db)
- [ ] `curl localhost:8080/api/status` → 200

Git-workflow-master делает force-push `refactor/v2` → `origin/main`, создаёт тег `pre-consolidation-main-2026-04-19`.

Deploy (шаг 2):
- [ ] `systemctl stop wb-irrigation` — clean stop
- [ ] `bash update_server.sh --yes --branch main`

Smoke (шаг 3):
- [ ] `systemctl status` — active, без ошибок в journalctl
- [ ] `curl localhost:8080/api/{status,zones,groups}` → 200
- [ ] `VERSION` / `git rev-parse --short HEAD` совпадают с ожидаемыми
- [ ] `sqlite3 COUNT(*)` по zones/groups — без деградации
- [ ] Watchdog cron восстановлен

Rollback (при необходимости):
- [ ] `git reset --hard pre-consolidation-main-2026-04-19` ИЛИ restore из `/root/irrigation-backup-<STAMP>/`
- [ ] При CI-rollback: `git push origin pre-consolidation-main-2026-04-19:main --force-with-lease`
