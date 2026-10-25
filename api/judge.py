"""Live judge-written invoices enter the ordinary structured-document pipeline.

No purchase order, receipt, vendor approval or signer outcome is invented here.
An invoice can pass, be held, or reach the signer depending on the recorded facts.
"""

from __future__ import annotations

import hashlib
import json
import os
import secrets
from collections.abc import Callable
from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict, Field, model_validator

from agents.payment_agent import Group
from agents.runtime import Runtime
from core.events import make_event
from sim.worldgen.formats import format_date, format_number, format_qty


class InvoiceForm(BaseModel):
    model_config = ConfigDict(extra="forbid")
    supplier_id: str | None = Field(default=None, max_length=80)
    supplier_name: str | None = Field(default=None, max_length=200)
    invoice_number: str = Field(min_length=1, max_length=120)
    po_reference: str | None = Field(default=None, max_length=160)
    currency: Literal["EUR", "PLN", "CZK", "JPY", "USD"] = "EUR"
    net_minor: int = Field(gt=0, lt=2**53, strict=True)
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
        if not (self.supplier_id or (self.supplier_name and self.supplier_name.strip())):
            raise ValueError("supplier_id or supplier_name is required")
        if not self.invoice_number.strip():
            raise ValueError("invoice_number cannot be blank")
        if self.quantity != 1 and self.unit_price_minor is None:
            raise ValueError("unit_price_minor is required when quantity differs from one")
        if self.gross_minor is None and self.net_minor + self.vat_minor >= 2**53:
            raise ValueError("computed gross must be below 2**53 minor units")
        return self


def _view(rt: Runtime, invoice_id: str) -> dict[str, Any]:
    received = next(
        (
            e
            for e in rt.store.query(type="invoice_received", limit=1_000_000)
            if e.payload["invoice_id"] == invoice_id and e.source == "judge"
        ),
        None,
    )
    if received is None:
        raise HTTPException(404, "No judge invoice with that identity")
    invoice = rt.invoice_state.invoice(invoice_id)
    payables = [p for p in rt.ledger.payables() if p.inv_id == invoice_id]
    payable_ids = {p.id for p in payables}
    payments = [p for p in rt.payment_state.payments() if payable_ids.intersection(p.payable_ids)]
    refs = {invoice_id, received.id, *payable_ids, *(p.pay_id for p in payments)}
    events = [
        e
        for e in rt.store.query(limit=1_000_000)
        if e.id in refs
        or e.payload.get("invoice_id") == invoice_id
        or e.payload.get("pay_id") in refs
        or refs.intersection(str(r) for r in e.payload.get("refs", []))
    ]
    packet_ids = {e.payload["packet_id"] for e in events if e.type == "escalation"}
    packet_ids.update(p.packet_id for p in payments if p.packet_id)
    refs.update(packet_ids)
    entries = [e for e in rt.ledger.entries() if e.cause_id in refs]
    payment = payments[-1] if payments else None
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

    @router.get("/judge/invoice/{invoice_id}")
    def invoice_state(invoice_id: str) -> dict[str, Any]:
        with runtime.runner._lock:
            return _view(runtime, invoice_id)

    @router.post("/judge/invoice", dependencies=[Depends(require_controller)])
    def inject(form: InvoiceForm) -> dict[str, Any]:
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
            name = form.supplier_name or supplier.legal_name
            locale = supplier.number_locale if supplier else "1,234.56"
            date_format = supplier.date_format if supplier else "YYYY-MM-DD"
            stamp = runtime.clock.sim_now
            invoice_id = "JUDGE-" + secrets.token_hex(10).upper()

            def number(minor: int) -> str:
                return format_number(minor, form.currency, locale)

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
                        "description": form.body or "Judge-submitted invoice line",
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
                "notes": form.body or "Live judge submission; no asserted truth label.",
            }
            raw = json.dumps(document, ensure_ascii=False, indent=2).encode()
            folder = runtime.out_dir / "judge-invoices"
            folder.mkdir(parents=True, exist_ok=True)
            path = folder / (invoice_id + ".json")
            with path.open("xb") as target:
                target.write(raw)
                target.flush()
                os.fsync(target.fileno())
            event = runtime.store.append(
                make_event(
                    "invoice_received",
                    {
                        "invoice_id": invoice_id,
                        "format": "structured",
                        "document_path": str(path.resolve()),
                        "received_via": "judge",
                        "sender": "judge@live-demo.invalid",
                        "supplier_hint": form.supplier_id,
                        "attachments": [],
                        "size_bytes": len(raw),
                        "sha256": hashlib.sha256(raw).hexdigest(),
                        "pages": None,
                    },
                    event_time=stamp,
                    source="judge",
                    actor=form.submitted_by or "judge_operator",
                )
            ).event
            runtime.runner.drain()
            payables = [
                p
                for p in runtime.ledger.payables()
                if p.inv_id == invoice_id and p.status == "open"
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
                )
            result = _view(runtime, invoice_id)
            assert result["event_id"] == event.id
            return result

    return router
