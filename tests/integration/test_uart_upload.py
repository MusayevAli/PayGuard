"""A real OS pseudo-TTY links the stdlib uploader and receiver. No board is claimed."""

import base64
import hashlib
import os
import pty
import select
import signal
import stat
import subprocess
import threading
import time
import tty
from contextlib import contextmanager, suppress
from pathlib import Path

import pytest

from scripts.uart_receiver import serve
from scripts.uart_upload import SerialLink, UploadError, bootstrap, upload
from scripts.uart_upload import main as upload_main


@pytest.fixture
def cable(tmp_path):
    master, slave = pty.openpty()
    tty.setraw(slave)
    path = os.ttyname(slave)
    destination = tmp_path / "board.tar.gz"
    stop = threading.Event()
    returned_to_console = threading.Event()
    errors = []

    def server():
        try:
            serve(master, destination, stop=stop, on_console=returned_to_console.set)
        except BaseException as exc:
            errors.append(exc)

    thread = threading.Thread(target=server, daemon=True)
    thread.start()
    yield path, destination, returned_to_console
    stop.set()
    thread.join(timeout=2)
    os.close(master)
    os.close(slave)
    assert not thread.is_alive()
    assert not errors, errors


def private_file(tmp_path, data):
    source = tmp_path / "host.tar.gz"
    source.write_bytes(data)
    source.chmod(0o600)
    return source


def _read_until(fd, marker, timeout=5):
    received = bytearray()
    deadline = time.monotonic() + timeout
    while marker not in received:
        remaining = deadline - time.monotonic()
        assert remaining > 0, f"Missing {marker!r}; terminal output: {bytes(received)!r}"
        ready, _, _ = select.select([fd], [], [], remaining)
        if ready:
            received.extend(os.read(fd, 65536))
    return bytes(received)


@contextmanager
def _login_bootstrap(tmp_path, script):
    """Paste the real generated script into a login shell with a controlling PTY."""
    board_home = tmp_path / "board-home"
    board_home.mkdir(mode=0o700)
    pid, master = pty.fork()
    if pid == 0:
        try:
            environment = dict(os.environ, HOME=str(board_home), PS1="PG_TEST_LOGIN> ", PS2="")
            os.chdir(board_home)
            # Echo is irrelevant to execution and would duplicate the embedded source.
            import termios

            attrs = termios.tcgetattr(0)
            attrs[3] &= ~termios.ECHO
            termios.tcsetattr(0, termios.TCSANOW, attrs)
            os.execve("/bin/bash", ["bash", "--noprofile", "--norc", "-i"], environment)
        finally:
            os._exit(127)
    try:
        _read_until(master, b"PG_TEST_LOGIN> ")
        pending = memoryview(script.encode())
        while pending:
            pending = pending[os.write(master, pending) :]
        yield master, board_home
    finally:
        with suppress(ProcessLookupError):
            os.kill(pid, signal.SIGKILL)
        os.waitpid(pid, 0)
        os.close(master)


def _master_link(fd):
    # The board occupies the PTY slave. Reuse the real host protocol on its master.
    link = object.__new__(SerialLink)
    link.fd = os.dup(fd)
    link.saved = None
    link.exclusive = False
    link.buffer = bytearray()
    return link


def test_exact_bootstrap_process_ready_upload_and_console_return(tmp_path):
    source = private_file(tmp_path, bytes(range(256)) * 17 + b"complete bootstrap test")
    with _login_bootstrap(tmp_path, bootstrap()) as (master, board_home):
        _read_until(master, b"PGUP1 READY 134217728 1024")
        link = _master_link(master)
        try:
            result = upload(link, source)
            destination = Path(result["board_path"])
            assert board_home in destination.parents
            assert destination.read_bytes() == source.read_bytes()
            link.exchange("touch UNAUTHORIZED", "PGUP1 DONE")
            link.exchange("PGUP1 CONSOLE wrong-token", "PGUP1 DONE")
            assert not (board_home / "UNAUTHORIZED").exists()
        finally:
            link.close()
        # The host transfer client is closed before the human console return.
        os.write(master, (result["console_command"] + "\r").encode())
        _read_until(master, b"Returning to console.")
        os.write(master, b"printf 'PG_SHELL_%s\\n' RETURNED\r")
        _read_until(master, b"PG_SHELL_RETURNED")


