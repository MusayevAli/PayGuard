"""Human-facing money uses major units while agent decisions retain minor units."""

from __future__ import annotations

from datetime import date
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from agents.invoice_agent import Held, InvoiceAgent
from agents.llm import template_message
from agents.payment_agent import PaymentAgent
from tests.agentkit import Kit, at


@pytest.mark.parametrize(
    "currency,minor,display", [("PLN", 1_150_000, "11,500.00 PLN"), ("JPY", 12_345, "12,345 JPY")]
)
def test_missing_po_query_formats_money_without_changing_structured_amount(
    currency: str, minor: int, display: str
) -> None:
    queries: list[dict[str, Any]] = []
    agent = SimpleNamespace(
        state=SimpleNamespace(last_reply=lambda *_: None, open_waits=lambda *_: []),
        _ask=lambda *args, **kwargs: queries.append(kwargs),
    )
    invoice = SimpleNamespace(
        invoice_number="LOCAL-0001",
        gross_minor=minor,
        currency=currency,
        lines=[SimpleNamespace(description="Steel frame", sku="SF-1010")],
    )
    supplier = SimpleNamespace(
        category="seat_frames", legal_name="Local Supplier", purchasing_contact="buyer"
    )
    with pytest.raises(Held):
        InvoiceAgent._no_po(agent, "INV-0001", invoice, supplier, "V-0001", "ev_000001", {}, None)
    assert len(queries) == 1
    assert f"({display})" in queries[0]["text"]
    assert (queries[0]["amount"], queries[0]["currency"]) == (minor, currency)


@pytest.mark.parametrize("confirmed_query", [True, False])
def test_freight_buyer_query_and_denial_packet_use_major_units(confirmed_query: bool) -> None:
    captured: list[dict[str, Any]] = []

    def hold(*_: Any, **kwargs: Any) -> None:
        captured.append(kwargs)
        raise Held

    agent = SimpleNamespace(
        _eur=lambda *_: 1_000_000,
        state=SimpleNamespace(
            last_reply=lambda *_: None if confirmed_query else {"refs": [], "body": "Denied"},
            open_waits=lambda *_: [],
        ),
        llm=SimpleNamespace(draft_message=template_message),
        _ask=lambda *args, **kwargs: captured.append(kwargs),
        _hold=hold,
    )
    invoice = SimpleNamespace(
        invoice_number="LOCAL-0001",
        invoice_date=date(2026, 10, 1),
        net_minor=1_150_000,
        gross_minor=1_150_000,
        currency="PLN",
        lines=[SimpleNamespace(description="Freight")],
    )
    supplier = SimpleNamespace(legal_name="Local Carrier", purchasing_contact="buyer")
    with pytest.raises(Held):
        InvoiceAgent._post_freight(
            agent, "INV-0001", invoice, supplier, "V-0001", "ev_000001", {}, None
        )
    assert len(captured) == 1
    if confirmed_query:
        assert "11500.00 PLN" in captured[0]["text"]
        assert (captured[0]["amount"], captured[0]["currency"]) == (1_000_000, "EUR")
    else:
        assert "11,500.00 PLN" in captured[0]["question"]
        assert (captured[0]["materiality"], captured[0]["currency"]) == (1_150_000, "PLN")
    assert invoice.net_minor == invoice.gross_minor == 1_150_000


def test_foreign_payable_cash_deferral_keeps_settlement_currency_explicit(tmp_path: Path) -> None:
    kit = Kit(
        tmp_path,
        payments=True,
        bank_opening={"EUR": 5_000, "CZK": 840_000_000, "PLN": 210_000_000},
        overdraft={},
    )
    try:
        kit.provision("V-0040")
        payable = kit.payable("V-0040", 10_800, due=date(2026, 10, 2), currency="USD")
        kit.tick(days=1)
        decision = kit.actions(action_type="payment_run_built")[-1]["decision"]
        assert decision["deferred_cash"] == [
            {
                "vendor_id": "V-0040",
                "currency": "USD",
                "amount_minor": 10_800,
                "cash_minor": 5_000,
                "cash_currency": "EUR",
                "settlement_currency": "EUR",
            }
        ]
        assert kit.bank.balance("EUR") == 5_000
        assert kit.ledger.get_payable(payable.id).status == "open"
        assert kit.store.query(type="payment_requested", limit=10) == []
    finally:
        kit.close()


def test_short_payment_notice_uses_major_strings_and_preserves_minor_facts() -> None:
    actions: list[dict[str, Any]] = []
    agent = SimpleNamespace(llm=SimpleNamespace(draft_message=template_message))
    invoice = SimpleNamespace(invoice_number="LOCAL-0001", currency="PLN")
    supplier = SimpleNamespace(id="V-0001", legal_name="Local Supplier")
    item = SimpleNamespace(
        balance=2,
        unit_price_po_units=12_345,
        qty_to_post=3,
        qty_po_units=5,
        qty_available=3,
        po_line=SimpleNamespace(sku="SF-1010"),
        po=SimpleNamespace(id="PO-10001"),
        receipts=[SimpleNamespace(gr=SimpleNamespace(id="GR-1001"))],
    )
    ctx = SimpleNamespace(act=lambda **kwargs: actions.append(kwargs))
    InvoiceAgent._short_pay_notice(agent, "INV-0001", invoice, supplier, [item], ctx)
    assert len(actions) == 1
    action = actions[0]
    assert "370.35 PLN" in action["decision"]["message"]
    assert "246.90 PLN" in action["decision"]["message"]
    assert action["decision"]["short_pay_minor"] == 24_690
    assert action["inputs"]["short_pay_minor"] == 24_690
    assert action["inputs"]["amount_posted_minor"] == 37_035
    assert action["inputs"]["qty_posted"] == 3


