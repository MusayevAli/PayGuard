from __future__ import annotations

from datetime import date
from pathlib import Path

from core.bootstrap import LEGACY_INVOICES, LEGACY_VENDOR_ID, bootstrap
from core.events import EventStore
from core.ledger import Ledger
from core.vendors import VendorMaster
from sim.fx import load_fx_rates
from sim.models import Event, Supplier
from sim.mt940 import parse_mt940
from tests.conftest import load_json


def test_bootstrap_is_idempotent_and_reconciles(
    world_dir: Path, tmp_path: Path, world_events: list[Event]
) -> None:
    db = tmp_path / "payguard.db"
    store = EventStore(db)
    ledger = Ledger(
        db,
        events=store,
        fx=load_fx_rates(world_dir / "fx_rates.json"),
        beancount_path=tmp_path / "ledger" / "payguard.beancount",
    )
    vendors = VendorMaster(db)
    suppliers = [Supplier.model_validate(s) for s in load_json(world_dir / "suppliers.json")]
    try:
        first = bootstrap(ledger, store, vendors, suppliers=suppliers)
        assert (
            first.entries_posted == 6
            and first.entries_existing == 0
            and first.payables_created == 3
            and first.vendors_seeded == 40
        )
        assert store.get(first.decision_event_id) is not None
        second = bootstrap(ledger, store, vendors, suppliers=suppliers)
        assert (
            second.entries_posted == 0
            and second.entries_existing == 6
            and second.payables_created == 0
        )
        assert second.decision_event_id == first.decision_event_id
        assert len(ledger.entries()) == 6
        assert store.count() == 1  # one bootstrap decision event, appended once
        rec = ledger.ap_control_reconciles()
        assert rec.ok, rec
        as_of = date(2026, 9, 30)
        assert ledger.balance("Assets:Bank:EUR", as_of) == {"EUR": 196_200_000}
        assert ledger.balance("Assets:Bank:CZK", as_of) == {"CZK": 840_000_000}
        assert ledger.balance("Assets:Bank:PLN", as_of) == {"PLN": 210_000_000}
        assert ledger.balance("Liabilities:AP", as_of) == {"EUR": -18_743_012}
        assert ledger.balance("Equity:Opening", as_of) == {
            "EUR": -(196_200_000 - 18_743_012),
            "CZK": -840_000_000,
            "PLN": -210_000_000,
        }
        payables = ledger.ap_open_items(vendor_id=LEGACY_VENDOR_ID)
        assert sorted(p.inv_id for p in payables) == sorted(num for num, _, _ in LEGACY_INVOICES)
        assert sum(p.amount_minor for p in payables) == 18_743_012 and all(
            p.status == "open" and p.currency == "EUR" for p in payables
        )
        assert all(p.due_date >= date(2026, 10, 2) for p in payables)
        for entry in ledger.entries():
            assert (
                entry.posted_by == "bootstrap"
                and entry.cause_type == "opening"
                and entry.effective_date == as_of
            )
            assert [e.event_id for e in entry.evidence] == [first.decision_event_id]
        # the 2 Oct batch payment in the world statements settles exactly these invoices
        stmt = next(
            e
            for e in world_events
            if e.type == "bank_statement"
            and e.payload["currency"] == "EUR"
            and e.payload["statement_date"] == "2026-10-02"
        )
        batch = next(
            ln
            for ln in parse_mt940(stmt.payload["mt940"]).lines
            if ln.customer_ref == "PAY-BATCH-0001"
        )
        assert batch.amount_minor == 18_743_012
        for num, _, _ in LEGACY_INVOICES:
            assert num in batch.remittance
        vendor = vendors.get_vendor("V-0001")
        assert vendor is not None and vendor.legal_name == "Kovak Trim s.r.o."
        assert vendors.active_account("V-0001").iban_or_acct == next(
            s.iban for s in suppliers if s.id == "V-0001"
        )
        assert vendors.active_account("V-0040").iban_or_acct  # local US account format
        assert (
            vendors.get_vendor("V-0033").merged_into == "V-0007"
        )  # M1: the duplicate record points at the canonical vendor
    finally:
        ledger.close()
        vendors.close()
        store.close()
