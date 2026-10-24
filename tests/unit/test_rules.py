"""One test per rule R0–R7, R11–R15 (spec §6) plus M3/M5/M6, on synthetic inputs through the
real invoice agent, policy, ledger and scripted responders."""

from __future__ import annotations

from datetime import date, timedelta
from pathlib import Path

import pytest

from agents.parsed import ParsedInvoice, ParsedLine
from core.rules import (
    DUP_TOTAL_TOL_BP,
    PRICE_TOL_BP,
    applicable_price_list,
    discount_terms,
    is_duplicate,
    normalize_uom,
    retention_split,
    rounding_delta,
    variance_bp,
)
from sim.models import WrongPoAnswer
from sim.worldgen.suppliers import build_suppliers
from tests.agentkit import Kit, at


@pytest.fixture
def kit(tmp_path: Path) -> Kit:
    k = Kit(tmp_path)
    yield k
    k.close()


# ------------------------------------------------------------------ pure helpers


def test_rule_helpers() -> None:
    assert variance_bp(1420, 1390) == 216 and variance_bp(1390, 1390) == 0
    assert variance_bp(1410, 1390) <= PRICE_TOL_BP < variance_bp(1420, 1390)
    assert is_duplicate(424000, date(2026, 10, 13), "GF-77", 424000, date(2026, 10, 15), "GF-77A")
    assert is_duplicate(
        424000, date(2026, 10, 13), "GF-77", 425272, date(2026, 10, 13), "GF-78"
    )  # +0.3 % (A15)
    assert not is_duplicate(
        424000, date(2026, 10, 13), "GF-77", 424000, date(2026, 10, 20), "GF-79"
    )  # 7 days
    assert not is_duplicate(
        424000, date(2026, 10, 13), "GF-77", 424000, date(2026, 10, 14), "GF-77"
    )  # same number
    assert not is_duplicate(
        100000,
        date(2026, 10, 13),
        "A",
        100000 + 100000 * (DUP_TOTAL_TOL_BP + 1) // 10000,
        date(2026, 10, 13),
        "B",
    )
    assert normalize_uom(2000, "pc", "box50") == (40, "box50")
    assert normalize_uom(40, "box50", "box50") == (40, "box50")
    assert normalize_uom(40, "box50", "pc") == (2000, "pc")
    assert normalize_uom(10, "kg", "m") is None and normalize_uom(10, "kg", "kg") == (10, "kg")
    assert (
        rounding_delta(61560, 61563) == 3
        and rounding_delta(61560, 61556) == -4
        and rounding_delta(61560, 61570) is None
    )
    assert retention_split(680000) == (612000, 68000) and sum(retention_split(100001)) == 100001
    assert discount_terms("2/10 net30") == (200, 10) and discount_terms("net30") is None
    gap = next(s for s in build_suppliers(42) if s.id == "V-0018")
    current, candidates = applicable_price_list(gap.price_lists, date(2026, 10, 7))
    assert current is None and [c.id for c in candidates] == ["PL-V-0018-2026A", "PL-V-0018-2026B"]
    current, _ = applicable_price_list(gap.price_lists, date(2026, 9, 15))
    assert current is not None and current.id == "PL-V-0018-2026A"


# ------------------------------------------------------------------ R0


def test_r0_unreconciled_totals_hold(kit: Kit) -> None:
    po = kit.po("V-0002", [("FM-2001", 2400, 1390)])
    kit.receipt(po, 1, 2400, when=at(date(2026, 9, 28)))
    bad = ParsedInvoice(
        supplier_name="Foamtech GmbH",
        supplier_vat_id=kit.by_id["V-0002"].vat_id,
        invoice_number="FT-200",
        invoice_date="30.09.2026",
        due_date=None,
        service_date=None,
        po_references=[po.id],
        currency="EUR",
        lines=[
            ParsedLine(
                description="Seat cushion foam front",
                sku="FM-2001",
                qty="2.400",
                uom="Stk.",
                unit_price="13,90",
                line_total="30.000,00",
            )
        ],
        net="33.360,00",
        vat_rate="19 %",
        vat_amount="6.338,40",
        gross="39.698,40",
        bank_details=None,
        document_language="de",
    )
    ev = kit.invoice(
        "V-0002",
        "FT-200",
        [("FM-2001", 2400, 1390)],
        invoice_date=date(2026, 9, 30),
        doc="pdf",
        parsed=[bad, bad],
    )
    kit.run()
    assert len(kit.llm.calls) == 2 and kit.llm.calls[1][1], (
        "second extraction must carry the validation errors"
    )
    row = kit.state.invoice(kit.invoice_id_of(ev))
    assert row is not None and row.status == "held" and row.rule_id == "R0"
    esc = kit.escalations("FT-200")
    assert esc and "unreadable" in esc[0].payload["question"].lower()
    assert kit.entry_for(ev.id) is None


