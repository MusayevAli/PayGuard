"""A stopped desk resumes its recorded results without rerunning opening setup."""

from datetime import timedelta

import pytest
from fastapi.testclient import TestClient

from api.main import create_app
from core.bootstrap import LEGACY_VENDOR_ID, OPENING_DATE, bootstrap
from core.ledger import Evidence, JournalEntryDraft, JournalLineDraft, PostRejected
from tests.integration.test_dashboard import AUTH, NoPaidLLM, make_test_app


def test_restart_after_payments_preserves_results_and_vendor_changes(tmp_path, monkeypatch):
    monkeypatch.setenv("SIGNER", "stub")
    monkeypatch.setenv("PAYGUARD_CONTROLLER_TOKEN", "test-controller")
    app = make_test_app(tmp_path)
    with TestClient(app) as client:
        for scenario in ("normal", "attack"):
            assert client.post(f"/demo/scenarios/{scenario}", headers=AUTH).status_code == 200
        rt = app.state.runtime
        rt.clock.advance_to(rt.clock.sim_now + timedelta(seconds=1))
        assert rt.store.has_type("day_boundary")
        vendor = rt.vendors.get_vendor("V-0040")
        changed = vendor.model_copy(update={"status": "suspended", "legal_name": "Updated name"})
        rt.vendors.upsert_vendor(changed)
        payable = rt.ledger.payables(vendor_id=LEGACY_VENDOR_ID)[0]
        rt.ledger.set_payable_status(payable.id, "held", held_reason="Existing review")
        rt.ledger.close_period("2026-09")
        before = {
            path: client.get(path).json()
            for path in (
                "/dashboard/payments",
                "/dashboard/invoices",
                "/payables",
                "/bank/accounts",
            )
        }
        entries = rt.ledger.entries()
        event_count = rt.store.count()
        accounts = rt.vendors.accounts()
    resumed = create_app(
        world_dir=tmp_path / "world",
        db_path=tmp_path / "desk.db",
        llm=NoPaidLLM(),
        provision=False,
    )
    with TestClient(resumed) as client:
        rt = resumed.state.runtime
        assert rt.ledger.entries() == entries
        assert rt.store.count() == event_count
        assert rt.vendors.get_vendor("V-0040") == changed
        assert rt.vendors.accounts() == accounts
        assert rt.ledger.period_status("2026-09") == "closed"
        for path, expected in before.items():
            assert client.get(path).json() == expected
        assert {row["status"] for row in before["/dashboard/payments"]["payments"]} == {
            "settled",
            "blocked",
        }
        assert client.get("/state").status_code == 200
        assert client.get("/dataset/summary").status_code == 200


def test_resume_does_not_relax_ledger_scope_or_seed_missing_vendors(tmp_path, monkeypatch):
    monkeypatch.setenv("SIGNER", "stub")
    app = make_test_app(tmp_path)
    with TestClient(app):
        rt = app.state.runtime
        rt.clock.advance_to(rt.clock.sim_now + timedelta(seconds=1))
        before = rt.store.count()
        entries = rt.ledger.entries()
        result = bootstrap(rt.ledger, rt.store, rt.vendors, suppliers=rt.suppliers)
        assert result.entries_existing == 6
        assert result.entries_posted == result.payables_created == result.vendors_seeded == 0
        new_draft = JournalEntryDraft(
            effective_date=OPENING_DATE,
            description="Forbidden new opening",
            cause_type="opening",
            cause_id="NEW-OPENING",
            evidence=[Evidence(event_id=result.decision_event_id, role="decision")],
            lines=[
                JournalLineDraft(account="Assets:Bank:EUR", debit_minor=100, currency="EUR"),
                JournalLineDraft(account="Equity:Opening", credit_minor=100, currency="EUR"),
            ],
        )
        with pytest.raises(PostRejected, match="out_of_scope"):
            rt.ledger.post(new_draft, "bootstrap")
        extra = rt.suppliers[0].model_copy(update={"id": "V-NEW"})
        with pytest.raises(PostRejected, match="Existing opening state is incomplete"):
            bootstrap(rt.ledger, rt.store, rt.vendors, suppliers=[*rt.suppliers, extra])
        assert rt.vendors.get_vendor("V-NEW") is None
        assert rt.ledger.entries() == entries
        assert rt.store.count() == before


def test_changed_opening_balances_are_rejected_without_writes(tmp_path, monkeypatch):
    monkeypatch.setenv("SIGNER", "stub")
    app = make_test_app(tmp_path)
    with TestClient(app):
        rt = app.state.runtime
        rt.clock.advance_to(rt.clock.sim_now + timedelta(seconds=1))
        entries, event_count = rt.ledger.entries(), rt.store.count()
        from core.bootstrap import OPENING_BANK_MINOR

        monkeypatch.setitem(OPENING_BANK_MINOR, "EUR", OPENING_BANK_MINOR["EUR"] + 1)
        with pytest.raises(PostRejected, match="Existing opening state is incomplete"):
            bootstrap(rt.ledger, rt.store, rt.vendors, suppliers=rt.suppliers)
        assert rt.ledger.entries() == entries
        assert rt.store.count() == event_count
