"""Transfer a private archive using the existing USB UART; no third-party modules.

First print/paste the bootstrap into the existing Kria login terminal. Close that
terminal after READY, then upload. No uploaded archive is extracted or executed.
"""

# ruff: noqa: T201 -- operator CLI; output contains receipts, never archive contents.
from __future__ import annotations

import argparse
import base64
import contextlib
import fcntl
import hashlib
import json
import os
import secrets
import select
import stat
import sys
import termios
import time
import tty
from pathlib import Path

MAX_SIZE = 128 * 1024 * 1024
CHUNK_SIZE = 1024


class UploadError(RuntimeError):
    pass


class SerialLink:
    def __init__(self, path: str):
        self.fd = os.open(path, os.O_RDWR | os.O_NOCTTY | os.O_NONBLOCK)
        self.saved = None
        self.exclusive = False
        self.buffer = bytearray()
        try:
            if not stat.S_ISCHR(os.fstat(self.fd).st_mode) or not os.isatty(self.fd):
                raise UploadError("Device is not a serial TTY")
            fcntl.flock(self.fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            fcntl.ioctl(self.fd, termios.TIOCEXCL)
            self.exclusive = True
            self.saved = termios.tcgetattr(self.fd)
            tty.setraw(self.fd, termios.TCSANOW)
            attrs = termios.tcgetattr(self.fd)
            attrs[4] = attrs[5] = termios.B115200
            attrs[2] |= termios.CLOCAL | termios.CREAD
            termios.tcsetattr(self.fd, termios.TCSANOW, attrs)
        except BaseException:
            self.close()
            raise

    def close(self):
        if self.fd is not None:
            if self.saved is not None:
                with contextlib.suppress(OSError):
                    termios.tcsetattr(self.fd, termios.TCSAFLUSH, self.saved)
            if self.exclusive:
                with contextlib.suppress(OSError):
                    fcntl.ioctl(self.fd, termios.TIOCNXCL)
            os.close(self.fd)
            self.fd = None

    def send(self, line: str):
        data = (line + "\n").encode("ascii")
        deadline = time.monotonic() + 5
        while data:
            if time.monotonic() >= deadline:
                raise UploadError("UART write timed out")
            _, writable, _ = select.select([], [self.fd], [], 0.25)
            if writable:
                try:
                    count = os.write(self.fd, data)
                except BlockingIOError:
                    continue
                data = data[count:]

    def line(self, deadline: float) -> str:
        while True:
            if b"\n" in self.buffer:
                line, _, rest = self.buffer.partition(b"\n")
                self.buffer = bytearray(rest)
                return line.decode("ascii", "replace").strip()
            if len(self.buffer) > 16384:
                raise UploadError("Unexpected UART output; receiver protocol is not active")
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("UART acknowledgement timed out")
            readable, _, _ = select.select([self.fd], [], [], remaining)
            if readable:
                chunk = os.read(self.fd, 4096)
                if not chunk:
                    raise UploadError("UART disconnected")
                self.buffer.extend(chunk)

    def exchange(
        self,
        command: str,
        expected: str | tuple[str, ...],
        *,
        timeout: float = 5,
        attempts: int = 5,
    ) -> str:
        for _ in range(attempts):
            self.send(command)
            deadline = time.monotonic() + timeout
            try:
                while True:
                    reply = self.line(deadline)
                    if reply.startswith(expected):
                        return reply
                    if reply.startswith("PGUP1 ERROR"):
                        raise UploadError(reply)
                    if reply.startswith("PGUP1 NACK"):
                        break
            except TimeoutError:
                continue
        raise UploadError(
            "No valid acknowledgement after bounded retries; preserve the board partial file and inspect the connection"
        )


def _completion(reply: str, path: Path, size: int, fingerprint: str, *, recovered: bool) -> dict:
    response = reply.split()
    if (
        len(response) not in (6, 7)
        or response[:2] != ["PGUP1", "SUCCESS"]
        or response[2] != str(size)
        or response[3] != fingerprint
    ):
        raise UploadError("Board completion receipt does not match the archive size/hash")
    token = response[4]
    if len(token) != 32 or any(c not in "0123456789abcdef" for c in token):
        raise UploadError("Invalid console-return receipt")
    destination = base64.b64decode(response[5], validate=True).decode("utf-8")
    source_name = (
        base64.b64decode(response[6], validate=True).decode("utf-8")
        if len(response) == 7
        else Path(destination).name
    )
    if source_name != path.name:
        raise UploadError("Board completion receipt belongs to a different archive filename")
    return {
        "bytes": size,
        "sha256": fingerprint,
        "file_name": source_name,
        "board_path": destination,
        "console_command": "PGUP1 CONSOLE " + token,
        "receipt_recovered": recovered,
        "archive_executed": False,
        "archive_extracted": False,
        "physical_board_verified": False,
    }


def upload(link: SerialLink, path: Path, *, progress=None) -> dict:
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(fd, "rb") as source:
        info = os.fstat(source.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid() or info.st_mode & 0o077:
            raise UploadError(
                "Archive must be a private regular file owned by this operator (chmod 600)"
            )
        if not 0 < info.st_size <= MAX_SIZE:
            raise UploadError("Archive must be nonempty and at most 128 MiB")
        digest = hashlib.sha256()
        while block := source.read(1024 * 1024):
            digest.update(block)
        fingerprint = digest.hexdigest()
        source.seek(0)
        greeting = link.exchange("PGUP1 HELLO", ("PGUP1 READY ", "PGUP1 SUCCESS "))
        if greeting.startswith("PGUP1 SUCCESS "):
            return _completion(greeting, path, info.st_size, fingerprint, recovered=True)
        ready = greeting.split()
        if len(ready) != 4 or int(ready[2]) < info.st_size or int(ready[3]) != CHUNK_SIZE:
            raise UploadError("Board receiver size/chunk capacity is incompatible")
        filename = base64.b64encode(path.name.encode("utf-8")).decode("ascii")
        link.exchange(f"PGUP1 BEGIN {info.st_size} {fingerprint} {filename}", "PGUP1 BEGIN-OK")
        sent, seq = 0, 0
        while data := source.read(CHUNK_SIZE):
            chunk_hash = hashlib.sha256(data).hexdigest()
            encoded = base64.b64encode(data).decode("ascii")
            expected = f"PGUP1 ACK {seq} {sent + len(data)}"
            if link.exchange(f"PGUP1 DATA {seq} {chunk_hash} {encoded}", expected) != expected:
                raise UploadError("Unexpected sequence/byte-count acknowledgement")
            sent += len(data)
            seq += 1
            if progress is not None:
                progress(sent, info.st_size)
        response = link.exchange("PGUP1 END", "PGUP1 SUCCESS ", timeout=15)
        return _completion(response, path, info.st_size, fingerprint, recovered=False)


def bootstrap() -> str:
    code = Path(__file__).with_name("uart_receiver.py").read_text()
    suffix = secrets.token_hex(8)
    outer, inner = f"PAYGUARD_BOOT_{suffix}", f"PAYGUARD_CODE_{suffix}"
    # exec replaces the original login shell. Any bootstrap failure remains an
    # input sink, rather than letting queued upload bytes reach a shell.
    return f"""exec /bin/bash <<'{outer}'
set -eu
umask 077
trap '' HUP
fail_closed() {{
  trap '' HUP INT QUIT
  printf 'PayGuard bootstrap stopped; UART remains closed. Use operator recovery.\\n' >/dev/tty
  while :; do IFS= read -r pg_discard </dev/tty || sleep 1; done
}}
trap fail_closed EXIT
pg_transfer_dir="$HOME/payguard-transfer-{suffix}"
mkdir -- "$pg_transfer_dir"
cat > "$pg_transfer_dir/receiver.py" <<'{inner}'
{code}
{inner}
exec python3 "$pg_transfer_dir/receiver.py" \\
  --destination "$pg_transfer_dir/payguard-kv260-board.tar.gz" \\
  <>/dev/tty >&0 2>&0
{outer}
"""


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--print-bootstrap", action="store_true")
    parser.add_argument("--device")
    parser.add_argument("--file", type=Path)
    parser.add_argument("--receipt", type=Path, help="New private JSON receipt; never overwritten")
    args = parser.parse_args(argv)
    if args.print_bootstrap:
        if args.device or args.file or args.receipt:
            parser.error("--print-bootstrap is a separate step")
        print(bootstrap(), end="")
        return 0
    if not args.device or not args.file:
        parser.error("supply --device and --file, after installing the receiver bootstrap")
    if args.receipt and (args.receipt.exists() or args.receipt.is_symlink()):
        parser.error("receipt path already exists; choose a new path")
    link = None
    last_progress = 0.0

    def progress(sent, total):
        nonlocal last_progress
        now = time.monotonic()
        if now - last_progress >= 2 or sent == total:
            print(
                f"UART transfer: {sent}/{total} bytes ({100 * sent / total:.1f}%)",
                file=sys.stderr,
                flush=True,
            )
            last_progress = now

    try:
        link = SerialLink(args.device)
        result = upload(link, args.file, progress=progress)
    except (OSError, ValueError, UploadError) as exc:
        print(f"Upload stopped: {exc}", file=sys.stderr)
        return 2
    finally:
        if link is not None:
            link.close()
    # Print the console command only after releasing the serial port. It is a
    # human-operated return-to-console action, never sent by this program.
    receipt_error = False
    if args.receipt:
        try:
            fd = os.open(args.receipt, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
            with os.fdopen(fd, "w") as handle:
                json.dump(result, handle, indent=2)
                handle.write("\n")
        except OSError as exc:
            receipt_error = True
            print(
                f"Upload succeeded, but receipt file could not be saved: {exc}. Keep the completion receipt printed below.",
                file=sys.stderr,
            )
    print(json.dumps(result, indent=2))
    print(
        "UART closed. Reopen your 115200-baud terminal, paste console_command and press Return. Then inspect the archive; nothing has been extracted or deployed."
    )
    return 2 if receipt_error else 0


if __name__ == "__main__":
    raise SystemExit(main())
