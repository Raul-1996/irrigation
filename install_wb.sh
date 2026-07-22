#!/usr/bin/env bash
# WB-Irrigation bootstrap для свежего Wirenboard (Debian 11/12 aarch64).
# Идемпотентен — можно запускать повторно.
#
# Что делает:
#   1) ставит системные deps (curl, git, build-essential, libssl-dev, mosquitto)
#   2) ставит uv (Astral) и через него — изолированный Python 3.11
#   3) клонирует репо в /mnt/data/wb-irrigation, симлинк /opt/wb-irrigation/irrigation
#   4) создаёт venv на Python 3.11, ставит hash-locked requirements.lock
#   5) копирует systemd unit, включает и стартует wb-irrigation.service
#   6) smoke check /readyz
#
# Использование:
#   sudo bash install_wb.sh --commit <FULL_SHA>
#   sudo bash install_wb.sh -y --commit <FULL_SHA>

set -euo pipefail
umask 077

REPO_URL=${REPO_URL:-https://github.com/Raul-1996/irrigation.git}
BRANCH=${BRANCH:-main}
TARGET_COMMIT=${TARGET_COMMIT:-}
DATA_DIR=${DATA_DIR:-/mnt/data/wb-irrigation}
APP_LINK=${APP_LINK:-/opt/wb-irrigation/irrigation}
SERVICE_NAME=${SERVICE_NAME:-wb-irrigation}
SERVICE_FILE="/etc/systemd/system/${SERVICE_NAME}.service"
ENV_FILE=${ENV_FILE:-/opt/wb-irrigation/.env}
RELEASE_ENV_FILE=${RELEASE_ENV_FILE:-/opt/wb-irrigation/release.env}
MQTT_TLS_DIR=${MQTT_TLS_DIR:-/opt/wb-irrigation/mqtt-tls}
PY_VERSION=${PY_VERSION:-3.11.15}
UV_BIN=${UV_BIN:-/mnt/data/wb-irrigation-tools/uv}
STATE_DIR=${STATE_DIR:-/mnt/data/wb-irrigation-state}
DEPLOY_CONTROL_DIR=${DEPLOY_CONTROL_DIR:-/mnt/data/wb-irrigation-deploy}
PYTHON_INSTALL_DIR=${PYTHON_INSTALL_DIR:-/mnt/data/wb-irrigation-python}
SERVICE_USER=${SERVICE_USER:-wb-irrigation}
SERVICE_GROUP=${SERVICE_GROUP:-wb-irrigation}
NONINTERACTIVE=${NONINTERACTIVE:-0}
DEPLOY_LOCK_DIR=${DEPLOY_LOCK_DIR:-/run/lock/wb-irrigation}
DEPLOY_LOCK_FILE=${DEPLOY_LOCK_FILE:-/run/lock/wb-irrigation/deploy.lock}
readonly FIXED_DATA_DIR=/mnt/data/wb-irrigation
readonly FIXED_APP_LINK=/opt/wb-irrigation/irrigation
readonly FIXED_SERVICE_NAME=wb-irrigation
readonly FIXED_ENV_FILE=/opt/wb-irrigation/.env
readonly FIXED_RELEASE_ENV_FILE=/opt/wb-irrigation/release.env
readonly FIXED_MQTT_TLS_DIR=/opt/wb-irrigation/mqtt-tls
readonly FIXED_STATE_DIR=/mnt/data/wb-irrigation-state
readonly FIXED_DEPLOY_CONTROL_DIR=/mnt/data/wb-irrigation-deploy
readonly FIXED_PYTHON_INSTALL_DIR=/mnt/data/wb-irrigation-python
readonly FIXED_PY_VERSION=3.11.15
readonly FIXED_UV_BIN=/mnt/data/wb-irrigation-tools/uv
readonly FIXED_DEPLOY_LOCK_DIR=/run/lock/wb-irrigation
readonly FIXED_DEPLOY_LOCK_FILE=/run/lock/wb-irrigation/deploy.lock
readonly UV_VERSION=0.11.31
readonly UV_ARCHIVE_URL=https://releases.astral.sh/github/uv/releases/download/0.11.31/uv-aarch64-unknown-linux-gnu.tar.gz
readonly UV_ARCHIVE_SHA256=d74f23949fd07be4970f293d06ca99d87cd2a78a341c3d7b7fc0df7bc2d8a145
# Versioned security contract for the currently integrated Telegram runtime.
# Any legitimate edit to that runtime must deliberately update this allowlist.
readonly TELEGRAM_LOG_RETIREMENT_BLOB=e4fc7b282236aac7675c879b892cbf1192dc66f0
MQTT_TLS_CREATED_FILES=()
MQTT_TLS_DIR_CREATED=0

usage(){
  cat <<EOF
Usage: sudo bash $(basename "$0") --commit SHA [-y|--yes]

Env overrides:
  REPO_URL      (default: $REPO_URL)
  BRANCH        (default: $BRANCH)
  TARGET_COMMIT (required full commit SHA)
  DATA_DIR      (default: $DATA_DIR)
  APP_LINK      (default: $APP_LINK)
Pinned production toolchain: uv $UV_VERSION, Python $PY_VERSION (не переопределяется).
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    -h|--help) usage; exit 0;;
    -y|--yes)  NONINTERACTIVE=1; shift;;
    --branch) BRANCH="$2"; shift 2;;
    --commit) TARGET_COMMIT="$2"; shift 2;;
    *) echo "Unknown arg: $1"; usage; exit 1;;
  esac
done

ts(){ date +"%Y-%m-%d %H:%M:%S"; }
info(){ echo -e "\033[1;34m[$(ts)]\033[0m $*"; }
ok(){   echo -e "\033[1;32m[$(ts)] OK\033[0m $*"; }
warn(){ echo -e "\033[1;33m[$(ts)] WARN\033[0m $*"; }
err(){  echo -e "\033[1;31m[$(ts)] ERROR\033[0m $*" >&2; }

require_full_commit_sha() {
  local target_commit=${1:-}

  if [[ ! "$target_commit" =~ ^[0-9a-f]{40}$ ]]; then
    err "TARGET_COMMIT должен быть полным lowercase Git commit SHA из 40 символов"
    return 1
  fi
}

validate_branch_name() {
  if [[ "$BRANCH" != "main" ]]; then
    err "Production install разрешает commit только из ветки main"
    return 1
  fi
}

fetch_authorization_branch() {
  local repository=$1

  info "Получаю ветку авторизации origin/$BRANCH"
  git -C "$repository" fetch --quiet origin \
    "+refs/heads/$BRANCH:refs/remotes/origin/$BRANCH"
}

resolve_authorized_target_commit() {
  local repository=$1
  local target_commit=$2
  local resolved_target

  require_full_commit_sha "$target_commit" || return 1
  if ! resolved_target=$(git -C "$repository" rev-parse --verify "${target_commit}^{commit}"); then
    err "Commit отсутствует после получения origin/$BRANCH: $target_commit"
    return 1
  fi
  if [[ "$resolved_target" != "$target_commit" ]]; then
    err "Ревизия не разрешилась в точный immutable commit: $target_commit"
    return 1
  fi
  if ! git -C "$repository" merge-base --is-ancestor "$target_commit" "refs/remotes/origin/$BRANCH"; then
    err "Commit не авторизован веткой origin/$BRANCH: $target_commit"
    return 1
  fi
  printf '%s\n' "$resolved_target"
}

validate_fixed_deployment_contract() {
  if [[ "$BRANCH" != "main" ]]; then
    err "BRANCH override не поддерживается production deployment: $BRANCH"
    return 1
  fi
  if [[ "$DATA_DIR" != "$FIXED_DATA_DIR" ]]; then
    err "DATA_DIR override не поддерживается production unit: $DATA_DIR"
    return 1
  fi
  if [[ "$APP_LINK" != "$FIXED_APP_LINK" ]]; then
    err "APP_LINK override не поддерживается production unit: $APP_LINK"
    return 1
  fi
  if [[ "$SERVICE_NAME" != "$FIXED_SERVICE_NAME" ]]; then
    err "SERVICE_NAME override не поддерживается production unit: $SERVICE_NAME"
    return 1
  fi
  if [[ "$ENV_FILE" != "$FIXED_ENV_FILE" ]]; then
    err "ENV_FILE override не поддерживается production unit: $ENV_FILE"
    return 1
  fi
  if [[ "$RELEASE_ENV_FILE" != "$FIXED_RELEASE_ENV_FILE" ]]; then
    err "RELEASE_ENV_FILE override не поддерживается production unit"
    return 1
  fi
  if [[ "$MQTT_TLS_DIR" != "$FIXED_MQTT_TLS_DIR" ]]; then
    err "MQTT_TLS_DIR override не поддерживается production unit"
    return 1
  fi
  if [[ "$STATE_DIR" != "$FIXED_STATE_DIR" ]]; then
    err "STATE_DIR override не поддерживается production unit: $STATE_DIR"
    return 1
  fi
  if [[ "$DEPLOY_CONTROL_DIR" != "$FIXED_DEPLOY_CONTROL_DIR" ]]; then
    err "DEPLOY_CONTROL_DIR override не поддерживается production deployment"
    return 1
  fi
  if [[ "$PYTHON_INSTALL_DIR" != "$FIXED_PYTHON_INSTALL_DIR" ]]; then
    err "PYTHON_INSTALL_DIR override не поддерживается production unit"
    return 1
  fi
  if [[ "$PY_VERSION" != "$FIXED_PY_VERSION" ]]; then
    err "PY_VERSION override не поддерживается pinned production runtime"
    return 1
  fi
  if [[ "$UV_BIN" != "$FIXED_UV_BIN" ]]; then
    err "UV_BIN override не поддерживается pinned production toolchain"
    return 1
  fi
  if [[ "$SERVICE_USER" != "wb-irrigation" || "$SERVICE_GROUP" != "wb-irrigation" ]]; then
    err "SERVICE_USER/SERVICE_GROUP overrides не поддерживаются"
    return 1
  fi
  if [[ "$DEPLOY_LOCK_DIR" != "$FIXED_DEPLOY_LOCK_DIR" \
    || "$DEPLOY_LOCK_FILE" != "$FIXED_DEPLOY_LOCK_FILE" ]]; then
    err "Deployment lock path override не поддерживается production deployment"
    return 1
  fi
}

