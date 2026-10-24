from __future__ import annotations

from datetime import UTC, date, datetime
from pathlib import Path

from core.events import EventStore, make_event
from core.ledger import Evidence, JournalEntryDraft, JournalLineDraft, Ledger
from core.score import Scoreboard
from core.vendors import VendorMaster
from sim.models import AnswerKey, AnswerKeyEntry
from tests.agentkit import full_synthetic_fx

NOW = datetime(2026, 10, 15, tzinfo=UTC)


def test_every_check_kind_and_case_lifecycle(tmp_path: Path) -> None:
    db = tmp_path / "pg.db"
    store = EventStore(db)
    ledger = Ledger(db, events=store, fx=full_synthetic_fx(), now=lambda: NOW)
    vendors = VendorMaster(db)
    inv_ev = store.append(
        make_event(
            "invoice_received",
            {
                "invoice_id": "INV-0001",
                "format": "pdf",
                "document_path": "x.pdf",
                "received_via": "portal",
                "sender": None,
                "supplier_hint": "V-0001",
                "attachments": [],
                "size_bytes": 1,
                "sha256": "0" * 64,
                "pages": 1,
            },
            event_time=NOW,
            source="portal",
        )
    ).event
    key = AnswerKey(
        seed=1,
        headline=[
            AnswerKeyEntry(
                id="T1",
                tier="headline",
                case="T1",
                title="t",
                expected_outcome="o",
                injected_event_ids=[inv_ev.id],
                refs=["INV-0001", "KT-2291"],
                checks=[
                    {
                        "kind": "journal_amount",
                        "entry_match": {"evidence_contains": inv_ev.id},
                        "account": "Liabilities:AP",
                        "credit_minor": 1000,
                    },
                    {
                        "kind": "journal_count",
                        "entry_match": {"evidence_contains": inv_ev.id},
                        "count": 1,
                    },
                    {
                        "kind": "event_exists",
                        "type": "agent_action",
                        "where": {
                            "action_type": "short_pay_notice",
                            "refs_contains": "KT-2291",
                            "decision.short_pay_minor": 5,
                        },
                    },
                    {
                        "kind": "no_event",
                        "type": "escalation",
                        "where": {"refs_contains": "KT-2291"},
                    },
                    {"kind": "payable_status", "inv_id": "INV-0001", "status": "open"},
                    {
                        "kind": "packet_exists",
                        "where": {"refs_contains": "V-0001"},
                        "recommendation": "approve",
                    },
                    {
                        "kind": "chip_response",
                        "where": {"refs_contains": "PAY-1"},
                        "ok": False,
                        "reasons_contains": ["in_hold"],
                    },
                    {
                        "kind": "vendor_account_status",
                        "vendor_id": "V-0001",
                        "iban": "CZ6701000000009198260449",
                        "status": "active",
                    },
                ],
            )
        ],
        ambient=[],
    )
    board = Scoreboard(
        key, store=store, ledger=ledger, vendors=vendors, truth=None, now=lambda: NOW
    )
    report = board.evaluate()
    case = report.cases[0]
    assert (
        case.status == "pending" and report.headline.pending == 1
    )  # injected event not handled yet
    store.mark_handled("invoice_agent", inv_ev.id, NOW)
    report = board.evaluate()
    assert report.cases[0].status == "missed" and report.headline.missed == 1
    failing = {c.kind for c in report.cases[0].checks if not c.ok}
    assert failing == {
        "journal_amount",
        "journal_count",
        "event_exists",
        "payable_status",
        "packet_exists",
        "chip_response",
        "vendor_account_status",
    }
    assert next(c for c in report.cases[0].checks if c.kind == "no_event").ok
    # now satisfy them
    entry = ledger.post(
        JournalEntryDraft(
            effective_date=date(2026, 10, 15),
            description="inv",
            cause_type="invoice",
            cause_id="INV-0001",
            lines=[
                JournalLineDraft(
                    account="Expenses:Materials:Frames", debit_minor=1000, currency="EUR"
                ),
                JournalLineDraft(account="Liabilities:AP", credit_minor=1000, currency="EUR"),
            ],
            evidence=[Evidence(event_id=inv_ev.id, role="invoice")],
        ),
        "invoice_agent",
    )
    ledger.create_payable(
        vendor_id="V-0001",
        inv_id="INV-0001",
        entry_id=entry.id,
        amount_minor=1000,
        currency="EUR",
        due_date=date(2026, 11, 14),
    )
    store.append(
        make_event(
            "agent_action",
            {
                "agent": "invoice_agent",
                "action_type": "short_pay_notice",
                "refs": ["KT-2291"],
                "rule_id": "R1",
                "inputs": {},
                "decision": {"short_pay_minor": 5},
                "explanation": "x",
            },
            event_time=NOW,
            source="agent",
        )
    )
    store.append(
        make_event(
            "escalation",
            {
                "packet_id": "PK-1",
                "agent": "vendor_agent",
                "question": "q",
                "severity": "action",
                "refs": ["V-0001"],
                "context": "c",
                "evidence": [],
                "materiality_minor": None,
                "currency": None,
                "recommendation": "approve",
                "recommendation_reason": "r",
                "options": ["approve"],
                "expires_at": None,
                "auto_action_on_expiry": "hold",
            },
            event_time=NOW,
            source="agent",
        )
    )
    store.append(
        make_event(
            "chip_response",
            {
                "pay_id": "PAY-1",
                "ok": False,
                "tag_hex": None,
                "reasons": ["not_allowlisted", "in_hold"],
                "latency_ms": 3,
                "raw_hex": None,
                "signer": "stub",
            },
            event_time=NOW,
            source="signer",
        )
    )
    from core.vendors import Vendor, VendorBankAccount, account_hash

    vendors.upsert_vendor(
        Vendor(
            id="V-0001",
            legal_name="Kovak",
            vat_id="CZ1",
            country="CZ",
            currency="EUR",
            terms="net30",
            contact_person="p",
            status="active",
            merged_into=None,
        )
    )
    vendors.insert_account(
        VendorBankAccount(
            id="VBA-000001",
            vendor_id="V-0001",
            iban_or_acct="CZ6701000000009198260449",
            account_hash=account_hash("CZ6701000000009198260449"),
            status="active",
            requested_at=NOW,
            requested_via="onboarding",
            requested_by="onboarding",
            request_event_id=None,
            verified_via="contract",
            verified_event_id=None,
            verified_at=NOW,
            hold_until=NOW,
            approved_by="controller",
            approval_event_id=None,
            approved_at=NOW,
            on_chip=False,
            retired_at=None,
        )
    )
    report = board.evaluate()
    assert (
        report.cases[0].status == "caught"
        and report.headline.caught == 1
        and report.headline.missed == 0
    )
    assert report.entries_without_evidence == 0
    # chip_response where/ok/reasons semantics: a wrong reason set fails
    store.append(
        make_event(
            "escalation",
            {
                "packet_id": "PK-2",
                "agent": "invoice_agent",
                "question": "q",
                "severity": "action",
                "refs": ["KT-2291"],
                "context": "c",
                "evidence": [],
                "materiality_minor": None,
                "currency": None,
                "recommendation": "hold",
                "recommendation_reason": "r",
                "options": ["hold"],
                "expires_at": None,
                "auto_action_on_expiry": "hold",
            },
            event_time=NOW,
            source="agent",
        )
    )
    report = board.evaluate()
    assert (
        report.cases[0].status == "missed"
        and not next(c for c in report.cases[0].checks if c.kind == "no_event").ok
    )
    # publish emits one score_update per change and none when unchanged
    first = board.publish()
    assert (
        first is not None
        and first.type == "score_update"
        and first.payload["headline"]["missed"] == 1
    )
    assert board.publish() is None
    ledger.close()
    vendors.close()
    store.close()