# ------------------------------------------------------------------ R1


def test_r1_short_pay(kit: Kit) -> None:
    po = kit.po("V-0001", [("SF-1001", 1000, 1390)], order_date=date(2026, 9, 18))
    kit.receipt(po, 1, 800, when=at(date(2026, 9, 25)))
    ev = kit.invoice(
        "V-0001",
        "KT-2291",
        [("SF-1001", 1000, 1390)],
        invoice_date=date(2026, 9, 30),
        po_refs=[po.id],
    )
    kit.run()
    entry = kit.entry_for(ev.id)
    assert entry is not None
    assert (
        kit.line(entry, "Liabilities:AP").credit_minor == 1_112_000
        and kit.line(entry, "Liabilities:AP").currency == "EUR"
    )
    assert kit.line(entry, "Expenses:Materials:Frames").debit_minor == 1_112_000
    assert kit.line(entry, "Expenses:VAT:Input").debit_minor == 211_280
    assert kit.line(entry, "Liabilities:VAT:Output").credit_minor == 211_280
    assert {e.role for e in entry.evidence} >= {"invoice", "receipt", "po"}
    types = kit.action_types("KT-2291")
    assert (
        "extracted" in types
        and "matched" in types
        and "posted" in types
        and "short_pay_notice" in types
    )
    posted = kit.actions("KT-2291", "posted")[0]
    assert posted["rule_id"] == "R1" and posted["decision"]["qty_posted"] == 800
    notice = kit.actions("KT-2291", "short_pay_notice")[0]
    assert notice["decision"]["short_pay_minor"] == 278_000 and "KT-2291" in notice["explanation"]
    assert kit.escalations() == []
    row = kit.state.invoice(kit.invoice_id_of(ev))
    assert row is not None and row.status == "posted" and row.rule_id == "R1"
    assert kit.state.po_line_balance(po.id, 1) == 200
    payables = kit.ledger.payables(vendor_id="V-0001")
    assert (
        len(payables) == 1
        and payables[0].amount_minor == 1_112_000
        and payables[0].inv_id == kit.invoice_id_of(ev)
    )
    assert kit.ledger.ap_control_reconciles().ok


# ------------------------------------------------------------------ R2a / R2b


def test_r2a_within_tolerance_posts_invoice_price(kit: Kit) -> None:
    po = kit.po("V-0002", [("FM-2001", 2400, 1390)])
    kit.receipt(po, 1, 2400, when=at(date(2026, 9, 28)))
    ev = kit.invoice(
        "V-0002",
        "FT-117",
        [("FM-2001", 2400, 1410)],
        invoice_date=date(2026, 9, 30),
        po_refs=[po.id],
    )
    kit.run()
    entry = kit.entry_for(ev.id)
    assert entry is not None
    assert kit.line(entry, "Liabilities:AP").credit_minor == 4_026_960
    assert kit.line(entry, "Expenses:Materials:Foam").debit_minor == 3_384_000
    assert kit.line(entry, "Expenses:VAT:Input").debit_minor == 642_960
    assert kit.actions("FT-117", "posted")[0]["rule_id"] == "R2a"
    assert kit.escalations() == []


def test_r2b_variance_escalates(kit: Kit) -> None:
    po = kit.po("V-0002", [("FM-2001", 2400, 1390)])
    kit.receipt(po, 1, 2400, when=at(date(2026, 9, 28)))
    ev = kit.invoice(
        "V-0002",
        "FT-118",
        [("FM-2001", 2400, 1420)],
        invoice_date=date(2026, 9, 30),
        po_refs=[po.id],
    )
    kit.run()
    assert kit.entry_for(ev.id) is None
    esc = kit.escalations("FT-118")
    assert len(esc) == 1 and esc[0].payload["refs"] and "216" in esc[0].payload["context"]
    row = kit.state.invoice(kit.invoice_id_of(ev))
    assert row is not None and row.status == "held" and row.rule_id == "R2b"
    assert "held" in kit.action_types("FT-118")
    assert kit.ledger.payables() == []


