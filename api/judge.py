"""Live invoices enter the ordinary document extraction and checking pipeline.

No purchase order, receipt, vendor approval or signer outcome is invented here.
An invoice can pass, be held, or reach the signer depending on the recorded facts.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
from collections.abc import Callable
from datetime import date
from email.message import EmailMessage
from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict, Field, model_validator

from agents.payment_agent import Group
from agents.runtime import Runtime
from core.events import make_event
from core.rules import applicable_price_list, is_duplicate
from core.tax import expected_vat_minor, treatment_for_country
from sim.worldgen.formats import format_date, format_number, format_qty


class InvoiceForm(BaseModel):
    model_config = ConfigDict(extra="forbid")
    supplier_id: str | None = Field(default=None, max_length=80)
    supplier_name: str | None = Field(default=None, max_length=200)
    document_text: str | None = Field(default=None, min_length=1, max_length=20000)
    invoice_number: str | None = Field(default=None, min_length=1, max_length=120)
    po_reference: str | None = Field(default=None, max_length=160)
    currency: Literal["EUR", "PLN", "CZK", "JPY", "USD"] = "EUR"
    net_minor: int | None = Field(default=None, gt=0, lt=2**53, strict=True)
    vat_minor: int = Field(default=0, ge=0, lt=2**53, strict=True)
    gross_minor: int | None = Field(default=None, gt=0, lt=2**53, strict=True)
    sku: str | None = Field(default=None, max_length=120)
    quantity: int = Field(default=1, ge=1, le=1_000_000, strict=True)
    unit_price_minor: int | None = Field(default=None, gt=0, lt=2**53, strict=True)
    bank_account: str | None = Field(default=None, max_length=100)
    body: str | None = Field(default=None, max_length=20000)
    submitted_by: str | None = Field(default=None, max_length=120)
    request_payment: bool = True

    @model_validator(mode="after")
    def coherent_input(self) -> InvoiceForm:
        if self.document_text is not None:
            if not self.document_text.strip():
                raise ValueError("document_text cannot be blank")
            return self
        if not (self.supplier_id or (self.supplier_name and self.supplier_name.strip())):
            raise ValueError("supplier_id or supplier_name is required")
        if not self.invoice_number or not self.invoice_number.strip():
            raise ValueError("invoice_number cannot be blank")
        if self.net_minor is None:
            raise ValueError("net_minor is required for a structured invoice")
        if self.quantity != 1 and self.unit_price_minor is None:
            raise ValueError("unit_price_minor is required when quantity differs from one")
        if self.gross_minor is None and self.net_minor + self.vat_minor >= 2**53:
            raise ValueError("computed gross must be below 2**53 minor units")
        return self


def invoice_view(rt: Runtime, invoice_id: str) -> dict[str, Any]:
    received = next(
        (
            e
            for e in rt.store.query(type="invoice_received", limit=1_000_000)
            if e.payload["invoice_id"] == invoice_id and e.source == "judge"
        ),
        None,
    )
    if received is None:
        raise HTTPException(404, "No live invoice with that identity")
    invoice = rt.invoice_state.invoice(invoice_id)
    payables = [p for p in rt.ledger.payables() if p.inv_id == invoice_id]
    payable_ids = {p.id for p in payables}
    payments = [p for p in rt.payment_state.payments() if payable_ids.intersection(p.payable_ids)]
    packet_ids = {p.packet_id for p in payments if p.packet_id}
    refs = {invoice_id, received.id, *payable_ids, *(p.pay_id for p in payments), *packet_ids}
    recorded = rt.store.query(limit=1_000_000)
    # Follow only identifiers owned by this submission. Shared vendor, PO and
    # receipt references must not pull another invoice into this live run.
    own_threads = {invoice_id, f"inv-bank-{invoice_id}"}
    roots = [
        e
        for e in recorded
        if e.type == "vendor_message"
        and e.source == "invoice_agent"
        and e.payload.get("channel") == "invoice"
        and e.payload.get("thread_id") in own_threads
    ]
    refs.update(e.id for e in roots)
    unique_threads = {
        thread
        for thread in own_threads
        if sum(
            e.type == "vendor_message"
            and e.payload.get("thread_id") == thread
            and not e.payload.get("in_reply_to")
            for e in recorded
        )
        == 1
    }
    matched = set()
    while True:
        before = len(matched)
        for event in recorded:
            payload = event.payload
            if not (
                event.id in refs
                or payload.get("invoice_id") == invoice_id
                or payload.get("pay_id") in refs
                or payload.get("packet_id") in refs
                or payload.get("in_reply_to") in refs
                or payload.get("thread_id") in refs
                or refs.intersection(str(r) for r in payload.get("refs", []))
            ):
                continue
            matched.add(event.id)
            refs.add(event.id)
            if event.type == "escalation":
                packet_ids.add(payload["packet_id"])
                refs.add(payload["packet_id"])
            if event.type == "vendor_message":
                refs.add(payload["message_id"])
                if payload.get("thread_id") in unique_threads:
                    refs.add(payload["thread_id"])
        if len(matched) == before:
            break
    events = [event for event in recorded if event.id in matched]
    entries = [e for e in rt.ledger.entries() if e.cause_id in refs]
    payment = payments[-1] if payments else None
    extraction = next(
        (
            e.payload.get("inputs", {}).get("path")
            for e in events
            if e.type == "agent_action"
            and e.payload.get("agent") == "invoice_agent"
            and e.payload.get("inputs", {}).get("path") in ("llm", "structured_mapper")
        ),
        None,
    )
    processing_error = next(
        (
            f"{e.payload['component']}: {e.payload['code']}: {e.payload['message']}"
            for e in events
            if e.type == "system_error"
        ),
        None,
    )
    return {
        "invoice_id": invoice_id,
        "event_id": received.id,
        "status": payment.status if payment else invoice.status if invoice else "received",
        "invoice_status": invoice.status if invoice else "received",
        "reason": payment.held_reason if payment else invoice.reason if invoice else None,
        "rule_id": invoice.rule_id if invoice else None,
        "payable_ids": sorted(payable_ids),
        "pay_ids": [p.pay_id for p in payments],
        "packet_ids": sorted(packet_ids),
        "signer_kind": rt.signer.kind,
        "bank_mode": "simulated",
        "document_format": received.payload["format"],
        "extraction_mode": extraction,
        **({"processing_error": processing_error} if processing_error else {}),
        "evidence_events": [e.model_dump(mode="json") for e in events],
        "journal_entries": [
            {
                "id": e.id,
                "cause_id": e.cause_id,
                "description": e.description,
                "lines": [
                    {
                        "account": ln.account,
                        "currency": ln.currency,
                        "debit_minor": ln.debit_minor,
                        "credit_minor": ln.credit_minor,
                    }
                    for ln in e.lines
                ],
            }
            for e in entries
        ],
        "notice": "This is the desk's observed result for your submitted invoice. Arbitrary fraud is not guaranteed to be detected; only recorded checks and signed FPGA responses establish what happened.",
    }


def submit_invoice(
    runtime: Runtime,
    form: InvoiceForm,
    *,
    invoice_id: str | None = None,
    progress: Callable[[dict[str, Any]], None] | None = None,
) -> dict[str, Any]:
    with runtime.runner._lock:
        if runtime.invoice_agent is None:
            raise HTTPException(503, "The invoice agent is not enabled")
        if form.request_payment and runtime.payment_agent is None:
            raise HTTPException(
                503,
                "The payment agent is not enabled; set request_payment=false to submit only",
            )
        supplier = next((s for s in runtime.suppliers if s.id == form.supplier_id), None)
        if form.supplier_id and supplier is None:
            raise HTTPException(
                422, "Unknown supplier_id; use supplier_name alone to submit an unknown vendor"
            )
        name = form.supplier_name or (supplier.legal_name if supplier else None)
        locale = supplier.number_locale if supplier else "1,234.56"
        date_format = supplier.date_format if supplier else "YYYY-MM-DD"
        stamp = runtime.clock.sim_now
        invoice_id = invoice_id or "JUDGE-" + secrets.token_hex(10).upper()
        if not re.fullmatch(r"(?:JUDGE|LIVE)-[0-9a-fA-F]{20,32}", invoice_id):
            raise ValueError("Invalid live invoice identity")
        if any(
            e.payload["invoice_id"] == invoice_id
            for e in runtime.store.query(type="invoice_received", limit=1_000_000)
        ):
            return invoice_view(runtime, invoice_id)

        def number(minor: int) -> str:
            return format_number(minor, form.currency, locale)

        if form.document_text is not None:
            message = EmailMessage()
            message["From"] = "demo.sender@live-demo.invalid"
            message["To"] = "accounts-payable@payguard.invalid"
            message["Subject"] = "Incoming invoice " + invoice_id
            message.set_content(form.document_text)
            raw, document_format, extension = message.as_bytes(), "eml", ".eml"
        else:
            assert form.net_minor is not None
            document = {
                "supplier": {"name": name, "vat_id": supplier.vat_id if supplier else None},
                "invoice_number": form.invoice_number,
                "invoice_date": format_date(stamp.date(), date_format),
                "due_date": format_date(stamp.date(), date_format),
                "po_reference": form.po_reference,
                "currency": form.currency,
                "net": number(form.net_minor),
                "vat": number(form.vat_minor),
                "gross": number(
                    form.gross_minor
                    if form.gross_minor is not None
                    else form.net_minor + form.vat_minor
                ),
                "lines": [
                    {
                        "description": form.body or "Live test invoice line",
                        "sku": form.sku,
                        "qty": format_qty(form.quantity, locale),
                        "uom": "pc",
                        "unit_price": number(
                            form.unit_price_minor
                            if form.unit_price_minor is not None
                            else form.net_minor
                        ),
                        "line_total": number(form.net_minor),
                    }
                ],
                "bank": {"iban": form.bank_account} if form.bank_account else None,
                "notes": form.body or "Live invoice submission; no asserted outcome.",
            }
            raw = json.dumps(document, ensure_ascii=False, indent=2).encode()
            document_format, extension = "structured", ".json"
        folder = runtime.out_dir / "judge-invoices"
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / (invoice_id + extension)
        with path.open("xb") as target:
            target.write(raw)
            target.flush()
            os.fsync(target.fileno())
        event = runtime.store.append(
            make_event(
                "invoice_received",
                {
                    "invoice_id": invoice_id,
                    "format": document_format,
                    "document_path": str(path.resolve()),
                    "received_via": "judge",
                    "sender": "demo.sender@live-demo.invalid",
                    "supplier_hint": form.supplier_id,
                    "attachments": [],
                    "size_bytes": len(raw),
                    "sha256": hashlib.sha256(raw).hexdigest(),
                    "pages": None,
                },
                event_time=stamp,
                source="judge",
                actor=form.submitted_by or "demo_operator",
            )
        ).event
        runtime.runner.drain_events([event], progress=progress)
        payables = [
            p for p in runtime.ledger.payables() if p.inv_id == invoice_id and p.status == "open"
        ]
        if form.request_payment and payables:
            group = Group(
                vendor_id=payables[0].vendor_id,
                currency=payables[0].currency,
                payables=payables,
            )
            runtime.runner.run_task(
                runtime.payment_agent,
                lambda ctx: runtime.payment_agent._pay(group, ctx, today=stamp.date()),
                scoped=True,
                progress=progress,
                refs=[invoice_id],
            )
        result = invoice_view(runtime, invoice_id)
        assert result["event_id"] == event.id
        return result


def example_invoice(rt: Runtime) -> dict[str, Any]:
    """Draft a new invoice from already recorded, unconsumed purchasing evidence."""
    agent = rt.invoice_agent
    if agent is None:
        raise HTTPException(503, "The invoice agent is not enabled")
    now = rt.clock.sim_now
    number = "LIVE-" + secrets.token_hex(5).upper()
    paid_vendors = {
        p.vendor_id for p in rt.payment_state.payments() if p.status in ("executed", "settled")
    }
    suppliers = sorted(rt.suppliers, key=lambda s: (s.id in paid_vendors, s.id))
    for supplier in suppliers:
        account = rt.vendors.active_account(supplier.id)
        prices, _ = applicable_price_list(supplier.price_lists, now.date())
        if (
            supplier.currency != "EUR"
            or not account
            or not account.on_chip
            or account.hold_until > now
            or prices is None
        ):
            continue
        for po in agent.pos_for_vendor(supplier.id):
            if po.currency != "EUR" or po.order_date > now.date():
                continue
            for line in sorted(po.lines, key=lambda item: item.unit_price_minor):
                if line.uom != "pc" or line.unit_price_minor <= 0:
                    continue
                receipts = agent.receipts_for(po.id, line.line_no)
                used = rt.invoice_state.qty_posted(po.id, line.line_no)
                available = min(line.qty, sum(r.qty_po_units for r in receipts)) - used
                if available <= 0:
                    continue
                # Choose a small genuine remaining quantity that is not already
                # represented by the ordinary near-duplicate rule.
                for quantity in range(1, min(available, 20) + 1):
                    net = quantity * line.unit_price_minor
                    vat = expected_vat_minor(net, treatment_for_country(supplier.country))
                    duplicate = False
                    for row in rt.invoice_state.invoices(vendor_id=supplier.id):
                        facts = rt.invoice_state.facts(row.invoice_id)
                        other = (facts or {}).get("validated")
                        if not other or row.status == "rejected" or other.get("is_credit_note"):
                            continue
                        if is_duplicate(
                            net + vat,
                            now.date(),
                            number,
                            other["gross_minor"],
                            date.fromisoformat(other["invoice_date"]),
                            other["invoice_number"],
                        ):
                            duplicate = True
                            break
                    if duplicate:
                        continue
                    form = {
                        "supplier_id": supplier.id,
                        "invoice_number": number,
                        "po_reference": po.id,
                        "currency": "EUR",
                        "net_minor": net,
                        "vat_minor": vat,
                        "sku": line.sku,
                        "quantity": quantity,
                        "unit_price_minor": line.unit_price_minor,
                        "body": line.description,
                        "request_payment": True,
                    }

                    def amount(value: int) -> str:
                        # This editable draft is written in English. Its printed
                        # amounts must agree with the language seen by extraction.
                        return format_number(value, "EUR", "1,234.56")

                    text = (
                        f"Supplier: {supplier.legal_name}\nVAT ID: {supplier.vat_id}\n"
                        f"Invoice number: {number}\n"
                        f"Invoice date: {now.date().isoformat()}\n"
                        f"Due date: {now.date().isoformat()}\n"
                        f"Purchase order: {po.id}\nCurrency: EUR\n"
                        f"Item: {line.description}\nSKU: {line.sku}\n"
                        f"Quantity: {quantity} pc\nUnit price: {amount(line.unit_price_minor)}\n"
                        f"Net amount: {amount(net)}\nVAT amount: {amount(vat)}\n"
                        f"Total: {amount(net + vat)}\n"
                        "Please pay the vendor account already on file. No bank change is requested."
                    )
                    return {
                        "document_text": text,
                        "invoice": form,
                        "purchase_order_id": po.id,
                        "receipt_ids": [r.gr.id for r in receipts if r.qty_po_units > 0],
                        "evidence_event_ids": [
                            e.id
                            for e in rt.store.query(type="po_created", limit=1_000_000)
                            if e.payload["po"]["id"] == po.id
                        ]
                        + [r.event_id for r in receipts if r.qty_po_units > 0],
                        "available_quantity": available,
                        "creates_purchase_orders_or_receipts": False,
                        "notice": "Drafted from existing purchase-order and receipt records. Processing will check the invoice and request payment only if it passes; no outcome is preset.",
                    }
    raise HTTPException(
        409,
        "No received, unconsumed EUR purchase-order line is available yet. Advance the dataset until a purchase order and its receipt have arrived, or enter an invoice manually. No supporting evidence was created.",
    )


def create_judge_router(runtime: Runtime, *, require_controller: Callable) -> APIRouter:
    router = APIRouter(tags=["judge-invoices"])

    @router.get("/judge/invoice/options")
    def options() -> dict[str, Any]:
        return {
            "suppliers": [
                {"id": s.id, "name": s.legal_name, "currency": s.currency}
                for s in runtime.suppliers
            ],
            "purchase_orders": [
                e.payload["po"] for e in runtime.store.query(type="po_created", limit=10000)
            ],
            "amount_unit": "integer minor units; JPY has zero decimal places, others two",
            "creates_purchase_orders_or_receipts": False,
        }

    @router.get("/judge/invoice/example")
    def example() -> dict[str, Any]:
        return example_invoice(runtime)

    @router.get("/judge/invoice/{invoice_id}")
    def invoice_state(invoice_id: str) -> dict[str, Any]:
        return invoice_view(runtime, invoice_id)

    @router.post("/judge/invoice", dependencies=[Depends(require_controller)])
    def inject(form: InvoiceForm) -> dict[str, Any]:
        return submit_invoice(runtime, form)

    return router