def test_reverse_charge_dispute_packet_scales_amounts_but_keeps_journal_exact(
    tmp_path: Path,
) -> None:
    kit = Kit(tmp_path)
    try:
        po = kit.po("V-0009", [("HM-4010", 9000, 695)], order_date=date(2026, 9, 19))
        kit.receipt(po, 1, 9000, when=at(date(2026, 9, 27)))
        invoice = kit.invoice(
            "V-0009",
            "LOCAL-0001",
            [("HM-4010", 9000, 695)],
            invoice_date=date(2026, 9, 30),
            po_refs=[po.id],
            vat_rate_bp=2100,
        )
        kit.run()
        packets = kit.escalations("LOCAL-0001")
        assert len(packets) == 1
        assert "13,135.50 CZK" in packets[0].payload["question"]
        assert "62,550.00 CZK" in packets[0].payload["context"]
        entry = kit.entry_for(invoice.id)
        assert entry is not None
        assert kit.line(entry, "Liabilities:AP").credit_minor == 6_255_000
        posted = kit.actions("LOCAL-0001", "posted")[0]
        assert posted["decision"]["vat_disputed_minor"] == 1_313_550
    finally:
        kit.close()


def test_domestic_vat_mismatch_question_labels_every_amount(tmp_path: Path) -> None:
    kit = Kit(tmp_path)
    try:
        po = kit.po("V-0002", [("FM-2040", 1500, 216)], order_date=date(2026, 9, 15))
        kit.receipt(po, 1, 1500, when=at(date(2026, 9, 26)))
        invoice = kit.invoice(
            "V-0002",
            "LOCAL-0001",
            [("FM-2040", 1500, 216)],
            invoice_date=date(2026, 9, 30),
            po_refs=[po.id],
            vat_rate_bp=1800,
        )
        kit.run()
        packets = kit.escalations("LOCAL-0001")
        assert len(packets) == 1
        question = packets[0].payload["question"]
        assert all(amount in question for amount in ("583.20 EUR", "3,240.00 EUR", "615.60 EUR"))
        assert kit.entry_for(invoice.id) is None
        assert kit.ledger.payables() == []
    finally:
        kit.close()


def test_new_vendor_message_uses_formatted_major_amount(tmp_path: Path) -> None:
    kit = Kit(tmp_path)
    try:
        kit.invoice(
            None,
            "LOCAL-0001",
            [("SF-1010", 1, 123_450)],
            invoice_date=date(2026, 9, 30),
            supplier_name="Unregistered Supplier",
            currency="PLN",
        )
        kit.run()
        messages = kit.store.query(type="vendor_message", limit=10)
        assert len(messages) == 1
        assert "1,234.50 PLN" in messages[0].payload["body"]
        assert kit.ledger.payables() == []
    finally:
        kit.close()


@pytest.mark.parametrize("limited_cap", [False, True])
def test_foreign_payment_and_cap_audit_metadata_preserves_each_currency(
    tmp_path: Path, limited_cap: bool
) -> None:
    kit = Kit(tmp_path, payments=True, daily_cap=5_000 if limited_cap else 500_000)
    try:
        kit.provision("V-0040")
        payable = kit.payable("V-0040", 10_800, due=date(2026, 10, 2), currency="USD")
        kit.tick(days=1)
        run = kit.actions(action_type="payment_run_built")[-1]
        assert run["inputs"]["daily_cap_remaining_currency"] == "EUR"
        if limited_cap:
            deferred = run["decision"]["deferred_cap"]
            assert len(deferred) == 1
            assert deferred[0]["currency"] == "USD"
            assert deferred[0]["remaining_cap_minor"] == 5_000
            assert deferred[0]["remaining_cap_currency"] == "EUR"
            assert kit.ledger.get_payable(payable.id).status == "open"
        else:
            posted = kit.actions("PAY-000001", "posted")[0]["decision"]
            assert (posted["post_minor"], posted["currency"]) == (10_800, "USD")
            assert (posted["paid_minor"], posted["paid_currency"]) == (10_000, "EUR")
            assert posted["settlement_currency"] == "EUR"
            assert kit.ledger.get_payable(payable.id).status == "paid"
    finally:
        kit.close()


def test_rounding_residual_metadata_identifies_native_currency() -> None:
    posted: list[dict[str, Any]] = []

    def post(*_: Any, **kwargs: Any) -> SimpleNamespace:
        posted.append(kwargs)
        return SimpleNamespace(id="JE-000001")

    agent = SimpleNamespace(_post=post)
    event = SimpleNamespace(payload={"value_date": "2026-10-02"}, id="ev_000001")
    assert PaymentAgent._post_residual(agent, -3, "PLN", event, None, "PAY-000001") == "JE-000001"
    assert posted[0]["inputs"] == {"difference_minor": -3, "currency": "PLN"}
    assert posted[0]["decision"] == {"rule": "R12", "post_minor": 3, "currency": "PLN"}
