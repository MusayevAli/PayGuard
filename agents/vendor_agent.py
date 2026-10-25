"""Vendor agent (spec §7.4): classifies vendor messages, runs R8 (pending account, verification over
another channel, approval packet, hold, activation, provisioning), R9 (chip rejection of a changed
account: suspend, restore, re-queue, urgent packet), R5 onboarding and the M2 bounce fallback.
Vendor state changes only through ``core.policy``; every decision is an ``agent_action``."""

from __future__ import annotations

from collections.abc import Callable, Sequence
from datetime import datetime, timedelta
from typing import Any

import structlog

from agents.base import Context
from agents.payment_state import PaymentState
from agents.resolve import VendorIndex
from agents.state import AgentState
from agents.vendor_classify import (
    Classification,
    classify,
    domain_of,
    iban_is_valid,
    lookalike,
    vat_is_valid,
)
from agents.vendor_state import VendorState, VendorWait
from core.events import EventStore, make_event
from core.ledger import Ledger
from core.packets import PacketDraft, PacketEvidence, PacketOption, PacketStore
from core.policy import (
    ActivateIfReadyAction,
    ActivateVendorAction,
    ApproveAccountAction,
    CreateVendorAction,
    MarkVerifiedAction,
    OpenPendingAccountAction,
    PolicyViolation,
    RestorePriorAction,
    SuspendAccountAction,
)
from core.rules import BANK_CHANGE_HOLD_HOURS
from core.vendors import VendorBankAccount, VendorMaster, account_hash
from signer.provisioning import Provisioner
from sim.models import Event, Supplier
from sim.people import People

log = structlog.get_logger("vendor_agent")

CHANNEL_MATRIX: dict[str, str] = {
    "portal": "phone",
    "email": "email",
    "invoice": "phone",
    "judge": "phone",
    "phone": "email",
}
"""Verification channel by request channel (DECISIONS 70): never the sender's own thread; email
requests are checked with the contact on file by email and the packet then asks for a phone call."""
JUST_ACTIVATED_HOURS = 72
BUYER_BY_COUNTRY: dict[str, str] = {
    "CZ": "petra.vlcek",
    "PL": "agnieszka.kowalczyk",
    "DE": "lukas.brandt",
    "AT": "lukas.brandt",
    "JP": "lukas.brandt",
    "US": "lukas.brandt",
    "EE": "lukas.brandt",
}
CURRENCY_BY_COUNTRY: dict[str, str] = {"CZ": "CZK", "PL": "PLN", "US": "USD", "JP": "JPY"}
OPTIONS_BANK_CHANGE = [
    PacketOption(id="approve", label="Approve change"),
    PacketOption(id="deny", label="Deny change"),
    PacketOption(id="request_phone_verification", label="Request phone verification"),
]
OPTIONS_ONBOARD = [
    PacketOption(id="approve", label="Onboard vendor"),
    PacketOption(id="deny", label="Decline vendor"),
]
OPTIONS_ACK = [PacketOption(id="acknowledge", label="Acknowledge")]


def _mask(account: str | None) -> str:
    if not account:
        return "****"
    compact = account.replace(" ", "")
    return f"{compact[:4]} **** {compact[-4:]}" if len(compact) > 8 else "****"


def _fmt(minor: int, currency: str) -> str:
    if currency == "JPY":
        return f"{minor:,}"
    sign = "-" if minor < 0 else ""
    return f"{sign}{abs(minor) // 100:,}.{abs(minor) % 100:02d}"


