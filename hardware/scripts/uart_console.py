#!/usr/bin/env python3
"""Resolve a Kria UART from kernel controlling-TTY identity, including sudo PTYs.

/dev/tty is a process-relative alias, not the physical device. Walk only this
process's parent chain to recover the original UART across sudo's PTY monitor.
Never scan unrelated processes, choose a default port, or trust a path alone.
"""

from __future__ import annotations

import os
import re
import stat
import sys
from pathlib import Path


class ConsoleError(RuntimeError):
    pass


def process_terminal(pid: int, proc_root: Path) -> tuple[int, int]:
    try:
        record = (proc_root / str(pid) / "stat").read_text()
        prefix, fields = record.rsplit(")", 1)
        if int(prefix.split(" ", 1)[0]) != pid:
            raise ValueError
        values = fields.split()
        parent, encoded = int(values[1]), int(values[4]) & 0xFFFFFFFF
        if parent < 0:
            raise ValueError
        return parent, encoded
    except (OSError, ValueError, IndexError) as exc:
        raise ConsoleError("Cannot verify the calling process's kernel TTY ancestry") from exc


def device_for_terminal(number: int, sys_root: Path, dev_root: Path) -> Path | None:
    if not number:
        return None
    link = sys_root / "dev" / "char" / f"{os.major(number)}:{os.minor(number)}"
    try:
        name = link.resolve(strict=True).name
    except FileNotFoundError:
        return None  # A PTY need not have a sysfs character-device entry.
    except OSError as exc:
        raise ConsoleError("Cannot inspect the controlling TTY in sysfs") from exc
    if not re.fullmatch(r"ttyPS[0-9]+", name):
        return None
    path = dev_root / name
    try:
        metadata = path.lstat()
    except OSError as exc:
        raise ConsoleError("Kernel UART does not have an accessible device node") from exc
    if not stat.S_ISCHR(metadata.st_mode) or metadata.st_rdev != number:
        raise ConsoleError(
            "UART path is not a non-symlink character device matching the kernel TTY"
        )
    return path


def resolve_uart(
    *,
    start_pid: int,
    claimed_tty: str | None = None,
    proc_root: Path = Path("/proc"),
    sys_root: Path = Path("/sys"),
    dev_root: Path = Path("/dev"),
) -> Path:
    if (
        claimed_tty
        and claimed_tty != "/dev/tty"
        and not re.fullmatch(r"/dev/ttyPS[0-9]+", claimed_tty)
    ):
        raise ConsoleError("SUDO_TTY must identify the Kria UART or its /dev/tty alias")
    visited: set[int] = set()
    pid = start_pid
    for _ in range(64):
        if pid <= 1 or pid in visited:
            break
        visited.add(pid)
        parent, terminal = process_terminal(pid, proc_root)
        path = device_for_terminal(terminal, sys_root, dev_root)
        if path is not None:
            if claimed_tty and claimed_tty != "/dev/tty" and Path(claimed_tty).name != path.name:
                raise ConsoleError("SUDO_TTY disagrees with the calling session's kernel UART")
            return path
        pid = parent
    raise ConsoleError(
        "No verified Kria UART exists in this console's process ancestry; no port was guessed"
    )


if __name__ == "__main__":
    try:
        uart = resolve_uart(start_pid=os.getpid(), claimed_tty=os.environ.get("SUDO_TTY"))
    except ConsoleError as exc:
        sys.stderr.write(str(exc) + "\n")
        raise SystemExit(2) from None
    sys.stdout.write(str(uart) + "\n")
