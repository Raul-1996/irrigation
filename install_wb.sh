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
umask 077

REPO_URL=${REPO_URL:-https://github.com/Raul-1996/irrigation.git}
BRANCH=${BRANCH:-main}
DATA_DIR=${DATA_DIR:-/mnt/data/wb-irrigation}
APP_LINK=${APP_LINK:-/opt/wb-irrigation/irrigation}
SERVICE_NAME=${SERVICE_NAME:-wb-irrigation}
SERVICE_FILE="/etc/systemd/system/${SERVICE_NAME}.service"
ENV_FILE=/opt/wb-irrigation/.env
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

read_env_port() {
  local env_file=$1
  local value

  [[ -f "$env_file" ]] || return 1
  grep -q '^[[:space:]]*PORT[[:space:]]*=' "$env_file" || return 1
  value=$(sed -n 's/^[[:space:]]*PORT[[:space:]]*=[[:space:]]*//p' "$env_file" | tail -n 1)
  printf '%s' "$value" | tr -d "'\"[:space:]"
}

configured_port() {
  local environment_file=$1
  local repository_file=${2:-}
  local port=""
  local found=0

  if [[ ${PORT+x} ]]; then
    port=$PORT
    found=1
  elif port=$(read_env_port "$environment_file"); then
    found=1
  elif [[ -n "$repository_file" ]] && port=$(read_env_port "$repository_file"); then
    found=1
  fi
  if [[ "$found" == "0" ]]; then
    port=8080
  fi
  if [[ ! "$port" =~ ^[0-9]+$ ]] || ((port < 1 || port > 65535)); then
    err "Некорректное значение PORT: ${port:-<пусто>}"
    return 1
  fi
  printf '%s\n' "$port"
}

upsert_env_value() {
  local key=$1
  local value=$2
  local env_file=$3

  mkdir -p "$(dirname "$env_file")"
  touch "$env_file"
  chmod 600 "$env_file"
  if grep -q "^${key}=" "$env_file"; then
    sed -i.bak "s|^${key}=.*|${key}=${value}|" "$env_file"
    rm -f "${env_file}.bak"
  else
    printf '%s=%s\n' "$key" "$value" >>"$env_file"
  fi
  chmod 600 "$env_file"
}

ensure_logrotate_ready() {
  if ! command -v logrotate >/dev/null 2>&1; then
    info "Устанавливаю обязательный пакет logrotate"
    apt-get update -qq
    DEBIAN_FRONTEND=noninteractive apt-get install -y logrotate
  fi
  if ! command -v logrotate >/dev/null 2>&1 || [[ ! -f /etc/logrotate.conf ]]; then
    err "Среда logrotate установлена не полностью"
    return 1
  fi

  # Проверяем всё дерево include: так обнаруживаются дубли с package-owned
  # конфигами, которые не видны при проверке только нашего файла.
  logrotate -d "/etc/logrotate.conf"

  if systemctl cat logrotate.timer >/dev/null 2>&1; then
    systemctl enable --now logrotate.timer
    if ! systemctl is-enabled logrotate.timer >/dev/null 2>&1 \
      || ! systemctl is-active logrotate.timer >/dev/null 2>&1; then
      err "logrotate.timer установлен, но не активен"
      return 1
    fi
    info "logrotate.timer включён и активен"
  elif [[ -x /etc/cron.daily/logrotate ]] && systemctl is-active cron >/dev/null 2>&1; then
    info "logrotate запускается активным cron"
  else
    err "Не найден активный logrotate.timer или cron"
    return 1
  fi
}

retire_telegram_file_logs() {
  local application_root=$1
  local application_root_real
  local expected_logs_dir
  local logs_dir_real
  local candidate
  local candidate_name

  application_root_real=$(cd "$application_root" && pwd -P) || return 1
  expected_logs_dir="$application_root_real/services/logs"
  if [[ ! -e "$expected_logs_dir" && ! -L "$expected_logs_dir" ]]; then
    return 0
  fi
  if [[ -L "$application_root_real/services" || -L "$expected_logs_dir" || ! -d "$expected_logs_dir" ]]; then
    err "Отказываюсь обходить неканонический каталог логов: $expected_logs_dir"
    return 1
  fi
  logs_dir_real=$(cd "$expected_logs_dir" && pwd -P) || return 1
  if [[ "$logs_dir_real" != "$expected_logs_dir" ]]; then
    err "Каталог логов выходит за пределы канонического репозитория: $logs_dir_real"
    return 1
  fi

  while IFS= read -r -d '' candidate; do
    candidate_name=${candidate##*/}
    if [[ "$candidate_name" == "telegram.txt" \
      || "$candidate_name" =~ ^telegram[.]txt([.][0-9]+|-[0-9]{8})([.]gz)?$ ]]; then
      if [[ -f "$candidate" || -L "$candidate" ]]; then
        rm -f -- "$candidate"
        info "Удалён устаревший файловый Telegram-лог: $candidate"
      elif [[ -e "$candidate" ]]; then
        err "Отказываюсь удалять не-файл по пути исторического лога: $candidate"
        return 1
      fi
    fi
  done < <(find -P "$logs_dir_real" -mindepth 1 -maxdepth 1 -print0)
}

atomic_switch_app_link() {
  local legacy_backup=${1:-}
  local link_stage="${APP_LINK}.new-$$"

  if [[ -e "$link_stage" || -L "$link_stage" ]]; then
    err "Временный симлинк уже существует: $link_stage"
    return 1
  fi
  ln -s "$DATA_DIR" "$link_stage"

  if [[ -n "$legacy_backup" ]]; then
    if ! mv "$APP_LINK" "$legacy_backup"; then
      rm -f -- "$link_stage"
      return 1
    fi
  fi
  if ! mv -Tf "$link_stage" "$APP_LINK"; then
    rm -f -- "$link_stage"
    if [[ -n "$legacy_backup" && -d "$legacy_backup" && ! -e "$APP_LINK" ]]; then
      if ! mv "$legacy_backup" "$APP_LINK"; then
        err "Legacy tree remains preserved at $legacy_backup; APP_LINK recovery failed"
      fi
    fi
    return 1
  fi
}

migrate_legacy_app_tree() {
  local current=""
  local desired=""
  local migration_stage=""
  local preserved_data=""
  local legacy_backup=""

  if [[ "$APP_LINK" == "$DATA_DIR" ]]; then
    return 0
  fi
  mkdir -p "$(dirname "$APP_LINK")" "$(dirname "$DATA_DIR")"
  desired=$(readlink -f "$DATA_DIR" 2>/dev/null || printf '%s' "$DATA_DIR")

  if [[ -L "$APP_LINK" ]]; then
    current=$(readlink -f "$APP_LINK" 2>/dev/null || true)
    if [[ "$current" == "$desired" ]]; then
      return 0
    fi
    # A broken/non-repository link is replaced only after DATA_DIR is cloned.
    [[ -n "$current" && -d "$current/.git" ]] || return 0
  elif [[ -d "$APP_LINK" ]]; then
    current=$APP_LINK
    if [[ ! -d "$current/.git" ]]; then
      err "Legacy application tree is not a Git repository: $current"
      return 1
    fi
    legacy_backup="${APP_LINK}.legacy-$(date +%Y%m%d_%H%M%S)-$$"
  elif [[ -e "$APP_LINK" ]]; then
    err "$APP_LINK существует и не является каталогом или симлинком"
    return 1
  else
    return 0
  fi

  migration_stage="${DATA_DIR}.migration-$(date +%Y%m%d_%H%M%S)-$$"
  mkdir -m 700 "$migration_stage"
  info "Копирую legacy-дерево в staging на data-разделе"
  if ! cp -a "$current/." "$migration_stage/"; then
    rm -rf -- "$migration_stage"
    err "Не удалось полностью скопировать legacy-дерево; активный путь не изменён"
    return 1
  fi
  if [[ ! -d "$migration_stage/.git" ]]; then
    rm -rf -- "$migration_stage"
    err "Staging-копия legacy-дерева не прошла проверку"
    return 1
  fi

  if [[ -e "$DATA_DIR" || -L "$DATA_DIR" ]]; then
    preserved_data="${DATA_DIR}.pre-migration-$(date +%Y%m%d_%H%M%S)-$$"
    if ! mv "$DATA_DIR" "$preserved_data"; then
      rm -rf -- "$migration_stage"
      return 1
    fi
  fi
  if ! mv "$migration_stage" "$DATA_DIR"; then
    if [[ -n "$preserved_data" && ! -e "$DATA_DIR" ]]; then
      if ! mv "$preserved_data" "$DATA_DIR"; then
        err "Previous data tree remains preserved at $preserved_data"
      fi
    fi
    rm -rf -- "$migration_stage"
    return 1
  fi

  if ! atomic_switch_app_link "$legacy_backup"; then
    err "Не удалось атомарно переключить APP_LINK; старый путь сохранён"
    return 1
  fi
}

# Tests source the actual migration helpers and inject an ENOSPC-like copy
# failure without running the privileged bootstrap body.
if [[ "${BASH_SOURCE[0]}" != "$0" ]]; then
  return 0
fi

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
  sqlite3 mosquitto logrotate
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

# На повторной установке не меняем живое дерево под работающим процессом.
# Если дальнейший шаг упадёт, EXIT-trap вернёт ранее активный сервис.
SERVICE_WAS_ACTIVE=0
SERVICE_RESTORED=0
restore_service_on_failure() {
  local status=$?
  trap - EXIT
  if [[ "$status" -ne 0 && "$SERVICE_WAS_ACTIVE" == "1" && "$SERVICE_RESTORED" == "0" ]]; then
    warn "Установка не завершилась; восстанавливаю ранее активный $SERVICE_NAME"
    if ! systemctl start "$SERVICE_NAME"; then
      err "Не удалось восстановить $SERVICE_NAME"
    fi
  fi
  exit "$status"
}
trap restore_service_on_failure EXIT

if systemctl is-active "$SERVICE_NAME" >/dev/null 2>&1; then
  SERVICE_WAS_ACTIVE=1
  info "Останавливаю активный $SERVICE_NAME перед обновлением дерева"
  systemctl stop "$SERVICE_NAME"
fi

ACTIVE_APP_DIR=$(readlink -f "$APP_LINK" 2>/dev/null || true)
if [[ -n "$ACTIVE_APP_DIR" && -d "$ACTIVE_APP_DIR/.git" ]]; then
  retire_telegram_file_logs "$ACTIVE_APP_DIR"
fi
ACTIVE_DATA_DIR=$(readlink -f "$DATA_DIR" 2>/dev/null || true)
if [[ -n "$ACTIVE_DATA_DIR" && "$ACTIVE_DATA_DIR" != "$ACTIVE_APP_DIR" && -d "$ACTIVE_DATA_DIR/.git" ]]; then
  retire_telegram_file_logs "$ACTIVE_DATA_DIR"
fi

migrate_legacy_app_tree

mkdir -p "$(dirname "$DATA_DIR")"
if [[ -d "${DATA_DIR}/.git" ]]; then
  info "Репо уже клонирован в ${DATA_DIR}, делаю pull"
  git -C "${DATA_DIR}" fetch --all -q
  git -C "${DATA_DIR}" reset --hard "origin/${BRANCH}" -q
else
  info "Клонирую ${REPO_URL} → ${DATA_DIR}"
  git clone -b "${BRANCH}" "${REPO_URL}" "${DATA_DIR}"
fi

if [[ "$APP_LINK" == "$DATA_DIR" ]]; then
  APP_DIR=$(readlink -f "$DATA_DIR")
else
  current=$(readlink -f "$APP_LINK" 2>/dev/null || true)
  desired=$(readlink -f "$DATA_DIR")
  if [[ "$current" != "$desired" ]]; then
    atomic_switch_app_link ""
  fi
  APP_DIR=$(readlink -f "$APP_LINK")
fi
retire_telegram_file_logs "$APP_DIR"
for secret_file in "$APP_DIR/.secret_key" "$APP_DIR/.irrig_secret_key" "$APP_DIR/.env"; do
  if [[ -f "$secret_file" ]]; then
    chmod 600 "$secret_file"
  fi
done
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
  install -m 0644 "$REPO_UNIT" "$SERVICE_FILE"
  systemctl daemon-reload
  ok "Unit обновлён: $SERVICE_FILE"
else
  info "Unit без изменений"
fi

LOGROTATE_SOURCE="${APP_DIR}/configs/logrotate.d/wb-irrigation"
LOGROTATE_TARGET="/etc/logrotate.d/wb-irrigation"
if [[ ! -f "$LOGROTATE_SOURCE" ]]; then
  err "В репо нет logrotate-конфига: $LOGROTATE_SOURCE"
  exit 1
fi
install -m 0644 "$LOGROTATE_SOURCE" "$LOGROTATE_TARGET"
ensure_logrotate_ready
ok "Logrotate-конфиг установлен: $LOGROTATE_TARGET"

CURRENT_COMMIT=$(git -C "$APP_DIR" rev-parse HEAD)
APP_PORT=$(configured_port "$ENV_FILE" "$APP_DIR/.env")
upsert_env_value GIT_COMMIT "$CURRENT_COMMIT" "$ENV_FILE"
if [[ ${PORT+x} ]]; then
  upsert_env_value PORT "$APP_PORT" "$ENV_FILE"
fi

# `enable --now` is a no-op for an already active service. Always restart so a
# rerun loads the freshly installed code, dependencies, unit and EnvironmentFile.
systemctl enable "$SERVICE_NAME"
systemctl restart "$SERVICE_NAME"
if ! systemctl is-active "$SERVICE_NAME" >/dev/null 2>&1; then
  err "$SERVICE_NAME не запустился"
  exit 1
fi
SERVICE_RESTORED=1

# -----------------------------------------------------------------------------
# Step 6: smoke check
# -----------------------------------------------------------------------------
info "Шаг 6/6 — smoke check /readyz"

# Дать сервису подняться (sd_notify READY ставится после старта планировщика).
for i in $(seq 1 15); do
  if curl -fsS --max-time 3 "http://127.0.0.1:${APP_PORT}/readyz" >/dev/null 2>&1; then
    ok "/readyz отвечает 200 (попытка $i)"
    READYZ_OK=1
    break
  fi
  sleep 2
done

if [[ "${READYZ_OK:-0}" != "1" ]]; then
  err "/readyz не отвечает за 30 секунд на PORT=$APP_PORT"
  err "Диагностика: systemctl status $SERVICE_NAME && journalctl -u $SERVICE_NAME -n 50"
  exit 1
fi

echo
ok "Установка завершена."
echo "  Веб-интерфейс: http://<wirenboard-ip>:${APP_PORT}"
echo "  Сервис:        systemctl status $SERVICE_NAME"
echo "  Логи:          journalctl -u $SERVICE_NAME -f"
echo "  Обновление:    bash ${APP_DIR}/update_server.sh --yes"
