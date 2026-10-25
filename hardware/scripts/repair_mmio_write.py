#!/usr/bin/env python3
"""Repair only the known R5 transport; preserve all prior crash evidence.

Run as root after a clean reboot, before loading or starting the FPGA bridge.
This script performs no MMIO and never starts, resets or reloads hardware.
"""
import hashlib
import os
from pathlib import Path
import shutil
import sqlite3
import struct
import tempfile

ROOT = Path('/opt/payguard-kv260-r5')
OLD_STATE = Path('/var/lib/payguard-fpga')
NEW_STATE = Path('/var/lib/payguard-fpga-mmiofix')
OLD_SHA = '12587c0b844d83208bd3110e25c3e278b32524dc95a7b66fd259920b77ef16ac'
OLD_WRITE = '    def _write(self,offset,value):struct.pack_into("<I",self.mem,offset,value)\n'
NEW_WRITE = '    def _write(self,offset,value):\n        # pack_into zero-fills its destination before packing. A transient zero\n        # at CTRL is an invalid AXI command, even when followed by START/ACK.\n        # CPython\'s native memoryview scalar setter copies one unsigned int\n        # without that preliminary clear. Never use a byte/slice assignment.\n        import sys\n        if sys.byteorder != "little" or struct.calcsize("@I") != 4:\n            raise TransportError("MMIO requires native little-endian 32-bit unsigned int")\n        if type(offset) is not int or offset < 0 or offset % 4 or offset > len(self.mem)-4:\n            raise ValueError("MMIO write offset must be an aligned word inside the mapping")\n        if type(value) is not int or not 0 <= value <= 0xffffffff:\n            raise ValueError("MMIO write value must be an unsigned 32-bit integer")\n        with memoryview(self.mem).cast("I") as words:\n            words[offset//4] = value\n'


def require(condition, message):
    if not condition:
        raise RuntimeError(message)


def regular(path):
    require(not path.is_symlink() and path.is_file(), 'Unsafe/missing file: ' + str(path))
    require(path.stat().st_size <= 64 * 1024 * 1024, 'Unexpectedly large file: ' + str(path))
    return path


def check_history(state):
    require(state.is_dir() and not state.is_symlink(), 'Original state directory missing/unsafe')
    allowed = {'external-journal.sqlite', 'external-journal.sqlite-wal',
               'external-journal.sqlite-shm', 'uart-bridge.log',
               'uart-device.previous', 'uart-stty.previous'}
    require(all(p.name in allowed and not p.is_symlink() and p.is_file()
                for p in state.iterdir()), 'Unexpected prior state; manual reconciliation required')
    words = [0] * 64
    words[0], words[1], words[2], words[10] = 0x53494c43, 1, 9, 840
    expected = struct.pack('<64I', *words)
    with tempfile.TemporaryDirectory(prefix='payguard-journal-review-') as directory:
        copied = Path(directory) / 'external-journal.sqlite'
        shutil.copyfile(regular(state / copied.name), copied)
        wal = state / (copied.name + '-wal')
        if wal.exists() or wal.is_symlink():
            shutil.copyfile(regular(wal), Path(str(copied) + '-wal'))
        with sqlite3.connect(copied.as_uri() + '?mode=ro', uri=True) as db:
            require(db.execute('PRAGMA quick_check').fetchall() == [('ok',)], 'Journal integrity failed')
            tables = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            require(tables == {'command_journal', 'recovery_events'}, 'Unknown journal schema')
            require(db.execute('SELECT COUNT(*) FROM recovery_events').fetchone()[0] == 0,
                    'Recovery history exists; manual reconciliation required')
            count = 0
            for command, mac in db.execute('SELECT command,command_mac FROM command_journal'):
                require(command == expected and mac == bytes(32),
                        'Non-startup-STATUS history exists; preserving all state and stopping')
                count += 1
    return count


def repair(root=ROOT, old_state=OLD_STATE, new_state=NEW_STATE):
    require(NEW_WRITE, 'Repair payload is not finalized')
    require(not new_state.exists() and not new_state.is_symlink(), 'New state already exists; preserved')
    source = root / 'hardware/python/siliconsign/transport.py'
    original = regular(source).read_bytes()
    require(hashlib.sha256(original).hexdigest() == OLD_SHA, 'Transport is not the exact original R5 file')
    require(original.count(OLD_WRITE.encode()) == 1, 'Original write function mismatch')
    updated = original.replace(OLD_WRITE.encode(), NEW_WRITE.encode())
    compile(updated, str(source), 'exec')
    count = check_history(old_state)
    backup = source.with_name('transport.py.before-mmiofix')
    with backup.open('xb') as handle:
        os.chmod(backup, 0o600)
        handle.write(original)
        handle.flush()
        os.fsync(handle.fileno())
    fd, temporary = tempfile.mkstemp(prefix='.transport-mmiofix-', dir=source.parent)
    try:
        with os.fdopen(fd, 'wb') as handle:
            handle.write(updated)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, source)
        directory = os.open(source.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        Path(temporary).unlink(missing_ok=True)
    new_state.mkdir(mode=0o700)
    print(f'MMIO writer repaired; preserved {count} unsigned startup STATUS record(s).')
    print('Old journal retained. New state:', new_state)
    print('No hardware access performed. Physical command verification is still pending.')


if __name__ == '__main__':
    try:
        require(os.geteuid() == 0, 'Run as root after a clean reboot')
        os.umask(0o077)
        repair()
    except (OSError, RuntimeError, sqlite3.Error, ValueError) as error:
        raise SystemExit('STOPPED: ' + str(error))