# ------------------------------------------------------------------ R3


def test_r3_no_receipt_asks_then_posts_when_the_receipt_arrives(kit: Kit) -> None:
    po = kit.po("V-0005", [("HM-4001", 400, 2140)], order_date=date(2026, 9, 25))
    ev = kit.invoice(
        "V-0005", "HM-09", [("HM-4001", 400, 2140)], invoice_date=date(2026, 9, 29), po_refs=[po.id]
    )
    kit.run()
    asked = kit.actions("HM-09", "asked_colleague")
    assert (
        len(asked) == 1
        and asked[0]["rule_id"] == "R3"
        and asked[0]["decision"]["to_person"] == "jonas.weber"
    )
    row = kit.state.invoice(kit.invoice_id_of(ev))
    assert row is not None and row.status == "held" and row.rule_id == "R3"
    assert kit.entry_for(ev.id) is None
    kit.receipt(po, 1, 400, when=kit.now + timedelta(hours=30))
    kit.tick(hours=31)
    entry = kit.entry_for(ev.id)
    assert entry is not None and kit.line(entry, "Liabilities:AP").credit_minor == 1_018_640
    assert kit.escalations() == []
    assert kit.state.invoice(kit.invoice_id_of(ev)).status == "posted"


def test_r3_escalates_after_48_hours_without_a_receipt(kit: Kit) -> None:
    po = kit.po("V-0005", [("HM-4001", 400, 2140)], order_date=date(2026, 9, 25))
    ev = kit.invoice(
        "V-0005", "HM-10", [("HM-4001", 400, 2140)], invoice_date=date(2026, 9, 29), po_refs=[po.id]
    )
    kit.run()
    kit.tick(days=1)
    assert kit.escalations("HM-10") == []  # still inside the 48 h window
    kit.tick(days=2)
    esc = kit.escalations("HM-10")
    assert len(esc) == 1 and esc[0].payload["agent"] == "invoice_agent"
    assert kit.state.invoice(kit.invoice_id_of(ev)).status == "escalated"
    assert kit.entry_for(ev.id) is None


# ------------------------------------------------------------------ R4


def test_r4_duplicate_rejected_citing_the_original(kit: Kit) -> None:
    po = kit.po("V-0003", [("FS-6001", 80, 5300)], order_date=date(2026, 9, 24))
    kit.receipt(po, 1, 80, when=at(date(2026, 9, 28)))
    first = kit.invoice(
        "V-0003", "GF-77", [("FS-6001", 80, 5300)], invoice_date=date(2026, 9, 29), po_refs=[po.id]
    )
    second = kit.invoice(
        "V-0003", "GF-77A", [("FS-6001", 80, 5300)], invoice_date=date(2026, 10, 1), po_refs=[po.id]
    )
    kit.run()
    assert kit.entry_for(first.id) is not None and kit.entry_for(second.id) is None
    rejected = kit.actions("GF-77A", "duplicate_rejected")
    assert len(rejected) == 1 and "GF-77" in rejected[0]["refs"] and rejected[0]["rule_id"] == "R4"
    assert kit.state.invoice(kit.invoice_id_of(second)).status == "rejected"
    assert len(kit.ledger.entries()) == 1 and len(kit.ledger.payables()) == 1


def test_r4_duplicate_within_50bp_total_difference(kit: Kit) -> None:
    po = kit.po(
        "V-0011", [("FS-6020", 100, 920), ("FS-6030", 4000, 43)], order_date=date(2026, 9, 20)
    )
    kit.receipt(po, 1, 100, when=at(date(2026, 9, 27)))
    kit.receipt(po, 2, 4000, when=at(date(2026, 9, 27)))
    first = kit.invoice(
        "V-0011",
        "2026-0411",
        [("FS-6020", 100, 920), ("FS-6030", 4000, 43)],
        invoice_date=date(2026, 9, 29),
        po_refs=[po.id],
    )
    second = kit.invoice(
        "V-0011",
        "2026-0412",
        [("FS-6020", 100, 920), ("FS-6030", 4000, 43), ("FR-8010", 1, 792)],
        invoice_date=date(2026, 9, 29),
        po_refs=[po.id],
        descriptions={"FR-8010": "Doprava / freight"},
    )
    kit.run()
    assert kit.entry_for(first.id) is not None and kit.entry_for(second.id) is None
    assert kit.actions("2026-0412", "duplicate_rejected")


