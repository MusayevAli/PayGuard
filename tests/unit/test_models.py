from __future__ import annotations

import json
from datetime import UTC, date, datetime

import pytest
from pydantic import ValidationError

from sim.models import (
    EVENT_TYPES,
    PAYLOAD_MODELS,
    AgentAction,
    AnswerKey,
    AnswerKeyEntry,
    BankAccount,
    Event,
    Invoice,
    InvoiceFormat,
    InvoiceReceived,
    Person,
    Supplier,
)

T0 = datetime(2026, 10, 1, 8, 0, tzinfo=UTC)
T1 = datetime(2026, 10, 1, 9, 0, tzinfo=UTC)


def day_boundary_payload() -> dict:
    return {"date": "2026-10-01", "day_index": 0, "previous_date": None, "month_closed": None}


def test_every_spec_event_type_has_a_payload_model() -> None:
    expected = {
        "po_created",
        "po_amended",
        "goods_received",
        "invoice_received",
        "vendor_message",
        "colleague_reply",
        "bank_statement",
        "bank_statement_line",
        "controller_decision",
        "agent_action",
        "escalation",
        "payment_requested",
        "chip_response",
        "payment_executed",
        "bank_rejected",
        "provisioning",
        "day_boundary",
        "judge_injection",
        "system_error",
        "score_update",
    }
    assert set(EVENT_TYPES) == expected
    assert set(PAYLOAD_MODELS) == expected


def test_event_requires_known_at_not_before_event_time() -> None:
    ok = Event(
        id="ev_000001",
        type="day_boundary",
        event_time=T0,
        known_at=T1,
        source="clock",
        actor=None,
        payload=day_boundary_payload(),
    )
    assert ok.known_at >= ok.event_time
    with pytest.raises(ValidationError, match="known_at"):
        Event(
            id="ev_000002",
            type="day_boundary",
            event_time=T1,
            known_at=T0,
            source="clock",
            actor=None,
            payload=day_boundary_payload(),
        )


def test_event_rejects_naive_datetimes_and_unknown_types() -> None:
    naive = datetime(2026, 10, 1, 8, 0)
    with pytest.raises(ValidationError):
        Event(
            id="ev_000003",
            type="day_boundary",
            event_time=naive,
            known_at=naive,
            source="clock",
            actor=None,
            payload=day_boundary_payload(),
        )
    with pytest.raises(ValidationError):
        Event(
            id="ev_000004",
            type="not_a_type",
            event_time=T0,
            known_at=T0,
            source="x",
            actor=None,
            payload={},
        )


def test_event_payload_is_validated_against_the_type_model() -> None:
    with pytest.raises(ValidationError):
        Event(
            id="ev_000005",
            type="day_boundary",
            event_time=T0,
            known_at=T0,
            source="clock",
            actor=None,
            payload={"bogus": 1},
        )
    ev = Event(
        id="ev_000006",
        type="day_boundary",
        event_time=T0,
        known_at=T0,
        source="clock",
        actor=None,
        payload=day_boundary_payload(),
    )
    typed = ev.typed_payload()
    assert typed.date == date(2026, 10, 1)


def test_event_json_round_trip_is_stable() -> None:
    ev = Event(
        id="ev_000007",
        type="agent_action",
        event_time=T0,
        known_at=T0,
        source="agent",
        actor="invoice_agent",
        payload=AgentAction(
            agent="invoice_agent",
            action_type="posted",
            refs=["KT-2291"],
            rule_id="R1",
            inputs={"qty": 800},
            decision={"post_minor": 1112000},
            explanation="Posted 800 of 1,000.",
        ).model_dump(mode="json"),
    )
    text = ev.model_dump_json()
    again = Event.model_validate(json.loads(text))
    assert again == ev
    assert again.model_dump_json() == text
    assert '"event_time":"2026-10-01T08:00:00Z"' in text


def test_agent_action_type_is_an_enum_of_spec_values() -> None:
    with pytest.raises(ValidationError):
        AgentAction(
            agent="a",
            action_type="did_something",
            refs=[],
            rule_id=None,
            inputs={},
            decision={},
            explanation="",
        )


def test_invoice_format_enum_and_received_payload() -> None:
    assert {f.value for f in InvoiceFormat} == {"structured", "pdf", "png", "eml"}
    with pytest.raises(ValidationError):
        InvoiceReceived(
            invoice_id="INV-0001",
            format="docx",
            document_path="x",
            received_via="email",
            sender=None,
            supplier_hint=None,
            attachments=[],
            size_bytes=1,
            sha256="0" * 64,
        )
    ok = InvoiceReceived(
        invoice_id="INV-0001",
        format="png",
        document_path="invoices/INV-0001.png",
        received_via="scan",
        sender=None,
        supplier_hint=None,
        attachments=[],
        size_bytes=1,
        sha256="0" * 64,
    )
    assert ok.format is InvoiceFormat.PNG


