#!/usr/bin/env bash
# Update an existing OTA service without running the full installer.
set -euo pipefail
source_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"

usage() {
  printf 'Usage: %s SSH_HOST [INSTALL_DIR]\n' "${0##*/}"
  printf 'Defaults: use the service WorkingDirectory and check port 4242.\n'
  printf 'Set OTA_UPDATE_PORT for a service listening on a different port.\n'
}

if [[ ${1:-} == --help || ${1:-} == -h ]]; then
  usage
  exit 0
fi
if (( $# < 1 || $# > 2 )); then
  usage >&2
  exit 2
fi

host=$1
expected_dir=${2:-}
port=${OTA_UPDATE_PORT:-4242}
if [[ ! "$host" =~ ^[A-Za-z0-9_][A-Za-z0-9_.-]*(@[A-Za-z0-9_][A-Za-z0-9_.-]*)?$ ]]; then
  printf 'Invalid SSH host. Use a hostname, user@host or SSH config alias.\n' >&2
  exit 2
fi
if [[ -n "$expected_dir" && ( ! "$expected_dir" =~ ^/[A-Za-z0-9_./-]+$ || "$expected_dir" == / ) ]]; then
  printf 'INSTALL_DIR must be an absolute path without spaces.\n' >&2
  exit 2
fi
if [[ ! "$port" =~ ^[1-9][0-9]{0,4}$ ]] || (( port > 65535 )); then
  printf 'Invalid OTA_UPDATE_PORT.\n' >&2
  exit 2
fi

stage=$(ssh -- "$host" 'mktemp -d /tmp/mindora-ota-update.XXXXXXXX')
if [[ ! "$stage" =~ ^/tmp/mindora-ota-update\.[A-Za-z0-9]{8}$ ]]; then
  printf 'Unexpected remote staging path.\n' >&2
  exit 1
fi
trap 'ssh -- "$host" "rm -rf -- $stage" || true' EXIT
rsync -rt -- "$source_dir/ota_server.py" "$source_dir/ota_reports.py" "$host:$stage/"

ssh -- "$host" "if [ \"\$(id -u)\" -eq 0 ]; then
  exec bash -s -- '$stage' '$expected_dir' '$port'
else
  exec sudo -n -- bash -s -- '$stage' '$expected_dir' '$port'
fi" <<'REMOTE'
set -euo pipefail
stage=$1
expected_dir=$2
port=$3
service=ota-server.service
files=(ota_server.py ota_reports.py)

install_dir=$(systemctl show --property=WorkingDirectory --value "$service")
entry=$(systemctl show --property=ExecStart --value "$service")
if [[ "$install_dir" != /* || "$install_dir" == / || ! -d "$install_dir" ||
      ! "$entry" =~ [[:space:]]ota_server:app([[:space:];]|$) ]]; then
  printf 'Expected an installed %s running ota_server:app. Use deploy.sh to install or switch the unit.\n' "$service" >&2
  exit 1
fi
if [[ -n "$expected_dir" && "${expected_dir%/}" != "${install_dir%/}" ]]; then
  printf 'WorkingDirectory is %s, expected %s. Update aborted.\n' "$install_dir" "$expected_dir" >&2
  exit 1
fi

exec 9>"$install_dir/.server-update.lock"
flock -n 9 || { printf 'An OTA service update is already running.\n' >&2; exit 1; }

work=$(mktemp -d "$install_dir/.server-update.XXXXXXXX")
rollback=0
cleanup() {
  local status=$?
  if (( rollback )); then
    printf 'Update failed. Restoring the previous OTA application.\n' >&2
    if mv -fT -- "$work/previous/ota_server.py" "$install_dir/ota_server.py" &&
       mv -fT -- "$work/previous/ota_reports.py" "$install_dir/ota_reports.py" &&
       systemctl restart "$service"; then
      printf 'Previous files restored and service restarted.\n' >&2
    else
      printf 'Rollback failed. Inspect %s and the service journal.\n' "$work" >&2
      return 1
    fi
  fi
  rm -rf -- "$work"
  return "$status"
}
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

mkdir -- "$work/new" "$work/previous"
for file in "${files[@]}"; do
  install -m 0644 -- "$stage/$file" "$work/new/$file"
  cp -p -- "$install_dir/$file" "$work/previous/$file"
done
(
  cd -- "$work/new"
  PYTHONDONTWRITEBYTECODE=1 "$install_dir/.venv/bin/python" -c 'import ota_server'
)

rollback=1
for file in "${files[@]}"; do
  mv -fT -- "$work/new/$file" "$install_dir/$file"
done
systemctl restart "$service"
for attempt in {1..10}; do
  if systemctl is-active --quiet "$service" &&
     response=$(curl --fail --silent --max-time 3 -H 'Content-Type: application/json' \
       -d '{}' "http://127.0.0.1:$port/ota/check"); then
    if version=$("$install_dir/.venv/bin/python" -c \
      'import json, sys; v = json.load(sys.stdin)["version"]; assert isinstance(v, str) and v; print(v)' \
      <<< "$response"); then
      rollback=0
      printf 'Updated %s in %s. /ota/check serves version %s.\n' "$service" "$install_dir" "$version"
      exit 0
    fi
  fi
  sleep 1
done
printf 'OTA check failed after restart.\n' >&2
exit 1
REMOTE
