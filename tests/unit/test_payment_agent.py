"""Payment agent (spec §7.5) on the synthetic kit: the run, the chip round trip, R10, T9, netting,
reconciliation, fees, FX, notices, retention and the daily cap."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import pytest

from agents.payment_agent import PaymentAgent
from core.bootstrap import OPENING_BANK_MINOR
from tests.agentkit import Kit, at

START = datetime(2026, 10, 1, tzinfo=UTC)
V_EUR = "V-0002"  # Foamtech GmbH, DE, EUR
V_CZK = "V-0001"  # Kovak Trim, CZ, but invoices in EUR per the world; the kit lets us choose
V_UNPROVISIONED = "V-0003"


@pytest.fixture
def kit(tmp_path: Path):
    k = Kit(tmp_path, payments=True)
    k.provision(V_EUR, V_CZK)
    yield k
    k.close()


def events(kit: Kit, type_: str) -> list:
    return kit.store.query(type=type_, limit=100000)


def test_due_payable_is_signed_executed_posted_and_reconciled(kit: Kit) -> None:
    p = kit.payable(V_EUR, 100_000, due=date(2026, 10, 2), number="FT-500")
    kit.tick(days=1)  # 2 Oct boundary: due within two days
    req = events(kit, "payment_requested")
    assert (
        len(req) == 1
        and req[0].payload["eur_equiv_minor"] == 100_000
        and req[0].payload["frame_hex"]
        and req[0].payload["payable_ids"] == [p.id]
    )
    chip = events(kit, "chip_response")
    assert (
        len(chip) == 1
        and chip[0].payload["ok"]
        and chip[0].payload["tag_hex"]
        and chip[0].payload["signer"] == "stub"
    )
    executed = events(kit, "payment_executed")
    assert (
        len(executed) == 1
        and executed[0].payload["pay_id"] == "PAY-000001"
        and executed[0].payload["amount_minor"] == 100_000
    )
    entry = next(e for e in kit.ledger.entries() if e.cause_type == "payment")
    assert (
        kit.line(entry, "Liabilities:AP").debit_minor == 100_000
        and kit.line(entry, "Assets:Bank:EUR").credit_minor == 100_000
    )
    assert {x.role for x in entry.evidence} == {"payment_request", "chip_response", "payment"}
    assert kit.ledger.get_payable(p.id).status == "paid"
    assert kit.bank.balance("EUR") == OPENING_BANK_MINOR["EUR"] - 100_000
    assert kit.pay_state.payment("PAY-000001").status == "executed"
    assert kit.ledger.ap_control_reconciles().ok
    assert {a["action_type"] for a in kit.actions("PAY-000001")} >= {"payment_requested", "posted"}
    kit.tick(days=1)  # 3 Oct: the bank publishes yesterday's statement for 06:00
    kit.tick(days=1)  # 4 Oct: statement and line delivered and reconciled
    lines = [
        e for e in events(kit, "bank_statement_line") if e.payload["customer_ref"] == "PAY-000001"
    ]
    assert len(lines) == 1 and lines[0].payload["statement_id"] == "EUR261002P"
    assert kit.pay_state.payment("PAY-000001").status == "settled"
    assert kit.actions("PAY-000001", "reconciled")
    assert kit.escalations() == []


def test_unprovisioned_vendor_is_held_not_allowlisted_and_never_retried(kit: Kit) -> None:
    p = kit.payable(V_UNPROVISIONED, 50_000, due=date(2026, 10, 2))
    kit.tick(days=1)
    chip = events(kit, "chip_response")
    assert (
        len(chip) == 1
        and not chip[0].payload["ok"]
        and chip[0].payload["reasons"] == ["not_allowlisted"]
        and chip[0].payload["chip_reason"] == "not_allowlisted"
        and chip[0].payload["desk_annotations"] == []
    )
    held = kit.actions(action_type="payment_held")
    assert len(held) == 1 and held[0]["decision"]["held_reason"] == "not_allowlisted"
    assert (
        kit.ledger.get_payable(p.id).status == "held"
        and kit.ledger.get_payable(p.id).held_reason == "not_allowlisted"
    )
    prov = events(kit, "provisioning")
    assert any(
        e.payload["vendor_id"] == V_UNPROVISIONED
        and not e.payload["ok"]
        and e.payload["reason"] == "jumper_absent"
        for e in prov
    )
    assert kit.provisioner.queued() == [V_UNPROVISIONED]
    assert events(kit, "payment_executed") == [] and kit.ledger.ap_control_reconciles().ok
    kit.tick(days=2)
    assert len(events(kit, "payment_requested")) == 1, (
        "a rejected payment is never retried on its own"
    )
    # once the operator provisions the vendor (jumper on), the held payables reopen and the next run pays
    kit.provision(V_UNPROVISIONED)
    kit.run()
    assert kit.ledger.get_payable(p.id).status == "open"
    kit.tick(days=1)
    assert kit.ledger.get_payable(p.id).status == "paid"


def test_payment_above_threshold_needs_the_button(kit: Kit) -> None:
    p = kit.payable(V_EUR, 6_000_000, due=date(2026, 10, 2), number="FT-501")
    kit.tick(days=1)
    chip = events(kit, "chip_response")
    assert len(chip) == 1 and chip[0].payload["reasons"] == ["needs_button"]
    assert kit.ledger.get_payable(p.id).held_reason == "needs_button"
    packets = kit.packets_open(kind="approval")
    assert len(packets) == 1
    pk = packets[0].payload
    assert (
        pk["recommendation"] == "approve"
        and "PAY-000001" in pk["refs"]
        and "FT-501" in pk["refs"]
        and pk["options"] == ["approve", "reject"]
    )
    assert kit.actions("PAY-000001", "payment_held")[0]["rule_id"] == "R10"
    kit.decide(pk["packet_id"], "approve")
    assert kit.pay_state.payment("PAY-000001").status == "awaiting_button", (
        "approval alone does not pay: the button nonce is still missing"
    )
    assert len(events(kit, "payment_executed")) == 0
    kit.press_button()
    assert kit.pay_state.payment("PAY-000001").status == "executed"
    chip = events(kit, "chip_response")
    assert chip[-1].payload["ok"] and chip[-1].payload["pay_id"] == "PAY-000001"
    assert kit.ledger.get_payable(p.id).status == "paid"
    assert kit.signer.nonce() is None, "the nonce was consumed by the payment"
    assert kit.actions("PAY-000001", "posted")[0]["rule_id"] == "R10"


def test_rejected_button_packet_keeps_the_payable_held(kit: Kit) -> None:
    p = kit.payable(V_EUR, 6_000_000, due=date(2026, 10, 2))
    kit.tick(days=1)
    pk = kit.packets_open(kind="approval")[0].payload
    kit.decide(pk["packet_id"], "reject")
    assert kit.pay_state.payment("PAY-000001").status == "rejected"
    assert kit.ledger.get_payable(p.id).held_reason == "rejected_by_controller"
    kit.press_button()
    assert events(kit, "payment_executed") == []


def test_discount_is_taken_and_posted_to_income(kit: Kit) -> None:
    p = kit.payable(
        V_EUR, 100_000, due=date(2026, 10, 22), discount_bp=200, discount_deadline=date(2026, 10, 2)
    )
    kit.tick(days=1)
    entry = next(e for e in kit.ledger.entries() if e.cause_type == "payment")
    assert kit.line(entry, "Liabilities:AP").debit_minor == 100_000
    assert kit.line(entry, "Assets:Bank:EUR").credit_minor == 98_000
    assert kit.line(entry, "Income:Discounts").credit_minor == 2_000
    assert (
        kit.ledger.get_payable(p.id).status == "paid"
        and kit.bank.balance("EUR") == OPENING_BANK_MINOR["EUR"] - 98_000
    )
    assert kit.ledger.ap_control_reconciles().ok


def test_t9_cash_shortfall_produces_quantified_options_and_draw_pays(tmp_path: Path) -> None:
    kit = Kit(
        tmp_path,
        payments=True,
        bank_opening={"EUR": 15_000_000, "CZK": 840_000_000, "PLN": 210_000_000},
        button_threshold=10**12,
    )
    kit.provision(V_EUR)
    p = kit.payable(
        V_EUR,
        18_000_000,
        due=date(2026, 10, 22),
        discount_bp=200,
        discount_deadline=date(2026, 10, 2),
        number="RE-2026-0931",
    )
    kit.tick(days=1)
    assert events(kit, "payment_requested") == [], "no payment without a decision"
    packets = kit.packets_open(kind="approval")
    assert len(packets) == 1
    pk = packets[0].payload
    assert (
        pk["options"] == ["pay_partial_now", "draw_intercompany", "skip_discount"]
        and pk["recommendation"] == "draw_intercompany"
    )
    assert (
        "RE-2026-0931" in pk["refs"]
        and pk["materiality_minor"] == 360_000
        and pk["currency"] == "EUR"
    )
    assert "2,640,000" in pk["question"] or "26,400.00" in pk["question"]
    assert "pay_partial_now" in pk["context"] and "draw_intercompany" in pk["context"]
    held = kit.actions(action_type="discount_escalated")
    assert not kit.actions(action_type="held"), "a discount question is not a hold"
    assert (
        held
        and held[0]["decision"]["recommendation"] == "draw_intercompany"
        and set(held[0]["decision"]["options"])
        == {"pay_partial_now", "draw_intercompany", "skip_discount"}
    )
    kit.tick(days=1)
    assert len(kit.packets_open(kind="approval")) == 1, "the open decision is not re-raised daily"
    kit.decide(pk["packet_id"], "draw_intercompany")
    draw = next(e for e in kit.ledger.entries() if e.cause_type == "intercompany")
    assert (
        kit.line(draw, "Assets:Bank:EUR").debit_minor == 2_640_000
        and kit.line(draw, "Liabilities:Intercompany").credit_minor == 2_640_000
    )
    pay = next(e for e in kit.ledger.entries() if e.cause_type == "payment")
    assert (
        kit.line(pay, "Liabilities:AP").debit_minor == 18_000_000
        and kit.line(pay, "Assets:Bank:EUR").credit_minor == 17_640_000
        and kit.line(pay, "Income:Discounts").credit_minor == 360_000
    )
    assert kit.bank.balance("EUR") == 0 and kit.ledger.get_payable(p.id).status == "paid"
    assert kit.ledger.ap_control_reconciles().ok
    kit.close()


def test_t9_pay_partial_now_pays_what_cash_allows(tmp_path: Path) -> None:
    kit = Kit(
        tmp_path,
        payments=True,
        bank_opening={"EUR": 15_000_000, "CZK": 840_000_000, "PLN": 210_000_000},
        button_threshold=10**12,
    )
    kit.provision(V_EUR)
    p = kit.payable(
        V_EUR,
        18_000_000,
        due=date(2026, 10, 22),
        discount_bp=200,
        discount_deadline=date(2026, 10, 2),
    )
    kit.tick(days=1)
    pk = kit.packets_open(kind="approval")[0].payload
    kit.decide(pk["packet_id"], "pay_partial_now")
    pay = next(e for e in kit.ledger.entries() if e.cause_type == "payment")
    assert (
        kit.line(pay, "Liabilities:AP").debit_minor == 15_000_000
        and kit.line(pay, "Assets:Bank:EUR").credit_minor == 14_700_000
        and kit.line(pay, "Income:Discounts").credit_minor == 300_000
    )
    remaining = kit.ledger.get_payable(p.id)
    assert remaining.status == "open" and remaining.amount_minor == 3_000_000
    assert kit.bank.balance("EUR") == 300_000 and kit.ledger.ap_control_reconciles().ok
    kit.tick(days=1)
    assert len(kit.packets_open(kind="approval")) == 1, (
        "the decided discount question is not asked again"
    )
    kit.close()


def test_t9_skip_discount_pays_in_full_at_the_due_date(tmp_path: Path) -> None:
    kit = Kit(
        tmp_path,
        payments=True,
        bank_opening={"EUR": 15_000_000, "CZK": 840_000_000, "PLN": 210_000_000},
        button_threshold=10**12,
    )
    kit.provision(V_EUR)
    p = kit.payable(
        V_EUR,
        18_000_000,
        due=date(2026, 10, 4),
        discount_bp=200,
        discount_deadline=date(2026, 10, 2),
    )
    kit.tick(days=1)
    pk = kit.packets_open(kind="approval")[0].payload
    kit.decide(pk["packet_id"], "skip_discount")
    assert events(kit, "payment_requested") == []
    kit.bank.credit("EUR", 5_000_000, reference="FUNDING-1", remittance="/REMI/CUSTOMER SETTLEMENT")
    kit.tick(days=2)  # 4 Oct: due today, paid in full, no discount
    pay = next(e for e in kit.ledger.entries() if e.cause_type == "payment")
    assert kit.line(pay, "Assets:Bank:EUR").credit_minor == 18_000_000
    assert kit.ledger.get_payable(p.id).status == "paid"
    assert all(ln.account != "Income:Discounts" for ln in pay.lines)
    kit.close()


def test_credit_note_nets_against_the_next_payment(kit: Kit) -> None:
    p = kit.payable(V_EUR, 100_000, due=date(2026, 10, 2))
    cn = kit.payable(V_EUR, -30_000, due=date(2026, 10, 1), inv_id="CN-12")
    kit.tick(days=1)
    req = events(kit, "payment_requested")[0].payload
    assert req["amount_minor"] == 70_000 and set(req["payable_ids"]) == {p.id, cn.id}
    entry = next(e for e in kit.ledger.entries() if e.cause_type == "payment")
    assert (
        kit.line(entry, "Liabilities:AP").debit_minor == 70_000
        and kit.line(entry, "Assets:Bank:EUR").credit_minor == 70_000
    )
    assert (
        kit.ledger.get_payable(p.id).status == "paid"
        and kit.ledger.get_payable(cn.id).status == "netted"
    )
    assert kit.ledger.ap_control_reconciles().ok


def test_legacy_batch_line_settles_exactly_the_scheduled_payables(kit: Kit) -> None:
    a = kit.payable(
        "V-0020", 6_248_000, due=date(2026, 10, 2), inv_id="KMF/2026/09/0412", status="scheduled"
    )
    b = kit.payable(
        "V-0020", 7_115_012, due=date(2026, 10, 5), inv_id="KMF/2026/09/0413", status="scheduled"
    )
    c = kit.payable(
        "V-0020", 5_380_000, due=date(2026, 10, 9), inv_id="KMF/2026/09/0417", status="scheduled"
    )
    kit.bank_line(
        "EUR",
        date(2026, 10, 2),
        "D",
        18_743_012,
        customer_ref="PAY-BATCH-0001",
        remittance="/REMI/KMF/2026/09/0412 KMF/2026/09/0413 KMF/2026/09/0417 KATOWICE METAL FORMING S.A. SEPTEMBER 2026 BATCH",
    )
    kit.tick(days=1)
    assert events(kit, "payment_requested") == [], "scheduled payables are not paid again"
    kit.tick(days=2)
    entry = next(e for e in kit.ledger.entries() if e.cause_type == "payment")
    assert (
        entry.cause_id == "PAY-BATCH-0001"
        and kit.line(entry, "Liabilities:AP").debit_minor == 18_743_012
        and kit.line(entry, "Assets:Bank:EUR").credit_minor == 18_743_012
    )
    assert {kit.ledger.get_payable(x.id).status for x in (a, b, c)} == {"paid"}
    rec = kit.actions("PAY-BATCH-0001", "reconciled")
    assert len(rec) == 1 and rec[0]["decision"]["matched"] == "payables_batch"
    assert kit.ledger.ap_control_reconciles().ok and kit.escalations() == []


def test_fees_fx_and_unmatched_lines(kit: Kit) -> None:
    kit.bank_line(
        "EUR",
        date(2026, 10, 1),
        "D",
        4500,
        txn_type="NCHG",
        customer_ref="NONREF",
        remittance="ACCOUNT MAINTENANCE FEE 09/2026",
    )
    kit.bank_line(
        "EUR",
        date(2026, 10, 1),
        "D",
        20_000_000,
        txn_type="NFEX",
        customer_ref="FX-2026-1001",
        remittance="/REMI/FX CONVERSION EUR/CZK",
        line_no=2,
    )
    kit.bank_line(
        "CZK",
        date(2026, 10, 1),
        "C",
        490_000_000,
        txn_type="NFEX",
        customer_ref="FX-2026-1001",
        remittance="/REMI/FX CONVERSION EUR/CZK",
    )
    kit.bank_line(
        "EUR",
        date(2026, 10, 1),
        "D",
        124_000_000,
        customer_ref="PAYROLL-1026",
        remittance="/REMI/PAYROLL 10/2026",
        line_no=3,
    )
    kit.tick(days=2)  # lines known 2 Oct 06:00
    fee = next(e for e in kit.ledger.entries() if e.cause_type == "fee")
    assert (
        kit.line(fee, "Expenses:Bank:Fees").debit_minor == 4500
        and kit.line(fee, "Assets:Bank:EUR").credit_minor == 4500
    )
    fx = next(e for e in kit.ledger.entries() if e.cause_type == "fx")
    assert (
        kit.line(fx, "Assets:Bank:CZK").debit_minor == 490_000_000
        and kit.line(fx, "Assets:Bank:EUR").credit_minor == 20_000_000
    )
    assert all(ln.account != "Expenses:FX:Realized" for ln in fx.lines), (
        "24.5 is the kit rate: nothing realised"
    )
    assert kit.packets_open() == [], "an unmatched line is not escalated before two days"
    kit.tick(days=2)
    notices = kit.packets_open(kind="notice")
    assert (
        len(notices) == 1
        and notices[0].payload["severity"] == "info"
        and "PAYROLL-1026" in notices[0].payload["refs"]
    )
    assert [ln.status for ln in kit.pay_state.lines()] and all(
        ln.status in ("fee", "fx", "escalated") for ln in kit.pay_state.lines()
    )
    kit.tick(days=1)
    assert len(kit.packets_open(kind="notice")) == 1, "escalated once"


def test_retention_payables_are_never_scheduled(kit: Kit) -> None:
    kit.payable(
        V_EUR,
        68_000,
        due=date(2026, 10, 2),
        inv_id="INV-RET:retention",
        status="held",
        held_reason="retention until tool acceptance (R15)",
    )
    kit.tick(days=2)
    assert events(kit, "payment_requested") == []


def test_daily_cap_defers_without_holding(tmp_path: Path) -> None:
    kit = Kit(tmp_path, payments=True, daily_cap=150_000)
    kit.provision(V_EUR, V_CZK)
    small = kit.payable(V_EUR, 100_000, due=date(2026, 10, 2))
    big = kit.payable(V_CZK, 120_000, due=date(2026, 10, 2), currency="EUR")
    kit.tick(
        days=1
    )  # chip days 1 and 2 Oct: both fall due on the 2nd; 100,000 fits, 120,000 would exceed the cap
    runs = kit.actions(action_type="payment_run_built")
    assert len(runs) == 2 and runs[0]["decision"]["paid"] == []
    assert [p["pay_id"] for p in runs[1]["decision"]["paid"]] == ["PAY-000001"]
    assert runs[1]["decision"]["deferred_cap"][0]["remaining_cap_minor"] == 50_000
    assert runs[1]["decision"]["held"] == [], "a cap deferral is not a hold"
    assert kit.ledger.get_payable(small.id).status == "paid"
    assert kit.ledger.get_payable(big.id).status == "open"
    assert len(events(kit, "chip_response")) == 1
    kit.tick(days=1)  # new chip day: cap resets
    assert kit.ledger.get_payable(big.id).status == "paid"
    kit.close()


def test_usd_payable_settles_from_the_eur_account(kit: Kit) -> None:
    kit.provision("V-0040")
    p = kit.payable("V-0040", 10_800, due=date(2026, 10, 2), currency="USD")
    kit.tick(days=1)
    executed = events(kit, "payment_executed")
    assert (
        len(executed) == 1
        and executed[0].payload["currency"] == "USD"
        and executed[0].payload["amount_minor"] == 10_800
    )
    entry = next(e for e in kit.ledger.entries() if e.cause_type == "payment")
    assert (
        kit.line(entry, "Liabilities:AP").debit_minor == 10_800
        and kit.line(entry, "Liabilities:AP").currency == "USD"
    )
    assert kit.line(entry, "Assets:Bank:EUR").credit_minor == 10_000
    assert (
        kit.bank.balance("EUR") == OPENING_BANK_MINOR["EUR"] - 10_000
        and kit.ledger.get_payable(p.id).status == "paid"
    )
    assert kit.ledger.ap_control_reconciles().ok


def test_insufficient_cash_defers_a_plain_payable(tmp_path: Path) -> None:
    kit = Kit(
        tmp_path,
        payments=True,
        bank_opening={"EUR": 50_000, "CZK": 840_000_000, "PLN": 210_000_000},
        overdraft={},
    )
    kit.provision(V_EUR)
    p = kit.payable(V_EUR, 100_000, due=date(2026, 10, 2))
    kit.tick(days=1)
    run = kit.actions(action_type="payment_run_built")[-1]
    assert (
        run["decision"]["deferred_cash"][0]["amount_minor"] == 100_000
        and kit.ledger.get_payable(p.id).status == "open"
    )
    assert events(kit, "payment_requested") == [] and kit.escalations() == []
    kit.close()


def test_agent_subscriptions_match_the_spec() -> None:
    assert PaymentAgent.subscribes >= {"day_boundary", "controller_decision", "bank_statement_line"}
    assert PaymentAgent.principal == "payment_agent"
    assert at(date(2026, 10, 2), 6) - at(date(2026, 10, 1), 6) == timedelta(days=1)
