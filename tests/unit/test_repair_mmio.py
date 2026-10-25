import hashlib
import importlib.util
from pathlib import Path
import sqlite3
import subprocess

import pytest

ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location('repair_mmio', ROOT / 'hardware/scripts/repair_mmio_write.py')
repair = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(repair)


def journal(path, command=None, mac=None):
    path.mkdir()
    words = [0] * 64
    words[0], words[1], words[2], words[10] = 0x53494c43, 1, 9, 840
    frame = repair.struct.pack('<64I', *words)
    with sqlite3.connect(path / 'external-journal.sqlite') as db:
        db.executescript('CREATE TABLE command_journal(command BLOB,command_mac BLOB);'
                         'CREATE TABLE recovery_events(id INTEGER);')
        db.execute('INSERT INTO command_journal VALUES(?,?)',
                   (frame if command is None else command, bytes(32) if mac is None else mac))


def fixture(tmp_path):
    root, old, new = tmp_path / 'install', tmp_path / 'old', tmp_path / 'new'
    source = root / 'hardware/python/siliconsign/transport.py'
    source.parent.mkdir(parents=True)
    original = subprocess.check_output(['git', 'show', '412ecf2:hardware/python/siliconsign/transport.py'], cwd=ROOT)
    source.write_bytes(original)
    journal(old)
    return root, old, new, source, original


def test_patch_exact_source_and_preserve_journal(tmp_path):
    root, old, new, source, original = fixture(tmp_path)
    before = (old / 'external-journal.sqlite').read_bytes()
    repair.repair(root, old, new)
    assert source.read_bytes() == (ROOT / 'hardware/python/siliconsign/transport.py').read_bytes()
    assert source.with_name('transport.py.before-mmiofix').read_bytes() == original
    assert (old / 'external-journal.sqlite').read_bytes() == before
    assert new.is_dir() and new.stat().st_mode & 0o777 == 0o700
    assert hashlib.sha256(original).hexdigest() == repair.OLD_SHA


def test_uncheckpointed_wal_is_inspected_without_touching_original(tmp_path):
    old = tmp_path / 'old'
    journal(old)
    db = sqlite3.connect(old / 'external-journal.sqlite')
    try:
        db.execute('PRAGMA journal_mode=WAL')
        db.execute('INSERT INTO command_journal SELECT command,command_mac FROM command_journal')
        db.commit()
        before = {p.name: p.read_bytes() for p in old.iterdir()}
        assert repair.check_history(old) == 2
        assert {p.name: p.read_bytes() for p in old.iterdir()} == before
        db.execute('UPDATE command_journal SET command_mac=?', (b'x' * 32,))
        db.commit()
        with pytest.raises(RuntimeError, match='Non-startup-STATUS'):
            repair.check_history(old)
    finally:
        db.close()


@pytest.mark.parametrize('change', ['payment', 'mac', 'session', 'recovery', 'extra', 'modified', 'existing'])
def test_refuse_history_or_source_ambiguity(tmp_path, change):
    root, old, new, source, original = fixture(tmp_path)
    with sqlite3.connect(old / 'external-journal.sqlite') as db:
        frame = bytearray(db.execute('SELECT command FROM command_journal').fetchone()[0])
        if change == 'payment':
            frame[8] = 6
        if change == 'session':
            frame[16] = 1
        db.execute('UPDATE command_journal SET command=?', (bytes(frame),))
        if change == 'mac':
            db.execute('UPDATE command_journal SET command_mac=?', (b'x' * 32,))
        if change == 'recovery':
            db.execute('INSERT INTO recovery_events VALUES(1)')
    if change == 'extra':
        (old / 'bank.sqlite').write_bytes(b'preserve')
    if change == 'modified':
        source.write_bytes(original + b'\n')
    if change == 'existing':
        new.mkdir()
    unchanged = source.read_bytes()
    with pytest.raises(RuntimeError):
        repair.repair(root, old, new)
    assert source.read_bytes() == unchanged
    assert not source.with_name('transport.py.before-mmiofix').exists()
