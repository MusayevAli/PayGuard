from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import pytest

from core.events import EventStore, make_event
from core.ledger import (
    Evidence,
    JournalEntryDraft,
    JournalLineDraft,
    Ledger,
    LedgerError,
    PeriodError,
    PostRejected,
)
from core.policy import PolicyViolation
from tests.conftest import synthetic_fx

START = datetime(2026, 10, 1, tzinfo=UTC)


@dataclass
class Env:
    store: EventStore
    ledger: Ledger
    ev: str
    db: Path
    beancount: Path


@pytest.fixture
def env(tmp_path: Path) -> Env:
    db = tmp_path / "payguard.db"
    store = EventStore(db)
    ev = store.append(
        make_event(
            "system_error",
            {
                "component": "test",
                "code": "probe",
                "message": "evidence",
                "refs": [],
                "recoverable": True,
            },
            event_time=START,
            source="test",
        )
    ).event.id
    beancount = tmp_path / "ledger" / "payguard.beancount"
    ledger = Ledger(
        db, events=store, fx=synthetic_fx(), now=lambda: START, beancount_path=beancount
    )
    yield Env(store=store, ledger=ledger, ev=ev, db=db, beancount=beancount)
    ledger.close()
    store.close()


def L(account: str, debit: int = 0, credit: int = 0, currency: str = "EUR") -> JournalLineDraft:
    return JournalLineDraft(
        account=account, debit_minor=debit, credit_minor=credit, currency=currency
    )


def draft(
    lines: list[JournalLineDraft],
    *,
    ev: str,
    effective: date = date(2026, 10, 5),
    cause_id: str = "INV-0001",
    description: str = "Invoice INV-0001",
    evidence: list[Evidence] | None = None,
) -> JournalEntryDraft:
    return JournalEntryDraft(
        effective_date=effective,
        description=description,
        cause_type="invoice",
        cause_id=cause_id,
        lines=lines,
        evidence=[Evidence(event_id=ev, role="invoice")] if evidence is None else evidence,
    )


def test_balanced_eur_entry_posts(env: Env) -> None:
    entry = env.ledger.post(
        draft(
            [L("Expenses:Materials:Foam", debit=10000), L("Liabilities:AP", credit=10000)],
            ev=env.ev,
        ),
        "invoice_agent",
    )
    assert entry.id == "JE-000001" and entry.seq == 1 and entry.posted_by == "invoice_agent"
    assert entry.posted_at == START and entry.effective_date == date(2026, 10, 5)
    assert [
        (ln.line_no, ln.account, ln.debit_minor, ln.credit_minor, ln.eur_equiv_minor, ln.fx_rate)
        for ln in entry.lines
    ] == [
        (1, "Expenses:Materials:Foam", 10000, 0, 10000, None),
        (2, "Liabilities:AP", 0, 10000, 10000, None),
    ]
    assert env.ledger.balance("Liabilities:AP", date(2026, 10, 31)) == {"EUR": -10000}
    assert env.ledger.balance("Expenses:Materials:Foam", date(2026, 10, 31)) == {"EUR": 10000}
    assert env.ledger.get_entry("JE-000001") == entry
    assert env.ledger.entries()[0].id == "JE-000001"
    assert env.beancount.is_file() and "^JE-000001" in env.beancount.read_text()


@pytest.mark.parametrize(
    ("line", "reason"),
    [
        (
            JournalLineDraft(
                account="Liabilities:AP", debit_minor=5, credit_minor=5, currency="EUR"
            ),
            "unbalanced_line",
        ),
        (
            JournalLineDraft(
                account="Liabilities:AP", debit_minor=0, credit_minor=0, currency="EUR"
            ),
            "unbalanced_line",
        ),
        (
            JournalLineDraft(
                account="Liabilities:AP", debit_minor=-5, credit_minor=0, currency="EUR"
            ),
            "unbalanced_line",
        ),
        (
            JournalLineDraft(
                account="Assets:VAT:Input", debit_minor=5, credit_minor=0, currency="EUR"
            ),
            "unknown_account",
        ),
    ],
)
def test_rejects_bad_lines(env: Env, line: JournalLineDraft, reason: str) -> None:
    with pytest.raises(PostRejected) as info:
        env.ledger.post(
            draft([line, L("Expenses:Materials:Foam", debit=5)], ev=env.ev), "controller"
        )
    assert info.value.reason_code == reason
    assert env.ledger.entries() == []