stat_owner_uid() {
  stat -c %u "$1" 2>/dev/null || stat -f %u "$1"
}

stat_link_count() {
  stat -c %h "$1" 2>/dev/null || stat -f %l "$1"
}

stat_followed_identity() {
  stat -Lc '%u:%h:%i' "$1" 2>/dev/null || stat -Lf '%u:%l:%i' "$1"
}

acquire_deploy_lock() {
  local inherited_fd=${WB_IRRIGATION_DEPLOY_LOCK_FD:-}
  local inherited_path=""
  local expected_uid=0
  local lock_parent
  local path_identity
  local descriptor_identity
  local descriptor_path=/proc/$$/fd/9

  lock_parent=$(dirname -- "$DEPLOY_LOCK_DIR")
  [[ -e "$descriptor_path" ]] || descriptor_path=/dev/fd/9
  if [[ "$DEPLOY_LOCK_FILE" != "$FIXED_DEPLOY_LOCK_FILE" ]]; then
    expected_uid=$(id -u)
  fi

  if ! command -v flock >/dev/null 2>&1; then
    err "Для сериализации install/update/uninstall требуется flock"
    return 1
  fi
  if [[ -L "$lock_parent" || ! -d "$lock_parent" \
    || "$(stat_owner_uid "$lock_parent")" != "$expected_uid" ]]; then
    err "Deployment lock parent не является trusted real directory: $lock_parent"
    return 1
  fi
  if [[ ! -e "$DEPLOY_LOCK_DIR" ]]; then
    if ! install -d -m 0700 "$DEPLOY_LOCK_DIR"; then
      err "Не удалось создать private deployment lock directory"
      return 1
    fi
  fi
  if [[ -L "$DEPLOY_LOCK_DIR" || ! -d "$DEPLOY_LOCK_DIR" \
    || "$(stat_owner_uid "$DEPLOY_LOCK_DIR")" != "$expected_uid" ]]; then
    err "Deployment lock directory не является trusted: $DEPLOY_LOCK_DIR"
    return 1
  fi
  if ! chmod 0700 "$DEPLOY_LOCK_DIR"; then
    err "Не удалось защитить deployment lock directory"
    return 1
  fi
  if [[ -e "$DEPLOY_LOCK_FILE" || -L "$DEPLOY_LOCK_FILE" ]]; then
    if [[ -L "$DEPLOY_LOCK_FILE" || ! -f "$DEPLOY_LOCK_FILE" \
      || "$(stat_owner_uid "$DEPLOY_LOCK_FILE")" != "$expected_uid" \
      || "$(stat_link_count "$DEPLOY_LOCK_FILE")" != "1" ]]; then
      err "Deployment lock file не является trusted regular file: $DEPLOY_LOCK_FILE"
      return 1
    fi
  fi
  if [[ -n "$inherited_fd" ]]; then
    if [[ "$inherited_fd" != "9" ]] || ! : >&9 2>/dev/null; then
      err "Некорректный унаследованный descriptor deployment lock"
      return 1
    fi
    inherited_path=$(realpath "$descriptor_path") || return 1
    if [[ ( "$descriptor_path" == /proc/* && "$inherited_path" != "$DEPLOY_LOCK_FILE" ) \
      || ! -f "$DEPLOY_LOCK_FILE" || -L "$DEPLOY_LOCK_FILE" ]]; then
      err "Унаследованный descriptor не указывает на $DEPLOY_LOCK_FILE"
      return 1
    fi
    path_identity=$(stat_followed_identity "$DEPLOY_LOCK_FILE") || return 1
    descriptor_identity=$(stat_followed_identity "$descriptor_path") || return 1
    if [[ "$path_identity" != "$descriptor_identity" \
      || "$path_identity" != "$expected_uid:1:"* ]]; then
      err "Унаследованный deployment lock descriptor имеет unsafe identity"
      return 1
    fi
    if ! flock -n 9; then
      err "Унаследованный deployment lock некорректен"
      return 1
    fi
    return 0
  fi

  exec 9>>"$DEPLOY_LOCK_FILE"
  if [[ -L "$DEPLOY_LOCK_FILE" || ! -f "$DEPLOY_LOCK_FILE" ]] \
    || ! chmod 0600 "$DEPLOY_LOCK_FILE"; then
    err "Не удалось защитить deployment lock file"
    return 1
  fi
  path_identity=$(stat_followed_identity "$DEPLOY_LOCK_FILE") || return 1
  descriptor_identity=$(stat_followed_identity "$descriptor_path") || return 1
  if [[ "$path_identity" != "$descriptor_identity" \
    || "$path_identity" != "$expected_uid:1:"* ]]; then
    err "Deployment lock path изменился во время открытия"
    return 1
  fi
  if ! flock -n 9; then
    err "Другой install, update или uninstall уже удерживает $DEPLOY_LOCK_FILE"
    return 1
  fi
  if [[ "$(stat_followed_identity "$DEPLOY_LOCK_FILE")" != "$descriptor_identity" ]]; then
    err "Deployment lock path изменился после flock"
    return 1
  fi
  export WB_IRRIGATION_DEPLOY_LOCK_FD=9
}

install_pinned_uv() {
  local architecture tools_dir staging_dir archive extracted_uv staged_uv metadata

  architecture=$(uname -m)
  if [[ "$architecture" != "aarch64" ]]; then
    err "Production uv bootstrap поддерживает только aarch64, получено: $architecture"
    return 1
  fi
  if [[ -L "$UV_BIN" || ( -e "$UV_BIN" && ! -f "$UV_BIN" ) ]]; then
    err "Небезопасный destination pinned uv: $UV_BIN"
    return 1
  fi

  tools_dir=$(dirname "$UV_BIN")
  if [[ -L "$tools_dir" || ( -e "$tools_dir" && ! -d "$tools_dir" ) ]]; then
    err "Небезопасный путь pinned uv tools: $tools_dir"
    return 1
  fi
  mkdir -p "$tools_dir"
  chown root:root "$tools_dir"
  chmod 0755 "$tools_dir"
  if ! staging_dir=$(mktemp -d /mnt/data/.wb-irrigation-uv.XXXXXX); then
    err "Не удалось создать staging-каталог pinned uv"
    return 1
  fi
  archive="$staging_dir/uv.tar.gz"
  if ! curl --proto '=https' --tlsv1.2 -fsSLo "$archive" "$UV_ARCHIVE_URL"; then
    rm -rf -- "$staging_dir"
    err "Не удалось скачать pinned uv archive"
    return 1
  fi
  if ! printf '%s  %s\n' "$UV_ARCHIVE_SHA256" "$archive" | sha256sum -c - >/dev/null; then
    rm -rf -- "$staging_dir"
    err "SHA-256 pinned uv archive не совпал"
    return 1
  fi
  if ! tar -xzf "$archive" -C "$staging_dir"; then
    rm -rf -- "$staging_dir"
    err "Не удалось распаковать pinned uv archive"
    return 1
  fi
  extracted_uv="$staging_dir/uv-aarch64-unknown-linux-gnu/uv"
  if [[ ! -f "$extracted_uv" || -L "$extracted_uv" ]]; then
    rm -rf -- "$staging_dir"
    err "Неожиданная структура pinned uv archive"
    return 1
  fi
  staged_uv="${UV_BIN}.new-$$"
  if [[ -e "$staged_uv" || -L "$staged_uv" ]]; then
    rm -rf -- "$staging_dir"
    err "Staging destination pinned uv уже существует: $staged_uv"
    return 1
  fi
  if ! install -o root -g root -m 0755 "$extracted_uv" "$staged_uv"; then
    rm -rf -- "$staging_dir"
    return 1
  fi
  if [[ "$($staged_uv --version)" != "uv $UV_VERSION" ]]; then
    rm -f -- "$staged_uv"
    rm -rf -- "$staging_dir"
    err "Pinned uv binary сообщает неожиданную версию"
    return 1
  fi
  if ! mv "$staged_uv" "$UV_BIN"; then
    rm -f -- "$staged_uv"
    rm -rf -- "$staging_dir"
    return 1
  fi
  metadata=$(stat -c '%u:%g:%a' "$UV_BIN")
  if [[ "$metadata" != "0:0:755" || "$($UV_BIN --version)" != "uv $UV_VERSION" ]]; then
    rm -rf -- "$staging_dir"
    err "Установленный uv не совпадает с pinned root-owned uv $UV_VERSION"
    return 1
  fi
  rm -rf -- "$staging_dir"
}

ensure_service_account() {
  local passwd_entry group_entry
  local group_gid group_members conflicting_primary
  local account_name password uid gid gecos account_home account_shell

  if ! getent group "$SERVICE_GROUP" >/dev/null 2>&1; then
    groupadd --system "$SERVICE_GROUP"
  fi
  group_entry=$(getent group "$SERVICE_GROUP")
  group_gid=$(printf '%s' "$group_entry" | cut -d: -f3)
  group_members=$(printf '%s' "$group_entry" | cut -d: -f4)
  if [[ "$group_gid" == "0" ]]; then
    err "$SERVICE_GROUP не должна использовать привилегированный GID 0"
    return 1
  fi
  if [[ -n "$group_members" && "$group_members" != "$SERVICE_USER" ]]; then
    err "$SERVICE_GROUP содержит неожиданных supplementary users"
    return 1
  fi
  conflicting_primary=$(getent passwd | awk -F: -v gid="$group_gid" -v user="$SERVICE_USER" \
    '$4 == gid && $1 != user {print $1; exit}')
  if [[ -n "$conflicting_primary" ]]; then
    err "$SERVICE_GROUP является primary group неожиданного пользователя $conflicting_primary"
    return 1
  fi
  if id "$SERVICE_USER" >/dev/null 2>&1; then
    if [[ "$(id -gn "$SERVICE_USER")" != "$SERVICE_GROUP" ]]; then
      err "$SERVICE_USER уже существует с другой primary group"
      return 1
    fi
    passwd_entry=$(getent passwd "$SERVICE_USER")
    IFS=: read -r account_name password uid gid gecos account_home account_shell <<<"$passwd_entry"
    if [[ "$uid" == "0" || "$gid" == "0" ]]; then
      err "$SERVICE_USER не должна использовать привилегированный UID или GID"
      return 1
    fi
    if [[ "$account_home" != "$STATE_DIR" || "$account_shell" != "/usr/sbin/nologin" ]]; then
      err "$SERVICE_USER уже существует с неожиданным home или shell"
      return 1
    fi
  else
    useradd --system --gid "$SERVICE_GROUP" --home-dir "$STATE_DIR" \
      --shell /usr/sbin/nologin --no-create-home "$SERVICE_USER"
  fi
  if [[ "$(id -Gn "$SERVICE_USER")" != "$SERVICE_GROUP" ]]; then
    err "$SERVICE_USER не должна входить в supplementary groups"
    return 1
  fi
}

assert_safe_state_source() {
  local source_path=$1
  local source_kind=$2
  local symlink_path=""

  if [[ -L "$source_path" ]]; then
    err "Отказываюсь копировать symlink mutable state: $source_path"
    return 1
  fi
  if [[ "$source_kind" == "file" ]]; then
    [[ ! -e "$source_path" || -f "$source_path" ]] || {
      err "Mutable state path не является обычным файлом: $source_path"
      return 1
    }
    return 0
  fi
  [[ ! -e "$source_path" || -d "$source_path" ]] || {
    err "Mutable state path не является каталогом: $source_path"
    return 1
  }
  if [[ -d "$source_path" ]]; then
    symlink_path=$(find -P "$source_path" -type l -print -quit)
    if [[ -n "$symlink_path" ]]; then
      err "Mutable state tree содержит symlink: $symlink_path"
      return 1
    fi
  fi
}

copy_state_database() {
  local source_path=$1 target_path=$2 owner=$3 group=$4
  local staged_path="${target_path}.state-stage-$$"
  local check_result

  assert_safe_state_source "$source_path" file || return 1
  [[ -f "$source_path" ]] || return 0
  rm -f -- "$staged_path"
  if ! sqlite3 "$source_path" ".backup '$staged_path'"; then
    rm -f -- "$staged_path"
    err "Не удалось скопировать SQLite state: $source_path"
    return 1
  fi
  check_result=$(sqlite3 "$staged_path" "PRAGMA quick_check;")
  if [[ "$check_result" != "ok" ]]; then
    rm -f -- "$staged_path"
    err "SQLite state не прошёл quick_check: $source_path"
    return 1
  fi
  chmod 0600 "$staged_path"
  chown "$owner:$group" "$staged_path"
  mv -f -- "$staged_path" "$target_path"
}

migrate_mqtt_tls_paths() {
  local database_path=$1 legacy_root=$2
  local table_exists schema_columns invalid_paths rows column server_id configured_path
  local source_path source_real destination_path staged_path check_result
  local sql_updates=""

  [[ -f "$database_path" && ! -L "$database_path" ]] || return 0
  table_exists=$(sqlite3 "$database_path" \
    "SELECT count(*) FROM sqlite_master WHERE type='table' AND name='mqtt_servers';") || return 1
  [[ "$table_exists" == "1" ]] || return 0
  schema_columns=$(sqlite3 "$database_path" \
    "SELECT group_concat(name, ',') FROM pragma_table_info('mqtt_servers');") || return 1
  for column in enabled tls_enabled tls_ca_path tls_cert_path tls_key_path; do
    [[ ",$schema_columns," == *",$column,"* ]] || return 0
  done
  if [[ -L "$MQTT_TLS_DIR" || ( -e "$MQTT_TLS_DIR" && ! -d "$MQTT_TLS_DIR" ) ]]; then
    err "MQTT TLS material directory небезопасен: $MQTT_TLS_DIR"
    return 1
  fi
  [[ -e "$MQTT_TLS_DIR" ]] || MQTT_TLS_DIR_CREATED=1
  if ! install -d -m 0750 "$MQTT_TLS_DIR" \
    || ! chown root:"$SERVICE_GROUP" "$MQTT_TLS_DIR" \
    || ! chmod 0750 "$MQTT_TLS_DIR"; then
    err "Не удалось защитить MQTT TLS material directory"
    return 1
  fi
  for column in tls_ca_path tls_cert_path tls_key_path; do
    invalid_paths=$(sqlite3 "$database_path" \
      "SELECT count(*) FROM mqtt_servers WHERE enabled=1 AND tls_enabled=1
       AND coalesce($column, '') != ''
       AND (instr($column, '|') > 0 OR instr($column, char(10)) > 0 OR instr($column, char(13)) > 0);") \
      || return 1
    if [[ "$invalid_paths" != "0" ]]; then
      err "MQTT TLS path содержит unsupported control characters: $column"
      return 1
    fi
    rows=$(sqlite3 -separator '|' "$database_path" \
      "SELECT id, $column FROM mqtt_servers WHERE enabled=1 AND tls_enabled=1
       AND coalesce($column, '') != '' ORDER BY id;") || return 1
    while IFS='|' read -r server_id configured_path; do
      [[ -n "$server_id" ]] || continue
      if [[ ! "$server_id" =~ ^[0-9]+$ || -z "$configured_path" ]]; then
        err "Invalid MQTT TLS database row"
        return 1
      fi
      if [[ "$configured_path" == /* ]]; then
        source_path=$configured_path
        if [[ -L "$source_path" || ! -f "$source_path" ]]; then
          err "MQTT TLS material отсутствует или является symlink: $source_path"
          return 1
        fi
        if runuser -u "$SERVICE_USER" -- test -r "$source_path"; then
          continue
        fi
      else
        if [[ "$configured_path" == ".." || "$configured_path" == ../* \
          || "$configured_path" == */../* || "$configured_path" == */.. ]]; then
          err "Unsafe relative MQTT TLS path: $configured_path"
          return 1
        fi
        source_path="$legacy_root/$configured_path"
      fi
      if [[ -L "$source_path" || ! -f "$source_path" ]]; then
        err "MQTT TLS material недоступен: $source_path"
        return 1
      fi
      source_real=$(readlink -f -- "$source_path") || return 1
      if [[ "$configured_path" != /* && "$source_real" != "$legacy_root"/* ]]; then
        err "Relative MQTT TLS material выходит за legacy repository"
        return 1
      fi
      destination_path="$MQTT_TLS_DIR/server-${server_id}-${column}.pem"
      if [[ -L "$destination_path" || ( -e "$destination_path" && ! -f "$destination_path" ) ]]; then
        err "MQTT TLS destination небезопасен: $destination_path"
        return 1
      fi
      if [[ -f "$destination_path" ]]; then
        if ! cmp -s -- "$source_real" "$destination_path"; then
          err "Existing MQTT TLS destination отличается от migration source"
          return 1
        fi
      else
        staged_path=""
        if ! staged_path=$(mktemp "$MQTT_TLS_DIR/.mqtt-tls.XXXXXX") \
          || ! cp -- "$source_real" "$staged_path" \
          || ! chmod 0640 "$staged_path" \
          || ! chown root:"$SERVICE_GROUP" "$staged_path" \
          || ! mv -- "$staged_path" "$destination_path"; then
          [[ -n "${staged_path:-}" ]] && rm -f -- "$staged_path"
          err "Не удалось publish MQTT TLS material"
          return 1
        fi
        MQTT_TLS_CREATED_FILES+=("$destination_path")
      fi
      if ! runuser -u "$SERVICE_USER" -- test -r "$destination_path"; then
        err "Migrated MQTT TLS material не читается service user"
        return 1
      fi
      sql_updates+="UPDATE mqtt_servers SET $column='$destination_path' WHERE id=$server_id;"
    done <<<"$rows"
  done
  if [[ -n "$sql_updates" ]]; then
    if ! sqlite3 "$database_path" "BEGIN IMMEDIATE;${sql_updates}COMMIT;"; then
      err "Не удалось atomically update MQTT TLS paths"
      return 1
    fi
    check_result=$(sqlite3 "$database_path" "PRAGMA quick_check;") || return 1
    if [[ "$check_result" != "ok" ]]; then
      err "Database failed quick_check after MQTT TLS path migration"
      return 1
    fi
  fi
}

cleanup_created_mqtt_tls_material() {
  local created_file

  for created_file in "${MQTT_TLS_CREATED_FILES[@]-}"; do
    [[ -n "$created_file" ]] || continue
    if [[ "$created_file" != "$MQTT_TLS_DIR"/server-*-tls_*_path.pem \
      || -L "$created_file" || ! -f "$created_file" ]]; then
      err "Refusing unsafe MQTT TLS rollback cleanup: $created_file"
      return 1
    fi
    rm -f -- "$created_file" || return 1
  done
  MQTT_TLS_CREATED_FILES=()
  if [[ "$MQTT_TLS_DIR_CREATED" == "1" && -d "$MQTT_TLS_DIR" && ! -L "$MQTT_TLS_DIR" ]]; then
    rmdir -- "$MQTT_TLS_DIR" || return 1
    MQTT_TLS_DIR_CREATED=0
  fi
}

copy_state_file() {
  local source_path=$1 target_path=$2 owner=$3 group=$4 mode=$5
  local staged_path="${target_path}.state-stage-$$"

  assert_safe_state_source "$source_path" file || return 1
  [[ -f "$source_path" ]] || return 0
  install -m "$mode" "$source_path" "$staged_path"
  chown "$owner:$group" "$staged_path"
  mv -f -- "$staged_path" "$target_path"
}

copy_state_directory() {
  local source_path=$1 target_path=$2 owner=$3 group=$4
  local staged_path="${target_path}.state-stage-$$"

  assert_safe_state_source "$source_path" directory || return 1
  [[ -d "$source_path" ]] || return 0
  if [[ -e "$staged_path" || -L "$staged_path" || -e "$target_path" || -L "$target_path" ]]; then
    err "State copy destination уже существует: $target_path"
    return 1
  fi
  mkdir -p "$(dirname "$target_path")"
  mkdir -m 0700 "$staged_path"
  if ! cp -a "$source_path/." "$staged_path/"; then
    rm -rf -- "$staged_path"
    return 1
  fi
  chown -R "$owner:$group" "$staged_path"
  chmod -R go-rwx "$staged_path"
  mv "$staged_path" "$target_path"
}

sync_mutable_state_tree() {
  local source_root=$1 target_root=$2 owner=$3 group=$4
  local state_dir

  if [[ -L "$source_root" || ! -d "$source_root" || -L "$target_root" ]]; then
    err "Mutable state roots должны быть обычными каталогами"
    return 1
  fi
  mkdir -p "$target_root"
  chmod 0750 "$target_root"
  chown "$owner:$group" "$target_root"
  copy_state_database "$source_root/irrigation.db" "$target_root/irrigation.db" "$owner" "$group" || return 1
  copy_state_database "$source_root/jobs.db" "$target_root/jobs.db" "$owner" "$group" || return 1
  copy_state_file "$source_root/.secret_key" "$target_root/.secret_key" "$owner" "$group" 0600 || return 1
  copy_state_file "$source_root/.irrig_secret_key" "$target_root/.irrig_secret_key" "$owner" "$group" 0600 || return 1
  for state_dir in backups static/media/maps static/media/zones; do
    if [[ -d "$source_root/$state_dir" || -L "$source_root/$state_dir" ]]; then
      copy_state_directory "$source_root/$state_dir" "$target_root/$state_dir" "$owner" "$group" || return 1
    else
      mkdir -p "$target_root/$state_dir"
      chmod 0700 "$target_root/$state_dir"
      chown "$owner:$group" "$target_root/$state_dir"
    fi
  done
}

prepare_state_layout() {
  local legacy_root=$1
  local state_parent state_stage

  state_parent=$(dirname "$STATE_DIR")
  if [[ -e "$STATE_DIR" || -L "$STATE_DIR" ]]; then
    err "STATE_DIR уже существует; свежая установка требует отсутствующий $STATE_DIR"
    return 1
  fi
  state_stage="$state_parent/.wb-irrigation-state.stage.$$"
  if [[ -e "$state_stage" || -L "$state_stage" ]]; then
    err "State staging path уже существует: $state_stage"
    return 1
  fi
  mkdir -m 0700 "$state_stage"
  if ! sync_mutable_state_tree "$legacy_root" "$state_stage" "$SERVICE_USER" "$SERVICE_GROUP"; then
    rm -rf -- "$state_stage"
    return 1
  fi
  mv "$state_stage" "$STATE_DIR"
  STATE_ACTIVATED=1
}

secure_python_runtime() {
  chown -R root:root "$PYTHON_INSTALL_DIR"
  chmod -R go-w "$PYTHON_INSTALL_DIR"
  chmod -R a+rX "$PYTHON_INSTALL_DIR"
}

secure_code_tree() {
  local repository=$1 tracked_file tracked_mode

  chown -R root:root "$repository"
  find -P "$repository" -type d -exec chmod 0755 {} +
  find -P "$repository" -type f -exec chmod go-rwx {} +
  while IFS= read -r -d '' tracked_file; do
    tracked_mode=$(git -C "$repository" ls-files -s -- "$tracked_file" | awk 'NR == 1 {print $1}')
    if [[ "$tracked_mode" == "100755" ]]; then
      chmod 0755 "$repository/$tracked_file"
    elif [[ ! -L "$repository/$tracked_file" ]]; then
      chmod 0644 "$repository/$tracked_file"
    fi
  done < <(git -C "$repository" ls-files -z)
  chmod -R go-rwx "$repository/.git"
  chmod -R go-w "$repository/venv"
  chmod -R a+rX "$repository/venv"
}

normalize_state_permissions() {
  chown -R "$SERVICE_USER:$SERVICE_GROUP" "$STATE_DIR"
  chmod 0700 "$STATE_DIR"
  find -P "$STATE_DIR" -type d -exec chmod 0700 {} +
  find -P "$STATE_DIR" -type f -exec chmod 0600 {} +
}

verify_service_runtime_access() {
  local tls_path

  runuser -u "$SERVICE_USER" -- "$VENV_DIR/bin/python" -V >/dev/null
  runuser -u "$SERVICE_USER" -- test -r "$APP_DIR/run.py"
  runuser -u "$SERVICE_USER" -- test -r "$RELEASE_ENV_FILE"
  runuser -u "$SERVICE_USER" -- test -w "$STATE_DIR"
  runuser -u "$SERVICE_USER" -- test -w "$STATE_DIR/static/media/maps"
  runuser -u "$SERVICE_USER" -- test -w "$STATE_DIR/static/media/zones"
  if runuser -u "$SERVICE_USER" -- test -w "$APP_DIR" \
    || runuser -u "$SERVICE_USER" -- test -w "$VENV_DIR" \
    || runuser -u "$SERVICE_USER" -- test -w "$PYTHON_INSTALL_DIR"; then
    err "$SERVICE_USER не должна иметь write-доступ к code, venv или shared Python"
    return 1
  fi
  for tls_path in \
    "$(configured_env_value WB_HTTP_TLS_CERTFILE "$ENV_FILE" || true)" \
    "$(configured_env_value WB_HTTP_TLS_KEYFILE "$ENV_FILE" || true)"; do
    if [[ -n "$tls_path" ]]; then
      runuser -u "$SERVICE_USER" -- test -r "$tls_path"
    fi
  done
}

redirect_existing_install_to_updater() {
  local existing_repo=""
  local existing_updater=""
  local resolved_target=""
  local updater_status=0
  local legacy_layout_migrated=0
  local service_was_active=0
  local database_name
  local check_result

  if [[ -e "$APP_LINK" || -L "$APP_LINK" ]]; then
    existing_repo=$(readlink -f "$APP_LINK" 2>/dev/null || true)
    if [[ -z "$existing_repo" || ! -d "$existing_repo/.git" ]]; then
      err "Существующий APP_LINK не указывает на Git-репозиторий; исправьте partial install вручную"
      return 2
    fi
  elif [[ -d "$DATA_DIR/.git" ]]; then
    err "Найдено data-дерево без production APP_LINK; исправьте partial install вручную"
    return 2
  elif [[ -e "$SERVICE_FILE" ]]; then
    err "Найден production unit без приложения; исправьте partial install вручную"
    return 2
  else
    return 10
  fi
  if ! fetch_authorization_branch "$existing_repo"; then
    err "Не удалось получить ветку авторизации для существующей установки"
    return 2
  fi
  if ! resolved_target=$(resolve_authorized_target_commit "$existing_repo" "$TARGET_COMMIT"); then
    return 2
  fi
  if ! existing_updater=$(mktemp /tmp/wb-irrigation-update.XXXXXX); then
    err "Не удалось создать staging-файл updater"
    return 2
  fi
  if ! git -C "$existing_repo" show "${resolved_target}:update_server.sh" >"$existing_updater"; then
    rm -f -- "$existing_updater"
    err "Target commit не содержит update_server.sh"
    return 2
  fi
  if ! chmod 0700 "$existing_updater"; then
    rm -f -- "$existing_updater" || true
    err "Не удалось защитить staging-файл updater"
    return 2
  fi
  if [[ ! -s "$existing_updater" ]] \
    || ! grep -Fq "rollback_update()" "$existing_updater" \
    || ! grep -Fq "trap finish_update EXIT" "$existing_updater"; then
    rm -f -- "$existing_updater"
    err "Повторная установка требует transactional updater в $existing_updater"
    return 2
  fi

  if [[ "$existing_repo" != "$FIXED_DATA_DIR" ]]; then
    if [[ -e "$DATA_DIR" || -L "$DATA_DIR" ]]; then
      rm -f -- "$existing_updater"
      err "Cannot migrate legacy code while $DATA_DIR already exists"
      return 2
    fi
    if ! command -v sqlite3 >/dev/null 2>&1; then
      rm -f -- "$existing_updater"
      err "sqlite3 is required for verified legacy code migration"
      return 2
    fi
    if systemctl is-active "$SERVICE_NAME" >/dev/null 2>&1; then
      service_was_active=1
      systemctl stop "$SERVICE_NAME"
      if systemctl is-active "$SERVICE_NAME" >/dev/null 2>&1; then
        rm -f -- "$existing_updater"
        err "Service did not stop before legacy code migration"
        return 2
      fi
    fi
    reset_legacy_migration_tracking
    if ! migrate_legacy_app_tree; then
      if ! rollback_legacy_app_tree_migration \
        || { [[ "$service_was_active" == "1" ]] && ! systemctl start "$SERVICE_NAME"; }; then
        rm -f -- "$existing_updater"
        err "Legacy code migration failed and automatic recovery was incomplete"
        return 2
      fi
      rm -f -- "$existing_updater"
      err "Legacy code migration failed; original layout retained"
      return 2
    fi
    existing_repo=$(readlink -f "$APP_LINK" 2>/dev/null || true)
    if [[ "$existing_repo" != "$FIXED_DATA_DIR" || ! -d "$existing_repo/.git" ]]; then
      if ! rollback_legacy_app_tree_migration \
        || { [[ "$service_was_active" == "1" ]] && ! systemctl start "$SERVICE_NAME"; }; then
        rm -f -- "$existing_updater"
        err "Canonical layout verification failed and automatic recovery was incomplete"
        return 2
      fi
      rm -f -- "$existing_updater"
      err "Legacy code migration did not publish the canonical data layout"
      return 2
    fi
    for database_name in irrigation.db jobs.db; do
      if [[ -f "$existing_repo/$database_name" ]]; then
        check_result=$(sqlite3 "$existing_repo/$database_name" "PRAGMA quick_check;")
        if [[ "$check_result" != "ok" ]]; then
          if ! rollback_legacy_app_tree_migration \
            || { [[ "$service_was_active" == "1" ]] && ! systemctl start "$SERVICE_NAME"; }; then
            rm -f -- "$existing_updater"
            err "Database verification failed and automatic recovery was incomplete"
            return 2
          fi
          rm -f -- "$existing_updater"
          err "Migrated $database_name failed quick_check; original layout restored"
          return 2
        fi
      fi
    done
    legacy_layout_migrated=1
  fi

  info "Существующая установка обнаружена; передаю commit transactional updater"
  TARGET_COMMIT="$TARGET_COMMIT" \
    BRANCH="$BRANCH" \
    REPO_DIR="$FIXED_APP_LINK" \
    SERVICE="$FIXED_SERVICE_NAME" \
    ENV_FILE="$FIXED_ENV_FILE" \
    DEPLOY_LOCK_FILE="$FIXED_DEPLOY_LOCK_FILE" \
    WB_IRRIGATION_DEPLOY_LOCK_FD=9 \
    WB_IRRIGATION_HANDOFF_SERVICE_WAS_ACTIVE="$service_was_active" \
    WB_IRRIGATION_DEFER_ROLLBACK_RESTART="$service_was_active" \
    bash "$existing_updater" --yes --branch "$BRANCH" --commit "$TARGET_COMMIT" \
    || updater_status=$?
  if [[ "$legacy_layout_migrated" == "1" ]]; then
    if [[ "$updater_status" -eq 0 ]]; then
      if ! finalize_legacy_app_tree_migration; then
        err "Update succeeded, but legacy code backup cleanup failed"
        updater_status=2
      fi
    else
      if ! rollback_legacy_app_tree_migration; then
        err "Updater failed and the legacy application layout could not be restored"
        updater_status=2
      elif [[ "$service_was_active" == "1" ]] && ! systemctl start "$SERVICE_NAME"; then
        err "Legacy layout was restored, but the previous service did not restart"
        updater_status=2
      fi
    fi
  fi
  if ! rm -f -- "$existing_updater"; then
    err "Не удалось удалить staging-файл updater: $existing_updater"
    [[ "$updater_status" -ne 0 ]] && return "$updater_status"
    return 2
  fi
  return "$updater_status"
}

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
  if [[ ! "$port" =~ ^[0-9]+$ ]] || ((port < 1024 || port > 65535)); then
    err "Некорректный unprivileged PORT (требуется 1024-65535): ${port:-<пусто>}"
    return 1
  fi
  printf '%s\n' "$port"
}

read_env_setting() {
  local key=$1
  local env_file=$2
  local value

  [[ -f "$env_file" ]] || return 1
  grep -q "^[[:space:]]*${key}[[:space:]]*=" "$env_file" || return 1
  value=$(sed -n "s/^[[:space:]]*${key}[[:space:]]*=[[:space:]]*//p" "$env_file" | tail -n 1)
  value=${value%$'\r'}
  value="${value#"${value%%[![:space:]]*}"}"
  value="${value%"${value##*[![:space:]]}"}"
  if [[ ${#value} -ge 2 ]] \
    && { [[ "${value:0:1}" == "'" && "${value: -1}" == "'" ]] \
      || [[ "${value:0:1}" == '"' && "${value: -1}" == '"' ]]; }; then
    value=${value:1:${#value}-2}
  fi
  printf '%s' "$value"
}

configured_env_value() {
  local key=$1
  shift
  local env_file
  local value

  if [[ ${!key+x} ]]; then
    printf '%s' "${!key}"
    return 0
  fi
  for env_file in "$@"; do
    if [[ -n "$env_file" ]] && value=$(read_env_setting "$key" "$env_file"); then
      printf '%s' "$value"
      return 0
    fi
  done
  return 1
}

configured_http_scheme() {
  local environment_file=$1
  local repository_file=${2:-}
  local cert_file=""
  local key_file=""

  cert_file=$(configured_env_value WB_HTTP_TLS_CERTFILE "$environment_file" "$repository_file" || true)
  key_file=$(configured_env_value WB_HTTP_TLS_KEYFILE "$environment_file" "$repository_file" || true)
  if [[ -n "$cert_file" && -n "$key_file" ]]; then
    printf 'https\n'
  elif [[ -z "$cert_file" && -z "$key_file" ]]; then
    printf 'http\n'
  else
    err "WB_HTTP_TLS_CERTFILE и WB_HTTP_TLS_KEYFILE должны задаваться вместе"
    return 1
  fi
}

validate_http_host_value() {
  local host_value=$1

  if [[ -z "$host_value" || "$host_value" =~ [^A-Za-z0-9.:-] || "$host_value" == -* ]]; then
    err "Некорректное значение HTTP host: ${host_value:-<пусто>}"
    return 1
  fi
}

configured_http_bind_host() {
  local environment_file=$1
  local repository_file=${2:-}
  local bind_host

  bind_host=$(configured_env_value WB_HTTP_BIND_HOST "$environment_file" "$repository_file" || printf '127.0.0.1')
  validate_http_host_value "$bind_host" || return 1
  printf '%s\n' "$bind_host"
}

configured_http_probe_host() {
  local environment_file=$1
  local repository_file=${2:-}
  local bind_host=$3
  local probe_host

  probe_host=$(configured_env_value WB_HTTP_PROBE_HOST "$environment_file" "$repository_file" || true)
  if [[ -n "$probe_host" ]]; then
    validate_http_host_value "$probe_host" || return 1
    if [[ "$probe_host" == "0.0.0.0" || "$probe_host" == "::" ]]; then
      err "WB_HTTP_PROBE_HOST не может быть wildcard-адресом"
      return 1
    fi
    printf '%s\n' "$probe_host"
    return 0
  fi
  case "$bind_host" in
    0.0.0.0) printf '127.0.0.1\n' ;;
    ::) printf '::1\n' ;;
    *) printf '%s\n' "$bind_host" ;;
  esac
}

configure_ready_probe() {
  local environment_file=$1
  local repository_file=${2:-}
  local ca_file
  local insecure_probe
  local url_host

  APP_BIND_HOST=$(configured_http_bind_host "$environment_file" "$repository_file") || return 1
  APP_PROBE_HOST=$(configured_http_probe_host "$environment_file" "$repository_file" "$APP_BIND_HOST") \
    || return 1
  APP_SCHEME=$(configured_http_scheme "$environment_file" "$repository_file") || return 1
  url_host=$APP_PROBE_HOST
  [[ "$url_host" == *:* ]] && url_host="[$url_host]"
  APP_READY_URL="${APP_SCHEME}://${url_host}:${APP_PORT}/readyz"
  READY_CURL_ARGS=(-fsS --max-time 3)
  if [[ "$APP_SCHEME" == "https" ]]; then
    ca_file=$(configured_env_value WB_HTTP_PROBE_CA_FILE "$environment_file" "$repository_file" || true)
    insecure_probe=$(configured_env_value WB_HTTP_PROBE_INSECURE_TLS "$environment_file" "$repository_file" || true)
    if [[ -n "$ca_file" ]]; then
      READY_CURL_ARGS+=(--cacert "$ca_file")
    elif [[ "$insecure_probe" == "1" ]]; then
      warn "WB_HTTP_PROBE_INSECURE_TLS=1 отключает TLS verification только для readiness probe"
      READY_CURL_ARGS+=(--insecure)
    fi
  fi
}

probe_ready_endpoint() {
  local http_status

  if ! http_status=$(curl "${READY_CURL_ARGS[@]}" \
    --output /dev/null --write-out '%{http_code}' "$APP_READY_URL"); then
    return 1
  fi
  [[ "$http_status" == "200" ]]
}

validate_http_transport_contract() {
  local environment_file=$1
  local repository_file=${2:-}
  local bind_host
  local allow_insecure
  local ca_file
  local insecure_probe
  local scheme

  bind_host=$(configured_http_bind_host "$environment_file" "$repository_file") || return 1
  configured_http_probe_host "$environment_file" "$repository_file" "$bind_host" >/dev/null || return 1
  allow_insecure=$(
    configured_env_value WB_HTTP_ALLOW_INSECURE_EXTERNAL "$environment_file" "$repository_file" || true
  )
  if ! scheme=$(configured_http_scheme "$environment_file" "$repository_file"); then
    return 1
  fi
  ca_file=$(configured_env_value WB_HTTP_PROBE_CA_FILE "$environment_file" "$repository_file" || true)
  insecure_probe=$(configured_env_value WB_HTTP_PROBE_INSECURE_TLS "$environment_file" "$repository_file" || true)
  if [[ -n "$insecure_probe" && "$insecure_probe" != "0" && "$insecure_probe" != "1" ]]; then
    err "WB_HTTP_PROBE_INSECURE_TLS должен быть 0 или 1"
    return 1
  fi
  if [[ -n "$ca_file" && "$insecure_probe" == "1" ]]; then
    err "WB_HTTP_PROBE_CA_FILE и WB_HTTP_PROBE_INSECURE_TLS=1 взаимоисключающие"
    return 1
  fi
  if [[ -n "$ca_file" && ( -L "$ca_file" || ! -f "$ca_file" || ! -r "$ca_file" ) ]]; then
    err "WB_HTTP_PROBE_CA_FILE должен быть читаемым обычным не-symlink файлом: $ca_file"
    return 1
  fi
  if [[ "$scheme" == "http" && ( -n "$ca_file" || "$insecure_probe" == "1" ) ]]; then
    err "TLS probe options требуют WB_HTTP_TLS_CERTFILE и WB_HTTP_TLS_KEYFILE"
    return 1
  fi
  case "$bind_host" in
    127.0.0.1 | localhost | ::1) ;;
    *)
      if [[ "$scheme" != "https" && "$allow_insecure" != "1" ]]; then
        err "Внешний WB_HTTP_BIND_HOST=$bind_host требует native TLS или WB_HTTP_ALLOW_INSECURE_EXTERNAL=1"
        return 1
      fi
      ;;
  esac
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

ensure_env_default() {
  local key=$1
  local value=$2
  local env_file=$3

  if [[ ! -f "$env_file" ]] || ! grep -q "^${key}=" "$env_file"; then
    upsert_env_value "$key" "$value" "$env_file"
  fi
}

merge_legacy_env_defaults() {
  local source_file=$1 target_file=$2
  local target_parent staged_file line key

  [[ -e "$source_file" || -L "$source_file" ]] || return 0
  if [[ -L "$source_file" || ! -f "$source_file" ]]; then
    err "Legacy environment source не является regular file: $source_file"
    return 1
  fi
  target_parent=$(dirname -- "$target_file")
  if [[ -L "$target_parent" || ! -d "$target_parent" ]]; then
    err "Environment destination parent небезопасен: $target_parent"
    return 1
  fi
  if [[ -e "$target_file" || -L "$target_file" ]]; then
    if [[ -L "$target_file" || ! -f "$target_file" ]]; then
      err "Environment destination небезопасен: $target_file"
      return 1
    fi
  fi
  if ! staged_file=$(mktemp "$target_parent/.wb-irrigation-env.XXXXXX"); then
    err "Не удалось создать environment migration staging file"
    return 1
  fi
  if [[ -f "$target_file" ]] && ! cp -a -- "$target_file" "$staged_file"; then
    rm -f -- "$staged_file"
    err "Не удалось stage operator environment"
    return 1
  fi
  while IFS= read -r line || [[ -n "$line" ]]; do
    if [[ "$line" =~ ^[[:space:]]*$ || "$line" =~ ^[[:space:]]*# ]]; then
      continue
    fi
    if [[ "$line" =~ ^[[:space:]]*(export[[:space:]]+)?([A-Za-z_][A-Za-z0-9_]*)[[:space:]]*= ]]; then
      key=${BASH_REMATCH[2]}
    else
      rm -f -- "$staged_file"
      err "Legacy environment содержит unsupported line"
      return 1
    fi
    if ! grep -Eq "^[[:space:]]*(export[[:space:]]+)?${key}[[:space:]]*=" "$staged_file"; then
      if ! printf '%s\n' "$line" >>"$staged_file"; then
        rm -f -- "$staged_file"
        err "Не удалось merge legacy environment key: $key"
        return 1
      fi
    fi
  done <"$source_file"
  if ! chmod 0640 "$staged_file" \
    || ! chown root:"$SERVICE_GROUP" "$staged_file" \
    || ! mv -f -- "$staged_file" "$target_file"; then
    rm -f -- "$staged_file"
    err "Не удалось publish merged root-owned environment"
    return 1
  fi
  if ! rm -f -- "$source_file"; then
    err "Не удалось retire legacy environment source: $source_file"
    return 1
  fi
}

publish_release_environment() {
  local commit_sha=$1
  local release_parent staged_release

  require_full_commit_sha "$commit_sha" || return 1
  release_parent=$(dirname -- "$RELEASE_ENV_FILE")
  if [[ -L "$release_parent" || ! -d "$release_parent" ]]; then
    err "Release environment parent небезопасен: $release_parent"
    return 1
  fi
  if [[ -e "$RELEASE_ENV_FILE" || -L "$RELEASE_ENV_FILE" ]]; then
    if [[ -L "$RELEASE_ENV_FILE" || ! -f "$RELEASE_ENV_FILE" ]]; then
      err "Release environment target небезопасен: $RELEASE_ENV_FILE"
      return 1
    fi
  fi
  if ! staged_release=$(mktemp "$release_parent/.wb-irrigation-release.XXXXXX"); then
    err "Не удалось создать release environment staging file"
    return 1
  fi
  if ! printf 'WB_APP_VERSION=%s\nGIT_COMMIT=%s\n' "$commit_sha" "$commit_sha" >"$staged_release" \
    || ! chmod 0640 "$staged_release" \
    || ! chown root:"$SERVICE_GROUP" "$staged_release" \
    || ! mv -f -- "$staged_release" "$RELEASE_ENV_FILE"; then
    rm -f -- "$staged_release"
    err "Не удалось atomically publish release environment"
    return 1
  fi
}

persist_http_environment() {
  local env_file=$1
  local key

  for key in \
    WB_HTTP_BIND_HOST \
    WB_HTTP_TLS_CERTFILE \
    WB_HTTP_TLS_KEYFILE \
    WB_HTTP_PROBE_CA_FILE \
    WB_HTTP_PROBE_HOST \
    WB_HTTP_PROBE_INSECURE_TLS \
    WB_HTTP_ALLOW_INSECURE_EXTERNAL; do
    if [[ ${!key+x} ]]; then
      upsert_env_value "$key" "${!key}" "$env_file"
    fi
  done
  ensure_env_default WB_HTTP_BIND_HOST 127.0.0.1 "$env_file"
}

ensure_deploy_control_dir() {
  local control_parent
  local expected_uid=0

  control_parent=$(dirname -- "$DEPLOY_CONTROL_DIR")
  if [[ "$DEPLOY_CONTROL_DIR" != "$FIXED_DEPLOY_CONTROL_DIR" ]]; then
    expected_uid=$(id -u)
  fi
  if [[ -L "$control_parent" || ! -d "$control_parent" \
    || "$(stat -c %u "$control_parent")" != "$expected_uid" ]]; then
    err "Deploy control parent не является trusted real directory: $control_parent"
    return 1
  fi
  if [[ ! -e "$DEPLOY_CONTROL_DIR" ]]; then
    if ! install -d -m 0700 "$DEPLOY_CONTROL_DIR"; then
      err "Не удалось создать deploy control directory"
      return 1
    fi
  fi
  if [[ -L "$DEPLOY_CONTROL_DIR" || ! -d "$DEPLOY_CONTROL_DIR" \
    || "$(stat -c %u "$DEPLOY_CONTROL_DIR")" != "$expected_uid" ]]; then
    err "Deploy control directory небезопасен: $DEPLOY_CONTROL_DIR"
    return 1
  fi
  if ! chmod 0700 "$DEPLOY_CONTROL_DIR"; then
    err "Не удалось защитить deploy control directory"
    return 1
  fi
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

require_retired_telegram_runtime_commit() {
  local repository=$1
  local target_commit=$2
  local services_entry
  local services_mode services_type services_object services_path
  local runtime_entry
  local runtime_mode runtime_type runtime_object runtime_path

  if ! services_entry=$(git -C "$repository" ls-tree "$target_commit" -- services); then
    err "Не удалось проверить services в target commit $target_commit"
    return 1
  fi
  read -r services_mode services_type services_object services_path <<<"$services_entry"
  if [[ "$services_mode" != "040000" || "$services_type" != "tree" || "$services_path" != "services" ]]; then
    err "Target commit не содержит обычное отслеживаемое дерево services"
    return 1
  fi

  if ! runtime_entry=$(git -C "$repository" ls-tree "$target_commit" -- services/telegram_bot.py); then
    err "Не удалось проверить Telegram runtime в target commit $target_commit"
    return 1
  fi
  read -r runtime_mode runtime_type runtime_object runtime_path <<<"$runtime_entry"
  if [[ "$runtime_type" != "blob" || "$runtime_path" != "services/telegram_bot.py" \
    || ("$runtime_mode" != "100644" && "$runtime_mode" != "100755") ]]; then
    err "Target commit не содержит обычный отслеживаемый Telegram runtime"
    return 1
  fi
  if [[ "$runtime_object" != "$TELEGRAM_LOG_RETIREMENT_BLOB" ]]; then
    err "Telegram runtime не выполняет версионированный контракт отказа от файлового лога"
    return 1
  fi
}

require_retired_telegram_runtime_tree() {
  local application_root=$1
  local application_root_real
  local repository_root
  local repository_root_real
  local target_commit
  local services_path
  local services_path_real
  local telegram_source
  local telegram_source_real
  local working_blob

  application_root_real=$(cd "$application_root" && pwd -P) || return 1
  if ! repository_root=$(git -C "$application_root_real" rev-parse --show-toplevel); then
    err "Не удалось найти Git root для $application_root_real"
    return 1
  fi
  repository_root_real=$(cd "$repository_root" && pwd -P) || return 1
  if [[ "$repository_root_real" != "$application_root_real" ]]; then
    err "Telegram runtime находится вне канонического корня приложения"
    return 1
  fi
  target_commit=$(git -C "$application_root_real" rev-parse --verify HEAD)
  require_retired_telegram_runtime_commit "$application_root_real" "$target_commit"

  services_path="$application_root_real/services"
  telegram_source="$services_path/telegram_bot.py"
  if [[ -L "$services_path" || ! -d "$services_path" || -L "$telegram_source" || ! -f "$telegram_source" ]]; then
    err "Telegram runtime или компонент его пути не является обычным файлом: $telegram_source"
    return 1
  fi
  services_path_real=$(cd "$services_path" && pwd -P) || return 1
  telegram_source_real=$(readlink -f "$telegram_source") || return 1
  if [[ "$services_path_real" != "$services_path" || "$telegram_source_real" != "$telegram_source" ]]; then
    err "Telegram runtime выходит за пределы канонического дерева приложения"
    return 1
  fi
  working_blob=$(git -C "$application_root_real" hash-object -- "$telegram_source")
  if [[ "$working_blob" != "$TELEGRAM_LOG_RETIREMENT_BLOB" ]]; then
    err "Рабочий Telegram runtime отличается от проверенного versioned blob"
    return 1
  fi
}

retire_telegram_file_logs() {
  local application_root=$1
  local application_root_real
  local expected_logs_dir
  local logs_dir_real
  local listing_file
  local enumeration_sentinel
  local enumeration_complete=0
  local iteration_failed=0
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

  if ! listing_file=$(mktemp /tmp/wb-irrigation-telegram-logs.XXXXXX); then
    err "Не удалось создать приватный файл перечисления Telegram-логов"
    return 1
  fi
  if ! chmod 600 "$listing_file"; then
    rm -f -- "$listing_file" || err "Не удалось удалить небезопасное перечисление: $listing_file"
    err "Не удалось закрыть права на файл перечисления Telegram-логов"
    return 1
  fi
  enumeration_sentinel="WB_IRRIGATION_ENUMERATION_COMPLETE_${listing_file##*/}"
  if ! find -P "$logs_dir_real" -mindepth 1 -maxdepth 1 -print0 >"$listing_file"; then
    rm -f -- "$listing_file" || err "Не удалось удалить файл сбойного перечисления: $listing_file"
    err "Не удалось перечислить устаревшие файловые Telegram-логи"
    return 1
  fi
  if ! printf '%s\0' "$enumeration_sentinel" >>"$listing_file"; then
    rm -f -- "$listing_file" || err "Не удалось удалить незавершённое перечисление: $listing_file"
    err "Не удалось завершить перечисление устаревших Telegram-логов"
    return 1
  fi

  if ! while IFS= read -r -d '' candidate; do
    if [[ "$candidate" == "$enumeration_sentinel" ]]; then
      enumeration_complete=1
      continue
    fi
    candidate_name=${candidate##*/}
    if [[ "$candidate_name" == "telegram.txt" \
      || "$candidate_name" =~ ^telegram[.]txt([.][0-9]+|-[0-9]{8})([.]gz)?$ ]]; then
      if [[ -f "$candidate" || -L "$candidate" ]]; then
        if ! rm -f -- "$candidate"; then
          err "Не удалось удалить устаревший Telegram-лог: $candidate"
          iteration_failed=1
          break
        fi
        info "Удалён устаревший файловый Telegram-лог: $candidate"
      elif [[ -e "$candidate" ]]; then
        err "Отказываюсь удалять не-файл по пути исторического лога: $candidate"
        iteration_failed=1
        break
      fi
    fi
  done <"$listing_file"; then
    iteration_failed=1
  fi

  if ! rm -f -- "$listing_file"; then
    err "Не удалось удалить файл перечисления Telegram-логов: $listing_file"
    return 1
  fi
  if [[ "$iteration_failed" == "1" || "$enumeration_complete" != "1" ]]; then
    err "Перечисление устаревших Telegram-логов обработано не полностью"
    return 1
  fi
}

atomic_switch_app_link() {
  local legacy_backup=${1:-}
  local link_stage="${APP_LINK}.new-$$"

  if [[ -e "$link_stage" || -L "$link_stage" ]]; then
    err "Временный симлинк уже существует: $link_stage"
    return 1
  fi
  if ! ln -s "$DATA_DIR" "$link_stage"; then
    return 1
  fi

  if [[ -n "$legacy_backup" ]]; then
    if ! mv "$APP_LINK" "$legacy_backup"; then
      rm -f -- "$link_stage"
      return 1
    fi
  fi
  if ! mv "$link_stage" "$APP_LINK"; then
    rm -f -- "$link_stage"
    if [[ -n "$legacy_backup" && ( -e "$legacy_backup" || -L "$legacy_backup" ) \
      && ! -e "$APP_LINK" && ! -L "$APP_LINK" ]]; then
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
  mkdir -p "$(dirname "$APP_LINK")" "$(dirname "$DATA_DIR")" || return 1
  desired=$(readlink -f "$DATA_DIR" 2>/dev/null || printf '%s' "$DATA_DIR")

  if [[ -L "$APP_LINK" ]]; then
    LEGACY_MIGRATION_ORIGINAL_LINK=$(readlink "$APP_LINK")
    current=$(readlink -f "$APP_LINK" 2>/dev/null || true)
    if [[ "$current" == "$desired" ]]; then
      return 0
    fi
    # A broken/non-repository link is replaced only after DATA_DIR is cloned.
    [[ -n "$current" && -d "$current/.git" ]] || return 0
    legacy_backup="${APP_LINK}.legacy-link-$(date +%Y%m%d_%H%M%S)-$$"
    LEGACY_MIGRATION_APP_BACKUP=$legacy_backup
  elif [[ -d "$APP_LINK" ]]; then
    current=$APP_LINK
    if [[ ! -d "$current/.git" ]]; then
      err "Legacy application tree is not a Git repository: $current"
      return 1
    fi
    legacy_backup="${APP_LINK}.legacy-$(date +%Y%m%d_%H%M%S)-$$"
    LEGACY_MIGRATION_APP_BACKUP=$legacy_backup
  elif [[ -e "$APP_LINK" ]]; then
    err "$APP_LINK существует и не является каталогом или симлинком"
    return 1
  else
    return 0
  fi

  migration_stage="${DATA_DIR}.migration-$(date +%Y%m%d_%H%M%S)-$$"
  mkdir -m 700 "$migration_stage" || return 1
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
  LEGACY_MIGRATION_DATA_DIR=$DATA_DIR
  LEGACY_MIGRATION_DATA_PUBLISHED=1

  if ! atomic_switch_app_link "$legacy_backup"; then
    err "Не удалось атомарно переключить APP_LINK; старый путь сохранён"
    return 1
  fi
  LEGACY_MIGRATION_COMPLETE=1
}

reset_legacy_migration_tracking() {
  LEGACY_MIGRATION_ORIGINAL_LINK=""
  LEGACY_MIGRATION_APP_BACKUP=""
  LEGACY_MIGRATION_DATA_DIR=""
  LEGACY_MIGRATION_DATA_PUBLISHED=0
  LEGACY_MIGRATION_COMPLETE=0
}

rollback_legacy_app_tree_migration() {
  local current_target=""

  if [[ "$LEGACY_MIGRATION_DATA_PUBLISHED" != "1" ]]; then
    return 0
  fi
  if [[ -L "$APP_LINK" ]]; then
    current_target=$(readlink -f "$APP_LINK" 2>/dev/null || true)
    if [[ "$current_target" == "$LEGACY_MIGRATION_DATA_DIR" ]]; then
      rm -f -- "$APP_LINK" || return 1
    fi
  fi
  if [[ -e "$DATA_DIR" || -L "$DATA_DIR" ]]; then
    if [[ -L "$DATA_DIR" || ! -d "$DATA_DIR" || "$DATA_DIR" != "$LEGACY_MIGRATION_DATA_DIR" \
      || "$DATA_DIR" != /* || "$DATA_DIR" == "/" ]]; then
      err "Refusing unsafe legacy migration rollback target: $DATA_DIR"
      return 1
    fi
    rm -rf -- "$DATA_DIR" || return 1
  fi
  if [[ -n "$LEGACY_MIGRATION_APP_BACKUP" \
    && ( -e "$LEGACY_MIGRATION_APP_BACKUP" || -L "$LEGACY_MIGRATION_APP_BACKUP" ) ]]; then
    if [[ -e "$APP_LINK" || -L "$APP_LINK" ]]; then
      err "Cannot restore legacy application path because it already exists"
      return 1
    fi
    mv "$LEGACY_MIGRATION_APP_BACKUP" "$APP_LINK" || return 1
  elif [[ -n "$LEGACY_MIGRATION_ORIGINAL_LINK" && ! -e "$APP_LINK" && ! -L "$APP_LINK" ]]; then
    ln -s "$LEGACY_MIGRATION_ORIGINAL_LINK" "$APP_LINK" || return 1
  fi
  LEGACY_MIGRATION_DATA_PUBLISHED=0
  LEGACY_MIGRATION_COMPLETE=0
}

finalize_legacy_app_tree_migration() {
  if [[ -n "$LEGACY_MIGRATION_APP_BACKUP" \
    && ( -e "$LEGACY_MIGRATION_APP_BACKUP" || -L "$LEGACY_MIGRATION_APP_BACKUP" ) ]]; then
    rm -rf -- "$LEGACY_MIGRATION_APP_BACKUP" || return 1
  fi
  reset_legacy_migration_tracking
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
validate_fixed_deployment_contract
require_full_commit_sha "$TARGET_COMMIT"
validate_branch_name
acquire_deploy_lock

if redirect_existing_install_to_updater; then
  exit 0
else
  handoff_status=$?
  if [[ "$handoff_status" -ne 10 ]]; then
    exit "$handoff_status"
  fi
fi

if [[ ! -d /mnt/data ]]; then
  err "/mnt/data отсутствует; production layout Wirenboard недоступен"
  exit 1
fi
if [[ -e "$DATA_DIR" || -L "$DATA_DIR" || -e "$STATE_DIR" || -L "$STATE_DIR" \
  || -e "$DEPLOY_CONTROL_DIR" || -L "$DEPLOY_CONTROL_DIR" \
  || -e "$APP_LINK" || -L "$APP_LINK" || -e "$SERVICE_FILE" ]]; then
  err "Обнаружена неполная свежая установка; автоматический bootstrap не будет перезаписывать её"
  exit 1
fi

ARCH=$(uname -m)
case "$ARCH" in
  aarch64) ;;
  *)
    err "Production bootstrap поддерживает только aarch64, получено: $ARCH"
    exit 1
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
  ca-certificates curl git build-essential libssl-dev \
  sqlite3 mosquitto logrotate
ok "Системные пакеты установлены"
ensure_service_account

# -----------------------------------------------------------------------------
# Step 2: uv + Python 3.11
# -----------------------------------------------------------------------------
info "Шаг 2/6 — установка uv и Python ${PY_VERSION}"

install_pinned_uv
export PATH="$(dirname "$UV_BIN"):$PATH"
export UV_PYTHON_INSTALL_DIR="$PYTHON_INSTALL_DIR"

info "uv version: $($UV_BIN --version)"

# uv python install — идемпотентно: если уже стоит, не качает заново.
"$UV_BIN" python install "${PY_VERSION}"
PY_BIN=$("$UV_BIN" python find "${PY_VERSION}")
if [[ ! -x "$PY_BIN" || "$PY_BIN" != "$PYTHON_INSTALL_DIR"/* ]]; then
  err "uv не вернул путь к Python ${PY_VERSION}"
  exit 1
fi
secure_python_runtime
ok "Python ${PY_VERSION}: $PY_BIN"

# -----------------------------------------------------------------------------
# Step 3: клонирование репо в /mnt/data + симлинк
# -----------------------------------------------------------------------------
info "Шаг 3/6 — клонирование репо"

INSTALL_TARGET_COMMIT=""
INSTALL_SUCCEEDED=0
FRESH_DATA_CREATED=0
STATE_ACTIVATED=0
DEPLOY_CONTROL_CREATED=0
APP_LINK_CREATED=0
UNIT_INSTALLED=0
ENV_CHANGED=0
HAD_ENV_FILE=0
ENV_BACKUP=""
RELEASE_ENV_CHANGED=0
HAD_RELEASE_ENV_FILE=0
RELEASE_ENV_BACKUP=""
LOGROTATE_CHANGED=0
HAD_LOGROTATE_FILE=0
LOGROTATE_BACKUP=""

# На повторной установке не меняем живое дерево под работающим процессом.
# Если дальнейший шаг упадёт, EXIT-trap вернёт ранее активный сервис.
SERVICE_WAS_ACTIVE=0
SERVICE_RESTORED=0
restore_service_on_failure() {
  local status=$?
  trap - EXIT
  if [[ "$status" -ne 0 && "$INSTALL_SUCCEEDED" == "0" ]]; then
    warn "Свежая установка не завершилась; удаляю только созданный staging"
    systemctl stop "$SERVICE_NAME" >/dev/null 2>&1 || true
    if [[ "$UNIT_INSTALLED" == "1" ]]; then
      systemctl disable "$SERVICE_NAME" >/dev/null 2>&1 || true
      rm -f -- "$SERVICE_FILE"
      systemctl daemon-reload || true
    fi
    if [[ "$LOGROTATE_CHANGED" == "1" ]]; then
      if [[ "$HAD_LOGROTATE_FILE" == "1" && -f "$LOGROTATE_BACKUP" ]]; then
        cp -a "$LOGROTATE_BACKUP" "$LOGROTATE_TARGET"
      else
        rm -f -- "$LOGROTATE_TARGET"
      fi
    fi
    if [[ "$STATE_ACTIVATED" == "1" && -d "$STATE_DIR" && ! -L "$STATE_DIR" ]]; then
      rm -rf -- "$STATE_DIR"
    fi
    if [[ "$DEPLOY_CONTROL_CREATED" == "1" \
      && -d "$DEPLOY_CONTROL_DIR" && ! -L "$DEPLOY_CONTROL_DIR" ]]; then
      rm -rf -- "$DEPLOY_CONTROL_DIR"
    fi
    cleanup_created_mqtt_tls_material || true
    if [[ "$APP_LINK_CREATED" == "1" && -L "$APP_LINK" ]]; then
      rm -f -- "$APP_LINK"
    fi
    if [[ "$FRESH_DATA_CREATED" == "1" && -d "$DATA_DIR" && ! -L "$DATA_DIR" ]]; then
      rm -rf -- "$DATA_DIR"
    fi
    if [[ "$ENV_CHANGED" == "1" ]]; then
      if [[ "$HAD_ENV_FILE" == "1" && -f "$ENV_BACKUP" ]]; then
        cp -a "$ENV_BACKUP" "$ENV_FILE"
      else
        rm -f -- "$ENV_FILE"
      fi
    fi
    if [[ "$RELEASE_ENV_CHANGED" == "1" ]]; then
      if [[ "$HAD_RELEASE_ENV_FILE" == "1" && -f "$RELEASE_ENV_BACKUP" ]]; then
        cp -a "$RELEASE_ENV_BACKUP" "$RELEASE_ENV_FILE"
      else
        rm -f -- "$RELEASE_ENV_FILE"
      fi
    fi
  fi
  [[ -n "$ENV_BACKUP" ]] && rm -f -- "$ENV_BACKUP"
  [[ -n "$RELEASE_ENV_BACKUP" ]] && rm -f -- "$RELEASE_ENV_BACKUP"
  [[ -n "$LOGROTATE_BACKUP" ]] && rm -f -- "$LOGROTATE_BACKUP"
  exit "$status"
}
trap restore_service_on_failure EXIT

if [[ ! -e "$DEPLOY_CONTROL_DIR" ]]; then
  DEPLOY_CONTROL_CREATED=1
fi
ensure_deploy_control_dir

if systemctl is-active "$SERVICE_NAME" >/dev/null 2>&1; then
  SERVICE_WAS_ACTIVE=1
  info "Останавливаю активный $SERVICE_NAME перед обновлением дерева"
  systemctl stop "$SERVICE_NAME"
fi

migrate_legacy_app_tree

mkdir -p "$(dirname "$DATA_DIR")"
info "Клонирую ${REPO_URL} → ${DATA_DIR} без checkout плавающей ветки"
FRESH_DATA_CREATED=1
git clone --no-checkout --branch "$BRANCH" "$REPO_URL" "$DATA_DIR"
fetch_authorization_branch "$DATA_DIR"
INSTALL_TARGET_COMMIT=$(resolve_authorized_target_commit "$DATA_DIR" "$TARGET_COMMIT")
require_retired_telegram_runtime_commit "$DATA_DIR" "$INSTALL_TARGET_COMMIT"
git -C "$DATA_DIR" reset --hard "$INSTALL_TARGET_COMMIT" -q

if [[ "$APP_LINK" == "$DATA_DIR" ]]; then
  APP_DIR=$(readlink -f "$DATA_DIR")
else
  current=$(readlink -f "$APP_LINK" 2>/dev/null || true)
  desired=$(readlink -f "$DATA_DIR")
  if [[ "$current" != "$desired" ]]; then
    atomic_switch_app_link ""
    APP_LINK_CREATED=1
  fi
  APP_DIR=$(readlink -f "$APP_LINK")
fi
# The runtime removal must be integrated before the destructive PII cleanup.
require_retired_telegram_runtime_tree "$APP_DIR"
retire_telegram_file_logs "$APP_DIR"
for secret_file in "$APP_DIR/.secret_key" "$APP_DIR/.irrig_secret_key" "$APP_DIR/.env"; do
  if [[ -f "$secret_file" ]]; then
    chmod 600 "$secret_file"
  fi
done
prepare_state_layout "$APP_DIR"
normalize_state_permissions
migrate_mqtt_tls_paths "$STATE_DIR/irrigation.db" "$APP_DIR"
ok "Код приложения: $APP_DIR"

# -----------------------------------------------------------------------------
# Step 4: venv + зависимости
# -----------------------------------------------------------------------------
info "Шаг 4/6 — venv (Python ${PY_VERSION}) и pip install"

VENV_DIR="${APP_DIR}/venv"
if [[ ! -f "$APP_DIR/requirements.lock" ]]; then
  err "В target commit отсутствует обязательный production dependency lock: $APP_DIR/requirements.lock"
  exit 1
fi
"$UV_BIN" venv --python "$PY_BIN" "$VENV_DIR"
"$UV_BIN" pip install --python "$VENV_DIR/bin/python" --require-hashes \
  -r "$APP_DIR/requirements.lock"
VENV_PY_VERSION=$("$VENV_DIR/bin/python" -c 'import sys; print(".".join(map(str, sys.version_info[:3])))')
if [[ "$VENV_PY_VERSION" != "$PY_VERSION" ]]; then
  err "venv использует Python $VENV_PY_VERSION вместо pinned $PY_VERSION"
  exit 1
fi
mkdir -p "$APP_DIR/static/media/maps" "$APP_DIR/static/media/zones"
secure_code_tree "$APP_DIR"
chown root:root /opt/wb-irrigation
chmod 0755 /opt/wb-irrigation
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
  UNIT_INSTALLED=1
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
if [[ -f "$LOGROTATE_TARGET" ]]; then
  HAD_LOGROTATE_FILE=1
  LOGROTATE_BACKUP=$(mktemp /tmp/wb-irrigation-install-logrotate.XXXXXX)
  cp -a "$LOGROTATE_TARGET" "$LOGROTATE_BACKUP"
fi
LOGROTATE_CHANGED=1
install -m 0644 "$LOGROTATE_SOURCE" "$LOGROTATE_TARGET"
ensure_logrotate_ready
ok "Logrotate-конфиг установлен: $LOGROTATE_TARGET"

CURRENT_COMMIT=$(git -C "$APP_DIR" rev-parse HEAD)
if [[ -f "$ENV_FILE" ]]; then
  HAD_ENV_FILE=1
  ENV_BACKUP=$(mktemp /tmp/wb-irrigation-install-env.XXXXXX)
  cp -a "$ENV_FILE" "$ENV_BACKUP"
fi
if [[ -f "$RELEASE_ENV_FILE" ]]; then
  HAD_RELEASE_ENV_FILE=1
  RELEASE_ENV_BACKUP=$(mktemp /tmp/wb-irrigation-install-release.XXXXXX)
  cp -a "$RELEASE_ENV_FILE" "$RELEASE_ENV_BACKUP"
fi
ENV_CHANGED=1
RELEASE_ENV_CHANGED=1
merge_legacy_env_defaults "$STATE_DIR/.env" "$ENV_FILE"
merge_legacy_env_defaults "$APP_DIR/.env" "$ENV_FILE"
upsert_env_value GIT_COMMIT "$CURRENT_COMMIT" "$ENV_FILE"
publish_release_environment "$CURRENT_COMMIT"
if [[ ${PORT+x} ]]; then
  APP_PORT=$(configured_port "$ENV_FILE")
  upsert_env_value PORT "$APP_PORT" "$ENV_FILE"
fi
persist_http_environment "$ENV_FILE"
chown root:"$SERVICE_GROUP" "$ENV_FILE"
chmod 0640 "$ENV_FILE"
validate_http_transport_contract "$ENV_FILE"
APP_PORT=$(configured_port "$ENV_FILE")
configure_ready_probe "$ENV_FILE"
verify_service_runtime_access

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
  if probe_ready_endpoint; then
    ok "/readyz отвечает 200 (попытка $i)"
    READYZ_OK=1
    break
  fi
  sleep 2
done

if [[ "${READYZ_OK:-0}" != "1" ]]; then
  err "/readyz не отвечает за 30 секунд: $APP_READY_URL"
  err "Диагностика: systemctl status $SERVICE_NAME && journalctl -u $SERVICE_NAME -n 50"
  exit 1
fi

install -m 0600 /dev/null "$DEPLOY_CONTROL_DIR/layout-v1"
INSTALL_SUCCEEDED=1
[[ -n "$ENV_BACKUP" ]] && rm -f -- "$ENV_BACKUP"
ENV_BACKUP=""
[[ -n "$RELEASE_ENV_BACKUP" ]] && rm -f -- "$RELEASE_ENV_BACKUP"
RELEASE_ENV_BACKUP=""
[[ -n "$LOGROTATE_BACKUP" ]] && rm -f -- "$LOGROTATE_BACKUP"
LOGROTATE_BACKUP=""

echo
ok "Установка завершена."
case "$APP_BIND_HOST" in
  127.0.0.1 | localhost | ::1)
    echo "  Web endpoint:   $APP_READY_URL (только локальный bind; LAN-доступ отключён)"
    ;;
  *)
    echo "  Проверенный endpoint: $APP_READY_URL"
    echo "  Внешний bind:         $APP_BIND_HOST (используйте имя/IP из TLS certificate)"
    ;;
esac
echo "  Сервис:        systemctl status $SERVICE_NAME"
echo "  Логи:          journalctl -u $SERVICE_NAME -f"
echo "  Обновление:    bash ${APP_DIR}/update_server.sh --yes --commit <FULL_SHA>"
