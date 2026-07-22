#!/usr/bin/env bash
set -euo pipefail
umask 077

# Uninstaller for the fixed native Wirenboard deployment. By default it removes
# service integration only and preserves application state. Destructive data
# removal requires the explicit --purge-data option.

SERVICE_NAME=wb-irrigation
SERVICE_FILE=/etc/systemd/system/wb-irrigation.service
LOGROTATE_TARGET=/etc/logrotate.d/wb-irrigation
APP_ROOT=/opt/wb-irrigation
APP_LINK=/opt/wb-irrigation/irrigation
DATA_DIR=/mnt/data/wb-irrigation
STATE_DIR=/mnt/data/wb-irrigation-state
PYTHON_INSTALL_DIR=/mnt/data/wb-irrigation-python
SERVICE_USER=wb-irrigation
SERVICE_GROUP=wb-irrigation
DEPLOY_LOCK_FILE=${DEPLOY_LOCK_FILE:-/run/lock/wb-irrigation-deploy.lock}
readonly FIXED_DEPLOY_LOCK_FILE=/run/lock/wb-irrigation-deploy.lock

ASSUME_YES=0
PURGE_DATA=0

usage() {
  cat <<'EOF'
Usage: sudo bash uninstall_wb.sh [--yes] [--purge-data]

Without --purge-data the service unit and logrotate configuration are removed,
but code, state, shared Python and /opt/wb-irrigation/.env are preserved.

--yes         confirm removal of service integration without a prompt
--purge-data  also remove code, /mnt/data/wb-irrigation-state, the shared
              /mnt/data/wb-irrigation-python runtime and the service account;
              this irreversible option has its own explicit confirmation
EOF
}

err() { echo "ERROR: $*" >&2; }

acquire_deploy_lock() {
  local inherited_fd=${WB_IRRIGATION_DEPLOY_LOCK_FD:-}
  local inherited_path=""

  if ! command -v flock >/dev/null 2>&1; then
    err "flock is required to serialize install, update and uninstall operations"
    return 1
  fi
  if [[ -n "$inherited_fd" ]]; then
    if [[ "$inherited_fd" != "9" ]] || ! : >&9 2>/dev/null; then
      err "Invalid inherited deployment lock descriptor"
      return 1
    fi
    if [[ -e "/proc/$$/fd/9" ]]; then
      inherited_path=$(readlink -f "/proc/$$/fd/9")
      if [[ "$inherited_path" != "$DEPLOY_LOCK_FILE" ]]; then
        err "Inherited descriptor does not reference $DEPLOY_LOCK_FILE"
        return 1
      fi
    fi
    if ! flock -n 9; then
      err "Inherited deployment lock is invalid"
      return 1
    fi
    return 0
  fi

  exec 9>"$DEPLOY_LOCK_FILE"
  chmod 0600 "$DEPLOY_LOCK_FILE"
  if ! flock -n 9; then
    err "Another install, update or uninstall operation holds $DEPLOY_LOCK_FILE"
    return 1
  fi
  export WB_IRRIGATION_DEPLOY_LOCK_FD=9
}

confirm_service_removal() {
  local answer

  if [[ "$ASSUME_YES" == "1" ]]; then
    return 0
  fi
  read -r -p "Type 'uninstall' to remove the wb-irrigation service integration: " answer
  [[ "$answer" == "uninstall" ]]
}

confirm_data_purge() {
  local answer

  if [[ "$PURGE_DATA" != "1" ]]; then
    return 0
  fi
  if [[ "$ASSUME_YES" == "1" ]]; then
    # Supplying both flags is the non-interactive explicit confirmation.
    return 0
  fi
  echo "This permanently removes databases, backups, code and environment files."
  read -r -p "Type 'PURGE wb-irrigation' to continue: " answer
  [[ "$answer" == "PURGE wb-irrigation" ]]
}

remove_service_integration() {
  if systemctl is-active "$SERVICE_NAME" >/dev/null 2>&1; then
    systemctl stop "$SERVICE_NAME"
  fi
  if systemctl is-enabled "$SERVICE_NAME" >/dev/null 2>&1; then
    systemctl disable "$SERVICE_NAME"
  fi
  rm -f -- "$SERVICE_FILE"
  rm -f -- "$LOGROTATE_TARGET"
  systemctl daemon-reload
  systemctl reset-failed "$SERVICE_NAME" >/dev/null 2>&1 || true
}

