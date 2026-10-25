"""Explicit operator reconciliation of one orphaned, read-only STATUS request.

Never imported by normal startup. Verification runs on a private database copy;
the original lock is cleared only after authenticated, unchanged board state.
"""

from __future__ import annotations

import argparse
import json
import os
import socket
import sqlite3
import sys
import tempfile
import uuid
from datetime import UTC, datetime
from pathlib import Path

GENERIC_LOCK = "unfinished hardware transaction; reconcile journals before restarting"


def reconcile(path, pending_id, transport, keys):
    # isort: off
    from signer.hardware import HardwareLocked, HardwareSigner
    from siliconsign.protocol import Op, command
    # isort: on

    path = Path(path).resolve(strict=True)
    original = sqlite3.connect(path.as_uri() + "?mode=rw", uri=True, isolation_level=None)
    original.row_factory = sqlite3.Row
    original.execute("PRAGMA synchronous=FULL")
    baseline = original.execute("SELECT data FROM pg_hw_state WHERE id=1").fetchone()[0]
    state = json.loads(baseline)
    pending = original.execute(
        "SELECT * FROM pg_hw_journal WHERE state='pending' ORDER BY id"
    ).fetchall()
    expected = (
        command(Op.STATUS, session=bytes.fromhex(state["session"]), currency=978).pack().hex()
    )
    if (
        not state["initialized"]
        or not state["locked"]
        or state["lock_reason"] != GENERIC_LOCK
        or len(pending) != 1
        or pending[0]["id"] != pending_id
        or pending[0]["command_hex"] != expected
        or pending[0]["request_mac_hex"] != bytes(32).hex()
        or pending[0]["response_hex"] is not None
        or pending[0]["response_mac_hex"] is not None
        or pending[0]["error"] is not None
        or original.execute("SELECT max(id) FROM pg_hw_journal").fetchone()[0] != pending_id
    ):
        original.close()
        raise HardwareLocked(
            "Refusing: this is not the single orphaned canonical STATUS with missing error evidence."
        )
    pending_snapshot = tuple(pending[0])
    backup = path.parent / (
        "status-recovery-"
        + datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
        + "-"
        + uuid.uuid4().hex[:8]
        + ".db"
    )
    fd = os.open(backup, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    os.close(fd)
    with sqlite3.connect(backup) as saved:
        original.backup(saved)
    print("Preserved database backup:", backup, flush=True)  # noqa: T201

    class StatusOnlyVerifier(HardwareSigner):
        def close(self):
            # The caller owns the serial port through the atomic commit.
            with self._lock:
                if not self._closed:
                    self._con.close()
                    self._closed = True

        def _recoverable_status_polls(self, rows):
            # Operator-requested reconciliation, not evidence of a known timeout.
            return len(rows) == 1 and tuple(rows[0]) == pending_snapshot

        def _exchange(self, frame, **kwargs):
            if frame.op != Op.STATUS:
                raise HardwareLocked("Recovery may only send STATUS")
            return super()._exchange(frame, **kwargs)

    try:
        with tempfile.TemporaryDirectory(prefix="status-verify-", dir=path.parent) as folder:
            candidate = Path(folder) / "verification.db"
            candidate.write_bytes(backup.read_bytes())
            candidate.chmod(0o600)
            verifier = StatusOnlyVerifier(
                candidate,
                transport=transport,
                admin_key=keys[0],
                controller_key=keys[1],
                response_key=keys[2],
                initialize=False,
            )
            try:
                verified = dict(verifier._state)
            finally:
                verifier.close()
            if any(
                verified[k] != value
                for k, value in state.items()
                if k not in ("locked", "lock_reason", "hardware_time")
            ):
                raise HardwareLocked("Refusing: verified state changed beyond the read clock")
            with sqlite3.connect(candidate) as checked:
                new_rows = checked.execute(
                    "SELECT * FROM pg_hw_journal WHERE id>?", (pending_id,)
                ).fetchall()
            if len(new_rows) != 1 or new_rows[0][3] != "complete" or new_rows[0][1] != expected:
                raise HardwareLocked("Refusing: missing completed STATUS proof")
            original.execute("BEGIN IMMEDIATE")
            current = original.execute(
                "SELECT * FROM pg_hw_journal WHERE state='pending' ORDER BY id"
            ).fetchall()
            if (
                original.execute("SELECT data FROM pg_hw_state WHERE id=1").fetchone()[0]
                != baseline
                or len(current) != 1
                or tuple(current[0]) != pending_snapshot
                or original.execute("SELECT max(id) FROM pg_hw_journal").fetchone()[0] != pending_id
            ):
                raise HardwareLocked("Database changed during verification; original lock retained")
            note = (
                "Explicit operator reconciliation of orphaned STATUS; original failure unknown. "
                "Fresh signed STATUS verified; session, role sequences, spent amount and authorization count unchanged. No payment replayed."
            )
            original.execute("INSERT INTO pg_hw_journal VALUES(?,?,?,?,?,?,?)", new_rows[0])
            original.execute(
                "UPDATE pg_hw_journal SET state='read_reconciled',error=? WHERE id=?",
                (note, pending_id),
            )
            original.execute(
                "UPDATE pg_hw_state SET data=? WHERE id=1", (json.dumps(verified, sort_keys=True),)
            )
            original.execute("COMMIT")
            print(  # noqa: T201
                "RECOVERED: signed FPGA state verified; no payment sent, no board reset.",
                flush=True,
            )
    finally:
        if original.in_transaction:
            original.execute("ROLLBACK")
        original.close()
    return backup


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--pending-id", type=int, required=True)
    parser.add_argument("--device", default="/dev/cu.usbserial-XFL1N5D0EKLC1")
    parser.add_argument("--start", action="store_true")
    args = parser.parse_args()
    root = args.root.expanduser().resolve(strict=True)
    sys.path.insert(0, str(root))
    # isort: off
    from signer.hardware import HardwareLocked
    from signer.authority import load_role_key
    from siliconsign.transport import SerialTransport

    # isort: on
    with socket.socket() as probe:
        probe.settimeout(0.3)
        if probe.connect_ex(("127.0.0.1", 8000)) == 0:
            raise SystemExit("Stop the Mac server before recovery. No changes made.")
    keys = [
        load_role_key(root / "private/roles" / (role + ".key"), role)
        for role in ("admin", "controller", "response")
    ]
    transport = SerialTransport(args.device)
    try:
        reconcile(root / "runtime/payguard-hw.db", args.pending_id, transport, keys)
    except (HardwareLocked, OSError, ValueError, sqlite3.Error) as exc:
        raise SystemExit("Recovery stopped; no reset or payment attempted: " + str(exc)) from exc
    finally:
        transport.close()
    if args.start:
        os.chdir(root)
        env = dict(os.environ)
        env.pop("PAYGUARD_CONTROLLER_TOKEN", None)
        argv = [
            str(root / ".venv/bin/python"),
            "-m",
            "scripts.launch",
            "--mode",
            "hw",
            "--device",
            args.device,
            "--db",
            str(root / "runtime/payguard-hw.db"),
            "--private-dir",
            str(root / "private"),
            "--world",
            str(root / "sim/world"),
            "--open",
        ]
        for role in ("admin", "controller", "response"):
            argv += ["--" + role + "-key", str(root / "private/roles" / (role + ".key"))]
        os.execve(argv[0], argv, env)


if __name__ == "__main__":
    main()