def test_rejects_unbalanced_single_currency_entry(env: Env) -> None:
    with pytest.raises(PostRejected) as info:
        env.ledger.post(
            draft(
                [
                    L("Expenses:Materials:Foam", debit=10000, currency="CZK"),
                    L("Liabilities:AP", credit=9000, currency="CZK"),
                ],
                ev=env.ev,
            ),
            "invoice_agent",
        )
    assert info.value.reason_code == "unbalanced_currency"
    assert "CZK" in info.value.detail


def test_multicurrency_entry_balances_in_eur_with_rounding_residual(env: Env) -> None:
    # 8 Oct: 24.345 CZK per EUR. 4,869,000.12 CZK = 200,000.0049 EUR -> 200,000.00; the EUR side is 200,000.01.
    d = draft(
        [
            L("Assets:Bank:CZK", debit=486900012, currency="CZK"),
            L("Assets:Bank:EUR", credit=20000001),
        ],
        ev=env.ev,
        effective=date(2026, 10, 8),
        cause_id="FX-2026-1008",
        description="FX conversion EUR/CZK",
    )
    d = d.model_copy(update={"cause_type": "statement"})
    entry = env.ledger.post(d, "payment_agent")
    assert len(entry.lines) == 3
    czk, eur, rounding = entry.lines
    assert (
        czk.fx_rate == "24.345"
        and czk.rate_date == date(2026, 10, 8)
        and czk.eur_equiv_minor == 20000000
    )
    assert eur.fx_rate is None and eur.eur_equiv_minor == 20000001
    assert rounding.account == "Expenses:FX:Rounding" and rounding.currency == "EUR"
    assert (
        rounding.debit_minor == 1 and rounding.credit_minor == 0 and rounding.eur_equiv_minor == 1
    )
    debits = sum(ln.eur_equiv_minor for ln in entry.lines if ln.debit_minor)
    credits = sum(ln.eur_equiv_minor for ln in entry.lines if ln.credit_minor)
    assert debits == credits
    assert env.ledger.balance("Expenses:FX:Rounding", date(2026, 10, 31)) == {"EUR": 1}
    text = env.beancount.read_text()
    assert "4869000.12 CZK @@ 200000.00 EUR" in text and "Expenses:FX:Rounding" in text


def test_residual_beyond_one_cent_is_rejected(env: Env) -> None:
    d = draft(
        [
            L("Assets:Bank:CZK", debit=486900000, currency="CZK"),
            L("Assets:Bank:EUR", credit=20000005),
        ],
        ev=env.ev,
        effective=date(2026, 10, 8),
    )
    with pytest.raises(PostRejected) as info:
        env.ledger.post(d.model_copy(update={"cause_type": "statement"}), "payment_agent")
    assert info.value.reason_code == "unbalanced_eur"
    assert "5" in info.value.detail


def test_rejects_out_of_scope_principal(env: Env) -> None:
    with pytest.raises(PostRejected) as info:
        env.ledger.post(
            draft([L("Assets:Bank:EUR", debit=100), L("Liabilities:AP", credit=100)], ev=env.ev),
            "invoice_agent",
        )
    assert info.value.reason_code == "out_of_scope"
    assert "invoice_agent" in str(info.value) and "Assets:Bank:EUR" in str(info.value)
    with pytest.raises(PostRejected) as info2:
        env.ledger.post(
            draft(
                [L("Liabilities:AP", debit=100), L("Expenses:Materials:Foam", credit=100)],
                ev=env.ev,
            ),
            "nobody",
        )
    assert info2.value.reason_code == "out_of_scope"


def test_rejects_missing_or_unknown_evidence(env: Env) -> None:
    lines = [L("Expenses:Materials:Foam", debit=100), L("Liabilities:AP", credit=100)]
    with pytest.raises(PostRejected) as info:
        env.ledger.post(draft(lines, ev=env.ev, evidence=[]), "invoice_agent")
    assert info.value.reason_code == "no_evidence"
    with pytest.raises(PostRejected) as info2:
        env.ledger.post(
            draft(
                lines,
                ev=env.ev,
                evidence=[Evidence(event_id="ev_ffffffffffffffff", role="invoice")],
            ),
            "invoice_agent",
        )
    assert info2.value.reason_code == "no_evidence" and "ev_ffffffffffffffff" in info2.value.detail