# ------------------------------------------------------------------ R5


def test_r5_unknown_vendor_held(kit: Kit) -> None:
    ev = kit.invoice(
        None,
        "NT-2026-0147",
        [("TF-3001", 300, 1520)],
        invoice_date=date(2026, 9, 29),
        supplier_name="Nordic Trim OÜ",
        vat_id="EE100931558",
        currency="EUR",
        po_refs=["PO-10882"],
    )
    kit.run()
    row = kit.state.invoice(kit.invoice_id_of(ev))
    assert (
        row is not None
        and row.status == "held"
        and row.rule_id == "R5"
        and row.reason == "held_unknown_vendor"
    )
    assert "held" in kit.action_types("NT-2026-0147") and "onboarding_opened" in kit.action_types(
        "NT-2026-0147"
    )
    assert kit.entry_for(ev.id) is None and kit.ledger.entries() == []


# ------------------------------------------------------------------ R6


def test_r6_credit_note_reverses_proportionally(kit: Kit) -> None:
    po = kit.po(
        "V-0004", [("WH-5001", 300, 1990), ("WH-5020", 2000, 105)], order_date=date(2026, 8, 20)
    )
    kit.receipt(po, 1, 300, when=at(date(2026, 9, 4)))
    kit.receipt(po, 2, 2000, when=at(date(2026, 9, 4)))
    inv = kit.invoice(
        "V-0004",
        "LB-301",
        [("WH-5001", 300, 1990), ("WH-5020", 2000, 105)],
        invoice_date=date(2026, 9, 8),
        po_refs=[po.id],
    )
    kit.run()
    original = kit.entry_for(inv.id)
    assert original is not None and kit.line(original, "Liabilities:AP").credit_minor == 807_000
    payable = kit.ledger.payables(vendor_id="V-0004")[0]
    assert payable.amount_minor == 807_000
    cn = kit.invoice(
        "V-0004",
        "CN-12",
        [("WH-5001", 1, -80_700)],
        invoice_date=date(2026, 10, 1),
        doc="pdf",
        kind="credit_note",
        credit_note_for="LB-301",
        descriptions={"WH-5001": "Quality rebate 10 % on LB-301"},
    )
    kit.run()
    entry = kit.entry_for(cn.id, cause_type="credit_note")
    assert entry is not None and entry.reverses == original.id
    assert kit.line(entry, "Liabilities:AP").debit_minor == 80_700
    assert kit.line(entry, "Expenses:Materials:Harness").credit_minor == 80_700
    assert kit.line(entry, "Expenses:VAT:Input").credit_minor == 15_333
    assert kit.line(entry, "Liabilities:VAT:Output").debit_minor == 15_333
    assert kit.ledger.get_payable(payable.id).amount_minor == 726_300
    assert kit.actions("CN-12", "posted")[0]["rule_id"] == "R6"
    assert kit.escalations() == [] and kit.ledger.ap_control_reconciles().ok


# ------------------------------------------------------------------ R7


def test_r7_month_end_grni_accrual_and_reversal(kit: Kit) -> None:
    po = kit.po("V-0038", [("HM-4040", 600, 5460)], order_date=date(2026, 10, 8))
    gr = kit.receipt(po, 1, 600, when=at(date(2026, 10, 29), 10))
    kit.tick(days=29)  # 30 Oct
    assert kit.ledger.balance("Liabilities:GRNI", date(2026, 12, 31)) == {}
    kit.tick(days=2)  # crosses into November: October closes
    accrual = next(e for e in kit.ledger.entries() if e.cause_type == "accrual")
    assert "GRNI" in accrual.description and accrual.effective_date == date(2026, 10, 31)
    assert kit.line(accrual, "Liabilities:GRNI").credit_minor == 3_276_000
    assert kit.line(accrual, "Expenses:Materials:Mechanisms").debit_minor == 3_276_000
    gr_event = next(
        e for e in kit.store.query(type="goods_received") if e.payload["receipt"]["id"] == gr.id
    )
    assert any(x.event_id == gr_event.id for x in accrual.evidence)
    inv = kit.invoice(
        "V-0038",
        "AR-26-2210",
        [("HM-4040", 600, 5460)],
        invoice_date=date(2026, 11, 2),
        po_refs=[po.id],
        known_at=at(date(2026, 11, 4), 9),
    )
    kit.tick(days=4)  # the invoice becomes known on 4 Nov 09:00
    reversal = next(e for e in kit.ledger.entries() if e.reverses == accrual.id)
    assert any(x.event_id == inv.id for x in reversal.evidence)
    assert kit.line(reversal, "Liabilities:GRNI").debit_minor == 3_276_000
    posted = kit.entry_for(inv.id)
    assert posted is not None and kit.line(posted, "Liabilities:AP").credit_minor == 3_276_000
    action = next(a for a in kit.actions("AR-26-2210", "posted") if "post_minor" in a["decision"])
    assert (
        action["inputs"]["accrual_entry_id"] == accrual.id
        and action["inputs"]["accrual_reversal_id"] == reversal.id
    )
    assert kit.ledger.balance("Liabilities:GRNI", date(2026, 12, 31)) == {"EUR": 0}


