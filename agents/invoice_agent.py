"""Invoice agent (spec §7.3): extraction → validation (R0) → vendor (R5) → credit notes (R6) →
duplicates (R4) → purchase orders (R13/R14) → receipts (R3, M5, M6) → price lists (M3, R2) →
quantities (R1) → VAT (R11) → entries (R12, R15) → posting, payables, bank-detail forwarding;
GRNI accruals at month end (R7). All side effects go through ``Context``."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

import structlog

from core.accounts import (
    AP_ACCOUNT,
    AP_RETENTION_ACCOUNT,
    FX_ROUNDING_ACCOUNT,
    GRNI_ACCOUNT,
    expense_account_for_category,
)
from core.events import EventStore
from core.ledger import Evidence, JournalEntry, JournalEntryDraft, JournalLineDraft, Ledger
from core.money import FxRateUnavailable, Money, allocate, convert
from core.packets import PacketDraft, PacketEvidence, PacketOption, PacketStore, format_minor
from core.policy import PostAction, ReverseAction
from core.rules import (
    FREIGHT_CONFIRM_MINOR,
    VAT_DISPUTE_ESCALATE_EUR_MINOR,
    WAREHOUSE_WAIT_HOURS,
    applicable_price_list,
    discount_terms,
    is_duplicate,
    normalize_uom,
    price_within_tolerance,
    retention_split,
    rounding_delta,
    short_pay,
    variance_bp,
)
from core.tax import (
    expected_vat_minor,
    is_vat_wrongly_charged,
    self_assessed_vat,
    treatment_for_country,
    vat_lines,
)
from core.vendors import VendorMaster
from sim.models import ColleagueQuery, Event, GoodsReceipt, POLine, PurchaseOrder, Supplier

from .base import Context
from .documents import load_document
from .parsed import (
    ExtractionFailure,
    ParsedInvoice,
    ParsedLine,
    ValidatedInvoice,
    ValidatedLine,
)
from .resolve import VendorIndex, line_similarity, normalize_name, parse_po_refs
from .state import AgentState
from .structured_mapper import map_structured
from .validate import validate_parsed

log = structlog.get_logger("invoice_agent")

TERM_DAYS = {"net30": 30, "net45": 45, "2/10 net30": 30}
FREIGHT_WORDS = (
    "freight",
    "fracht",
    "transport",
    "spedition",
    "doprava",
    "przewóz",
    "przewoz",
    "kurier",
    "courier",
    "shipping",
    "palet",
    "lcl",
    "ftl",
    "customs",
    "celn",
    "zoll",
)
WAREHOUSE_CONTACT = "jonas.weber"  # the warehouse lead answers receipt questions (spec §2.8)
OPTIONS_HOLD = [
    PacketOption(id="approve", label="Approve and post"),
    PacketOption(id="reject", label="Reject invoice"),
    PacketOption(id="hold", label="Keep on hold"),
]


@dataclass
class Receipt:
    gr: GoodsReceipt
    event_id: str
    line_no: int  # effective line after reassignment
    qty_po_units: int


@dataclass
class LineItem:
    inv_line: ValidatedLine
    po: PurchaseOrder
    po_line: POLine
    qty_po_units: int
    unit_price_po_units: int
    list_price: int | None = None
    variance_bp: int = 0
    receipts: list[Receipt] = field(default_factory=list)
    qty_available: int = 0
    qty_to_post: int = 0
    balance: int = 0


class Held(Exception):
    """Processing stops here (held, escalated, rejected, waiting); the state is already recorded."""


class InvoiceAgent:
    principal = "invoice_agent"
    subscribes = frozenset(
        {
            "invoice_received",
            "goods_received",
            "colleague_reply",
            "po_amended",
            "day_boundary",
            "judge_injection",
        }
    )

    def __init__(
        self,
        *,
        world_dir: Path,
        store: EventStore,
        ledger: Ledger,
        vendors: VendorMaster,
        packets: PacketStore,
        state: AgentState,
        llm: Any,
        suppliers: list[Supplier],
        now: Callable[[], datetime],
    ) -> None:
        self.world_dir = Path(world_dir)
        self.store = store
        self.ledger = ledger
        self.vendors = vendors
        self.packets = packets
        self.state = state
        self.llm = llm
        self.suppliers = {s.id: s for s in suppliers}
        self.index = VendorIndex(suppliers, vendors)
        self._now = now
        self._seq = -1
        self._pos: dict[str, PurchaseOrder] = {}
        self._po_events: dict[str, str] = {}
        self._receipts: list[tuple[GoodsReceipt, str]] = []
        self._envelopes: dict[str, Event] = {}

    # ------------------------------------------------------------------ views of the world

    def _refresh(self) -> None:
        seq = self.store.latest_seq()
        if seq == self._seq:
            return
        pos: dict[str, PurchaseOrder] = {}
        po_events: dict[str, str] = {}
        receipts: list[tuple[GoodsReceipt, str]] = []
        envelopes: dict[str, Event] = {}
        for ev in self.store.query(limit=10_000_000):
            if ev.type == "po_created":
                po = PurchaseOrder.model_validate(ev.payload["po"])
                pos[po.id] = po
                po_events.setdefault(po.id, ev.id)
            elif ev.type == "po_amended":
                po = PurchaseOrder.model_validate(ev.payload["po_after"])
                pos[po.id] = po
            elif ev.type == "goods_received":
                receipts.append((GoodsReceipt.model_validate(ev.payload["receipt"]), ev.id))
            elif ev.type == "invoice_received":
                envelopes[str(ev.payload["invoice_id"])] = ev
        self._pos, self._po_events, self._receipts, self._envelopes, self._seq = (
            pos,
            po_events,
            receipts,
            envelopes,
            seq,
        )

    def po(self, po_id: str) -> PurchaseOrder | None:
        self._refresh()
        return self._pos.get(po_id)

    def pos_for_vendor(self, vendor_id: str) -> list[PurchaseOrder]:
        self._refresh()
        ids = {vendor_id} | {
            s.id for s in self.suppliers.values() if self.index.canonical(s.id) == vendor_id
        }
        return sorted(
            (po for po in self._pos.values() if po.supplier_id in ids), key=lambda p: p.order_date
        )

    def receipts_on_po(self, po_id: str) -> list[Receipt]:
        self._refresh()
        po = self._pos.get(po_id)
        if po is None:
            return []
        reassigned = {r.gr_id: r for r in self.state.reassignments(po_id)}
        out: list[Receipt] = []
        for gr, ev_id in self._receipts:
            if gr.po_id != po_id:
                continue
            line_no, qty, uom = gr.line_no, gr.qty, gr.uom
            if gr.id in reassigned:
                r = reassigned[gr.id]
                line_no = r.to_line
                uom = r.uom or uom
                qty = r.qty if r.qty is not None else qty
            po_line = next((ln for ln in po.lines if ln.line_no == line_no), None)
            if po_line is None:
                continue
            norm = normalize_uom(qty, uom, po_line.uom)
            qty_po = norm[0] if norm is not None else 0
            out.append(Receipt(gr=gr, event_id=ev_id, line_no=line_no, qty_po_units=qty_po))
        return out

    def receipts_for(self, po_id: str, line_no: int) -> list[Receipt]:
        return [r for r in self.receipts_on_po(po_id) if r.line_no == line_no]

    # ------------------------------------------------------------------ runner hooks

    def prefetch(self, events: list[Event]) -> None:
        extract_many = getattr(self.llm, "extract_many", None)
        if not callable(extract_many):
            return
        docs = []
        for ev in events:
            if (
                ev.type != "invoice_received"
                or ev.payload["format"] == "structured"
                or self.state.facts(ev.payload["invoice_id"]) is not None
            ):
                continue
            docs.append(
                load_document(
                    self.world_dir,
                    ev.payload,
                    self.index.hints_for(ev.payload.get("supplier_hint"), ev.payload.get("sender")),
                )
            )
        if docs:
            extract_many(docs)

    def handle(self, ev: Event, ctx: Context) -> list[Event]:
        if ev.type == "invoice_received":
            self._run(str(ev.payload["invoice_id"]), ctx)
        elif ev.type == "judge_injection":
            if ev.payload.get("kind") == "invoice":
                self._judge(ev, ctx)
        elif ev.type == "goods_received":
            receipt = ev.payload["receipt"]
            for wait in self.state.waits_for_po(receipt["po_id"]):
                self._run(wait.invoice_id, ctx)
            for row in self.state.invoices(status="held"):
                facts = self.state.facts(row.invoice_id) or {}
                if receipt["po_id"] in facts.get("po_ids", []):
                    self._run(row.invoice_id, ctx)
        elif ev.type == "colleague_reply":
            wait = self.state.wait_by_asked_event(str(ev.payload.get("in_reply_to") or ""))
            if wait is not None and wait.resolved_at is None:
                self.state.record_reply(
                    wait.id,
                    {
                        "refs": ev.payload.get("refs", []),
                        "body": ev.payload.get("body", ""),
                        "person_id": ev.payload.get("person_id"),
                        "bounced": ev.payload.get("bounced", False),
                        "reply_event_id": ev.id,
                    },
                )
                self._run(wait.invoice_id, ctx)
        elif ev.type == "po_amended":
            for row in self.state.invoices(status="held"):
                facts = self.state.facts(row.invoice_id) or {}
                if ev.payload["po_id"] in facts.get("po_ids", []):
                    self._run(row.invoice_id, ctx)
        elif ev.type == "day_boundary":
            self._expire_waits(ctx)
            month = ev.payload.get("month_closed")
            if month:
                self._accrue_month_end(str(month), ctx)
            for row in self.state.invoices(status="received"):
                self._run(row.invoice_id, ctx)
        return []

    # ------------------------------------------------------------------ main flow

    def _run(self, invoice_id: str, ctx: Context) -> None:
        try:
            self._process(invoice_id, ctx)
        except Held:
            return

    def _envelope(self, invoice_id: str) -> Event | None:
        self._refresh()
        return self._envelopes.get(invoice_id)

    def _process(self, invoice_id: str, ctx: Context) -> None:
        row = self.state.invoice(invoice_id)
        if row is not None and row.status in ("posted", "rejected"):
            return
        envelope = self._envelope(invoice_id)
        facts = self.state.facts(invoice_id)
        if facts is None:
            if envelope is None:
                return
            validated = self._extract(envelope, ctx)
            facts = {
                "validated": validated.to_json(),
                "envelope_event_id": envelope.id,
                "received_via": envelope.payload.get("received_via"),
                "sender": envelope.payload.get("sender"),
                "supplier_hint": envelope.payload.get("supplier_hint"),
                "po_ids": [],
            }
            self.state.set_invoice(
                invoice_id,
                "extracted",
                rule_id=None,
                reason=None,
                now=ctx.now,
                invoice_number=validated.invoice_number,
                facts=facts,
            )
        validated = ValidatedInvoice.model_validate(facts["validated"])
        envelope_event_id = str(facts["envelope_event_id"])
        number = validated.invoice_number
        refs = [invoice_id, number]
        vendor_id = self._resolve_vendor(invoice_id, validated, facts, ctx)
        supplier = self.suppliers[vendor_id]
        refs.append(vendor_id)
        if validated.is_credit_note:
            self._credit_note(invoice_id, validated, vendor_id, envelope_event_id, ctx)
            return
        self._check_duplicate(invoice_id, validated, vendor_id, envelope_event_id, ctx)
        items, pos = self._match_lines(invoice_id, validated, supplier, vendor_id, facts, ctx)
        if not items:
            self._no_po(invoice_id, validated, supplier, vendor_id, envelope_event_id, facts, ctx)
            return
        earliest_po = min(po.order_date for po in pos)
        if validated.invoice_date < earliest_po:
            self._hold(
                invoice_id,
                validated,
                "R14",
                "invoice_dated_before_po",
                ctx,
                refs=[*refs, *[p.id for p in pos]],
                severity="action",
                question=f"Invoice {number} is dated {validated.invoice_date}, before purchase order {pos[0].id} ({earliest_po}). Approve anyway?",
                context=f"R14: invoice date precedes the PO date by {(earliest_po - validated.invoice_date).days} days; this is suspicious.",
                materiality=validated.gross_minor,
                currency=validated.currency,
            )
        self._apply_receipts(invoice_id, validated, items, ctx)
        self._apply_prices(invoice_id, validated, supplier, items, ctx)
        self._post_invoice(
            invoice_id, validated, supplier, vendor_id, items, pos, envelope_event_id, facts, ctx
        )

    # ------------------------------------------------------------------ extraction

    def _extract(self, envelope: Event, ctx: Context) -> ValidatedInvoice:
        invoice_id = str(envelope.payload["invoice_id"])
        hints = self.index.hints_for(
            envelope.payload.get("supplier_hint"), envelope.payload.get("sender")
        )
        doc = load_document(self.world_dir, envelope.payload, hints)
        path = "llm"
        parsed: ParsedInvoice | ExtractionFailure | None = None
        number_hint: str | None = None
        if doc.structured is not None:
            mapped = map_structured(doc.structured, hints)
            if mapped is not None:
                parsed, path = mapped, "structured_mapper"
        try:
            if parsed is None:
                parsed = self.llm.extract_invoice(doc)
            validated, errors = (
                (None, ["extraction failed: " + parsed.reason])
                if isinstance(parsed, ExtractionFailure)
                else validate_parsed(parsed, hints)
            )
            if not isinstance(parsed, ExtractionFailure):
                number_hint = parsed.invoice_number.strip() or None
            attempts = 1
            if validated is None:
                attempts = 2
                retry = self.llm.extract_invoice(doc, feedback=errors)
                if isinstance(retry, ExtractionFailure):
                    errors = [*errors, "retry failed: " + retry.reason]
                else:
                    validated, errors2 = validate_parsed(retry, hints)
                    errors = errors + errors2
                    number_hint = retry.invoice_number.strip() or number_hint
                    path = "llm"
        except Exception as exc:
            if type(exc).__name__ != "LLMUnavailable":
                raise
            self.state.set_invoice(
                invoice_id, "received", rule_id=None, reason="llm_unavailable", now=ctx.now
            )
            log.warning(
                "extraction_unavailable",
                invoice_id=invoice_id,
                error=str(exc),
                event_id=envelope.id,
                principal=self.principal,
                rule_id=None,
            )
            raise Held from exc
        if validated is None:
            refs = [invoice_id, *([number_hint] if number_hint else []), envelope.id]
            self.state.set_invoice(
                invoice_id,
                "held",
                rule_id="R0",
                reason="unreadable_invoice",
                now=ctx.now,
                invoice_number=number_hint,
            )
            ctx.act(
                action_type="held",
                rule_id="R0",
                refs=refs,
                inputs={"path": path, "attempts": attempts, "errors": errors},
                decision={"reason": "unreadable_invoice", "status": "held"},
            )
            self._packet(
                ctx,
                invoice_id,
                f"Invoice {number_hint or invoice_id}: unreadable invoice, totals do not reconcile after re-extraction. Hold?",
                "R0: " + "; ".join(errors)[:800],
                "action",
                refs,
                None,
                None,
                [PacketEvidence(event_id=envelope.id, role="invoice", summary="the document")],
            )
            raise Held
        ctx.act(
            action_type="extracted",
            rule_id=None,
            refs=[invoice_id, validated.invoice_number, envelope.id],
            inputs={
                "path": path,
                "format": doc.format,
                "pages": doc.pages,
                "attempts": attempts,
                "hints": hints.model_dump(),
            },
            decision={
                "invoice_number": validated.invoice_number,
                "supplier_name": validated.supplier_name,
                "currency": validated.currency,
                "net_minor": validated.net_minor,
                "vat_minor": validated.vat_minor,
                "gross_minor": validated.gross_minor,
                "lines": len(validated.lines),
            },
        )
        return validated

    def _judge(self, ev: Event, ctx: Context) -> None:
        form = dict(ev.payload.get("form") or {})
        invoice_id = str(form.get("invoice_id") or f"JUDGE-{ev.id[-8:]}")
        hints = self.index.hints_for(form.get("supplier_id"), form.get("sender"))
        parsed = ParsedInvoice(
            supplier_name=str(form.get("supplier_name", "")),
            supplier_vat_id=form.get("vat_id"),
            invoice_number=str(form.get("invoice_number", invoice_id)),
            invoice_date=str(form.get("invoice_date", "")),
            due_date=form.get("due_date"),
            service_date=None,
            po_references=list(form.get("po_refs", [])),
            currency=str(form.get("currency", "EUR")),
            lines=[
                ParsedLine(
                    description=str(ln.get("description", "")),
                    sku=ln.get("sku"),
                    qty=str(ln.get("qty", "")),
                    uom=ln.get("uom"),
                    unit_price=str(ln.get("unit_price", "")),
                    line_total=str(ln.get("line_total", "")),
                )
                for ln in form.get("lines", [])
            ],
            net=str(form.get("net", "")),
            vat_rate=form.get("vat_rate"),
            vat_amount=str(form.get("vat", "0")),
            gross=str(form.get("gross", "")),
            bank_details=None,
            document_language=str(form.get("language", "en")),
            notes=form.get("notes"),
            source="judge",
        )
        validated, errors = validate_parsed(parsed, hints)
        if validated is None:
            self.state.set_invoice(
                invoice_id, "held", rule_id="R0", reason="unreadable_invoice", now=ctx.now
            )
            ctx.act(
                action_type="held",
                rule_id="R0",
                refs=[invoice_id, ev.id],
                inputs={"path": "judge", "errors": errors},
                decision={"reason": "unreadable_invoice"},
            )
            return
        facts = {
            "validated": validated.to_json(),
            "envelope_event_id": ev.id,
            "received_via": "judge",
            "sender": None,
            "supplier_hint": form.get("supplier_id"),
            "po_ids": [],
        }
        self.state.set_invoice(
            invoice_id,
            "extracted",
            rule_id=None,
            reason=None,
            now=ctx.now,
            invoice_number=validated.invoice_number,
            facts=facts,
        )
        self._envelopes[invoice_id] = ev
        self._run(invoice_id, ctx)

    # ------------------------------------------------------------------ vendor (R5)

    def _resolve_vendor(
        self, invoice_id: str, v: ValidatedInvoice, facts: dict[str, Any], ctx: Context
    ) -> str:
        resolved = self.index.resolve(v.supplier_name, v.supplier_vat_id)
        if (
            resolved is None
            and facts.get("supplier_hint")
            and facts.get("received_via") in ("edi", "portal", "judge")
        ):
            hinted = str(facts["supplier_hint"])
            if hinted in self.suppliers:
                resolved = self.index.resolve(
                    self.suppliers[hinted].legal_name, self.suppliers[hinted].vat_id
                )
        if resolved is None:
            row = self.state.invoice(invoice_id)
            if row is None or row.reason != "held_unknown_vendor":
                self.state.set_invoice(
                    invoice_id, "held", rule_id="R5", reason="held_unknown_vendor", now=ctx.now
                )
                ctx.act(
                    action_type="held",
                    rule_id="R5",
                    refs=[invoice_id, v.invoice_number],
                    inputs={"supplier_name": v.supplier_name, "vat_id": v.supplier_vat_id},
                    decision={
                        "reason": "held_unknown_vendor",
                        "normalized_name": normalize_name(v.supplier_name),
                    },
                )
                ctx.emit(
                    "vendor_message",
                    {
                        "message_id": f"inv-onboard-{invoice_id}",
                        "channel": "invoice",
                        "vendor_id": None,
                        "claimed_supplier_name": v.supplier_name,
                        "from_address": str(facts.get("sender") or "invoices@unknown.example"),
                        "reply_to": None,
                        "to": "ap-invoices@tbeu.example",
                        "subject": f"Unknown vendor on invoice {v.invoice_number}",
                        "body": (
                            f"Invoice {v.invoice_number} from {v.supplier_name} for {format_minor(v.gross_minor, v.currency)}. "
                            f"VAT ID {v.supplier_vat_id or 'none'}. "
                            f"IBAN {(v.bank_details.iban if v.bank_details else None) or 'none'}. New supplier: onboarding needed."
                        ),
                        "eml_path": None,
                        "in_reply_to": None,
                        "thread_id": invoice_id,
                        "attachments": [],
                        "bank_details": v.bank_details.model_dump() | {"holder": v.supplier_name}
                        if v.bank_details
                        else None,
                    },
                    source="invoice_agent",
                    actor="invoice_agent",
                )
                ctx.act(
                    action_type="onboarding_opened",
                    rule_id="R5",
                    refs=[invoice_id, v.invoice_number, v.supplier_name],
                    inputs={"supplier_name": v.supplier_name, "vat_id": v.supplier_vat_id},
                    decision={
                        "onboarding": "requested",
                        "message": self.llm.draft_message(
                            "onboarding_request",
                            {"invoice_number": v.invoice_number, "supplier_name": v.supplier_name},
                        ),
                    },
                )
                self._packet(
                    ctx,
                    invoice_id,
                    f"Invoice {v.invoice_number} from unknown vendor {v.supplier_name}. Start onboarding?",
                    f"R5: no vendor matches name '{v.supplier_name}' or VAT id {v.supplier_vat_id}; invoice held.",
                    "action",
                    [invoice_id, v.invoice_number, v.supplier_name],
                    v.gross_minor,
                    v.currency,
                    [],
                )
            raise Held
        facts["vendor_id"] = resolved.vendor_id
        if not facts.get("vendor_logged"):
            ctx.act(
                action_type="matched",
                rule_id=None,
                refs=[invoice_id, v.invoice_number, resolved.vendor_id, resolved.original_id],
                inputs={
                    "supplier_name": v.supplier_name,
                    "vat_id": v.supplier_vat_id,
                    "matched_by": resolved.matched_by,
                },
                decision={
                    "vendor_id": resolved.vendor_id,
                    "original_id": resolved.original_id,
                    "merged": resolved.original_id != resolved.vendor_id,
                },
            )
            facts["vendor_logged"] = True
            self.state.set_invoice(
                invoice_id,
                "matched",
                rule_id=None,
                reason=None,
                now=ctx.now,
                vendor_id=resolved.vendor_id,
                facts=facts,
            )
        return resolved.vendor_id

    # ------------------------------------------------------------------ duplicates (R4)

    def _check_duplicate(
        self,
        invoice_id: str,
        v: ValidatedInvoice,
        vendor_id: str,
        envelope_event_id: str,
        ctx: Context,
    ) -> None:
        for row in self.state.invoices(vendor_id=vendor_id):
            if row.invoice_id == invoice_id or row.status in ("rejected",):
                continue
            other = self.state.facts(row.invoice_id)
            if not other:
                continue
            ov = ValidatedInvoice.model_validate(other["validated"])
            if _distinct_supplies(ov, v):
                continue
            if ov.is_credit_note or not is_duplicate(
                ov.gross_minor,
                ov.invoice_date,
                ov.invoice_number,
                v.gross_minor,
                v.invoice_date,
                v.invoice_number,
            ):
                continue
            self.state.set_invoice(
                invoice_id,
                "rejected",
                rule_id="R4",
                reason=f"duplicate_of:{row.invoice_id}",
                now=ctx.now,
            )
            ctx.act(
                action_type="duplicate_rejected",
                rule_id="R4",
                refs=[invoice_id, v.invoice_number, row.invoice_id, ov.invoice_number, vendor_id],
                inputs={
                    "gross_minor": v.gross_minor,
                    "invoice_date": v.invoice_date,
                    "original_gross_minor": ov.gross_minor,
                    "original_date": ov.invoice_date,
                },
                decision={
                    "duplicate_of": row.invoice_id,
                    "original_number": ov.invoice_number,
                    "reason": "same vendor, total within 50 bp, dates within 3 days, different number",
                },
            )
            raise Held

    # ------------------------------------------------------------------ purchase orders

    def _match_lines(
        self,
        invoice_id: str,
        v: ValidatedInvoice,
        supplier: Supplier,
        vendor_id: str,
        facts: dict[str, Any],
        ctx: Context,
    ) -> tuple[list[LineItem], list[PurchaseOrder]]:
        self._refresh()
        refs = parse_po_refs(v.po_references)
        vendor_pos = self.pos_for_vendor(vendor_id)
        candidates = [po for po in vendor_pos if po.id in refs]
        reply = self.state.last_reply(invoice_id, "po_query")
        if not candidates and reply:
            for ref in reply.get("refs", []):
                if ref.startswith("po:") and self.po(ref[3:]):
                    candidates.append(self.po(ref[3:]))  # type: ignore[arg-type]
        if not candidates:
            candidates = [po for po in vendor_pos if self._po_open_qty(po) > 0]
        items: list[LineItem] = []
        used: set[tuple[str, int]] = set()
        for inv_line in v.lines:
            best: tuple[float, PurchaseOrder, POLine] | None = None
            for po in candidates:
                for pl in po.lines:
                    score = line_similarity(
                        inv_line, pl.sku, [pl.description, pl.description_local]
                    )
                    if (po.id, pl.line_no) in used and score < 1.0:
                        score -= 0.05
                    if score >= 0.6 and (best is None or score > best[0]):
                        best = (score, po, pl)
            if best is None:
                continue
            _, po, pl = best
            uom = inv_line.uom or pl.uom
            norm = normalize_uom(inv_line.qty, _canonical_uom(uom, pl.uom), pl.uom)
            if inv_line.uom is None and pl.uom == "box50":
                alt = normalize_uom(inv_line.qty, "pc", "box50")
                if (
                    alt is not None
                    and alt[0]
                    and abs(
                        round(inv_line.unit_price_minor * inv_line.qty / alt[0])
                        - pl.unit_price_minor
                    )
                    < abs(inv_line.unit_price_minor - pl.unit_price_minor)
                ):
                    norm = alt
            if norm is None:
                self._hold(
                    invoice_id,
                    v,
                    "R2b",
                    "uom_incompatible",
                    ctx,
                    refs=[invoice_id, v.invoice_number, po.id],
                    severity="action",
                    question=f"Invoice {v.invoice_number} bills {inv_line.qty} {uom} on {po.id} line {pl.line_no} ({pl.uom}); units are incompatible. Decide?",
                    context="UoM normalisation: kg and m must match the PO unit.",
                    materiality=v.gross_minor,
                    currency=v.currency,
                )
            qty_po, _ = norm
            factor = inv_line.qty / qty_po if qty_po else 1
            unit_price_po = (
                round(inv_line.unit_price_minor * factor)
                if factor != 1
                else inv_line.unit_price_minor
            )
            if qty_po and inv_line.line_total_minor and inv_line.qty == qty_po:
                unit_price_po = inv_line.unit_price_minor
            items.append(
                LineItem(
                    inv_line=inv_line,
                    po=po,
                    po_line=pl,
                    qty_po_units=qty_po,
                    unit_price_po_units=unit_price_po,
                )
            )
            used.add((po.id, pl.line_no))
        pos = sorted({it.po.id: it.po for it in items}.values(), key=lambda p: p.id)
        if items and len(items) < len(v.lines):
            self._hold(
                invoice_id,
                v,
                "R2b",
                "unmatched_lines",
                ctx,
                refs=[invoice_id, v.invoice_number, *[p.id for p in pos]],
                severity="action",
                question=f"Invoice {v.invoice_number}: {len(v.lines) - len(items)} of {len(v.lines)} lines match no purchase-order line. Decide?",
                context="Lines without a PO line cannot be valued against a price list.",
                materiality=v.gross_minor,
                currency=v.currency,
            )
        if pos:
            facts["po_ids"] = [p.id for p in pos]
            if not facts.get("po_logged"):
                ctx.act(
                    action_type="matched",
                    rule_id=None,
                    refs=[invoice_id, v.invoice_number, vendor_id, *[p.id for p in pos]],
                    inputs={
                        "po_references_as_printed": v.po_references,
                        "parsed_refs": refs,
                        "lines": [
                            [
                                it.inv_line.sku,
                                it.po.id,
                                it.po_line.line_no,
                                it.qty_po_units,
                                it.po_line.uom,
                            ]
                            for it in items
                        ],
                    },
                    decision={"po_ids": [p.id for p in pos], "line_count": len(items)},
                )
                facts["po_logged"] = True
            self.state.set_invoice(
                invoice_id, "matched", rule_id=None, reason=None, now=ctx.now, facts=facts
            )
        return items, pos

    def _po_open_qty(self, po: PurchaseOrder) -> int:
        return sum(max(0, ln.qty - self.state.qty_posted(po.id, ln.line_no)) for ln in po.lines)

    def _no_po(
        self,
        invoice_id: str,
        v: ValidatedInvoice,
        supplier: Supplier,
        vendor_id: str,
        envelope_event_id: str,
        facts: dict[str, Any],
        ctx: Context,
    ) -> None:
        text = (
            " ".join(ln.description.lower() for ln in v.lines)
            + " "
            + " ".join((ln.sku or "") for ln in v.lines)
        )
        freight_like = (
            supplier.category == "freight"
            or any(w in text for w in FREIGHT_WORDS)
            or any((ln.sku or "").upper().startswith("FR-") for ln in v.lines)
        )
        if freight_like:
            self._post_freight(invoice_id, v, supplier, vendor_id, envelope_event_id, facts, ctx)
            return
        reply = self.state.last_reply(invoice_id, "po_query")
        if reply is None:
            if self.state.open_waits(invoice_id):
                raise Held
            self._ask(
                ctx,
                invoice_id,
                v,
                kind="po_query",
                to_person=supplier.purchasing_contact,
                po_id=None,
                line_no=None,
                sku=None,
                qty=None,
                amount=v.gross_minor,
                currency=v.currency,
                extra_refs=[],
                text=f"Invoice {v.invoice_number} from {supplier.legal_name} ({format_minor(v.gross_minor, v.currency)}) references no purchase order and matches no open PO line. Which PO is this?",
                rule_id="R13",
            )
            raise Held
        self._hold(
            invoice_id,
            v,
            "R13",
            "no_purchase_order",
            ctx,
            refs=[invoice_id, v.invoice_number, vendor_id],
            severity="action",
            question=f"Invoice {v.invoice_number} has no purchase order and the buyer could not place it. Approve, reject or hold?",
            context=f"Buyer reply: {reply.get('body', '')}",
            materiality=v.gross_minor,
            currency=v.currency,
        )

    # ------------------------------------------------------------------ receipts (R3, M5, M6)

    def _apply_receipts(
        self, invoice_id: str, v: ValidatedInvoice, items: list[LineItem], ctx: Context
    ) -> None:
        self._consume_replies(invoice_id, v, items, ctx)
        for it in items:
            it.receipts = self.receipts_for(it.po.id, it.po_line.line_no)
            received = sum(r.qty_po_units for r in it.receipts)
            posted = self.state.qty_posted(it.po.id, it.po_line.line_no, exclude_invoice=invoice_id)
            it.qty_available = max(0, received - posted)
            if it.qty_available > 0:
                continue
            pending = [
                w
                for w in self.state.open_waits(invoice_id)
                if w.po_id == it.po.id and w.line_no == it.po_line.line_no
            ]
            if pending:
                raise Held
            self._ask_warehouse(invoice_id, v, it, ctx)
            raise Held

    def _ask_warehouse(
        self, invoice_id: str, v: ValidatedInvoice, it: LineItem, ctx: Context
    ) -> None:
        po_receipts = self.receipts_on_po(it.po.id)
        wrong_unit = [
            gr
            for gr, _ in self._receipts
            if gr.po_id == it.po.id
            and gr.line_no == it.po_line.line_no
            and gr.uom != it.po_line.uom
        ]
        other_line = [
            r
            for r in po_receipts
            if r.line_no != it.po_line.line_no
            and r.qty_po_units == it.qty_po_units
            and not self.state.qty_posted(it.po.id, r.line_no)
            and next(ln for ln in it.po.lines if ln.line_no == r.line_no).sku != it.po_line.sku
        ]
        extra_refs: list[str] = []
        if wrong_unit:
            gr = wrong_unit[0]
            detail = f"{gr.id} is booked as {gr.qty} {gr.uom} but the PO line is in {it.po_line.uom}; please confirm the unit."
            extra_refs = [gr.id]
            kind = "uom_query"
        elif other_line:
            r = other_line[0]
            detail = f"{r.gr.id} is booked on line {r.line_no} ({r.qty_po_units} units) but the invoice bills {it.po_line.sku} on line {it.po_line.line_no}; is that the same delivery?"
            extra_refs = [r.gr.id]
            kind = "wrong_line_query"
        else:
            detail = ""
            kind = "receipt_wait"
        self._ask(
            ctx,
            invoice_id,
            v,
            kind=kind,
            to_person=WAREHOUSE_CONTACT,
            po_id=it.po.id,
            line_no=it.po_line.line_no,
            sku=it.po_line.sku,
            qty=it.qty_po_units,
            amount=None,
            currency=None,
            extra_refs=extra_refs,
            rule_id="R3",
            text=self.llm.draft_message(
                "warehouse_query",
                {
                    "to_name": "Jonas",
                    "invoice_number": v.invoice_number,
                    "qty_billed": it.qty_po_units,
                    "sku": it.po_line.sku,
                    "po_id": it.po.id,
                    "line_no": it.po_line.line_no,
                    "detail": detail,
                },
            ),
        )

    def _consume_replies(
        self, invoice_id: str, v: ValidatedInvoice, items: list[LineItem], ctx: Context
    ) -> None:
        """Apply the structured facts of colleague replies (the ``refs`` tokens of sim/people.py):
        a confirmed/reassigned/unit-corrected receipt moves to the invoiced line; a bare quantity
        that contradicts the invoice is a wrong-PO answer and escalates; nothing_received waits."""
        for wait in self.state.open_waits(invoice_id):
            reply = wait.reply
            if (
                wait.kind not in ("receipt_wait", "wrong_line_query", "uom_query")
                or not reply
                or reply.get("consumed")
            ):
                continue
            self.state.record_reply(wait.id, {**reply, "consumed": True})
            refs = [
                str(r) for r in reply.get("refs", [])
            ]  # query refs first, then the reply's own tokens
            tokens = {r.split(":", 1)[0]: r.split(":", 1)[1] for r in refs if ":" in r}
            gr_ids = [r for r in refs if r.startswith("GR-")]
            gr_id = gr_ids[-1] if gr_ids else None
            confirming = any(t in refs for t in ("reassign", "unit_corrected", "confirmed"))
            item = next(
                (
                    it
                    for it in items
                    if it.po.id == wait.po_id and it.po_line.line_no == wait.line_no
                ),
                None,
            )
            if item is None:
                continue
            if confirming and gr_id:
                gr = next((g for g, _ in self._receipts if g.id == gr_id), None)
                if gr is not None and (
                    gr.line_no != item.po_line.line_no or gr.uom != item.po_line.uom
                ):
                    qty = int(tokens["qty"]) if "qty" in tokens else gr.qty
                    uom = tokens.get("uom") if "unit_corrected" in refs else None
                    self.state.reassign_receipt(
                        gr_id,
                        item.po.id,
                        gr.line_no,
                        item.po_line.line_no,
                        uom=uom,
                        qty=qty,
                        invoice_id=invoice_id,
                        now=ctx.now,
                    )
                    ctx.act(
                        action_type="matched",
                        rule_id="R3",
                        refs=[
                            invoice_id,
                            v.invoice_number,
                            item.inv_line.sku or item.po_line.sku,
                            item.po.id,
                            gr_id,
                        ],
                        inputs={
                            "reassigned_receipts": [gr_id],
                            "from_line": gr.line_no,
                            "to_line": item.po_line.line_no,
                            "uom": uom or gr.uom,
                            "reply": reply.get("body", ""),
                        },
                        decision={
                            "receipt_reassigned": True,
                            "confirmed_by": reply.get("person_id"),
                        },
                    )
            elif "qty" in tokens and "nothing_received" not in refs:
                answered = int(tokens["qty"])
                if answered != item.qty_po_units:
                    self._hold(
                        invoice_id,
                        v,
                        "R3",
                        "warehouse_quantity_mismatch",
                        ctx,
                        refs=[invoice_id, v.invoice_number, item.po.id, *refs],
                        severity="action",
                        question=f"Warehouse reports {answered} units for {item.po.id} but invoice bills {item.qty_po_units} on line {item.po_line.line_no}. Decide?",
                        context=f"Reply from {reply.get('person_id')}: {reply.get('body', '')}",
                        materiality=None,
                        currency=None,
                    )

    def _close_waits(self, invoice_id: str, ctx: Context, outcome: str) -> None:
        for w in self.state.open_waits(invoice_id):
            self.state.resolve_wait(w.id, now=ctx.now, reply={**(w.reply or {}), "closed": outcome})

    # ------------------------------------------------------------------ prices (M3, R2)

    def _apply_prices(
        self,
        invoice_id: str,
        v: ValidatedInvoice,
        supplier: Supplier,
        items: list[LineItem],
        ctx: Context,
    ) -> None:
        current, candidates = applicable_price_list(supplier.price_lists, v.invoice_date)
        if current is None:
            names = [c.id for c in candidates]
            self._hold(
                invoice_id,
                v,
                "R2b",
                "price_list_gap",
                ctx,
                refs=[invoice_id, v.invoice_number, supplier.id, *names],
                severity="action",
                question=f"Invoice {v.invoice_number} dated {v.invoice_date} falls between price lists {', '.join(names)}. Which list applies?",
                context=f"M3: no price list of {supplier.legal_name} covers {v.invoice_date}; candidates: {', '.join(names)}.",
                materiality=v.gross_minor,
                currency=v.currency,
            )
        # The reference price is the agreed PO line price (amendments included): the buyer placed
        # the order from the list valid on the order date, and a renegotiated PO price is the
        # contract. The list at the invoice date is consulted only for the M3 gap above.
        for it in items:
            it.list_price = it.po_line.unit_price_minor
            it.variance_bp = variance_bp(it.unit_price_po_units, it.list_price)
        worst = max(items, key=lambda it: it.variance_bp)
        if not price_within_tolerance(worst.variance_bp):
            self._hold(
                invoice_id,
                v,
                "R2b",
                "price_variance",
                ctx,
                refs=[invoice_id, v.invoice_number, supplier.id, worst.po.id],
                severity="action",
                question=f"Invoice {v.invoice_number}: {worst.po_line.sku} billed at {worst.unit_price_po_units} vs the agreed PO price {worst.list_price} ({worst.variance_bp} bp). Approve the price?",
                context=f"R2b: variance {worst.variance_bp} bp exceeds the 200 bp tolerance on {worst.po.id} line {worst.po_line.line_no}.",
                materiality=abs(worst.unit_price_po_units - worst.list_price) * worst.qty_po_units,
                currency=v.currency,
            )

    # ------------------------------------------------------------------ posting

    def _post_invoice(
        self,
        invoice_id: str,
        v: ValidatedInvoice,
        supplier: Supplier,
        vendor_id: str,
        items: list[LineItem],
        pos: list[PurchaseOrder],
        envelope_event_id: str,
        facts: dict[str, Any],
        ctx: Context,
    ) -> None:
        self._refresh()
        short = False
        for it in items:
            it.qty_to_post, it.balance = short_pay(it.qty_po_units, it.qty_available)
            short = short or it.balance > 0
        lines: list[JournalLineDraft] = []
        net_posted = 0
        for it in items:
            amount = (
                it.inv_line.line_total_minor
                if it.qty_to_post == it.qty_po_units
                else it.qty_to_post * it.unit_price_po_units
            )
            net_posted += amount
            lines.append(
                JournalLineDraft(
                    account=self._expense_account(it.po_line.sku, supplier),
                    debit_minor=amount,
                    currency=v.currency,
                )
            )
        treatment = treatment_for_country(supplier.country)
        full = not short and net_posted == v.net_minor
        vat_disputed = 0
        rounding = 0
        rule = "R2a" if max(it.variance_bp for it in items) > 0 else None
        if treatment == "domestic":
            if full:
                expected = expected_vat_minor(v.net_minor, "domestic")
                delta = rounding_delta(expected, v.vat_minor)
                if delta is None:
                    self._hold(
                        invoice_id,
                        v,
                        "R0",
                        "vat_mismatch",
                        ctx,
                        refs=[invoice_id, v.invoice_number, vendor_id],
                        severity="action",
                        question=f"Invoice {v.invoice_number} prints VAT {format_minor(v.vat_minor, v.currency)} but 19 % of {format_minor(v.net_minor, v.currency)} is {format_minor(expected, v.currency)}. Decide?",
                        context="Domestic VAT differs from 19 % by more than the rounding tolerance.",
                        materiality=abs(v.vat_minor - expected),
                        currency=v.currency,
                    )
                vat_in = expected
                rounding = delta
            else:
                vat_in = self_assessed_vat(net_posted)
            for account, debit, credit in vat_lines(net_posted, "domestic", vat_in):
                lines.append(
                    JournalLineDraft(
                        account=account, debit_minor=debit, credit_minor=credit, currency=v.currency
                    )
                )
            ap_total = net_posted + vat_in + rounding
        else:
            if is_vat_wrongly_charged(treatment, v.vat_minor):
                vat_disputed = v.vat_minor
                rule = "R11"
            for account, debit, credit in vat_lines(net_posted, treatment, 0):
                lines.append(
                    JournalLineDraft(
                        account=account, debit_minor=debit, credit_minor=credit, currency=v.currency
                    )
                )
            ap_total = net_posted
        if rounding:
            lines.append(
                JournalLineDraft(
                    account=FX_ROUNDING_ACCOUNT,
                    debit_minor=rounding if rounding > 0 else 0,
                    credit_minor=-rounding if rounding < 0 else 0,
                    currency=v.currency,
                )
            )
            rule = rule or "R12"
        due_minor, retained = (
            retention_split(ap_total, v.currency, v.retention_bp)
            if v.retention_bp
            else (ap_total, 0)
        )
        lines.append(
            JournalLineDraft(account=AP_ACCOUNT, credit_minor=due_minor, currency=v.currency)
        )
        if retained:
            lines.append(
                JournalLineDraft(
                    account=AP_RETENTION_ACCOUNT, credit_minor=retained, currency=v.currency
                )
            )
            rule = "R15"
        if short:
            rule = "R1"
        evidence = [Evidence(event_id=envelope_event_id, role="invoice")]
        for it in items:
            for r in it.receipts:
                if Evidence(event_id=r.event_id, role="receipt") not in evidence:
                    evidence.append(Evidence(event_id=r.event_id, role="receipt"))
        for po in pos:
            ev_id = self._po_events.get(po.id)
            if ev_id and Evidence(event_id=ev_id, role="po") not in evidence:
                evidence.append(Evidence(event_id=ev_id, role="po"))
        # R7: reverse open accruals for the lines being invoiced
        accrual_ids: list[str] = []
        reversal_ids: list[str] = []
        for it in items:
            for acc in self.state.open_accruals(it.po.id, it.po_line.line_no):
                if acc.entry_id in accrual_ids:
                    continue
                res = ctx.act(
                    action_type="posted",
                    rule_id="R7",
                    refs=[invoice_id, v.invoice_number, it.po.id, acc.entry_id],
                    inputs={
                        "reverses_accrual": acc.entry_id,
                        "po_id": it.po.id,
                        "line_no": it.po_line.line_no,
                        "month": acc.month,
                    },
                    decision={
                        "reversal_of": acc.entry_id,
                        "reason": "invoice arrived for an accrued receipt",
                    },
                    policy_action=ReverseAction(
                        entry_id=acc.entry_id,
                        cause_type="accrual_reversal",
                        cause_id=invoice_id,
                        effective_date=ctx.now.date(),
                        extra_evidence=[Evidence(event_id=envelope_event_id, role="invoice")],
                    ),
                )
                reversal = res.result
                if isinstance(reversal, JournalEntry):
                    self.state.mark_accrual_reversed(acc.entry_id, reversal.id)
                    accrual_ids.append(acc.entry_id)
                    reversal_ids.append(reversal.id)
        description = f"Invoice {v.invoice_number} {supplier.legal_name}"
        if short:
            description += " (short-pay R1)"
        if accrual_ids:
            description += f" (reverses GRNI accrual {', '.join(accrual_ids)})"
        draft = JournalEntryDraft(
            effective_date=ctx.now.date(),
            description=description,
            cause_type="invoice",
            cause_id=invoice_id,
            lines=_merge_lines(lines),
            evidence=evidence,
        )
        decision: dict[str, Any] = {
            "post_minor": ap_total,
            "ap_minor": due_minor,
            "retention_minor": retained,
            "net_posted_minor": net_posted,
            "currency": v.currency,
            "treatment": treatment,
            "vat_disputed_minor": vat_disputed,
            "rounding_minor": rounding,
            "qty_posted": sum(it.qty_to_post for it in items),
            "qty_billed": sum(it.qty_po_units for it in items),
            "qty_received": sum(it.qty_available for it in items),
            "variance_bp": max(it.variance_bp for it in items),
            "prepayment_minor": v.prepayment_minor or 0,
        }
        inputs: dict[str, Any] = {
            "lines": [
                [
                    it.po.id,
                    it.po_line.line_no,
                    it.po_line.sku,
                    it.qty_po_units,
                    it.qty_to_post,
                    it.unit_price_po_units,
                    it.list_price,
                ]
                for it in items
            ],
            "receipts": [r.gr.id for it in items for r in it.receipts],
            "accrual_entry_id": accrual_ids[0] if accrual_ids else None,
            "accrual_reversal_id": reversal_ids[0] if reversal_ids else None,
        }
        refs = [
            invoice_id,
            v.invoice_number,
            vendor_id,
            *[p.id for p in pos],
            *[r.gr.id for it in items for r in it.receipts],
        ]
        res = ctx.act(
            action_type="posted",
            rule_id=rule,
            refs=refs,
            inputs=inputs,
            decision=decision,
            policy_action=PostAction(draft=draft, posted_at=ctx.now),
        )
        entry = res.result
        assert isinstance(entry, JournalEntry)
        for it in items:
            self.state.record_consumption(
                it.po.id, it.po_line.line_no, invoice_id, it.qty_to_post, it.qty_po_units
            )
        self._payables(invoice_id, v, supplier, vendor_id, entry, due_minor, retained)
        self.state.set_invoice(
            invoice_id,
            "posted",
            rule_id=rule,
            reason=None,
            now=ctx.now,
            vendor_id=vendor_id,
            entry_id=entry.id,
            facts=facts,
        )
        self._close_waits(invoice_id, ctx, "posted")
        if vat_disputed:
            eur = self._eur(vat_disputed, v.currency, v.invoice_date)
            if eur > VAT_DISPUTE_ESCALATE_EUR_MINOR:
                self._packet(
                    ctx,
                    invoice_id,
                    f"{supplier.legal_name} charged VAT {format_minor(vat_disputed, v.currency)} on reverse-charge invoice {v.invoice_number}. Dispute?",
                    f"R11: intra-EU supply billed with VAT; posted net {format_minor(net_posted, v.currency)}, VAT disputed.",
                    "action",
                    [invoice_id, v.invoice_number, vendor_id],
                    vat_disputed,
                    v.currency,
                    [
                        PacketEvidence(
                            event_id=envelope_event_id, role="invoice", summary="invoice with VAT"
                        )
                    ],
                    recommendation="reject",
                    options=[
                        PacketOption(id="reject", label="Dispute the VAT with the supplier"),
                        PacketOption(id="approve", label="Accept and pay the VAT"),
                    ],
                )
        if short:
            self._short_pay_notice(invoice_id, v, supplier, items, ctx)
        self._forward_bank_details(invoice_id, v, supplier, vendor_id, envelope_event_id, ctx)

    def _payables(
        self,
        invoice_id: str,
        v: ValidatedInvoice,
        supplier: Supplier,
        vendor_id: str,
        entry: JournalEntry,
        due_minor: int,
        retained: int,
    ) -> None:
        terms = supplier.terms
        due_date = v.due_date or v.invoice_date + timedelta(days=TERM_DAYS.get(terms, 30))
        disc = discount_terms(terms) or discount_terms(v.payment_terms)
        self.ledger.create_payable(
            vendor_id=vendor_id,
            inv_id=invoice_id,
            entry_id=entry.id,
            amount_minor=due_minor,
            currency=v.currency,
            due_date=due_date,
            discount_deadline=(v.invoice_date + timedelta(days=disc[1])) if disc else None,
            discount_bp=disc[0] if disc else 0,
            retention_minor=0,
        )
        if retained:
            self.ledger.create_payable(
                vendor_id=vendor_id,
                inv_id=f"{invoice_id}:retention",
                entry_id=entry.id,
                amount_minor=retained,
                currency=v.currency,
                due_date=due_date + timedelta(days=90),
                status="held",
                held_reason="retention until tool acceptance (R15)",
            )

    def _short_pay_notice(
        self,
        invoice_id: str,
        v: ValidatedInvoice,
        supplier: Supplier,
        items: list[LineItem],
        ctx: Context,
    ) -> None:
        shorts = [it for it in items if it.balance > 0]
        short_minor = sum(it.balance * it.unit_price_po_units for it in shorts)
        posted_minor = sum(it.qty_to_post * it.unit_price_po_units for it in shorts)
        facts = {
            "supplier_name": supplier.legal_name,
            "invoice_number": v.invoice_number,
            "qty_billed": sum(it.qty_po_units for it in shorts),
            "sku": ", ".join(it.po_line.sku for it in shorts),
            "po_id": ", ".join(sorted({it.po.id for it in shorts})),
            "gr_ids": ", ".join(r.gr.id for it in shorts for r in it.receipts),
            "qty_received": sum(it.qty_available for it in shorts),
            "qty_posted": sum(it.qty_to_post for it in shorts),
            "amount_posted_minor": posted_minor,
            "amount_posted": str(Money(posted_minor, v.currency).as_decimal()),
            "currency": v.currency,
            "short_pay_minor": short_minor,
            "short_pay": str(Money(short_minor, v.currency).as_decimal()),
            "balance_qty": sum(it.balance for it in shorts),
        }
        message = self.llm.draft_message("short_pay_notice", facts)
        ctx.act(
            action_type="short_pay_notice",
            rule_id="R1",
            refs=[
                invoice_id,
                v.invoice_number,
                supplier.id,
                *[it.po.id for it in shorts],
                *[r.gr.id for it in shorts for r in it.receipts],
            ],
            inputs=facts,
            decision={
                "short_pay_minor": short_minor,
                "balance_qty": facts["balance_qty"],
                "message": message,
            },
        )

    def _forward_bank_details(
        self,
        invoice_id: str,
        v: ValidatedInvoice,
        supplier: Supplier,
        vendor_id: str,
        envelope_event_id: str,
        ctx: Context,
    ) -> None:
        b = v.bank_details
        if b is None or not (b.iban or b.account_number):
            return
        printed = (b.iban or b.account_number or "").replace(" ", "").upper()
        active = self.vendors.active_account(vendor_id)
        if active is not None and active.iban_or_acct.replace(" ", "").upper().endswith(printed):
            return
        ctx.emit(
            "vendor_message",
            {
                "message_id": f"inv-bank-{invoice_id}",
                "channel": "invoice",
                "vendor_id": vendor_id,
                "claimed_supplier_name": v.supplier_name,
                "from_address": supplier.email,
                "reply_to": None,
                "to": "ap-invoices@tbeu.example",
                "subject": f"Bank details printed on invoice {v.invoice_number}",
                "body": f"Invoice {v.invoice_number} prints bank details that differ from the vendor master: {b.iban or b.account_number} ({b.bank_name or ''}).",
                "eml_path": None,
                "in_reply_to": None,
                "thread_id": f"inv-bank-{invoice_id}",
                "attachments": [],
                "bank_details": {
                    "iban": b.iban,
                    "bic": b.bic,
                    "bank_name": b.bank_name,
                    "account_number": b.account_number,
                    "routing_number": b.routing_number,
                    "bank_code": b.bank_code,
                    "branch_code": b.branch_code,
                    "holder": v.supplier_name,
                },
            },
            source="invoice_agent",
            actor="invoice_agent",
        )

    # ------------------------------------------------------------------ freight (R13)

    def _post_freight(
        self,
        invoice_id: str,
        v: ValidatedInvoice,
        supplier: Supplier,
        vendor_id: str,
        envelope_event_id: str,
        facts: dict[str, Any],
        ctx: Context,
    ) -> None:
        eur_net = self._eur(v.net_minor, v.currency, v.invoice_date)
        if eur_net > FREIGHT_CONFIRM_MINOR:
            reply = self.state.last_reply(invoice_id, "buyer_confirmation")
            if reply is None:
                if self.state.open_waits(invoice_id):
                    raise Held
                buyer = supplier.purchasing_contact
                self._ask(
                    ctx,
                    invoice_id,
                    v,
                    kind="buyer_confirmation",
                    to_person=buyer,
                    po_id=None,
                    line_no=None,
                    sku=None,
                    qty=None,
                    amount=eur_net,
                    currency="EUR",
                    extra_refs=[],
                    rule_id="R13",
                    text=self.llm.draft_message(
                        "buyer_confirmation",
                        {
                            "to_name": buyer.split(".")[0].capitalize(),
                            "supplier_name": supplier.legal_name,
                            "invoice_number": v.invoice_number,
                            "amount_minor": v.net_minor,
                            "amount": str(Money(v.net_minor, v.currency).as_decimal()),
                            "currency": v.currency,
                            "description": "; ".join(ln.description for ln in v.lines),
                        },
                    ),
                )
                raise Held
            if "confirmed" not in reply.get("refs", []):
                self._hold(
                    invoice_id,
                    v,
                    "R13",
                    "freight_not_confirmed",
                    ctx,
                    refs=[invoice_id, v.invoice_number, vendor_id],
                    severity="action",
                    question=f"Freight invoice {v.invoice_number} ({format_minor(v.net_minor, v.currency)}) was not confirmed by the buyer. Approve or reject?",
                    context=f"R13: above EUR 2,000; buyer reply: {reply.get('body', '')}",
                    materiality=v.gross_minor,
                    currency=v.currency,
                )
        treatment = treatment_for_country(supplier.country)
        lines = [
            JournalLineDraft(
                account="Expenses:Freight", debit_minor=v.net_minor, currency=v.currency
            )
        ]
        vat_disputed = 0
        if treatment == "domestic":
            expected = expected_vat_minor(v.net_minor, "domestic")
            delta = rounding_delta(expected, v.vat_minor) or 0
            for account, debit, credit in vat_lines(v.net_minor, "domestic", expected):
                lines.append(
                    JournalLineDraft(
                        account=account, debit_minor=debit, credit_minor=credit, currency=v.currency
                    )
                )
            if delta:
                lines.append(
                    JournalLineDraft(
                        account=FX_ROUNDING_ACCOUNT,
                        debit_minor=delta if delta > 0 else 0,
                        credit_minor=-delta if delta < 0 else 0,
                        currency=v.currency,
                    )
                )
            ap_total = v.net_minor + expected + delta
        else:
            vat_disputed = v.vat_minor if is_vat_wrongly_charged(treatment, v.vat_minor) else 0
            for account, debit, credit in vat_lines(v.net_minor, treatment, 0):
                lines.append(
                    JournalLineDraft(
                        account=account, debit_minor=debit, credit_minor=credit, currency=v.currency
                    )
                )
            ap_total = v.net_minor
        lines.append(
            JournalLineDraft(account=AP_ACCOUNT, credit_minor=ap_total, currency=v.currency)
        )
        draft = JournalEntryDraft(
            effective_date=ctx.now.date(),
            description=f"Freight invoice {v.invoice_number} {supplier.legal_name} (no PO)",
            cause_type="invoice",
            cause_id=invoice_id,
            lines=_merge_lines(lines),
            evidence=[Evidence(event_id=envelope_event_id, role="invoice")],
        )
        res = ctx.act(
            action_type="posted",
            rule_id="R13",
            refs=[invoice_id, v.invoice_number, vendor_id],
            inputs={
                "freight": True,
                "eur_net_minor": eur_net,
                "confirmation_required": eur_net > FREIGHT_CONFIRM_MINOR,
            },
            decision={
                "post_minor": ap_total,
                "account": "Expenses:Freight",
                "vat_disputed_minor": vat_disputed,
                "treatment": treatment,
            },
            policy_action=PostAction(draft=draft, posted_at=ctx.now),
        )
        entry = res.result
        assert isinstance(entry, JournalEntry)
        self._payables(invoice_id, v, supplier, vendor_id, entry, ap_total, 0)
        self.state.set_invoice(
            invoice_id,
            "posted",
            rule_id="R13",
            reason=None,
            now=ctx.now,
            vendor_id=vendor_id,
            entry_id=entry.id,
            facts=facts,
        )
        self._close_waits(invoice_id, ctx, "posted")
        self._forward_bank_details(invoice_id, v, supplier, vendor_id, envelope_event_id, ctx)

    # ------------------------------------------------------------------ credit notes (R6)

    def _credit_note(
        self,
        invoice_id: str,
        v: ValidatedInvoice,
        vendor_id: str,
        envelope_event_id: str,
        ctx: Context,
    ) -> None:
        target = (v.credit_note_for or "").strip().upper()
        original_row = None
        for row in self.state.invoices(vendor_id=vendor_id):
            if (
                row.status == "posted"
                and row.invoice_number
                and row.invoice_number.strip().upper() == target
            ):
                original_row = row
        original = (
            self.ledger.get_entry(original_row.entry_id)
            if original_row and original_row.entry_id
            else None
        )
        if original is None or original_row is None:
            self._hold(
                invoice_id,
                v,
                "R6",
                "credit_note_without_original",
                ctx,
                refs=[invoice_id, v.invoice_number, vendor_id, target],
                severity="action",
                question=f"Credit note {v.invoice_number} references {v.credit_note_for}, which is not a posted invoice. Decide?",
                context="R6 needs a posted original.",
                materiality=abs(v.gross_minor),
                currency=v.currency,
            )
            raise Held
        orig_facts = self.state.facts(original_row.invoice_id) or {}
        ov = ValidatedInvoice.model_validate(orig_facts["validated"])
        cn_net, cn_vat, cn_gross = abs(v.net_minor), abs(v.vat_minor), abs(v.gross_minor)
        expense_lines = [
            ln
            for ln in original.lines
            if ln.account.startswith("Expenses:")
            and ln.account not in ("Expenses:VAT:Input", FX_ROUNDING_ACCOUNT)
            and ln.debit_minor
        ]
        shares = (
            allocate(Money(cn_net, v.currency), [ln.debit_minor for ln in expense_lines])
            if expense_lines
            else []
        )
        lines = [
            JournalLineDraft(account=ln.account, credit_minor=share.minor, currency=v.currency)
            for ln, share in zip(expense_lines, shares, strict=True)
            if share.minor
        ]
        vat_in = next((ln for ln in original.lines if ln.account == "Expenses:VAT:Input"), None)
        vat_out = next(
            (ln for ln in original.lines if ln.account == "Liabilities:VAT:Output"), None
        )
        if vat_out is not None and vat_in is not None:
            vat_rev = round(vat_in.debit_minor * cn_net / max(1, ov.net_minor))
            if vat_rev:
                lines.append(
                    JournalLineDraft(
                        account="Expenses:VAT:Input", credit_minor=vat_rev, currency=v.currency
                    )
                )
                lines.append(
                    JournalLineDraft(
                        account="Liabilities:VAT:Output", debit_minor=vat_rev, currency=v.currency
                    )
                )
        elif vat_in is not None and cn_vat:
            lines.append(
                JournalLineDraft(
                    account="Expenses:VAT:Input", credit_minor=cn_vat, currency=v.currency
                )
            )
        ap_lines = [
            ln
            for ln in original.lines
            if ln.account in (AP_ACCOUNT, AP_RETENTION_ACCOUNT) and ln.credit_minor
        ]
        ap_shares = allocate(Money(cn_gross, v.currency), [ln.credit_minor for ln in ap_lines])
        for ln, share in zip(ap_lines, ap_shares, strict=True):
            if share.minor:
                lines.append(
                    JournalLineDraft(
                        account=ln.account, debit_minor=share.minor, currency=v.currency
                    )
                )
        debits = sum(ln.debit_minor for ln in lines)
        credits = sum(ln.credit_minor for ln in lines)
        if debits != credits:
            diff = debits - credits
            lines.append(
                JournalLineDraft(
                    account=FX_ROUNDING_ACCOUNT,
                    debit_minor=-diff if diff < 0 else 0,
                    credit_minor=diff if diff > 0 else 0,
                    currency=v.currency,
                )
            )
        draft = JournalEntryDraft(
            effective_date=ctx.now.date(),
            description=f"Credit note {v.invoice_number} against {ov.invoice_number} (partial reversal of {original.id})",
            cause_type="credit_note",
            cause_id=invoice_id,
            lines=_merge_lines(lines),
            reverses=original.id,
            evidence=[
                Evidence(event_id=envelope_event_id, role="invoice"),
                *[e for e in original.evidence if e.role == "invoice"],
            ],
        )
        res = ctx.act(
            action_type="posted",
            rule_id="R6",
            refs=[
                invoice_id,
                v.invoice_number,
                ov.invoice_number,
                original_row.invoice_id,
                vendor_id,
                original.id,
            ],
            inputs={
                "credit_note_for": ov.invoice_number,
                "original_entry": original.id,
                "cn_net_minor": cn_net,
                "cn_gross_minor": cn_gross,
            },
            decision={"reversal_minor": cn_gross, "reverses": original.id, "proportional": True},
            policy_action=PostAction(draft=draft, posted_at=ctx.now),
        )
        entry = res.result
        assert isinstance(entry, JournalEntry)
        payable = next(
            (
                p
                for p in self.ledger.payables(vendor_id=vendor_id)
                if p.inv_id == original_row.invoice_id
            ),
            None,
        )
        if payable is not None and payable.status in ("open", "scheduled", "held"):
            remaining = payable.amount_minor - cn_gross
            if remaining > 0:
                self.ledger.set_payable_amount(payable.id, remaining)
            else:
                self.ledger.set_payable_amount(payable.id, 0)
                self.ledger.set_payable_status(payable.id, "netted", held_reason=None)
        else:
            self.ledger.create_payable(
                vendor_id=vendor_id,
                inv_id=invoice_id,
                entry_id=entry.id,
                amount_minor=-cn_gross,
                currency=v.currency,
                due_date=ctx.now.date(),
            )
        self.state.set_invoice(
            invoice_id,
            "posted",
            rule_id="R6",
            reason=None,
            now=ctx.now,
            vendor_id=vendor_id,
            entry_id=entry.id,
        )

    # ------------------------------------------------------------------ R7 month end

    def _accrue_month_end(self, month: str, ctx: Context) -> None:
        """R7 as an auto-reversing accrual per purchase order: every line received by the last
        day of ``month`` and not yet invoiced is accrued at PO price in one entry whose evidence
        is the receipt events. The first invoice for the PO reverses the whole entry; the next
        month end re-accrues whatever is still uninvoiced."""
        self._refresh()
        year, mon = int(month[:4]), int(month[5:7])
        last = date(year + (mon == 12), (mon % 12) + 1, 1) - timedelta(days=1)
        for po in sorted(self._pos.values(), key=lambda p: p.id):
            supplier = self.suppliers.get(po.supplier_id)
            if supplier is None:
                continue
            lines: list[JournalLineDraft] = []
            evidence: list[Evidence] = []
            accrued: list[tuple[int, int]] = []
            refs: list[str] = [po.id]
            total = 0
            for pl in po.lines:
                if self.state.accrual_for_month(po.id, pl.line_no, month) is not None:
                    continue
                receipts = [
                    r for r in self.receipts_for(po.id, pl.line_no) if r.gr.booked_at.date() <= last
                ]
                remaining = sum(r.qty_po_units for r in receipts) - self.state.qty_posted(
                    po.id, pl.line_no
                )
                if remaining <= 0:
                    continue
                amount = remaining * pl.unit_price_minor
                total += amount
                lines.append(
                    JournalLineDraft(
                        account=self._expense_account(pl.sku, supplier),
                        debit_minor=amount,
                        currency=po.currency,
                    )
                )
                for r in receipts:
                    ev = Evidence(event_id=r.event_id, role="receipt")
                    if ev not in evidence:
                        evidence.append(ev)
                    refs.append(r.gr.id)
                refs.append(pl.sku)
                accrued.append((pl.line_no, remaining))
            if not accrued:
                continue
            if self._po_events.get(po.id):
                evidence.append(Evidence(event_id=self._po_events[po.id], role="po"))
            lines.append(
                JournalLineDraft(account=GRNI_ACCOUNT, credit_minor=total, currency=po.currency)
            )
            units = sum(q for _, q in accrued)
            draft = JournalEntryDraft(
                effective_date=last,
                description=(
                    f"GRNI accrual {po.id} {supplier.legal_name}: {units} units on {len(accrued)} "
                    f"line(s) received, not invoiced, at PO price, month {month}"
                ),
                cause_type="accrual",
                cause_id=f"GRNI-{po.id}-{month}",
                evidence=evidence,
                lines=_merge_lines(lines),
            )
            res = ctx.act(
                action_type="posted",
                rule_id="R7",
                refs=list(dict.fromkeys(refs)),
                inputs={"po_id": po.id, "month": month, "lines": [[ln, q] for ln, q in accrued]},
                decision={"accrual_minor": total, "currency": po.currency, "qty": units},
                policy_action=PostAction(draft=draft, posted_at=ctx.now),
            )
            entry = res.result
            if isinstance(entry, JournalEntry):
                for line_no, qty in accrued:
                    self.state.add_accrual(po.id, line_no, month, entry.id, qty)

    # ------------------------------------------------------------------ waits, holds, packets

    def _ask(
        self,
        ctx: Context,
        invoice_id: str,
        v: ValidatedInvoice,
        *,
        kind: str,
        to_person: str,
        po_id: str | None,
        line_no: int | None,
        sku: str | None,
        qty: int | None,
        amount: int | None,
        currency: str | None,
        extra_refs: list[str],
        text: str,
        rule_id: str,
    ) -> None:
        query_kind = (
            "buyer_confirmation"
            if kind in ("buyer_confirmation", "po_query")
            else "warehouse_query"
        )
        refs = [invoice_id, v.invoice_number, *([po_id] if po_id else []), *extra_refs]
        query = ColleagueQuery(
            kind=query_kind,
            to_person=to_person,
            text=text,
            invoice_id=invoice_id,
            invoice_number=v.invoice_number,
            po_id=po_id,
            line_no=line_no,
            sku=sku,
            qty_billed=qty,
            amount_minor=amount,
            currency=currency,
            refs=refs,
        )
        res = ctx.act(
            action_type="asked_colleague",
            rule_id=rule_id,
            refs=refs,
            inputs={"kind": kind, "query": query.model_dump()},
            decision={"to_person": to_person, "wait_hours": WAREHOUSE_WAIT_HOURS, "message": text},
        )
        self.state.add_wait(
            kind=kind,
            invoice_id=invoice_id,
            po_id=po_id,
            line_no=line_no,
            asked_event_id=res.event.id,
            deadline=ctx.now + timedelta(hours=WAREHOUSE_WAIT_HOURS),
            now=ctx.now,
        )
        self.state.set_invoice(
            invoice_id, "held", rule_id=rule_id, reason=f"waiting_for:{to_person}", now=ctx.now
        )
        ctx.ask(query, asked_event_id=res.event.id)

    def _expire_waits(self, ctx: Context) -> None:
        for wait in self.state.open_waits():
            if wait.deadline > ctx.now:
                continue
            self.state.resolve_wait(wait.id, now=ctx.now, reply={"expired": True, "consumed": True})
            facts = self.state.facts(wait.invoice_id) or {}
            number = str((facts.get("validated") or {}).get("invoice_number", wait.invoice_id))
            self.state.set_invoice(
                wait.invoice_id,
                "escalated",
                rule_id="R3",
                reason="no_reply_within_48h",
                now=ctx.now,
            )
            self._packet(
                ctx,
                wait.invoice_id,
                f"Invoice {number}: no goods receipt and no answer from the warehouse within 48 hours. Decide?",
                f"R3: asked about {wait.po_id} line {wait.line_no}; the wait expired at {wait.deadline.isoformat()}.",
                "action",
                [wait.invoice_id, number, *([wait.po_id] if wait.po_id else [])],
                None,
                None,
                [],
            )

    def _hold(
        self,
        invoice_id: str,
        v: ValidatedInvoice,
        rule_id: str,
        reason: str,
        ctx: Context,
        *,
        refs: list[str],
        severity: str,
        question: str,
        context: str,
        materiality: int | None,
        currency: str | None,
    ) -> None:
        self.state.set_invoice(invoice_id, "held", rule_id=rule_id, reason=reason, now=ctx.now)
        self._close_waits(invoice_id, ctx, reason)
        ctx.act(
            action_type="held",
            rule_id=rule_id,
            refs=refs,
            inputs={"reason": reason},
            decision={
                "reason": reason,
                "status": "held",
                "candidates": [r for r in refs if r.startswith("PL-")],
            },
        )
        self._packet(ctx, invoice_id, question, context, severity, refs, materiality, currency, [])
        raise Held

    def _packet(
        self,
        ctx: Context,
        invoice_id: str,
        question: str,
        context: str,
        severity: str,
        refs: list[str],
        materiality: int | None,
        currency: str | None,
        evidence: list[PacketEvidence],
        *,
        recommendation: str = "hold",
        options: list[PacketOption] | None = None,
    ) -> None:
        envelope = self._envelope(invoice_id)
        ev = list(evidence)
        if envelope is not None and not any(e.event_id == envelope.id for e in ev):
            ev.append(
                PacketEvidence(
                    event_id=envelope.id, role="invoice", summary=f"invoice document {invoice_id}"
                )
            )
        opts = options or OPTIONS_HOLD
        self.packets.create_packet(
            PacketDraft(
                agent=self.principal,
                question=question,
                context=context,
                severity=severity,
                refs=list(dict.fromkeys(refs)),
                evidence=ev,  # type: ignore[arg-type]
                materiality_minor=materiality,
                currency=currency if currency in ("EUR", "CZK", "PLN", "USD", "JPY") else None,
                recommendation=recommendation,
                recommendation_reason="the rule holds the invoice until a human decides",
                options=opts,
                expires_at=ctx.now + timedelta(days=5),
                consequence_of_silence="The invoice stays on hold; nothing is paid.",
            ),
            created_at=ctx.now,
        )

    # ------------------------------------------------------------------ helpers

    def _expense_account(self, sku: str, supplier: Supplier) -> str:
        up = sku.upper()
        if up.startswith("TL-"):
            return "Expenses:Tooling"
        if up.startswith("FR-"):
            return "Expenses:Freight"
        return expense_account_for_category(supplier.category)

    def _eur(self, minor: int, currency: str, on: date) -> int:
        if currency == "EUR":
            return minor
        try:
            return convert(Money(minor, currency), "EUR", on, self.ledger._fx).money.minor
        except (FxRateUnavailable, ValueError):
            return minor


def _distinct_supplies(a: ValidatedInvoice, b: ValidatedInvoice) -> bool:
    """Two invoices that name different purchase orders, or bill disjoint SKUs, are different
    supplies even when their totals and dates coincide (R4 needs the same supply billed twice)."""
    pos_a, pos_b = set(parse_po_refs(a.po_references)), set(parse_po_refs(b.po_references))
    if pos_a and pos_b and not pos_a & pos_b:
        return True
    skus_a = {ln.sku.upper() for ln in a.lines if ln.sku}
    skus_b = {ln.sku.upper() for ln in b.lines if ln.sku}
    return bool(skus_a and skus_b and not skus_a & skus_b)


def _merge_lines(lines: list[JournalLineDraft]) -> list[JournalLineDraft]:
    """One line per (account, currency): debits and credits on the same account net; zero lines drop."""
    merged: dict[tuple[str, str], int] = {}
    for ln in lines:
        key = (ln.account, ln.currency)
        merged[key] = merged.get(key, 0) + ln.debit_minor - ln.credit_minor
    out: list[JournalLineDraft] = []
    for (account, currency), net in merged.items():
        if net:
            out.append(
                JournalLineDraft(
                    account=account,
                    debit_minor=net if net > 0 else 0,
                    credit_minor=-net if net < 0 else 0,
                    currency=currency,
                )
            )
    return out


def _canonical_uom(uom: str, po_uom: str) -> str:
    u = uom.strip().lower()
    if u in ("pc", "pcs", "ks", "szt.", "szt", "stk.", "stk", "個", "piece", "pieces", "ea"):
        return "pc"
    if "50" in u or u in ("box", "boxes", "karton", "bal.", "opak.", "箱(50)", "bal", "opak"):
        return "box50"
    if u in ("kg", "kilogram", "kilograms"):
        return "kg"
    if u in ("m", "meter", "metre", "meters", "metres"):
        return "m"
    return po_uom
