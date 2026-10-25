"""Resolve the calling UART across /dev/tty aliases and sudo PTY ancestry."""

from __future__ import annotations

import importlib.util
import os
import stat
from pathlib import Path
from types import SimpleNamespace

import pytest

HELPER = Path(__file__).resolve().parents[2] / "hardware/scripts/uart_console.py"
spec = importlib.util.spec_from_file_location("uart_console", HELPER)
console = importlib.util.module_from_spec(spec)
spec.loader.exec_module(console)


@pytest.fixture
def kernel(tmp_path, monkeypatch):
    proc, sys, dev = (tmp_path / x for x in ("proc", "sys", "dev"))
    for path in (proc, sys / "dev/char", dev):
        path.mkdir(parents=True)
    nodes = {}
    original_stat = Path.lstat

    def node_stat(path):
        return nodes[path] if path in nodes else original_stat(path)

    monkeypatch.setattr(Path, "lstat", node_stat)

    def process(pid, parent, number, name="worker ) with spaces"):
        folder = proc / str(pid)
        folder.mkdir(exist_ok=True)
        (folder / "stat").write_text(f"{pid} ({name}) S {parent} {pid} {pid} {number} 0 0 0\n")

    def device(name, number, *, mode=stat.S_IFCHR | 0o600, actual_number=None):
        target = sys / "devices" / "platform" / name
        target.mkdir(parents=True, exist_ok=True)
        (sys / "dev/char" / f"{os.major(number)}:{os.minor(number)}").symlink_to(target)
        node = dev / name
        node.touch()
        nodes[node] = SimpleNamespace(
            st_mode=mode, st_rdev=number if actual_number is None else actual_number
        )
        return node

    def resolve(pid=300, claimed=None):
        return console.resolve_uart(
            start_pid=pid, claimed_tty=claimed, proc_root=proc, sys_root=sys, dev_root=dev
        )

    return SimpleNamespace(
        proc=proc, sys=sys, dev=dev, process=process, device=device, resolve=resolve
    )


def test_alias_resolves_nonzero_uart_from_kernel_not_guess(kernel):
    number = os.makedev(204, 71)
    expected = kernel.device("ttyPS7", number)
    kernel.process(300, 1, number)
    assert kernel.resolve(claimed="/dev/tty") == expected
    assert kernel.resolve() == expected


def test_sudo_pty_finds_original_ancestor_uart(kernel):
    uart = os.makedev(204, 66)
    pty = os.makedev(136, 3)
    expected = kernel.device("ttyPS2", uart)
    kernel.process(300, 200, pty)
    kernel.process(200, 100, pty)
    kernel.process(100, 1, uart)
    assert kernel.resolve(claimed="/dev/tty") == expected
    assert kernel.resolve(claimed="/dev/ttyPS2") == expected
    with pytest.raises(console.ConsoleError, match="disagrees"):
        kernel.resolve(claimed="/dev/ttyPS0")


def test_unrelated_uart_is_never_selected_and_no_port_is_guessed(kernel):
    kernel.device("ttyPS0", os.makedev(204, 64))
    kernel.process(300, 1, os.makedev(136, 0))
    with pytest.raises(console.ConsoleError, match="no port was guessed"):
        kernel.resolve(claimed="/dev/tty")


@pytest.mark.parametrize(
    "path", ["/dev/pts/1", "/dev/ttyUSB0", "/tmp/ttyPS0", "/dev/ttyPS0/../ttyPS1"]
)
def test_claimed_path_cannot_bypass_console_identity(kernel, path):
    with pytest.raises(console.ConsoleError, match="SUDO_TTY"):
        kernel.resolve(claimed=path)


@pytest.mark.parametrize(
    "mode,actual",
    [
        (stat.S_IFLNK | 0o777, None),
        (stat.S_IFREG | 0o600, None),
        (stat.S_IFCHR | 0o600, os.makedev(204, 99)),
    ],
)
def test_device_node_must_be_real_character_device_with_exact_number(kernel, mode, actual):
    number = os.makedev(204, 65)
    kernel.device("ttyPS1", number, mode=mode, actual_number=actual)
    kernel.process(300, 1, number)
    with pytest.raises(console.ConsoleError, match="matching the kernel TTY"):
        kernel.resolve()


def test_parent_cycle_or_unreadable_proc_fails_closed(kernel):
    kernel.process(300, 200, 0)
    kernel.process(200, 300, 0)
    with pytest.raises(console.ConsoleError, match="no port was guessed"):
        kernel.resolve()
    (kernel.proc / "200/stat").unlink()
    with pytest.raises(console.ConsoleError, match="kernel TTY ancestry"):
        kernel.resolve()


def test_proc_fields_allow_parentheses_and_space_in_process_name(kernel):
    number = os.makedev(204, 67)
    kernel.process(300, 200, number)
    assert console.process_terminal(300, kernel.proc) == (200, number)
