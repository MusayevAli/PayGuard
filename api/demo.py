"""Explicit, durable demo fixtures exercised through the real desk and selected signer.

These endpoints never reset a database, approve a payment, provision an account, change
the clock, or manufacture a signer/bank result. Mount the POST route behind the same
controller authorization as the other operator routes.
"""

from __future__ import annotations

import threading
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict

from agents.payment_agent import Group
from agents.runtime import Runtime
from core.events import make_event
from core.ledger import Evidence, JournalEntryDraft, JournalLineDraft
from core.policy import PostAction
from core.rules import applicable_price_list
from core.tax import expected_vat_minor, treatment_for_country
from core.vendors import VendorBankAccount, account_hash
from sim.models import GoodsReceipt, POLine, PurchaseOrder
from sim.worldgen.formats import format_date, format_number, format_qty

Scenario = Literal["normal", "attack", "large"]
SCENARIOS: dict[str, dict[str, str]] = {
    "normal": {
        "title": "A legitimate invoice gets paid",
        "description": "Matching purchase order, receipt and structured invoice; the selected signer authorizes the payment to the simulated bank.",
        "guard": "Approved account, amount cap, real double-entry posting",
    },
    "attack": {
        "title": "Assume the agent is already fooled",
        "description": "An explicit compromised-host fixture requests payment to an unapproved account. Watch the actual signer response.",
        "guard": "Account allowlist; no email or fixture can enroll this account",
    },
    "large": {
        "title": "A legitimate exception reaches a human",
        "description": "A matching invoice above EUR 50,000 produces an evidence packet. Review and authorize it through the controller flow.",
        "guard": "Amount threshold and separate human authorization",
    },
}
ATTACK_ACCOUNT = "DEMO-ATTACK-ACCOUNT-001"
ATTACK_IBAN = "DE02120300000000202051"


class LiveRunRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    run_id: UUID


def _invoice_id(scenario: str, run_id: str | None = None) -> str:
    suffix = UUID(run_id).hex if run_id else "001"
    return f"DEMO-{scenario.upper()}-{suffix}"


def _run_markers(rt: Runtime) -> dict[str, Any]:
    return {
        str(e.payload["form"]["run_id"]): e
        for e in rt.store.query(type="judge_injection", limit=1_000_000)
        if e.payload.get("form", {}).get("demo_fixture_version") == 2
    }


def _fixture_document_ids(rt: Runtime) -> tuple[str, str]:
    """Allocate unused IDs inside the existing PO/receipt schemas, without edits."""
    po_ids = {e.payload["po"]["id"] for e in rt.store.query(type="po_created", limit=1_000_000)}
    gr_ids = {
        e.payload["receipt"]["id"] for e in rt.store.query(type="goods_received", limit=1_000_000)
    }
    # Keep the legacy scenario IDs (90001/90002 and 9001/9002) reserved.
    po_id = next((f"PO-{n}" for n in range(90003, 100000) if f"PO-{n}" not in po_ids), None)
    gr_id = next((f"GR-{n}" for n in range(9003, 10000) if f"GR-{n}" not in gr_ids), None)
    if po_id is None or gr_id is None:
        raise HTTPException(
            409, "Live-demo document IDs are exhausted; existing records were preserved."
        )
    return po_id, gr_id


def _markers(rt: Runtime) -> dict[str, Any]:
    return {
        str(e.payload["form"]["demo_scenario"]): e
        for e in rt.store.query(type="judge_injection", limit=1_000_000)
        if e.payload.get("form", {}).get("demo_fixture_version") == 1
    }