def test_duplicate_content_hash_is_rejected_without_a_second_entry(env: Env) -> None:
    d = draft([L("Expenses:Materials:Foam", debit=100), L("Liabilities:AP", credit=100)], ev=env.ev)
    first = env.ledger.post(d, "invoice_agent")
    with pytest.raises(PostRejected) as info:
        env.ledger.post(d, "invoice_agent")
    assert info.value.reason_code == "duplicate" and info.value.existing_entry_id == first.id
    assert len(env.ledger.entries()) == 1
    assert env.ledger.balance("Liabilities:AP", date(2026, 10, 31)) == {"EUR": -100}


def test_checks_run_in_the_specified_order(env: Env) -> None:
    # unbalanced AND out of scope AND no evidence: the earliest failing check wins
    with pytest.raises(PostRejected) as info:
        env.ledger.post(
            draft(
                [L("Assets:Bank:EUR", debit=100), L("Liabilities:AP", credit=90)],
                ev=env.ev,
                evidence=[],
            ),
            "invoice_agent",
        )
    assert info.value.reason_code == "unbalanced_currency"
    with pytest.raises(PostRejected) as info2:
        env.ledger.post(
            draft(
                [L("Assets:Bank:EUR", debit=100), L("Liabilities:AP", credit=100)],
                ev=env.ev,
                evidence=[],
            ),
            "invoice_agent",
        )
    assert info2.value.reason_code == "out_of_scope"


def test_reverse_mirrors_and_preserves_the_original(env: Env) -> None:
    original = env.ledger.post(
        draft(
            [
                L("Expenses:Materials:Foam", debit=243450, currency="CZK"),
                L("Liabilities:AP", credit=243450, currency="CZK"),
            ],
            ev=env.ev,
            effective=date(2026, 10, 1),
        ),
        "invoice_agent",
    )
    before = env.ledger.get_entry(original.id)
    reversal = env.ledger.reverse(
        original.id,
        "invoice_agent",
        cause_type="credit_note",
        cause_id="CN-12",
        effective_date=date(2026, 10, 20),
    )
    assert reversal.reverses == original.id and reversal.id == "JE-000002"
    assert (
        reversal.effective_date == date(2026, 10, 20)
        and reversal.cause_type == "credit_note"
        and reversal.cause_id == "CN-12"
    )
    assert [
        (
            ln.account,
            ln.debit_minor,
            ln.credit_minor,
            ln.currency,
            ln.eur_equiv_minor,
            ln.fx_rate,
            ln.rate_date,
        )
        for ln in reversal.lines
    ] == [
        (
            ln.account,
            ln.credit_minor,
            ln.debit_minor,
            ln.currency,
            ln.eur_equiv_minor,
            ln.fx_rate,
            ln.rate_date,
        )
        for ln in original.lines
    ]
    assert reversal.evidence == original.evidence
    assert env.ledger.get_entry(original.id) == before
    assert env.ledger.balance("Liabilities:AP", date(2026, 10, 31)) == {"CZK": 0}
    with pytest.raises(PostRejected) as info:
        env.ledger.reverse(
            original.id,
            "invoice_agent",
            cause_type="credit_note",
            cause_id="CN-12",
            effective_date=date(2026, 10, 20),
        )
    assert info.value.reason_code == "duplicate"
    with pytest.raises(LedgerError, match="JE-999999"):
        env.ledger.reverse(
            "JE-999999",
            "controller",
            cause_type="x",
            cause_id="y",
            effective_date=date(2026, 10, 20),
        )
    with pytest.raises(PostRejected) as info2:
        env.ledger.reverse(
            original.id,
            "vendor_agent",
            cause_type="credit_note",
            cause_id="CN-13",
            effective_date=date(2026, 10, 21),
        )
    assert info2.value.reason_code == "out_of_scope"


