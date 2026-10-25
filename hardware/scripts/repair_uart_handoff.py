#!/usr/bin/env python3
"""Install the narrowly scoped R5 UART handoff repair, as root on the KV260.

This changes only two known source files and one command-specific sudo setting.
It never accesses the FPGA, loads firmware, changes journals or starts a bridge.
After this installer and its enclosing sudo command EXIT, use the direct command:
  exec sudo /opt/payguard-kv260-r5/hardware/scripts/run_uart_bridge.sh \
    /opt/payguard-kv260-r5/private/response.json \
    /var/lib/payguard-fpga-mmiofix --external-gateway
Do not add "bash" after sudo: the setting matches the launcher itself. The
launcher guard refuses a remaining PTY, including one forced by I/O logging.
"""
from __future__ import annotations

import hashlib
import os
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path('/opt/payguard-kv260-r5')
RULE = Path('/etc/sudoers.d/payguard-uart')
LAUNCHER = 'hardware/scripts/run_uart_bridge.sh'
BRIDGE = 'hardware/python/siliconsign/external_bridge.py'
GUARD_NEEDLE = b'\nexec < "$uart_device" > "$uart_device" 2> "$uart_device"\n'
GUARD = b'\n[[ $(tty) == "$uart_device" ]] || { printf "STOP: sudo still owns a PTY; no UART handoff performed.\\n" >&2; exit 2; }\n'
BLANK_NEEDLE = b'            if not line:\n                return 0\n'
BLANK = (b'            # Terminal handoff can leave a blank line; it grants no authority.\n'
         b'            # Oversized input must still reach the existing fail-closed handler.\n'
         b'            if len(line) <= 2048 and not line.strip():\n'
         b'                continue\n')
PATCHES = (
    (LAUNCHER, '254c2af793c61cdcb2de89e29f5aa99fb08a10f658124f66c62ac788f2fc81c7',
     'db8538265d96b51eab75661e7c79634c5eeb57d76f359b76f27a9512c295ffcf',
     GUARD_NEEDLE, GUARD + GUARD_NEEDLE, '.before-ttyguard', 0o700),
    (BRIDGE, '5b4464d9156cfc2fb1d88aabbff930b51725be71c4e41e004a095ab958af2a70',
     '80698927332ac4feb76919aee1c0a52f2d392ba6365c3f2dac05706d63f84b29',
     BLANK_NEEDLE, BLANK_NEEDLE + BLANK, '.before-blankfix', 0o600),
)


def require(condition, message):
    if not condition:
        raise RuntimeError(message)


def digest(data):
    return hashlib.sha256(data).hexdigest()


def read_owned(path):
    require(path.is_file() and not path.is_symlink() and path.resolve() == path,
            'Missing or unsafe file: ' + str(path))
    require(path.stat().st_uid == 0 and path.stat().st_size < 1024 * 1024,
            'File must be root-owned and bounded: ' + str(path))
    return path.read_bytes()


def sync_directory(path):
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def stage(path, data, mode):
    fd, name = tempfile.mkstemp(prefix='.' + path.name + '.', dir=path.parent)
    with os.fdopen(fd, 'wb') as handle:
        handle.write(data)
        handle.flush()
        os.fchmod(handle.fileno(), mode)
        os.fsync(handle.fileno())
    return Path(name)


def preserve(path, data):
    if path.exists() or path.is_symlink():
        require(read_owned(path) == data, 'Conflicting backup preserved: ' + str(path))
        return
    temporary = stage(path, data, 0o600)
    try:
        os.link(temporary, path)  # Exclusive publication; never overwrite a backup.
        sync_directory(path.parent)
    finally:
        temporary.unlink(missing_ok=True)


def repair(root=ROOT, rule=RULE, run=subprocess.run):
    require(root.is_dir() and root.resolve() == root, 'Unsafe installation directory')
    require(rule.parent.is_dir() and rule.parent.resolve() == rule.parent,
            'Unsafe sudoers directory')
    planned = []
    for relative, old_sha, new_sha, needle, replacement, suffix, mode in PATCHES:
        path = root / relative
        current = read_owned(path)
        if digest(current) == old_sha:
            require(current.count(needle) == 1, 'Original patch anchor is ambiguous')
            original, updated = current, current.replace(needle, replacement)
        elif digest(current) == new_sha:
            require(current.count(replacement) == 1, 'Installed patch anchor is ambiguous')
            original, updated = current.replace(replacement, needle), current
        else:
            raise RuntimeError('Unrecognized source; preserved: ' + str(path))
        require(digest(original) == old_sha and digest(updated) == new_sha,
                'Repair source/hash mismatch')
        backup = path.with_name(path.name + suffix)
        if backup.exists() or backup.is_symlink():
            require(read_owned(backup) == original, 'Conflicting backup preserved: ' + str(backup))
        if relative == BRIDGE:
            compile(updated, str(path), 'exec')
        planned.append((path, current, original, updated, backup, mode))
    expected_rule = f'Defaults!{root / LAUNCHER} !use_pty\n'.encode()
    existing_rule = rule.exists() or rule.is_symlink()
    if existing_rule:
        require(read_owned(rule) == expected_rule, 'Existing sudoers rule differs; preserved')
    run(['visudo', '-c'], check=True, capture_output=True, text=True)
    staged_rule = stage(rule, expected_rule, 0o440)
    staged_sources = []
    created_rule = False
    try:
        run(['visudo', '-cf', str(staged_rule)], check=True, capture_output=True, text=True)
        for path, _current, _original, updated, _backup, mode in planned:
            temporary = stage(path, updated, mode)
            staged_sources.append(temporary)
            if path == root / LAUNCHER:
                run(['bash', '-n', str(temporary)], check=True, capture_output=True, text=True)
        for record, temporary in zip(planned, staged_sources, strict=True):
            path, current, original, updated, backup, mode = record
            require(read_owned(path) == current, 'Source changed during repair; stopped')
            preserve(backup, original)
            os.replace(temporary, path)
            sync_directory(path.parent)
        if not existing_rule:
            os.link(staged_rule, rule)
            created_rule = True
            sync_directory(rule.parent)
        checked = run(['visudo', '-c'], check=True, capture_output=True, text=True)
        require(str(rule) + ':' in checked.stdout,
                'Full sudoers check did not include the scoped rule; stopped')
        require(read_owned(rule) == expected_rule, 'Scoped rule changed during repair')
    except BaseException:
        if created_rule and not rule.is_symlink() and rule.read_bytes() == expected_rule:
            rule.unlink()
            sync_directory(rule.parent)
        raise
    finally:
        staged_rule.unlink(missing_ok=True)
        for temporary in staged_sources:
            temporary.unlink(missing_ok=True)
    return {'launcher_sha256': digest((root / LAUNCHER).read_bytes()),
            'bridge_sha256': digest((root / BRIDGE).read_bytes()), 'sudoers_rule': str(rule)}


if __name__ == '__main__':
    try:
        require(os.geteuid() == 0, 'Run as root on the KV260')
        os.umask(0o077)
        result = repair()
        sys.stdout.write('UART repair installed; originals and journals preserved.\n')
        sys.stdout.write('No FPGA access, reload or bridge start was performed.\n')
        sys.stdout.write('Exit this sudo command before the direct launcher command below:\n')
        sys.stdout.write(f'exec sudo {ROOT / LAUNCHER} {ROOT}/private/response.json '
                         '/var/lib/payguard-fpga-mmiofix --external-gateway\n')
    except (OSError, RuntimeError, ValueError, subprocess.SubprocessError) as error:
        raise SystemExit('STOPPED: ' + str(error)) from None