purge_application_data() {
  local passwd_entry account_name password uid gid gecos account_home account_shell
  local group_entry group_gid group_members conflicting_primary

  # These are fixed, validated deployment paths; never derive recursive-delete
  # targets from symlinks, globs or environment variables.
  if [[ "$DATA_DIR" != "/mnt/data/wb-irrigation" || "$STATE_DIR" != "/mnt/data/wb-irrigation-state" \
    || "$PYTHON_INSTALL_DIR" != "/mnt/data/wb-irrigation-python" \
    || "$APP_ROOT" != "/opt/wb-irrigation" ]]; then
    err "Refusing to purge unexpected paths"
    return 1
  fi
  if id "$SERVICE_USER" >/dev/null 2>&1; then
    passwd_entry=$(getent passwd "$SERVICE_USER")
    IFS=: read -r account_name password uid gid gecos account_home account_shell <<<"$passwd_entry"
    if [[ "$uid" == "0" || "$gid" == "0" || "$account_home" != "$STATE_DIR" \
      || "$account_shell" != "/usr/sbin/nologin" \
      || "$(id -gn "$SERVICE_USER")" != "$SERVICE_GROUP" ]]; then
      err "Refusing to remove an account that does not match the dedicated service contract"
      return 1
    fi
  fi
  if getent group "$SERVICE_GROUP" >/dev/null 2>&1; then
    group_entry=$(getent group "$SERVICE_GROUP")
    group_gid=$(printf '%s' "$group_entry" | cut -d: -f3)
    group_members=$(printf '%s' "$group_entry" | cut -d: -f4)
    conflicting_primary=$(getent passwd | awk -F: -v gid="$group_gid" -v user="$SERVICE_USER" \
      '$4 == gid && $1 != user {print $1; exit}')
    if [[ "$group_gid" == "0" || ( -n "$group_members" && "$group_members" != "$SERVICE_USER" ) \
      || -n "$conflicting_primary" ]]; then
      err "Refusing to remove a group that is not dedicated to wb-irrigation"
      return 1
    fi
  fi
  rm -rf -- "$DATA_DIR"
  rm -rf -- "$STATE_DIR"
  rm -rf -- "$PYTHON_INSTALL_DIR"
  rm -rf -- "$APP_ROOT"
  if id "$SERVICE_USER" >/dev/null 2>&1; then
    userdel "$SERVICE_USER"
  fi
  if getent group "$SERVICE_GROUP" >/dev/null 2>&1; then
    groupdel "$SERVICE_GROUP"
  fi
}

# Tests source the real locking helper without running privileged operations.
if [[ "${BASH_SOURCE[0]}" != "$0" ]]; then
  return 0
fi

while [[ $# -gt 0 ]]; do
  case "$1" in
    -h | --help)
      usage
      exit 0
      ;;
    -y | --yes)
      ASSUME_YES=1
      shift
      ;;
    --purge-data)
      PURGE_DATA=1
      shift
      ;;
    *)
      err "Unknown argument: $1"
      usage
      exit 1
      ;;
  esac
done

if [[ "$(id -u)" -ne 0 ]]; then
  err "Please run as root (sudo bash $0)"
  exit 1
fi
if [[ "$DEPLOY_LOCK_FILE" != "$FIXED_DEPLOY_LOCK_FILE" ]]; then
  err "DEPLOY_LOCK_FILE override is unsupported for production uninstall"
  exit 1
fi
if ! confirm_service_removal; then
  err "Uninstall cancelled"
  exit 1
fi
if ! confirm_data_purge; then
  err "Data purge cancelled"
  exit 1
fi

acquire_deploy_lock
remove_service_integration

if [[ "$PURGE_DATA" == "1" ]]; then
  purge_application_data
  echo "WB-Irrigation service, logrotate configuration and application data removed."
else
  echo "WB-Irrigation service and logrotate configuration removed; данные сохранены."
  echo "Preserved: $DATA_DIR, $STATE_DIR, $PYTHON_INSTALL_DIR and $APP_ROOT (including .env)."
  echo "Run again with --purge-data for irreversible removal."
fi