# ------------------------------------------------------------------ R11


def test_r11_reverse_charge_supply_billed_with_vat(kit: Kit) -> None:
    po = kit.po("V-0009", [("HM-4010", 9000, 695)], order_date=date(2026, 9, 19))
    kit.receipt(po, 1, 9000, when=at(date(2026, 9, 27)))
    # 21 % Czech VAT on CZK 62,550 net = CZK 13,135.50 ≈ EUR 536 > the EUR 500 dispute threshold
    ev = kit.invoice(
        "V-0009",
        "20260455",
        [("HM-4010", 9000, 695)],
        invoice_date=date(2026, 9, 30),
        po_refs=[po.id],
        vat_rate_bp=2100,
    )
    kit.run()
    entry = kit.entry_for(ev.id)
    assert entry is not None
    assert (
        kit.line(entry, "Liabilities:AP").credit_minor == 6_255_000
        and kit.line(entry, "Liabilities:AP").currency == "CZK"
    )
    assert kit.line(entry, "Expenses:VAT:Input").debit_minor == 1_188_450
    assert kit.line(entry, "Liabilities:VAT:Output").credit_minor == 1_188_450
    posted = kit.actions("20260455", "posted")[0]
    assert posted["rule_id"] == "R11" and posted["decision"]["vat_disputed_minor"] == 1_313_550
    assert len(kit.escalations("20260455")) == 1
    assert kit.ledger.payables()[0].amount_minor == 6_255_000


# ------------------------------------------------------------------ R12


def test_r12_rounding_difference_goes_to_rounding(kit: Kit) -> None:
    po = kit.po("V-0002", [("FM-2040", 1500, 216)], order_date=date(2026, 9, 15))
    kit.receipt(po, 1, 1500, when=at(date(2026, 9, 26)))
    ev = kit.invoice(
        "V-0002",
        "FT-119",
        [("FM-2040", 1500, 216)],
        invoice_date=date(2026, 9, 30),
        po_refs=[po.id],
        gross_delta=3,
    )
    kit.run()
    entry = kit.entry_for(ev.id)
    assert entry is not None
    assert kit.line(entry, "Liabilities:AP").credit_minor == 385_563
    assert kit.line(entry, "Expenses:VAT:Input").debit_minor == 61_560
    assert kit.line(entry, "Expenses:FX:Rounding").debit_minor == 3
    assert (
        kit.escalations() == []
        and kit.actions("FT-119", "posted")[0]["decision"]["rounding_minor"] == 3
    )


# ------------------------------------------------------------------ R13


def test_r13_freight_without_po(kit: Kit) -> None:
    small = kit.invoice(
        "V-0010", "RE-2026-0501", [("FR-8010", 1, 145_000)], invoice_date=date(2026, 9, 30)
    )
    kit.run()
    entry = kit.entry_for(small.id)
    assert entry is not None and kit.line(entry, "Expenses:Freight").debit_minor == 145_000
    assert kit.line(entry, "Liabilities:AP").credit_minor == 172_550
    assert kit.actions("RE-2026-0501", "posted")[0]["rule_id"] == "R13" and not kit.actions(
        "RE-2026-0501", "asked_colleague"
    )
    big = kit.invoice(
        "V-0010", "RE-2026-0502", [("FR-8001", 1, 320_000)], invoice_date=date(2026, 9, 30)
    )
    kit.run()
    asked = kit.actions("RE-2026-0502", "asked_colleague")
    assert len(asked) == 1 and asked[0]["decision"]["to_person"] == "tomas.horak"
    assert kit.entry_for(big.id) is None
    kit.tick(hours=7)  # the buyer confirms after six hours
    entry = kit.entry_for(big.id)
    assert entry is not None and kit.line(entry, "Expenses:Freight").debit_minor == 320_000
    assert kit.escalations() == []


