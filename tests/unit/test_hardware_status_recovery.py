"""Read-only UART loss may recover; uncertain authority changes must stay locked.

These are host/reference-model and local pseudo-terminal tests, not board tests.
"""

from __future__ import annotations

import json
import os
import pty
import sqlite3
import threading
import time

import pytest

# isort: off
# Importing the signer establishes the vendored hardware-client import path.
from signer.hardware import HardwareLocked, HardwareUnavailable
from siliconsign.protocol import HELLO_REQUEST, Op, command, envelope
from siliconsign.transport import SerialTransport, TransportError
from tests.unit.test_hardware_signer import ACCOUNT, ExplicitModelTransport, make_signer, request
# isort: on

SERIAL_TIMEOUT = "serial response timeout; do not reissue payment"
OLD_LOCK_PREFIX = "uncertain hardware transaction; do not retry or reset: "
OVERWRITTEN_LOCK = "unfinished hardware transaction; reconcile journals before restarting"


def old_pending_read(signer, *, frame=None, reason=SERIAL_TIMEOUT):
    """Reproduce the prior version's durable lock after a lost STATUS reply."""
    frame = frame or command(Op.STATUS, session=signer.session, currency=978)
    signer._con.execute(
        "INSERT INTO pg_hw_journal(command_hex,request_mac_hex,state) VALUES(?,?,'pending')",
        (frame.pack().hex(), bytes(32).hex()),
    )
    signer._state.update(locked=True, lock_reason=OLD_LOCK_PREFIX + reason)
    signer._save_state()


def test_lost_status_recovers_without_reset_or_payment(tmp_path):
    signer, transport = make_signer(tmp_path / "state.db")
    transport.lose_response = True
    before = len(transport.requests)
    with pytest.raises(HardwareUnavailable, match="no payment was authorized"):
        signer.status()
    assert not signer._state["locked"]
    assert (
        signer._con.execute("SELECT state FROM pg_hw_journal ORDER BY id DESC LIMIT 1").fetchone()[
            0
        ]
        == "read_failed"
    )
    transport.lose_response = False
    assert signer.status().daily_spent_minor == 0
    assert [frame.op for frame, _ in transport.requests[before:]] == [Op.STATUS, Op.STATUS]
    assert transport.model.auth_counter == 0


def test_no_mutation_until_read_recovery_authenticates(tmp_path):
    signer, transport = make_signer(tmp_path / "state.db")
    assert signer.provision_vendor("V-0001", ACCOUNT, 10_000).ok
    raw, _ = request(signer)
    transport.lose_response = True
    with pytest.raises(HardwareUnavailable):
        signer.status()
    before = len(transport.requests)
    with pytest.raises(HardwareUnavailable):
        signer.sign(raw)
    assert [frame.op for frame, _ in transport.requests[before:]] == [Op.STATUS]
    assert transport.model.spent == 0
    transport.lose_response = False
    assert signer.sign(raw).ok
    assert [frame.op for frame, _ in transport.requests[before:]] == [
        Op.STATUS,
        Op.STATUS,
        Op.COMMIT_INVOICE,
        Op.PAY,
    ]


def test_transient_status_survives_restart_without_initialization(tmp_path):
    path = tmp_path / "state.db"
    signer, transport = make_signer(path)
    transport.lose_response = True
    with pytest.raises(HardwareUnavailable):
        signer.status()
    session = signer.session
    signer.close()
    transport.lose_response = False
    before = len(transport.requests)
    reopened, _ = make_signer(path, transport)
    assert reopened.session == session
    assert [frame.op for frame, _ in transport.requests[before:]] == [Op.STATUS]


def test_old_timeout_lock_reconciles_only_by_new_authenticated_status(tmp_path):
    path = tmp_path / "state.db"
    signer, transport = make_signer(path)
    assert signer.provision_vendor("V-0001", ACCOUNT, 10_000).ok
    raw, _ = request(signer)
    assert signer.sign(raw).ok
    session = signer.session
    old_pending_read(signer)
    signer.close()
    before = len(transport.requests)
    reopened, _ = make_signer(path, transport)
    assert [frame.op for frame, _ in transport.requests[before:]] == [Op.STATUS]
    assert reopened.session == session
    assert reopened._state["spent"] == 100
    assert reopened._state["auth_counter"] == 1
    assert not reopened._state["locked"]
    assert (
        reopened._con.execute(
            "SELECT count(*) FROM pg_hw_journal WHERE state='read_reconciled'"
        ).fetchone()[0]
        == 1
    )
    assert (
        reopened._con.execute(
            "SELECT count(*) FROM pg_hw_journal WHERE state='pending'"
        ).fetchone()[0]
        == 0
    )


