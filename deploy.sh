#!/usr/bin/env bash
# Install from this checkout on the target Linux machine.
set -euo pipefail
SOURCE_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
INSTALL_DIR=/root/server/ota-server
WITH_DATA=false
case "${1:-}" in
  --with-data) WITH_DATA=true ;;
  '') ;;
  *) echo "Usage: sudo bash $0 [--with-data]" >&2; exit 2 ;;
esac
[[ $# -le 1 ]] || { echo 'Too many arguments' >&2; exit 2; }
[[ $(id -u) -eq 0 ]] || { echo 'Run with sudo or as root.' >&2; exit 1; }
command -v systemctl >/dev/null || { echo 'A Linux system using systemd is required.' >&2; exit 1; }
[[ -d /run/systemd/system ]] || { echo 'systemd must be running.' >&2; exit 1; }
if "$WITH_DATA"; then
  [[ -f "$SOURCE_DIR/ota/version.json" ]] || { echo 'Missing ota/version.json' >&2; exit 1; }
  # Dereference resource links so the installed data is self-contained.
  if [[ -n "$(find -L "$SOURCE_DIR/ota" -type l -print -quit)" ]]; then
    echo 'ota/ contains a broken or cyclic symbolic link; fix it before copying data.' >&2
    exit 1
  fi
fi
if command -v apt-get >/dev/null; then
  apt-get update
  apt-get install -y python3 python3-venv python3-pip ca-certificates curl
elif command -v dnf >/dev/null; then
  dnf install -y python3 python3-pip ca-certificates curl
elif command -v yum >/dev/null; then
  yum install -y python3 python3-pip ca-certificates curl
else
  echo 'Supported package managers: apt-get, dnf, yum.' >&2
  exit 1
fi
install -d -m 0755 "$INSTALL_DIR"
# Avoid copying files onto themselves when rerunning from the installation.
if [[ "$SOURCE_DIR" != "$INSTALL_DIR" ]]; then
  install -m 0644 "$SOURCE_DIR/server.py" "$SOURCE_DIR/requirements.txt" "$INSTALL_DIR/"
fi
python3 -m venv "$INSTALL_DIR/.venv"
"$INSTALL_DIR/.venv/bin/python" -m pip install -r "$INSTALL_DIR/requirements.txt"
install -d -m 0750 -o root -g root "$INSTALL_DIR/ota"
if "$WITH_DATA" && [[ "$SOURCE_DIR" != "$INSTALL_DIR" ]]; then
  cp -aL "$SOURCE_DIR/ota/." "$INSTALL_DIR/ota/"
fi
# The service only reads data; publishing is performed by an administrator.
chown -R root:root "$INSTALL_DIR/ota"
chmod -R u+rwX,g+rX,o-rwx "$INSTALL_DIR/ota"
install -m 0644 "$SOURCE_DIR/deploy/ota-server.service" /etc/systemd/system/ota-server.service
systemctl daemon-reload
systemctl enable ota-server.service
systemctl restart ota-server.service
for attempt in {1..30}; do
  if systemctl is-active --quiet ota-server.service && curl --fail --silent --max-time 2 http://127.0.0.1:4242/healthz >/dev/null; then
    echo 'OTA server started: http://127.0.0.1:4242'
    if [[ ! -f "$INSTALL_DIR/ota/version.json" ]]; then
      echo 'Upload OTA data to /root/server/ota-server/ota; /ota/check returns 503 until version.json is present.'
    fi
    exit 0
  fi
  sleep 1
done
systemctl status ota-server.service --no-pager || true
journalctl -u ota-server.service -n 50 --no-pager || true
exit 1