@pytest.mark.parametrize("failure", ["read_only_stdin", "unsafe_destination", "bootstrap_setup"])
def test_bootstrap_receiver_failure_is_visible_and_never_returns_to_shell(tmp_path, failure):
    script = bootstrap()
    if failure == "read_only_stdin":
        script = script.replace("<>/dev/tty >&0 2>&0", "</dev/tty >/dev/tty 2>/dev/tty")
    elif failure == "unsafe_destination":
        script = script.replace('mkdir -- "$pg_transfer_dir"', 'mkdir -m 755 -- "$pg_transfer_dir"')
    else:
        script = script.replace('mkdir -- "$pg_transfer_dir"', "false")
    with _login_bootstrap(tmp_path, script) as (master, board_home):
        error = (
            b"PayGuard bootstrap stopped; UART remains closed. Use operator recovery."
            if failure == "bootstrap_setup"
            else b"PGUP1 ERROR RECEIVER_STOPPED_OPERATOR_RECOVERY_REQUIRED"
        )
        _read_until(master, error)
        os.write(master, b"touch UNAUTHORIZED\n")
        if failure == "bootstrap_setup":
            # The shell input sink intentionally does not echo each discarded line.
            assert select.select([master], [], [], 0.1)[0] == []
        else:
            _read_until(master, error)
        assert not (board_home / "UNAUTHORIZED").exists()
        assert not list(board_home.glob("payguard-transfer-*/payguard-kv260-board.tar.gz"))


def test_byte_exact_transfer_and_explicit_console_return(cable, tmp_path):
    device, destination, console = cable
    data = bytes(range(256)) * 129 + b"a final partial chunk\x00\xff"
    source = private_file(tmp_path, data)
    link = SerialLink(device)
    try:
        result = upload(link, source)
        assert destination.read_bytes() == data
        assert result["sha256"] == hashlib.sha256(data).hexdigest()
        assert result["bytes"] == len(data)
        assert result["archive_executed"] is False and result["archive_extracted"] is False
        assert stat.S_IMODE(destination.stat().st_mode) == 0o600
        assert not console.is_set()
    finally:
        link.close()
    terminal = SerialLink(device)
    try:
        terminal.exchange("touch UNAUTHORIZED", "PGUP1 DONE")
        terminal.exchange("PGUP1 CONSOLE wrong-token", "PGUP1 DONE")
        assert not console.is_set()
        os.write(terminal.fd, (result["console_command"] + "\r").encode())
        assert console.wait(1), "ordinary terminal Return (CR) must work"
    finally:
        terminal.close()


def test_chunk_corruption_is_nacked_and_retried(cable, tmp_path):
    device, destination, _ = cable
    data = os.urandom(8193)
    source = private_file(tmp_path, data)

    class CorruptOnce(SerialLink):
        corrupted = False

        def send(self, line):
            if line.startswith("PGUP1 DATA") and not self.corrupted:
                fields = line.split()
                fields[-1] = base64.b64encode(b"corrupted in transit").decode()
                line = " ".join(fields)
                self.corrupted = True
            super().send(line)

    link = CorruptOnce(device)
    try:
        result = upload(link, source)
        assert link.corrupted and result["bytes"] == len(data)
        assert destination.read_bytes() == data
    finally:
        link.close()


def test_lost_completion_receipt_is_recovered_without_rewriting(cable, tmp_path):
    device, destination, console = cable
    source = private_file(tmp_path, os.urandom(2049))
    link = SerialLink(device)
    try:
        original = upload(link, source)
    finally:
        link.close()
    before = destination.stat()
    retry = SerialLink(device)
    try:
        recovered = upload(retry, source)
    finally:
        retry.close()
    assert recovered["receipt_recovered"] is True
    assert recovered["console_command"] == original["console_command"]
    assert recovered["sha256"] == original["sha256"]
    assert recovered["file_name"] == source.name
    assert destination.stat().st_mtime_ns == before.st_mtime_ns
    assert destination.read_bytes() == source.read_bytes()
    assert not console.is_set()


@pytest.mark.parametrize("mismatch", ["size", "hash", "name"])
def test_completed_receipt_refuses_a_different_archive(cable, tmp_path, mismatch):
    device, destination, console = cable
    original_bytes = b"original private archive"
    source = private_file(tmp_path, original_bytes)
    link = SerialLink(device)
    try:
        upload(link, source)
    finally:
        link.close()
    if mismatch == "size":
        source.write_bytes(original_bytes + b"different length")
    elif mismatch == "hash":
        source.write_bytes(b"X" * len(original_bytes))
    else:
        other = source.with_name("different-name.tar.gz")
        source.rename(other)
        source = other
    retry = SerialLink(device)
    try:
        with pytest.raises(UploadError, match="completion receipt"):
            upload(retry, source)
    finally:
        retry.close()
    assert destination.read_bytes() == original_bytes
    assert not console.is_set()


