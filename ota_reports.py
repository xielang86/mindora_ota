"""Additive telemetry routes. Existing OTA GET/POST/download responses are untouched."""
import hmac
import fcntl
import json
import os
from pathlib import Path
import sqlite3
import time

from flask import Blueprint, jsonify, request

reports = Blueprint('ota_reports', __name__)
LIMIT = 128 * 1024


def connection():
    root = Path(os.environ.get('OTA_STATE_DIR', '/var/lib/ota-server'))
    root.mkdir(parents=True, exist_ok=True, mode=0o750)
    # Serialize first-use schema/WAL setup across Gunicorn workers.
    with (root / '.reports-init.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        db = sqlite3.connect(root / 'reports.sqlite3', timeout=5)
        try:
            if db.execute('PRAGMA journal_mode').fetchone()[0] != 'wal':
                db.execute('PRAGMA journal_mode=WAL')
            db.execute('CREATE TABLE IF NOT EXISTS events (event_id TEXT PRIMARY KEY, '
                       'update_id TEXT NOT NULL, received_at REAL NOT NULL, client_ip TEXT, payload TEXT NOT NULL)')
            db.execute('CREATE INDEX IF NOT EXISTS events_received ON events(received_at)')
        except Exception:
            db.close()
            raise

    return db


@reports.post('/ota/report')
@reports.post('/ota/health')
@reports.post('/ota/logs')
def receive():
    token = os.environ.get('OTA_REPORT_TOKEN', '')
    if not token:
        return jsonify(error='reporting not configured'), 503
    supplied = request.headers.get('Authorization', '')
    if not hmac.compare_digest(supplied.encode(), ('Bearer ' + token).encode()):
        return jsonify(error='unauthorized'), 401
    if request.content_length is not None and request.content_length > LIMIT:
        return jsonify(error='report too large'), 413
    raw = request.stream.read(LIMIT+1)
    if len(raw) > LIMIT:
        return jsonify(error='report too large'), 413
    try:
        data = json.loads(raw)
    except (ValueError, UnicodeDecodeError):
        return jsonify(error='invalid JSON'), 400
    if not isinstance(data, dict):
        return jsonify(error='expected object'), 400
    for name in ('event_id', 'update_id', 'event'):
        if not isinstance(data.get(name), str) or not 1 <= len(data[name]) <= 128:
            return jsonify(error='invalid ' + name), 400
    for name in ('serial_number', 'mac', 'current_version', 'target_version',
                 'selected_source', 'phase', 'failed_stage', 'error', 'running_version'):
        if name in data and (not isinstance(data[name], str) or len(data[name]) > 2048):
            return jsonify(error='invalid ' + name), 400
    if request.path == '/ota/health' and data['event'] != 'healthy':
        return jsonify(error='health endpoint requires healthy event'), 400
    peer = request.remote_addr
    ip = request.headers.get('X-Real-IP', peer) if peer in ('127.0.0.1', '::1') else peer
    try:
        db = connection()
        try:
            with db:
                result = db.execute('INSERT OR IGNORE INTO events VALUES (?, ?, ?, ?, ?)',
                    (data['event_id'], data['update_id'], time.time(), ip, json.dumps(data, ensure_ascii=False)))
                duplicate = result.rowcount == 0
                days = max(1, int(os.environ.get('OTA_REPORT_RETENTION_DAYS', '30')))
                db.execute('DELETE FROM events WHERE received_at < ?', (time.time()-days*86400,))
                max_rows = max(100, int(os.environ.get('OTA_REPORT_MAX_ROWS', '10000')))
                db.execute('''DELETE FROM events WHERE event_id IN
                    (SELECT event_id FROM events ORDER BY received_at DESC LIMIT -1 OFFSET ?)''', (max_rows,))
        finally:
            db.close()
    except (OSError, sqlite3.Error, ValueError):
        from flask import current_app
        current_app.logger.exception('cannot persist OTA report')
        return jsonify(error='report storage unavailable'), 503
    return jsonify(status='accepted', duplicate=duplicate), 202