def _view(rt: Runtime, scenario: str, run_id: str | None = None) -> dict[str, Any]:
    invoice_id = _invoice_id(scenario, run_id)
    marker = _run_markers(rt).get(run_id) if run_id else _markers(rt).get(scenario)
    payables = [p for p in rt.ledger.payables() if p.inv_id == invoice_id]
    payable_ids = {p.id for p in payables}
    payments = [p for p in rt.payment_state.payments() if payable_ids.intersection(p.payable_ids)]
    payment = payments[-1] if payments else None
    invoice = rt.invoice_state.invoice(invoice_id)
    status = payment.status if payment else (invoice.status if invoice else "not_started")
    if marker and status == "not_started":
        status = "incomplete"
    refs = {invoice_id, *payable_ids}
    if marker:
        refs.add(marker.id)
    if payment:
        refs.add(payment.pay_id)
        if payment.packet_id:
            refs.add(payment.packet_id)
    entries = [e for e in rt.ledger.entries() if e.cause_id in refs]
    refs.update(evidence.event_id for entry in entries for evidence in entry.evidence)
    events = []
    for event in rt.store.query(limit=1_000_000):
        p = event.payload
        if (
            event.id in refs
            or p.get("pay_id") in refs
            or p.get("packet_id") in refs
            or refs.intersection(str(r) for r in p.get("refs", []))
            or p.get("form", {}).get("invoice_id") == invoice_id
        ):
            events.append(event)
    chips = [e for e in events if e.type == "chip_response"]
    chip = chips[-1].payload if chips else None
    outcome = {
        "not_started": "Ready to run; no demo money has been requested.",
        "executed": "The selected signer authorized this payment and the simulated bank executed it.",
        "settled": "The simulated bank payment settled and was reconciled.",
        "awaiting_decision": "The signer refused; a human evidence packet is waiting for review.",
        "awaiting_button": "The evidence packet is approved; separate signer authorization is still required.",
        "held": "The payment is held. The actual reason is recorded below.",
        "incomplete": "The fixture started without completing. Inspect the audit events; no success is assumed.",
    }.get(status, f"Observed desk status: {status}.")
    return {
        "id": scenario,
        "run_id": run_id,
        "run_started_at": marker.payload["form"].get("run_started_at") if marker else None,
        **SCENARIOS[scenario],
        "status": status,
        "outcome": outcome,
        "started": marker is not None,
        "invoice_id": invoice_id,
        "vendor_id": payment.vendor_id
        if payment
        else (payables[0].vendor_id if payables else None),
        "payable_ids": sorted(payable_ids),
        "pay_id": payment.pay_id if payment else None,
        "packet_id": payment.packet_id if payment else None,
        "amount_minor": payment.amount_minor
        if payment
        else (sum(p.amount_minor for p in payables) or None),
        "currency": "EUR",
        "held_reason": payment.held_reason if payment else (invoice.reason if invoice else None),
        "signer_kind": rt.signer.kind,
        "bank_mode": "simulated",
        "fixture_mode": "compromised_host" if scenario == "attack" else "structured_invoice",
        "chip_response": chip,
        "evidence_event_ids": [e.id for e in events],
        "evidence_events": [e.model_dump(mode="json") for e in events],
        "journal_entry_ids": [e.id for e in entries],
        "journal_entries": [
            {
                "id": e.id,
                "cause_id": e.cause_id,
                "description": e.description,
                "evidence": [{"event_id": item.event_id, "role": item.role} for item in e.evidence],
                "lines": [
                    {
                        "account": ln.account,
                        "debit_minor": ln.debit_minor,
                        "credit_minor": ln.credit_minor,
                        "currency": ln.currency,
                    }
                    for ln in e.lines
                ],
            }
            for e in entries
        ],
        "next_action": "Review the evidence packet, then use the controller authorization flow."
        if status in ("awaiting_decision", "awaiting_button")
        else None,
    }


def _emit(rt: Runtime, type_: str, payload: dict[str, Any]):
    return rt.store.append(
        make_event(
            type_,
            payload,
            event_time=rt.clock.sim_now,
            source="demo_fixture",
            actor="demo_operator",
        )
    ).event


