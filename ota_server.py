"""Small OTA origin. Run with gunicorn; keep ota/resources outside code releases."""
import json
import logging
from pathlib import Path
from flask import Flask, g, jsonify, request, send_from_directory

from ota_reports import reports

app = Flask(__name__)
app.register_blueprint(reports)
app.logger.setLevel(logging.INFO)
BASE = Path(__file__).resolve().parent / 'ota'


@app.get('/healthz')
def health():
    return jsonify(status='ok')


@app.post('/ota/check')
def check():
    # Devices perform their own version comparison. No production rollout/serial
    # selection is implemented by this all-devices gray origin.
    try:
        data = json.loads((BASE / 'version.json').read_text())
        if isinstance(data, dict):
            g.offered_version = data.get('version')
        return jsonify(data)
    except (OSError, ValueError):
        app.logger.exception('Cannot read release metadata')
        return jsonify(error='release metadata unavailable'), 503


@app.get('/ota/<path:filename>')
def files(filename):
    if filename.startswith('.') or any(p.startswith('.') for p in Path(filename).parts):
        return '', 404
    return send_from_directory(str(BASE), filename, conditional=True,
                               mimetype='application/json' if filename.endswith('.json') else 'application/octet-stream')


@app.after_request
def headers(response):
    is_check = request.path in ('/ota/check', '/ota/version.json')
    is_download = request.endpoint == 'files' and request.method in ('GET', 'HEAD')
    if is_check or is_download:
        payload = request.get_json(silent=True) if request.method == 'POST' else request.args
        if not hasattr(payload, 'get'):
            payload = {}
        # Only the local reverse proxy may supply the public client IP.
        peer_ip = request.remote_addr
        client_ip = (request.headers.get('X-Real-IP') or peer_ip
                     if peer_ip in ('127.0.0.1', '::1') else peer_ip)
        event = {
            'event': 'ota_check' if is_check else 'ota_download',
            'client_ip': client_ip,
            'peer_ip': peer_ip,
            'serial_number': str(request.headers.get('X-OTA-SN') or payload.get('serial_number') or '')[:128],
            'mac': str(request.headers.get('X-OTA-MAC') or payload.get('mac') or payload.get('mac_address') or '')[:64],
            'current_version': str(request.headers.get('X-OTA-Version') or payload.get('current_version') or '')[:64],
            'method': request.method,
            'path': request.path,
            'status': response.status_code,
        }
        if is_check:
            target_version = g.get('offered_version')
            if request.path == '/ota/version.json' and request.method == 'GET' and response.status_code == 200:
                try:
                    target_version = json.loads((BASE / 'version.json').read_text()).get('version')
                except (OSError, ValueError, AttributeError):
                    pass
            event['offered_version'] = target_version
        else:
            event['range'] = request.headers.get('Range', '')[:128]
            event['content_length'] = response.content_length
        # JSON encoding keeps untrusted device fields on a single log line.
        app.logger.info('OTA %s', json.dumps(event, ensure_ascii=True))
    response.headers['Content-Encoding'] = 'identity'
    if request.path.endswith('.json') or request.path == '/ota/check':
        response.headers['Cache-Control'] = 'no-store'
    return response


if __name__ == '__main__':
    # Local gray testing only; use Gunicorn/systemd for the deployed service.
    app.run(host='0.0.0.0', port=4242, debug=False)
