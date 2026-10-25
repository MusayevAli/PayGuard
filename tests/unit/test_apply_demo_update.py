import hashlib
import importlib.util
import json
import os
import socket
import subprocess
import zipfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location('demo_update', ROOT / 'scripts/apply_demo_update.py')
update = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(update)


@pytest.fixture
def project(tmp_path):
    root = tmp_path / 'project'
    names = ['scripts/launch.py', 'api/main.py', 'sim/clock.py', 'core/bootstrap.py', 'ui/index.html',
             'ui/app.js', 'ui/style.css', 'runtime/payguard-hw.db',
             'runtime/payguard-hw.db-wal', 'runtime/payguard-hw.db-shm',
             'private/controller-token', '.venv/bin/python']
    names += ['private/roles/' + role + '.key' for role in update.ROLES]
    names += ['sim/world/' + name for name in update.WORLD_FILES]
    for name in names:
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(('preserve-' + name).encode())
        path.chmod(0o600)
    (root / '.venv/bin/python').chmod(0o700)
    (root / 'private/controller-token').write_bytes(b'private-controller-token-' + b'x' * 32)
    return root


def package(tmp_path, files, *, as_zip=True, extra=None, records=None):
    manifest = {'schema': 'payguard.demo-update.v1', 'files': records or {
        name: {'bytes': len(data), 'sha256': hashlib.sha256(data).hexdigest()}
        for name, data in files.items()}}
    members = {'manifest.json': json.dumps(manifest).encode(),
               **{'payload/' + name: data for name, data in files.items()}}
    if extra:
        members.update(extra)
    source = tmp_path / ('update.pyz' if as_zip else 'update-dir')
    if as_zip:
        with zipfile.ZipFile(source, 'w') as archive:
            archive.writestr('__main__.py', b'# installer')
            for name, data in members.items():
                archive.writestr(name, data)
    else:
        for name, data in members.items():
            path = source / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)
    return source


@pytest.mark.parametrize('as_zip', [True, False])
def test_update_preserves_all_state_and_backs_up_only_changed_sources(tmp_path, project, as_zip):
    before = {p.relative_to(project).as_posix(): p.read_bytes()
              for p in project.rglob('*') if p.is_file()}
    files = {'ui/app.js': b'updated-ui', 'api/dataset.py': b'VALUE = 42\n',
             'core/bootstrap.py': b'UPDATED = True\n'}
    source = package(tmp_path, files, as_zip=as_zip)
    backup = update.apply_update(project, update.load_payload(source), stopped=lambda: None)
    assert (project / 'ui/app.js').read_bytes() == b'updated-ui'
    assert (project / 'api/dataset.py').read_bytes() == files['api/dataset.py']
    assert (project / 'core/bootstrap.py').read_bytes() == files['core/bootstrap.py']
    for name, data in before.items():
        if name not in files:
            assert (project / name).read_bytes() == data
    assert (backup / 'ui/app.js').read_bytes() == before['ui/app.js']
    assert (backup / 'core/bootstrap.py').read_bytes() == before['core/bootstrap.py']
    assert not (backup / 'api/dataset.py').exists()
    assert {p.relative_to(backup).as_posix() for p in backup.rglob('*') if p.is_file()} == {
        'ui/app.js', 'core/bootstrap.py', 'manifest.json'}


@pytest.mark.parametrize('name', ['../ui/app.js', '/ui/app.js', 'private/controller-token',
                                 'runtime/payguard-hw.db', 'hardware/rtl/policy_engine.sv',
                                 'core/policy.py', 'core/bootstrap.py/../payments.py'])
def test_protected_and_traversal_manifest_paths_rejected(tmp_path, name):
    source = package(tmp_path, {name: b'bad'})
    with pytest.raises(update.UpdateError, match='protected or unknown'):
        update.load_payload(source)


def test_wrong_hash_and_extra_zip_member_rejected(tmp_path):
    source = package(tmp_path, {'ui/app.js': b'new'}, records={
        'ui/app.js': {'bytes': 3, 'sha256': '0' * 64}})
    with pytest.raises(update.UpdateError, match='hash/size mismatch'):
        update.load_payload(source)
    source = package(tmp_path, {'ui/app.js': b'new'}, extra={'payload/private/key': b'bad'})
    with pytest.raises(update.UpdateError, match='Undeclared'):
        update.load_payload(source)