def test_lost_ack_does_not_duplicate_bytes(cable, tmp_path):
    device, destination, _ = cable
    data = os.urandom(2049)
    source = private_file(tmp_path, data)

    class LostAck(SerialLink):
        dropped = False

        def exchange(self, command, expected, **kwargs):
            return super().exchange(command, expected, timeout=0.1, attempts=4)

        def line(self, deadline):
            while True:
                reply = super().line(deadline)
                if reply.startswith("PGUP1 ACK 0 ") and not self.dropped:
                    self.dropped = True
                    continue
                return reply

    link = LostAck(device)
    try:
        upload(link, source)
        assert link.dropped
        assert destination.read_bytes() == data
    finally:
        link.close()


def test_existing_destination_is_not_overwritten(cable, tmp_path):
    device, destination, console = cable
    destination.write_bytes(b"existing private artifact")
    source = private_file(tmp_path, b"replacement")
    link = SerialLink(device)
    try:
        with pytest.raises(UploadError, match="EXISTS"):
            upload(link, source)
        assert destination.read_bytes() == b"existing private artifact"
        assert not console.is_set()
    finally:
        link.close()


def test_whole_file_hash_failure_never_publishes_or_returns_console(cable):
    device, destination, console = cable
    data = b"valid chunk but wrong declared final file hash"
    link = SerialLink(device)
    try:
        link.exchange("PGUP1 HELLO", "PGUP1 READY")
        link.exchange(f"PGUP1 BEGIN {len(data)} {'0' * 64}", "PGUP1 BEGIN-OK")
        line = f"PGUP1 DATA 0 {hashlib.sha256(data).hexdigest()} {base64.b64encode(data).decode()}"
        link.exchange(line, "PGUP1 ACK 0")
        with pytest.raises(UploadError, match="FILE_HASH"):
            link.exchange("PGUP1 END", "PGUP1 SUCCESS")
        assert not destination.exists()
        assert destination.with_name("." + destination.name + ".partial").read_bytes() == data
        assert not console.is_set()
    finally:
        link.close()


def test_receiver_rejects_oversize_before_creating_file(cable):
    device, destination, console = cable
    link = SerialLink(device)
    try:
        link.exchange("PGUP1 HELLO", "PGUP1 READY")
        with pytest.raises(UploadError, match="HEADER"):
            link.exchange(f"PGUP1 BEGIN {128 * 1024 * 1024 + 1} {'1' * 64}", "PGUP1 BEGIN-OK")
        assert not destination.exists()
        assert not destination.with_name("." + destination.name + ".partial").exists()
        assert not console.is_set()
    finally:
        link.close()


def test_receiver_preserves_an_existing_partial_file(cable, tmp_path):
    device, destination, console = cable
    partial = destination.with_name("." + destination.name + ".partial")
    partial.write_bytes(b"preserve failed transfer evidence")
    source = private_file(tmp_path, b"new upload")
    link = SerialLink(device)
    try:
        with pytest.raises(UploadError, match="PARTIAL_EXISTS"):
            upload(link, source)
        assert partial.read_bytes() == b"preserve failed transfer evidence"
        assert not destination.exists()
        assert not console.is_set()
    finally:
        link.close()


def test_receipt_save_failure_still_returns_console_code_after_closing_uart(
    cable, tmp_path, capsys
):
    import json

    device, destination, _ = cable
    source = private_file(tmp_path, b"private archive transfer test")
    result = upload_main(
        [
            "--device",
            device,
            "--file",
            str(source),
            "--receipt",
            str(tmp_path / "missing-directory" / "receipt.json"),
        ]
    )
    captured = capsys.readouterr()
    assert result == 2 and "Upload succeeded" in captured.err
    receipt, _ = json.JSONDecoder().raw_decode(captured.out)
    assert receipt["console_command"].startswith("PGUP1 CONSOLE ")
    assert destination.read_bytes() == source.read_bytes()
    reopened = SerialLink(device)
    reopened.close()


def test_bootstrap_is_valid_shell_with_bounded_lines_and_original_shell_exec():
    script = bootstrap()
    assert script.startswith("exec /bin/bash <<")
    assert "trap fail_closed EXIT" in script
    assert "exec python3" in script
    assert "<>/dev/tty >&0 2>&0" in script
    assert max(len(line.encode()) for line in script.splitlines()) < 4096
    checked = subprocess.run(["bash", "-n"], input=script, text=True, capture_output=True)
    assert checked.returncode == 0, checked.stderr