# ------------------------------------------------------------------ R14


def test_r14_invoice_dated_before_the_po_escalates(kit: Kit) -> None:
    po = kit.po("V-0035", [("SF-1030", 2000, 331)], order_date=date(2026, 10, 2))
    kit.receipt(po, 1, 2000, when=at(date(2026, 10, 16)))
    kit.tick(days=16)
    ev = kit.invoice(
        "V-0035",
        "2026-11870",
        [("SF-1030", 2000, 331)],
        invoice_date=date(2026, 9, 29),
        po_refs=[po.id],
    )
    kit.run()
    assert kit.entry_for(ev.id) is None
    esc = kit.escalations("2026-11870")
    assert len(esc) == 1 and "before" in esc[0].payload["question"].lower()
    assert kit.state.invoice(kit.invoice_id_of(ev)).rule_id == "R14"


# ------------------------------------------------------------------ R15


def test_r15_retention_split(kit: Kit) -> None:
    po = kit.po("V-0016", [("TL-7020", 1, 680_000)], order_date=date(2026, 9, 1))
    kit.receipt(po, 1, 1, when=at(date(2026, 9, 25)))
    ev = kit.invoice(
        "V-0016",
        "FV-2026/118",
        [("TL-7020", 1, 680_000)],
        invoice_date=date(2026, 9, 30),
        po_refs=[po.id],
        retention=True,
    )
    kit.run()
    entry = kit.entry_for(ev.id)
    assert entry is not None
    assert kit.line(entry, "Liabilities:AP").credit_minor == 612_000
    assert kit.line(entry, "Liabilities:AP:Retention").credit_minor == 68_000
    assert kit.line(entry, "Expenses:Tooling").debit_minor == 680_000
    payables = sorted(kit.ledger.payables(vendor_id="V-0016"), key=lambda p: p.inv_id)
    inv_id = kit.invoice_id_of(ev)
    assert [(p.inv_id, p.status, p.amount_minor) for p in payables] == [
        (inv_id, "open", 612_000),
        (f"{inv_id}:retention", "held", 68_000),
    ]
    assert payables[1].held_reason is not None and payables[1].held_reason.startswith("retention")
    assert kit.ledger.ap_control_reconciles().ok
    assert kit.actions("FV-2026/118", "posted")[0]["rule_id"] == "R15"


# ------------------------------------------------------------------ M3 / M5 / M6


def test_m3_price_list_gap_escalates_naming_both_lists(kit: Kit) -> None:
    sup = kit.by_id["V-0018"]
    price = sup.price_lists[0].prices["TF-3001"]
    po = kit.po("V-0018", [("TF-3001", 800, price)], order_date=date(2026, 9, 21), plant="PL")
    kit.receipt(po, 1, 800, when=at(date(2026, 10, 5)))
    kit.tick(days=6)
    ev = kit.invoice(
        "V-0018",
        "FV/2026/10/0098",
        [("TF-3001", 800, price)],
        invoice_date=date(2026, 10, 7),
        po_refs=[po.id],
    )
    kit.run()
    assert kit.entry_for(ev.id) is None
    esc = kit.escalations("FV/2026/10/0098")
    assert (
        len(esc) == 1
        and "PL-V-0018-2026A" in esc[0].payload["context"]
        and "PL-V-0018-2026B" in esc[0].payload["context"]
    )
    assert kit.state.invoice(kit.invoice_id_of(ev)).reason == "price_list_gap"


def test_m5_receipt_on_the_wrong_line_is_confirmed_and_reassigned(kit: Kit) -> None:
    po = kit.po(
        "V-0013",
        [("WH-5001", 400, 45_800), ("WH-5040", 600, 17_700)],
        order_date=date(2026, 9, 12),
        plant="CZ",
    )
    gr = kit.receipt(po, 1, 400, when=at(date(2026, 9, 26)), booked_line_no=2)
    ev = kit.invoice(
        "V-0013",
        "20260510",
        [("WH-5001", 400, 45_800)],
        invoice_date=date(2026, 9, 29),
        po_refs=[po.id],
    )
    kit.run()
    asked = kit.actions("20260510", "asked_colleague")
    assert len(asked) == 1 and gr.id in asked[0]["refs"]
    assert kit.entry_for(ev.id) is None
    kit.tick(hours=5)  # Jonas confirms the booking belongs to line 1
    entry = kit.entry_for(ev.id)
    assert entry is not None and kit.line(entry, "Liabilities:AP").credit_minor == 18_320_000
    matched = [
        a for a in kit.actions("20260510", "matched") if a["inputs"].get("reassigned_receipts")
    ]
    assert matched and matched[0]["inputs"]["reassigned_receipts"] == [gr.id]
    assert not kit.actions("20260510", "short_pay_notice") and kit.escalations() == []