class VendorAgent:
    principal = "vendor_agent"
    subscribes = frozenset(
        {
            "vendor_message",
            "colleague_reply",
            "controller_decision",
            "chip_response",
            "day_boundary",
            "judge_injection",
            "timer",
        }
    )

    def __init__(
        self,
        *,
        store: EventStore,
        ledger: Ledger,
        vendors: VendorMaster,
        packets: PacketStore,
        state: VendorState,
        people: People,
        suppliers: Sequence[Supplier],
        payment_state: PaymentState,
        invoice_state: AgentState,
        schedule: Callable[[Event], None],
        now: Callable[[], datetime],
        provisioner: Provisioner | None = None,
    ) -> None:
        self.store = store
        self.ledger = ledger
        self.vendors = vendors
        self.packets = packets
        self.state = state
        self.people = people
        self.suppliers = {s.id: s for s in suppliers}
        self.index = VendorIndex(list(suppliers), vendors)
        self.payment_state = payment_state
        self.invoice_state = invoice_state
        self._schedule = schedule
        self._now = now
        self.provisioner = provisioner

    # ------------------------------------------------------------------ dispatch

    def handle(self, ev: Event, ctx: Context) -> list[Event]:
        if ev.type == "vendor_message":
            self._on_message(ev, ctx)
        elif ev.type == "colleague_reply":
            self._on_reply(ev, ctx)
        elif ev.type == "controller_decision":
            self._on_decision(ev, ctx)
        elif ev.type == "chip_response":
            self._on_chip_response(ev, ctx)
        elif ev.type == "timer":
            self._on_timer(ev, ctx)
        elif ev.type == "day_boundary":
            self._on_day(ctx)
        return []

    # ------------------------------------------------------------------ messages

    def _vendor_for(self, payload: dict[str, Any]) -> str | None:
        vendor_id = payload.get("vendor_id")
        if vendor_id and self.vendors.get_vendor(str(vendor_id)) is not None:
            return self.index.canonical(str(vendor_id))
        resolved = self.index.resolve(payload.get("claimed_supplier_name"), None)
        if resolved is not None:
            return resolved.vendor_id
        supplier = self.index.by_email(payload.get("from_address"))
        return self.index.canonical(supplier.id) if supplier is not None else None

    def _on_message(self, ev: Event, ctx: Context) -> None:
        p = ev.payload
        vendor_id = self._vendor_for(p)
        known = vendor_id is not None
        cls = classify(p, known_vendor=known)
        supplier = self.suppliers.get(vendor_id or "")
        refs = [
            str(p["message_id"]),
            *([vendor_id] if vendor_id else []),
            str(p.get("thread_id") or ""),
        ]
        if (
            supplier is not None
            and p.get("channel") in ("email", "judge")
            and p.get("from_address")
        ):
            look = lookalike(domain_of(str(p["from_address"])), supplier.domain)
            if look.flagged:
                ctx.act(
                    action_type="domain_flagged",
                    rule_id=None,
                    refs=[*refs, look.from_domain],
                    inputs={
                        "from_domain": look.from_domain,
                        "known_domain": look.known_domain,
                        "channel": p["channel"],
                    },
                    decision={
                        "flagged": True,
                        "distance": look.distance,
                        "reason": look.reason,
                        "blocking": False,
                    },
                )
        ctx.act(
            action_type="message_classified",
            rule_id=None,
            refs=refs,
            inputs={
                "channel": p["channel"],
                "claimed": p.get("claimed_supplier_name"),
                "known_vendor": known,
                "subject": p.get("subject"),
            },
            decision={
                "classification": cls.kind,
                "iban": _mask(cls.iban),
                "vat_id": cls.vat_id,
                "reasons": cls.reasons,
            },
        )
        if cls.kind == "bank_change" and vendor_id is not None:
            self._bank_change(ev, vendor_id, cls, ctx)
        elif cls.kind == "onboarding":
            self._onboarding(ev, cls, ctx)

    # ------------------------------------------------------------------ R8

    def _bank_change(self, ev: Event, vendor_id: str, cls: Classification, ctx: Context) -> None:
        p = ev.payload
        details = p.get("bank_details") or {}
        account = cls.iban or details.get("account_number")
        if not account:
            return
        digest = account_hash(str(account))
        existing = self.vendors.account_by_hash(vendor_id, digest)
        if existing is not None:
            ctx.act(
                action_type="matched",
                rule_id="R8",
                refs=[str(p["message_id"]), vendor_id, existing.id],
                inputs={"iban": _mask(str(account)), "account_status": existing.status},
                decision={
                    "duplicate_request": True,
                    "account_id": existing.id,
                    "status": existing.status,
                },
            )
            return
        channel = str(p.get("channel") or "email")
        verify_via = CHANNEL_MATRIX.get(channel, "phone")
        res = ctx.act(
            action_type="bank_change_opened",
            rule_id="R8",
            refs=[str(p["message_id"]), vendor_id, str(p.get("thread_id") or "")],
            inputs={
                "channel": channel,
                "iban": _mask(str(account)),
                "bank": details.get("bank_name"),
                "sender": p.get("from_address"),
            },
            decision={
                "hold_hours": BANK_CHANGE_HOLD_HOURS,
                "verification_channel": verify_via,
                "status": "pending",
            },
            policy_action=OpenPendingAccountAction(
                vendor_id=vendor_id,
                iban_or_acct=str(account),
                requested_via=channel,
                request_event_id=ev.id,
                hold_hours=BANK_CHANGE_HOLD_HOURS,
            ),
        )
        acct = res.result
        assert isinstance(acct, VendorBankAccount)
        self._request_verification(
            vendor_id,
            acct,
            verify_via,
            ctx,
            kind="bank_change",
            thread_id=str(p.get("thread_id") or ""),
            request_message_id=str(p["message_id"]),
        )

    def _contact_for(self, vendor_id: str, exclude: str | None) -> tuple[str | None, bool]:
        """(person id, via_successor). The supplier's purchasing contact, or the successor when
        that person has left; the plant buyer for vendors the supplier master does not know."""
        supplier = self.suppliers.get(vendor_id)
        vendor = self.vendors.get_vendor(vendor_id)
        contact = (
            supplier.purchasing_contact
            if supplier is not None
            else BUYER_BY_COUNTRY.get(vendor.country if vendor else "DE", "lukas.brandt")
        )
        if not self.people.has(contact):
            contact = BUYER_BY_COUNTRY.get(vendor.country if vendor else "DE", "lukas.brandt")
        via_successor = False
        if exclude == contact:  # the first attempt bounced (M2): try whoever took over
            successor = self.people.successor(contact)
            if successor is None or successor.id == exclude:
                return None, False
            contact, via_successor = successor.id, True
        return contact, via_successor

    def _request_verification(
        self,
        vendor_id: str,
        acct: VendorBankAccount,
        channel: str,
        ctx: Context,
        *,
        kind: str,
        thread_id: str | None,
        request_message_id: str | None,
        exclude_person: str | None = None,
        reason: str | None = None,
    ) -> None:
        vendor = self.vendors.get_vendor(vendor_id)
        name = vendor.legal_name if vendor else vendor_id
        contact, via_successor = self._contact_for(vendor_id, exclude_person)
        if contact is None:
            self._no_contact_packet(vendor_id, acct, ctx, exclude_person)
            return
        person = self.people.person(contact)
        text = (
            f"Hi {person.name.split()[0]}, {name} asks us to pay a new bank account {_mask(acct.iban_or_acct)} "
            f"(request received via {acct.requested_via}). Did they tell you about this change? Please confirm or deny."
        )
        res = ctx.act(
            action_type="verification_requested",
            rule_id="R8",
            refs=[vendor_id, acct.id, person.id, *([thread_id] if thread_id else [])],
            inputs={
                "to": person.id,
                "channel": channel,
                "request_channel": acct.requested_via,
                "message": text,
                "via_successor": via_successor,
                "reason": reason,
            },
            decision={"verification_channel": channel, "person": person.id},
        )
        reply = self.people.reply(
            person.id,
            channel,
            text,
            ctx.now,
            context={
                "kind": "bank_change_verification"
                if kind != "onboarding_account"
                else "onboarding_verification",
                "vendor_id": vendor_id,
                "vendor_name": name,
                "thread_id": thread_id,
                "new_iban_masked": _mask(acct.iban_or_acct),
            },
        )
        delivered = self.people.deliver(
            reply,
            in_reply_to=res.event.id,
            thread_id=thread_id,
            sim_time=ctx.now,
            refs=[vendor_id, acct.id],
        )
        self.state.add_wait(
            kind=kind,
            vendor_id=vendor_id,
            account_id=acct.id,
            person_id=person.id,
            channel=channel,
            thread_id=thread_id,
            request_message_id=request_message_id,
            asked_event_id=res.event.id,
            now=ctx.now,
            data={
                "delivered_event_id": delivered.id if delivered else None,
                "world_reply_expected": delivered is None,
                "via_successor": via_successor,
            },
        )

    def _no_contact_packet(
        self, vendor_id: str, acct: VendorBankAccount, ctx: Context, exclude_person: str | None
    ) -> None:
        vendor = self.vendors.get_vendor(vendor_id)
        name = vendor.legal_name if vendor else vendor_id
        ctx.act(
            action_type="held",
            rule_id="R8",
            refs=[vendor_id, acct.id, *([exclude_person] if exclude_person else [])],
            inputs={"bounced_contact": exclude_person},
            decision={"reason": "no_reachable_contact", "status": "pending"},
        )
        packet = self.packets.create_packet(
            PacketDraft(
                agent=self.principal,
                question=f"Nobody can verify the bank change for {name}: who should we ask?",
                context=f"The purchasing contact {exclude_person or 'on file'} is unreachable and no successor is recorded. The new account {_mask(acct.iban_or_acct)} stays pending.",
                severity="action",
                refs=[vendor_id, acct.id, *([exclude_person] if exclude_person else [])],
                evidence=[
                    PacketEvidence(
                        event_id=acct.request_event_id or "",
                        role="message",
                        summary=f"Request via {acct.requested_via}",
                    )
                ]
                if acct.request_event_id
                else [],
                materiality_minor=self._open_payables_minor(vendor_id)[0],
                currency=vendor.currency if vendor else None,
                recommendation="acknowledge",
                recommendation_reason="a human must name a contact; payments continue to the account on file",
                options=OPTIONS_ACK,
                expires_at=ctx.now + timedelta(days=14),
                consequence_of_silence="Change stays pending. Payments continue to the account on file.",
                kind="anomaly",
            ),
            created_at=ctx.now,
        )
        self.state.add_packet(packet.id, kind="no_contact", vendor_id=vendor_id, account_id=acct.id)

    # ------------------------------------------------------------------ replies

    @staticmethod
    def _verdict(payload: dict[str, Any]) -> str:
        refs = [str(r) for r in payload.get("refs", [])]
        if payload.get("bounced") or "bounced" in refs:
            return "bounced"
        if "denied" in refs:
            return "denied"
        if "confirmed" in refs:
            return "confirmed"
        text = str(payload.get("body") or "").lower()
        if any(
            k in text
            for k in (
                "do not change",
                "did not",
                "nobody",
                "not confirm",
                "never",
                "no.",
                "no,",
                "i do not know",
            )
        ):
            return "denied"
        if "confirm" in text or text.startswith("yes") or " yes" in text:
            return "confirmed"
        return "unclear"

    def _on_reply(self, ev: Event, ctx: Context) -> None:
        p = ev.payload
        waits = self.state.waits_for_person(str(p["person_id"]))
        match: VendorWait | None = None
        for w in waits:
            if p.get("in_reply_to") in (w.asked_event_id, w.request_message_id) or (
                p.get("thread_id") and p.get("thread_id") == w.thread_id
            ):
                match = w
                break
        if match is None:
            return
        verdict = self._verdict(p)
        self.state.resolve_wait(match.id, now=ctx.now, reply_event_id=ev.id, verdict=verdict)
        acct = self.vendors.get_account(match.account_id) if match.account_id else None
        if acct is None:
            return
        if verdict == "bounced":
            self._request_verification(
                match.vendor_id,
                acct,
                match.channel,
                ctx,
                kind=match.kind,
                thread_id=match.thread_id,
                request_message_id=match.request_message_id,
                exclude_person=match.person_id,
            )
            return
        if acct.status != "pending":
            return
        recheck = match.kind in ("phone_recheck", "activation_recheck")
        if not recheck:
            ctx.act(
                action_type="verification_received",
                rule_id="R8",
                refs=[match.vendor_id, acct.id, str(p["person_id"]), ev.id],
                inputs={
                    "channel": p["channel"],
                    "body": str(p.get("body") or "")[:200],
                    "refs": p.get("refs", []),
                },
                decision={"verdict": verdict, "verified_via": p["channel"]},
                policy_action=MarkVerifiedAction(
                    account_id=acct.id, verified_via=str(p["channel"]), verified_event_id=ev.id
                ),
            )
        else:
            # a confirmed call becomes the account's verification: the second channel §7.4 asks
            # for before activation; any other answer leaves the record as it was
            ctx.act(
                action_type="verification_received",
                rule_id="R8",
                refs=[match.vendor_id, acct.id, str(p["person_id"]), ev.id],
                inputs={
                    "channel": p["channel"],
                    "body": str(p.get("body") or "")[:200],
                    "recheck": True,
                    "kind": match.kind,
                },
                decision={
                    "verdict": verdict,
                    "verified_via": "phone" if verdict == "confirmed" else acct.verified_via,
                    "recheck": True,
                },
                policy_action=MarkVerifiedAction(
                    account_id=acct.id, verified_via="phone", verified_event_id=ev.id
                )
                if verdict == "confirmed"
                else None,
            )
        acct = self.vendors.get_account(acct.id) or acct
        if verdict == "denied":
            ctx.act(
                action_type="account_suspended",
                rule_id="R8",
                refs=[match.vendor_id, acct.id, ev.id],
                inputs={"verdict": verdict, "person": p["person_id"], "channel": p["channel"]},
                decision={"reason": "verification_denied"},
                policy_action=SuspendAccountAction(
                    account_id=acct.id,
                    reason=f"verification denied by {p['person_id']} over {p['channel']}",
                ),
            )
            self._denied_packet(match.vendor_id, acct, ev, ctx)
            return
        if match.kind == "activation_recheck" and verdict == "confirmed":
            # approved, hold elapsed and now verified over a second channel
            self._activate(match.vendor_id, acct, ctx)
            return
        extra: list[PacketEvidence] = []
        if recheck:
            extra.append(
                PacketEvidence(
                    event_id=ev.id,
                    role="message",
                    summary=f"Phone verification: {p['person_id']} answered '{verdict}' on {ev.known_at.date().isoformat()}",
                )
            )
        verified_via = "phone" if recheck else str(p["channel"])
        if verdict == "unclear":
            verified_via = "email"  # an unclear answer never counts as a strong verification
        self._raise_change_packet(
            match.vendor_id,
            acct,
            ev,
            ctx,
            verified_via=verified_via,
            extra_rows=extra,
            verdict=verdict,
        )

    def _open_payables_minor(self, vendor_id: str) -> tuple[int, int]:
        rows = [
            p
            for p in self.ledger.payables(vendor_id=vendor_id)
            if p.status in ("open", "scheduled", "held") and p.amount_minor > 0
        ]
        return sum(p.amount_minor for p in rows), len(rows)

    def _raise_change_packet(
        self,
        vendor_id: str,
        acct: VendorBankAccount,
        cause: Event,
        ctx: Context,
        *,
        verified_via: str,
        extra_rows: list[PacketEvidence],
        verdict: str,
    ) -> None:
        vendor = self.vendors.get_vendor(vendor_id)
        name = vendor.legal_name if vendor else vendor_id
        prior = self.vendors.active_account(vendor_id)
        total, count = self._open_payables_minor(vendor_id)
        request_ev = self.store.get(acct.request_event_id) if acct.request_event_id else None
        sender = (
            str(request_ev.payload.get("from_address")) if request_ev is not None else "unknown"
        )
        bank = (
            str((request_ev.payload.get("bank_details") or {}).get("bank_name") or "")
            if request_ev is not None
            else ""
        )
        rows = [
            PacketEvidence(
                event_id=acct.request_event_id or cause.id,
                role="message",
                summary=f"Request: via {acct.requested_via} from {sender} on {acct.requested_at.date().isoformat()}, new account {_mask(acct.iban_or_acct)} {bank}".strip(),
            ),
            PacketEvidence(
                event_id=acct.verified_event_id or cause.id,
                role="message",
                summary=f"Verification: {verified_via} reply '{verdict}' on {cause.known_at.date().isoformat()}",
            ),
            PacketEvidence(
                event_id=acct.request_event_id or cause.id,
                role="decision",
                summary=f"Mismatch: on file {_mask(prior.iban_or_acct) if prior else 'none'} since {prior.requested_at.date().isoformat() if prior else 'n/a'} vs requested {_mask(acct.iban_or_acct)}",
            ),
            PacketEvidence(
                event_id=cause.id,
                role="decision",
                summary=f"Pending payments: {count} payable(s), {_fmt(total, vendor.currency if vendor else 'EUR')} {vendor.currency if vendor else ''}".strip(),
            ),
            *extra_rows,
        ]
        if verified_via == "email":
            recommendation = "request_phone_verification"
            reason = "an email confirmation can come from a compromised mailbox; a call to the contract number settles it"
        else:
            recommendation = "approve"
            reason = f"verified by {verified_via}, a channel other than the request's {acct.requested_via}; hold until {acct.hold_until.isoformat()}"
        packet = self.packets.create_packet(
            PacketDraft(
                agent=self.principal,
                question=f"Approve bank change for {name}?",
                context=f"{name} asks to be paid at {_mask(acct.iban_or_acct)} instead of {_mask(prior.iban_or_acct) if prior else 'no account on file'}. Requested via {acct.requested_via}, verified via {verified_via}; the 72-hour hold ends {acct.hold_until.isoformat()}.",
                severity="action",
                refs=[
                    vendor_id,
                    acct.id,
                    *([acct.request_event_id] if acct.request_event_id else []),
                    *([request_ev.payload.get("message_id")] if request_ev is not None else []),
                ],
                evidence=rows,
                materiality_minor=total,
                currency=vendor.currency if vendor else None,
                recommendation=recommendation,
                recommendation_reason=reason,
                options=OPTIONS_BANK_CHANGE,
                expires_at=acct.hold_until,
                consequence_of_silence="Change stays pending. Payments continue to the account on file.",
                kind="approval",
            ),
            created_at=ctx.now,
        )
        self.state.add_packet(
            packet.id,
            kind="bank_change",
            vendor_id=vendor_id,
            account_id=acct.id,
            data={"verified_via": verified_via},
        )

    def _denied_packet(
        self, vendor_id: str, acct: VendorBankAccount, cause: Event, ctx: Context
    ) -> None:
        vendor = self.vendors.get_vendor(vendor_id)
        supplier = self.suppliers.get(vendor_id)
        name = vendor.legal_name if vendor else vendor_id
        packet = self.packets.create_packet(
            PacketDraft(
                agent=self.principal,
                question=f"Bank change for {name} was denied by the contact on file. Who is asking us to change it?",
                context=f"The request via {acct.requested_via} for {_mask(acct.iban_or_acct)} is suspended; payments continue to the account on file. Contract phone {supplier.phone if supplier else 'on file'}.",
                severity="urgent",
                refs=[
                    vendor_id,
                    acct.id,
                    *([acct.request_event_id] if acct.request_event_id else []),
                ],
                evidence=[
                    PacketEvidence(
                        event_id=acct.request_event_id or cause.id,
                        role="message",
                        summary=f"Request via {acct.requested_via}",
                    ),
                    PacketEvidence(
                        event_id=cause.id, role="message", summary="Verification denied"
                    ),
                ],
                materiality_minor=self._open_payables_minor(vendor_id)[0],
                currency=vendor.currency if vendor else None,
                recommendation="acknowledge",
                recommendation_reason="the account is suspended; purchasing should warn the supplier",
                options=OPTIONS_ACK,
                expires_at=ctx.now + timedelta(days=14),
                consequence_of_silence="The suspended account is never paid.",
                kind="anomaly",
            ),
            created_at=ctx.now,
        )
        self.state.add_packet(packet.id, kind="denied", vendor_id=vendor_id, account_id=acct.id)

    # ------------------------------------------------------------------ decisions

    def _on_decision(self, ev: Event, ctx: Context) -> None:
        pk = self.state.packet(str(ev.payload["packet_id"]))
        if pk is None or pk.decided:
            return
        decision = str(ev.payload["decision"])
        self.state.decide_packet(pk.packet_id, decision)
        if pk.kind == "bank_change" and pk.account_id:
            acct = self.vendors.get_account(pk.account_id)
            if acct is None:
                return
            if decision == "approve":
                if acct.approved_by is None:
                    ctx.with_principal("controller").act(
                        action_type="account_approved",
                        rule_id="R8",
                        refs=[pk.vendor_id, acct.id, ev.id],
                        inputs={
                            "decided_by": ev.payload.get("decided_by"),
                            "via": ev.payload.get("via"),
                            "note": ev.payload.get("note"),
                        },
                        decision={"approved": True, "hold_until": acct.hold_until.isoformat()},
                        policy_action=ApproveAccountAction(
                            account_id=acct.id, approval_event_id=ev.id
                        ),
                    )
                self._activate_or_schedule(pk.vendor_id, acct.id, ctx)
            elif decision == "deny":
                if acct.status == "pending":
                    ctx.act(
                        action_type="account_suspended",
                        rule_id="R8",
                        refs=[pk.vendor_id, acct.id, ev.id],
                        inputs={
                            "decided_by": ev.payload.get("decided_by"),
                            "note": ev.payload.get("note"),
                        },
                        decision={"reason": "denied_by_controller"},
                        policy_action=SuspendAccountAction(
                            account_id=acct.id,
                            reason=f"denied by {ev.payload.get('decided_by')}: {ev.payload.get('note') or ''}",
                        ),
                    )
            elif decision == "request_phone_verification":
                self._request_verification(
                    pk.vendor_id,
                    acct,
                    "phone",
                    ctx,
                    kind="phone_recheck",
                    thread_id=f"recheck:{acct.id}",
                    request_message_id=None,
                )
        elif pk.kind == "onboarding":
            self._onboarding_decided(pk.vendor_id, decision, ev, ctx)
        elif pk.kind == "r9" and pk.pay_id:
            self._r9_decided(pk.vendor_id, pk.pay_id, pk.account_id, decision, ev, ctx)

    def _activate_or_schedule(self, vendor_id: str, account_id: str, ctx: Context) -> None:
        acct = self.vendors.get_account(account_id)
        if acct is None or acct.status != "pending":
            return
        if ctx.now >= acct.hold_until:
            self._activate(vendor_id, acct, ctx)
            return
        key = f"activate:{acct.id}"
        timer = make_event(
            "timer",
            {
                "agent": self.principal,
                "purpose": "activate_account",
                "refs": [acct.id, vendor_id],
                "due_at": acct.hold_until.isoformat(),
                "key": key,
            },
            event_time=acct.hold_until,
            source="vendor_agent",
            actor=self.principal,
        )
        self._schedule(timer)
        self.state.schedule_activation(
            account_id=acct.id, vendor_id=vendor_id, due_at=acct.hold_until, timer_key=key
        )
        ctx.act(
            action_type="activation_scheduled",
            rule_id="R8",
            refs=[vendor_id, acct.id],
            inputs={"hold_until": acct.hold_until.isoformat(), "now": ctx.now.isoformat()},
            decision={
                "activate_at": acct.hold_until.isoformat(),
                "timer_key": key,
                "timer_event_id": timer.id,
            },
        )

    def _activate(self, vendor_id: str, acct: VendorBankAccount, ctx: Context) -> None:
        if acct.verified_via is None or acct.verified_via == acct.requested_via:
            # §7.4: activation needs a verification over another channel than the request. The
            # controller's approval stands; the desk phones the contact on file first and
            # activates on a confirmed call (DECISIONS 71)
            self.state.mark_activation_done(acct.id)
            if not self.state.open_waits(vendor_id, kind="activation_recheck"):
                self._request_verification(
                    vendor_id,
                    acct,
                    "phone",
                    ctx,
                    kind="activation_recheck",
                    thread_id=f"activation:{acct.id}",
                    request_message_id=None,
                    reason="same_channel_before_activation",
                )
            return
        prior = self.vendors.active_account(vendor_id)
        ctx.act(
            action_type="account_activated",
            rule_id="R8",
            refs=[vendor_id, acct.id, *([prior.id] if prior else [])],
            inputs={
                "hold_until": acct.hold_until.isoformat(),
                "approved_by": acct.approved_by,
                "verified_via": acct.verified_via,
            },
            decision={
                "active_account": acct.id,
                "retired": prior.id if prior else None,
                "iban": _mask(acct.iban_or_acct),
            },
            policy_action=ActivateIfReadyAction(account_id=acct.id),
        )
        self.state.mark_activation_done(acct.id)
        if self.provisioner is not None:
            self.provisioner.enqueue(vendor_id)

    def _on_timer(self, ev: Event, ctx: Context) -> None:
        p = ev.payload
        if p.get("agent") != self.principal or p.get("purpose") != "activate_account":
            return
        account_id = str(p["refs"][0])
        acct = self.vendors.get_account(account_id)
        if (
            acct is None
            or acct.status != "pending"
            or acct.approved_by is None
            or ctx.now < acct.hold_until
        ):
            return
        self._try_activate(acct.vendor_id, acct, ctx)

    def _try_activate(self, vendor_id: str, acct: VendorBankAccount, ctx: Context) -> None:
        """A scheduled activation the policy refuses is reported once, never retried every day."""
        try:
            self._activate(vendor_id, acct, ctx)
        except PolicyViolation as exc:
            self.state.mark_activation_done(acct.id)
            self._activation_blocked(vendor_id, acct, exc, ctx)

    def _activation_blocked(
        self, vendor_id: str, acct: VendorBankAccount, exc: PolicyViolation, ctx: Context
    ) -> None:
        vendor = self.vendors.get_vendor(vendor_id)
        name = vendor.legal_name if vendor else vendor_id
        packet = self.packets.create_packet(
            PacketDraft(
                agent=self.principal,
                question=f"Activation of the new account for {name} is blocked ({exc.reason}). What now?",
                context=f"{exc.detail}. The account {_mask(acct.iban_or_acct)} stays pending; payments continue to the account on file.",
                severity="action",
                refs=[vendor_id, acct.id],
                evidence=[],
                materiality_minor=self._open_payables_minor(vendor_id)[0],
                currency=vendor.currency if vendor else None,
                recommendation="acknowledge",
                recommendation_reason="policy refused the activation; a human decides how to proceed",
                options=OPTIONS_ACK,
                expires_at=ctx.now + timedelta(days=14),
                consequence_of_silence="The account stays pending.",
                kind="notice",
            ),
            created_at=ctx.now,
        )
        self.state.add_packet(
            packet.id, kind="activation_blocked", vendor_id=vendor_id, account_id=acct.id
        )

    def _on_day(self, ctx: Context) -> None:
        self.packets.expire_open_packets(ctx.now)
        for sched in self.state.scheduled_activations():
            if sched.due_at <= ctx.now:
                acct = self.vendors.get_account(sched.account_id)
                if acct is not None and acct.status == "pending" and acct.approved_by is not None:
                    self._try_activate(sched.vendor_id, acct, ctx)
                else:
                    self.state.mark_activation_done(sched.account_id)

    # ------------------------------------------------------------------ R9

    def _on_chip_response(self, ev: Event, ctx: Context) -> None:
        p = ev.payload
        if p.get("ok"):
            return
        pay = self.payment_state.payment(str(p["pay_id"]))
        if pay is None:
            return
        acct = self.vendors.get_account(pay.account_id)
        if acct is None:
            return
        chip_reason = p.get("chip_reason") or (p["reasons"][0] if p.get("reasons") else None)
        annotations = list(p.get("desk_annotations") or [])
        if chip_reason not in ("not_allowlisted", "in_hold") and "in_hold" not in annotations:
            return
        just_activated = (
            acct.status == "active"
            and acct.on_chip
            and acct.approved_at is not None
            and ctx.now - acct.approved_at <= timedelta(hours=JUST_ACTIVATED_HOURS)
        )
        if acct.status != "pending" and not just_activated:
            # an active account the chip does not carry yet sits in the provisioning queue: the
            # payment desk holds the payable (not_allowlisted) and the operator provisions it
            return
        vendor_id = acct.vendor_id
        vendor = self.vendors.get_vendor(vendor_id)
        supplier = self.suppliers.get(vendor_id)
        ctx.act(
            action_type="account_suspended",
            rule_id="R9",
            refs=[vendor_id, acct.id, pay.pay_id, ev.id],
            inputs={
                "chip_reason": chip_reason,
                "desk_annotations": annotations,
                "account_status": acct.status,
            },
            decision={"reason": "chip_rejected_changed_account", "suspended": acct.id},
            policy_action=SuspendAccountAction(
                account_id=acct.id, reason=f"chip rejected payment {pay.pay_id}: {chip_reason}"
            ),
        )
        prior = self.vendors.active_account(vendor_id)
        if prior is None and not self.vendors.accounts(vendor_id=vendor_id, status="retired"):
            pass  # a first account: nothing to restore, the packet says so
        elif prior is None:
            res = ctx.act(
                action_type="account_restored",
                rule_id="R9",
                refs=[vendor_id, pay.pay_id, ev.id],
                inputs={"suspended": acct.id},
                decision={"restored": "most recently retired account"},
                policy_action=RestorePriorAction(vendor_id=vendor_id),
            )
            prior = (
                res.result
                if isinstance(res.result, VendorBankAccount)
                else self.vendors.active_account(vendor_id)
            )
        else:
            ctx.act(
                action_type="account_restored",
                rule_id="R9",
                refs=[vendor_id, prior.id, pay.pay_id, ev.id],
                inputs={"suspended": acct.id, "prior_still_active": True},
                decision={
                    "account_on_file": prior.id,
                    "since": prior.requested_at.date().isoformat(),
                },
            )
        for pid in pay.payable_ids:
            self.ledger.set_payable_status(pid, "held", held_reason="awaiting_controller")
        self.payment_state.update_payment(
            pay.pay_id, now=ctx.now, status="held", held_reason="awaiting_controller"
        )
        ctx.act(
            action_type="payment_requeued",
            rule_id="R9",
            refs=[pay.pay_id, vendor_id, *pay.payable_ids],
            inputs={"prior_account": prior.id if prior else None, "suspended": acct.id},
            decision={
                "status": "held",
                "held_reason": "awaiting_controller",
                "requeue_to": prior.id if prior else None,
            },
        )
        phone = supplier.phone if supplier else "the contract number on file"
        name = vendor.legal_name if vendor else vendor_id
        verifier = self.state.waits_for_account(acct.id)
        path = (
            f"{verifier[0].person_id} by {verifier[0].channel}, answered '{verifier[0].verdict}'"
            if verifier
            else "no verification recorded"
        )
        rows = [
            PacketEvidence(
                event_id=acct.request_event_id or ev.id,
                role="message",
                summary=f"Original request: via {acct.requested_via} on {acct.requested_at.date().isoformat()} for {_mask(acct.iban_or_acct)}",
            ),
            PacketEvidence(
                event_id=acct.verified_event_id or ev.id,
                role="message",
                summary=f"Verification path: {path}",
            ),
            PacketEvidence(
                event_id=ev.id,
                role="chip_response",
                summary=f"Chip rejection: {chip_reason}"
                + (f" + desk {', '.join(annotations)}" if annotations else ""),
            ),
            PacketEvidence(
                event_id=prior.request_event_id or ev.id if prior else ev.id,
                role="decision",
                summary=f"Account on file since {prior.requested_at.date().isoformat() if prior else 'n/a'}: {_mask(prior.iban_or_acct) if prior else 'none'}",
            ),
        ]
        packet = self.packets.create_packet(
            PacketDraft(
                agent=self.principal,
                question=f"The chip refused {_fmt(pay.eur_equiv_minor, 'EUR')} EUR to the new account of {name}. Call them on the contract number {phone}?",
                context=f"Payment {pay.pay_id} ({_fmt(pay.amount_minor, pay.currency)} {pay.currency}) targeted {_mask(acct.iban_or_acct)}, approved after a {acct.verified_via or 'pending'} verification. The chip answered {chip_reason}{' and the desk notes ' + ', '.join(annotations) if annotations else ''}. The account is suspended and the payment is held for you. Contract phone: {phone}.",
                severity="urgent",
                refs=[
                    vendor_id,
                    acct.id,
                    pay.pay_id,
                    *pay.payable_ids,
                    *([self._invoice_number(pid) or "" for pid in pay.payable_ids]),
                ],
                evidence=rows,
                materiality_minor=pay.eur_equiv_minor,
                currency="EUR",
                recommendation="call_supplier_on_contract_number",
                recommendation_reason=f"use the number from the contract ({phone}), not any number in the email",
                options=[
                    PacketOption(
                        id="call_supplier_on_contract_number", label=f"Call {name} on {phone}"
                    ),
                    PacketOption(id="pay_prior_account", label="Pay the account on file"),
                    PacketOption(id="cancel_payment", label="Cancel the payment"),
                ],
                expires_at=ctx.now + timedelta(days=7),
                consequence_of_silence="The payment stays held; the suspended account is never paid.",
                kind="anomaly",
            ),
            created_at=ctx.now,
        )
        self.state.add_packet(
            packet.id, kind="r9", vendor_id=vendor_id, account_id=acct.id, pay_id=pay.pay_id
        )

    def _invoice_number(self, payable_id: str) -> str | None:
        payable = self.ledger.get_payable(payable_id)
        if payable is None:
            return None
        row = self.invoice_state.invoice(payable.inv_id)
        return row.invoice_number if row is not None else None

    def _r9_decided(
        self,
        vendor_id: str,
        pay_id: str,
        account_id: str | None,
        decision: str,
        ev: Event,
        ctx: Context,
    ) -> None:
        pay = self.payment_state.payment(pay_id)
        if pay is None:
            return
        if decision == "pay_prior_account":
            for pid in pay.payable_ids:
                self.ledger.set_payable_status(pid, "open")
            self.payment_state.update_payment(
                pay_id, now=ctx.now, status="cancelled", held_reason="requeued_to_prior_account"
            )
            ctx.act(
                action_type="payment_requeued",
                rule_id="R9",
                refs=[pay_id, vendor_id, ev.id],
                inputs={"decision": decision},
                decision={
                    "payables": list(pay.payable_ids),
                    "status": "open",
                    "to": "account on file",
                },
            )
        elif decision == "cancel_payment":
            for pid in pay.payable_ids:
                self.ledger.set_payable_status(pid, "held", held_reason="cancelled_by_controller")
            self.payment_state.update_payment(
                pay_id, now=ctx.now, status="cancelled", held_reason="cancelled_by_controller"
            )
            ctx.act(
                action_type="payment_held",
                rule_id="R9",
                refs=[pay_id, vendor_id, ev.id],
                inputs={"decision": decision},
                decision={
                    "held_reason": "cancelled_by_controller",
                    "payables": list(pay.payable_ids),
                },
            )
        elif decision == "call_supplier_on_contract_number":
            supplier = self.suppliers.get(vendor_id)
            contact = (supplier.purchasing_contact if supplier else None) or BUYER_BY_COUNTRY.get(
                "DE"
            )
            if contact and self.people.has(contact):
                acct = self.vendors.get_account(account_id) if account_id else None
                if acct is not None:
                    self._request_verification(
                        vendor_id,
                        acct,
                        "phone",
                        ctx,
                        kind="phone_recheck",
                        thread_id=f"r9:{pay_id}",
                        request_message_id=None,
                    )

    # ------------------------------------------------------------------ onboarding (R5)

    def _onboarding(self, ev: Event, cls: Classification, ctx: Context) -> None:
        p = ev.payload
        name = str(p.get("claimed_supplier_name") or "unknown vendor").strip()
        existing = self.state.onboarding_by_name(name)
        if existing is not None:
            ctx.act(
                action_type="matched",
                rule_id="R5",
                refs=[str(p["message_id"]), existing.vendor_id, name],
                inputs={"status": existing.status},
                decision={"duplicate_onboarding": True},
            )
            return
        vat = cls.vat_id
        iban = cls.iban
        country = (vat[:2] if vat else None) or (iban[:2] if iban else None) or "DE"
        currency = CURRENCY_BY_COUNTRY.get(country, "EUR")
        vendor_id = f"V-{9000 + self.state.onboarding_count() + 1:04d}"
        invoice_id = str(p.get("thread_id")) if p.get("channel") == "invoice" else None
        facts = self.invoice_state.facts(invoice_id) if invoice_id else None
        validated = (facts or {}).get("validated") or {}
        gross = int(validated.get("gross_minor") or 0)
        inv_ccy = str(validated.get("currency") or currency)
        number = str(validated.get("invoice_number") or "")
        contact = BUYER_BY_COUNTRY.get(country, "lukas.brandt")
        ctx.act(
            action_type="vendor_created",
            rule_id="R5",
            refs=[
                str(p["message_id"]),
                vendor_id,
                name,
                *([invoice_id] if invoice_id else []),
                *([number] if number else []),
            ],
            inputs={
                "vat_id": vat,
                "iban": _mask(iban),
                "country": country,
                "channel": p.get("channel"),
            },
            decision={"vendor_id": vendor_id, "status": "onboarding", "contact": contact},
            policy_action=CreateVendorAction(
                vendor_id=vendor_id,
                legal_name=name,
                vat_id=vat or "",
                country=country,
                currency=currency,
                terms="net30",
                contact_person=contact,
            ),
        )
        self.state.add_onboarding(
            vendor_id=vendor_id,
            legal_name=name,
            vat_id=vat,
            iban=iban,
            invoice_id=invoice_id,
            message_event_id=ev.id,
            status="pending",
            data={"invoice_number": number},
        )
        vat_ok = bool(vat) and vat_is_valid(vat or "")
        iban_ok = bool(iban) and iban_is_valid(iban or "")
        rows = [
            PacketEvidence(
                event_id=ev.id,
                role="message",
                summary=f"VAT ID validity: {vat or 'none given'} is {'a valid EU VAT id format' if vat_ok else 'missing or invalid'}",
            ),
            PacketEvidence(
                event_id=ev.id,
                role="message",
                summary=f"IBAN validity: {_mask(iban)} is {'a valid IBAN' if iban_ok else 'missing or invalid'}",
            ),
            PacketEvidence(
                event_id=ev.id,
                role="message",
                summary=f"First invoice amount: {_fmt(gross, inv_ccy)} {inv_ccy} ({number})"
                if gross
                else "First invoice amount: no invoice yet",
            ),
        ]
        context = f"{name} is not in the vendor master. " + (
            "CRS checks (business credit, OFAC, IDV) were not run: no CRS client is configured in this build."
            if True
            else ""
        )
        packet = self.packets.create_packet(
            PacketDraft(
                agent=self.principal,
                question=f"Onboard {name}?",
                context=context,
                severity="action",
                refs=[
                    vendor_id,
                    name,
                    str(p["message_id"]),
                    *([invoice_id] if invoice_id else []),
                    *([number] if number else []),
                ],
                evidence=rows,
                materiality_minor=gross or None,
                currency=inv_ccy if gross else None,
                recommendation="approve" if (vat_ok and iban_ok) else "deny",
                recommendation_reason="VAT id and IBAN both validate"
                if (vat_ok and iban_ok)
                else "a required identifier is missing or invalid",
                options=OPTIONS_ONBOARD,
                expires_at=ctx.now + timedelta(days=14),
                consequence_of_silence="The vendor stays unknown and its invoice stays held.",
                kind="approval",
            ),
            created_at=ctx.now,
        )
        self.state.add_packet(
            packet.id,
            kind="onboarding",
            vendor_id=vendor_id,
            account_id=None,
            data={"iban": iban, "invoice_id": invoice_id},
        )

    def _onboarding_decided(self, vendor_id: str, decision: str, ev: Event, ctx: Context) -> None:
        case = self.state.onboarding(vendor_id)
        if case is None:
            return
        if decision != "approve":
            self.state.set_onboarding_status(vendor_id, "declined")
            ctx.act(
                action_type="held",
                rule_id="R5",
                refs=[vendor_id, case.legal_name, ev.id],
                inputs={"decision": decision},
                decision={"reason": "onboarding_declined", "status": "onboarding"},
            )
            return
        ctx.with_principal("controller").act(
            action_type="vendor_activated",
            rule_id="R5",
            refs=[vendor_id, case.legal_name, ev.id],
            inputs={"decided_by": ev.payload.get("decided_by")},
            decision={"status": "active"},
            policy_action=ActivateVendorAction(vendor_id=vendor_id, approval_event_id=ev.id),
        )
        self.state.set_onboarding_status(vendor_id, "active")
        if not case.iban:
            return
        res = ctx.act(
            action_type="bank_change_opened",
            rule_id="R8",
            refs=[vendor_id, case.legal_name, case.message_event_id],
            inputs={"channel": "invoice", "iban": _mask(case.iban), "onboarding": True},
            decision={
                "hold_hours": BANK_CHANGE_HOLD_HOURS,
                "verification_channel": CHANNEL_MATRIX["invoice"],
                "status": "pending",
            },
            policy_action=OpenPendingAccountAction(
                vendor_id=vendor_id,
                iban_or_acct=case.iban,
                requested_via="invoice",
                request_event_id=case.message_event_id,
                hold_hours=BANK_CHANGE_HOLD_HOURS,
            ),
        )
        acct = res.result
        assert isinstance(acct, VendorBankAccount)
        self._request_verification(
            vendor_id,
            acct,
            CHANNEL_MATRIX["invoice"],
            ctx,
            kind="onboarding_account",
            thread_id=f"onboarding:{vendor_id}",
            request_message_id=None,
        )
