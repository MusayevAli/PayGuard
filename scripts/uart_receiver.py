"""Private, bounded UART file receiver. No uploaded content is ever executed."""

from __future__ import annotations

import argparse
import base64
import contextlib
import fcntl
import hashlib
import hmac
import os
import secrets
import select
import signal
import sys
import termios
import time
import tty
from pathlib import Path

MAX_SIZE = 128 * 1024 * 1024
CHUNK_SIZE = 1024
MAX_LINE = 8192


def write_line(fd: int, line: str) -> None:
    data = (line + "\n").encode("ascii")
    while data:
        _, writable, _ = select.select([], [fd], [], 5)
        if not writable:
            raise TimeoutError("UART write timed out")
        try:
            sent = os.write(fd, data)
        except BlockingIOError:
            continue
        data = data[sent:]


class Receiver:
    def __init__(self, destination: Path, max_size: int = MAX_SIZE):
        if not 1 <= max_size <= MAX_SIZE:
            raise ValueError("Invalid size limit")
        self.path = destination.absolute()
        self.dir_fd = os.open(self.path.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        info = os.fstat(self.dir_fd)
        if info.st_uid != os.geteuid() or info.st_mode & 0o077:
            os.close(self.dir_fd)
            raise ValueError("Destination directory must be private and owned by this user")
        self.max_size = max_size
        self.partial = "." + self.path.name + ".partial"
        self.fd = None
        self.total = 0
        self.seq = 0
        self.size = None
        self.expected_hash = None
        self.source_name = None
        self.digest = hashlib.sha256()
        self.previous_hash = None
        self.result = None
        self.failed = False
        self.console_token = None

    def close(self):
        if self.fd is not None:
            os.close(self.fd)
            self.fd = None
        os.close(self.dir_fd)

    def _begin(self, fields):
        if len(fields) not in (4, 5):
            return "PGUP1 ERROR HEADER"
        size = int(fields[2])
        digest = fields[3]
        name = (
            base64.b64decode(fields[4], validate=True).decode("utf-8")
            if len(fields) == 5
            else self.path.name
        )
        if (
            not name
            or len(name.encode("utf-8")) > 255
            or name in (".", "..")
            or any(c in name for c in ("/", "\\", "\x00"))
        ):
            return "PGUP1 ERROR HEADER"
        if (
            not 0 < size <= self.max_size
            or len(digest) != 64
            or any(c not in "0123456789abcdef" for c in digest)
        ):
            return "PGUP1 ERROR HEADER"
        if self.size is not None:
            return (
                "PGUP1 BEGIN-OK"
                if self.seq == 0
                and size == self.size
                and digest == self.expected_hash
                and name == self.source_name
                else "PGUP1 ERROR ALREADY_STARTED"
            )
        try:
            os.stat(self.path.name, dir_fd=self.dir_fd, follow_symlinks=False)
        except FileNotFoundError:
            pass
        else:
            self.failed = True
            return "PGUP1 ERROR EXISTS"
        try:
            self.fd = os.open(
                self.partial,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                0o600,
                dir_fd=self.dir_fd,
            )
        except FileExistsError:
            self.failed = True
            return "PGUP1 ERROR PARTIAL_EXISTS"
        self.size, self.expected_hash = size, digest
        self.source_name = name  # Logical identity only; never used as a filesystem path.
        return "PGUP1 BEGIN-OK"

    def _data(self, fields):
        if self.fd is None or len(fields) != 5:
            return "PGUP1 ERROR DATA_STATE"
        seq, digest = int(fields[2]), fields[3]
        data = base64.b64decode(fields[4], validate=True)
        if (
            not data
            or len(data) > CHUNK_SIZE
            or not hmac.compare_digest(hashlib.sha256(data).hexdigest(), digest)
        ):
            return f"PGUP1 NACK {self.seq} CHUNK_HASH"
        if seq == self.seq - 1 and digest == self.previous_hash:
            return f"PGUP1 ACK {seq} {self.total}"
        if seq != self.seq:
            return f"PGUP1 NACK {self.seq} SEQUENCE"
        if self.total + len(data) > self.size:
            return f"PGUP1 NACK {self.seq} SIZE"
        view = memoryview(data)
        while view:
            count = os.write(self.fd, view)
            view = view[count:]
        self.digest.update(data)
        self.total += len(data)
        self.seq += 1
        self.previous_hash = digest
        return f"PGUP1 ACK {seq} {self.total}"

    def _finish(self):
        if self.fd is None or self.total != self.size:
            return "PGUP1 ERROR INCOMPLETE"
        if not hmac.compare_digest(self.digest.hexdigest(), self.expected_hash):
            self.failed = True
            return "PGUP1 ERROR FILE_HASH"
        os.fsync(self.fd)
        os.close(self.fd)
        self.fd = None
        try:
            os.link(
                self.partial,
                self.path.name,
                src_dir_fd=self.dir_fd,
                dst_dir_fd=self.dir_fd,
                follow_symlinks=False,
            )
        except FileExistsError:
            self.failed = True
            return "PGUP1 ERROR EXISTS"
        os.unlink(self.partial, dir_fd=self.dir_fd)
        os.fsync(self.dir_fd)
        self.console_token = secrets.token_hex(16)
        location = base64.b64encode(str(self.path).encode()).decode("ascii")
        source_name = base64.b64encode(self.source_name.encode("utf-8")).decode("ascii")
        self.result = f"PGUP1 SUCCESS {self.total} {self.expected_hash} {self.console_token} {location} {source_name}"
        return self.result

    def handle(self, line: str) -> str:
        """The sole parser is this closed protocol. No shell/eval/path command exists."""
        if self.result is not None:
            return (
                self.result
                if line in ("PGUP1 HELLO", "PGUP1 END")
                else "PGUP1 DONE CLOSE_CLIENT_THEN_CONSOLE"
            )
        if self.failed:
            return "PGUP1 ERROR LOCKED_PRESERVE_PARTIAL"
        if line == "PGUP1 HELLO":
            return f"PGUP1 READY {self.max_size} {CHUNK_SIZE}"
        try:
            fields = line.split()
            if len(fields) < 2 or fields[0] != "PGUP1":
                return "PGUP1 ERROR PROTOCOL"
            if fields[1] == "BEGIN":
                return self._begin(fields)
            if fields[1] == "DATA":
                return self._data(fields)
            if line == "PGUP1 END":
                return self._finish()
        except (ValueError, TypeError, OverflowError):
            return f"PGUP1 NACK {self.seq} MALFORMED"
        return "PGUP1 ERROR PROTOCOL"


def serve(fd: int, destination: Path, *, stop=None, on_console=None) -> None:
    """Serve indefinitely; stop/on_console are injected only by isolated PTY tests."""
    receiver = Receiver(destination)
    buffer = bytearray()
    discarding = False
    try:
        write_line(fd, receiver.handle("PGUP1 HELLO"))
        while stop is None or not stop.is_set():
            readable, _, _ = select.select([fd], [], [], 0.2)
            if not readable:
                continue
            chunk = os.read(fd, 4096)
            if not chunk:
                time.sleep(0.05)
                continue
            for byte in chunk:
                if byte in (10, 13):
                    if discarding:
                        reply = "PGUP1 ERROR LINE_TOO_LONG"
                    else:
                        line = buffer.decode("ascii", "replace").rstrip("\r")
                        if (
                            receiver.result is not None
                            and receiver.console_token is not None
                            and line.isascii()
                            and hmac.compare_digest(line, "PGUP1 CONSOLE " + receiver.console_token)
                        ):
                            buffer.clear()
                            termios.tcflush(fd, termios.TCIFLUSH)
                            if on_console is not None:
                                on_console()
                            return
                        reply = receiver.handle(line)
                    buffer.clear()
                    discarding = False
                    write_line(fd, reply)
                elif len(buffer) < MAX_LINE and not discarding:
                    buffer.append(byte)
                else:
                    buffer.clear()
                    discarding = True
    finally:
        receiver.close()


def fail_closed(fd: int, *, report_fd: int | None = None) -> None:
    """A fault retains the console; file bytes can never fall through to a shell."""
    for sig in (signal.SIGHUP, signal.SIGINT, signal.SIGQUIT):
        signal.signal(sig, signal.SIG_IGN)
    if report_fd is None:
        report_fd = fd
    error = "PGUP1 ERROR RECEIVER_STOPPED_OPERATOR_RECOVERY_REQUIRED"
    # Report immediately, including when a legacy bootstrap supplied read-only
    # stdin. Its separate stderr handle is still writable. Never print payloads.
    with contextlib.suppress(OSError, TimeoutError):
        write_line(report_fd, error)
    while True:
        try:
            ready, _, _ = select.select([fd], [], [], 1)
            if ready:
                if not os.read(fd, 4096):
                    time.sleep(0.05)
                    continue
                write_line(report_fd, error)
        except (OSError, TimeoutError):
            time.sleep(0.05)
            continue


def main() -> None:
    signal.signal(signal.SIGHUP, signal.SIG_IGN)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--destination", required=True, type=Path)
    args = parser.parse_args()
    fd = sys.stdin.fileno()
    try:
        if not os.isatty(fd):
            raise RuntimeError("Receiver requires the existing login UART")
        if fcntl.fcntl(fd, fcntl.F_GETFL) & os.O_ACCMODE != os.O_RDWR:
            raise RuntimeError("Receiver console must be opened read/write")
        saved = termios.tcgetattr(fd)
        tty.setraw(fd, termios.TCSANOW)
        attrs = termios.tcgetattr(fd)
        attrs[4] = attrs[5] = termios.B115200
        attrs[2] |= termios.CLOCAL | termios.CREAD
        termios.tcsetattr(fd, termios.TCSANOW, attrs)
        serve(fd, args.destination)
        # The only normal return is the exact, unpredictable console command
        # after verified SUCCESS. Uploader never sends that command.
        termios.tcflush(fd, termios.TCIFLUSH)
        termios.tcsetattr(fd, termios.TCSAFLUSH, saved)
        os.write(
            fd,
            b"\r\nUpload verified. Archive has not been extracted or executed. Returning to console.\r\n",
        )
        for destination_fd in (0, 1, 2):
            os.dup2(fd, destination_fd)
        os.execv("/bin/bash", ["/bin/bash", "-l"])
    except BaseException:
        fail_closed(fd, report_fd=sys.stderr.fileno())


if __name__ == "__main__":
    main()
