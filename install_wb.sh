#!/usr/bin/env bash
# WB-Irrigation bootstrap для свежего Wirenboard (Debian 11/12 aarch64).
# Идемпотентен — можно запускать повторно.
#
# Что делает:
#   1) ставит системные deps (curl, git, build-essential, libssl-dev, mosquitto)
#   2) ставит uv (Astral) и через него — изолированный Python 3.11
#   3) клонирует репо в /mnt/data/wb-irrigation, симлинк /opt/wb-irrigation/irrigation
#   4) создаёт venv на Python 3.11, ставит requirements.txt
#   5) копирует systemd unit, включает и стартует wb-irrigation.service
#   6) smoke check /readyz
#
# Использование:
#   sudo bash install_wb.sh           # интерактивно
#   sudo bash install_wb.sh -y        # без подтверждений

set -euo pipefail

REPO_URL=${REPO_URL:-https://github.com/Raul-1996/irrigation.git}
BRANCH=${BRANCH:-main}
DATA_DIR=${DATA_DIR:-/mnt/data/wb-irrigation}
APP_LINK=${APP_LINK:-/opt/wb-irrigation/irrigation}
SERVICE_NAME=${SERVICE_NAME:-wb-irrigation}
SERVICE_FILE="/etc/systemd/system/${SERVICE_NAME}.service"
PY_VERSION=${PY_VERSION:-3.11}
NONINTERACTIVE=${NONINTERACTIVE:-0}

usage(){
  cat <<EOF
Usage: sudo bash $(basename "$0") [-y|--yes]

Env overrides:
  REPO_URL      (default: $REPO_URL)
  BRANCH        (default: $BRANCH)
  DATA_DIR      (default: $DATA_DIR)
  APP_LINK      (default: $APP_LINK)
  PY_VERSION    (default: $PY_VERSION)
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    -h|--help) usage; exit 0;;
    -y|--yes)  NONINTERACTIVE=1; shift;;
    *) echo "Unknown arg: $1"; usage; exit 1;;
  esac
done

ts(){ date +"%Y-%m-%d %H:%M:%S"; }
info(){ echo -e "\033[1;34m[$(ts)]\033[0m $*"; }
ok(){   echo -e "\033[1;32m[$(ts)] OK\033[0m $*"; }
warn(){ echo -e "\033[1;33m[$(ts)] WARN\033[0m $*"; }
err(){  echo -e "\033[1;31m[$(ts)] ERROR\033[0m $*" >&2; }

confirm(){
  if [[ "$NONINTERACTIVE" == "1" ]]; then return 0; fi
  read -r -p "$1 [y/N]: " ans || true
  [[ "${ans,,}" == "y" || "${ans,,}" == "yes" ]]
}

# -----------------------------------------------------------------------------
# Step 0: проверки окружения
# -----------------------------------------------------------------------------
if [[ "$(id -u)" -ne 0 ]]; then
  err "Запускать под root (sudo -i, либо sudo bash $0)"
  exit 1
fi

ARCH=$(uname -m)
case "$ARCH" in
  aarch64|armv7l) ;;  # WB7 — armv7l, более новые модели — aarch64
  *)
    warn "Архитектура $ARCH (ожидались aarch64/armv7l — Wirenboard)"
    confirm "Продолжить всё равно?" || { err "Прервано пользователем"; exit 1; }
    ;;
esac

info "Архитектура: $ARCH, ОС: $(. /etc/os-release; echo "$PRETTY_NAME")"

# -----------------------------------------------------------------------------
# Step 1: системные пакеты
# -----------------------------------------------------------------------------
info "Шаг 1/6 — установка системных пакетов"
export DEBIAN_FRONTEND=noninteractive

# На свежем WB dpkg иногда оставлен в прерванном состоянии (например, апгрейд
# через UI был прерван). Без этого apt-get будет валиться с "dpkg was interrupted".
# --force-confold сохраняет существующие конфиги пакетов (напр. mosquitto.conf),
# чтобы не было интерактивных промптов на conffile-конфликте.
APT_OPTS=(-o "Dpkg::Options::=--force-confold" -o "Dpkg::Options::=--force-confdef")
dpkg --configure -a "${APT_OPTS[@]}" >/dev/null 2>&1 || true

apt-get update -qq
apt-get install -y "${APT_OPTS[@]}" \
  curl git build-essential libssl-dev \
  sqlite3 mosquitto
ok "Системные пакеты установлены"

# -----------------------------------------------------------------------------
# Step 2: uv + Python 3.11
# -----------------------------------------------------------------------------
info "Шаг 2/6 — установка uv и Python ${PY_VERSION}"

UV_BIN="/root/.local/bin/uv"
if ! [[ -x "$UV_BIN" ]]; then
  curl -LsSf https://astral.sh/uv/install.sh | sh
fi

# В новой шелл-сессии uv сам в PATH не появится — добавляем явно.
export PATH="/root/.local/bin:$PATH"

if ! command -v uv >/dev/null 2>&1; then
  err "uv не найден после установки. Проверь /root/.local/bin/uv"
  exit 1
fi
info "uv version: $(uv --version)"

# uv python install — идемпотентно: если уже стоит, не качает заново.
uv python install "${PY_VERSION}"
PY_BIN=$(uv python find "${PY_VERSION}")
if [[ ! -x "$PY_BIN" ]]; then
  err "uv не вернул путь к Python ${PY_VERSION}"
  exit 1
