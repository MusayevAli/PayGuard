#!/usr/bin/env python3
"""Apply a local, manifest-checked Mac demo update without resetting hardware.

ZIP/zipapp layout: manifest.json, payload/<declared path>, optional __main__.py.
Manifest: {"schema":"payguard.demo-update.v1","files":{
  "ui/app.js":{"bytes":123,"sha256":"64 lowercase hex characters"}}}.
Run a packaged zipapp directly, or pass an update ZIP/directory as the positional
argument. Stop the existing Mac server with Ctrl-C first; this program kills no
process. --start resumes the existing hardware session, never initializes it.
"""
from __future__ import annotations

import argparse
import errno
import hashlib
import json
import os
import re
import socket
import stat
import subprocess
import sys
import tempfile
import uuid
import zipfile
from datetime import UTC, datetime
from pathlib import Path

ALLOWED = frozenset({'ui/index.html', 'ui/app.js', 'ui/style.css', 'ui/mark.svg',
                     'api/dataset.py', 'api/main.py', 'api/dashboard.py', 'api/authorization.py', 'api/demo.py',
                     'api/judge.py', 'api/live_jobs.py', 'agents/runner.py', 'signer/hardware.py',
                     'hardware/python/siliconsign/transport.py',
                     'sim/clock.py', 'core/bootstrap.py', 'core/events.py'})
WORLD_FILES = ('world.jsonl', 'suppliers.json', 'people.json', 'fx_rates.json',
               'answer_key.json', 'invoices_truth.json')
ROLES = ('admin', 'controller', 'response')
MAX_FILE = 4 * 1024 * 1024
DEFAULT_DEVICE = '/dev/cu.usbserial-XFL1N5D0EKLC1'


class UpdateError(RuntimeError):
    pass


def require(condition, message):
    if not condition:
        raise UpdateError(message)


def digest(data):
    return hashlib.sha256(data).hexdigest()


def regular(path):
    require(path.is_file() and not path.is_symlink() and path.resolve() == path,
            'Expected a regular file without symlink parents: ' + str(path))
    return path


def load_payload(source):
    archive = None
    try:
        if source.is_dir():
            source = source.resolve()

            def read(name, limit):
                path = regular(source / name)
                require(path.stat().st_size <= limit, 'Oversized update member: ' + name)
                return path.read_bytes()
        else:
            archive = zipfile.ZipFile(source)
            members = archive.infolist()
            require(len(members) <= 32, 'Unexpected update archive contents')
            names = [item.filename for item in members]
            require(len(names) == len(set(names)), 'Duplicate ZIP members are forbidden')
            require(all(not stat.S_ISLNK(item.external_attr >> 16) for item in members),
                    'Symlink ZIP members are forbidden')

            def read(name, limit):
                info = archive.getinfo(name)
                require(not info.is_dir() and info.file_size <= limit,
                        'Oversized or invalid update member: ' + name)
                return archive.read(info)

        manifest = json.loads(read('manifest.json', 65536))
        require(isinstance(manifest, dict) and set(manifest) == {'schema', 'files'}
                and manifest['schema'] == 'payguard.demo-update.v1', 'Unknown update manifest')
        files = manifest['files']
        require(isinstance(files, dict) and 0 < len(files) <= len(ALLOWED), 'Invalid file manifest')
        require(all(name in ALLOWED for name in files), 'Update includes a protected or unknown path')
        if archive:
            accepted = {'manifest.json', '__main__.py'} | {'payload/' + name for name in files}
            directories = {'payload/', 'payload/ui/', 'payload/api/', 'payload/sim/', 'payload/core/'}
            require(all(item.filename in (directories if item.is_dir() else accepted)
                        for item in members), 'Undeclared or unsafe archive member')
        payload = {}
        for name, record in files.items():
            require(isinstance(record, dict) and set(record) == {'bytes', 'sha256'}
                    and type(record['bytes']) is int and 0 <= record['bytes'] <= MAX_FILE
                    and isinstance(record['sha256'], str)
                    and re.fullmatch(r'[0-9a-f]{64}', record['sha256']),
                    'Invalid file record: ' + name)
            data = read('payload/' + name, MAX_FILE)
            require(len(data) == record['bytes'] and digest(data) == record['sha256'],
                    'Update hash/size mismatch: ' + name)
            if name.endswith('.py'):
                compile(data, name, 'exec')  # Syntax check only; never execute payload code here.
            payload[name] = data
        return payload
    except (OSError, KeyError, ValueError, SyntaxError, zipfile.BadZipFile) as error:
        raise UpdateError('Cannot validate update: ' + str(error)) from None
    finally:
        if archive:
            archive.close()


