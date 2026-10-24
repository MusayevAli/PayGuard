from __future__ import annotations

import subprocess
import sys
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

from beancount import loader
from beancount.core import realization
from fava.application import create_app
from fava.core import FavaLedger

from core.bootstrap import bootstrap
from core.events import EventStore
from core.ledger import Evidence, JournalEntryDraft, JournalLineDraft, Ledger
from core.policy import Policy, PostAction
from core.vendors import VendorMaster
from sim.fx import load_fx_rates
from sim.models import Event, Supplier
from tests.conftest import load_json

NOW = datetime(2026, 10, 20, 12, 0, tzinfo=UTC)


def L(account: str, debit: int = 0, credit: int = 0, currency: str = "EUR") -> JournalLineDraft:
    return JournalLineDraft(
        account=account, debit_minor=debit, credit_minor=credit, currency=currency
    )


def test_bean_check_zero_errors_and_fava_matches_ledger(
    world_dir: Path, tmp_path: Path, world_events: list[Event]
) -> None:
    db = tmp_path / "payguard.db"
    bean = tmp_path / "ledger" / "payguard.beancount"
    store = EventStore(db)
    store.append_many(world_events)
    ledger = Ledger(
        db,
        events=store,
        fx=load_fx_rates(world_dir / "fx_rates.json"),
        now=lambda: NOW,
        beancount_path=bean,
    )
    vendors = VendorMaster(db)
    suppliers = [Supplier.model_validate(s) for s in load_json(world_dir / "suppliers.json")]
    policy = Policy(
        ledger=ledger, vendors=vendors, events=store, jumper_present=lambda: False, now=lambda: NOW
    )
    try:
        bootstrap(ledger, store, vendors, suppliers=suppliers)
        invoice_events = [e for e in world_events if e.type == "invoice_received"][:40]
        statement_lines = [e for e in world_events if e.type == "bank_statement_line"]
        posted = 0
        for i, ev in enumerate(invoice_events[:18]):
            ccy = ["CZK", "EUR", "PLN"][i % 3]
            net = 100_000 + i * 12_345
            vat = net * 19 // 100 if ccy == "EUR" else 0
            lines = [
                L("Expenses:Materials:Frames", debit=net, currency=ccy),
                L("Liabilities:AP", credit=net + vat, currency=ccy),
            ]
            if vat:
                lines.insert(1, L("Expenses:VAT:Input", debit=vat, currency=ccy))
            draft = JournalEntryDraft(
                effective_date=date(2026, 10, 1) + timedelta(days=i),
                description=f"Invoice {ev.payload['invoice_id']}",
                cause_type="invoice",
                cause_id=ev.payload["invoice_id"],
                lines=lines,
                evidence=[Evidence(event_id=ev.id, role="invoice")],
            )
            entry = policy.execute(PostAction(draft=draft), "invoice_agent")
            ledger.create_payable(
                vendor_id="V-0001",
                inv_id=ev.payload["invoice_id"],
                entry_id=entry.id,
                amount_minor=net + vat,
                currency=ccy,
                due_date=date(2026, 11, 30),
            )
            posted += 1
        # a tooling invoice with retention
        ev = invoice_events[20]
        entry = policy.execute(
            PostAction(
                draft=JournalEntryDraft(
                    effective_date=date(2026, 10, 9),
                    description="Tooling with retention",
                    cause_type="invoice",
                    cause_id=ev.payload["invoice_id"],
                    lines=[
                        L("Expenses:Tooling", debit=68_000_000),
                        L("Liabilities:AP", credit=61_200_000),
                        L("Liabilities:AP:Retention", credit=6_800_000),
                    ],
                    evidence=[Evidence(event_id=ev.id, role="invoice")],
                )
            ),
            "invoice_agent",
        )
        ledger.create_payable(
            vendor_id="V-0016",
            inv_id=ev.payload["invoice_id"],
            entry_id=entry.id,
            amount_minor=61_200_000,
            currency="EUR",
            due_date=date(2026, 11, 20),
            retention_minor=6_800_000,
        )
        posted += 1
        # the FX conversion from the 8 Oct statement, posted by the payment agent (multi-currency, residual to rounding)
        fx_line = next(
            e
            for e in statement_lines
            if e.payload["txn_type"] == "NFEX" and e.payload["currency"] == "CZK"
        )
        fx_eur = next(
            e
            for e in statement_lines
            if e.payload["txn_type"] == "NFEX" and e.payload["currency"] == "EUR"
        )
        fx_entry = policy.execute(
            PostAction(
                draft=JournalEntryDraft(
                    effective_date=date(2026, 10, 8),
                    description="FX conversion EUR/CZK FX-2026-1008",
                    cause_type="statement",
                    cause_id=fx_line.payload["statement_id"],
                    lines=[
                        L(
                            "Assets:Bank:CZK",
                            debit=fx_line.payload["amount_minor"] + 1,
                            currency="CZK",
                        ),
                        L("Assets:Bank:EUR", credit=fx_eur.payload["amount_minor"]),
                    ],
                    evidence=[
                        Evidence(event_id=fx_line.id, role="message"),
                        Evidence(event_id=fx_eur.id, role="message"),
                    ],
                )
            ),
            "payment_agent",
        )
        posted += 1
        assert posted == 20
        assert any(ln.account == "Expenses:FX:Rounding" for ln in fx_entry.lines) or sum(
            ln.eur_equiv_minor for ln in fx_entry.lines if ln.debit_minor
        ) == sum(ln.eur_equiv_minor for ln in fx_entry.lines if ln.credit_minor)
        assert ledger.ap_control_reconciles().ok

        # bean-check: zero errors, no output
        proc = subprocess.run(
            [sys.executable, "-m", "beancount.scripts.check", str(bean)],
            capture_output=True,
            text=True,
            check=False,
        )
        assert proc.returncode == 0, proc.stdout + proc.stderr
        assert proc.stdout.strip() == "" and proc.stderr.strip() == ""
        entries, errors, _ = loader.load_file(str(bean))
        assert errors == []
        transactions = [e for e in entries if type(e).__name__ == "Transaction"]
        assert len(transactions) == 6 + 20

        # Fava loads it with zero errors and serves the balance sheet
        fava = FavaLedger(str(bean))
        fava.load_file()
        assert fava.errors == []
        app = create_app([str(bean)])
        resp = app.test_client().get("/payguard/api/balance_sheet?time=2026")
        assert resp.status_code == 200

        # realized balances per account and currency equal the ledger's own balances
        root = realization.realize(entries)
        ours = ledger.balances(date(2026, 12, 31))
        for account, by_ccy in ours.items():
            real = realization.get(root, account)
            assert real is not None, account
            theirs = {pos.units.currency: pos.units.number for pos in real.balance}
            for ccy, minor in by_ccy.items():
                digits = 0 if ccy == "JPY" else 2
                assert theirs.get(ccy, 0) * (10**digits) == minor, (account, ccy, theirs, minor)
            for ccy, number in theirs.items():
                assert ccy in by_ccy or number == 0, (account, ccy, number)
    finally:
        ledger.close()
        vendors.close()
        store.close()
