import json
import sqlite3

import pytest

# isort: off
from scripts.reconcile_status import GENERIC_LOCK, reconcile
from signer.hardware import HardwareLocked
from tests.unit.test_hardware_signer import KEYS, make_signer
from tests.unit.test_hardware_status_recovery import old_pending_read
from siliconsign.protocol import Op, command
# isort: on


def prepare(path, op=Op.STATUS):
    signer, transport = make_signer(path)
    old_pending_read(signer, frame=command(op, session=signer.session, currency=978))
    signer._state["lock_reason"] = GENERIC_LOCK
    signer._save_state()
    ident = signer._con.execute("SELECT max(id) FROM pg_hw_journal").fetchone()[0]
    signer.close()
    return transport, ident


def snapshot(path):
    with sqlite3.connect(path) as con:
        return con.execute("SELECT * FROM pg_hw_state").fetchall(), con.execute(
            "SELECT * FROM pg_hw_journal"
        ).fetchall()


def test_manual_status_reconciliation_preserves_authority_and_backup(tmp_path):
    path = tmp_path / "state.db"
    transport, ident = prepare(path)
    before = snapshot(path)
    count = len(transport.requests)
    backup = reconcile(path, ident, transport, KEYS)
    assert snapshot(backup) == before
    after = snapshot(path)
    state = json.loads(after[0][0][1])
    assert not state["locked"]
    assert state["spent"] == state["auth_counter"] == 0
    assert [f.op for f, _ in transport.requests[count:]] == [Op.STATUS]
    assert after[1][-2][3] == "read_reconciled"
    assert "original failure unknown" in after[1][-2][-1]
    assert after[1][-1][3] == "complete"


@pytest.mark.parametrize("failure", ["pay", "mac", "spent", "sequence", "session", "id"])
def test_recovery_failure_preserves_original_lock_and_journal(tmp_path, failure):
    path = tmp_path / "state.db"
    transport, ident = prepare(path, Op.PAY if failure == "pay" else Op.STATUS)
    before = snapshot(path)
    if failure == "mac":
        transport.bad_mac = True
    elif failure == "spent":
        transport.model.spent += 1
    elif failure == "sequence":
        transport.model.pay_seq += 1
    elif failure == "session":
        transport.model.session = bytes([7]) * 16
    elif failure == "id":
        ident += 1
    count = len(transport.requests)
    with pytest.raises(HardwareLocked):
        reconcile(path, ident, transport, KEYS)
    assert snapshot(path) == before
    assert all(f.op == Op.STATUS for f, _ in transport.requests[count:])