def test_triggers_block_update_and_delete(env: Env) -> None:
    env.ledger.post(
        draft(
            [L("Expenses:Materials:Foam", debit=100), L("Liabilities:AP", credit=100)], ev=env.ev
        ),
        "invoice_agent",
    )
    con = sqlite3.connect(env.db)
    try:
        for sql in (
            "UPDATE journal_entries SET description='x' WHERE id='JE-000001'",
            "DELETE FROM journal_entries WHERE id='JE-000001'",
            "UPDATE journal_lines SET debit_minor=1 WHERE entry_id='JE-000001'",
            "DELETE FROM journal_lines",
            "DELETE FROM entry_evidence",
        ):
            with pytest.raises(sqlite3.DatabaseError, match="append-only"):
                con.execute(sql)
        cols = {r[1] for r in con.execute("PRAGMA table_info(journal_entries)")}
        assert cols == {
            "id",
            "seq",
            "posted_at",
            "effective_date",
            "description",
            "posted_by",
            "cause_type",
            "cause_id",
            "reverses",
            "content_hash",
        }
        cols = {r[1] for r in con.execute("PRAGMA table_info(journal_lines)")}
        assert cols == {
            "entry_id",
            "line_no",
            "account",
            "debit_minor",
            "credit_minor",
            "currency",
            "fx_rate",
            "rate_date",
            "eur_equiv_minor",
        }
        assert {r[1] for r in con.execute("PRAGMA table_info(entry_evidence)")} == {
            "entry_id",
            "event_id",
            "role",
        }
        assert {r[1] for r in con.execute("PRAGMA table_info(periods)")} == {
            "month",
            "status",
            "closed_at",
            "closed_by",
        }
    finally:
        con.close()


def test_balance_as_of_known_at_and_effective_date(env: Env) -> None:
    lines = [L("Expenses:Materials:Foam", debit=100), L("Liabilities:AP", credit=100)]
    env.ledger.post(
        draft(lines, ev=env.ev, effective=date(2026, 10, 1), cause_id="A"),
        "invoice_agent",
        posted_at=START,
    )
    env.ledger.post(
        draft(lines, ev=env.ev, effective=date(2026, 10, 1), cause_id="B"),
        "invoice_agent",
        posted_at=START + timedelta(days=4),
    )
    env.ledger.post(
        draft(lines, ev=env.ev, effective=date(2026, 11, 2), cause_id="C"),
        "invoice_agent",
        posted_at=START + timedelta(days=1),
    )
    assert env.ledger.balance("Liabilities:AP", date(2026, 10, 31)) == {"EUR": -200}
    assert env.ledger.balance(
        "Liabilities:AP", date(2026, 10, 31), as_of_known_at=START + timedelta(days=2)
    ) == {"EUR": -100}
    assert env.ledger.balance("Liabilities:AP", date(2026, 12, 31)) == {"EUR": -300}
    assert env.ledger.balance("Liabilities:AP", date(2026, 9, 30)) == {}
    assert env.ledger.balances(date(2026, 12, 31))["Expenses:Materials:Foam"] == {"EUR": 300}


def test_close_period_blocks_posts_and_reopen_needs_a_controller_decision(env: Env) -> None:
    lines = [L("Expenses:Materials:Foam", debit=100), L("Liabilities:AP", credit=100)]
    entry = env.ledger.post(draft(lines, ev=env.ev, effective=date(2026, 10, 3)), "invoice_agent")
    env.ledger.create_payable(
        vendor_id="V-0001",
        inv_id="INV-0001",
        entry_id=entry.id,
        amount_minor=100,
        currency="EUR",
        due_date=date(2026, 11, 2),
    )
    with pytest.raises(PolicyViolation):
        env.ledger.close_period("2026-10", principal="invoice_agent")
    env.ledger.close_period("2026-10", principal="controller")
    assert env.ledger.period_status("2026-10") == "closed"
    with pytest.raises(PostRejected) as info:
        env.ledger.post(
            draft(lines, ev=env.ev, effective=date(2026, 10, 15), cause_id="late"), "invoice_agent"
        )
    assert info.value.reason_code == "period_closed"
    env.ledger.post(
        draft(lines, ev=env.ev, effective=date(2026, 11, 1), cause_id="nov"), "invoice_agent"
    )  # next month is open
    with pytest.raises(PeriodError):
        env.ledger.close_period("2026-10", principal="controller")  # already closed
    bogus = env.store.append(
        make_event(
            "agent_action",
            {
                "agent": "x",
                "action_type": "held",
                "refs": [],
                "rule_id": None,
                "inputs": {},
                "decision": {},
                "explanation": "",
            },
            event_time=START,
            source="test",
        )
    ).event
    with pytest.raises(PeriodError):
        env.ledger.reopen_period("2026-10", bogus)
    decision = env.store.append(
        make_event(
            "controller_decision",
            {
                "packet_id": "PK-reopen-2026-10",
                "decision": "reopen_period",
                "note": "2026-10 reopened for a late invoice",
                "decided_by": "marta.nowak",
                "via": "dashboard",
            },
            event_time=START + timedelta(days=40),
            source="controller",
        )
    ).event
    env.ledger.reopen_period("2026-10", decision)
    assert env.ledger.period_status("2026-10") == "open"
    env.ledger.post(
        draft(lines, ev=env.ev, effective=date(2026, 10, 15), cause_id="late"), "invoice_agent"
    )
    log = env.ledger.period_log("2026-10")
    assert [row.action for row in log] == ["closed", "reopened"]
    assert log[1].event_id == decision.id


