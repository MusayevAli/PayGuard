"""The world through 5 Nov with the invoice and payment agents, the stub provisioned with the 14
demo accounts (jumper on, then pulled). Spec §17 Part 4 acceptance on real world data."""

from __future__ import annotations

import json
import sqlite3
from datetime import date
from pathlib import Path

import pytest

from agents.payment_state import PaymentState
from agents.runtime import Runtime
from core.bootstrap import LEGACY_INVOICES, OPENING_BANK_MINOR
from core.events import EventStore
from core.ledger import Evidence, JournalEntryDraft, JournalLineDraft
from core.policy import PostAction
from scripts.run_world import RunResult, run_world
from signer.provisioning import DEMO_VENDORS
from tests.truth_llm import TruthLLM

WORLD = Path(__file__).resolve().parents[2] / "sim" / "world"
INJECTED_VENDOR = "V-0023"  # not among the 14 provisioned accounts
INJECTED_INV = "TEST-NOT-ALLOWLISTED"
BUTTON_LOG: list[dict[str, object]] = []


def _inject_payable(rt: Runtime) -> None:
    assert INJECTED_VENDOR not in DEMO_VENDORS
    decision = rt.store.query(type="controller_decision", limit=1)[0]
    draft = JournalEntryDraft(
        effective_date=date(2026, 9, 30),
        description=f"Test invoice {INJECTED_INV}",
        cause_type="invoice",
        cause_id=INJECTED_INV,
        lines=[
            JournalLineDraft(
                account="Expenses:Materials:Harness", debit_minor=123_400, currency="EUR"
            ),
            JournalLineDraft(account="Liabilities:AP", credit_minor=123_400, currency="EUR"),
        ],
        evidence=[Evidence(event_id=decision.id, role="decision")],
    )
    entry = rt.policy.execute(PostAction(draft=draft, posted_at=rt.clock.sim_now), "invoice_agent")
    rt.ledger.create_payable(
        vendor_id=INJECTED_VENDOR,
        inv_id=INJECTED_INV,
        entry_id=entry.id,
        amount_minor=123_400,
        currency="EUR",
        due_date=date(2026, 10, 3),
    )


def _controller(rt: Runtime, day: date) -> None:
    """The controller approves button requests and presses the button; other packets wait."""
    for packet in rt.packets.open_packets():
        if (
            packet.agent != "payment_agent"
            or packet.kind != "approval"
            or "approve" not in [o.id for o in packet.options]
        ):
            continue
        rt.packets.decide_packet(
            packet.id, "approve", None, "controller", via="phone", decided_at=rt.clock.sim_now
        )
        rt.runner.drain()
        nonce = rt.signer.press_button()  # type: ignore[attr-defined]
        agent = rt.payment_agent
        assert agent is not None
        resent = rt.runner.run_task(agent, agent.retry_waiting)
        BUTTON_LOG.append(
            {
                "day": day.isoformat(),
                "packet": packet.id,
                "nonce": nonce,
                "resent": resent,
                "refs": packet.refs,
            }
        )


@pytest.fixture(scope="module")
def result(tmp_path_factory: pytest.TempPathFactory) -> RunResult:
    out = tmp_path_factory.mktemp("payment_run")
    return run_world(
        WORLD,
        out,
        until="2026-11-05",
        llm=TruthLLM(WORLD),
        before_start=_inject_payable,
        each_day=_controller,
    )


def _events(result: RunResult, type_: str) -> list:
    store = EventStore(result.db_path)
    try:
        return store.query(type=type_, limit=1_000_000)
    finally:
        store.close()


def _payables(result: RunResult) -> list[sqlite3.Row]:
    con = sqlite3.connect(result.db_path)
    con.row_factory = sqlite3.Row
    try:
        return con.execute("SELECT * FROM payables").fetchall()
    finally:
        con.close()


def test_legitimate_payments_sign_execute_settle_and_reconcile(result: RunResult) -> None:
    executed = _events(result, "payment_executed")
    assert len(executed) >= 5, "several vendors fall due inside the window"
    chips = {e.payload["pay_id"]: e for e in _events(result, "chip_response") if e.payload["ok"]}
    lines = {e.payload["customer_ref"]: e for e in _events(result, "bank_statement_line")}
    state = PaymentState(result.db_path)
    try:
        for ex in executed:
            pay_id = ex.payload["pay_id"]
            assert pay_id in chips and chips[pay_id].payload["tag_hex"]
            row = state.payment(pay_id)
            assert row is not None and row.status in ("executed", "settled")
            if ex.payload["statement_date"] < "2026-11-04":
                assert pay_id in lines, f"{pay_id} missing from the next-day MT940"
                assert row.status == "settled"
        payables = {r["id"]: r for r in _payables(result)}
        for row in state.payments():
            if row.status in ("executed", "settled"):
                for pid in row.payable_ids:
                    assert payables[pid]["status"] in ("paid", "netted", "open"), (
                        pid,
                        payables[pid]["status"],
                    )
                assert any(payables[pid]["status"] == "paid" for pid in row.payable_ids)
    finally:
        state.close()
    assert all(
        e.payload["kind"] in ("anomaly", "approval", "notice")
        for e in _events(result, "escalation")
    )


