"""Independent bank verification of FPGA receipts and durable, atomic consumption.

These fixtures synthesize authenticated protocol bytes with a TEST response key.
They validate bank security properties, not the physical board or synthesized RTL.
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from datetime import UTC, datetime
from pathlib import Path

import pytest

from bank.mock_bank import MockBank
from core.events import EventStore, make_event
from core.money import Money, convert
from core.vendors import account_hash
from signer.authority import PROOF_SCHEMA, canonical_binding, evidence_digest, invoice_digest
from signer.frames import PayFrame, encode, hmac_tag

from siliconsign.protocol import Frame, Op, RESPONSE_MAGIC, authenticate, command  # isort: skip
from tests.agentkit import full_synthetic_fx

KEY = bytes.fromhex("a5" * 32)
LEGACY_KEY = bytes.fromhex("b5" * 32)
SESSION = bytes.fromhex("c5" * 16)
NOW = datetime(2026, 10, 6, tzinfo=UTC)
ACCOUNT = account_hash("DE89370400440532013000")
OPENING = {"EUR": 1_000_000, "CZK": 1_000_000, "PLN": 1_000_000}


class HardwareBank:
    def __init__(self, path: Path):
        self.path = path
        self.store = EventStore(path / "bank.db")
        self.bank = self.open_bank()
        self.bank._con.executescript(
            "CREATE TABLE pg_hw_vendors (vendor_id TEXT PRIMARY KEY, slot INTEGER, account_hash TEXT, version INTEGER);"
        )
        self.bank._con.execute(
            "INSERT INTO pg_hw_vendors VALUES (?,?,?,?)", ("V-0001", 0, ACCOUNT, 1)
        )
        # Explicit posted-obligation fixture. Full ledger posting/immutability is
        # exercised separately by the Kit and end-to-end HTTP tests below.
        self.bank._con.executescript(
            "CREATE TABLE payables (id TEXT PRIMARY KEY,vendor_id TEXT,inv_id TEXT,entry_id TEXT,amount_minor INTEGER,currency TEXT,status TEXT,held_reason TEXT);"
            "CREATE TABLE journal_entries (id TEXT PRIMARY KEY,cause_id TEXT);"
            "CREATE TABLE journal_lines (entry_id TEXT,account TEXT,debit_minor INTEGER,credit_minor INTEGER,currency TEXT);"
        )

    def obligation(self, payable_id, amount, currency="EUR", vendor="V-0001"):
        if self.bank._con.execute("SELECT 1 FROM payables WHERE id=?", (payable_id,)).fetchone():
            return
        entry_id, invoice_id = f"JE-{payable_id}", f"INV-{payable_id}"
        self.bank._con.execute("INSERT INTO journal_entries VALUES (?,?)", (entry_id, invoice_id))
        self.bank._con.execute(
            "INSERT INTO journal_lines VALUES (?,?,?,?,?)",
            (entry_id, "Liabilities:AP", max(0, -amount), max(0, amount), currency),
        )
        self.bank._con.execute(
            "INSERT INTO journal_lines VALUES (?,?,?,?,?)",
            (entry_id, "Expenses:Materials", max(0, amount), max(0, -amount), currency),
        )
        self.bank._con.execute(
            "INSERT INTO payables VALUES (?,?,?,?,?,?,?,NULL)",
            (payable_id, vendor, invoice_id, entry_id, amount, currency, "open"),
        )

    def open_bank(self) -> MockBank:
        return MockBank(
            self.path / "bank.db",
            store=self.store,
            key=LEGACY_KEY,
            fx=full_synthetic_fx(),
            now=lambda: NOW,
            schedule=lambda event: None,
            out_dir=self.path,
            opening=OPENING,
            overdraft={},
            hardware_response_key=KEY,
            hardware_session=SESSION,
        )

    def request(
        self,
        *,
        pay_id="PAY-000001",
        amount=10800,
        currency="USD",
        seq=1,
        covered=None,
        credits=None,
        discount=0,
    ):
        fx = full_synthetic_fx()
        conversion = convert(Money(amount, currency), "EUR", NOW.date(), fx)
        eur = conversion.money.minor
        covered = covered or {"AP-000001": amount}
        credits = credits or {}
        for payable_id, liability in {**covered, **credits}.items():
            self.obligation(payable_id, liability, currency)
        frame = encode(
            PayFrame(
                seq=seq,
                pay_id=pay_id,
                account_hash=bytes.fromhex(ACCOUNT),
                amount_minor=eur,
                currency=currency,
            )
        )
        payload = {
            "pay_id": pay_id,
            "vendor_id": "V-0001",
            "account_hash": ACCOUNT,
            "account_masked": "DE89 **** 3000",
            "amount_minor": amount,
            "currency": currency,
            "eur_equiv_minor": eur,
            "payable_ids": [*covered, *credits],
            "covered": covered,
            "credits": credits,
            "discount_minor": discount,
            "settlement_currency": currency if currency in OPENING else "EUR",
            "settlement_minor": amount if currency in OPENING else eur,
            "fx_rate": None if currency == "EUR" else str(conversion.rate),
            "rate_date": None if currency == "EUR" else conversion.rate_date.isoformat(),
            "requested_by": "payment_agent",
            "frame_hex": frame.hex(),
        }
        self.record(payload)
        return frame, payload

    def record(self, payload):
        self.store.append(
            make_event(
                "payment_requested", payload, event_time=NOW, source="agent", actor="payment_agent"
            )
        )


@pytest.fixture
def env(tmp_path):
    kit = HardwareBank(tmp_path)
    yield kit
    kit.bank.close()
    kit.store.close()


def proof(
    frame: bytes,
    payload: dict,
    *,
    counter=1,
    session=SESSION,
    status=0,
    mutate_command=None,
    mutate_response=None,
):
    binding = canonical_binding(payload)
    cmd = command(
        Op.PAY,
        seq=17,
        vendor=0,
        invoice=0,
        currency=978,
        session=session,
        account=bytes.fromhex(binding["account_hash"]),
        vendor_version=1,
        amount=binding["eur_equiv_minor"],
        invoice_hash=invoice_digest(binding),
        evidence=evidence_digest(frame, binding),
    )
    if mutate_command:
        cmd = mutate_command(cmd)
    words = list(cmd.words)
    words[0], words[56], words[57], words[63] = RESPONSE_MAGIC, status, counter, 1
    response = Frame(tuple(words))
    if mutate_response:
        response = mutate_response(response)
    return {
        "schema": PROOF_SCHEMA,
        "command_hex": cmd.pack().hex(),
        "response_hex": response.pack().hex(),
        "response_mac_hex": authenticate(KEY, response).hex(),
        "legacy_frame_hex": frame.hex(),
        "binding": binding,
    }


def test_hardware_bank_settles_native_currency_and_retains_full_receipt(env):
    frame, request = env.request()
    auth = proof(frame, request)
    result = env.bank.execute(frame, authorization=auth)
    assert result.ok and result.currency == "USD" and result.amount_minor == 10800
    assert result.settlement_currency == "EUR" and result.settlement_amount_minor == 10000
    assert env.bank.balance("EUR") == OPENING["EUR"] - 10000
    stored = env.bank._con.execute("SELECT detail FROM bank_settlements").fetchone()[0]
    assert auth["response_mac_hex"] in stored
    assert env.bank._con.execute("SELECT COUNT(*) FROM bank_hardware_receipts").fetchone()[0] == 1


def test_legacy_hmac_cannot_bypass_hardware_mode(env):
    frame, _ = env.request()
    result = env.bank.execute(frame, hmac_tag(LEGACY_KEY, frame))
    assert not result.ok and result.reason == "missing_hardware_authorization"
    assert env.bank.balance("EUR") == OPENING["EUR"]


@pytest.mark.parametrize(
    "mutation",
    [
        lambda a: a.update(response_mac_hex="00" * 32),
        lambda a: a.update(response_hex="00"),
        lambda a: a.update(command_hex="zz"),
        lambda a: a.update(legacy_frame_hex="00" * 64),
        lambda a: a.update(binding={**a["binding"], "amount_minor": 10801}),
        lambda a: a.update(binding={**a["binding"], "payable_ids": ["OTHER"]}),
        lambda a: a.update(schema="untrusted"),
        lambda a: a.update(response_hex=None),
        lambda a: a.update(extra=True),
    ],
)
def test_malformed_or_modified_proof_refuses_without_money_movement(env, mutation):
    frame, request = env.request()
    auth = proof(frame, request)
    mutation(auth)
    result = env.bank.execute(frame, authorization=auth)
    assert not result.ok and result.reason == "bad_hardware_authorization"
    assert env.bank.balance("EUR") == OPENING["EUR"]


@pytest.mark.parametrize(
    "options",
    [
        {"session": bytes.fromhex("11" * 16)},
        {"status": 9},
        {"counter": 0},
        {"mutate_command": lambda f: f.replace(op=Op.STATUS)},
        {"mutate_command": lambda f: f.replace(amount=10001)},
        {"mutate_command": lambda f: f.replace(currency=840)},
        {"mutate_command": lambda f: f.replace(vendor=1)},
        {"mutate_command": lambda f: f.replace(vendor_version=2)},
        {"mutate_command": lambda f: f.replace(account=bytes.fromhex("11" * 32))},
        {"mutate_command": lambda f: f.replace(invoice_hash=bytes.fromhex("11" * 32))},
        {"mutate_command": lambda f: f.replace(evidence=bytes.fromhex("11" * 32))},
        {"mutate_response": lambda f: f.replace(vendor=1)},
    ],
)
def test_authenticated_but_wrong_hardware_outcomes_cannot_settle(env, options):
    frame, request = env.request()
    result = env.bank.execute(frame, authorization=proof(frame, request, **options))
    assert not result.ok and result.reason == "bad_hardware_authorization"
    assert env.bank.balance("EUR") == OPENING["EUR"]


def test_request_cannot_change_native_amount_within_same_rounded_eur_cent(env):
    frame, request = env.request(amount=101, currency="CZK")
    changed = deepcopy(request)
    changed["amount_minor"] = 102
    changed["covered"]["AP-000001"] = 102
    changed["settlement_minor"] = 102
    # At the recorded CZK rate these native amounts round to the same EUR-cent frame.
    env.record(changed)
    result = env.bank.execute(frame, authorization=proof(frame, changed))
    assert not result.ok and result.reason == "bad_hardware_authorization"
    assert env.bank.balance("CZK") == OPENING["CZK"]


def test_bank_recomputes_actual_settlement_independently(env):
    frame, request = env.request()
    changed = deepcopy(request)
    changed["settlement_minor"] += 1
    env.record(changed)
    result = env.bank.execute(frame, authorization=proof(frame, changed))
    assert not result.ok
    assert env.bank.balance("EUR") == OPENING["EUR"]


def test_one_authorization_covers_explicit_batch_with_credit_and_discount(env):
    frame, request = env.request(
        amount=10800, covered={"AP-1": 8000, "AP-2": 4000}, credits={"CN-1": -1000}, discount=200
    )
    assert env.bank.execute(frame, authorization=proof(frame, request)).ok
    assert env.bank._con.execute("SELECT COUNT(*) FROM bank_settlements").fetchone()[0] == 1


def test_identical_receipt_is_idempotent_after_restart_and_concurrent_replay(env):
    frame, request = env.request()
    auth = proof(frame, request)
    first = env.bank.execute(frame, authorization=auth)
    env.bank.close()
    env.bank = env.open_bank()
    second = env.open_bank()
    try:
        with ThreadPoolExecutor(max_workers=8) as pool:
            replies = list(
                pool.map(
                    lambda i: (env.bank if i % 2 else second).execute(frame, authorization=auth),
                    range(16),
                )
            )
        assert all(r.ok and r.bank_ref == first.bank_ref for r in replies)
        assert env.bank.balance("EUR") == OPENING["EUR"] - 10000
        assert env.bank._con.execute("SELECT COUNT(*) FROM bank_settlements").fetchone()[0] == 1
    finally:
        second.close()


def test_counter_cannot_authorize_a_second_batch(env):
    frame, request = env.request()
    assert env.bank.execute(frame, authorization=proof(frame, request)).ok
    frame2, request2 = env.request(pay_id="PAY-000002", seq=2)
    refused = env.bank.execute(frame2, authorization=proof(frame2, request2, counter=1))
    assert not refused.ok and refused.reason == "hardware_receipt_replayed"
    assert env.bank.balance("EUR") == OPENING["EUR"] - 10000


def test_failed_funds_check_does_not_consume_authorization(env):
    frame, request = env.request(amount=OPENING["EUR"] + 1, currency="EUR")
    result = env.bank.execute(frame, authorization=proof(frame, request))
    assert not result.ok and result.reason == "insufficient_funds"
    assert env.bank._con.execute("SELECT COUNT(*) FROM bank_hardware_receipts").fetchone()[0] == 0
    assert env.bank._con.execute("SELECT COUNT(*) FROM bank_settlements").fetchone()[0] == 0


class ReceiptSigner:
    """Protocol test double: synthesized receipts are never presented as board tests."""

    kind = "hw"

    def __init__(self, stub, *, missing_proof=False):
        self.stub = stub
        self.binding = None
        self.missing_proof = missing_proof
        self.requests = []

    def status(self):
        return self.stub.status()

    def tick_day(self, day):
        # Hardware real-time rollover behavior is tested in the signer test suite.
        pass

    def prepare_payment(self, frame, binding):
        self.binding = binding
        self.requests.append((frame, binding))

    def sign(self, frame):
        from signer.interface import SignResult

        auth = None if self.missing_proof else proof(frame, self.binding)
        return SignResult(ok=True, raw=b"", authorization=auth)

    def approval_ready(self, pay_id):
        return False


def install_test_hardware(kit, *, missing_proof=False):
    acct = kit.vendors.active_account("V-0002")
    kit.bank._con.executescript(
        "CREATE TABLE pg_hw_vendors (vendor_id TEXT PRIMARY KEY, slot INTEGER, account_hash TEXT, version INTEGER);"
    )
    kit.bank._con.execute(
        "INSERT INTO pg_hw_vendors VALUES (?,?,?,?)", ("V-0002", 0, acct.account_hash, 1)
    )
    kit.bank._hardware_response_key = KEY
    kit.bank._hardware_session = SESSION
    kit.bank._require_hardware_authorization = True
    signer = ReceiptSigner(kit.signer, missing_proof=missing_proof)
    kit.payment_agent.signer = signer
    return signer


def payment_context(kit):
    from agents.base import Context

    return Context(
        principal="payment_agent",
        policy=kit.policy,
        store=kit.store,
        now=lambda: kit.now,
        explainer=kit.llm,
        messenger=kit.responders,
    )


def test_payment_agent_transmits_complete_hardware_proof_without_legacy_tag(tmp_path):
    from agents.payment_agent import Group
    from tests.agentkit import Kit

    kit = Kit(tmp_path, payments=True)
    try:
        kit.provision("V-0002")
        payable = kit.payable("V-0002", 10000, due=kit.now.date())
        signer = install_test_hardware(kit)
        result = kit.payment_agent._pay(
            Group("V-0002", "EUR", [payable]), payment_context(kit), today=kit.now.date()
        )
        assert result.status == "executed" and result.tag_hex is None
        assert signer.requests[0][1]["covered"] == {payable.id: 10000}
        chip = kit.store.query(type="chip_response")[-1]
        assert chip.payload["authorization"]["schema"] == PROOF_SCHEMA
        assert kit.ledger.get_payable(payable.id).status == "paid"
        assert kit.ledger.ap_control_reconciles().ok
    finally:
        kit.close()


def test_hardware_ok_without_full_receipt_stays_held_with_evidence(tmp_path):
    from agents.payment_agent import Group
    from tests.agentkit import Kit

    kit = Kit(tmp_path, payments=True)
    try:
        kit.provision("V-0002")
        payable = kit.payable("V-0002", 10000, due=kit.now.date())
        install_test_hardware(kit, missing_proof=True)
        before = kit.bank.balance("EUR")
        result = kit.payment_agent._pay(
            Group("V-0002", "EUR", [payable]), payment_context(kit), today=kit.now.date()
        )
        assert (
            result.status == "held" and result.held_reason == "bank:missing_hardware_authorization"
        )
        assert kit.bank.balance("EUR") == before
        packet = kit.store.query(type="escalation")[-1]
        assert {x["role"] for x in packet.payload["evidence"]} == {
            "payment_request",
            "chip_response",
            "bank_rejection",
        }
    finally:
        kit.close()


def test_interrupted_ledger_post_cannot_create_a_second_hardware_payment(tmp_path, monkeypatch):
    from agents.base import Context
    from agents.payment_agent import Group
    from agents.payment_state import PaymentState
    from tests.agentkit import Kit

    kit = Kit(tmp_path, payments=True)
    try:
        kit.provision("V-0002")
        payable = kit.payable("V-0002", 10000, due=kit.now.date())
        signer = install_test_hardware(kit)
        original = Context.act

        def crash_at_post(self, **kwargs):
            if kwargs["action_type"] == "posted":
                raise RuntimeError("simulated interruption after bank settlement")
            return original(self, **kwargs)

        monkeypatch.setattr(Context, "act", crash_at_post)
        ctx = payment_context(kit)
        with pytest.raises(RuntimeError, match="simulated interruption"):
            kit.payment_agent._pay(Group("V-0002", "EUR", [payable]), ctx, today=kit.now.date())
        monkeypatch.setattr(Context, "act", original)
        assert kit.bank._con.execute("SELECT COUNT(*) FROM bank_settlements").fetchone()[0] == 1
        assert kit.ledger.get_payable(payable.id).status == "scheduled"
        # Reopen the durable payment state, just as a restarted desk would.
        kit.pay_state.close()
        kit.pay_state = PaymentState(kit.db)
        kit.payment_agent.state = kit.pay_state
        kit.payment_agent._run(kit.now.date(), ctx)
        row = kit.pay_state.payment("PAY-000001")
        assert row.status == "held" and row.held_reason == "settlement_recovery_required"
        assert row.packet_id and len(signer.requests) == 1
        assert len(kit.pay_state.payments()) == 1
        assert kit.bank._con.execute("SELECT COUNT(*) FROM bank_settlements").fetchone()[0] == 1
    finally:
        kit.close()


def test_new_payment_id_cannot_pay_the_same_obligation_again(env):
    first, request = env.request(amount=1000, currency="EUR")
    assert env.bank.execute(first, authorization=proof(first, request)).ok
    second, changed_id = env.request(amount=1000, currency="EUR", pay_id="PAY-000002", seq=2)
    result = env.bank.execute(second, authorization=proof(second, changed_id, counter=2))
    assert not result.ok and result.reason == "coverage_already_consumed"
    assert env.bank.balance("EUR") == OPENING["EUR"] - 1000
    assert (
        env.bank._con.execute("SELECT COUNT(*) FROM bank_obligation_consumptions").fetchone()[0]
        == 1
    )


def test_partial_batches_consume_exactly_the_original_remaining_capacity(env):
    env.obligation("AP-000001", 1000)
    first, req1 = env.request(amount=400, currency="EUR")
    assert env.bank.execute(first, authorization=proof(first, req1)).ok
    env.bank._con.execute("UPDATE payables SET amount_minor=600 WHERE id='AP-000001'")
    env.bank.close()
    env.bank = env.open_bank()
    second, req2 = env.request(amount=600, currency="EUR", pay_id="PAY-000002", seq=2)
    assert env.bank.execute(second, authorization=proof(second, req2, counter=2)).ok
    # Raising the mutable ledger amount cannot replenish the bank's capacity.
    env.bank._con.execute("UPDATE payables SET amount_minor=1000 WHERE id='AP-000001'")
    third, req3 = env.request(amount=100, currency="EUR", pay_id="PAY-000003", seq=3)
    result = env.bank.execute(third, authorization=proof(third, req3, counter=3))
    assert not result.ok and result.reason == "coverage_already_consumed"
    row = env.bank._con.execute("SELECT * FROM bank_obligation_limits").fetchone()
    assert row["capacity_minor"] == row["consumed_minor"] == 1000
    assert env.bank.balance("EUR") == OPENING["EUR"] - 1000


def test_credit_note_cannot_be_consumed_by_a_second_batch(env):
    one, req1 = env.request(
        amount=900, currency="EUR", covered={"AP-1": 1000}, credits={"CN-1": -100}
    )
    assert env.bank.execute(one, authorization=proof(one, req1)).ok
    two, req2 = env.request(
        amount=900,
        currency="EUR",
        pay_id="PAY-000002",
        seq=2,
        covered={"AP-2": 1000},
        credits={"CN-1": -100},
    )
    result = env.bank.execute(two, authorization=proof(two, req2, counter=2))
    assert not result.ok and result.reason == "coverage_already_consumed"
    # The invoice examined before the exhausted credit must roll back too.
    assert (
        env.bank._con.execute(
            "SELECT 1 FROM bank_obligation_limits WHERE payable_id='AP-2'"
        ).fetchone()
        is None
    )
    assert env.bank.balance("EUR") == OPENING["EUR"] - 900


@pytest.mark.parametrize(
    "change",
    [
        "DELETE FROM payables",
        "UPDATE payables SET vendor_id='V-OTHER'",
        "UPDATE payables SET currency='USD'",
        "UPDATE payables SET status='paid'",
        "UPDATE payables SET status='netted'",
        "UPDATE payables SET held_reason='retention until acceptance'",
        "UPDATE payables SET amount_minor=1",
        "UPDATE payables SET inv_id='OTHER-INVOICE'",
        "DELETE FROM journal_entries",
        "DELETE FROM journal_lines",
    ],
)
def test_fresh_authorization_must_match_a_real_outstanding_ledger_obligation(env, change):
    frame, req = env.request(amount=1000, currency="EUR")
    env.bank._con.execute(change)
    result = env.bank.execute(frame, authorization=proof(frame, req))
    assert not result.ok
    assert result.reason in (
        "unknown_obligation",
        "ledger_obligation_mismatch",
        "coverage_exceeds_obligation",
    )
    assert env.bank.balance("EUR") == OPENING["EUR"]
    assert (
        env.bank._con.execute("SELECT COUNT(*) FROM bank_obligation_consumptions").fetchone()[0]
        == 0
    )


def test_independent_processes_cannot_consume_the_same_coverage_concurrently(env):
    one, req1 = env.request(amount=1000, currency="EUR")
    two, req2 = env.request(amount=1000, currency="EUR", pay_id="PAY-000002", seq=2)
    peer = env.open_bank()
    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [
                pool.submit(bank.execute, frame, authorization=proof(frame, req, counter=count))
                for bank, frame, req, count in ((env.bank, one, req1, 1), (peer, two, req2, 2))
            ]
            results = [f.result() for f in futures]
        assert sum(r.ok for r in results) == 1
        assert next(r for r in results if not r.ok).reason == "coverage_already_consumed"
        assert env.bank.balance("EUR") == OPENING["EUR"] - 1000
        assert (
            env.bank._con.execute("SELECT COUNT(*) FROM bank_hardware_receipts").fetchone()[0] == 1
        )
    finally:
        peer.close()


def test_identical_settlement_retry_after_ledger_marks_paid_is_not_new_coverage(env):
    frame, req = env.request(amount=1000, currency="EUR")
    auth = proof(frame, req)
    first = env.bank.execute(frame, authorization=auth)
    env.bank._con.execute("UPDATE payables SET status='paid' WHERE id='AP-000001'")
    again = env.bank.execute(frame, authorization=auth)
    assert again.ok and again.bank_ref == first.bank_ref and again.detail == "already settled"
    assert (
        env.bank._con.execute("SELECT COUNT(*) FROM bank_obligation_consumptions").fetchone()[0]
        == 1
    )
    assert env.bank.balance("EUR") == OPENING["EUR"] - 1000