def test_m5_wrong_quantity_from_the_warehouse_escalates(kit: Kit) -> None:
    po = kit.po(
        "V-0013",
        [("WH-5001", 400, 45_800), ("WH-5040", 600, 17_700)],
        order_date=date(2026, 9, 12),
        plant="CZ",
    )
    kit.receipt(po, 1, 400, when=at(date(2026, 9, 26)), booked_line_no=2)
    jonas = kit.responders.person("jonas.weber")
    jonas.script.wrong_po_answer = WrongPoAnswer(
        invoice_number="20260511", answers_from_po=po.id, answers_qty=600
    )
    ev = kit.invoice(
        "V-0013",
        "20260511",
        [("WH-5001", 400, 45_800)],
        invoice_date=date(2026, 9, 29),
        po_refs=[po.id],
    )
    kit.run()
    kit.tick(hours=5)
    assert kit.entry_for(ev.id) is None
    assert len(kit.escalations("20260511")) == 1
    assert not kit.actions("20260511", "short_pay_notice")


def test_m6_receipt_in_pc_where_the_po_is_box50(kit: Kit) -> None:
    po = kit.po("V-0003", [("FS-6002", 20, 7_100)], order_date=date(2026, 9, 20), plant="PL")
    gr = kit.receipt(po, 1, 20, when=at(date(2026, 9, 29)), uom="pc")
    ev = kit.invoice(
        "V-0003",
        "FV/2026/10/0012",
        [("FS-6002", 20, 7_100)],
        invoice_date=date(2026, 9, 30),
        po_refs=[po.id],
    )
    kit.run()
    asked = kit.actions("FV/2026/10/0012", "asked_colleague")
    assert len(asked) == 1 and gr.id in asked[0]["refs"]
    kit.tick(hours=5)
    entry = kit.entry_for(ev.id)
    assert entry is not None and kit.line(entry, "Liabilities:AP").credit_minor == 142_000
    assert not kit.actions("FV/2026/10/0012", "short_pay_notice") and kit.escalations() == []


# ------------------------------------------------------------------ A-cases exercised through the same machinery


def test_a4_uom_mismatch_posts_po_value(kit: Kit) -> None:
    po = kit.po("V-0025", [("FS-6001", 40, 34_870)], order_date=date(2026, 9, 15), plant="CZ")
    kit.receipt(po, 1, 40, when=at(date(2026, 9, 26)))
    ev = kit.invoice(
        "V-0025",
        "20260477",
        [("FS-6001", 2000, 697)],
        invoice_date=date(2026, 9, 30),
        po_refs=[po.id],
        descriptions={"FS-6001": "Hex flange bolt M8x25 (pc)"},
        uoms={"FS-6001": "ks"},
    )
    kit.run()
    entry = kit.entry_for(ev.id)
    assert entry is not None and kit.line(entry, "Liabilities:AP").credit_minor == 1_394_000
    assert kit.escalations() == [] and not kit.actions("20260477", "short_pay_notice")


def test_a5_invoice_covering_two_pos(kit: Kit) -> None:
    po_a = kit.po(
        "V-0013",
        [("WH-5001", 400, 45_800), ("WH-5020", 3000, 2_580)],
        order_date=date(2026, 9, 1),
        plant="CZ",
    )
    po_b = kit.po("V-0013", [("WH-5010", 800, 13_300)], order_date=date(2026, 9, 4), plant="CZ")
    for po, qty in ((po_a, [400, 3000]), (po_b, [800])):
        for n, q in enumerate(qty, start=1):
            kit.receipt(po, n, q, when=at(date(2026, 9, 27)))
    ev = kit.invoice(
        "V-0013",
        "20260520",
        [("WH-5001", 400, 45_800), ("WH-5020", 3000, 2_580), ("WH-5010", 800, 13_300)],
        invoice_date=date(2026, 9, 30),
        po_refs=[po_a.id, po_b.id],
    )
    kit.run()
    entry = kit.entry_for(ev.id)
    assert entry is not None and kit.line(entry, "Liabilities:AP").credit_minor == 36_700_000
    matched = kit.actions("20260520", "matched")[-1]
    assert po_a.id in matched["refs"] and po_b.id in matched["refs"]
    assert len(kit.entries_for(ev.id)) == 1


