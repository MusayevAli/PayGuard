"""One-time bootstrap: opening bank balances (spec §2.7) against ``Equity:Opening`` dated
2026-09-30, opening AP for the legacy invoices the 2 Oct batch payment settles (see
docs/DECISIONS.md), matching open payables, and the vendor master seeded from the world.
Idempotent: a second run posts nothing new."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, date, datetime, timedelta
from typing import Final

from pydantic import BaseModel, ConfigDict

from core.accounts import AP_ACCOUNT, BANK_ACCOUNT_BY_CURRENCY, OPENING_EQUITY_ACCOUNT
from core.events import EventStore, make_event
from core.ledger import (
    Evidence,
    JournalEntryDraft,
    JournalLineDraft,
    Ledger,
    PostRejected,
    entry_content_hash,
)
from core.vendors import VendorMaster
from sim.models import Supplier

OPENING_DATE: Final[date] = date(2026, 9, 30)
OPENING_FX_RATE_DATE: Final[date] = date(2026, 10, 1)
"""The ECB table starts on 1 Oct; opening CZK/PLN balances are valued at that first published
rate, and the journal lines record it as their rate_date (no hidden fallback in convert())."""
OPENING_POSTED_AT: Final[datetime] = datetime(2026, 9, 30, 23, 59, 59, tzinfo=UTC)
OPENING_BANK_MINOR: Final[dict[str, int]] = {
    "EUR": 196_200_000,
    "CZK": 840_000_000,
    "PLN": 210_000_000,
}

LEGACY_VENDOR_ID: Final[str] = "V-0020"
LEGACY_VENDOR_NAME: Final[str] = "Katowice Metal Forming S.A."
LEGACY_INVOICES: Final[tuple[tuple[str, date, int], ...]] = (
    ("KMF/2026/09/0412", date(2026, 9, 2), 6_248_000),
    ("KMF/2026/09/0413", date(2026, 9, 5), 7_115_012),
    ("KMF/2026/09/0417", date(2026, 9, 9), 5_380_000),
)
BATCH_TOTAL_MINOR: Final[int] = sum(amount for _, _, amount in LEGACY_INVOICES)
assert BATCH_TOTAL_MINOR == 18_743_012


class BootstrapResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    decision_event_id: str
    entries_posted: int
    entries_existing: int
    payables_created: int
    vendors_seeded: int


def _opening_drafts(evidence: list[Evidence]) -> list[JournalEntryDraft]:
    drafts = [
        JournalEntryDraft(
            effective_date=OPENING_DATE,
            description=f"Opening balance {BANK_ACCOUNT_BY_CURRENCY[currency]}",
            cause_type="opening",
            cause_id=f"OPENING-{currency}",
            evidence=evidence,
            lines=[
                JournalLineDraft(
                    account=BANK_ACCOUNT_BY_CURRENCY[currency],
                    debit_minor=minor,
                    currency=currency,
                    rate_date=OPENING_FX_RATE_DATE,
                ),
                JournalLineDraft(
                    account=OPENING_EQUITY_ACCOUNT,
                    credit_minor=minor,
                    currency=currency,
                    rate_date=OPENING_FX_RATE_DATE,
                ),
            ],
        )
        for currency, minor in OPENING_BANK_MINOR.items()
    ]
    drafts.extend(
        JournalEntryDraft(
            effective_date=OPENING_DATE,
            description=f"Opening AP {number} {LEGACY_VENDOR_NAME}",
            cause_type="opening",
            cause_id=number,
            evidence=evidence,
            lines=[
                JournalLineDraft(
                    account=OPENING_EQUITY_ACCOUNT, debit_minor=amount, currency="EUR"
                ),
                JournalLineDraft(account=AP_ACCOUNT, credit_minor=amount, currency="EUR"),
            ],
        )
        for number, _, amount in LEGACY_INVOICES
    )
    return drafts


def bootstrap(
    ledger: Ledger,
    events: EventStore,
    vendors: VendorMaster,
    *,
    suppliers: Sequence[Supplier],
    principal: str = "bootstrap",
    posted_at: datetime = OPENING_POSTED_AT,
) -> BootstrapResult:
    decision = make_event(
        "controller_decision",
        {
            "packet_id": "BOOTSTRAP-OPENING",
            "decision": "opening_balances",
            "note": "Opening balances at 2026-09-30 per spec §2.7; legacy AP for the September invoices settled by PAY-BATCH-0001 on 2 Oct",
            "decided_by": "controller",
            "via": "api",
        },
        event_time=posted_at - timedelta(minutes=1),
        source="controller",
        actor=principal,
    )
    evidence = [Evidence(event_id=decision.id, role="decision")]
    drafts = _opening_drafts(evidence)
    if events.has_type("day_boundary"):
        # After the desk has run, bootstrap is verification only. Reposting an
        # identical draft would fail the ledger's scope check before its duplicate
        # check; reseeding vendors could also overwrite a later suspension.
        # Never relax that scope check or repair missing opening state here.
        recorded = {entry.content_hash: entry for entry in ledger.entries()}
        prior = [recorded.get(entry_content_hash(draft)) for draft in drafts]
        payables = {item.inv_id: item for item in ledger.payables(vendor_id=LEGACY_VENDOR_ID)}
        complete = (
            events.get(decision.id) is not None
            and all(entry is not None and entry.posted_by == principal for entry in prior)
            and all(vendors.get_vendor(supplier.id) is not None for supplier in suppliers)
        )
        for (number, _, _), entry in zip(LEGACY_INVOICES, prior[3:], strict=True):
            payable = payables.get(number)
            complete = complete and (
                entry is not None
                and payable is not None
                and payable.entry_id == entry.id
                and payable.currency == "EUR"
            )
        if not complete:
            raise PostRejected(
                "out_of_scope",
                "Existing opening state is incomplete or differs from this dataset; "
                "restart cannot create or change opening balances after the desk has run",
            )
        return BootstrapResult(
            decision_event_id=decision.id,
            entries_posted=0,
            entries_existing=len(drafts),
            payables_created=0,
            vendors_seeded=0,
        )
    decision = events.append(decision).event
    posted = existing = payables_created = 0

    def post(draft: JournalEntryDraft) -> str:
        nonlocal posted, existing
        try:
            entry = ledger.post(draft, principal, posted_at=posted_at)
            posted += 1
            return entry.id
        except PostRejected as exc:
            if exc.reason_code != "duplicate" or exc.existing_entry_id is None:
                raise
            existing += 1
            return exc.existing_entry_id

    for draft in drafts[:3]:
        post(draft)
    for (number, invoice_date, amount), draft in zip(LEGACY_INVOICES, drafts[3:], strict=True):
        entry_id = post(draft)
        before = {p.inv_id for p in ledger.payables(vendor_id=LEGACY_VENDOR_ID)}
        ledger.create_payable(
            vendor_id=LEGACY_VENDOR_ID,
            inv_id=number,
            entry_id=entry_id,
            amount_minor=amount,
            currency="EUR",
            due_date=invoice_date + timedelta(days=30),
            status="scheduled",  # already in the bank batch PAY-BATCH-0001 of 2 Oct
        )
        if number not in before:
            payables_created += 1
    seeded = vendors.seed_from_suppliers(suppliers, now=posted_at)
    return BootstrapResult(
        decision_event_id=decision.id,
        entries_posted=posted,
        entries_existing=existing,
        payables_created=payables_created,
        vendors_seeded=seeded,
    )
