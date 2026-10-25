from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from agents.parsed import ExtractionFailure, ParsedInvoice, ParsedLine
from api.judge import InvoiceForm, invoice_view, submit_invoice
from core.events import make_event
from sim.worldgen.formats import format_number
from tests.integration import test_judge_invoice_api as judge_tests

AUTH = judge_tests.AUTH
desk = judge_tests.desk
judge_desk = judge_tests.judge_desk
matching_input = judge_tests.matching_input


def test_raw_invoice_uses_actual_extraction_boundary_and_reports_failure(judge_desk, monkeypatch):
    rt, client = judge_desk
    calls = []

    def extract(document, **kwargs):
        calls.append(document)
        assert document.format == "eml" and document.structured is None
        assert "Unreadable custom invoice" in document.text_preview
        return ExtractionFailure(reason="Missing printed totals")

    monkeypatch.setattr(rt.invoice_agent.llm, "extract_invoice", extract)
    response = client.post(
        "/judge/invoice", headers=AUTH, json={"document_text": "Unreadable custom invoice"}
    )
    assert response.status_code == 200, response.text
    result = response.json()
    assert len(calls) == 2
    assert result["invoice_status"] == "held"
    assert result["reason"] == "unreadable_invoice"
    assert result["document_format"] == "eml" and result["extraction_mode"] == "llm"
    assert not result["pay_ids"]
    envelope = rt.store.get(result["event_id"])
    assert Path(envelope.payload["document_path"]).suffix == ".eml"


def test_live_submission_is_scoped_idempotent_and_readable_while_runner_busy(judge_desk):
    rt, _ = judge_desk
    form = matching_input(rt)
    backlog = rt.store.append(
        make_event(
            "invoice_received",
            {
                "invoice_id": "UNRELATED-BACKLOG",
                "format": "structured",
                "document_path": "/not-a-real-invoice.json",
                "received_via": "judge",
                "sender": "backlog@example.invalid",
                "supplier_hint": None,
                "attachments": [],
                "size_bytes": 1,
                "sha256": "0" * 64,
                "pages": None,
            },
            event_time=rt.clock.sim_now,
            source="test",
        )
    ).event
    identity = "LIVE-" + "a" * 32
    activities = []
    result = submit_invoice(
        rt, InvoiceForm(**form), invoice_id=identity, progress=activities.append
    )
    assert result["status"] == "executed", result
    assert result["extraction_mode"] == "structured_mapper"
    assert not rt.store.is_handled("invoice_agent", backlog.id)
    before = rt.store.latest_seq()
    cash = rt.bank.balance("EUR")
    assert (
        submit_invoice(rt, InvoiceForm(**form), invoice_id=identity)["pay_ids"] == result["pay_ids"]
    )
    assert rt.store.latest_seq() == before and rt.bank.balance("EUR") == cash
    assert {a["agent"] for a in activities} >= {"invoice_agent", "payment_agent"}
    with ThreadPoolExecutor(max_workers=1) as pool, rt.runner._lock:
        assert pool.submit(invoice_view, rt, identity).result(timeout=2)["status"] == "executed"


def test_example_uses_existing_evidence_and_creates_nothing(judge_desk):
    rt, client = judge_desk
    assert client.get("/judge/invoice/example").status_code == 409
    form = matching_input(rt)
    before = rt.store.latest_seq()
    first = client.get("/judge/invoice/example")
    assert first.status_code == 200, first.text
    example = first.json()
    second = client.get("/judge/invoice/example").json()
    assert example["invoice"]["invoice_number"] != second["invoice"]["invoice_number"]
    assert rt.store.latest_seq() == before
    assert example["purchase_order_id"] == form["po_reference"]
    assert example["receipt_ids"] == ["GR-8801"]
    assert not example["creates_purchase_orders_or_receipts"]
    assert all(rt.store.get(identity) for identity in example["evidence_event_ids"])
    assert {rt.store.get(identity).type for identity in example["evidence_event_ids"]} == {
        "po_created",
        "goods_received",
    }
    assert example["purchase_order_id"] in example["document_text"]
    paid = client.post("/judge/invoice", json=example["invoice"], headers=AUTH)
    assert paid.status_code == 200 and paid.json()["status"] == "executed", paid.text


