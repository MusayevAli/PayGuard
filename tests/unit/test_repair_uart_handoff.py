import importlib.util
import os
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location('uart_repair', ROOT / 'hardware/scripts/repair_uart_handoff.py')
repair = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(repair)


@pytest.fixture
def installation(tmp_path):
    root, rule = tmp_path / 'install', tmp_path / 'sudoers.d/payguard-uart'
    rule.parent.mkdir()
    originals = {}
    for relative, *_ in repair.PATCHES:
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        data = subprocess.check_output(['git', 'show', '412ecf2:' + relative], cwd=ROOT)
        path.write_bytes(data)
        originals[path] = data
    calls = []

    def run(args, **kwargs):
        calls.append(args)
        if args[0] == 'bash':
            return subprocess.run(args, **kwargs)
        assert args[0] == 'visudo'
        if args[1] == '-cf':
            assert Path(args[2]).read_text() == f'Defaults!{root / repair.LAUNCHER} !use_pty\n'
        return subprocess.CompletedProcess(args, 0, stdout=str(rule) + ': parsed OK\n', stderr='')

    return root, rule, originals, calls, run


def test_exact_repairs_preserve_backups_and_allow_identical_repeat(installation):
    root, rule, originals, calls, run = installation
    for _ in range(2):
        result = repair.repair(root, rule, run)
        assert result['launcher_sha256'] == repair.PATCHES[0][2]
        assert result['bridge_sha256'] == repair.PATCHES[1][2]
    for relative, _, expected, _, _, suffix, mode in repair.PATCHES:
        path = root / relative
        assert repair.digest(path.read_bytes()) == expected
        assert path.with_name(path.name + suffix).read_bytes() == originals[path]
        assert path.stat().st_mode & 0o777 == mode
    assert rule.read_text() == f'Defaults!{root / repair.LAUNCHER} !use_pty\n'
    assert rule.stat().st_mode & 0o777 == 0o440
    assert [c[0] for c in calls] == ['visudo', 'visudo', 'bash', 'visudo'] * 2
    assert not list(root.rglob('.*'))


@pytest.mark.parametrize('bad', ['launcher', 'bridge', 'backup', 'rule', 'symlink'])
def test_unknown_content_stops_before_any_mutation(installation, bad):
    root, rule, _originals, calls, run = installation
    if bad in ('launcher', 'bridge'):
        path = root / (repair.LAUNCHER if bad == 'launcher' else repair.BRIDGE)
        path.write_bytes(path.read_bytes() + b'\n')
    elif bad == 'backup':
        path = root / repair.LAUNCHER
        path.with_name(path.name + '.before-ttyguard').write_bytes(b'preserve')
    elif bad == 'rule':
        rule.write_bytes(b'Defaults !use_pty\n')
    else:
        rule.symlink_to(root / repair.LAUNCHER)
    before = {p: p.read_bytes() for p in root.rglob('*') if p.is_file()}
    with pytest.raises(RuntimeError):
        repair.repair(root, rule, run)
    assert {p: p.read_bytes() for p in root.rglob('*') if p.is_file()} == before
    assert not calls


def test_invalid_sudo_fragment_prevents_source_change(installation):
    root, rule, originals, _calls, run = installation

    def reject(args, **kwargs):
        if args[:2] == ['visudo', '-cf']:
            raise subprocess.CalledProcessError(1, args)
        return run(args, **kwargs)

    with pytest.raises(subprocess.CalledProcessError):
        repair.repair(root, rule, reject)
    assert all(path.read_bytes() == original for path, original in originals.items())
    assert not rule.exists() and not list(rule.parent.iterdir())


def test_missing_sudo_include_rolls_back_only_new_rule(installation):
    root, rule, _, _, run = installation

    def omitted(args, **kwargs):
        result = run(args, **kwargs)
        result.stdout = '/etc/sudoers: parsed OK\n'
        return result

    with pytest.raises(RuntimeError, match='did not include'):
        repair.repair(root, rule, omitted)
    assert not rule.exists()
    # The guard remains fail-closed; the installer never starts the bridge.
    assert repair.digest((root / repair.LAUNCHER).read_bytes()) == repair.PATCHES[0][2]


def test_direct_uart_guard_precedes_every_physical_reopen(installation):
    root, rule, _, _, run = installation
    repair.repair(root, rule, run)
    launcher = (root / repair.LAUNCHER).read_bytes()
    assert launcher.index(repair.GUARD) < launcher.index(repair.GUARD_NEEDLE)
    assert os.access(root / repair.LAUNCHER, os.X_OK)