def test_the_october_batch_settles_exactly_the_three_legacy_payables(result: RunResult) -> None:
    payables = {r["inv_id"]: r for r in _payables(result)}
    for number, _day, amount in LEGACY_INVOICES:
        assert payables[number]["status"] == "paid" and payables[number]["amount_minor"] == amount
    reconciled = [
        a
        for a in _events(result, "agent_action")
        if a.payload["action_type"] == "reconciled" and "PAY-BATCH-0001" in a.payload["refs"]
    ]
    assert len(reconciled) == 1 and sorted(reconciled[0].payload["decision"]["payables"]) == sorted(
        payables[n]["id"] for n, _d, _a in LEGACY_INVOICES
    )
    legacy_ids = {payables[n]["id"] for n, _d, _a in LEGACY_INVOICES}
    assert not any(
        legacy_ids & set(e.payload["payable_ids"]) for e in _events(result, "payment_requested")
    ), "the legacy backlog is never paid twice"


def test_injected_payment_to_a_non_allowlisted_account_is_held(result: RunResult) -> None:
    requests = [
        e
        for e in _events(result, "payment_requested")
        if INJECTED_INV in e.payload["payable_ids"] or e.payload["vendor_id"] == INJECTED_VENDOR
    ]
    assert requests, "the injected payable fell due and was requested"
    pay_id = requests[0].payload["pay_id"]
    chip = [e for e in _events(result, "chip_response") if e.payload["pay_id"] == pay_id]
    assert (
        len(chip) == 1
        and not chip[0].payload["ok"]
        and chip[0].payload["reasons"] == ["not_allowlisted"]
    )
    held = [
        a
        for a in _events(result, "agent_action")
        if a.payload["action_type"] == "payment_held" and pay_id in a.payload["refs"]
    ]
    assert len(held) == 1 and held[0].payload["decision"]["held_reason"] == "not_allowlisted"
    payable = next(r for r in _payables(result) if r["inv_id"] == INJECTED_INV)
    assert payable["status"] == "held" and payable["held_reason"] == "not_allowlisted"
    assert not any(e.payload["pay_id"] == pay_id for e in _events(result, "payment_executed"))


def test_payment_above_threshold_needs_approval_and_the_button(result: RunResult) -> None:
    assert BUTTON_LOG, "at least one payment above EUR 50,000 fell due"
    approvals = [
        e
        for e in _events(result, "escalation")
        if e.payload["kind"] == "approval" and e.payload["options"] == ["approve", "reject"]
    ]
    assert approvals
    executed = {e.payload["pay_id"] for e in _events(result, "payment_executed")}
    for entry in BUTTON_LOG:
        pay_id = next(r for r in entry["refs"] if str(r).startswith("PAY-"))  # type: ignore[union-attr]
        assert pay_id in executed, (
            f"{pay_id} was approved and the button pressed but never executed"
        )
        chips = [e for e in _events(result, "chip_response") if e.payload["pay_id"] == pay_id]
        assert chips[0].payload["reasons"] == ["needs_button"] and chips[-1].payload["ok"]


def test_t9_packet_has_quantified_options(result: RunResult) -> None:
    t9 = [
        e
        for e in _events(result, "escalation")
        if "RE-2026-0931" in e.payload["refs"] and "skip_discount" in e.payload["options"]
    ]
    assert len(t9) == 1
    pk = t9[0].payload
    assert pk["options"] == ["pay_partial_now", "draw_intercompany", "skip_discount"]
    assert (
        pk["recommendation"] in pk["options"]
        and "EUR" in pk["question"]
        and "net benefit" in pk["context"].lower()
    )
    payable = next(r for r in _payables(result) if r["inv_id"] in pk["refs"])
    assert payable["status"] == "open" and payable["discount_bp"] == 200


def test_ledger_and_bank_invariants(result: RunResult) -> None:
    assert all(result.daily_reconciliation), result.daily_reconciliation
    assert result.report.entries_without_evidence == 0
    assert result.policy_violations == 0 and result.post_rejections_scope == 0
    world_lines = [
        json.loads(line)
        for line in (WORLD / "world.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    con = sqlite3.connect(result.db_path)
    con.row_factory = sqlite3.Row
    try:
        state_rows = {r["currency"]: r for r in con.execute("SELECT * FROM bank_state").fetchall()}
        settlements = con.execute(
            "SELECT currency, dc, amount_minor FROM bank_settlements"
        ).fetchall()
    finally:
        con.close()
    for ccy, opening in OPENING_BANK_MINOR.items():
        last = state_rows[ccy]["last_statement_date"]
        world_delta = sum(
            (
                e["payload"]["amount_minor"]
                if e["payload"]["dc"] == "C"
                else -e["payload"]["amount_minor"]
            )
            for e in world_lines
            if e["type"] == "bank_statement_line"
            and e["payload"]["currency"] == ccy
            and e["payload"]["statement_id"] <= f"{ccy}{date.fromisoformat(last):%y%m%d}"
        )
        runtime_delta = sum(
            (r["amount_minor"] if r["dc"] == "C" else -r["amount_minor"])
            for r in settlements
            if r["currency"] == ccy
        )
        assert result.bank_balances[ccy] == opening + world_delta + runtime_delta, ccy


def test_headline_and_ambient_cases_still_pass(result: RunResult) -> None:
    by_id = {c.id: c for c in result.report.cases}
    for cid in ("T1", "T2", "T3", "T4", "T5", "T6", "T9", "T10"):
        assert by_id[cid].status == "caught", (
            cid,
            [(x.kind, x.detail) for x in by_id[cid].checks if not x.ok],
        )
    ambient = [c for c in result.report.cases if c.tier == "ambient"]
    assert sum(1 for c in ambient if c.status == "caught") >= 43
    assert result.report.headline.fp == 0 and result.report.ambient.fp == 0