def validate_root(root):
    root = root.expanduser().resolve(strict=True)
    require(root.is_dir(), 'Project root is not a directory')
    for name in ('scripts/launch.py', 'api/main.py', 'sim/clock.py',
                 'ui/index.html', 'ui/app.js', 'ui/style.css',
                 'runtime/payguard-hw.db', 'private/controller-token'):
        regular(root / name)
    for role in ROLES:
        regular(root / 'private/roles' / (role + '.key'))
    for name in WORLD_FILES:
        regular(root / 'sim/world' / name)
    python = root / '.venv/bin/python'
    # Virtual environments commonly symlink their interpreter; do not replace it.
    require(python.is_file() and os.access(python, os.X_OK), 'Existing .venv/bin/python is missing')
    require((root / 'runtime/payguard-hw.db').stat().st_size > 0,
            'Existing hardware database is empty; refusing a fresh session')
    return root


def require_server_stopped(port=8000):
    for family, address in ((socket.AF_INET, ('127.0.0.1', port)),
                            (socket.AF_INET6, ('::1', port))):
        try:
            with socket.socket(family, socket.SOCK_STREAM) as connection:
                connection.settimeout(0.3)
                result = connection.connect_ex(address)
        except OSError as error:
            if family == socket.AF_INET6 and error.errno in (errno.EAFNOSUPPORT, errno.EADDRNOTAVAIL):
                continue
            raise UpdateError('Cannot verify that the local server stopped') from error
        if result == 0:
            raise UpdateError('Port 8000 is still listening. Press Ctrl-C in the old Mac server terminal, then rerun. No process was killed.')
        require(result in (errno.ECONNREFUSED, errno.EAFNOSUPPORT, errno.EADDRNOTAVAIL),
                'Cannot confirm the local server is stopped; no source files changed')


def sync_directory(path):
    fd = os.open(path, os.O_RDONLY)
    try:
        try:
            os.fsync(fd)
        except OSError as error:
            # Some Darwin filesystems do not implement directory fsync. File
            # data was still fsynced before atomic replacement; other errors fail.
            if error.errno not in (errno.EINVAL, errno.ENOTSUP):
                raise
    finally:
        os.close(fd)


def stage(path, data, mode):
    fd, temporary = tempfile.mkstemp(prefix='.payguard-update-', dir=path.parent)
    with os.fdopen(fd, 'wb') as handle:
        handle.write(data)
        handle.flush()
        os.fchmod(handle.fileno(), mode)
        os.fsync(handle.fileno())
    return Path(temporary)


