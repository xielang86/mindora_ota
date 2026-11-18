"""Gunicorn settings; systemd supplies OTA_LOG_DIR."""
import os
from pathlib import Path

log_dir = Path(os.environ.get('OTA_LOG_DIR', '/var/log/ota-server'))
accesslog = str(log_dir / 'access.log')
errorlog = str(log_dir / 'error.log')
loglevel = os.environ.get('OTA_LOG_LEVEL', 'info')
capture_output = True
# Use Nginx's overwritten X-Real-IP for diagnosis; the peer remains visible.
access_log_format = '%(t)s peer=%(h)s client=%({x-real-ip}i)s pid=%(p)s "%(r)s" status=%(s)s bytes=%(b)s duration=%(L)s'
