"""Synthetic mini-world for agent tests: suppliers from the generator, hand-built POs, receipts
and invoice documents, a fake LLM, the real policy/ledger/runner, and the scripted responders."""

from __future__ import annotations

import hashlib
import json
import os
import sys
from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path
from typing import Any

from agents.invoice_agent import InvoiceAgent
from agents.llm import template_explanation, template_message
from agents.parsed import ExtractionFailure, ParsedBankDetails, ParsedInvoice, ParsedLine
from agents.payment_agent import PaymentAgent
from agents.payment_state import PaymentState
from agents.runner import Runner
from agents.runtime import JumperFlag
from agents.state import AgentState
from bank.mock_bank import MockBank
from core.accounts import expense_account_for_category
from core.events import EventStore, make_event
from core.ledger import Evidence, JournalEntry, JournalEntryDraft, JournalLineDraft, Ledger, Payable
from core.packets import PacketStore
from core.policy import Policy, PostAction
from core.rules import BUTTON_THRESHOLD_MINOR, DAILY_CAP_EUR_MINOR, applicable_price_list
from core.vendors import VendorMaster
from signer.provisioning import DEV_CHIP_KEY, Provisioner
from signer.stub import StubSigner
from sim.clock import SimClock
from sim.fx import DayRates, FxTable, SourceWindow
from sim.models import Event, GoodsReceipt, POLine, PurchaseOrder, Supplier
from sim.people import Responders, roster
from sim.worldgen.catalog import BANK_ACCOUNTS, SKUS
from sim.worldgen.suppliers import build_suppliers

RETENTION_NOTE = "10 % retention on the invoice total, released after final tool acceptance."
START = datetime(2026, 10, 1, tzinfo=UTC)
SKU = {s.sku: s for s in SKUS}


def at(day: date, hour: int = 9, minute: int = 0) -> datetime:
    return datetime.combine(day, time(hour, minute), tzinfo=UTC)


def full_synthetic_fx() -> FxTable:
    rates: dict[str, DayRates] = {}
    day = date(2026, 8, 1)
    while day <= date(2026, 12, 31):
        rates[day.isoformat()] = DayRates(
            rates={
                "CZK": "24.5",
                "PLN": "4.3",
                "USD": "1.08",
                "JPY": "162",
                "GBP": "0.85",
                "CHF": "0.94",
                "HUF": "395",
            },
            carried_forward=False,
            source_date=day.isoformat(),
        )
        day += timedelta(days=1)
    return FxTable(
        base="EUR",
        source="synthetic",
        source_window=SourceWindow(start="2026-08-01", end="2026-12-31"),
        rates=rates,
    )


def money_text(minor: int, currency: str = "EUR") -> str:
    if currency == "JPY":
        return f"{minor:,}"
    sign = "-" if minor < 0 else ""
    return f"{sign}{abs(minor) // 100:,}.{abs(minor) % 100:02d}"


class FakeLLM:
    """Scripted extraction results keyed by document sha256; deterministic templates for prose."""

    def __init__(self) -> None:
        self.scripts: dict[str, list[ParsedInvoice | ExtractionFailure]] = {}
        self.calls: list[tuple[str, list[str] | None]] = []

    def extract_invoice(
        self, doc: Any, feedback: list[str] | None = None
    ) -> ParsedInvoice | ExtractionFailure:
        self.calls.append((doc.sha256, feedback))
        queue = self.scripts.get(doc.sha256)
        if not queue:
            return ExtractionFailure(
                reason="no scripted extraction for this document", errors=[], attempts=1
            )
        return queue.pop(0) if len(queue) > 1 else queue[0]

    def extract_many(self, docs: list[Any]) -> None:
        return None

    def explain(self, decision: dict[str, Any]) -> str:
        return template_explanation(decision)

    def draft_message(self, kind: str, facts: dict[str, Any]) -> str:
        return template_message(kind, facts)