def test_raw_example_fresh_extraction_reaches_payment(judge_desk, monkeypatch):
    rt, client = judge_desk
    matching_input(rt)
    example = client.get("/judge/invoice/example").json()
    form = example["invoice"]
    supplier = next(s for s in rt.suppliers if s.id == form["supplier_id"])
    calls = []

    def amount(value):
        return format_number(value, "EUR", "1,234.56")

    def extract(document, **kwargs):
        calls.append(document.invoice_id)
        assert example["document_text"] in document.text_preview
        assert document.structured is None
        return ParsedInvoice(
            supplier_name=supplier.legal_name,
            supplier_vat_id=supplier.vat_id,
            invoice_number=form["invoice_number"],
            invoice_date=rt.clock.sim_now.date().isoformat(),
            due_date=rt.clock.sim_now.date().isoformat(),
            po_references=[form["po_reference"]],
            currency="EUR",
            lines=[
                ParsedLine(
                    description=form["body"],
                    sku=form["sku"],
                    qty=str(form["quantity"]),
                    uom="pc",
                    unit_price=amount(form["unit_price_minor"]),
                    line_total=amount(form["net_minor"]),
                )
            ],
            net=amount(form["net_minor"]),
            vat_amount=amount(form["vat_minor"]),
            gross=amount(form["net_minor"] + form["vat_minor"]),
            document_language="en",
        )

    monkeypatch.setattr(rt.invoice_agent.llm, "extract_invoice", extract)
    response = client.post(
        "/judge/invoice", json={"document_text": example["document_text"]}, headers=AUTH
    )
    assert response.status_code == 200, response.text
    result = response.json()
    assert result["status"] == "executed", result
    assert result["extraction_mode"] == "llm"
    assert calls == [result["invoice_id"]]
    assert len(result["journal_entries"]) == 2


def test_evidence_follows_bank_message_without_crossing_shared_vendor(judge_desk):
    rt, client = judge_desk
    form = matching_input(rt)
    unrelated = rt.store.append(
        make_event(
            "agent_action",
            {
                "agent": "vendor_agent",
                "action_type": "message_classified",
                "rule_id": None,
                "refs": [form["supplier_id"], form["po_reference"]],
                "inputs": {},
                "decision": {},
                "explanation": "A separate invoice from the same vendor",
            },
            event_time=rt.clock.sim_now,
            source="test",
        )
    ).event
    response = client.post(
        "/judge/invoice",
        json={**form, "bank_account": "DE02120300000000202051", "request_payment": False},
        headers=AUTH,
    )
    assert response.status_code == 200, response.text
    events = response.json()["evidence_events"]
    assert unrelated.id not in {e["id"] for e in events}
    assert any(e["type"] == "vendor_message" for e in events)
    assert any(
        e["type"] == "agent_action"
        and e["payload"]["agent"] == "vendor_agent"
        and e["payload"]["action_type"] == "message_classified"
        for e in events
    )


def test_payment_task_failure_is_visible_in_invoice_result(judge_desk, monkeypatch):
    rt, _ = judge_desk
    form = matching_input(rt)
    cash = rt.bank.balance("EUR")

    def fail_payment(*args, **kwargs):
        raise RuntimeError("signer transport disconnected")

    monkeypatch.setattr(rt.payment_agent, "_pay", fail_payment)
    progress = []
    result = submit_invoice(rt, InvoiceForm(**form), progress=progress.append)
    assert result["invoice_status"] == "posted"
    assert not result["pay_ids"] and rt.bank.balance("EUR") == cash
    assert "signer transport disconnected" in result["processing_error"]
    errors = [e for e in result["evidence_events"] if e["type"] == "system_error"]
    assert len(errors) == 1 and result["invoice_id"] in errors[0]["payload"]["refs"]
    assert progress[-1]["agent"] == "payment_agent" and progress[-1]["state"] == "failed"
    assert invoice_view(rt, result["invoice_id"])["processing_error"] == result["processing_error"]
