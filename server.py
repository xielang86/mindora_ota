"""Small OTA origin. Run with gunicorn; keep ota/resources outside code releases."""
import json
from pathlib import Path
from flask import Flask, jsonify, request, send_from_directory

app = Flask(__name__)
BASE = Path(__file__).resolve().parent / 'ota'


@app.get('/healthz')
def health():
    return jsonify(status='ok')


@app.post('/ota/check')
def check():
    # Devices perform their own version comparison. No production rollout/serial
    # selection is implemented by this all-devices gray origin.
    try:
        return jsonify(json.loads((BASE / 'version.json').read_text()))
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
    response.headers['Content-Encoding'] = 'identity'
    if request.path.endswith('.json') or request.path == '/ota/check':
        response.headers['Cache-Control'] = 'no-store'
    return response


if __name__ == '__main__':
    # Local gray testing only; use Gunicorn/systemd for the deployed service.
    app.run(host='0.0.0.0', port=4242, debug=False)