class Kit:
    def __init__(
        self,
        tmp_path: Path,
        *,
        now: datetime = START,
        payments: bool = False,
        bank_opening: dict[str, int] | None = None,
        button_threshold: int = BUTTON_THRESHOLD_MINOR,
        daily_cap: int = DAILY_CAP_EUR_MINOR,
    ) -> None:
        self.jumper = JumperFlag(False)
        self.payments_enabled = payments
        self.world = tmp_path / "world"
        (self.world / "invoices").mkdir(parents=True)
        self.suppliers: list[Supplier] = build_suppliers(42)
        self.by_id = {s.id: s for s in self.suppliers}
        self.db = tmp_path / "pg.db"
        self.store = EventStore(self.db)
        self.clock = SimClock(self.store, [], start=now, speed=1)
        self.ledger = Ledger(
            self.db,
            events=self.store,
            fx=full_synthetic_fx(),
            now=lambda: self.clock.sim_now,
            beancount_path=tmp_path / "ledger" / "payguard.beancount",
        )
        self.vendors = VendorMaster(self.db)
        self.vendors.seed_from_suppliers(self.suppliers, now=now)
        self.packets = PacketStore(self.db, events=self.store, now=lambda: self.clock.sim_now)
        self.policy = Policy(
            ledger=self.ledger,
            vendors=self.vendors,
            events=self.store,
            jumper_present=self.jumper,
            now=lambda: self.clock.sim_now,
        )
        self.state = AgentState(self.db)
        self.llm = FakeLLM()
        self.people = roster()
        self.responders = Responders(
            self.people, events_source=self.events_source, clock=self.clock, store=self.store
        )
        self.agent = InvoiceAgent(
            world_dir=self.world,
            store=self.store,
            ledger=self.ledger,
            vendors=self.vendors,
            packets=self.packets,
            state=self.state,
            llm=self.llm,
            suppliers=self.suppliers,
            now=lambda: self.clock.sim_now,
        )
        self.fx = full_synthetic_fx()
        self.signer = StubSigner(None, jumper_present=self.jumper, nonce_seed=5)
        self.bank = MockBank(
            self.db,
            store=self.store,
            key=DEV_CHIP_KEY,
            fx=self.fx,
            now=lambda: self.clock.sim_now,
            schedule=self.clock.schedule,
            out_dir=tmp_path,
            opening=bank_opening,
        )
        self.pay_state = PaymentState(self.db)
        self.provisioner = Provisioner(
            self.db,
            signer=self.signer,
            vendors=self.vendors,
            policy=self.policy,
            store=self.store,
            now=lambda: self.clock.sim_now,
            jumper_present=self.jumper,
            key=DEV_CHIP_KEY,
        )
        self._button_threshold = button_threshold
        self._daily_cap = daily_cap
        self.payment_agent = PaymentAgent(
            store=self.store,
            ledger=self.ledger,
            vendors=self.vendors,
            packets=self.packets,
            state=self.pay_state,
            invoice_state=self.state,
            signer=self.signer,
            bank=self.bank,
            fx=self.fx,
            now=lambda: self.clock.sim_now,
            provisioner=self.provisioner,
        )
        agents: list[Any] = [self.agent] + ([self.payment_agent] if payments else [])
        self.runner = Runner(
            store=self.store,
            policy=self.policy,
            agents=agents,
            now=lambda: self.clock.sim_now,
            explainer=self.llm,
            messenger=self.responders,
        )
        self.runner.attach(self.clock)
        if payments:
            self.provision()
        self._po_n = 20000
        self._gr_n = 9000
        self._inv_n = 9000

    # -- payment desk helpers -----------------------------------------------------------------
    def provision(self, *vendor_ids: str) -> list[Event]:
        """Key, caps and the given vendors' accounts onto the stub with the jumper present."""
        self.jumper.present = True
        try:
            out = []
            if not self.signer.status().key_loaded:
                out.extend(
                    self.provisioner.load_key_and_caps(
                        daily_cap=self._daily_cap, button_threshold=self._button_threshold
                    )
                )
            out.extend(self.provisioner.enqueue(v) for v in vendor_ids)
            return out
        finally:
            self.jumper.present = False

    def payable(
        self,
        vendor_id: str,
        amount_minor: int,
        *,
        due: date,
        currency: str | None = None,
        discount_bp: int = 0,
        discount_deadline: date | None = None,
        number: str | None = None,
        inv_id: str | None = None,
        status: str = "open",
        held_reason: str | None = None,
    ) -> Payable:
        """An AP liability with its payable, posted as the invoice agent would (evidence: a controller decision event)."""
        sup = self.by_id[vendor_id]
        currency = currency or sup.currency
        self._inv_n += 1
        inv_id = inv_id or f"INV-{self._inv_n}"
        number = number or f"TST-{self._inv_n}"
        evidence_ev = self.store.append(
            make_event(
                "controller_decision",
                {
                    "packet_id": f"TEST-{inv_id}",
                    "decision": "test_payable",
                    "note": number,
                    "decided_by": "test",
                    "via": "api",
                },
                event_time=self.now,
                source="test",
                actor="test",
            )
        ).event
        expense = expense_account_for_category(sup.category)
        lines = (
            [
                JournalLineDraft(account=expense, debit_minor=amount_minor, currency=currency),
                JournalLineDraft(
                    account="Liabilities:AP", credit_minor=amount_minor, currency=currency
                ),
            ]
            if amount_minor >= 0
            else [
                JournalLineDraft(
                    account="Liabilities:AP", debit_minor=-amount_minor, currency=currency
                ),
                JournalLineDraft(account=expense, credit_minor=-amount_minor, currency=currency),
            ]
        )
        draft = JournalEntryDraft(
            effective_date=self.now.date(),
            description=f"Test invoice {number} {sup.legal_name}",
            cause_type="invoice" if amount_minor >= 0 else "credit_note",
            cause_id=inv_id,
            lines=lines,
            evidence=[Evidence(event_id=evidence_ev.id, role="decision")],
        )
        entry = self.policy.execute(PostAction(draft=draft, posted_at=self.now), "invoice_agent")
        self.state.set_invoice(
            inv_id,
            "posted",
            rule_id=None,
            reason=None,
            now=self.now,
            invoice_number=number,
            vendor_id=vendor_id,
            entry_id=entry.id,
        )
        return self.ledger.create_payable(
            vendor_id=vendor_id,
            inv_id=inv_id,
            entry_id=entry.id,
            amount_minor=amount_minor,
            currency=currency,
            due_date=due,
            discount_deadline=discount_deadline,
            discount_bp=discount_bp,
            status=status,
            held_reason=held_reason,
        )  # type: ignore[arg-type]

    def bank_line(
        self,
        currency: str,
        value_date: date,
        dc: str,
        amount_minor: int,
        *,
        customer_ref: str,
        remittance: str,
        txn_type: str = "NTRF",
        known_at: datetime | None = None,
        line_no: int = 1,
    ) -> Event:
        """A world-style statement line (known the next morning at 06:00 unless given)."""
        known = known_at or (at(value_date, 6) + timedelta(days=1))
        payload = {
            "statement_id": f"{currency}{value_date:%y%m%d}",
            "account_iban": BANK_ACCOUNTS[currency]["iban"],
            "currency": currency,
            "line_no": line_no,
            "value_date": value_date.isoformat(),
            "entry_date": value_date.isoformat(),
            "dc": dc,
            "amount_minor": amount_minor,
            "txn_type": txn_type,
            "customer_ref": customer_ref,
            "bank_ref": None,
            "remittance": remittance[:140],
        }
        return self._append(
            make_event(
                "bank_statement_line",
                payload,
                event_time=at(value_date, 12),
                known_at=known,
                source="bank",
                actor=BANK_ACCOUNTS[currency]["bic"],
            )
        )

    def decide(self, packet_id: str, decision: str) -> None:
        self.packets.decide_packet(
            packet_id, decision, None, "controller", via="phone", decided_at=self.now
        )
        self.run()

    def press_button(self) -> int:
        nonce = self.signer.press_button()
        self.runner.run_task(self.payment_agent, self.payment_agent.retry_waiting)
        return nonce

    def packets_open(self, kind: str | None = None) -> list[Event]:
        return [
            e
            for e in self.store.query(type="escalation", limit=100000)
            if kind is None or e.payload.get("kind") == kind
        ]

    def close(self) -> None:
        self.ledger.close()
        self.vendors.close()
        self.pay_state.close()
        self.bank.close()
        self.provisioner.close()
        self.packets.close()
        self.state.close()
        self.store.close()

    # -- time -------------------------------------------------------------------------------
    @property
    def now(self) -> datetime:
        return self.clock.sim_now

    def tick(self, *, hours: float = 0, days: float = 0) -> list[Event]:
        out = self.clock.advance_to(self.now + timedelta(hours=hours, days=days))
        self._debug(f"tick -> {self.now.isoformat()}")
        return out

    def run(self) -> int:
        n = self.runner.drain()
        self._debug("run")
        return n

    def _debug(self, label: str) -> None:
        if os.environ.get("PAYGUARD_KIT_DEBUG"):
            print(f"  --- {label}", file=sys.stderr)
            for e in self.store.query(type="agent_action", limit=100000):
                p = e.payload
                print(
                    f"  ACTION {p['action_type']:<18} {p['rule_id'] or '-':<4} refs={p['refs']} decision={json.dumps(p['decision'], default=str)[:300]}",
                    file=sys.stderr,
                )
            for e in self.store.query(type="escalation", limit=1000):
                print(
                    f"  PACKET refs={e.payload.get('refs')} {e.payload.get('question', '')[:160]}",
                    file=sys.stderr,
                )
            for row in self.state.invoices():
                print(
                    f"  STATE {row.invoice_id} {row.status} {row.rule_id} {row.reason}",
                    file=sys.stderr,
                )
            for e in self.store.query(type="system_error", limit=1000):
                print(
                    f"  SYSERR {e.payload.get('component')} {e.payload.get('code')} {e.payload.get('message', '')[:300]}",
                    file=sys.stderr,
                )

    def events_source(self) -> list[Event]:
        return self.store.query(limit=100000) + self.clock.pending_events()

    def _append(self, ev: Event) -> Event:
        if ev.known_at > self.now:
            self.clock.schedule(ev)
            return ev
        return self.store.append(ev).event

    # -- world building ---------------------------------------------------------------------
    def po(
        self,
        supplier_id: str,
        lines: list[tuple[str, int, int]],
        *,
        order_date: date = date(2026, 9, 10),
        plant: str = "DE",
        list_price: bool = True,
    ) -> PurchaseOrder:
        sup = self.by_id[supplier_id]
        self._po_n += 1
        po_lines = []
        for n, (sku_id, qty, price) in enumerate(lines, start=1):
            sku = SKU[sku_id]
            if list_price:
                current, _ = applicable_price_list(sup.price_lists, order_date)
                if current is not None:
                    current.prices[sku_id] = price  # the PO was placed at the contracted price
            po_lines.append(
                POLine(
                    line_no=n,
                    sku=sku_id,
                    description=sku.name("en"),
                    description_local=sku.name(sup.language),
                    qty=qty,
                    uom=sku.uom,
                    unit_price_minor=price,
                    currency=sup.currency,
                    price_list_id=sup.price_lists[-1].id,
                    requested_delivery=order_date + timedelta(days=20),
                )
            )
        po = PurchaseOrder(
            id=f"PO-{self._po_n}",
            supplier_id=supplier_id,
            buyer=sup.purchasing_contact,
            created_at=at(order_date, 9),
            order_date=order_date,
            ship_to_plant=plant,
            currency=sup.currency,
            lines=po_lines,
            notes=None,
            version=1,
        )  # type: ignore[arg-type]
        self._append(
            make_event(
                "po_created",
                {"po": po.model_dump(mode="json")},
                event_time=po.created_at,
                source="erp",
                actor=po.buyer,
            )
        )
        return po

    def amend_po(
        self, po: PurchaseOrder, line_no: int, field: str, new: int, *, when: datetime
    ) -> Event:
        line = next(ln for ln in po.lines if ln.line_no == line_no)
        old = getattr(line, field)
        after = po.model_copy(
            update={
                "lines": [
                    ln.model_copy(update={field: new}) if ln.line_no == line_no else ln
                    for ln in po.lines
                ],
                "version": 2,
            }
        )
        return self._append(
            make_event(
                "po_amended",
                {
                    "po_id": po.id,
                    "amendment_no": 1,
                    "reason": "buyer change",
                    "changes": [{"line_no": line_no, "field": field, "old": old, "new": new}],
                    "po_after": after.model_dump(mode="json"),
                },
                event_time=when,
                source="erp",
                actor=po.buyer,
            )
        )

    def receipt(
        self,
        po: PurchaseOrder,
        line_no: int,
        qty: int,
        *,
        when: datetime,
        uom: str | None = None,
        booked_line_no: int | None = None,
    ) -> GoodsReceipt:
        self._gr_n += 1
        line = next(ln for ln in po.lines if ln.line_no == line_no)
        gr = GoodsReceipt(
            id=f"GR-{self._gr_n}",
            po_id=po.id,
            line_no=booked_line_no or line_no,
            received_at=when - timedelta(minutes=30),
            booked_at=when,
            qty=qty,
            uom=uom or line.uom,
            dock_clerk="jonas.weber",
            plant=po.ship_to_plant,
            note=None,
        )  # type: ignore[arg-type]
        self._append(
            make_event(
                "goods_received",
                {"receipt": gr.model_dump(mode="json")},
                event_time=gr.received_at,
                known_at=gr.booked_at,
                source="warehouse",
                actor="jonas.weber",
            )
        )
        return gr

    def invoice(
        self,
        supplier_id: str | None,
        number: str,
        lines: list[tuple[str, int, int]],
        *,
        invoice_date: date,
        known_at: datetime | None = None,
        po_refs: list[str] | None = None,
        currency: str | None = None,
        vat_rate_bp: int | None = None,
        gross_delta: int = 0,
        doc: str = "structured",
        parsed: ParsedInvoice
        | ExtractionFailure
        | list[ParsedInvoice | ExtractionFailure]
        | None = None,
        supplier_name: str | None = None,
        vat_id: str | None = None,
        kind: str = "invoice",
        credit_note_for: str | None = None,
        terms: str | None = None,
        bank_details: dict[str, str] | None = None,
        prepayment_minor: int | None = None,
        descriptions: dict[str, str] | None = None,
        retention: bool = False,
        uoms: dict[str, str] | None = None,
    ) -> Event:
        sup = self.by_id[supplier_id] if supplier_id else None
        self._inv_n += 1
        inv_id = f"INV-{self._inv_n}"
        currency = currency or (sup.currency if sup else "EUR")
        rate = (
            vat_rate_bp if vat_rate_bp is not None else (1900 if sup and sup.country == "DE" else 0)
        )
        line_rows = []
        net = 0
        for sku_id, qty, price in lines:
            desc = (descriptions or {}).get(sku_id) or (
                SKU[sku_id].name("en") if sku_id in SKU else sku_id
            )
            uom = (uoms or {}).get(sku_id) or (SKU[sku_id].uom if sku_id in SKU else "pc")
            total = qty * price
            net += total
            line_rows.append(
                {
                    "sku": sku_id if sku_id in SKU else None,
                    "description": desc,
                    "qty": qty,
                    "uom": uom,
                    "unit_price": price,
                    "amount": total,
                }
            )
        vat = round(net * rate / 10000)
        gross = net + vat + gross_delta
        name = supplier_name or (sup.legal_name if sup else "Unknown")
        tax_id = vat_id or (sup.vat_id if sup else None)
        known = known_at or self.now
        note_parts = []
        if prepayment_minor:
            note_parts.append(f"Less prepayment received: {money_text(prepayment_minor, currency)}")
        if retention:
            note_parts.append(RETENTION_NOTE)
        notes_text = " ".join(note_parts) or None
        if doc == "structured":
            document = {
                "invoice_number": number,
                "document_type": "credit_memo" if kind == "credit_note" else "invoice",
                "invoice_date": invoice_date.isoformat(),
                "service_date": invoice_date.isoformat(),
                "due_date": (invoice_date + timedelta(days=30)).isoformat(),
                "vendor": {
                    "name": name,
                    "address": sup.address.street if sup else "x",
                    "city": sup.address.city if sup else "x",
                    "postal_code": "0",
                    "country": sup.country if sup else "EE",
                    "tax_id": tax_id,
                },
                "bill_to": {"name": "Toyota Boshoku Europe GmbH", "vat_id": "DE812345670"},
                "po_number": ", ".join(po_refs or []),
                "currency": currency,
                "line_items": [
                    {
                        "line": n,
                        "sku": r["sku"],
                        "description": r["description"],
                        "quantity": f"{r['qty']:,}",
                        "unit": r["uom"],
                        "unit_price": money_text(r["unit_price"], currency),
                        "amount": money_text(r["amount"], currency),
                    }
                    for n, r in enumerate(line_rows, start=1)
                ],
                "subtotal": money_text(net, currency),
                "tax_rate": f"{rate / 100:g}%",
                "tax": money_text(vat + gross_delta, currency),
                "total": money_text(gross, currency),
                "terms": terms or (sup.terms if sup else "net30"),
                "remit_to": bank_details
                or (
                    {"iban": sup.iban, "bank": sup.bank_account.bank_name}
                    if sup and sup.iban
                    else {}
                ),
                "notes": notes_text,
            }
            if credit_note_for:
                document["notes"] = f"Credit memo applies to invoice {credit_note_for}"
            rel = f"invoices/{inv_id}.json"
            content = (json.dumps(document, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
            fmt, via = "structured", "edi"
        else:
            rel = f"invoices/{inv_id}.pdf"
            content = (
                b"%PDF-1.4 kit placeholder " + number.encode() + b" " + str(self._inv_n).encode()
            )
            fmt, via = "pdf", "portal"
            if parsed is None:
                parsed = ParsedInvoice(
                    supplier_name=name,
                    supplier_vat_id=tax_id,
                    invoice_number=number,
                    invoice_date=invoice_date.isoformat(),
                    due_date=None,
                    service_date=None,
                    po_references=list(po_refs or []),
                    currency=currency,
                    lines=[
                        ParsedLine(
                            description=r["description"],
                            sku=r["sku"],
                            qty=str(r["qty"]),
                            uom=r["uom"],
                            unit_price=money_text(r["unit_price"], currency),
                            line_total=money_text(r["amount"], currency),
                        )
                        for r in line_rows
                    ],
                    net=money_text(net, currency),
                    vat_rate=f"{rate / 100:g}%",
                    vat_amount=money_text(vat + gross_delta, currency),
                    gross=money_text(gross, currency),
                    bank_details=ParsedBankDetails(**bank_details) if bank_details else None,
                    document_language="en",
                    is_credit_note=kind == "credit_note",
                    credit_note_for=credit_note_for,
                    payment_terms=terms,
                    prepayment_amount=money_text(prepayment_minor, currency)
                    if prepayment_minor
                    else None,
                    notes=notes_text,
                )
            sha = hashlib.sha256(content).hexdigest()
            self.llm.scripts[sha] = list(parsed) if isinstance(parsed, list) else [parsed]
        (self.world / rel).write_bytes(content)
        payload = {
            "invoice_id": inv_id,
            "format": fmt,
            "document_path": rel,
            "received_via": via,
            "sender": sup.email if sup else "invoices@unknown.example",
            "supplier_hint": supplier_id,
            "attachments": [],
            "size_bytes": len(content),
            "sha256": hashlib.sha256(content).hexdigest(),
            "pages": None if fmt == "structured" else 1,
        }
        ev = make_event(
            "invoice_received", payload, event_time=known, source=via, actor=payload["sender"]
        )
        return self._append(ev)

    # -- inspection ---------------------------------------------------------------------------
    def actions(
        self, ref: str | None = None, action_type: str | None = None
    ) -> list[dict[str, Any]]:
        out = []
        for e in self.store.query(type="agent_action", limit=100000):
            p = e.payload
            if ref is not None and ref not in p["refs"]:
                continue
            if action_type is not None and p["action_type"] != action_type:
                continue
            out.append(p)
        return out

    def action_types(self, ref: str) -> list[str]:
        return [a["action_type"] for a in self.actions(ref)]

    def escalations(self, ref: str | None = None) -> list[Event]:
        return [
            e
            for e in self.store.query(type="escalation", limit=100000)
            if ref is None or ref in e.payload["refs"]
        ]

    def entries_for(self, event_id: str) -> list[JournalEntry]:
        return [e for e in self.ledger.entries() if any(x.event_id == event_id for x in e.evidence)]

    def entry_for(self, event_id: str, cause_type: str = "invoice") -> JournalEntry | None:
        found = [e for e in self.entries_for(event_id) if e.cause_type == cause_type]
        return found[0] if found else None

    @staticmethod
    def line(entry: JournalEntry, account: str) -> Any:
        return next(ln for ln in entry.lines if ln.account == account)

    def invoice_id_of(self, ev: Event) -> str:
        return str(ev.payload["invoice_id"])
