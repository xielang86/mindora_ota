#!/usr/bin/env python3
"""Publish a versioned OTA resource snapshot over rsync and SSH."""
import argparse
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
from urllib.parse import quote, urlsplit
from urllib.request import Request, urlopen
from uuid import uuid4


VERSION = re.compile(r'[0-9][A-Za-z0-9._+-]{0,63}')
RELEASES = 'resource-releases'


def file_digest(path, algorithm):
    with path.open('rb') as stream:
        return hashlib.file_digest(stream, algorithm).hexdigest()


def read_manifest(directory):
    raw = (directory / 'manifest.json').read_bytes()
    data = json.loads(raw)
    if not isinstance(data, dict) or not VERSION.fullmatch(str(data.get('version', ''))):
        raise ValueError('manifest.json needs a valid resource version')
    if not isinstance(data.get('files'), list):
        raise ValueError('manifest.json needs a files list')
    files = {}
    for entry in data['files']:
        name = entry['path']
        if (not isinstance(name, str) or '\0' in name or name == 'manifest.json'
                or any(not part or part.startswith('.') for part in name.split('/'))):
            raise ValueError(f'Invalid manifest path: {name!r}')
        if name in files:
            raise ValueError(f'Duplicate manifest path: {name}')
        if type(entry.get('size')) is not int or entry['size'] < 0:
            raise ValueError(f'Invalid size for {name}')
        for field, length in (('md5', 32), ('sha256', 64)):
            if field == 'sha256' and field not in entry:
                continue
            value = entry.get(field)
            if not isinstance(value, str) or not re.fullmatch(f'[0-9a-f]{{{length}}}', value):
                raise ValueError(f'Invalid {field} for {name}')
        files[name] = entry
    return str(data['version']), hashlib.sha256(raw).hexdigest(), files


def verify_snapshot(directory, version, checksum):
    actual_version, actual_checksum, files = read_manifest(directory)
    if (actual_version, actual_checksum) != (version, checksum):
        raise ValueError('Snapshot manifest differs from the release manifest')
    for name, entry in files.items():
        path = directory / name
        if not path.is_file() or path.resolve() != path:
            raise ValueError(f'Missing or symlinked resource: {name}')
        if path.stat().st_size != entry['size']:
            raise ValueError(f'Size mismatch: {name}')
        for algorithm in ('md5', 'sha256'):
            if algorithm in entry and file_digest(path, algorithm) != entry[algorithm]:
                raise ValueError(f'{algorithm} mismatch: {name}')


def atomic_write(path, contents):
    with tempfile.NamedTemporaryFile(dir=path.parent, prefix='.publish-', delete=False) as stream:
        temporary = Path(stream.name)
        try:
            stream.write(contents)
            stream.flush()
            os.fsync(stream.fileno())
            os.fchmod(stream.fileno(), 0o644)
            temporary.replace(path)
        finally:
            temporary.unlink(missing_ok=True)


def read_version_metadata(path):
    data = json.loads(path.read_bytes())
    if not isinstance(data, dict) or not data.get('version'):
        raise ValueError('Existing code version.json is required')
    if data.get('signature'):
        raise ValueError('Cannot update signed version.json without resigning')
    return data


def published_releases(directory):
    return sorted(
        (path for path in directory.iterdir()
         if VERSION.fullmatch(path.name) and path.is_dir() and not path.is_symlink()
         and (path / '.published').is_file()),
        key=lambda path: (path / '.published').stat().st_mtime_ns,
        reverse=True,
    )


def prepare(ota, stage):
    read_version_metadata(ota / 'version.json')
    releases = ota / RELEASES
    releases.mkdir(exist_ok=True)
    if releases.is_symlink() or stage.parent.is_symlink():
        raise ValueError('Release and staging directories must be managed directories')
    stage.mkdir(parents=True)
    if stage.stat().st_dev != releases.stat().st_dev:
        raise ValueError('Resource staging and releases must share a filesystem')
    snapshots = published_releases(releases)
    if snapshots:
        return {'stage': str(stage), 'basis': str(snapshots[0]), 'basis_mode': 'link'}
    legacy = ota / 'resources'
    if (legacy / 'manifest.json').is_file():
        return {'stage': str(stage), 'basis': str(legacy), 'basis_mode': 'copy'}
    return {'stage': str(stage)}


