"""Priority-ordered mirrors. A fallback must advertise the same pinned artifact."""
import logging
import re
import time
from urllib.parse import urlsplit

import requests
import json
import os
from pathlib import Path
from ota.config import OTA_CHECK_URLS


def settings():
    path = Path(os.environ.get('MINDORA_OTA_CONFIG', '/home/rpdzkj/update/config.json'))
    try:
        data = json.loads(path.read_text())
        if not isinstance(data, dict):
            raise ValueError('OTA config must be an object')
        return data
    except FileNotFoundError:
        return {}
    except (OSError, ValueError) as e:
        logging.warning('Invalid OTA config; use existing default: %s', e)
        return {}


def check_urls(default=''):
    """Environment overrides, optional legacy file, production defaults, fallback."""
    overrides = []
    raw = os.environ.get('MINDORA_OTA_CHECK_URLS', '').strip()
    if raw:
        try:
            overrides = json.loads(raw)
            if not isinstance(overrides, list):
                raise ValueError('expected a JSON array')
        except (ValueError, TypeError) as e:
            logging.warning('Ignoring invalid MINDORA_OTA_CHECK_URLS: %s', e)
            overrides = []
    configured = settings().get('check_urls', [])
    if not isinstance(configured, list):
        logging.warning('Ignoring invalid OTA check_urls')
        configured = []
    result = []
    for value in overrides + configured + OTA_CHECK_URLS + [default]:
        if not isinstance(value, str) or not value.strip():
            continue
        url = value.strip()
        try:
            parsed = urlsplit(url)
            valid = parsed.scheme in ('http', 'https') and parsed.hostname and not parsed.username and not parsed.password
        except ValueError:
            valid = False
        if not valid:
            logging.warning('Ignoring invalid OTA check URL: %r', url)
            continue
        if url not in result:
            result.append(url)
    return result


def get_info(url, serial, version):
    device = {'serial_number': serial, 'current_version': version}
    if urlsplit(url).path.endswith('.json'):
        r = requests.get(url, params=device, timeout=(2, 10))
    else:
        r = requests.post(url, json=device, timeout=(2, 10))
    with r:
        r.raise_for_status()
        data = r.json()
    if not isinstance(data, dict) or not all(k in data for k in ('version', 'url', 'sha256', 'size')):
        raise ValueError('incomplete OTA metadata')
    if not re.fullmatch('[0-9a-fA-F]{64}', data['sha256']) or int(data['size']) <= 0:
        raise ValueError('invalid artifact checksum/size')
    return data


def candidates(info):
    result = []
    urls = check_urls(info.get('_check_url', ''))
    for source in filter(None, urls):
        try:
            data = get_info(source, '', info['version'])
            if (data['version'], data['sha256'].lower(), int(data['size'])) != (
                    info['version'], info['sha256'].lower(), int(info['size'])):
                logging.warning('skip mirror with different target: %s', source)
                continue
            result.append(data['url'])
        except Exception as e:
            logging.warning('mirror metadata unavailable %s: %s', source, e)
    result.extend(info.get('download_urls', []))
    result.append(info['url'])
    return list(dict.fromkeys(result))


def probe(url, size, cancelled=lambda: None):
    """At most 64 KiB, short read timeout; size must match pinned metadata."""
    start = time.monotonic()
    received = 0
    with requests.get(url, headers={'Range': 'bytes=0-65535', 'Accept-Encoding': 'identity'},
                      stream=True, timeout=(2, 2)) as r:
        r.raise_for_status()
        if r.headers.get('Content-Encoding', 'identity') not in ('identity', ''):
            raise RuntimeError('compressed download cannot be ranged safely')
        if r.status_code == 206:
            match = re.fullmatch(r'bytes 0-(\d+)/(\d+)', r.headers.get('Content-Range', ''))
            if not match or int(match[2]) != size:
                raise RuntimeError('probe Content-Range mismatch')
        elif r.status_code != 200 or int(r.headers.get('Content-Length', '-1')) != size:
            raise RuntimeError('probe file length mismatch')
        for chunk in r.iter_content(8192):
            cancelled()
            received += len(chunk)
            if received >= min(size, 65536) or time.monotonic()-start >= 3:
                break
    if not received:
        raise RuntimeError('empty probe')
    return received / max(0.001, time.monotonic()-start)


def resource_mirrors(info, manifest_bytes):
    import hashlib
    pinned = hashlib.sha256(manifest_bytes).digest()
    urls = [info['resources_url']]
    for endpoint in filter(None, check_urls(info.get('_check_url', ''))):
        try:
            other = get_info(endpoint, '', info['version'])
            with requests.get(other['resources_manifest_url'], timeout=(2, 5)) as r:
                r.raise_for_status()
                if hashlib.sha256(r.content).digest() == pinned:
                    urls.append(other['resources_url'])
        except Exception as e:
            logging.warning('resource mirror skipped %s: %s', endpoint, e)
    return list(dict.fromkeys(urls))


def fetch_manifest(info):
    """Fallback a missing manifest only when metadata pins its SHA256 explicitly."""
    import hashlib
    expected = info.get('resources_manifest_sha256')
    choices = [(info['resources_manifest_url'], info['resources_url'])]
    if expected:
        for endpoint in filter(None, check_urls(info.get('_check_url', ''))):
            try:
                mirror = get_info(endpoint, '', info['version'])
                if (mirror['version'], mirror['sha256'].lower(), int(mirror['size'])) == (
                        info['version'], info['sha256'].lower(), int(info['size'])):
                    choices.append((mirror['resources_manifest_url'], mirror['resources_url']))
            except Exception as e:
                logging.warning('manifest mirror metadata skipped: %s', e)
    last = RuntimeError('resource manifest unavailable')
    for url, base in dict.fromkeys(choices):
        try:
            with requests.get(url, timeout=(3, 15)) as response:
                response.raise_for_status()
                content = response.content
            if expected and hashlib.sha256(content).hexdigest().lower() != expected.lower():
                raise RuntimeError('resource manifest SHA256 mismatch')
            return content, base
        except Exception as e:
            last = e
            logging.warning('manifest source failed %s: %s', url, e)
    raise last
