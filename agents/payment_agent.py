"""Payment agent (spec §7.5): the daily run, the signer round trip, bank execution, R10 button
approvals, T9 discount decisions, credit-note netting and bank-statement reconciliation. Every
side effect goes through ``Context`` (policy); every decision is an ``agent_action``."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Any

import structlog

from agents.base import Context
from agents.payment_state import PaymentRow, PaymentState
from agents.state import AgentState
from bank.mock_bank import MockBank
from core.accounts import (
    AP_ACCOUNT,
    BANK_ACCOUNT_BY_CURRENCY,
    BANK_FEES_ACCOUNT,
    DISCOUNT_INCOME_ACCOUNT,
    FX_REALIZED_ACCOUNT,
    FX_ROUNDING_ACCOUNT,
    INTERCOMPANY_ACCOUNT,
)
from core.events import EventStore
from core.ledger import Evidence, JournalEntry, JournalEntryDraft, JournalLineDraft, Ledger, Payable
from core.money import FxRateUnavailable, Money, convert
from core.packets import PacketDraft, PacketEvidence, PacketOption, PacketStore
from core.policy import PolicyViolation, PostAction, RequestPaymentAction
from core.rules import (
    BUTTON_THRESHOLD_MINOR,
    DISCOUNT_LEAD_DAYS,
    INTERCOMPANY_RATE_BP,
    PAY_LEAD_DAYS,
    RECON_GRACE_DAYS,
    ROUNDING_TOL_MINOR,
    discount_minor,
    financing_cost_minor,
    is_fee_line,
    is_fx_line,
    references_in,
)
from core.vendors import VendorBankAccount, VendorMaster
from signer.frames import PayFrame, day_index, encode
from signer.interface import Signer
from signer.provisioning import Provisioner
from sim.fx import FxTable
from sim.models import Event

log = structlog.get_logger("payment_agent")

SETTLEMENT_CURRENCIES = ("EUR", "CZK", "PLN")
T9_OPTIONS = ("pay_partial_now", "draw_intercompany", "skip_discount")


@dataclass
class Group:
    vendor_id: str
    currency: str
    payables: list[Payable] = field(default_factory=list)
    credits: list[Payable] = field(default_factory=list)
    discounted: list[Payable] = field(default_factory=list)

    @property
    def gross(self) -> int:
        return sum(p.amount_minor for p in self.payables)

    @property
    def credit(self) -> int:
        return sum(-p.amount_minor for p in self.credits)

    @property
    def discount(self) -> int:
        return sum(discount_minor(p.amount_minor, p.discount_bp) for p in self.discounted)

    @property
    def pay_amount(self) -> int:
        return self.gross - self.credit - self.discount

    @property
    def sort_key(self) -> tuple[date, date]:
        deadlines = [p.discount_deadline for p in self.discounted if p.discount_deadline]
        return (min(deadlines) if deadlines else date.max, min(p.due_date for p in self.payables))

    @property
    def payable_ids(self) -> list[str]:
        return [p.id for p in self.payables] + [p.id for p in self.credits]


class PaymentAgent:
    principal = "payment_agent"
    subscribes = frozenset(
        {
            "day_boundary",
            "controller_decision",
            "bank_statement",
            "bank_statement_line",
            "provisioning",
        }
    )

    def __init__(
        self,
        *,
        store: EventStore,
        ledger: Ledger,
        vendors: VendorMaster,
        packets: PacketStore,
        state: PaymentState,
        invoice_state: AgentState,
        signer: Signer,
        bank: MockBank,
        fx: FxTable,
        now: Callable[[], datetime],
        provisioner: Provisioner | None = None,
    ) -> None:
        self.store = store
        self.ledger = ledger
        self.vendors = vendors
        self.packets = packets
        self.state = state
        self.invoice_state = invoice_state
        self.signer = signer
        self.bank = bank
        self.fx = fx
        self._now = now
        self.provisioner = provisioner

    # ------------------------------------------------------------------ dispatch

    def handle(self, ev: Event, ctx: Context) -> list[Event]:
        if ev.type == "day_boundary":
            self._day(date.fromisoformat(str(ev.payload["date"])), ctx)
        elif ev.type == "controller_decision":
            self._on_decision(ev, ctx)
        elif ev.type == "bank_statement":
            self.bank.ingest(ev)
            self._after_statement(ev, ctx)
        elif ev.type == "bank_statement_line":
            self._reconcile_line(ev, ctx)
        elif ev.type == "provisioning":
            self._on_provisioning(ev, ctx)
        return []

    def retry_waiting(self, ctx: Context) -> int:
        """Re-send approved payments once a button nonce exists (after ``POST /stub/button``)."""
        sent = 0
        for row in self.state.payments(status="awaiting_button"):
            if self._try_button(row, ctx):
                sent += 1
        return sent

    # ------------------------------------------------------------------ the day

    def _day(self, today: date, ctx: Context) -> None:
        self.bank.publish_statements(through=today - timedelta(days=1))
        self.signer.tick_day(day_index(today))
        self.retry_waiting(ctx)
        self._escalate_stale_lines(today, ctx)
        if self.bank.world_statements_seen() == 0 and not self.state.run_recorded(today):
            # no statement delivery yet (or ever, on a bare test desk): run at the day boundary
            self._run(today, ctx)

    def _after_statement(self, ev: Event, ctx: Context) -> None:
        """The morning run: once every bank account's statement for the day has arrived, the desk
        knows yesterday's closing balances and builds the day's payment run."""
        today = ctx.now.date()
        if self.state.run_recorded(today) or ev.known_at.date() != today:
            return
        if self.bank.statements_ingested_on(today) >= set(SETTLEMENT_CURRENCIES):
            self._run(today, ctx)

    def _run(self, today: date, ctx: Context) -> None:
        groups = sorted(self._eligible(today), key=lambda g: g.sort_key)
        cash = {ccy: self.bank.balance(ccy) for ccy in SETTLEMENT_CURRENCIES}
        status = self.signer.status()
        remaining_cap = max(0, status.daily_cap_minor - status.daily_spent_minor)
        decision: dict[str, Any] = {
            "paid": [],
            "held": [],
            "deferred_cash": [],
            "deferred_cap": [],
            "discount_decisions": [],
            "netted_only": [],
        }
        for g in groups:
            if g.pay_amount <= 0:
                decision["netted_only"].append(
                    {
                        "vendor_id": g.vendor_id,
                        "currency": g.currency,
                        "credit_minor": g.credit,
                        "gross_minor": g.gross,
                    }
                )
                continue
            settle_ccy, settle_minor, eur = self._settlement(g.pay_amount, g.currency, today)
            if g.discounted and self._t9_pending(g):
                decision["discount_decisions"].append(
                    {"vendor_id": g.vendor_id, "status": "awaiting_decision"}
                )
                continue
            if cash[settle_ccy] - settle_minor < 0:
                if g.discount > 0:
                    pkt_id = self._t9_packet(
                        g, today, cash[settle_ccy], settle_minor, settle_ccy, ctx
                    )
                    decision["discount_decisions"].append(
                        {
                            "vendor_id": g.vendor_id,
                            "packet_id": pkt_id,
                            "shortfall_minor": settle_minor - cash[settle_ccy],
                            "currency": settle_ccy,
                        }
                    )
                else:
                    decision["deferred_cash"].append(
                        {
                            "vendor_id": g.vendor_id,
                            "currency": g.currency,
                            "amount_minor": g.pay_amount,
                            "cash_minor": cash[settle_ccy],
                        }
                    )
                continue
            if eur > remaining_cap:
                decision["deferred_cap"].append(
                    {
                        "vendor_id": g.vendor_id,
                        "currency": g.currency,
                        "eur_equiv_minor": eur,
                        "remaining_cap_minor": remaining_cap,
                    }
                )
                continue
            row = self._pay(g, ctx, today=today)
            if row is None:
                continue
            if row.status == "executed":
                cash[settle_ccy] -= settle_minor
                remaining_cap -= eur
                decision["paid"].append(
                    {
                        "pay_id": row.pay_id,
                        "vendor_id": g.vendor_id,
                        "amount_minor": row.amount_minor,
                        "currency": g.currency,
                        "eur_equiv_minor": row.eur_equiv_minor,
                    }
                )
            else:
                decision["held"].append(
                    {"pay_id": row.pay_id, "vendor_id": g.vendor_id, "reason": row.held_reason}
                )
        self.state.record_run(today, decision)
        ctx.act(
            action_type="payment_run_built",
            rule_id=None,
            refs=[
                today.isoformat(),
                *[p["pay_id"] for p in decision["paid"]],
                *[h["pay_id"] for h in decision["held"]],
            ],
            inputs={
                "day": today.isoformat(),
                "groups": len(groups),
                "cash_before_minor": {ccy: self.bank.balance(ccy) for ccy in SETTLEMENT_CURRENCIES},
                "daily_cap_remaining_minor": max(
                    0, status.daily_cap_minor - status.daily_spent_minor
                ),
            },
            decision=decision,
        )

    def _eligible(self, today: date) -> list[Group]:
        groups: dict[tuple[str, str], Group] = {}
        for p in self.ledger.payables():
            if p.status != "open" or self.ledger.is_retention_payable(p):
                continue
            g = groups.setdefault(
                (p.vendor_id, p.currency), Group(vendor_id=p.vendor_id, currency=p.currency)
            )
            if p.amount_minor < 0:
                g.credits.append(p)
                continue
            if p.amount_minor == 0:
                continue
            takes_discount = (
                p.discount_bp > 0
                and p.discount_deadline is not None
                and today <= p.discount_deadline <= today + timedelta(days=DISCOUNT_LEAD_DAYS)
                and not self._skip_discount(p)
            )
            if takes_discount:
                g.payables.append(p)
                g.discounted.append(p)
            elif p.due_date <= today + timedelta(days=PAY_LEAD_DAYS):
                g.payables.append(p)
        return [g for g in groups.values() if g.payables]

    # ------------------------------------------------------------------ one payment

    def _pay(
        self,
        g: Group,
        ctx: Context,
        *,
        today: date,
        nonce: int = 0,
        existing: PaymentRow | None = None,
        covered: dict[str, int] | None = None,
        rule_id: str | None = None,
    ) -> PaymentRow | None:
        """Request → sign → execute → post. Returns the payment row (executed or held), or None when
        the policy refused the request (payables held)."""
        inv_ids = [p.inv_id for p in g.payables]
        numbers = [n for n in (self._invoice_number(p.inv_id) for p in g.payables) if n]
        acct = self.vendors.active_account(g.vendor_id)
        if acct is None:
            self._hold_payables(
                g, "no_active_account", ctx, refs=[g.vendor_id, *inv_ids, *numbers], rule_id=None
            )
            return None
        covered = covered or {p.id: p.amount_minor for p in g.payables}
        gross = sum(covered.values())
        discount = sum(
            discount_minor(covered[p.id], p.discount_bp) for p in g.discounted if p.id in covered
        )
        pay_amount = gross - g.credit - discount
        settle_ccy, settle_minor, eur = self._settlement(pay_amount, g.currency, today)
        above = eur > BUTTON_THRESHOLD_MINOR
        rule = rule_id or ("R10" if above else None)
        pay_id = existing.pay_id if existing is not None else self.state.next_pay_id()
        refs = [pay_id, g.vendor_id, acct.id, *inv_ids, *numbers]
        try:
            ctx.act(
                action_type="payment_requested",
                rule_id=rule,
                refs=refs,
                inputs={
                    "payables": [
                        [p.id, p.inv_id, p.amount_minor, covered.get(p.id, 0)] for p in g.payables
                    ],
                    "credits": [[c.id, c.inv_id, c.amount_minor] for c in g.credits],
                    "discount_minor": discount,
                    "nonce_present": nonce != 0,
                },
                decision={
                    "amount_minor": pay_amount,
                    "currency": g.currency,
                    "eur_equiv_minor": eur,
                    "settlement_currency": settle_ccy,
                    "settlement_minor": settle_minor,
                    "above_threshold": above,
                },
                policy_action=RequestPaymentAction(
                    pay_id=pay_id,
                    vendor_id=g.vendor_id,
                    account_id=acct.id,
                    amount_minor=pay_amount,
                    currency=g.currency,
                    eur_equiv_minor=eur,
                    payable_ids=tuple(g.payable_ids),
                ),
            )
        except PolicyViolation as exc:
            self._hold_payables(g, f"policy:{exc.reason}", ctx, refs=refs, rule_id=rule)
            return None
        conv = self._conversion(pay_amount, g.currency, today)
        frame = encode(
            PayFrame(
                seq=self.signer.status().seq + 1,
                account_hash=bytes.fromhex(acct.account_hash),
                amount_minor=eur,
                currency=g.currency,
                nonce=nonce,
                pay_id=pay_id,
            )
        )
        req = ctx.emit(
            "payment_requested",
            {
                "pay_id": pay_id,
                "payable_ids": g.payable_ids,
                "vendor_id": g.vendor_id,
                "account_hash": acct.account_hash,
                "account_masked": _mask(acct.iban_or_acct),
                "amount_minor": pay_amount,
                "currency": g.currency,
                "eur_equiv_minor": eur,
                "fx_rate": conv[0],
                "rate_date": conv[1],
                "requested_by": self.principal,
                "frame_hex": frame.hex(),
            },
            source="agent",
            actor=self.principal,
        )
        if existing is None:
            self.state.add_payment(
                pay_id=pay_id,
                vendor_id=g.vendor_id,
                currency=g.currency,
                amount_minor=pay_amount,
                eur_equiv_minor=eur,
                payable_ids=g.payable_ids,
                account_id=acct.id,
                account_hash=acct.account_hash,
                status="requested",
                now=ctx.now,
                discount_minor=discount,
                netted_minor=g.credit,
                extra={
                    "covered": covered,
                    "settlement_currency": settle_ccy,
                    "settlement_minor": settle_minor,
                },
            )
        else:
            self.state.update_payment(
                pay_id,
                now=ctx.now,
                status="requested",
                amount_minor=pay_amount,
                eur_equiv_minor=eur,
                discount_minor=discount,
                netted_minor=g.credit,
                held_reason=None,
                extra={
                    **existing.extra,
                    "covered": covered,
                    "settlement_currency": settle_ccy,
                    "settlement_minor": settle_minor,
                },
            )
        self.state.update_payment(
            pay_id,
            now=ctx.now,
            requested_event_id=req.id,
            frame_hex=frame.hex(),
            seq=self.signer.status().seq + 1,
        )
        res = self.signer.sign(frame)
        reasons: list[str] = [] if res.ok else [res.reason or "unknown"]
        if not res.ok and acct.hold_until > ctx.now and "in_hold" not in reasons:
            reasons.append("in_hold")
        chip = ctx.emit(
            "chip_response",
            {
                "pay_id": pay_id,
                "ok": res.ok,
                "tag_hex": res.tag.hex() if res.tag else None,
                "reasons": reasons,
                "latency_ms": res.latency_ms,
                "raw_hex": res.raw.hex(),
                "signer": self.signer.kind,
            },
            source="signer",
            actor=self.signer.kind,
        )
        self.state.update_payment(
            pay_id, now=ctx.now, chip_event_id=chip.id, tag_hex=res.tag.hex() if res.tag else None
        )
        if not res.ok:
            return self._chip_rejected(
                g,
                pay_id,
                acct,
                reasons,
                ctx,
                refs=refs,
                rule=rule,
                request_event_id=req.id,
                chip_event_id=chip.id,
            )
        assert res.tag is not None
        bank_res = self.bank.execute(frame, res.tag)
        if not bank_res.ok:
            reason = f"bank:{bank_res.reason}"
            self._hold_payables(
                g,
                reason,
                ctx,
                refs=refs,
                rule_id=rule,
                extra={
                    "chip_event_id": chip.id,
                    "bank_event_id": bank_res.event_id,
                    "detail": bank_res.detail,
                },
            )
            return self.state.update_payment(pay_id, now=ctx.now, status="held", held_reason=reason)
        executed = ctx.emit(
            "payment_executed",
            {
                "pay_id": pay_id,
                "bank_ref": bank_res.bank_ref,
                "vendor_id": g.vendor_id,
                "amount_minor": pay_amount,
                "currency": g.currency,
                "account_masked": _mask(acct.iban_or_acct),
                "executed_at": ctx.now,
                "statement_date": bank_res.value_date,
            },
            source="bank",
            actor="bank",
        )
        lines = [
            JournalLineDraft(
                account=AP_ACCOUNT,
                debit_minor=gross - g.credit,
                currency=g.currency,
                rate_date=today,
            )
        ]
        lines.append(
            JournalLineDraft(
                account=BANK_ACCOUNT_BY_CURRENCY[settle_ccy],
                credit_minor=settle_minor,
                currency=settle_ccy,
                rate_date=today,
            )
        )
        if discount:
            lines.append(
                JournalLineDraft(
                    account=DISCOUNT_INCOME_ACCOUNT,
                    credit_minor=discount,
                    currency=g.currency,
                    rate_date=today,
                )
            )
        draft = JournalEntryDraft(
            effective_date=today,
            description=f"Payment {pay_id} to {g.vendor_id} ({', '.join(numbers or inv_ids)})"
            + (f", discount {discount}" if discount else "")
            + (f", netted {g.credit}" if g.credit else ""),
            cause_type="payment",
            cause_id=pay_id,
            lines=lines,
            evidence=[
                Evidence(event_id=req.id, role="payment_request"),
                Evidence(event_id=chip.id, role="chip_response"),
                Evidence(event_id=executed.id, role="payment"),
            ],
        )
        posted = ctx.act(
            action_type="posted",
            rule_id=rule,
            refs=[*refs, bank_res.bank_ref or ""],
            inputs={
                "payment_requested": req.id,
                "chip_response": chip.id,
                "payment_executed": executed.id,
                "settlement": [settle_ccy, settle_minor, bank_res.fx_rate],
            },
            decision={
                "post_minor": gross - g.credit,
                "paid_minor": settle_minor,
                "currency": g.currency,
                "settlement_currency": settle_ccy,
                "discount_minor": discount,
                "netted_minor": g.credit,
                "eur_equiv_minor": eur,
            },
            policy_action=PostAction(draft=draft, posted_at=ctx.now),
        )
        entry = posted.result
        assert isinstance(entry, JournalEntry)
        for p in g.payables:
            paid = covered.get(p.id, 0)
            if paid >= p.amount_minor:
                self.ledger.set_payable_status(p.id, "paid")
            else:
                self.ledger.set_payable_amount(p.id, p.amount_minor - paid)
                self.ledger.set_payable_status(p.id, "open")
        for c in g.credits:
            self.ledger.set_payable_status(c.id, "netted")
        return self.state.update_payment(
            pay_id,
            now=ctx.now,
            status="executed",
            held_reason=None,
            bank_ref=bank_res.bank_ref,
            executed_event_id=executed.id,
            entry_id=entry.id,
        )

    def _chip_rejected(
        self,
        g: Group,
        pay_id: str,
        acct: VendorBankAccount,
        reasons: list[str],
        ctx: Context,
        *,
        refs: list[str],
        rule: str | None,
        request_event_id: str,
        chip_event_id: str,
    ) -> PaymentRow:
        reason = reasons[0]
        for p in g.payables + g.credits:
            self.ledger.set_payable_status(p.id, "held", held_reason=reason)
        status = "awaiting_decision" if reason == "needs_button" else "held"
        row = self.state.update_payment(pay_id, now=ctx.now, status=status, held_reason=reason)
        ctx.act(
            action_type="payment_held",
            rule_id="R10" if reason == "needs_button" else rule,
            refs=[*refs, chip_event_id],
            inputs={
                "reasons": reasons,
                "chip_response": chip_event_id,
                "payment_requested": request_event_id,
                "account_hold_until": acct.hold_until.isoformat(),
            },
            decision={
                "held_reason": reason,
                "payables": g.payable_ids,
                "amount_minor": row.amount_minor,
                "currency": row.currency,
                "eur_equiv_minor": row.eur_equiv_minor,
            },
        )
        if reason == "needs_button":
            numbers = [n for n in (self._invoice_number(p.inv_id) for p in g.payables) if n]
            packet = self.packets.create_packet(
                PacketDraft(
                    agent=self.principal,
                    question=f"Approve payment {pay_id}: {_fmt(row.amount_minor, row.currency)} {row.currency} (EUR {_fmt(row.eur_equiv_minor, 'EUR')}) to {g.vendor_id}? It exceeds the EUR 50,000.00 button threshold (R10).",
                    context=f"The chip answered needs_button for {', '.join(numbers or [p.inv_id for p in g.payables])}. Approving asks the controller to press the button; the payment is re-sent with the button nonce.",
                    severity="action",
                    refs=[pay_id, g.vendor_id, *[p.inv_id for p in g.payables], *numbers],
                    evidence=[
                        PacketEvidence(
                            event_id=request_event_id,
                            role="payment_request",
                            summary="the payment request and frame",
                        ),
                        PacketEvidence(
                            event_id=chip_event_id, role="chip_response", summary="needs_button"
                        ),
                    ],
                    materiality_minor=row.eur_equiv_minor,
                    currency="EUR",
                    recommendation="approve",
                    recommendation_reason="the vendor account is on the allowlist and the payables are due; only the amount triggers the control",
                    options=[
                        PacketOption(id="approve", label="Approve and press the button"),
                        PacketOption(id="reject", label="Reject the payment"),
                    ],
                    expires_at=ctx.now + timedelta(days=5),
                    consequence_of_silence="The payment stays held; the payables remain open liabilities.",
                    kind="approval",
                ),
                created_at=ctx.now,
            )
            self.state.add_packet(
                packet.id,
                kind="button_approval",
                pay_id=pay_id,
                payable_id=None,
                data={"vendor_id": g.vendor_id},
            )
            row = self.state.update_payment(pay_id, now=ctx.now, packet_id=packet.id)
        elif reason == "not_allowlisted" and self.provisioner is not None:
            self.provisioner.enqueue(g.vendor_id)
        return row

    def _hold_payables(
        self,
        g: Group,
        reason: str,
        ctx: Context,
        *,
        refs: list[str],
        rule_id: str | None,
        extra: dict[str, Any] | None = None,
    ) -> None:
        for p in g.payables + g.credits:
            self.ledger.set_payable_status(p.id, "held", held_reason=reason)
        ctx.act(
            action_type="payment_held",
            rule_id=rule_id,
            refs=refs,
            inputs={"reason": reason, **(extra or {})},
            decision={
                "held_reason": reason,
                "payables": g.payable_ids,
                "amount_minor": g.pay_amount,
                "currency": g.currency,
            },
        )

    # ------------------------------------------------------------------ R10: decisions and the button

    def _on_decision(self, ev: Event, ctx: Context) -> None:
        pkt = self.state.packet(str(ev.payload["packet_id"]))
        if pkt is None or pkt.decided:
            return
        decision = str(ev.payload["decision"])
        self.state.set_packet_decision(pkt.packet_id, decision)
        if pkt.kind == "button_approval" and pkt.pay_id:
            row = self.state.payment(pkt.pay_id)
            if row is None:
                return
            if decision == "approve":
                self.state.update_payment(row.pay_id, now=ctx.now, status="awaiting_button")
                self._try_button(
                    self.state.payment(row.pay_id) or row, ctx, decision_event_id=ev.id
                )
            else:
                for pid in row.payable_ids:
                    self.ledger.set_payable_status(
                        pid, "held", held_reason="rejected_by_controller"
                    )
                self.state.update_payment(
                    row.pay_id, now=ctx.now, status="rejected", held_reason="rejected_by_controller"
                )
                ctx.act(
                    action_type="payment_held",
                    rule_id="R10",
                    refs=[row.pay_id, row.vendor_id, ev.id],
                    inputs={"decision": decision, "packet_id": pkt.packet_id},
                    decision={
                        "held_reason": "rejected_by_controller",
                        "payables": list(row.payable_ids),
                    },
                )
        elif pkt.kind == "t9":
            self._t9_decide(pkt.data, decision, ev, ctx)

    def _try_button(
        self, row: PaymentRow, ctx: Context, *, decision_event_id: str | None = None
    ) -> bool:
        nonce = self.signer.nonce()
        if nonce is None:
            return False
        g = self._group_from(row)
        if g is None:
            return False
        covered = {k: int(v) for k, v in row.extra.get("covered", {}).items()} or None
        result = self._pay(
            g, ctx, today=ctx.now.date(), nonce=nonce, existing=row, covered=covered, rule_id="R10"
        )
        return result is not None and result.status == "executed"

    def _group_from(self, row: PaymentRow) -> Group | None:
        g = Group(vendor_id=row.vendor_id, currency=row.currency)
        for pid in row.payable_ids:
            p = self.ledger.get_payable(pid)
            if p is None or p.status == "paid":
                continue
            if p.amount_minor < 0:
                g.credits.append(p)
            else:
                g.payables.append(p)
                if p.discount_bp and p.id in row.extra.get("covered", {}) and row.discount_minor:
                    g.discounted.append(p)
        return g if g.payables else None

    # ------------------------------------------------------------------ T9: discount versus cash

    def _t9_pending(self, g: Group) -> bool:
        ids = {p.id for p in g.discounted}
        return any(not pk.decided and pk.payable_id in ids for pk in self.state.packets(kind="t9"))

    def _skip_discount(self, p: Payable) -> bool:
        return any(
            pk.decided
            and pk.payable_id == p.id
            and pk.data.get("decision") in ("skip_discount", "pay_partial_now")
            for pk in self.state.packets(kind="t9")
        )

    def _t9_packet(
        self, g: Group, today: date, cash: int, settle_minor: int, settle_ccy: str, ctx: Context
    ) -> str:
        shortfall = settle_minor - cash
        discount_eur = self._settlement(g.discount, g.currency, today)[2]
        deadline = min(p.discount_deadline for p in g.discounted if p.discount_deadline)
        due = min(p.due_date for p in g.payables)
        available = max(0, cash)
        covered_share = available / settle_minor if settle_minor else 0
        captured = int(g.discount * covered_share)
        lost_partial = self._settlement(g.discount - captured, g.currency, today)[2]
        shortfall_eur = (
            shortfall if settle_ccy == "EUR" else self._settlement(shortfall, settle_ccy, today)[2]
        )
        finance_days = max(1, (due - today).days)
        cost_draw = financing_cost_minor(shortfall_eur, INTERCOMPANY_RATE_BP, finance_days)
        benefits = {
            "pay_partial_now": discount_eur - lost_partial,
            "draw_intercompany": discount_eur - cost_draw,
            "skip_discount": 0,
        }
        recommendation = max(T9_OPTIONS, key=lambda o: (benefits[o], o == "draw_intercompany"))
        numbers = [n for n in (self._invoice_number(p.inv_id) for p in g.payables) if n]
        options = [
            PacketOption(
                id="pay_partial_now",
                label=f"Pay what cash allows now ({_fmt(available, settle_ccy)} {settle_ccy}), rest at due date: discount lost EUR {_fmt(lost_partial, 'EUR')}",
            ),
            PacketOption(
                id="draw_intercompany",
                label=f"Draw EUR {_fmt(shortfall_eur, 'EUR')} intercompany for {finance_days} days (cost EUR {_fmt(cost_draw, 'EUR')}), take the full discount",
            ),
            PacketOption(
                id="skip_discount",
                label=f"Skip the discount, pay {_fmt(g.gross - g.credit, g.currency)} {g.currency} on {due.isoformat()}: discount lost EUR {_fmt(discount_eur, 'EUR')}",
            ),
        ]
        packet = self.packets.create_packet(
            PacketDraft(
                agent=self.principal,
                question=f"Discount of EUR {_fmt(discount_eur, 'EUR')} on {', '.join(numbers or [p.inv_id for p in g.payables])} expires {deadline.isoformat()}; {settle_ccy} cash {_fmt(cash, settle_ccy)} is short by {_fmt(shortfall, settle_ccy)} for the {_fmt(settle_minor, settle_ccy)} payment. Which option?",
                context=f"Net benefit in EUR: pay_partial_now {_fmt(benefits['pay_partial_now'], 'EUR')}; draw_intercompany {_fmt(benefits['draw_intercompany'], 'EUR')} (at {INTERCOMPANY_RATE_BP / 100:.2f} % p.a.); skip_discount 0.00. Due date {due.isoformat()}.",
                severity="action",
                refs=[g.vendor_id, *[p.inv_id for p in g.payables], *numbers],
                evidence=[],
                materiality_minor=discount_eur,
                currency="EUR",
                recommendation=recommendation,
                recommendation_reason=f"largest net benefit: EUR {_fmt(benefits[recommendation], 'EUR')}",
                options=options,
                expires_at=datetime.combine(deadline, datetime.min.time(), tzinfo=ctx.now.tzinfo),
                consequence_of_silence="The discount lapses and the invoice is paid at its due date.",
                kind="approval",
            ),
            created_at=ctx.now,
        )
        for p in g.discounted:
            self.state.add_packet(
                packet.id,
                kind="t9",
                pay_id=None,
                payable_id=p.id,
                data={
                    "vendor_id": g.vendor_id,
                    "currency": g.currency,
                    "payable_ids": g.payable_ids,
                    "shortfall_minor": shortfall,
                    "settlement_currency": settle_ccy,
                    "available_minor": available,
                    "benefits": benefits,
                    "recommendation": recommendation,
                },
            )
        ctx.act(
            action_type="held",
            rule_id=None,
            refs=[packet.id, g.vendor_id, *[p.inv_id for p in g.payables], *numbers],
            inputs={
                "cash_minor": cash,
                "payment_minor": settle_minor,
                "settlement_currency": settle_ccy,
            },
            decision={
                "packet_id": packet.id,
                "options": benefits,
                "recommendation": recommendation,
                "discount_eur_minor": discount_eur,
            },
        )
        return packet.id

    def _t9_decide(self, data: dict[str, Any], decision: str, ev: Event, ctx: Context) -> None:
        self.state.set_packet_decision(str(ev.payload["packet_id"]), decision)
        g = Group(vendor_id=str(data["vendor_id"]), currency=str(data["currency"]))
        for pid in data["payable_ids"]:
            p = self.ledger.get_payable(str(pid))
            if p is None or p.status not in ("open", "scheduled"):
                continue
            (g.credits if p.amount_minor < 0 else g.payables).append(p)
            if p.amount_minor > 0 and p.discount_bp and decision != "skip_discount":
                g.discounted.append(p)
        if not g.payables or decision == "skip_discount":
            ctx.act(
                action_type="payment_run_built",
                rule_id=None,
                refs=[str(ev.payload["packet_id"]), g.vendor_id],
                inputs={"decision": decision},
                decision={"outcome": "pay_at_due_date"},
            )
            return
        today = ctx.now.date()
        if decision == "draw_intercompany":
            settle_ccy = str(data["settlement_currency"])
            shortfall = int(data["shortfall_minor"])
            self.bank.credit(
                settle_ccy,
                shortfall,
                reference=f"ICO-{ev.id[-8:]}",
                remittance=f"/REMI/INTERCOMPANY DRAW {g.vendor_id} {shortfall}",
            )
            draft = JournalEntryDraft(
                effective_date=today,
                description=f"Intercompany draw of {_fmt(shortfall, settle_ccy)} {settle_ccy} to take the discount from {g.vendor_id}",
                cause_type="intercompany",
                cause_id=f"ICO-{ev.id[-8:]}",
                lines=[
                    JournalLineDraft(
                        account=BANK_ACCOUNT_BY_CURRENCY[settle_ccy],
                        debit_minor=shortfall,
                        currency=settle_ccy,
                    ),
                    JournalLineDraft(
                        account=INTERCOMPANY_ACCOUNT, credit_minor=shortfall, currency=settle_ccy
                    ),
                ],
                evidence=[Evidence(event_id=ev.id, role="decision")],
            )
            ctx.act(
                action_type="posted",
                rule_id=None,
                refs=[g.vendor_id, ev.id],
                inputs={"decision": decision},
                decision={"draw_minor": shortfall, "currency": settle_ccy},
                policy_action=PostAction(draft=draft, posted_at=ctx.now),
            )
            self._pay(g, ctx, today=today)
            return
        # pay_partial_now: cover payables in order until cash runs out
        settle_ccy = str(data["settlement_currency"])
        available = self.bank.balance(settle_ccy)
        covered: dict[str, int] = {}
        for p in g.payables:
            share = min(
                p.amount_minor,
                available
                if g.currency == settle_ccy
                else self._native_for(available, settle_ccy, g.currency, today),
            )
            if share <= 0:
                break
            covered[p.id] = share
            available -= (
                share if g.currency == settle_ccy else self._settlement(share, g.currency, today)[1]
            )
        if not covered:
            ctx.act(
                action_type="payment_run_built",
                rule_id=None,
                refs=[str(ev.payload["packet_id"]), g.vendor_id],
                inputs={"decision": decision},
                decision={"outcome": "no_cash_now"},
            )
            return
        g.payables = [p for p in g.payables if p.id in covered]
        g.discounted = [p for p in g.discounted if p.id in covered]
        self._pay(g, ctx, today=today, covered=covered)

    # ------------------------------------------------------------------ provisioning follow-up

    def _on_provisioning(self, ev: Event, ctx: Context) -> None:
        if not ev.payload.get("ok") or ev.payload.get("reason") == "already_provisioned":
            return
        vendor_id = str(ev.payload["vendor_id"])
        reopened = []
        for p in self.ledger.payables(status="held", vendor_id=vendor_id):
            if p.held_reason == "not_allowlisted":
                self.ledger.set_payable_status(p.id, "open")
                reopened.append(p.id)
        if reopened:
            ctx.act(
                action_type="payment_run_built",
                rule_id=None,
                refs=[vendor_id, ev.id],
                inputs={"provisioning": ev.id},
                decision={"reopened_payables": reopened},
            )

    # ------------------------------------------------------------------ reconciliation

    def _reconcile_line(self, ev: Event, ctx: Context) -> None:
        p = ev.payload
        key = MockBank.line_key_of(p)
        known = self.state.line(key)
        if known is not None and known.status not in ("unmatched", "fx_pending"):
            return
        currency = str(p["currency"])
        amount = int(p["amount_minor"])
        value_date = date.fromisoformat(str(p["value_date"]))
        statement_id = str(p["statement_id"])
        ref = str(p["customer_ref"])
        common = {
            "event_id": ev.id,
            "statement_id": statement_id,
            "currency": currency,
            "value_date": value_date,
            "now": ctx.now,
        }
        # 1. our own payment
        row = self.state.payment(ref)
        if row is not None and row.status in ("executed", "settled") and p["dc"] == "D":
            settle_minor = int(row.extra.get("settlement_minor", row.amount_minor))
            diff = amount - settle_minor
            entry_id = None
            if diff and abs(diff) <= ROUNDING_TOL_MINOR:
                entry_id = self._post_residual(diff, currency, ev, ctx, ref)
            self.state.update_payment(
                row.pay_id,
                now=ctx.now,
                status="settled",
                settled_event_id=ev.id,
                bank_ref=p.get("bank_ref") or row.bank_ref,
            )
            self.state.set_line(
                key, status="matched", pay_id=row.pay_id, entry_id=entry_id, **common
            )
            ctx.act(
                action_type="reconciled",
                rule_id="R12" if entry_id else None,
                refs=[row.pay_id, row.vendor_id, statement_id, ev.id],
                inputs={"line_amount_minor": amount, "settlement_minor": settle_minor},
                decision={"matched": "payment_executed", "difference_minor": diff},
            )
            return
        # 2. a credit we arranged (intercompany draw)
        if p["dc"] == "C" and ref.startswith("ICO-"):
            self.state.set_line(key, status="matched", **common, note="intercompany draw")
            ctx.act(
                action_type="reconciled",
                rule_id=None,
                refs=[ref, statement_id, ev.id],
                inputs={"amount_minor": amount},
                decision={"matched": "intercompany_draw"},
            )
            return
        # 3. a batch settling open payables named in the remittance
        if p["dc"] == "D":
            targets = self._payables_named(str(p["remittance"]), currency)
            if targets and sum(t.amount_minor for t in targets) == amount:
                entry = self._post(
                    ctx,
                    JournalEntryDraft(
                        effective_date=value_date,
                        description=f"Bank batch {ref} settles {', '.join(t.inv_id for t in targets)}",
                        cause_type="payment",
                        cause_id=ref,
                        lines=[
                            JournalLineDraft(
                                account=AP_ACCOUNT, debit_minor=amount, currency=currency
                            ),
                            JournalLineDraft(
                                account=BANK_ACCOUNT_BY_CURRENCY[currency],
                                credit_minor=amount,
                                currency=currency,
                            ),
                        ],
                        evidence=[Evidence(event_id=ev.id, role="statement_line")],
                    ),
                    refs=[ref, *[t.inv_id for t in targets], targets[0].vendor_id, statement_id],
                    inputs={"line": ev.id, "payables": [t.id for t in targets]},
                    decision={
                        "post_minor": amount,
                        "currency": currency,
                        "settles": [t.inv_id for t in targets],
                    },
                )
                for t in targets:
                    self.ledger.set_payable_status(t.id, "paid")
                self.state.set_line(key, status="batch", entry_id=entry.id, **common)
                ctx.act(
                    action_type="reconciled",
                    rule_id=None,
                    refs=[
                        ref,
                        *[t.inv_id for t in targets],
                        targets[0].vendor_id,
                        statement_id,
                        ev.id,
                    ],
                    inputs={"amount_minor": amount},
                    decision={
                        "matched": "payables_batch",
                        "payables": [t.id for t in targets],
                        "entry_id": entry.id,
                    },
                )
                return
        # 4. fees
        if is_fee_line(str(p["txn_type"])) and p["dc"] == "D":
            entry = self._post(
                ctx,
                JournalEntryDraft(
                    effective_date=value_date,
                    description=f"Bank fee {statement_id} line {p['line_no']}: {str(p['remittance'])[:60]}",
                    cause_type="fee",
                    cause_id=f"{statement_id}:{p['line_no']}",
                    lines=[
                        JournalLineDraft(
                            account=BANK_FEES_ACCOUNT, debit_minor=amount, currency=currency
                        ),
                        JournalLineDraft(
                            account=BANK_ACCOUNT_BY_CURRENCY[currency],
                            credit_minor=amount,
                            currency=currency,
                        ),
                    ],
                    evidence=[Evidence(event_id=ev.id, role="statement_line")],
                ),
                refs=[statement_id, ref, ev.id],
                inputs={"line": ev.id},
                decision={"post_minor": amount, "currency": currency, "account": BANK_FEES_ACCOUNT},
            )
            self.state.set_line(key, status="fee", entry_id=entry.id, **common)
            return
        # 5. FX conversion legs pair up by reference across currencies
        if is_fx_line(str(p["txn_type"])):
            other = next(
                (
                    ln
                    for ln in self.state.lines(status="fx_pending")
                    if ln.note == ref and ln.currency != currency
                ),
                None,
            )
            if other is None:
                self.state.set_line(key, status="fx_pending", note=ref, **common)
                return
            other_ev = self.store.get(other.event_id)
            assert other_ev is not None
            self._post_fx_pair(ev, other_ev, ctx)
            self.state.set_line(key, status="fx", **common)
            self.state.set_line(
                other.line_key,
                status="fx",
                event_id=other.event_id,
                statement_id=other.statement_id,
                currency=other.currency,
                value_date=other.value_date,
                now=ctx.now,
            )
            return
        self.state.set_line(key, status="unmatched", **common)

    def _post_fx_pair(self, a: Event, b: Event, ctx: Context) -> None:
        debit = a if a.payload["dc"] == "D" else b
        credit = b if debit is a else a
        d_ccy, c_ccy = str(debit.payload["currency"]), str(credit.payload["currency"])
        d_amt, c_amt = int(debit.payload["amount_minor"]), int(credit.payload["amount_minor"])
        value_date = date.fromisoformat(str(debit.payload["value_date"]))
        lines = [
            JournalLineDraft(
                account=BANK_ACCOUNT_BY_CURRENCY[c_ccy],
                debit_minor=c_amt,
                currency=c_ccy,
                rate_date=value_date,
            ),
            JournalLineDraft(
                account=BANK_ACCOUNT_BY_CURRENCY[d_ccy],
                credit_minor=d_amt,
                currency=d_ccy,
                rate_date=value_date,
            ),
        ]
        realized = self._eur(d_amt, d_ccy, value_date) - self._eur(c_amt, c_ccy, value_date)
        if abs(realized) > 1:
            lines.append(
                JournalLineDraft(
                    account=FX_REALIZED_ACCOUNT,
                    debit_minor=realized if realized > 0 else 0,
                    credit_minor=-realized if realized < 0 else 0,
                    currency="EUR",
                    rate_date=value_date,
                )
            )
        ref = str(debit.payload["customer_ref"])
        self._post(
            ctx,
            JournalEntryDraft(
                effective_date=value_date,
                description=f"FX conversion {ref}: {_fmt(d_amt, d_ccy)} {d_ccy} to {_fmt(c_amt, c_ccy)} {c_ccy}",
                cause_type="fx",
                cause_id=ref,
                lines=lines,
                evidence=[
                    Evidence(event_id=debit.id, role="statement_line"),
                    Evidence(event_id=credit.id, role="statement_line"),
                ],
            ),
            refs=[ref, str(debit.payload["statement_id"]), str(credit.payload["statement_id"])],
            inputs={"lines": [debit.id, credit.id]},
            decision={
                "debit": [d_ccy, d_amt],
                "credit": [c_ccy, c_amt],
                "realized_eur_minor": realized,
            },
        )

    def _post_residual(self, diff: int, currency: str, ev: Event, ctx: Context, ref: str) -> str:
        entry = self._post(
            ctx,
            JournalEntryDraft(
                effective_date=date.fromisoformat(str(ev.payload["value_date"])),
                description=f"Settlement rounding on {ref}",
                cause_type="payment",
                cause_id=f"{ref}:rounding",
                lines=[
                    JournalLineDraft(
                        account=FX_ROUNDING_ACCOUNT,
                        debit_minor=diff if diff > 0 else 0,
                        credit_minor=-diff if diff < 0 else 0,
                        currency=currency,
                    ),
                    JournalLineDraft(
                        account=BANK_ACCOUNT_BY_CURRENCY[currency],
                        credit_minor=diff if diff > 0 else 0,
                        debit_minor=-diff if diff < 0 else 0,
                        currency=currency,
                    ),
                ],
                evidence=[Evidence(event_id=ev.id, role="statement_line")],
            ),
            refs=[ref, ev.id],
            inputs={"difference_minor": diff},
            decision={"rule": "R12", "post_minor": abs(diff)},
            rule_id="R12",
        )
        return entry.id

    def _escalate_stale_lines(self, today: date, ctx: Context) -> None:
        stale = []
        for ln in self.state.lines(status="unmatched"):
            line_event = self.store.get(ln.event_id)
            known = line_event.known_at.date() if line_event is not None else ln.value_date
            if known + timedelta(days=RECON_GRACE_DAYS) <= today:
                stale.append(ln)
        by_statement: dict[str, list[Any]] = {}
        for ln in stale:
            by_statement.setdefault(ln.statement_id, []).append(ln)
        for statement_id, lines in by_statement.items():
            events = [self.store.get(ln.event_id) for ln in lines]
            refs_lines = [str(e.payload["customer_ref"]) for e in events if e is not None]
            total = sum(
                int(e.payload["amount_minor"]) * (1 if e.payload["dc"] == "C" else -1)
                for e in events
                if e is not None
            )
            currency = lines[0].currency
            packet = self.packets.create_packet(
                PacketDraft(
                    agent=self.principal,
                    question=f"{len(lines)} bank line(s) on {statement_id} ({currency}) match no payment, payable, fee or FX conversion. Who owns them?",
                    context="; ".join(
                        f"{e.payload['value_date']} {e.payload['dc']} {_fmt(int(e.payload['amount_minor']), currency)} {e.payload['customer_ref']}: {str(e.payload['remittance'])[:70]}"
                        for e in events
                        if e is not None
                    )[:1500],
                    severity="info",
                    refs=[statement_id, *refs_lines],
                    evidence=[
                        PacketEvidence(
                            event_id=e.id,
                            role="statement_line",
                            summary=str(e.payload["remittance"])[:80],
                        )
                        for e in events
                        if e is not None
                    ][:12],
                    materiality_minor=abs(total),
                    currency=currency,
                    recommendation="acknowledge",
                    recommendation_reason="payroll, tax, treasury and customer lines are not accounts-payable movements",
                    options=[PacketOption(id="acknowledge", label="Acknowledge: not an AP item")],
                    expires_at=ctx.now + timedelta(days=14),
                    consequence_of_silence="The lines stay unmatched in the reconciliation report.",
                    kind="notice",
                ),
                created_at=ctx.now,
            )
            for ln in lines:
                self.state.set_line(
                    ln.line_key,
                    event_id=ln.event_id,
                    statement_id=ln.statement_id,
                    currency=ln.currency,
                    value_date=ln.value_date,
                    status="escalated",
                    now=ctx.now,
                    note=packet.id,
                )

    # ------------------------------------------------------------------ helpers

    def _payables_named(self, remittance: str, currency: str) -> list[Payable]:
        tokens = set(references_in(remittance))
        if not tokens:
            return []
        out = []
        for p in self.ledger.payables():
            if (
                p.status in ("open", "scheduled")
                and p.currency == currency
                and (p.inv_id in tokens or (self._invoice_number(p.inv_id) or "") in tokens)
            ):
                out.append(p)
        return out

    def _post(
        self,
        ctx: Context,
        draft: JournalEntryDraft,
        *,
        refs: list[str],
        inputs: dict[str, Any],
        decision: dict[str, Any],
        rule_id: str | None = None,
    ) -> JournalEntry:
        res = ctx.act(
            action_type="posted",
            rule_id=rule_id,
            refs=refs,
            inputs=inputs,
            decision=decision,
            policy_action=PostAction(draft=draft, posted_at=ctx.now),
        )
        entry = res.result
        assert isinstance(entry, JournalEntry)
        return entry

    def _invoice_number(self, inv_id: str) -> str | None:
        row = self.invoice_state.invoice(inv_id)
        return row.invoice_number if row is not None else None

    def _conversion(self, minor: int, currency: str, on: date) -> tuple[str | None, date | None]:
        if currency == "EUR":
            return None, None
        conv = convert(Money(minor, currency), "EUR", on, self.fx)
        return str(conv.rate), conv.rate_date

    def _eur(self, minor: int, currency: str, on: date) -> int:
        if currency == "EUR":
            return minor
        return convert(Money(minor, currency), "EUR", on, self.fx).money.minor

    def _native_for(self, eur_minor: int, settle_ccy: str, currency: str, on: date) -> int:
        if settle_ccy == currency:
            return eur_minor
        return convert(Money(eur_minor, settle_ccy), currency, on, self.fx).money.minor

    def _settlement(self, amount_minor: int, currency: str, on: date) -> tuple[str, int, int]:
        """(settlement currency, settlement amount, EUR equivalent): CZK/PLN/EUR from their own
        accounts; USD/JPY from the EUR account at the day's rate."""
        try:
            eur = self._eur(amount_minor, currency, on)
        except FxRateUnavailable:
            eur = self._eur(amount_minor, currency, on - timedelta(days=1))
        if currency in SETTLEMENT_CURRENCIES:
            return currency, amount_minor, eur
        return "EUR", eur, eur


def _mask(account: str) -> str:
    compact = account.replace(" ", "")
    return f"{compact[:4]} **** {compact[-4:]}" if len(compact) > 8 else "****"


def _fmt(minor: int, currency: str) -> str:
    if currency == "JPY":
        return f"{minor:,}"
    sign = "-" if minor < 0 else ""
    return f"{sign}{abs(minor) // 100:,}.{abs(minor) % 100:02d}"