fi
ok "Python ${PY_VERSION}: $PY_BIN"

# -----------------------------------------------------------------------------
# Step 3: клонирование репо в /mnt/data + симлинк
# -----------------------------------------------------------------------------
info "Шаг 3/6 — клонирование репо"

# На WB корневой раздел маленький и read-mostly, /mnt/data — большой rw.
if [[ ! -d /mnt/data ]]; then
  warn "/mnt/data отсутствует — кладу репо прямо в /opt (не WB?)"
  DATA_DIR="/opt/wb-irrigation/irrigation"
  mkdir -p "$(dirname "$DATA_DIR")"
fi

mkdir -p "$(dirname "$DATA_DIR")"
if [[ -d "${DATA_DIR}/.git" ]]; then
  info "Репо уже клонирован в ${DATA_DIR}, делаю pull"
  git -C "${DATA_DIR}" fetch --all -q
  git -C "${DATA_DIR}" reset --hard "origin/${BRANCH}" -q
else
  info "Клонирую ${REPO_URL} → ${DATA_DIR}"
  git clone -b "${BRANCH}" "${REPO_URL}" "${DATA_DIR}"
fi

# Симлинк /opt/wb-irrigation/irrigation → /mnt/data/wb-irrigation
mkdir -p "$(dirname "$APP_LINK")"
if [[ -L "$APP_LINK" ]]; then
  current=$(readlink -f "$APP_LINK")
  if [[ "$current" != "$(readlink -f "$DATA_DIR")" ]]; then
    warn "Симлинк $APP_LINK указывает на $current, перенастраиваю"
    rm "$APP_LINK"
    ln -s "$DATA_DIR" "$APP_LINK"
  fi
elif [[ -d "$APP_LINK" ]]; then
  # Если там уже обычный каталог (старая установка) — не трогаю, работаем по нему.
  if [[ "$(readlink -f "$APP_LINK")" != "$(readlink -f "$DATA_DIR")" ]]; then
    warn "$APP_LINK существует как каталог (не симлинк). Использую его, репо в $DATA_DIR не привязан."
  fi
else
  ln -s "$DATA_DIR" "$APP_LINK"
fi
APP_DIR=$(readlink -f "$APP_LINK")
ok "Код приложения: $APP_DIR"

# -----------------------------------------------------------------------------
# Step 4: venv + зависимости
# -----------------------------------------------------------------------------
info "Шаг 4/6 — venv (Python ${PY_VERSION}) и pip install"

VENV_DIR="${APP_DIR}/venv"
if [[ -d "$VENV_DIR" ]]; then
  existing_py=$("${VENV_DIR}/bin/python" -c 'import sys;print(f"{sys.version_info.major}.{sys.version_info.minor}")' 2>/dev/null || echo "?")
  if [[ "$existing_py" != "$PY_VERSION" ]]; then
    warn "Существующий venv использует Python ${existing_py}, пересоздаю под ${PY_VERSION}"
    rm -rf "$VENV_DIR"
  fi
fi

if [[ ! -d "$VENV_DIR" ]]; then
  "$PY_BIN" -m venv "$VENV_DIR"
fi

"${VENV_DIR}/bin/pip" install -q -U pip wheel
"${VENV_DIR}/bin/pip" install -q -r "${APP_DIR}/requirements.txt"
ok "venv готов: $(${VENV_DIR}/bin/python -V)"

# -----------------------------------------------------------------------------
# Step 5: systemd unit
# -----------------------------------------------------------------------------
info "Шаг 5/6 — systemd unit"

REPO_UNIT="${APP_DIR}/wb-irrigation.service"
if [[ ! -f "$REPO_UNIT" ]]; then
  err "В репо нет wb-irrigation.service — деплой невозможен"
  exit 1
fi

if ! cmp -s "$REPO_UNIT" "$SERVICE_FILE" 2>/dev/null; then
  cp "$REPO_UNIT" "$SERVICE_FILE"
  systemctl daemon-reload
  ok "Unit обновлён: $SERVICE_FILE"
else
  info "Unit без изменений"
fi

systemctl enable --now "$SERVICE_NAME"

# -----------------------------------------------------------------------------
# Step 6: smoke check
# -----------------------------------------------------------------------------
info "Шаг 6/6 — smoke check /readyz"

# Дать сервису подняться (sd_notify READY ставится после старта планировщика).
for i in $(seq 1 15); do
  sleep 2
  if curl -fsS --max-time 3 "http://127.0.0.1:8080/readyz" >/dev/null 2>&1; then
    ok "/readyz отвечает 200 (попытка $i)"
    READYZ_OK=1
    break
  fi
done

if [[ "${READYZ_OK:-0}" != "1" ]]; then
  err "/readyz не отвечает за 30 секунд"
  err "Диагностика: systemctl status $SERVICE_NAME && journalctl -u $SERVICE_NAME -n 50"
  exit 1
fi

echo
ok "Установка завершена."
echo "  Веб-интерфейс: http://<wirenboard-ip>:8080"
echo "  Сервис:        systemctl status $SERVICE_NAME"
echo "  Логи:          journalctl -u $SERVICE_NAME -f"
echo "  Обновление:    bash ${APP_DIR}/update_server.sh --yes"
