#!/usr/bin/env bash

# Supported native updater for Wirenboard.
# A verified backup is followed by a staged runtime activation. Any failure
# restores the previous Git revision, venv and external configuration before
# returning the previously active service to operation.

set -euo pipefail
umask 077

REPO_DIR=${REPO_DIR:-/opt/wb-irrigation/irrigation}
BRANCH=${BRANCH:-main}
TARGET_COMMIT=${TARGET_COMMIT:-}
SERVICE=${SERVICE:-wb-irrigation}
PY_VERSION=${PY_VERSION:-3.11.15}
UV_BIN=${UV_BIN:-/mnt/data/wb-irrigation-tools/uv}
BACKUP_BASE=${BACKUP_BASE:-}
BACKUP_KEEP=${BACKUP_KEEP:-10}
ENV_FILE=${ENV_FILE:-/opt/wb-irrigation/.env}
RELEASE_ENV_FILE=${RELEASE_ENV_FILE:-/opt/wb-irrigation/release.env}
MQTT_TLS_DIR=${MQTT_TLS_DIR:-/opt/wb-irrigation/mqtt-tls}
STATE_DIR=${STATE_DIR:-/mnt/data/wb-irrigation-state}
DEPLOY_CONTROL_DIR=${DEPLOY_CONTROL_DIR:-/mnt/data/wb-irrigation-deploy}
PYTHON_INSTALL_DIR=${PYTHON_INSTALL_DIR:-/mnt/data/wb-irrigation-python}
SERVICE_USER=${SERVICE_USER:-wb-irrigation}
SERVICE_GROUP=${SERVICE_GROUP:-wb-irrigation}
NONINTERACTIVE=${NONINTERACTIVE:-0}
HANDOFF_SERVICE_WAS_ACTIVE=${WB_IRRIGATION_HANDOFF_SERVICE_WAS_ACTIVE:-0}
DEFER_ROLLBACK_RESTART=${WB_IRRIGATION_DEFER_ROLLBACK_RESTART:-0}
DEPLOY_LOCK_DIR=${DEPLOY_LOCK_DIR:-/run/lock/wb-irrigation}
DEPLOY_LOCK_FILE=${DEPLOY_LOCK_FILE:-/run/lock/wb-irrigation/deploy.lock}
readonly FIXED_REPO_DIR=/opt/wb-irrigation/irrigation
readonly FIXED_REPO_DIR_REAL=/mnt/data/wb-irrigation
readonly FIXED_SERVICE=wb-irrigation
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

usage() {
  cat <<EOF
Usage: $(basename "$0") --commit SHA [--branch BRANCH] [--backup-dir DIR] [--yes]
Env vars: TARGET_COMMIT, BACKUP_BASE, BACKUP_KEEP
Pinned production toolchain: uv $UV_VERSION, Python $PY_VERSION (not overridable)
Examples:
  sudo bash $0 --yes --commit 0123456789abcdef0123456789abcdef01234567
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    -h | --help)
      usage
      exit 0
      ;;
    --repo-dir)
      REPO_DIR="$2"
      shift 2
      ;;
    --branch)
      BRANCH="$2"
      shift 2
      ;;
    --commit)
      TARGET_COMMIT="$2"
      shift 2
      ;;
    --service)
      SERVICE="$2"
      shift 2
      ;;
    --backup-dir)
      BACKUP_BASE="$2"
      shift 2
      ;;
    -y | --yes)
      NONINTERACTIVE=1
      shift
      ;;
    *)
      echo "Unknown arg: $1"
      usage
      exit 1
      ;;
  esac
done

ts() { date +"%Y-%m-%d %H:%M:%S"; }
info() { echo -e "\033[1;34m[$(ts)]\033[0m $*"; }
ok() { echo -e "\033[1;32m[$(ts)] OK\033[0m $*"; }
warn() { echo -e "\033[1;33m[$(ts)] WARN\033[0m $*"; }
err() { echo -e "\033[1;31m[$(ts)] ERROR\033[0m $*" >&2; }

require_full_commit_sha() {
  local target_commit=${1:-}

  if [[ ! "$target_commit" =~ ^[0-9a-f]{40}$ ]]; then
    err "TARGET_COMMIT must be an explicit full lowercase 40-character Git commit SHA"
    return 1
  fi
}

validate_branch_name() {
  if [[ "$BRANCH" != "main" ]]; then
    err "Production updates authorize commits only from branch main"
    return 1
  fi
}

fetch_authorization_branch() {
  local repository=$1

  info "Fetching authorization branch origin/$BRANCH"
  git -C "$repository" fetch --quiet origin \
    "+refs/heads/$BRANCH:refs/remotes/origin/$BRANCH"
}

resolve_authorized_target_commit() {
  local repository=$1
  local target_commit=$2
  local resolved_target

  require_full_commit_sha "$target_commit" || return 1
  if ! resolved_target=$(git -C "$repository" rev-parse --verify "${target_commit}^{commit}"); then
    err "Requested commit is not available after fetching origin/$BRANCH: $target_commit"
    return 1
  fi
  if [[ "$resolved_target" != "$target_commit" ]]; then
    err "Requested revision did not resolve to the exact immutable commit: $target_commit"
    return 1
  fi
  if ! git -C "$repository" merge-base --is-ancestor "$target_commit" "refs/remotes/origin/$BRANCH"; then
    err "Requested commit is not authorized by origin/$BRANCH: $target_commit"
    return 1
  fi
  printf '%s\n' "$resolved_target"
}