def _invoice_fixture(rt: Runtime, scenario: str, run_id: str | None = None) -> None:
    """Generate input facts; the existing invoice agent checks and posts them."""
    now = rt.clock.sim_now
    selected = None
    for supplier in rt.suppliers:
        acct = rt.vendors.active_account(supplier.id)
        price_list, _ = applicable_price_list(supplier.price_lists, now.date())
        if (
            supplier.currency == "EUR"
            and acct
            and acct.on_chip
            and price_list
            and acct.hold_until <= now
            and rt.payment_agent._target_account(supplier.id).id == acct.id
        ):
            selected = supplier, price_list
            break
    if selected is None:
        raise HTTPException(
            409,
            "No active, already provisioned EUR vendor is available. Provision through the trusted setup flow first.",
        )
    supplier, price_list = selected
    sku, price = next((sku, price) for sku, price in price_list.prices.items() if price > 0)
    target = 125_000 if scenario == "normal" else 7_500_000
    qty = max(1, (target + price - 1) // price)
    net = qty * price
    vat = expected_vat_minor(net, treatment_for_country(supplier.country))
    suffix = 1 if scenario == "normal" else 2
    po_id, gr_id = _fixture_document_ids(rt) if run_id else (f"PO-9000{suffix}", f"GR-900{suffix}")
    for ev in rt.store.query(type="po_created", limit=1_000_000):
        if ev.payload["po"]["id"] == po_id:
            raise HTTPException(409, f"Reserved demo purchase order {po_id} already exists.")
    po = PurchaseOrder(
        id=po_id,
        supplier_id=supplier.id,
        buyer=supplier.purchasing_contact,
        created_at=now,
        order_date=now.date(),
        ship_to_plant="DE",
        currency="EUR",
        notes="Explicit live-demo fixture; no real purchase order or goods movement.",
        lines=[
            POLine(
                line_no=1,
                sku=sku,
                description=f"Demo material {sku}",
                description_local=f"Demo material {sku}",
                qty=qty,
                uom="pc",
                unit_price_minor=price,
                currency="EUR",
                price_list_id=price_list.id,
                requested_delivery=now.date(),
            )
        ],
    )
    receipt = GoodsReceipt(
        id=gr_id,
        po_id=po_id,
        line_no=1,
        received_at=now,
        booked_at=now,
        qty=qty,
        uom="pc",
        dock_clerk="demo_operator",
        plant="DE",
        note="Explicit live-demo receipt fixture.",
    )
    _emit(rt, "po_created", {"po": po.model_dump(mode="json")})
    _emit(rt, "goods_received", {"receipt": receipt.model_dump(mode="json")})

    def number(amount: int) -> str:
        return format_number(amount, "EUR", supplier.number_locale)

    form = {
        "invoice_id": _invoice_id(scenario, run_id),
        "invoice_number": _invoice_id(scenario, run_id),
        "supplier_id": supplier.id,
        "supplier_name": supplier.legal_name,
        "vat_id": supplier.vat_id,
        "invoice_date": format_date(now.date(), supplier.date_format),
        "due_date": format_date(now.date(), supplier.date_format),
        "po_refs": [po_id],
        "currency": "EUR",
        "language": supplier.language,
        "net": number(net),
        "vat": number(vat),
        "gross": number(net + vat),
        "vat_rate": "19" if vat else "0",
        "lines": [
            {
                "description": f"Demo material {sku}",
                "sku": sku,
                "qty": format_qty(qty, supplier.number_locale),
                "uom": "pc",
                "unit_price": number(price),
                "line_total": number(net),
            }
        ],
        "notes": "Explicit structured demo fixture. The invoice agent uses its ordinary validation and matching rules.",
    }
    _emit(
        rt,
        "judge_injection",
        {
            "kind": "invoice",
            "submitted_by": "demo_operator",
            "form": form,
            "resulting_event_id": None,
        },
    )
    rt.runner.drain()


def _attack_fixture(rt: Runtime, marker: Any, ctx: Any, run_id: str | None = None) -> None:
    """Fault-inject a known vendor's host record, then restore it in all exit paths.

    A known vendor is essential: an unknown vendor can fail in the host adapter
    before reaching the FPGA. This swap changes no chip record or approval.
    The caller holds the runner lock throughout the scoped mutation and payment.
    """
    now = rt.clock.sim_now
    attack_account = f"DEMO-ATTACK-ACCOUNT-{UUID(run_id).hex}" if run_id else ATTACK_ACCOUNT
    invoice_id = _invoice_id("attack", run_id)
    if rt.vendors.get_account(attack_account) is not None:
        raise ValueError("Reserved attack-fixture account already exists; refusing overwrite")
    original = next(
        (
            acct
            for supplier in rt.suppliers
            if supplier.currency == "EUR"
            and (acct := rt.vendors.active_account(supplier.id)) is not None
            and acct.on_chip
            and rt.payment_agent._target_account(supplier.id).id == acct.id
        ),
        None,
    )
    if original is None:
        raise ValueError(
            "An already provisioned EUR vendor is required for the FPGA account-mismatch proof"
        )
    fake = VendorBankAccount(
        id=attack_account,
        vendor_id=original.vendor_id,
        iban_or_acct=ATTACK_IBAN,
        account_hash=account_hash(ATTACK_IBAN),
        status="active",
        requested_at=now,
        requested_via="demo_compromised_host",
        requested_by="demo_operator",
        request_event_id=marker.id,
        verified_via=None,
        verified_event_id=None,
        verified_at=None,
        hold_until=now,
        approved_by=None,
        approval_event_id=None,
        approved_at=None,
        on_chip=False,
        retired_at=None,
    )
    draft = JournalEntryDraft(
        effective_date=now.date(),
        description="DEMO fault injection: assume an agent already accepted a fraudulent payable",
        cause_type="invoice",
        cause_id=invoice_id,
        lines=[
            JournalLineDraft(
                account="Expenses:Materials:Harness", debit_minor=125_000, currency="EUR"
            ),
            JournalLineDraft(account="Liabilities:AP", credit_minor=125_000, currency="EUR"),
        ],
        evidence=[Evidence(event_id=marker.id, role="invoice")],
    )
    entry = rt.policy.execute(PostAction(draft=draft, posted_at=now), "invoice_agent")
    rt.ledger.create_payable(
        vendor_id=original.vendor_id,
        inv_id=invoice_id,
        entry_id=entry.id,
        amount_minor=125_000,
        currency="EUR",
        due_date=now.date(),
    )
    _emit(
        rt,
        "judge_injection",
        {
            "kind": "message",
            "submitted_by": "demo_operator",
            "resulting_event_id": None,
            "form": {
                "invoice_id": invoice_id,
                "fault_injection": "temporarily replace the host target account; preserve chip enrollment",
                "vendor_id": original.vendor_id,
                "original_account_id": original.id,
                "original_account_hash": original.account_hash,
                "fake_account_hash": fake.account_hash,
            },
        },
    )
    rt.vendors.update_account(original.model_copy(update={"status": "suspended"}))
    try:
        rt.vendors.insert_account(fake)
        payables = [p for p in rt.ledger.payables() if p.inv_id == invoice_id]
        rt.payment_agent._pay(
            Group(vendor_id=original.vendor_id, currency="EUR", payables=payables),
            ctx,
            today=now.date(),
        )
    finally:
        if rt.vendors.get_account(attack_account) is not None:
            rt.vendors.update_account(
                fake.model_copy(update={"status": "retired", "retired_at": now})
            )
        rt.vendors.update_account(original)
        _emit(
            rt,
            "judge_injection",
            {
                "kind": "message",
                "submitted_by": "demo_operator",
                "resulting_event_id": None,
                "form": {
                    "invoice_id": invoice_id,
                    "fault_injection": "original host account restored; fake account retired",
                    "vendor_id": original.vendor_id,
                    "original_account_id": original.id,
                },
            },
        )


def create_demo_router(
    runtime: Runtime, *, require_controller: Callable | None = None
) -> APIRouter:
    router = APIRouter(tags=["live-demo"])
    lock = threading.RLock()

    @router.get("/demo")
    def demo_state() -> dict[str, Any]:
        with lock:
            latest = {}
            for run_id, marker in _run_markers(runtime).items():
                latest[marker.payload["form"]["demo_scenario"]] = run_id
            scenarios = []
            for name in SCENARIOS:
                card = _view(runtime, name)
                run_id = latest.get(name)
                card["latest_run_id"] = run_id
                card["latest_run"] = _view(runtime, name, run_id) if run_id else None
                scenarios.append(card)
            return {
                "signer_kind": runtime.signer.kind,
                "bank_mode": "simulated",
                "fixture_mode": "explicit",
                "resets_existing_state": False,
                "scenarios": scenarios,
                "notice": "Run live creates a fresh input and signer request. View result reads a previous execution. Inputs are demo fixtures; outcomes come from the selected signer, agents, ledger and simulated bank.",
            }

    dependencies = [Depends(require_controller)] if require_controller is not None else []

    def execute(scenario: Scenario, run_id: str | None = None) -> dict[str, Any]:
        if runtime.invoice_agent is None or runtime.payment_agent is None:
            raise HTTPException(503, "The invoice and payment agents must be enabled.")
        # Check the selected device before introducing any fixture state. No stub fallback.
        try:
            runtime.signer.status()
        except Exception as exc:
            raise HTTPException(
                503, "The selected signer is unavailable; no demo fixture was inserted."
            ) from exc
        marker = _emit(
            runtime,
            "judge_injection",
            {
                "kind": "message",
                "submitted_by": "demo_operator",
                "resulting_event_id": None,
                "form": {
                    "demo_fixture_version": 2 if run_id else 1,
                    "demo_scenario": scenario,
                    "invoice_id": _invoice_id(scenario, run_id),
                    **(
                        {"run_id": run_id, "run_started_at": datetime.now(UTC).isoformat()}
                        if run_id
                        else {}
                    ),
                    "assumption": "The host has already been fooled; this is a fault injection, not live LLM extraction."
                    if scenario == "attack"
                    else "Explicit matching PO, receipt and structured invoice fixtures.",
                },
            },
        )
        if scenario == "attack":
            runtime.runner.run_task(
                runtime.payment_agent,
                lambda ctx: _attack_fixture(runtime, marker, ctx, run_id),
            )
            return _view(runtime, scenario, run_id)
        _invoice_fixture(runtime, scenario, run_id)
        payables = [
            p
            for p in runtime.ledger.payables()
            if p.inv_id == _invoice_id(scenario, run_id) and p.status == "open"
        ]
        if payables:
            group = Group(vendor_id=payables[0].vendor_id, currency="EUR", payables=payables)
            # One new fixture group only: never run unrelated due payments or advance time.
            runtime.runner.run_task(
                runtime.payment_agent,
                lambda ctx: runtime.payment_agent._pay(
                    group, ctx, today=runtime.clock.sim_now.date()
                ),
            )
        return _view(runtime, scenario, run_id)

    @router.post("/demo/scenarios/{scenario}", dependencies=dependencies)
    def run_scenario(scenario: Scenario) -> dict[str, Any]:
        with lock:
            if scenario in _markers(runtime):
                return _view(runtime, scenario)
            return execute(scenario)

    @router.post("/demo/runs/{scenario}", dependencies=dependencies)
    def run_live(scenario: Scenario, request: LiveRunRequest) -> dict[str, Any]:
        run_id = str(request.run_id)
        with lock:
            marker = _run_markers(runtime).get(run_id)
            if marker is not None:
                if marker.payload["form"]["demo_scenario"] != scenario:
                    raise HTTPException(409, "This run ID belongs to a different scenario.")
                # Retrying after a lost HTTP response never creates another payment.
                return _view(runtime, scenario, run_id)
            return execute(scenario, run_id)

    @router.get("/demo/runs/{run_id}")
    def live_result(run_id: UUID) -> dict[str, Any]:
        with lock:
            marker = _run_markers(runtime).get(str(run_id))
            if marker is None:
                raise HTTPException(404, "Live run not found.")
            return _view(runtime, marker.payload["form"]["demo_scenario"], str(run_id))

    return router