def test_overwritten_startup_lock_requires_retained_status_timeout_evidence(tmp_path):
    path = tmp_path / "state.db"
    signer, transport = make_signer(path)
    old_pending_read(signer)
    signer._con.execute(
        "UPDATE pg_hw_journal SET error=? WHERE state='pending'",
        ("TransportError: " + SERIAL_TIMEOUT,),
    )
    signer._state["lock_reason"] = OVERWRITTEN_LOCK
    signer._save_state()
    signer.close()
    before = len(transport.requests)
    reopened, _ = make_signer(path, transport)
    assert [frame.op for frame, _ in transport.requests[before:]] == [Op.STATUS]
    assert not reopened._state["locked"]
    assert (
        reopened._con.execute(
            "SELECT count(*) FROM pg_hw_journal WHERE state='read_reconciled'"
        ).fetchone()[0]
        == 1
    )


@pytest.mark.parametrize(
    "error", [None, "", "HardwareLocked: invalid response MAC", "unexpected process interruption"]
)
def test_overwritten_lock_without_proven_timeout_remains_locked(tmp_path, error):
    path = tmp_path / "state.db"
    signer, transport = make_signer(path)
    old_pending_read(signer)
    signer._con.execute("UPDATE pg_hw_journal SET error=? WHERE state='pending'", (error,))
    signer._state["lock_reason"] = OVERWRITTEN_LOCK
    signer._save_state()
    signer.close()
    before = len(transport.requests)
    with pytest.raises(HardwareLocked, match="unfinished hardware transaction"):
        make_signer(path, transport)
    assert len(transport.requests) == before


def test_overwritten_lock_does_not_recover_mutation_even_with_timeout_error(tmp_path):
    path = tmp_path / "state.db"
    signer, transport = make_signer(path)
    old_pending_read(signer, frame=command(Op.PAY, session=signer.session, currency=978))
    signer._con.execute(
        "UPDATE pg_hw_journal SET error=? WHERE state='pending'",
        ("TransportError: " + SERIAL_TIMEOUT,),
    )
    signer._state["lock_reason"] = OVERWRITTEN_LOCK
    signer._save_state()
    signer.close()
    before = len(transport.requests)
    with pytest.raises(HardwareLocked, match="unfinished hardware transaction"):
        make_signer(path, transport)
    assert len(transport.requests) == before


def test_integrity_failure_remains_recorded_after_startup_overwrites_lock(tmp_path):
    path = tmp_path / "state.db"
    signer, transport = make_signer(path)
    transport.bad_mac = True
    with pytest.raises(HardwareLocked, match="invalid response MAC"):
        signer.status()
    signer.close()
    transport.bad_mac = False
    with pytest.raises(HardwareLocked, match="unfinished hardware transaction"):
        make_signer(path, transport)
    with sqlite3.connect(path) as con:
        assert (
            con.execute("SELECT error FROM pg_hw_journal WHERE state='pending'").fetchone()[0]
            == "HardwareLocked: invalid response MAC"
        )


@pytest.mark.parametrize("op", [Op.PAY, Op.COMMIT_INVOICE, Op.APPROVE_EXCEPTION])
def test_uncertain_mutation_is_never_recovered_as_status(tmp_path, op):
    path = tmp_path / "state.db"
    signer, transport = make_signer(path)
    old_pending_read(signer, frame=command(op, session=signer.session, currency=978))
    signer.close()
    before = len(transport.requests)
    with pytest.raises(HardwareLocked, match="unfinished hardware transaction"):
        make_signer(path, transport)
    assert len(transport.requests) == before


@pytest.mark.parametrize("reason", ["invalid response MAC", "unexpected exception"])
def test_status_integrity_failure_is_not_recoverable(tmp_path, reason):
    path = tmp_path / "state.db"
    signer, transport = make_signer(path)
    old_pending_read(signer, reason=reason)
    signer.close()
    before = len(transport.requests)
    with pytest.raises(HardwareLocked, match="unfinished hardware transaction"):
        make_signer(path, transport)
    assert len(transport.requests) == before