def test_packager_matches_source_allowlist_without_state(tmp_path, project, monkeypatch):
    spec = importlib.util.spec_from_file_location('package_demo', ROOT / 'scripts/package_demo_update.py')
    packager = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(packager)
    assert set(packager.FILES) == update.ALLOWED and len(update.ALLOWED) == 17
    monkeypatch.setattr(packager, 'ROOT', project)
    for name in packager.FILES:
        target = project / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(b'VALUE = 1\n' if name.endswith('.py') else b'updated-ui')
    (project / 'scripts/apply_demo_update.py').write_bytes((ROOT / 'scripts/apply_demo_update.py').read_bytes())
    output = tmp_path / 'PayGuard-restart-fix.pyz'
    packager.build(output)
    assert set(update.load_payload(output)) == update.ALLOWED
    with zipfile.ZipFile(output) as archive:
        assert all(not name.startswith(('payload/private/', 'payload/runtime/', 'payload/hardware/rtl/'))
                   for name in archive.namelist())
def test_listening_server_refused_without_killing_or_changing_files(project):
    with socket.socket() as server:
        server.bind(('127.0.0.1', 0))
        server.listen()
        port = server.getsockname()[1]
        with pytest.raises(update.UpdateError, match='still listening'):
            update.apply_update(project, {'ui/app.js': b'new'},
                                stopped=lambda: update.require_server_stopped(port))
        assert server.fileno() >= 0
    assert (project / 'ui/app.js').read_bytes() == b'preserve-ui/app.js'
    assert not (project / 'runtime/update-backups').exists()


def test_partial_replacement_failure_restores_sources(project, monkeypatch):
    original_replace = os.replace
    calls = 0

    def fail_second(source, target):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError('injected replacement failure')
        return original_replace(source, target)

    monkeypatch.setattr(update.os, 'replace', fail_second)
    with pytest.raises(update.UpdateError, match='originals remain'):
        update.apply_update(project, {'api/main.py': b'VALUE = 1\n', 'ui/app.js': b'new'},
                            stopped=lambda: None)
    assert (project / 'api/main.py').read_bytes() == b'preserve-api/main.py'
    assert (project / 'ui/app.js').read_bytes() == b'preserve-ui/app.js'
    assert not list(project.rglob('.payguard-update-*'))


def test_symlink_target_rejected_and_unrelated_destination_preserved(project):
    target = project / 'ui/app.js'
    target.unlink()
    target.symlink_to(project / 'private/controller-token')
    before = (project / 'private/controller-token').read_bytes()
    with pytest.raises(update.UpdateError, match='regular file'):
        update.apply_update(project, {'ui/app.js': b'bad'}, stopped=lambda: None)
    assert (project / 'private/controller-token').read_bytes() == before


def test_resume_uses_existing_state_without_initialization_and_copies_token(project, monkeypatch, capsys):
    captured = {}
    monkeypatch.setattr(update, 'require_server_stopped', lambda: None)
    monkeypatch.setattr(update.sys, 'platform', 'darwin')
    monkeypatch.setenv('PAYGUARD_HW_INITIALIZE', '1')
    monkeypatch.setenv('PAYGUARD_HW_PROVISION', '1')
    monkeypatch.setenv('PAYGUARD_AUTOSTART', '1')
    monkeypatch.setenv('PAYGUARD_CONTROLLER_TOKEN', 'stale-secret')
    monkeypatch.setattr(update.os, 'chdir', lambda root: captured.update(root=root))

    def execute(python, args, environment):
        captured.update(python=python, args=args, environment=environment)

    def clipboard(args, **kwargs):
        captured['clipboard'] = kwargs['input']
        assert args == ['pbcopy']
        return subprocess.CompletedProcess(args, 0)

    monkeypatch.setattr(update.os, 'execve', execute)
    monkeypatch.setattr(update.subprocess, 'run', clipboard)
    update.start_existing(project, update.DEFAULT_DEVICE)
    assert captured['python'] == str(project / '.venv/bin/python')
    assert '--initialize' not in captured['args'] and '--provision' not in captured['args']
    assert str(project / 'runtime/payguard-hw.db') in captured['args']
    assert captured['environment']['PAYGUARD_HW_INITIALIZE'] == '0'
    assert captured['environment']['PAYGUARD_HW_PROVISION'] == '0'
    assert captured['environment']['PAYGUARD_AUTOSTART'] == '0'
    assert 'PAYGUARD_CONTROLLER_TOKEN' not in captured['environment']
    assert captured['clipboard'] == (project / 'private/controller-token').read_bytes()
    output = capsys.readouterr().out
    assert 'Control password copied' in output
    assert captured['clipboard'].decode() not in output and 'stale-secret' not in output