def test_close_refuses_when_ap_control_does_not_reconcile(env: Env) -> None:
    lines = [L("Expenses:Materials:Foam", debit=100), L("Liabilities:AP", credit=100)]
    entry = env.ledger.post(draft(lines, ev=env.ev, effective=date(2026, 10, 3)), "invoice_agent")
    assert env.ledger.ap_control_reconciles().ok is False  # entry posted, no payable yet
    with pytest.raises(PeriodError, match="ap_control_mismatch"):
        env.ledger.close_period("2026-10", principal="controller")
    env.ledger.create_payable(
        vendor_id="V-0001",
        inv_id="INV-0001",
        entry_id=entry.id,
        amount_minor=100,
        currency="EUR",
        due_date=date(2026, 11, 2),
    )
    rec = env.ledger.ap_control_reconciles()
    assert (
        rec.ok
        and rec.per_currency["EUR"].payables_minor == 100
        and rec.per_currency["EUR"].ap_balance_minor == 100
    )
    env.ledger.close_period("2026-10", principal="controller")


def test_payables_and_retention_reconcile(env: Env) -> None:
    entry = env.ledger.post(
        draft(
            [
                L("Expenses:Tooling", debit=10000),
                L("Liabilities:AP", credit=9000),
                L("Liabilities:AP:Retention", credit=1000),
            ],
            ev=env.ev,
            cause_id="TOOL-1",
        ),
        "invoice_agent",
    )
    p = env.ledger.create_payable(
        vendor_id="V-0016",
        inv_id="TOOL-1",
        entry_id=entry.id,
        amount_minor=9000,
        currency="EUR",
        due_date=date(2026, 11, 20),
        retention_minor=1000,
    )
    assert p.id.startswith("AP-") and p.status == "open" and p.retention_minor == 1000
    rec = env.ledger.ap_control_reconciles()
    assert (
        rec.ok
        and rec.per_currency["EUR"].retention_payables_minor
        == 1000
        == rec.per_currency["EUR"].retention_balance_minor
    )
    assert [x.id for x in env.ledger.ap_open_items()] == [p.id]
    assert env.ledger.ap_open_items(vendor_id="V-0001") == []
    extra = env.ledger.create_payable(
        vendor_id="V-0001",
        inv_id="INV-0009",
        entry_id=entry.id,
        amount_minor=5,
        currency="EUR",
        due_date=date(2026, 11, 1),
    )
    assert env.ledger.ap_control_reconciles().ok is False
    env.ledger.set_payable_status(extra.id, "rejected", held_reason=None)
    assert env.ledger.ap_control_reconciles().ok is True
    assert env.ledger.get_payable(extra.id).status == "rejected"
    held = env.ledger.set_payable_status(p.id, "held", held_reason="bank change pending")
    assert (
        held.held_reason == "bank change pending" and env.ledger.ap_control_reconciles().ok
    )  # held items stay in open AP


def test_beancount_export_is_atomic_and_rewritten_after_each_post(env: Env) -> None:
    lines = [L("Expenses:Materials:Foam", debit=100), L("Liabilities:AP", credit=100)]
    env.ledger.post(draft(lines, ev=env.ev, cause_id="one"), "invoice_agent")
    first = env.beancount.read_text()
    env.ledger.post(draft(lines, ev=env.ev, cause_id="two"), "invoice_agent")
    second = env.beancount.read_text()
    assert "^JE-000001" in first and "^JE-000002" not in first
    assert "^JE-000001" in second and "^JE-000002" in second
    assert not list(env.beancount.parent.glob("*.tmp"))
    assert (
        'option "operating_currency" "EUR"' in second
        and 'option "operating_currency" "CZK"' in second
        and 'option "operating_currency" "PLN"' in second
    )
    assert "open Assets:Bank:EUR" in second and "open Equity:Opening" in second