@pytest.mark.parametrize("field", ["spent", "auth_counter"])
def test_recovery_rejects_authenticated_unexplained_counter_change(tmp_path, field):
    path = tmp_path / "state.db"
    signer, transport = make_signer(path)
    old_pending_read(signer)
    signer.close()
    setattr(transport.model, field, 1)
    with pytest.raises(HardwareLocked, match="changed spending or authorization count"):
        make_signer(path, transport)


def test_failed_legacy_reconciliation_keeps_old_durable_lock(tmp_path):
    path = tmp_path / "state.db"
    signer, transport = make_signer(path)
    old_pending_read(signer)
    signer.close()
    transport.lose_response = True
    with pytest.raises(HardwareUnavailable):
        make_signer(path, transport)
    # A later successful connection can still reconcile the original read, and
    # the unsuccessful attempt did not silently discard its pending journal.
    with sqlite3.connect(path) as con:
        state = json.loads(con.execute("SELECT data FROM pg_hw_state").fetchone()[0])
        assert state["locked"] and state["lock_reason"] == OLD_LOCK_PREFIX + SERIAL_TIMEOUT
        assert (
            con.execute("SELECT count(*) FROM pg_hw_journal WHERE state='pending'").fetchone()[0]
            == 1
        )
    transport.lose_response = False
    reopened, _ = make_signer(path, transport)
    assert not reopened._state["locked"]


def test_bridge_sequence_change_blocks_commands_after_lost_read(tmp_path):
    signer, transport = make_signer(tmp_path / "state.db")
    transport.lose_response = True
    with pytest.raises(HardwareUnavailable):
        signer.status()
    transport.lose_response = False
    transport.model.controller_seq += 1
    before = len(transport.requests)
    with pytest.raises(HardwareLocked, match="different bridge session or sequence"):
        signer.provision_vendor("V-0001", ACCOUNT, 10_000)
    assert len(transport.requests) == before


def test_resync_timeout_remains_unavailable_without_locking(tmp_path):
    signer, transport = make_signer(tmp_path / "state.db")
    transport.lose_response = True
    with pytest.raises(HardwareUnavailable):
        signer.status()

    def unavailable():
        raise TransportError(SERIAL_TIMEOUT)

    transport.info = unavailable
    with pytest.raises(HardwareUnavailable, match="UART read recovery is incomplete"):
        signer.status()
    assert signer._status_read_failed and not signer._state["locked"]


def test_uart_barrier_consumes_delayed_status_before_hello():
    master, slave = pty.openpty()
    serial = SerialTransport(os.ttyname(slave), timeout=1)
    model_transport = ExplicitModelTransport()
    info = model_transport.info()
    status = command(Op.STATUS).pack()
    delayed_response = envelope(*model_transport.exchange(status, bytes(32)))
    received = []
    failures = []

    def bridge():
        try:
            data = b""
            while data.count(b"\n") < 2:
                data += os.read(master, 4096)
            received.append(data)
            os.write(master, delayed_response + json.dumps(info).encode() + b"\n")
        except BaseException as exc:
            failures.append(exc)

    worker = threading.Thread(target=bridge, daemon=True)
    worker.start()
    try:
        assert serial.resynchronize_read_only() == info
        assert serial.buffer == b""
        worker.join(timeout=2)
        assert not worker.is_alive() and not failures
        assert received[0].startswith(b"\n")
        assert json.loads(received[0].strip()) == HELLO_REQUEST
    finally:
        serial.close()
        os.close(slave)
        os.close(master)


def test_uart_resync_has_one_timeout_budget_even_with_repeated_noise():
    master, slave = pty.openpty()
    serial = SerialTransport(os.ttyname(slave), timeout=0.3)
    stop = threading.Event()

    def noisy_bridge():
        try:
            while not stop.wait(0.08):
                os.write(master, b"{}\n")
        except OSError:
            pass  # The endpoint may close immediately after the timeout.

    worker = threading.Thread(target=noisy_bridge, daemon=True)
    worker.start()
    try:
        started = time.monotonic()
        with pytest.raises(TransportError, match="serial response timeout"):
            serial.resynchronize_read_only()
        assert time.monotonic() - started < 0.55
    finally:
        stop.set()
        worker.join(timeout=1)
        serial.close()
        os.close(slave)
        os.close(master)
