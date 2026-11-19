#!/usr/bin/env bash
# Install from this checkout on the target Linux machine.
set -euo pipefail
SOURCE_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
INSTALL_DIR=${INSTALL_DIR:-/root/server/ota_server}
LOG_DIR=${LOG_DIR:-/var/log/ota-server}
LOG_RETENTION=${LOG_RETENTION:-14}
LOG_MAX_SIZE=${LOG_MAX_SIZE:-50M}
# Paths are also rendered into systemd and logrotate configuration.
for path in "$INSTALL_DIR" "$LOG_DIR"; do
  [[ "$path" =~ ^/[A-Za-z0-9_./-]+$ && "$path" != / && "$path" != */../* && "$path" != */.. ]] || { echo "Invalid absolute path: $path" >&2; exit 2; }
done
[[ "$LOG_RETENTION" =~ ^[1-9][0-9]*$ && "$LOG_MAX_SIZE" =~ ^[1-9][0-9]*[kMG]$ ]] || { echo "Invalid log retention or size" >&2; exit 2; }
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
  apt-get install -y python3 python3-venv python3-pip ca-certificates curl logrotate
elif command -v dnf >/dev/null; then
  dnf install -y python3 python3-pip ca-certificates curl logrotate
elif command -v yum >/dev/null; then
  yum install -y python3 python3-pip ca-certificates curl logrotate
else
  echo 'Supported package managers: apt-get, dnf, yum.' >&2
  exit 1
fi
install -d -m 0755 "$INSTALL_DIR"
# Avoid copying files onto themselves when rerunning from the installation.
if [[ "$SOURCE_DIR" != "$INSTALL_DIR" ]]; then
  install -m 0644 "$SOURCE_DIR/server.py" "$SOURCE_DIR/requirements.txt" "$SOURCE_DIR/gunicorn.conf.py" "$SOURCE_DIR/ota_reports.py" "$INSTALL_DIR/"
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
install -d -m 0750 "$LOG_DIR" /etc/ota-server /var/lib/ota-server
sed -e "s#/root/server/ota_server#$INSTALL_DIR#g" \
    -e "s#/var/log/ota-server#$LOG_DIR#g" \
    "$SOURCE_DIR/deploy/ota-server.service" > /etc/systemd/system/ota-server.service
chmod 0644 /etc/systemd/system/ota-server.service
cat > /etc/ota-server/logrotate.conf <<EOF_ROTATE
$LOG_DIR/access.log $LOG_DIR/error.log {
    daily
    maxsize $LOG_MAX_SIZE
    rotate $LOG_RETENTION
    missingok
    notifempty
    compress
    delaycompress
    create 0640 root root
    sharedscripts
    postrotate
        /bin/systemctl kill --kill-who=main --signal=USR1 ota-server.service
    endscript
}
EOF_ROTATE
chmod 0640 /etc/ota-server/logrotate.conf
install -m 0644 "$SOURCE_DIR/deploy/ota-server-logrotate.service" "$SOURCE_DIR/deploy/ota-server-logrotate.timer" /etc/systemd/system/
/usr/sbin/logrotate --debug --state /var/lib/ota-server/logrotate.status /etc/ota-server/logrotate.conf
install -d -m 0755 /etc/default
if [[ ! -f /etc/default/ota-server ]]; then
  (umask 077; cat > /etc/default/ota-server <<'EOF_REPORT'
# Upload endpoints return 503 until a token is provisioned. Downloads remain public.
OTA_REPORT_TOKEN=
OTA_STATE_DIR=/var/lib/ota-server
OTA_REPORT_RETENTION_DAYS=30
OTA_REPORT_MAX_ROWS=10000
EOF_REPORT
  )
fi
systemctl daemon-reload
systemctl enable --now ota-server-logrotate.timer
systemctl enable ota-server.service
systemctl restart ota-server.service
for attempt in {1..30}; do
  if systemctl is-active --quiet ota-server.service && curl --fail --silent --max-time 2 http://127.0.0.1:4242/healthz >/dev/null; then
    echo 'OTA server started: http://127.0.0.1:4242'
    if [[ ! -f "$INSTALL_DIR/ota/version.json" ]]; then
      echo "Upload OTA data to $INSTALL_DIR/ota; /ota/check returns 503 until version.json is present."
    fi
    exit 0
  fi
  sleep 1
done
systemctl status ota-server.service --no-pager || true
journalctl -u ota-server.service -n 50 --no-pager || true
exit 1
