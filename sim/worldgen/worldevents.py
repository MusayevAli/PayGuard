"""Assemble world events (spec §5.1 types) from the materialised world, ordered by known_at,
with ids ``ev_NNNNNN`` assigned in that order."""

from __future__ import annotations

import random
from dataclasses import dataclass, field
from datetime import UTC, datetime, time, timedelta

from sim.models import Event, Invoice, InvoiceFormat, POChange, PurchaseOrder
from sim.mt940 import Statement, write_mt940
from sim.worldgen.catalog import BANK_ACCOUNTS
from sim.worldgen.materialize import World
from sim.worldgen.messages import MessageRecord

TYPE_RANK = {
    "po_created": 0,
    "po_amended": 1,
    "goods_received": 2,
    "invoice_received": 3,
    "vendor_message": 4,
    "colleague_reply": 5,
    "bank_statement": 6,
    "bank_statement_line": 7,
}
BANK_BIC = {"EUR": "INGDDEFFXXX", "CZK": "KOMBCZPPXXX", "PLN": "INGBPLPWXXX"}
TWO_PAGE = {"de_rechnung_two_page"}


@dataclass
class EventIndex:
    by_key: dict[str, str] = field(default_factory=dict)


@dataclass
class Pending:
    key: str
    type: str
    event_time: datetime
    known_at: datetime
    source: str
    actor: str | None
    payload: dict


def build_events(
    seed: int,
    world: World,
    invoices: list[Invoice],
    messages: list[MessageRecord],
    statements: list[Statement],
    statement_paths: dict[str, str],
    doc_sizes: dict[str, int],
) -> tuple[list[Event], EventIndex]:
    rng = random.Random(seed ^ 0x5EED)
    pending: list[Pending] = []
    plans_by_id = {po.po_id: po for po in world.po_plans}
    for po in world.pos:
        pending.append(
            Pending(
                f"po:{po.id}",
                "po_created",
                po.created_at,
                po.created_at,
                "erp",
                po.buyer,
                {"po": po.model_dump(mode="json")},
            )
        )
        plan = plans_by_id[po.id]
        if plan.amendment is not None:
            when = world.amendment_times[po.id]
            a = plan.amendment
            lines = [
                ln.model_copy(update={a.field: a.new_value})
                if ln.line_no == a.line_index + 1
                else ln
                for ln in po.lines
            ]
            after = PurchaseOrder(
                **{**po.model_dump(), "lines": [ln.model_dump() for ln in lines], "version": 2}
            )
            pending.append(
                Pending(
                    f"amend:{po.id}",
                    "po_amended",
                    when,
                    when,
                    "erp",
                    po.buyer,
                    {
                        "po_id": po.id,
                        "amendment_no": 1,
                        "reason": a.reason,
                        "changes": [
                            POChange(
                                line_no=a.line_index + 1,
                                field=a.field,
                                old=a.old_value,
                                new=a.new_value,
                            ).model_dump()
                        ],
                        "po_after": after.model_dump(mode="json"),
                    },
                )
            )
    for gr in world.receipts:
        pending.append(
            Pending(
                f"gr:{gr.id}",
                "goods_received",
                gr.received_at,
                gr.booked_at,
                "warehouse",
                gr.dock_clerk,
                {"receipt": gr.model_dump(mode="json")},
            )
        )
    drafts = {d.id: d for d in world.drafts}
    for inv in invoices:
        d = drafts[inv.id]
        known = d.plan.known_at
        event_time = known
        sender: str | None
        hint: str | None
        if inv.format == InvoiceFormat.PNG:
            event_time = known - timedelta(hours=rng.randrange(2, 30), minutes=rng.randrange(60))
            sender, hint = None, None
        elif inv.format == InvoiceFormat.EML:
            sender, hint = (d.supplier.email, None)
        elif inv.format == InvoiceFormat.PDF:
            sender, hint = (d.supplier.email, inv.supplier_id)
        else:
            sender, hint = (inv.supplier_id or d.supplier.email, inv.supplier_id)
        pages = (
            None
            if inv.format == InvoiceFormat.STRUCTURED
            else (2 if inv.template_id in TWO_PAGE else 1)
        )
        source = {"edi": "edi", "portal": "portal", "email": "email", "scan": "scan"}[
            inv.received_via
        ]
        actor = sender if sender else "mailroom"
        pending.append(
            Pending(
                f"inv:{inv.id}",
                "invoice_received",
                event_time,
                known,
                source,
                actor,
                {
                    "invoice_id": inv.id,
                    "format": inv.format.value,
                    "document_path": inv.document_path,
                    "received_via": inv.received_via,
                    "sender": sender,
                    "supplier_hint": hint,
                    "attachments": [a.model_dump() for a in inv.attachments],
                    "size_bytes": doc_sizes[inv.id],
                    "sha256": inv.sha256,
                    "pages": pages,
                },
            )
        )
    for m in messages:
        pending.append(
            Pending(f"msg:{m.key}", m.type, m.known_at, m.known_at, m.source, m.actor, m.payload)
        )
    for stmt in statements:
        day = stmt.statement_date + timedelta(days=1)
        event_time = datetime.combine(day, time(5, 30), tzinfo=UTC)
        known = datetime.combine(day, time(6, 0), tzinfo=UTC)
        text = write_mt940(stmt)
        pending.append(
            Pending(
                f"stmt:{stmt.statement_id}",
                "bank_statement",
                event_time,
                known,
                "bank",
                BANK_BIC[stmt.currency],
                {
                    "statement_id": stmt.statement_id,
                    "account_iban": stmt.account_iban,
                    "currency": stmt.currency,
                    "statement_date": stmt.statement_date.isoformat(),
                    "sequence_no": stmt.sequence_no,
                    "path": statement_paths[stmt.statement_id],
                    "mt940": text,
                },
            )
        )
        for n, ln in enumerate(stmt.lines, start=1):
            pending.append(
                Pending(
                    f"stmtline:{stmt.statement_id}:{n}",
                    "bank_statement_line",
                    datetime.combine(ln.value_date, time(12, 0), tzinfo=UTC),
                    known,
                    "bank",
                    BANK_BIC[stmt.currency],
                    {
                        "statement_id": stmt.statement_id,
                        "account_iban": stmt.account_iban,
                        "currency": stmt.currency,
                        "line_no": n,
                        "value_date": ln.value_date.isoformat(),
                        "entry_date": ln.entry_date.isoformat() if ln.entry_date else None,
                        "dc": ln.dc,
                        "amount_minor": ln.amount_minor,
                        "txn_type": ln.txn_type,
                        "customer_ref": ln.customer_ref,
                        "bank_ref": ln.bank_ref,
                        "remittance": ln.remittance,
                    },
                )
            )
    pending.sort(key=lambda p: (p.known_at, TYPE_RANK[p.type], p.key))
    events: list[Event] = []
    index = EventIndex()
    for n, p in enumerate(pending, start=1):
        ev_id = f"ev_{n:06d}"
        payload = dict(p.payload)
        events.append(
            Event(
                id=ev_id,
                type=p.type,
                event_time=p.event_time,
                known_at=p.known_at,
                source=p.source,
                actor=p.actor,
                payload=payload,
            )
        )  # type: ignore[arg-type]
        index.by_key[p.key] = ev_id
    return events, index


def account_iban(currency: str) -> str:
    return BANK_ACCOUNTS[currency]["iban"]