def publish(root, ota, stage, job):
    version, checksum = job['version'], job['sha256']
    verify_snapshot(stage, version, checksum)
    releases = ota / RELEASES
    snapshot = releases / version
    base = job['public_base_url']
    resources_url = f'{base}/{RELEASES}/{quote(version)}'
    patch = {
        'resources_url': resources_url,
        'resources_manifest_url': resources_url + '/manifest.json',
        'resources_manifest_sha256': checksum,
    }
    with (root / '.publish.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        metadata = ota / 'version.json'
        current = read_version_metadata(metadata)
        history = root / '.resource-history'
        history.mkdir(exist_ok=True)
        release_hash = history / (version + '.sha256')
        if release_hash.exists() and release_hash.read_text().strip() != checksum:
            raise ValueError('Resource version already used with different contents; increment it')
        if snapshot.exists() or snapshot.is_symlink():
            if snapshot.is_symlink():
                raise ValueError('Release destination is a symlink')
            verify_snapshot(snapshot, version, checksum)
        if not release_hash.exists():
            atomic_write(release_hash, (checksum + '\n').encode())
        if not snapshot.exists():
            stage.replace(snapshot)
        with urlopen(patch['resources_manifest_url'], timeout=20) as response:
            if hashlib.sha256(response.read()).hexdigest() != checksum:
                raise ValueError('Public manifest does not match the release')
        current.update(patch)
        atomic_write(metadata, (json.dumps(current, ensure_ascii=False, indent=2) + '\n').encode())

        try:
            check = Request(base + '/check', data=b'{}', headers={'Content-Type': 'application/json'})
            with urlopen(check, timeout=20) as response:
                offered = json.load(response)
            if any(offered.get(key) != value for key, value in patch.items()):
                raise ValueError('Public /ota/check has different resource metadata')
            atomic_write(snapshot / '.published', b'')
            for old in published_releases(releases)[job['keep']:]:
                if old != snapshot:
                    shutil.rmtree(old)
            if stage.exists():
                shutil.rmtree(stage)
        except (OSError, ValueError, AttributeError) as exc:
            raise ValueError(f'Resource metadata committed; verification or cleanup failed: {exc}') from exc
    return patch


def worker(action, job):
    root = Path(job['remote_root']).resolve(strict=True)
    ota = (root / 'ota').resolve(strict=True)
    stage = root / '.resource-incoming' / job['stage_id']
    if action == 'prepare':
        return prepare(ota, stage)
    return publish(root, ota, stage, job)


def run_worker(host, action, job):
    command = ['python3', '-', action, json.dumps(job)]
    script = Path(__file__).read_bytes()
    if host not in ('localhost', '127.0.0.1', '::1'):
        command = ['ssh', host, shlex.join(command)]
    result = subprocess.run(command, input=script, stdout=subprocess.PIPE, check=True)
    return json.loads(result.stdout)


def run(args):
    source = args.source.resolve(strict=True)
    version, checksum, files = read_manifest(source)
    config = json.loads(args.config.read_text())
    host = config['ssh_host']
    if not re.fullmatch(r'[A-Za-z0-9_][A-Za-z0-9_.-]*(?:@[A-Za-z0-9_][A-Za-z0-9_.-]*)?|::1', host):
        raise ValueError('Invalid SSH host; use an SSH config alias for advanced options')
    remote_root = config['remote_root']
    if not re.fullmatch(r'/[A-Za-z0-9_./-]+', remote_root) or '..' in Path(remote_root).parts:
        raise ValueError('remote_root must be an absolute path without spaces or ..')
    public_base = config['public_base_url'].rstrip('/')
    parsed = urlsplit(public_base)
    if (parsed.scheme not in ('http', 'https') or not parsed.hostname
            or parsed.query or parsed.fragment or not parsed.path.endswith('/ota')):
        raise ValueError('public_base_url must be an HTTP(S) URL ending in /ota')

    job = {
        'remote_root': remote_root, 'public_base_url': public_base, 'stage_id': uuid4().hex,
        'version': version, 'sha256': checksum, 'keep': args.keep,
    }
    setup = run_worker(host, 'prepare', job)
    target = setup['stage'] + '/'
    if host not in ('localhost', '127.0.0.1', '::1'):
        target = f'{host}:{target}'
    command = ['rsync', '-rpt', '--checksum', '--stats', '--from0', '--files-from=-',
               '--chmod=Du=rwx,Dgo=rx,Fu=r,Fgo=r']
    if setup.get('basis'):
        command.append(f"--{setup['basis_mode']}-dest={setup['basis']}")
    command.extend(['--', str(source) + '/', target])
    paths = b''.join(os.fsencode(name) + b'\0' for name in ('manifest.json', *files))
    print(f'Publishing resource {version} ({len(files)} files), staging at {setup["stage"]}', flush=True)
    subprocess.run(command, input=paths, check=True)
    print(json.dumps(run_worker(host, 'publish', job), indent=2))


def main():
    if sys.argv[0] == '-' and len(sys.argv) == 3:
        print(json.dumps(worker(sys.argv[1], json.loads(sys.argv[2]))))
        return
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source', type=Path, required=True, help='Resource tree containing manifest.json')
    parser.add_argument('--config', type=Path, required=True, help='OTA deployment configuration JSON')
    parser.add_argument('--keep', type=int, default=3, help='Number of published snapshots to retain (default: 3)')
    args = parser.parse_args()
    if args.keep < 3:
        parser.error('--keep must be at least 3')
    run(args)


if __name__ == '__main__':
    try:
        main()
    except (OSError, ValueError, KeyError, TypeError, subprocess.CalledProcessError) as exc:
        sys.exit(f'Resource publication failed: {exc}')