validate_fixed_deployment_contract() {
  if [[ "$BRANCH" != "main" ]]; then
    err "BRANCH override is unsupported for production deployment: $BRANCH"
    return 1
  fi
  if [[ "$REPO_DIR" != "$FIXED_REPO_DIR" ]]; then
    err "REPO_DIR override is unsupported by the fixed systemd unit: $REPO_DIR"
    return 1
  fi
  if [[ "$SERVICE" != "$FIXED_SERVICE" ]]; then
    err "SERVICE override is unsupported by the fixed systemd unit: $SERVICE"
    return 1
  fi
  if [[ "$ENV_FILE" != "$FIXED_ENV_FILE" ]]; then
    err "ENV_FILE override is unsupported by the fixed systemd unit: $ENV_FILE"
    return 1
  fi
  if [[ "$RELEASE_ENV_FILE" != "$FIXED_RELEASE_ENV_FILE" ]]; then
    err "RELEASE_ENV_FILE override is unsupported by the fixed systemd unit"
    return 1
  fi
  if [[ "$MQTT_TLS_DIR" != "$FIXED_MQTT_TLS_DIR" ]]; then
    err "MQTT_TLS_DIR override is unsupported by the fixed systemd unit"
    return 1
  fi
  if [[ "$STATE_DIR" != "$FIXED_STATE_DIR" ]]; then
    err "STATE_DIR override is unsupported by the fixed systemd unit: $STATE_DIR"
    return 1
  fi
  if [[ "$DEPLOY_CONTROL_DIR" != "$FIXED_DEPLOY_CONTROL_DIR" ]]; then
    err "DEPLOY_CONTROL_DIR override is unsupported for production deployment"
    return 1
  fi
  if [[ "$PYTHON_INSTALL_DIR" != "$FIXED_PYTHON_INSTALL_DIR" ]]; then
    err "PYTHON_INSTALL_DIR override is unsupported by the fixed systemd unit"
    return 1
  fi
  if [[ "$PY_VERSION" != "$FIXED_PY_VERSION" ]]; then
    err "PY_VERSION override is unsupported by the pinned production runtime"
    return 1
  fi
  if [[ "$UV_BIN" != "$FIXED_UV_BIN" ]]; then
    err "UV_BIN override is unsupported by the pinned production toolchain"
    return 1
  fi
  if [[ "$SERVICE_USER" != "wb-irrigation" || "$SERVICE_GROUP" != "wb-irrigation" ]]; then
    err "SERVICE_USER/SERVICE_GROUP overrides are unsupported"
    return 1
  fi
  if [[ "$DEPLOY_LOCK_DIR" != "$FIXED_DEPLOY_LOCK_DIR" \
    || "$DEPLOY_LOCK_FILE" != "$FIXED_DEPLOY_LOCK_FILE" ]]; then
    err "Deployment lock path override is unsupported for production deployment"
    return 1
  fi
  if [[ "$HANDOFF_SERVICE_WAS_ACTIVE" != "0" && "$HANDOFF_SERVICE_WAS_ACTIVE" != "1" ]]; then
    err "Invalid WB_IRRIGATION_HANDOFF_SERVICE_WAS_ACTIVE value"
    return 1
  fi
  if [[ "$DEFER_ROLLBACK_RESTART" != "0" && "$DEFER_ROLLBACK_RESTART" != "1" ]]; then
    err "Invalid WB_IRRIGATION_DEFER_ROLLBACK_RESTART value"
    return 1
  fi
  if [[ "$DEFER_ROLLBACK_RESTART" == "1" && "$HANDOFF_SERVICE_WAS_ACTIVE" != "1" ]]; then
    err "Deferred rollback restart requires an active-service install handoff"
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
    err "flock is required to serialize install, update and uninstall operations"
    return 1
  fi
  if [[ -L "$lock_parent" || ! -d "$lock_parent" \
    || "$(stat_owner_uid "$lock_parent")" != "$expected_uid" ]]; then
    err "Deployment lock parent is not a trusted real directory: $lock_parent"
    return 1
  fi
  if [[ ! -e "$DEPLOY_LOCK_DIR" ]]; then
    if ! install -d -m 0700 "$DEPLOY_LOCK_DIR"; then
      err "Could not create private deployment lock directory"
      return 1
    fi
  fi
  if [[ -L "$DEPLOY_LOCK_DIR" || ! -d "$DEPLOY_LOCK_DIR" \
    || "$(stat_owner_uid "$DEPLOY_LOCK_DIR")" != "$expected_uid" ]]; then
    err "Deployment lock directory is not trusted: $DEPLOY_LOCK_DIR"
    return 1
  fi
  if ! chmod 0700 "$DEPLOY_LOCK_DIR"; then
    err "Could not secure deployment lock directory"
    return 1
  fi
  if [[ -e "$DEPLOY_LOCK_FILE" || -L "$DEPLOY_LOCK_FILE" ]]; then
    if [[ -L "$DEPLOY_LOCK_FILE" || ! -f "$DEPLOY_LOCK_FILE" \
      || "$(stat_owner_uid "$DEPLOY_LOCK_FILE")" != "$expected_uid" \
      || "$(stat_link_count "$DEPLOY_LOCK_FILE")" != "1" ]]; then
      err "Deployment lock file is not a trusted regular file: $DEPLOY_LOCK_FILE"
      return 1
    fi
  fi
  if [[ -n "$inherited_fd" ]]; then
    if [[ "$inherited_fd" != "9" ]] || ! : >&9 2>/dev/null; then
      err "Invalid inherited deployment lock descriptor"
      return 1
    fi
    inherited_path=$(realpath "$descriptor_path") || return 1
    if [[ ( "$descriptor_path" == /proc/* && "$inherited_path" != "$DEPLOY_LOCK_FILE" ) \
      || ! -f "$DEPLOY_LOCK_FILE" || -L "$DEPLOY_LOCK_FILE" ]]; then
      err "Inherited descriptor does not reference $DEPLOY_LOCK_FILE"
      return 1
    fi
    path_identity=$(stat_followed_identity "$DEPLOY_LOCK_FILE") || return 1
    descriptor_identity=$(stat_followed_identity "$descriptor_path") || return 1
    if [[ "$path_identity" != "$descriptor_identity" \
      || "$path_identity" != "$expected_uid:1:"* ]]; then
      err "Inherited deployment lock descriptor identity is unsafe"
      return 1
    fi
    if ! flock -n 9; then
      err "Inherited deployment lock is invalid"
      return 1
    fi
    return 0
  fi

  exec 9>>"$DEPLOY_LOCK_FILE"
  if [[ -L "$DEPLOY_LOCK_FILE" || ! -f "$DEPLOY_LOCK_FILE" ]] \
    || ! chmod 0600 "$DEPLOY_LOCK_FILE"; then
    err "Could not secure deployment lock file"
    return 1
  fi
  path_identity=$(stat_followed_identity "$DEPLOY_LOCK_FILE") || return 1
  descriptor_identity=$(stat_followed_identity "$descriptor_path") || return 1
  if [[ "$path_identity" != "$descriptor_identity" \
    || "$path_identity" != "$expected_uid:1:"* ]]; then
    err "Deployment lock path changed while it was opened"
    return 1
  fi
  if ! flock -n 9; then
    err "Another install, update or uninstall operation holds $DEPLOY_LOCK_FILE"
    return 1
  fi
  if [[ "$(stat_followed_identity "$DEPLOY_LOCK_FILE")" != "$descriptor_identity" ]]; then
    err "Deployment lock path changed after locking"
    return 1
  fi
  export WB_IRRIGATION_DEPLOY_LOCK_FD=9
}

install_pinned_uv() {
  local architecture
  local tools_dir
  local staging_dir
  local archive
  local extracted_uv
  local staged_uv
  local metadata

  architecture=$(uname -m)
  if [[ "$architecture" != "aarch64" ]]; then
    err "Production uv bootstrap supports only aarch64, got $architecture"
    return 1
  fi
  if [[ -L "$UV_BIN" || ( -e "$UV_BIN" && ! -f "$UV_BIN" ) ]]; then
    err "Pinned uv destination is unsafe: $UV_BIN"
    return 1
  fi

  tools_dir=$(dirname "$UV_BIN")
  if [[ -L "$tools_dir" || ( -e "$tools_dir" && ! -d "$tools_dir" ) ]]; then
    err "Pinned uv tools path is unsafe: $tools_dir"
    return 1
  fi
  mkdir -p "$tools_dir"
  chown root:root "$tools_dir"
  chmod 0755 "$tools_dir"
  if ! staging_dir=$(mktemp -d /mnt/data/.wb-irrigation-uv.XXXXXX); then
    err "Could not create pinned uv staging directory"
    return 1
  fi
  archive="$staging_dir/uv.tar.gz"
  if ! curl --proto '=https' --tlsv1.2 -fsSLo "$archive" "$UV_ARCHIVE_URL"; then
    rm -rf -- "$staging_dir"
    err "Could not download pinned uv archive"
    return 1
  fi
  if ! printf '%s  %s\n' "$UV_ARCHIVE_SHA256" "$archive" | sha256sum -c - >/dev/null; then
    rm -rf -- "$staging_dir"
    err "Pinned uv archive SHA-256 verification failed"
    return 1
  fi
  if ! tar -xzf "$archive" -C "$staging_dir"; then
    rm -rf -- "$staging_dir"
    err "Could not extract pinned uv archive"
    return 1
  fi
  extracted_uv="$staging_dir/uv-aarch64-unknown-linux-gnu/uv"
  if [[ ! -f "$extracted_uv" || -L "$extracted_uv" ]]; then
    rm -rf -- "$staging_dir"
    err "Pinned uv archive has an unexpected layout"
    return 1
  fi
  staged_uv="${UV_BIN}.new-$$"
  if [[ -e "$staged_uv" || -L "$staged_uv" ]]; then
    rm -rf -- "$staging_dir"
    err "Pinned uv staging destination already exists: $staged_uv"
    return 1
  fi
  if ! install -o root -g root -m 0755 "$extracted_uv" "$staged_uv"; then
    rm -rf -- "$staging_dir"
    return 1
  fi
  if [[ "$($staged_uv --version)" != "uv $UV_VERSION" ]]; then
    rm -f -- "$staged_uv"
    rm -rf -- "$staging_dir"
    err "Pinned uv binary reports an unexpected version"
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
    err "Installed uv does not match the pinned root-owned uv $UV_VERSION"
    return 1
  fi
  rm -rf -- "$staging_dir"
}

ensure_service_account() {
  local passwd_entry
  local group_entry
  local group_gid
  local group_members
  local conflicting_primary
  local account_name password uid gid gecos account_home account_shell

  if ! getent group "$SERVICE_GROUP" >/dev/null 2>&1; then
    groupadd --system "$SERVICE_GROUP"
  fi
  group_entry=$(getent group "$SERVICE_GROUP")
  group_gid=$(printf '%s' "$group_entry" | cut -d: -f3)
  group_members=$(printf '%s' "$group_entry" | cut -d: -f4)
  if [[ "$group_gid" == "0" ]]; then
    err "$SERVICE_GROUP must not use privileged GID 0"
    return 1
  fi
  if [[ -n "$group_members" && "$group_members" != "$SERVICE_USER" ]]; then
    err "$SERVICE_GROUP has unexpected supplementary members"
    return 1
  fi
  conflicting_primary=$(getent passwd | awk -F: -v gid="$group_gid" -v user="$SERVICE_USER" \
    '$4 == gid && $1 != user {print $1; exit}')
  if [[ -n "$conflicting_primary" ]]; then
    err "$SERVICE_GROUP is the primary group of unexpected user $conflicting_primary"
    return 1
  fi
  if id "$SERVICE_USER" >/dev/null 2>&1; then
    if [[ "$(id -gn "$SERVICE_USER")" != "$SERVICE_GROUP" ]]; then
      err "$SERVICE_USER exists with an unexpected primary group"
      return 1
    fi
    passwd_entry=$(getent passwd "$SERVICE_USER")
    IFS=: read -r account_name password uid gid gecos account_home account_shell <<<"$passwd_entry"
    if [[ "$uid" == "0" || "$gid" == "0" ]]; then
      err "$SERVICE_USER must not use a privileged UID or GID"
      return 1
    fi
    if [[ "$account_home" != "$STATE_DIR" || "$account_shell" != "/usr/sbin/nologin" ]]; then
      err "$SERVICE_USER exists with an unexpected home or shell"
      return 1
    fi
  else
    useradd --system --gid "$SERVICE_GROUP" --home-dir "$STATE_DIR" \
      --shell /usr/sbin/nologin --no-create-home "$SERVICE_USER"
  fi
  if [[ "$(id -Gn "$SERVICE_USER")" != "$SERVICE_GROUP" ]]; then
    err "$SERVICE_USER must not belong to supplementary groups"
    return 1
  fi
}

assert_safe_state_source() {
  local source_path=$1
  local source_kind=$2
  local symlink_path=""

  if [[ -L "$source_path" ]]; then
    err "Refusing symlinked mutable state path: $source_path"
    return 1
  fi
  if [[ "$source_kind" == "file" ]]; then
    [[ ! -e "$source_path" || -f "$source_path" ]] || {
      err "Mutable state path is not a regular file: $source_path"
      return 1
    }
    return 0
  fi
  [[ ! -e "$source_path" || -d "$source_path" ]] || {
    err "Mutable state path is not a directory: $source_path"
    return 1
  }
  if [[ -d "$source_path" ]]; then
    symlink_path=$(find -P "$source_path" -type l -print -quit)
    if [[ -n "$symlink_path" ]]; then
      err "Refusing mutable state tree containing a symlink: $symlink_path"
      return 1
    fi
  fi
}

copy_state_database() {
  local source_path=$1
  local target_path=$2
  local owner=$3
  local group=$4
  local staged_path="${target_path}.state-stage-$$"
  local check_result

  assert_safe_state_source "$source_path" file || return 1
  [[ -f "$source_path" ]] || return 0
  rm -f -- "$staged_path"
  if ! sqlite3 "$source_path" ".backup '$staged_path'"; then
    rm -f -- "$staged_path"
    err "Could not copy SQLite state: $source_path"
    return 1
  fi
  check_result=$(sqlite3 "$staged_path" "PRAGMA quick_check;")
  if [[ "$check_result" != "ok" ]]; then
    rm -f -- "$staged_path"
    err "Copied SQLite state failed quick_check: $source_path"
    return 1
  fi
  chmod 0600 "$staged_path"
  chown "$owner:$group" "$staged_path"
  mv -f -- "$staged_path" "$target_path"
}

copy_state_file() {
  local source_path=$1
  local target_path=$2
  local owner=$3
  local group=$4
  local mode=$5
  local staged_path="${target_path}.state-stage-$$"

  assert_safe_state_source "$source_path" file || return 1
  [[ -f "$source_path" ]] || return 0
  install -m "$mode" "$source_path" "$staged_path"
  chown "$owner:$group" "$staged_path"
  mv -f -- "$staged_path" "$target_path"
}

copy_state_directory() {
  local source_path=$1
  local target_path=$2
  local owner=$3
  local group=$4
  local staged_path="${target_path}.state-stage-$$"
  local previous_path="${target_path}.state-previous-$$"

  assert_safe_state_source "$source_path" directory || return 1
  [[ -d "$source_path" ]] || return 0
  if [[ -e "$staged_path" || -L "$staged_path" || -e "$previous_path" || -L "$previous_path" ]]; then
    err "State copy staging path already exists for $target_path"
    return 1
  fi
  mkdir -p "$(dirname "$target_path")"
  mkdir -m 0700 "$staged_path"
  if ! cp -a "$source_path/." "$staged_path/"; then
    rm -rf -- "$staged_path"
    err "Could not copy mutable state directory: $source_path"
    return 1
  fi
  chown -R "$owner:$group" "$staged_path"
  chmod -R go-rwx "$staged_path"
  if [[ -e "$target_path" || -L "$target_path" ]]; then
    mv "$target_path" "$previous_path"
  fi
  if ! mv "$staged_path" "$target_path"; then
    [[ -e "$previous_path" ]] && mv "$previous_path" "$target_path"
    rm -rf -- "$staged_path"
    return 1
  fi
  rm -rf -- "$previous_path"
}

sync_mutable_state_tree() {
  local source_root=$1
  local target_root=$2
  local owner=$3
  local group=$4
  local state_dir

  if [[ -L "$source_root" || ! -d "$source_root" || -L "$target_root" ]]; then
    err "Mutable state roots must be real directories"
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
  touch "$target_root/static/media/maps/.gitkeep" "$target_root/static/media/zones/.gitkeep"
  chown "$owner:$group" \
    "$target_root/static/media/maps/.gitkeep" "$target_root/static/media/zones/.gitkeep"
  chmod 0600 "$target_root/static/media/maps/.gitkeep" "$target_root/static/media/zones/.gitkeep"
}

preflight_state_copy_space() {
  local source_root=$1
  local state_parent=$2
  local required_kib=0
  local available_kib
  local state_path
  local size

  for state_path in irrigation.db jobs.db .secret_key .irrig_secret_key .env backups static/media/maps static/media/zones; do
    if [[ -e "$source_root/$state_path" && ! -L "$source_root/$state_path" ]]; then
      size=$(du -sk "$source_root/$state_path" | awk '{print $1}')
      required_kib=$((required_kib + size))
    fi
  done
  available_kib=$(df -Pk "$state_parent" | awk 'NR == 2 {print $4}')
  if [[ ! "$available_kib" =~ ^[0-9]+$ ]] || ((available_kib <= required_kib + 10240)); then
    err "Insufficient free space for transactional state migration"
    return 1
  fi
}

prepare_state_layout() {
  local legacy_root=$1
  local import_legacy=$2
  local state_parent
  local state_stage

  state_parent=$(dirname "$STATE_DIR")
  if [[ -L "$STATE_DIR" ]]; then
    err "STATE_DIR must not be a symlink: $STATE_DIR"
    return 1
  fi
  if [[ "$import_legacy" == "0" ]]; then
    if [[ ! -d "$STATE_DIR" || -L "$STATE_DIR" \
      || ! -d "$STATE_DIR/backups" \
      || ! -d "$STATE_DIR/static/media/maps" \
      || ! -d "$STATE_DIR/static/media/zones" ]]; then
      err "Active non-root unit requires a complete marked state layout"
      return 1
    fi
    return 0
  fi
  if [[ -e "$STATE_DIR" ]]; then
    err "Legacy unit and existing STATE_DIR conflict; refusing an ambiguous migration"
    return 1
  fi

  preflight_state_copy_space "$legacy_root" "$state_parent"
  state_stage="$state_parent/.wb-irrigation-state.stage.$$"
  if [[ -e "$state_stage" || -L "$state_stage" ]]; then
    err "State migration staging path already exists: $state_stage"
    return 1
  fi
  mkdir -m 0700 "$state_stage"
  if ! sync_mutable_state_tree "$legacy_root" "$state_stage" "$SERVICE_USER" "$SERVICE_GROUP"; then
    rm -rf -- "$state_stage"
    return 1
  fi
  if ! mv "$state_stage" "$STATE_DIR"; then
    rm -rf -- "$state_stage"
    return 1
  fi
  STATE_ACTIVATED=1
}

secure_python_runtime() {
  chown -R root:root "$PYTHON_INSTALL_DIR"
  chmod -R go-w "$PYTHON_INSTALL_DIR"
  chmod -R a+rX "$PYTHON_INSTALL_DIR"
}

secure_code_tree() {
  local repository=$1
  local tracked_file
  local tracked_mode

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
  if [[ -d "$repository/venv" ]]; then
    chmod -R go-w "$repository/venv"
    chmod -R a+rX "$repository/venv"
  fi
  for private_path in \
    "$repository/.env" "$repository/.secret_key" "$repository/.irrig_secret_key" \
    "$repository/irrigation.db" "$repository/jobs.db" "$repository/backups"; do
    if [[ -e "$private_path" && ! -L "$private_path" ]]; then
      chmod -R go-rwx "$private_path"
    fi
  done
}

normalize_state_permissions() {
  normalize_state_tree_permissions "$STATE_DIR"
}

normalize_state_tree_permissions() {
  local state_root=$1

  if [[ -L "$state_root" || ! -d "$state_root" ]]; then
    err "State permission target is not a real directory: $state_root"
    return 1
  fi
  if ! chown -R "$SERVICE_USER:$SERVICE_GROUP" "$state_root"; then
    err "Could not set state ownership: $state_root"
    return 1
  fi
  if ! chmod 0700 "$state_root"; then
    err "Could not secure state root: $state_root"
    return 1
  fi
  if ! find -P "$state_root" -type d -exec chmod 0700 {} +; then
    err "Could not secure state directories: $state_root"
    return 1
  fi
  if ! find -P "$state_root" -type f -exec chmod 0600 {} +; then
    err "Could not secure state files: $state_root"
    return 1
  fi
}

verify_service_runtime_access() {
  local tls_path

  runuser -u "$SERVICE_USER" -- "$VENV_DIR/bin/python" -V >/dev/null
  runuser -u "$SERVICE_USER" -- test -r "$REPO_DIR_REAL/run.py"
  runuser -u "$SERVICE_USER" -- test -r "$RELEASE_ENV_FILE"
  runuser -u "$SERVICE_USER" -- test -w "$STATE_DIR"
  runuser -u "$SERVICE_USER" -- test -w "$STATE_DIR/static/media/maps"
  runuser -u "$SERVICE_USER" -- test -w "$STATE_DIR/static/media/zones"
  if runuser -u "$SERVICE_USER" -- test -w "$REPO_DIR_REAL" \
    || runuser -u "$SERVICE_USER" -- test -w "$VENV_DIR" \
    || runuser -u "$SERVICE_USER" -- test -w "$PYTHON_INSTALL_DIR"; then
    err "$SERVICE_USER must not be able to write code, venv or shared Python"
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
    err "Invalid unprivileged PORT value (1024-65535 required): ${port:-<empty>}"
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
    err "WB_HTTP_TLS_CERTFILE and WB_HTTP_TLS_KEYFILE must be configured together"
    return 1
  fi
}

validate_http_host_value() {
  local host_value=$1

  if [[ -z "$host_value" || "$host_value" =~ [^A-Za-z0-9.:-] || "$host_value" == -* ]]; then
    err "Invalid HTTP host value: ${host_value:-<empty>}"
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
      err "WB_HTTP_PROBE_HOST must not be a wildcard address"
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
      warn "WB_HTTP_PROBE_INSECURE_TLS=1 disables TLS verification for this local readiness probe"
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
    err "WB_HTTP_PROBE_INSECURE_TLS must be 0 or 1"
    return 1
  fi
  if [[ -n "$ca_file" && "$insecure_probe" == "1" ]]; then
    err "WB_HTTP_PROBE_CA_FILE and WB_HTTP_PROBE_INSECURE_TLS=1 are mutually exclusive"
    return 1
  fi
  if [[ -n "$ca_file" && ( -L "$ca_file" || ! -f "$ca_file" || ! -r "$ca_file" ) ]]; then
    err "WB_HTTP_PROBE_CA_FILE must be a readable regular non-symlink file: $ca_file"
    return 1
  fi
  if [[ "$scheme" == "http" && ( -n "$ca_file" || "$insecure_probe" == "1" ) ]]; then
    err "TLS probe options require WB_HTTP_TLS_CERTFILE and WB_HTTP_TLS_KEYFILE"
    return 1
  fi
  case "$bind_host" in
    127.0.0.1 | localhost | ::1) ;;
    *)
      if [[ "$scheme" != "https" && "$allow_insecure" != "1" ]]; then
        err "External WB_HTTP_BIND_HOST=$bind_host requires native TLS or WB_HTTP_ALLOW_INSECURE_EXTERNAL=1"
        return 1
      fi
      ;;
  esac
}

upsert_env_value() {
  local key=$1
  local value=$2
  local env_file=$3

  mkdir -p -m 700 "$(dirname "$env_file")"
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
  local source_file=$1
  local target_file=$2
  local target_parent
  local staged_file
  local line
  local key

  [[ -e "$source_file" || -L "$source_file" ]] || return 0
  if [[ -L "$source_file" || ! -f "$source_file" ]]; then
    err "Legacy environment source is not a regular file: $source_file"
    return 1
  fi
  target_parent=$(dirname -- "$target_file")
  if [[ -L "$target_parent" || ! -d "$target_parent" ]]; then
    err "Environment destination parent is unsafe: $target_parent"
    return 1
  fi
  if [[ -e "$target_file" || -L "$target_file" ]]; then
    if [[ -L "$target_file" || ! -f "$target_file" ]]; then
      err "Environment destination is unsafe: $target_file"
      return 1
    fi
  fi
  if ! staged_file=$(mktemp "$target_parent/.wb-irrigation-env.XXXXXX"); then
    err "Could not create environment migration staging file"
    return 1
  fi
  if [[ -f "$target_file" ]] && ! cp -a -- "$target_file" "$staged_file"; then
    rm -f -- "$staged_file"
    err "Could not stage the operator environment"
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
      err "Legacy environment contains an unsupported line"
      return 1
    fi
    if ! grep -Eq "^[[:space:]]*(export[[:space:]]+)?${key}[[:space:]]*=" "$staged_file"; then
      if ! printf '%s\n' "$line" >>"$staged_file"; then
        rm -f -- "$staged_file"
        err "Could not merge legacy environment key: $key"
        return 1
      fi
    fi
  done <"$source_file"
  if ! chmod 0640 "$staged_file" \
    || ! chown root:"$SERVICE_GROUP" "$staged_file" \
    || ! mv -f -- "$staged_file" "$target_file"; then
    rm -f -- "$staged_file"
    err "Could not publish the merged root-owned environment"
    return 1
  fi
  if ! rm -f -- "$source_file"; then
    err "Could not retire legacy environment source: $source_file"
    return 1
  fi
}

publish_release_environment() {
  local commit_sha=$1
  local release_parent
  local staged_release

  require_full_commit_sha "$commit_sha" || return 1
  release_parent=$(dirname -- "$RELEASE_ENV_FILE")
  if [[ -L "$release_parent" || ! -d "$release_parent" ]]; then
    err "Release environment parent is unsafe: $release_parent"
    return 1
  fi
  if [[ -e "$RELEASE_ENV_FILE" || -L "$RELEASE_ENV_FILE" ]]; then
    if [[ -L "$RELEASE_ENV_FILE" || ! -f "$RELEASE_ENV_FILE" ]]; then
      err "Release environment target is unsafe: $RELEASE_ENV_FILE"
      return 1
    fi
  fi
  if ! staged_release=$(mktemp "$release_parent/.wb-irrigation-release.XXXXXX"); then
    err "Could not create release environment staging file"
    return 1
  fi
  if ! printf 'WB_APP_VERSION=%s\nGIT_COMMIT=%s\n' "$commit_sha" "$commit_sha" >"$staged_release" \
    || ! chmod 0640 "$staged_release" \
    || ! chown root:"$SERVICE_GROUP" "$staged_release" \
    || ! mv -f -- "$staged_release" "$RELEASE_ENV_FILE"; then
    rm -f -- "$staged_release"
    err "Could not atomically publish release environment"
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
    err "Deploy control parent is not a trusted real directory: $control_parent"
    return 1
  fi
  if [[ ! -e "$DEPLOY_CONTROL_DIR" ]]; then
    if ! install -d -m 0700 "$DEPLOY_CONTROL_DIR"; then
      err "Could not create deploy control directory"
      return 1
    fi
  fi
  if [[ -L "$DEPLOY_CONTROL_DIR" || ! -d "$DEPLOY_CONTROL_DIR" \
    || "$(stat -c %u "$DEPLOY_CONTROL_DIR")" != "$expected_uid" ]]; then
    err "Deploy control directory is unsafe: $DEPLOY_CONTROL_DIR"
    return 1
  fi
  if ! chmod 0700 "$DEPLOY_CONTROL_DIR"; then
    err "Could not secure deploy control directory"
    return 1
  fi
}

validate_layout_marker() {
  local marker="$DEPLOY_CONTROL_DIR/layout-v1"
  local expected_uid=0

  if [[ "$DEPLOY_CONTROL_DIR" != "$FIXED_DEPLOY_CONTROL_DIR" ]]; then
    expected_uid=$(id -u)
  fi
  if [[ -L "$marker" || ! -f "$marker" \
    || "$(stat -c %u "$marker")" != "$expected_uid" \
    || "$(stat -c %h "$marker")" != "1" ]]; then
    err "Trusted state layout marker is unavailable: $marker"
    return 1
  fi
}

canonicalize_backup_base() {
  local backup_parent
  local backup_name

  if [[ -z "${DEPLOY_CONTROL_DIR:-}" || "$DEPLOY_CONTROL_DIR" != /* ]]; then
    err "Deploy control path must be absolute before validating backups"
    return 1
  fi
  if [[ -z "${BACKUP_BASE:-}" || "$BACKUP_BASE" != /* ]]; then
    err "BACKUP_BASE must be an absolute dedicated directory"
    return 1
  fi
  if [[ -L "$BACKUP_BASE" ]]; then
    err "BACKUP_BASE must not be a symlink: $BACKUP_BASE"
    return 1
  fi

  backup_parent=$(dirname -- "$BACKUP_BASE")
  backup_name=$(basename -- "$BACKUP_BASE")
  if [[ "$backup_name" != "backups" || "$backup_parent" != "$DEPLOY_CONTROL_DIR" ]]; then
    err "BACKUP_BASE must be the dedicated child $DEPLOY_CONTROL_DIR/backups"
    return 1
  fi
  if [[ -e "$BACKUP_BASE" && ! -d "$BACKUP_BASE" ]]; then
    err "BACKUP_BASE exists but is not a directory: $BACKUP_BASE"
    return 1
  fi

  BACKUP_BASE="$DEPLOY_CONTROL_DIR/backups"
}

ensure_logrotate_ready() {
  if ! command -v logrotate >/dev/null 2>&1; then
    info "Installing required logrotate package"
    apt-get update -qq
    DEBIAN_FRONTEND=noninteractive apt-get install -y logrotate
  fi
  if ! command -v logrotate >/dev/null 2>&1 || [[ ! -f /etc/logrotate.conf ]]; then
    err "logrotate runtime is incomplete"
    return 1
  fi

  logrotate -d "/etc/logrotate.conf"
  if systemctl cat logrotate.timer >/dev/null 2>&1; then
    systemctl enable --now logrotate.timer
    if ! systemctl is-enabled logrotate.timer >/dev/null 2>&1 \
      || ! systemctl is-active logrotate.timer >/dev/null 2>&1; then
      err "logrotate.timer is installed but not active"
      return 1
    fi
    info "logrotate.timer is enabled and active"
  elif [[ -x /etc/cron.daily/logrotate ]] && systemctl is-active cron >/dev/null 2>&1; then
    info "logrotate is scheduled by active cron"
  else
    err "No active logrotate.timer or cron execution path found"
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
    err "Could not inspect services in target commit $target_commit"
    return 1
  fi
  read -r services_mode services_type services_object services_path <<<"$services_entry"
  if [[ "$services_mode" != "040000" || "$services_type" != "tree" || "$services_path" != "services" ]]; then
    err "Target commit does not contain a tracked regular services tree"
    return 1
  fi

  if ! runtime_entry=$(git -C "$repository" ls-tree "$target_commit" -- services/telegram_bot.py); then
    err "Could not inspect Telegram runtime in target commit $target_commit"
    return 1
  fi
  read -r runtime_mode runtime_type runtime_object runtime_path <<<"$runtime_entry"
  if [[ "$runtime_type" != "blob" || "$runtime_path" != "services/telegram_bot.py" \
    || ("$runtime_mode" != "100644" && "$runtime_mode" != "100755") ]]; then
    err "Target commit lacks a tracked regular Telegram runtime"
    return 1
  fi
  if [[ "$runtime_object" != "$TELEGRAM_LOG_RETIREMENT_BLOB" ]]; then
    err "Target Telegram runtime does not satisfy the versioned file-log retirement contract"
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
    err "Refusing to traverse a non-canonical application logs directory: $expected_logs_dir"
    return 1
  fi
  logs_dir_real=$(cd "$expected_logs_dir" && pwd -P) || return 1
  if [[ "$logs_dir_real" != "$expected_logs_dir" ]]; then
    err "Application logs directory escapes the canonical repository: $logs_dir_real"
    return 1
  fi

  if ! listing_file=$(mktemp /tmp/wb-irrigation-telegram-logs.XXXXXX); then
    err "Could not create a private Telegram log enumeration file"
    return 1
  fi
  if ! chmod 600 "$listing_file"; then
    rm -f -- "$listing_file" || err "Could not remove insecure enumeration file: $listing_file"
    err "Could not make the Telegram log enumeration file private"
    return 1
  fi
  enumeration_sentinel="WB_IRRIGATION_ENUMERATION_COMPLETE_${listing_file##*/}"
  if ! find -P "$logs_dir_real" -mindepth 1 -maxdepth 1 -print0 >"$listing_file"; then
    rm -f -- "$listing_file" || err "Could not remove failed enumeration file: $listing_file"
    err "Could not enumerate retired Telegram file logs"
    return 1
  fi
  if ! printf '%s\0' "$enumeration_sentinel" >>"$listing_file"; then
    rm -f -- "$listing_file" || err "Could not remove incomplete enumeration file: $listing_file"
    err "Could not finalize retired Telegram log enumeration"
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
          err "Could not remove retired Telegram file log: $candidate"
          iteration_failed=1
          break
        fi
        info "Removed retired Telegram file log: $candidate"
      elif [[ -e "$candidate" ]]; then
        err "Refusing to remove non-file historical log path: $candidate"
        iteration_failed=1
        break
      fi
    fi
  done <"$listing_file"; then
    iteration_failed=1
  fi

  if ! rm -f -- "$listing_file"; then
    err "Could not remove Telegram log enumeration file: $listing_file"
    return 1
  fi
  if [[ "$iteration_failed" == "1" || "$enumeration_complete" != "1" ]]; then
    err "Retired Telegram log enumeration was not processed completely"
    return 1
  fi
}

prune_old_backups() {
  local -a backups=()
  local old_backup
  local retained_previous

  retained_previous=$((BACKUP_KEEP - 1))

  while IFS= read -r -d '' old_backup; do
    [[ "$old_backup" == "${BACKUP_DIR:-}" ]] && continue
    backups+=("$old_backup")
  done < <(find "$BACKUP_BASE" -mindepth 1 -maxdepth 1 -type d -name '????????_??????' -print0 | sort -z)

  while ((${#backups[@]} > retained_previous)); do
    old_backup=${backups[0]}
    info "Removing expired backup: $old_backup"
    rm -rf -- "$old_backup"
    backups=("${backups[@]:1}")
  done
}

backup_sqlite_database() {
  local database_path=$1
  local backup_path=$2
  local database_name=$3
  local check_result

  if ! sqlite3 "$database_path" ".backup '$backup_path'"; then
    err "Could not back up $database_name"
    return 1
  fi
  if ! check_result=$(sqlite3 "$backup_path" "PRAGMA quick_check;"); then
    err "Could not verify $database_name backup"
    return 1
  fi
  if [[ "$check_result" != "ok" ]]; then
    err "$database_name backup verification failed: $check_result"
    return 1
  fi
  if ! chmod 600 "$backup_path"; then
    err "Could not secure $database_name backup"
    return 1
  fi
}

migrate_mqtt_tls_paths() {
  local database_path=$1
  local legacy_root=$2
  local table_exists
  local schema_columns
  local invalid_paths
  local rows
  local column
  local server_id
  local configured_path
  local source_path
  local source_real
  local destination_path
  local staged_path
  local check_result
  local sql_updates=""

  [[ -f "$database_path" && ! -L "$database_path" ]] || return 0
  table_exists=$(sqlite3 "$database_path" \
    "SELECT count(*) FROM sqlite_master WHERE type='table' AND name='mqtt_servers';") || return 1
  [[ "$table_exists" == "1" ]] || return 0
  schema_columns=$(sqlite3 "$database_path" \
    "SELECT group_concat(name, ',') FROM pragma_table_info('mqtt_servers');") || return 1
  for column in enabled tls_enabled tls_ca_path tls_cert_path tls_key_path; do
    if [[ ",$schema_columns," != *",$column,"* ]]; then
      return 0
    fi
  done
  if [[ -L "$MQTT_TLS_DIR" || ( -e "$MQTT_TLS_DIR" && ! -d "$MQTT_TLS_DIR" ) ]]; then
    err "MQTT TLS material directory is unsafe: $MQTT_TLS_DIR"
    return 1
  fi
  [[ -e "$MQTT_TLS_DIR" ]] || MQTT_TLS_DIR_CREATED=1
  if ! install -d -m 0750 "$MQTT_TLS_DIR" \
    || ! chown root:"$SERVICE_GROUP" "$MQTT_TLS_DIR" \
    || ! chmod 0750 "$MQTT_TLS_DIR"; then
    err "Could not secure MQTT TLS material directory"
    return 1
  fi

  for column in tls_ca_path tls_cert_path tls_key_path; do
    invalid_paths=$(sqlite3 "$database_path" \
      "SELECT count(*) FROM mqtt_servers WHERE enabled=1 AND tls_enabled=1
       AND coalesce($column, '') != ''
       AND (instr($column, '|') > 0 OR instr($column, char(10)) > 0 OR instr($column, char(13)) > 0);") \
      || return 1
    if [[ "$invalid_paths" != "0" ]]; then
      err "MQTT TLS path contains unsupported control characters: $column"
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
          err "MQTT TLS material is unavailable or symlinked: $source_path"
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
        err "MQTT TLS material is unavailable: $source_path"
        return 1
      fi
      source_real=$(readlink -f -- "$source_path") || return 1
      if [[ "$configured_path" != /* && "$source_real" != "$legacy_root"/* ]]; then
        err "Relative MQTT TLS material escapes the legacy repository"
        return 1
      fi
      destination_path="$MQTT_TLS_DIR/server-${server_id}-${column}.pem"
      if [[ -L "$destination_path" || ( -e "$destination_path" && ! -f "$destination_path" ) ]]; then
        err "MQTT TLS destination is unsafe: $destination_path"
        return 1
      fi
      if [[ -f "$destination_path" ]]; then
        if ! cmp -s -- "$source_real" "$destination_path"; then
          err "Existing MQTT TLS destination differs from migration source"
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
          err "Could not publish MQTT TLS material"
          return 1
        fi
        MQTT_TLS_CREATED_FILES+=("$destination_path")
      fi
      if ! runuser -u "$SERVICE_USER" -- test -r "$destination_path"; then
        err "Migrated MQTT TLS material is not service-readable"
        return 1
      fi
      sql_updates+="UPDATE mqtt_servers SET $column='$destination_path' WHERE id=$server_id;"
    done <<<"$rows"
  done
  if [[ -n "$sql_updates" ]]; then
    DATABASES_MAY_HAVE_MUTATED=1
    if ! sqlite3 "$database_path" "BEGIN IMMEDIATE;${sql_updates}COMMIT;"; then
      err "Could not atomically update MQTT TLS paths"
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
    if ! rm -f -- "$created_file"; then
      return 1
    fi
  done
  MQTT_TLS_CREATED_FILES=()
  if [[ "$MQTT_TLS_DIR_CREATED" == "1" && -d "$MQTT_TLS_DIR" && ! -L "$MQTT_TLS_DIR" ]]; then
    if ! rmdir -- "$MQTT_TLS_DIR"; then
      err "Could not remove empty MQTT TLS migration directory"
      return 1
    fi
    MQTT_TLS_DIR_CREATED=0
  fi
}

restore_sqlite_database() {
  local database_path=$1
  local backup_path=$2
  local existed_before=$3
  local backup_verified=$4
  local database_name=$5
  local check_result
  local database_dir
  local database_dir_real
  local staged_path

  if [[ "$existed_before" != "0" && "$existed_before" != "1" \
    || "$backup_verified" != "0" && "$backup_verified" != "1" ]]; then
    err "Invalid $database_name rollback state"
    return 1
  fi
  if [[ "$existed_before" == "0" ]]; then
    if ! rm -f -- "${database_path}-wal" "${database_path}-shm"; then
      err "Could not remove new $database_name WAL files"
      return 1
    fi
    if ! rm -f -- "$database_path"; then
      err "Could not remove $database_name created by the failed update"
      return 1
    fi
    return 0
  fi
  if [[ "$backup_verified" != "1" || -L "$backup_path" || ! -f "$backup_path" ]]; then
    err "$database_name rollback requires a verified regular backup: $backup_path"
    return 1
  fi
  if [[ -L "$database_path" || ! -f "$database_path" ]]; then
    err "$database_name live rollback target must remain a regular non-symlink file"
    return 1
  fi
  database_dir=$(dirname "$database_path")
  if [[ -L "$database_dir" || ! -d "$database_dir" ]]; then
    err "$database_name rollback destination directory is unsafe: $database_dir"
    return 1
  fi
  database_dir_real=$(cd "$database_dir" && pwd -P) || return 1
  if [[ "$database_dir_real" != "$database_dir" ]]; then
    err "$database_name rollback destination directory is not canonical: $database_dir"
    return 1
  fi
  if ! staged_path=$(mktemp "$database_dir/.${database_name}.rollback-stage.XXXXXX"); then
    err "Could not create $database_name rollback staging file"
    return 1
  fi
  if ! sqlite3 "$staged_path" ".restore '$backup_path'"; then
    rm -f -- "$staged_path" "${staged_path}-wal" "${staged_path}-shm"
    err "Could not restore $database_name"
    return 1
  fi
  if ! check_result=$(sqlite3 "$staged_path" "PRAGMA quick_check;"); then
    rm -f -- "$staged_path" "${staged_path}-wal" "${staged_path}-shm"
    err "Could not verify restored $database_name"
    return 1
  fi
  if [[ "$check_result" != "ok" ]]; then
    rm -f -- "$staged_path" "${staged_path}-wal" "${staged_path}-shm"
    err "$database_name rollback verification failed: $check_result"
    return 1
  fi
  if ! chmod 600 "$staged_path" \
    || ! chown "$SERVICE_USER:$SERVICE_GROUP" "$staged_path"; then
    rm -f -- "$staged_path" "${staged_path}-wal" "${staged_path}-shm"
    err "Could not secure restored $database_name staging file"
    return 1
  fi
  if ! rm -f -- "${database_path}-wal" "${database_path}-shm"; then
    rm -f -- "$staged_path" "${staged_path}-wal" "${staged_path}-shm"
    err "Could not remove stale $database_name WAL files"
    return 1
  fi
  if ! mv -f -- "$staged_path" "$database_path"; then
    rm -f -- "$staged_path" "${staged_path}-wal" "${staged_path}-shm"
    err "Could not atomically publish restored $database_name"
    return 1
  fi
}

validate_legacy_runtime_relative_path() {
  local relative_path=$1

  if [[ -z "$relative_path" || "$relative_path" == /* \
    || "$relative_path" == ".." || "$relative_path" == ../* \
    || "$relative_path" == */../* || "$relative_path" == */.. ]]; then
    return 1
  fi
  case "$relative_path" in
    irrigation.db | irrigation.db-wal | irrigation.db-shm \
      | jobs.db | jobs.db-wal | jobs.db-shm \
      | .secret_key | .irrig_secret_key | .env \
      | backups/* | static/media/maps/* | static/media/zones/*)
      return 0
      ;;
    *)
      return 1
      ;;
  esac
}

ensure_safe_repository_parent() {
  local repository=$1
  local relative_parent=$2
  local repository_real
  local current
  local component
  local current_real
  local -a parent_components=()

  repository_real=$(cd "$repository" && pwd -P) || return 1
  current=$repository_real
  if [[ "$relative_parent" == "." ]]; then
    return 0
  fi
  IFS=/ read -r -a parent_components <<<"$relative_parent"
  for component in "${parent_components[@]}"; do
    if [[ -z "$component" || "$component" == "." || "$component" == ".." ]]; then
      err "Unsafe legacy rollback destination component"
      return 1
    fi
    current="$current/$component"
    if [[ -L "$current" || ( -e "$current" && ! -d "$current" ) ]]; then
      err "Unsafe legacy rollback destination parent: $current"
      return 1
    fi
    if [[ ! -d "$current" ]] && ! mkdir -- "$current"; then
      err "Could not create legacy rollback destination parent: $current"
      return 1
    fi
    current_real=$(cd "$current" && pwd -P) || return 1
    if [[ "$current_real" != "$current" || "$current_real" != "$repository_real"/* ]]; then
      err "Legacy rollback destination escapes the repository: $current"
      return 1
    fi
  done
}

restore_state_snapshot() {
  local state_parent
  local state_parent_real
  local state_name
  local staged_state
  local previous_state
  local relative_path
  local staged_path
  local live_path
  local previous_path
  local symlink_path
  local publication_failed=0
  local rollback_publication_failed=0
  local index
  local -a managed_paths=(.secret_key .irrig_secret_key .env backups static)
  local -a published_paths=()

  if [[ "${STATE_SNAPSHOT_CREATED:-0}" != "1" ]]; then
    return 0
  fi
  if [[ -L "$STATE_DIR" || ! -d "$STATE_DIR" \
    || -L "$STATE_ARCHIVE" || ! -f "$STATE_ARCHIVE" ]]; then
    err "State rollback snapshot or destination is unavailable"
    return 1
  fi
  state_parent=$(dirname -- "$STATE_DIR")
  state_name=$(basename -- "$STATE_DIR")
  if [[ -L "$state_parent" || ! -d "$state_parent" ]]; then
    err "State rollback parent is unsafe: $state_parent"
    return 1
  fi
  state_parent_real=$(cd "$state_parent" && pwd -P) || return 1
  if [[ "$state_parent_real" != "$state_parent" ]]; then
    err "State rollback parent is not canonical: $state_parent"
    return 1
  fi
  if ! staged_state=$(mktemp -d "$state_parent/.${state_name}.rollback-stage.XXXXXX"); then
    err "Could not create state rollback staging directory"
    return 1
  fi
  if ! tar -xzf "$STATE_ARCHIVE" -C "$staged_state"; then
    rm -rf -- "$staged_state" || true
    err "Could not extract state rollback snapshot"
    return 1
  fi
  if ! symlink_path=$(find -P "$staged_state" -type l -print -quit); then
    rm -rf -- "$staged_state" || true
    err "Could not validate state rollback snapshot"
    return 1
  fi
  if [[ -n "$symlink_path" \
    || ! -d "$staged_state/backups" \
    || ! -d "$staged_state/static/media/maps" \
    || ! -d "$staged_state/static/media/zones" ]]; then
    rm -rf -- "$staged_state" || true
    err "State rollback snapshot has an unsafe or incomplete layout"
    return 1
  fi
  if ! normalize_state_tree_permissions "$staged_state"; then
    rm -rf -- "$staged_state" || true
    err "Could not normalize staged state rollback snapshot"
    return 1
  fi

  for relative_path in "${managed_paths[@]}"; do
    staged_path="$staged_state/$relative_path"
    live_path="$STATE_DIR/$relative_path"
    if [[ -L "$staged_path" || -L "$live_path" ]]; then
      rm -rf -- "$staged_state" || true
      err "State rollback refuses symlinked managed path: $relative_path"
      return 1
    fi
    case "$relative_path" in
      backups | static)
        if { [[ -e "$staged_path" ]] && [[ ! -d "$staged_path" ]]; } \
          || { [[ -e "$live_path" ]] && [[ ! -d "$live_path" ]]; }; then
          rm -rf -- "$staged_state" || true
          err "State rollback managed directory has an unexpected type: $relative_path"
          return 1
        fi
        ;;
      *)
        if { [[ -e "$staged_path" ]] && [[ ! -f "$staged_path" ]]; } \
          || { [[ -e "$live_path" ]] && [[ ! -f "$live_path" ]]; }; then
          rm -rf -- "$staged_state" || true
          err "State rollback managed file has an unexpected type: $relative_path"
          return 1
        fi
        ;;
    esac
  done

  if ! previous_state=$(mktemp -d "$state_parent/.${state_name}.rollback-previous.XXXXXX"); then
    rm -rf -- "$staged_state" || true
    err "Could not create state rollback holding directory"
    return 1
  fi
  for relative_path in "${managed_paths[@]}"; do
    staged_path="$staged_state/$relative_path"
    live_path="$STATE_DIR/$relative_path"
    previous_path="$previous_state/$relative_path"
    published_paths+=("$relative_path")
    if [[ -e "$live_path" ]]; then
      if ! mv -- "$live_path" "$previous_path"; then
        publication_failed=1
        break
      fi
    fi
    if [[ -e "$staged_path" ]] && ! mv -- "$staged_path" "$live_path"; then
      publication_failed=1
      break
    fi
  done
  if [[ "$publication_failed" == "1" ]]; then
    for ((index = ${#published_paths[@]} - 1; index >= 0; index--)); do
      relative_path=${published_paths[index]}
      live_path="$STATE_DIR/$relative_path"
      previous_path="$previous_state/$relative_path"
      if [[ -e "$live_path" ]] && ! rm -rf -- "$live_path"; then
        rollback_publication_failed=1
      fi
      if [[ -e "$previous_path" ]] && ! mv -- "$previous_path" "$live_path"; then
        rollback_publication_failed=1
      fi
    done
    rm -rf -- "$staged_state" "$previous_state" || rollback_publication_failed=1
    err "Could not transactionally publish restored state"
    [[ "$rollback_publication_failed" == "0" ]] || err "State publication rollback was incomplete"
    return 1
  fi
  if ! rm -rf -- "$staged_state" "$previous_state"; then
    err "Could not remove state rollback staging directories"
    return 1
  fi
}

quarantine_legacy_runtime_state() {
  local repository=$1
  local relative_path
  local source_path
  local target_path
  local manifest_complete=0
  local readonly manifest_sentinel=WB_IRRIGATION_LEGACY_MANIFEST_COMPLETE

  LEGACY_QUARANTINE="$BACKUP_DIR/legacy-runtime"
  LEGACY_MANIFEST="$BACKUP_DIR/legacy-runtime.manifest"
  if [[ -e "$LEGACY_QUARANTINE" || -L "$LEGACY_QUARANTINE" || -e "$LEGACY_MANIFEST" ]]; then
    err "Legacy runtime quarantine already exists"
    return 1
  fi
  mkdir -m 0700 "$LEGACY_QUARANTINE"
  if ! git -C "$repository" ls-files -z --others -- \
    'irrigation.db*' 'jobs.db*' .secret_key .irrig_secret_key .env backups \
    static/media/maps static/media/zones >"$LEGACY_MANIFEST"; then
    rm -rf -- "$LEGACY_QUARANTINE" "$LEGACY_MANIFEST"
    err "Could not enumerate legacy mutable state"
    return 1
  fi
  if ! printf '%s\0' "$manifest_sentinel" >>"$LEGACY_MANIFEST" \
    || ! chmod 0600 "$LEGACY_MANIFEST"; then
    rm -rf -- "$LEGACY_QUARANTINE" "$LEGACY_MANIFEST" || true
    err "Could not finalize legacy mutable state manifest"
    return 1
  fi
  LEGACY_CLEANUP_STARTED=1
  while IFS= read -r -d '' relative_path; do
    if [[ "$relative_path" == "$manifest_sentinel" ]]; then
      manifest_complete=1
      continue
    fi
    if ! validate_legacy_runtime_relative_path "$relative_path"; then
      err "Unsafe legacy runtime path: $relative_path"
      return 1
    fi
    source_path="$repository/$relative_path"
    target_path="$LEGACY_QUARANTINE/$relative_path"
    if [[ -L "$source_path" || ! -f "$source_path" ]]; then
      err "Refusing unexpected legacy runtime entry: $source_path"
      return 1
    fi
  done <"$LEGACY_MANIFEST"
  if [[ "$manifest_complete" != "1" ]]; then
    err "Legacy runtime manifest is incomplete"
    return 1
  fi
  while IFS= read -r -d '' relative_path; do
    [[ "$relative_path" == "$manifest_sentinel" ]] && continue
    source_path="$repository/$relative_path"
    target_path="$LEGACY_QUARANTINE/$relative_path"
    if ! mkdir -p "$(dirname "$target_path")" \
      || ! mv "$source_path" "$target_path"; then
      err "Could not quarantine legacy runtime path: $relative_path"
      return 1
    fi
  done <"$LEGACY_MANIFEST"
}

restore_legacy_runtime_state() {
  local repository=$1
  local relative_path
  local source_path
  local target_path
  local manifest_path
  local quarantine_path
  local manifest_complete=0
  local readonly manifest_sentinel=WB_IRRIGATION_LEGACY_MANIFEST_COMPLETE

  [[ "${LEGACY_CLEANUP_STARTED:-0}" == "1" ]] || return 0
  manifest_path="${BACKUP_DIR:-}/legacy-runtime.manifest"
  quarantine_path="${BACKUP_DIR:-}/legacy-runtime"
  if [[ -z "${BACKUP_DIR:-}" || -L "$manifest_path" || ! -f "$manifest_path" \
    || -L "$quarantine_path" || ! -d "$quarantine_path" ]]; then
    err "Legacy runtime rollback manifest is unavailable"
    return 1
  fi
  while IFS= read -r -d '' relative_path; do
    if [[ "$relative_path" == "$manifest_sentinel" ]]; then
      manifest_complete=1
      continue
    fi
    if ! validate_legacy_runtime_relative_path "$relative_path"; then
      err "Unsafe legacy rollback manifest entry: $relative_path"
      return 1
    fi
    source_path="$quarantine_path/$relative_path"
    target_path="$repository/$relative_path"
    if [[ -L "$source_path" || ! -f "$source_path" ]]; then
      err "Legacy rollback source is not a regular file: $source_path"
      return 1
    fi
    if [[ -e "$target_path" || -L "$target_path" ]]; then
      err "Legacy runtime rollback target already exists: $target_path"
      return 1
    fi
  done <"$manifest_path"
  if [[ "$manifest_complete" != "1" ]]; then
    err "Legacy rollback manifest is incomplete"
    return 1
  fi
  while IFS= read -r -d '' relative_path; do
    [[ "$relative_path" == "$manifest_sentinel" ]] && continue
    source_path="$quarantine_path/$relative_path"
    target_path="$repository/$relative_path"
    if ! ensure_safe_repository_parent "$repository" "$(dirname -- "$relative_path")"; then
      return 1
    fi
    if ! mv "$source_path" "$target_path"; then
      err "Could not restore legacy runtime path: $relative_path"
      return 1
    fi
  done <"$manifest_path"
}

activate_state_layout_marker() {
  if ! install -m 0600 /dev/null "$DEPLOY_CONTROL_DIR/layout-v1"; then
    err "Could not publish trusted state layout marker"
    return 1
  fi
}

quarantine_failed_state_layout() {
  local failed_state

  [[ "${STATE_ACTIVATED:-0}" == "1" ]] || return 0
  if [[ -L "$STATE_DIR" || ! -d "$STATE_DIR" ]]; then
    err "Cannot quarantine failed state layout"
    return 1
  fi
  failed_state="${STATE_DIR}.failed-${STAMP:-rollback}-$$"
  if [[ -e "$failed_state" || -L "$failed_state" ]]; then
    err "Failed-state quarantine already exists: $failed_state"
    return 1
  fi
  mv "$STATE_DIR" "$failed_state"
  RECOVERY_STATE_DIR=$failed_state
}

rollback_update() {
  local rollback_failed=0
  local database_restore_failed=0

  warn "Update failed; restoring previous code and runtime"
  if [[ "${CODE_UPDATED:-0}" == "1" || "${SERVICE_WAS_ACTIVE:-0}" == "1" ]]; then
    if ! systemctl stop "${SERVICE:-wb-irrigation}"; then
      err "Rollback could not stop ${SERVICE:-wb-irrigation}"
      rollback_failed=1
    fi
  fi

  if [[ "${VENV_SWAPPED:-0}" == "1" ]]; then
    if [[ -e "${VENV_DIR:-}" ]] && ! rm -rf -- "$VENV_DIR"; then
      err "Rollback could not remove staged live venv"
      rollback_failed=1
    fi
    if [[ "${HAD_OLD_VENV:-0}" == "1" && -e "${OLD_VENV_BACKUP:-}" ]]; then
      if ! mv "$OLD_VENV_BACKUP" "$VENV_DIR"; then
        err "Rollback could not restore previous venv"
        rollback_failed=1
      fi
    fi
  fi
  if [[ -n "${STAGED_VENV:-}" && -e "${STAGED_VENV:-}" ]] && ! rm -rf -- "$STAGED_VENV"; then
    err "Rollback could not remove incomplete staged venv"
    rollback_failed=1
  fi

  if [[ "${CODE_UPDATED:-0}" == "1" ]]; then
    if ! git -C "$REPO_DIR_REAL" reset --hard "$ORIGINAL_COMMIT" -q; then
      err "Rollback could not restore Git revision $ORIGINAL_COMMIT"
      rollback_failed=1
    fi
  fi
  if ! restore_state_snapshot; then
    rollback_failed=1
  fi
  if [[ "${DATABASES_MAY_HAVE_MUTATED:-0}" == "1" \
    && -n "${DB_PATH:-}" && -n "${DB_BACKUP:-}" ]]; then
    if ! restore_sqlite_database "$DB_PATH" "$DB_BACKUP" \
      "${DB_EXISTED_BEFORE:-0}" "${DB_BACKUP_VERIFIED:-0}" "irrigation.db"; then
      rollback_failed=1
      database_restore_failed=1
    fi
  fi
  if [[ "${DATABASES_MAY_HAVE_MUTATED:-0}" == "0" || "$database_restore_failed" == "0" ]]; then
    if ! cleanup_created_mqtt_tls_material; then
      rollback_failed=1
    fi
  fi
  if [[ "${DATABASES_MAY_HAVE_MUTATED:-0}" == "1" \
    && -n "${JOBS_DB_PATH:-}" && -n "${JOBS_DB_BACKUP:-}" ]]; then
    if ! restore_sqlite_database "$JOBS_DB_PATH" "$JOBS_DB_BACKUP" \
      "${JOBS_DB_EXISTED_BEFORE:-0}" "${JOBS_DB_BACKUP_VERIFIED:-0}" "jobs.db"; then
      rollback_failed=1
    fi
  fi
  if [[ -d "${STATE_DIR:-}" ]]; then
    if ! normalize_state_permissions; then
      rollback_failed=1
    fi
  fi

  if [[ "${UNIT_CHANGED:-0}" == "1" ]]; then
    if [[ "${SERVICE_ENABLE_CHANGED:-0}" == "1" ]]; then
      systemctl disable "${SERVICE:-wb-irrigation}" || rollback_failed=1
    fi
    if [[ "${HAD_SERVICE_FILE:-0}" == "1" ]]; then
      install -m 0644 "$SERVICE_FILE_BACKUP" "$SERVICE_FILE" || rollback_failed=1
    else
      rm -f -- "$SERVICE_FILE" || rollback_failed=1
    fi
    systemctl daemon-reload || rollback_failed=1
  fi
  if [[ "${LOGROTATE_CHANGED:-0}" == "1" ]]; then
    if [[ "${HAD_LOGROTATE_FILE:-0}" == "1" ]]; then
      install -m 0644 "$LOGROTATE_FILE_BACKUP" "$LOGROTATE_TARGET" || rollback_failed=1
    else
      rm -f -- "$LOGROTATE_TARGET" || rollback_failed=1
    fi
  fi
  if [[ "${ENV_CHANGED:-0}" == "1" ]]; then
    if [[ "${HAD_ENV_FILE:-0}" == "1" ]]; then
      install -m 0640 "$ENV_FILE_BACKUP" "$ENV_FILE" || rollback_failed=1
      chown root:"${SERVICE_GROUP:-wb-irrigation}" "$ENV_FILE" || rollback_failed=1
    else
      rm -f -- "$ENV_FILE" || rollback_failed=1
    fi
  fi
  if [[ "${RELEASE_ENV_CHANGED:-0}" == "1" ]]; then
    if [[ "${HAD_RELEASE_ENV_FILE:-0}" == "1" ]]; then
      install -m 0640 "$RELEASE_ENV_FILE_BACKUP" "$RELEASE_ENV_FILE" || rollback_failed=1
      chown root:"${SERVICE_GROUP:-wb-irrigation}" "$RELEASE_ENV_FILE" || rollback_failed=1
    else
      rm -f -- "$RELEASE_ENV_FILE" || rollback_failed=1
    fi
  fi

  if ! restore_legacy_runtime_state "$REPO_DIR_REAL"; then
    rollback_failed=1
  fi
  if ! quarantine_failed_state_layout; then
    rollback_failed=1
  fi

  if [[ "$rollback_failed" == "0" && "${ORIGINAL_USES_STATE:-0}" == "1" ]]; then
    if ! secure_code_tree "$REPO_DIR_REAL"; then
      err "Rollback could not restore non-root-readable code permissions"
      rollback_failed=1
    fi
    chown root:root /opt/wb-irrigation || rollback_failed=1
    chmod 0755 /opt/wb-irrigation || rollback_failed=1
    if [[ "$rollback_failed" == "0" ]] && ! verify_service_runtime_access; then
      err "Rollback runtime is not accessible under the dedicated service account"
      rollback_failed=1
    fi
  fi

  if [[ "$rollback_failed" == "0" && "${SERVICE_WAS_ACTIVE:-0}" == "1" \
    && "${DEFER_ROLLBACK_RESTART:-0}" != "1" ]]; then
    if ! systemctl start "${SERVICE:-wb-irrigation}"; then
      err "Rollback could not restart ${SERVICE:-wb-irrigation}"
      rollback_failed=1
    fi
  fi
  if [[ "$rollback_failed" == "1" ]]; then
    err "Rollback completed with errors; manual recovery from ${BACKUP_DIR:-<unknown>} is required"
    return 1
  fi
  ok "Previous code and runtime restored"
}

finish_update() {
  local status=$?
  trap - EXIT
  if [[ "$status" -ne 0 && "${UPDATE_SUCCEEDED:-0}" != "1" ]]; then
    rollback_update || true
  fi
  exit "$status"
}

initialize_rollback_state() {
  UPDATE_SUCCEEDED=0
  SERVICE_WAS_ACTIVE=$HANDOFF_SERVICE_WAS_ACTIVE
  ORIGINAL_USES_STATE=0
  STATE_ACTIVATED=0
  STATE_SNAPSHOT_CREATED=0
  LEGACY_CLEANUP_STARTED=0
  CODE_UPDATED=0
  VENV_SWAPPED=0
  HAD_OLD_VENV=0
  UNIT_CHANGED=0
  SERVICE_ENABLE_CHANGED=0
  LOGROTATE_CHANGED=0
  ENV_CHANGED=0
  RELEASE_ENV_CHANGED=0
  HAD_SERVICE_FILE=0
  HAD_LOGROTATE_FILE=0
  HAD_ENV_FILE=0
  HAD_RELEASE_ENV_FILE=0
  DB_EXISTED_BEFORE=0
  DB_BACKUP_VERIFIED=0
  JOBS_DB_EXISTED_BEFORE=0
  JOBS_DB_BACKUP_VERIFIED=0
  DATABASES_MAY_HAVE_MUTATED=0
  MQTT_TLS_CREATED_FILES=()
  MQTT_TLS_DIR_CREATED=0
  VENV_DIR="$REPO_DIR_REAL/venv"
  STAGED_VENV=""
  OLD_VENV_BACKUP=""
  BACKUP_DIR=""
  STATE_ARCHIVE=""
  LEGACY_QUARANTINE=""
  LEGACY_MANIFEST=""
  DB_PATH="$STATE_DIR/irrigation.db"
  DB_BACKUP=""
  JOBS_DB_PATH="$STATE_DIR/jobs.db"
  JOBS_DB_BACKUP=""
  SERVICE_FILE="/etc/systemd/system/${SERVICE}.service"
  SERVICE_FILE_BACKUP=""
  LOGROTATE_TARGET="/etc/logrotate.d/wb-irrigation"
  LOGROTATE_FILE_BACKUP=""
  ENV_FILE_BACKUP=""
  RELEASE_ENV_FILE_BACKUP=""
}

# Unit tests source the real helper functions and inject failures without
# executing the privileged deployment body.
if [[ "${BASH_SOURCE[0]}" != "$0" ]]; then
  return 0
fi

if [[ "$(id -u)" -ne 0 ]]; then
  err "Run as root (sudo bash $0)"
  exit 1
fi
validate_fixed_deployment_contract
require_full_commit_sha "$TARGET_COMMIT"
validate_branch_name
acquire_deploy_lock
if [[ ! -d "$REPO_DIR/.git" ]]; then
  err "Git repository not found: $REPO_DIR"
  exit 1
fi
if [[ ! "$BACKUP_KEEP" =~ ^[0-9]+$ ]] || ((BACKUP_KEEP < 1)); then
  err "BACKUP_KEEP must be a positive integer"
  exit 1
fi

REPO_DIR_REAL=$(cd "$REPO_DIR" && pwd -P)
if [[ "$REPO_DIR_REAL" != "$FIXED_REPO_DIR_REAL" ]]; then
  err "Legacy code layout must be migrated to $FIXED_REPO_DIR_REAL before this updater can run"
  exit 1
fi
cd "$REPO_DIR_REAL"
info "Target repo: $REPO_DIR_REAL (authorization branch: $BRANCH, commit: $TARGET_COMMIT)"

if [[ -n $(git status --porcelain --untracked-files=normal) ]]; then
  err "Deployment checkout has local changes; refusing an update that cannot be rolled back exactly"
  exit 1
fi
ORIGINAL_COMMIT=$(git rev-parse --verify HEAD)
fetch_authorization_branch "$REPO_DIR_REAL"
RESOLVED_TARGET=$(resolve_authorized_target_commit "$REPO_DIR_REAL" "$TARGET_COMMIT")
# Integration ordering contract: the Telegram runtime must stop creating the
# legacy file before this deploy migration is allowed to purge it.
require_retired_telegram_runtime_commit "$REPO_DIR_REAL" "$RESOLVED_TARGET"

initialize_rollback_state
ensure_deploy_control_dir
TRUSTED_LAYOUT_MARKER=0
if [[ -e "$DEPLOY_CONTROL_DIR/layout-v1" || -L "$DEPLOY_CONTROL_DIR/layout-v1" ]]; then
  validate_layout_marker
  TRUSTED_LAYOUT_MARKER=1
fi
if [[ -f "$SERVICE_FILE" ]]; then
  if grep -Fxq "User=$SERVICE_USER" "$SERVICE_FILE" \
    && grep -Fxq "WorkingDirectory=$STATE_DIR" "$SERVICE_FILE"; then
    ORIGINAL_USES_STATE=1
  elif grep -Fq "User=$SERVICE_USER" "$SERVICE_FILE" \
    || grep -Fq "WorkingDirectory=$STATE_DIR" "$SERVICE_FILE"; then
    err "Installed unit contains a partial non-root state contract; refusing migration"
    exit 1
  fi
elif [[ "$TRUSTED_LAYOUT_MARKER" == "1" ]]; then
  ORIGINAL_USES_STATE=1
elif [[ -d "$STATE_DIR" && ! -L "$STATE_DIR" \
  && -d "$STATE_DIR/backups" \
  && -d "$STATE_DIR/static/media/maps" \
  && -d "$STATE_DIR/static/media/zones" ]]; then
  warn "Migrating a pre-control-directory state layout"
  ORIGINAL_USES_STATE=1
fi
trap finish_update EXIT

ensure_service_account

if systemctl is-active "$SERVICE" >/dev/null 2>&1; then
  SERVICE_WAS_ACTIVE=1
  info "Stopping $SERVICE for a consistent backup"
  systemctl stop "$SERVICE"
  if systemctl is-active "$SERVICE" >/dev/null 2>&1; then
    err "Service did not stop: $SERVICE"
    exit 1
  fi
fi

retire_telegram_file_logs "$REPO_DIR_REAL"

if [[ "$ORIGINAL_USES_STATE" == "1" ]]; then
  prepare_state_layout "$REPO_DIR_REAL" 0
else
  prepare_state_layout "$REPO_DIR_REAL" 1
fi
normalize_state_permissions

BACKUP_BASE=${BACKUP_BASE:-"${DEPLOY_CONTROL_DIR}/backups"}
canonicalize_backup_base
mkdir -p -m 700 "$BACKUP_BASE"
chmod 700 "$BACKUP_BASE"
STAMP=$(date +"%Y%m%d_%H%M%S")
BACKUP_DIR="$BACKUP_BASE/$STAMP"
mkdir -m 700 "$BACKUP_DIR"
info "Creating verified backup at $BACKUP_DIR"

DB_BACKUP="$BACKUP_DIR/irrigation.db"
JOBS_DB_BACKUP="$BACKUP_DIR/jobs.db"
if [[ -f "$DB_PATH" ]]; then
  DB_EXISTED_BEFORE=1
  backup_sqlite_database "$DB_PATH" "$DB_BACKUP" "irrigation.db"
  DB_BACKUP_VERIFIED=1
else
  warn "Database does not exist yet; code and secrets will still be backed up"
fi
if [[ -f "$JOBS_DB_PATH" ]]; then
  JOBS_DB_EXISTED_BEFORE=1
  backup_sqlite_database "$JOBS_DB_PATH" "$JOBS_DB_BACKUP" "jobs.db"
  JOBS_DB_BACKUP_VERIFIED=1
fi

for secret_file in "$STATE_DIR/.secret_key" "$STATE_DIR/.irrig_secret_key" "$STATE_DIR/.env"; do
  if [[ -f "$secret_file" ]]; then
    chmod 600 "$secret_file"
  fi
done

ARCHIVE="$BACKUP_DIR/repo_snapshot.tar.gz"
tar --exclude="./venv" --exclude="./.git" --exclude="./__pycache__" \
  --exclude="./backups" --exclude="./*.pid" \
  --exclude="./irrigation.db" --exclude="./irrigation.db-wal" --exclude="./irrigation.db-shm" \
  --exclude="./jobs.db" --exclude="./jobs.db-wal" --exclude="./jobs.db-shm" \
  -czf "$ARCHIVE" .
chmod 600 "$ARCHIVE"

STATE_ARCHIVE="$BACKUP_DIR/state_snapshot.tar.gz"
tar \
  --exclude="./irrigation.db" --exclude="./irrigation.db-wal" --exclude="./irrigation.db-shm" \
  --exclude="./jobs.db" --exclude="./jobs.db-wal" --exclude="./jobs.db-shm" \
  -czf "$STATE_ARCHIVE" -C "$STATE_DIR" .
chmod 600 "$STATE_ARCHIVE"
STATE_SNAPSHOT_CREATED=1

SERVICE_FILE_BACKUP="$BACKUP_DIR/systemd-unit.service"
LOGROTATE_FILE_BACKUP="$BACKUP_DIR/logrotate.conf"
ENV_FILE_BACKUP="$BACKUP_DIR/environment.env"
RELEASE_ENV_FILE_BACKUP="$BACKUP_DIR/release.env"
if [[ -f "$SERVICE_FILE" ]]; then
  HAD_SERVICE_FILE=1
  install -m 0600 "$SERVICE_FILE" "$SERVICE_FILE_BACKUP"
fi
if [[ -f "$LOGROTATE_TARGET" ]]; then
  HAD_LOGROTATE_FILE=1
  install -m 0600 "$LOGROTATE_TARGET" "$LOGROTATE_FILE_BACKUP"
fi
if [[ -f "$ENV_FILE" ]]; then
  HAD_ENV_FILE=1
  chmod 600 "$ENV_FILE"
  install -m 0600 "$ENV_FILE" "$ENV_FILE_BACKUP"
fi
if [[ -f "$RELEASE_ENV_FILE" ]]; then
  HAD_RELEASE_ENV_FILE=1
  install -m 0600 "$RELEASE_ENV_FILE" "$RELEASE_ENV_FILE_BACKUP"
fi
ok "Verified backup created: $BACKUP_DIR"
migrate_mqtt_tls_paths "$DB_PATH" "$REPO_DIR_REAL"

info "Activating target code $RESOLVED_TARGET"
CODE_UPDATED=1
git reset --hard "$RESOLVED_TARGET" -q
ok "Code updated to $(git rev-parse --short HEAD)"

find . -path ./venv -prune -o -path ./.git -prune -o -type d -name __pycache__ -print -exec rm -rf -- {} +

install_pinned_uv
export PATH="$(dirname "$UV_BIN"):$PATH"
export UV_PYTHON_INSTALL_DIR="$PYTHON_INSTALL_DIR"
"$UV_BIN" python install "$PY_VERSION"
PY_BIN=$("$UV_BIN" python find "$PY_VERSION")
if [[ ! -x "$PY_BIN" || "$PY_BIN" != "$PYTHON_INSTALL_DIR"/* ]]; then
  err "uv Python is not installed in the shared runtime directory: $PY_BIN"
  exit 1
fi
secure_python_runtime

STAGED_VENV="$REPO_DIR_REAL/.venv-staged-$STAMP-$$"
OLD_VENV_BACKUP="$REPO_DIR_REAL/.venv-rollback-$STAMP-$$"
if [[ ! -f "$REPO_DIR_REAL/requirements.lock" ]]; then
  err "Production dependency lock is missing: $REPO_DIR_REAL/requirements.lock"
  exit 1
fi
"$UV_BIN" venv --python "$PY_BIN" "$STAGED_VENV"
"$UV_BIN" pip install --python "$STAGED_VENV/bin/python" --require-hashes -r requirements.lock
STAGED_VERSION=$("$STAGED_VENV/bin/python" -c 'import sys; print(".".join(map(str, sys.version_info[:3])))')
if [[ "$STAGED_VERSION" != "$PY_VERSION" ]]; then
  err "Staged venv uses Python $STAGED_VERSION instead of $PY_VERSION"
  exit 1
fi

if [[ -e "$VENV_DIR" ]]; then
  mv "$VENV_DIR" "$OLD_VENV_BACKUP"
  HAD_OLD_VENV=1
fi
VENV_SWAPPED=1
mv "$STAGED_VENV" "$VENV_DIR"
mkdir -p "$REPO_DIR_REAL/static/media/maps" "$REPO_DIR_REAL/static/media/zones"
secure_code_tree "$REPO_DIR_REAL"
chown root:root /opt/wb-irrigation
chmod 0755 /opt/wb-irrigation
ok "Staged runtime activated with $($VENV_DIR/bin/python -V)"

REPO_UNIT="$REPO_DIR_REAL/wb-irrigation.service"
if [[ ! -f "$REPO_UNIT" ]]; then
  err "Repository systemd unit is missing: $REPO_UNIT"
  exit 1
fi
if ! cmp -s "$REPO_UNIT" "$SERVICE_FILE" 2>/dev/null; then
  UNIT_CHANGED=1
  install -m 0644 "$REPO_UNIT" "$SERVICE_FILE"
  systemctl daemon-reload
fi

LOGROTATE_SOURCE="$REPO_DIR_REAL/configs/logrotate.d/wb-irrigation"
if [[ ! -f "$LOGROTATE_SOURCE" ]]; then
  err "Repository logrotate config is missing: $LOGROTATE_SOURCE"
  exit 1
fi
if ! cmp -s "$LOGROTATE_SOURCE" "$LOGROTATE_TARGET" 2>/dev/null; then
  LOGROTATE_CHANGED=1
  install -m 0644 "$LOGROTATE_SOURCE" "$LOGROTATE_TARGET"
fi
ensure_logrotate_ready

CURRENT_COMMIT=$(git rev-parse HEAD)
ENV_CHANGED=1
RELEASE_ENV_CHANGED=1
quarantine_legacy_runtime_state "$REPO_DIR_REAL"
merge_legacy_env_defaults "$STATE_DIR/.env" "$ENV_FILE"
merge_legacy_env_defaults "$LEGACY_QUARANTINE/.env" "$ENV_FILE"
upsert_env_value GIT_COMMIT "$CURRENT_COMMIT" "$ENV_FILE"
publish_release_environment "$CURRENT_COMMIT"
if [[ ${PORT+x} ]]; then
  VALIDATED_PORT=$(configured_port "$ENV_FILE")
  upsert_env_value PORT "$VALIDATED_PORT" "$ENV_FILE"
fi
persist_http_environment "$ENV_FILE"
chown root:"$SERVICE_GROUP" "$ENV_FILE"
chmod 0640 "$ENV_FILE"
validate_http_transport_contract "$ENV_FILE"
verify_service_runtime_access
info "GIT_COMMIT updated in $ENV_FILE -> ${CURRENT_COMMIT:0:12}"

if ! systemctl is-enabled "$SERVICE" >/dev/null 2>&1; then
  if [[ "$HAD_SERVICE_FILE" == "0" ]]; then
    systemctl enable "$SERVICE"
    SERVICE_ENABLE_CHANGED=1
  else
    err "Existing service is not enabled: $SERVICE"
    exit 1
  fi
fi
DATABASES_MAY_HAVE_MUTATED=1
systemctl restart "$SERVICE"
if ! systemctl is-active "$SERVICE" >/dev/null 2>&1; then
  err "Service failed to start. Check: journalctl -u $SERVICE -n 200"
  exit 1
fi

APP_PORT=$(configured_port "$ENV_FILE")
configure_ready_probe "$ENV_FILE"
info "Waiting up to 30s for $APP_READY_URL"
for attempt in $(seq 1 15); do
  if probe_ready_endpoint; then
    ok "/readyz returned a successful HTTP status (attempt $attempt)"
    break
  fi
  if [[ "$attempt" -eq 15 ]]; then
    err "/readyz failed on configured PORT=$APP_PORT"
    exit 1
  fi
  sleep 2
done

activate_state_layout_marker
normalize_state_permissions
prune_old_backups
UPDATE_SUCCEEDED=1
VENV_SWAPPED=0
if [[ "$HAD_OLD_VENV" == "1" ]] && ! rm -rf -- "$OLD_VENV_BACKUP"; then
  warn "Update succeeded, but old venv cleanup failed: $OLD_VENV_BACKUP"
fi
if [[ -n "$LEGACY_QUARANTINE" ]] && ! rm -rf -- "$LEGACY_QUARANTINE" "$LEGACY_MANIFEST"; then
  warn "Update succeeded, but legacy quarantine cleanup failed: $LEGACY_QUARANTINE"
fi
ok "Update completed successfully. Backup stored at: $BACKUP_DIR"