def test_supplier_constraints() -> None:
    base = dict(
        id="V-0001",
        legal_name="Kovak Trim s.r.o.",
        country="CZ",
        currency="EUR",
        vat_id="CZ25123891",
        terms="net30",
        purchasing_contact="petra.vlcek",
        onboarded_at="2018-03-12",
        invoice_template_id="cz_faktura_classic",
        language="cs",
        number_locale="1 234,56",
        date_format="DD.MM.YYYY",
        email="fakturace@kovaktrim.cz",
        domain="kovaktrim.cz",
        phone="+420 485 100 200",
        category="trim fabric",
        registration_id="25123891",
        address={
            "street": "Průmyslová 12",
            "city": "Liberec",
            "postal_code": "460 01",
            "country": "CZ",
        },
        bank_account={
            "kind": "iban",
            "iban": "CZ6508000000192000145399",
            "bic": "GIBACZPX",
            "bank_name": "Česká spořitelna, a.s.",
            "account_number": None,
            "routing_number": None,
            "bank_code": "0800",
            "branch_code": None,
        },
        price_lists=[],
    )
    ok = Supplier(**base, name_variants=["Kovak Trim", "KOVAK TRIM s.r.o.", "Kovak-Trim sro"])
    assert ok.iban == "CZ6508000000192000145399"
    with pytest.raises(ValidationError):
        Supplier(**base, name_variants=["only", "two"])
    with pytest.raises(ValidationError):
        Supplier(**base, name_variants=["a", "b", "c", "d", "e", "f"])
    with pytest.raises(ValidationError):
        Supplier(**{**base, "terms": "net60"}, name_variants=["a", "b", "c"])
    with pytest.raises(ValidationError):
        Supplier(**{**base, "number_locale": "1'234.56"}, name_variants=["a", "b", "c"])


def test_bank_account_local_formats() -> None:
    us = BankAccount(
        kind="us_aba",
        iban=None,
        bic=None,
        bank_name="First Michigan Bank",
        account_number="4471002983",
        routing_number="111000025",
        bank_code=None,
        branch_code=None,
    )
    assert us.iban is None
    with pytest.raises(ValidationError):
        BankAccount(
            kind="iban",
            iban=None,
            bic=None,
            bank_name="x",
            account_number=None,
            routing_number=None,
            bank_code=None,
            branch_code=None,
        )


def test_invoice_model_minimal(tmp_path) -> None:
    inv = Invoice(
        id="INV-0001",
        supplier_id="V-0001",
        supplier_name_as_written="Kovak Trim s.r.o.",
        invoice_number="KT-2291",
        kind="invoice",
        format="pdf",
        document_path="invoices/INV-0001.pdf",
        template_id="cz_faktura_classic",
        language="cs",
        currency="EUR",
        number_locale="1 234,56",
        date_format="DD.MM.YYYY",
        invoice_date="2026-10-10",
        due_date="2026-11-09",
        service_date="2026-10-08",
        po_refs_as_written=["PO-10234"],
        po_ids=["PO-10234"],
        lines=[
            {
                "line_no": 1,
                "sku": "SF-1001",
                "description": "Seat frame front left",
                "qty": 1000,
                "uom": "pc",
                "unit_price_minor": 1390,
                "net_minor": 1390000,
                "vat_rate_bp": 0,
                "vat_minor": 0,
                "gross_minor": 1390000,
                "po_id": "PO-10234",
                "po_line_no": 1,
                "gr_ids": ["GR-4471"],
            }
        ],
        net_minor=1390000,
        vat_minor=0,
        gross_minor=1390000,
        vat_treatment="reverse_charge",
        vat_note="Reverse charge",
        terms="net30",
        discount_bp=0,
        discount_deadline=None,
        retention_minor=0,
        bank_details=None,
        credit_note_for=None,
        received_via="portal",
        attachments=[],
        case_tags=["T1"],
        sha256="0" * 64,
    )
    assert inv.format is InvoiceFormat.PDF
    assert inv.gross_minor == 1390000


def test_person_model() -> None:
    p = Person(
        id="petra.vlcek",
        name="Petra Vlček",
        role="purchasing",
        email="petra.vlcek@tbeu.example",
        phone="+420 485 100 001",
        plant="CZ",
        mailbox_compromised_from="2026-10-14",
        active=True,
        left_at=None,
        persona="Busy purchasing lead for CZ suppliers.",
        script={
            "email_reply_when_compromised": "Confirmed, they told me last week.",
            "phone_reply": "No. Nobody told me about a bank change.",
        },
    )
    assert p.mailbox_compromised_from == date(2026, 10, 14)


def test_answer_key_checks_are_discriminated() -> None:
    entry = AnswerKeyEntry(
        id="T1",
        tier="headline",
        case="T1",
        title="Short delivery",
        expected_outcome="R1",
        injected_event_ids=["ev_000412"],
        refs=["KT-2291"],
        checks=[
            {
                "kind": "journal_amount",
                "entry_match": {"evidence_contains": "ev_000412"},
                "account": "Liabilities:AP",
                "credit_minor": 1112000,
            },
            {
                "kind": "event_exists",
                "type": "agent_action",
                "where": {"action_type": "short_pay_notice", "refs_contains": "KT-2291"},
            },
            {"kind": "no_event", "type": "escalation", "where": {"refs_contains": "KT-2291"}},
        ],
    )
    assert [c.kind for c in entry.checks] == ["journal_amount", "event_exists", "no_event"]
    with pytest.raises(ValidationError):
        AnswerKeyEntry(
            id="X",
            tier="ambient",
            case="A1",
            title="t",
            expected_outcome="o",
            injected_event_ids=["ev_1"],
            refs=[],
            checks=[{"kind": "nope"}],
        )
    with pytest.raises(ValidationError):
        AnswerKeyEntry(
            id="X",
            tier="ambient",
            case="A1",
            title="t",
            expected_outcome="o",
            injected_event_ids=[],
            refs=[],
            checks=[],
        )
    key = AnswerKey(seed=42, headline=[entry], ambient=[])
    assert key.headline[0].id == "T1"