def apply_update(root, payload, stopped=require_server_stopped):
    root = validate_root(root)
    require(payload and all(name in ALLOWED for name in payload), 'Protected update destination')
    stopped()
    records = []
    for name, updated in sorted(payload.items()):
        path = root / name
        require(path.parent.is_dir() and path.parent.resolve() == path.parent,
                'Unsafe update parent: ' + name)
        old = regular(path).read_bytes() if path.exists() or path.is_symlink() else None
        mode = stat.S_IMODE(path.stat().st_mode) if old is not None else 0o644
        records.append((name, path, old, updated, mode))
    parent = root / 'runtime/update-backups'
    require(not parent.is_symlink(), 'Backup directory cannot be a symlink')
    parent.mkdir(mode=0o700, exist_ok=True)
    stamp = datetime.now(UTC).strftime('%Y%m%dT%H%M%SZ') + '-' + uuid.uuid4().hex[:8]
    backup = parent / stamp
    backup.mkdir(mode=0o700)
    staged, applied = [], []
    try:
        receipt = {'schema': 'payguard.demo-update-backup.v1', 'files': {}}
        for name, path, old, updated, mode in records:
            if old is not None:
                saved = backup / name
                saved.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                with saved.open('xb') as handle:
                    os.fchmod(handle.fileno(), 0o600)
                    handle.write(old)
                    handle.flush()
                    os.fsync(handle.fileno())
            receipt['files'][name] = {'previous_sha256': digest(old) if old is not None else None,
                                      'updated_sha256': digest(updated), 'previous_mode': mode}
            staged.append(stage(path, updated, mode))
        (backup / 'manifest.json').write_text(json.dumps(receipt, indent=2) + '\n')
        stopped()  # Do not race a server restarted while the payload was staged.
        for record, temporary in zip(records, staged, strict=True):
            name, path, old, updated, mode = record
            require((regular(path).read_bytes() == old) if old is not None else not path.exists(),
                    'Source changed while updating: ' + name)
            os.replace(temporary, path)
            applied.append(record)
            sync_directory(path.parent)
    except BaseException as error:
        rollback_errors = []
        for name, path, old, updated, mode in reversed(applied):
            try:
                require(regular(path).read_bytes() == updated, 'Updated source changed: ' + name)
                if old is None:
                    path.unlink()
                else:
                    restored = stage(path, old, mode)
                    os.replace(restored, path)
                sync_directory(path.parent)
            except (OSError, UpdateError) as rollback_error:
                rollback_errors.append(str(rollback_error))
        detail = ' Rollback needs review: ' + '; '.join(rollback_errors) if rollback_errors else ''
        raise UpdateError(f'Update stopped; originals remain at {backup}.{detail} Cause: {error}') from error
    finally:
        for temporary in staged:
            temporary.unlink(missing_ok=True)
    return backup


def start_existing(root, device):
    require(device.startswith('/dev/'), 'An explicit serial device is required')
    require_server_stopped()
    environment = dict(os.environ)
    for name in ('PAYGUARD_HW_INITIALIZE', 'PAYGUARD_HW_PROVISION', 'PAYGUARD_AUTOSTART'):
        environment[name] = '0'
    environment.pop('PAYGUARD_CONTROLLER_TOKEN', None)
    python = str(root / '.venv/bin/python')
    args = [python, '-m', 'scripts.launch', '--mode', 'hw', '--device', device,
            '--db', str(root / 'runtime/payguard-hw.db'), '--private-dir', str(root / 'private'),
            '--world', str(root / 'sim/world'), '--open']
    for role in ROLES:
        args += ['--' + role + '-key', str(root / 'private/roles' / (role + '.key'))]
    if sys.platform == 'darwin':
        token_path = regular(root / 'private/controller-token')
        info = token_path.stat()
        if info.st_uid == os.geteuid() and not info.st_mode & 0o077 and info.st_size <= 4096:
            token = token_path.read_bytes().strip()
            if len(token) >= 32 and not any(byte in b' \t\r\n' for byte in token):
                try:
                    subprocess.run(['pbcopy'], input=token, check=True, capture_output=True)
                except (OSError, subprocess.SubprocessError):
                    pass  # Clipboard convenience must not prevent a safe resume.
                else:
                    sys.stdout.write('Control password copied. Paste it in Settings to unlock actions.\n')
                    sys.stdout.flush()
    os.chdir(root)
    os.execve(python, args, environment)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('update', nargs='?', type=Path, help='ZIP/directory; defaults to this zipapp')
    parser.add_argument('--root', required=True, type=Path)
    parser.add_argument('--start', action='store_true')
    parser.add_argument('--device', default=DEFAULT_DEVICE)
    args = parser.parse_args(argv)
    try:
        root = validate_root(args.root)
        require_server_stopped()
        payload = load_payload((args.update or Path(sys.argv[0])).expanduser())
        backup = apply_update(root, payload)
        sys.stdout.write(f'Demo source updated. Previous source files: {backup}\n')
        sys.stdout.write('Existing database, keys, world and FPGA session preserved.\n')
        sys.stdout.flush()
        if args.start:
            start_existing(root, args.device)
        return 0
    except (OSError, UpdateError) as error:
        sys.stderr.write('Update stopped: ' + str(error) + '\n')
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