def test_a6_partial_invoices_against_one_line(kit: Kit) -> None:
    po = kit.po("V-0036", [("SF-1040", 4000, 84)], order_date=date(2026, 9, 10), plant="CZ")
    kit.receipt(po, 1, 1600, when=at(date(2026, 9, 27)))
    first = kit.invoice(
        "V-0036",
        "FV-2026/301",
        [("SF-1040", 1600, 84)],
        invoice_date=date(2026, 9, 29),
        po_refs=[po.id],
    )
    kit.run()
    kit.receipt(po, 1, 2400, when=at(date(2026, 10, 1), 8))
    kit.tick(hours=9)
    second = kit.invoice(
        "V-0036",
        "FV-2026/309",
        [("SF-1040", 2400, 84)],
        invoice_date=date(2026, 10, 1),
        po_refs=[po.id],
    )
    kit.run()
    assert kit.line(kit.entry_for(first.id), "Liabilities:AP").credit_minor == 134_400
    assert kit.line(kit.entry_for(second.id), "Liabilities:AP").credit_minor == 201_600
    assert not kit.actions("FV-2026/309", "duplicate_rejected") and not kit.actions(
        "FV-2026/309", "short_pay_notice"
    )
    assert kit.state.po_line_balance(po.id, 1) == 0


def test_a11_discount_terms_on_the_payable(kit: Kit) -> None:
    po = kit.po("V-0029", [("FS-6010", 10, 593)], order_date=date(2026, 9, 10))
    kit.receipt(po, 1, 10, when=at(date(2026, 9, 25)))
    ev = kit.invoice(
        "V-0029",
        "RE-2026-0510",
        [("FS-6010", 10, 593)],
        invoice_date=date(2026, 9, 25),
        po_refs=[po.id],
    )
    kit.run()
    payable = kit.ledger.payables(vendor_id="V-0029")[0]
    assert (
        payable.discount_bp == 200
        and payable.discount_deadline == date(2026, 10, 5)
        and payable.due_date == date(2026, 10, 25)
    )
    assert kit.entry_for(ev.id) is not None


def test_a13_prepayment_is_recorded_on_the_posting(kit: Kit) -> None:
    po = kit.po("V-0012", [("TL-7001", 1, 4_850_000)], order_date=date(2026, 9, 2))
    kit.receipt(po, 1, 1, when=at(date(2026, 9, 29)))
    ev = kit.invoice(
        "V-0012",
        "RE-2026-0600",
        [("TL-7001", 1, 4_850_000)],
        invoice_date=date(2026, 9, 30),
        po_refs=[po.id],
        prepayment_minor=1_455_000,
    )
    kit.run()
    entry = kit.entry_for(ev.id)
    assert entry is not None and kit.line(entry, "Liabilities:AP").credit_minor == 5_771_500
    assert kit.actions("RE-2026-0600", "posted")[0]["decision"]["prepayment_minor"] == 1_455_000


def test_bank_details_on_an_invoice_are_forwarded(kit: Kit) -> None:
    po = kit.po("V-0001", [("SF-1002", 100, 1390)], order_date=date(2026, 9, 18))
    kit.receipt(po, 1, 100, when=at(date(2026, 9, 25)))
    kit.invoice(
        "V-0001",
        "KT-2300",
        [("SF-1002", 100, 1390)],
        invoice_date=date(2026, 9, 30),
        po_refs=[po.id],
        bank_details={"iban": "CZ6220100000005459621954", "bank": "Fio banka, a.s."},
    )
    kit.run()
    forwarded = [
        e for e in kit.store.query(type="vendor_message") if e.payload["channel"] == "invoice"
    ]
    assert len(forwarded) == 1 and forwarded[0].payload["vendor_id"] == "V-0001"
    assert (
        forwarded[0].payload["bank_details"]["iban"] == "CZ6220100000005459621954"
        and forwarded[0].source == "invoice_agent"
    )
