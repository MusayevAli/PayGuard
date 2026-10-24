from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import pytest

from core.events import EventStore, make_event
from core.ledger import Evidence, JournalEntryDraft, JournalLineDraft, Ledger, PostRejected
from core.policy import (
    PRINCIPALS,
    ActivateIfReadyAction,
    ApproveAccountAction,
    ClosePeriodAction,
    MarkVerifiedAction,
    OpenPendingAccountAction,
    Policy,
    PolicyViolation,
    PostAction,
    RestorePriorAction,
    Scope,
    SetOnChipAction,
    SuspendAccountAction,
    account_in_scope,
)
from core.vendors import Vendor, VendorBankAccount, VendorMaster, account_hash
from tests.conftest import synthetic_fx

NOW = datetime(2026, 10, 15, 9, 0, tzinfo=UTC)


@dataclass
class Env:
    store: EventStore
    ledger: Ledger
    vendors: VendorMaster
    policy: Policy
    ev: str
    jumper: dict[str, bool]
    clock: dict[str, datetime]


@pytest.fixture
def env(tmp_path: Path) -> Env:
    db = tmp_path / "payguard.db"
    store = EventStore(db)
    ev = store.append(
        make_event(
            "vendor_message",
            {
                "message_id": "<m1@kovak-trim.cz>",
                "channel": "email",
                "vendor_id": None,
                "claimed_supplier_name": "Kovak Trim s.r.o.",
                "from_address": "accounts@kovak-trim.cz",
                "reply_to": None,
                "to": "ap@tbeu.example",
                "subject": "change",
                "body": "IBAN ...",
                "eml_path": None,
                "in_reply_to": None,
                "thread_id": "thr_1",
                "attachments": [],
                "bank_details": None,
            },
            event_time=NOW,
            source="email",
        )
    ).event.id
    jumper = {"on": False}
    clock = {"now": NOW}
    ledger = Ledger(db, events=store, fx=synthetic_fx(), now=lambda: clock["now"])
    vendors = VendorMaster(db)
    vendors.upsert_vendor(
        Vendor(
            id="V-0001",
            legal_name="Kovak Trim s.r.o.",
            vat_id="CZ71310983",
            country="CZ",
            currency="EUR",
            terms="net30",
            contact_person="petra.vlcek",
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
            requested_at=datetime(2018, 3, 12, tzinfo=UTC),
            requested_via="onboarding",
            requested_by="onboarding",
            request_event_id=None,
            verified_via="contract",
            verified_event_id=None,
            verified_at=datetime(2018, 3, 12, tzinfo=UTC),
            hold_until=datetime(2018, 3, 15, tzinfo=UTC),
            approved_by="controller",
            approval_event_id=None,
            approved_at=datetime(2018, 3, 15, tzinfo=UTC),
            on_chip=True,
            retired_at=None,
        )
    )
    policy = Policy(
        ledger=ledger,
        vendors=vendors,
        events=store,
        jumper_present=lambda: jumper["on"],
        now=lambda: clock["now"],
    )
    yield Env(
        store=store,
        ledger=ledger,
        vendors=vendors,
        policy=policy,
        ev=ev,
        jumper=jumper,
        clock=clock,
    )
    ledger.close()
    vendors.close()
    store.close()


def violations(env: Env) -> list[dict]:
    return [
        e.payload
        for e in env.store.query(type="agent_action", limit=1000)
        if e.payload["action_type"] == "policy_violation"
    ]


def test_principals_table_matches_spec() -> None:
    assert set(PRINCIPALS) == {
        "invoice_agent",
        "vendor_agent",
        "payment_agent",
        "controller",
        "provisioner",
    }
    assert PRINCIPALS["invoice_agent"] == Scope(
        post=frozenset({"Liabilities:AP*", "Liabilities:GRNI", "Expenses:*"}),
        request_payment=False,
        edit_vendor=False,
        edit_allowlist=False,
    )
    assert PRINCIPALS["vendor_agent"] == Scope(
        post=frozenset(), request_payment=False, edit_vendor=True, edit_allowlist=False
    )
    assert PRINCIPALS["payment_agent"] == Scope(
        post=frozenset({"Assets:Bank:*", "Liabilities:AP*", "Expenses:Bank:*", "Expenses:FX:*"}),
        request_payment=True,
        edit_vendor=False,
        edit_allowlist=False,
    )
    assert PRINCIPALS["controller"] == Scope(approve=True)
    assert PRINCIPALS["provisioner"] == Scope(edit_allowlist=True)
    assert (
        PRINCIPALS["controller"].post == frozenset()
        and PRINCIPALS["controller"].edit_vendor is False
    )


@pytest.mark.parametrize(
    ("principal", "account", "allowed"),
    [
        ("invoice_agent", "Liabilities:AP", True),
        ("invoice_agent", "Liabilities:AP:Retention", True),
        ("invoice_agent", "Liabilities:GRNI", True),
        ("invoice_agent", "Expenses:Materials:Foam", True),
        ("invoice_agent", "Expenses:FX:Rounding", True),
        ("invoice_agent", "Assets:Bank:EUR", False),
        ("invoice_agent", "Liabilities:VAT:Output", False),
        ("invoice_agent", "Equity:Opening", False),
        ("invoice_agent", "Assets:Prepayments", False),
        ("vendor_agent", "Liabilities:AP", False),
        ("vendor_agent", "Expenses:Freight", False),
        ("vendor_agent", "Assets:Bank:EUR", False),
        ("payment_agent", "Assets:Bank:CZK", True),
        ("payment_agent", "Liabilities:AP", True),
        ("payment_agent", "Liabilities:AP:Retention", True),
        ("payment_agent", "Expenses:Bank:Fees", True),
        ("payment_agent", "Expenses:FX:Rounding", True),
        ("payment_agent", "Expenses:FX:Realized", True),
        ("payment_agent", "Expenses:Materials:Foam", False),
        ("payment_agent", "Liabilities:GRNI", False),
        ("payment_agent", "Equity:Opening", False),
        ("controller", "Equity:Opening", True),
        ("controller", "Assets:Bank:EUR", True),
        ("controller", "Liabilities:VAT:Output", True),
        ("provisioner", "Liabilities:AP", False),
        ("provisioner", "Expenses:Freight", False),
        ("nobody", "Liabilities:AP", False),
    ],
)
def test_scope_positive_and_negative(principal: str, account: str, allowed: bool) -> None:
    assert account_in_scope(principal, account) is allowed


def _draft(
    ev: str, account: str = "Expenses:Materials:Foam", counter: str = "Liabilities:AP"
) -> JournalEntryDraft:
    return JournalEntryDraft(
        effective_date=date(2026, 10, 15),
        description="test",
        cause_type="invoice",
        cause_id="INV-0001",
        lines=[
            JournalLineDraft(account=account, debit_minor=100, credit_minor=0, currency="EUR"),
            JournalLineDraft(account=counter, debit_minor=0, credit_minor=100, currency="EUR"),
        ],
        evidence=[Evidence(event_id=ev, role="invoice")],
    )


def test_execute_post_success_and_post_rejected_event(env: Env) -> None:
    entry = env.policy.execute(PostAction(draft=_draft(env.ev)), "invoice_agent")
    assert entry.id == "JE-000001"
    with pytest.raises(PostRejected) as info:
        env.policy.execute(
            PostAction(draft=_draft(env.ev, account="Assets:Bank:EUR")), "invoice_agent"
        )
    assert info.value.reason_code == "out_of_scope"
    rejected = [
        e
        for e in env.store.query(type="agent_action")
        if e.payload["action_type"] == "post_rejected"
    ]
    assert len(rejected) == 1
    assert rejected[0].payload["agent"] == "invoice_agent"
    assert rejected[0].payload["decision"]["reason_code"] == "out_of_scope"
    assert "Assets:Bank:EUR" in rejected[0].payload["explanation"]
    assert "INV-0001" in rejected[0].payload["refs"]


def test_unknown_principal_and_unknown_action_are_violations(env: Env) -> None:
    with pytest.raises(PolicyViolation):
        env.policy.execute(PostAction(draft=_draft(env.ev)), "intruder")
    assert violations(env)[-1]["agent"] == "intruder"


def test_vendor_account_lifecycle_through_policy(env: Env) -> None:
    acct = env.policy.execute(
        OpenPendingAccountAction(
            vendor_id="V-0001",
            iban_or_acct="CZ6220100000005459621954",
            requested_via="email",
            request_event_id=env.ev,
        ),
        "vendor_agent",
    )
    assert (
        acct.status == "pending"
        and acct.requested_by == "vendor_agent"
        and acct.hold_until == NOW + timedelta(hours=72)
    )
    assert acct.account_hash == account_hash("CZ6220100000005459621954") and acct.on_chip is False
    with pytest.raises(PolicyViolation, match="not_verified"):
        env.policy.execute(ActivateIfReadyAction(account_id=acct.id), "vendor_agent")
    acct = env.policy.execute(
        MarkVerifiedAction(account_id=acct.id, verified_via="phone", verified_event_id=env.ev),
        "vendor_agent",
    )
    assert acct.verified_via == "phone" and acct.verified_at == NOW
    with pytest.raises(PolicyViolation, match="not_approved"):
        env.policy.execute(ActivateIfReadyAction(account_id=acct.id), "vendor_agent")
    with pytest.raises(PolicyViolation):  # vendor_agent cannot approve
        env.policy.execute(
            ApproveAccountAction(account_id=acct.id, approval_event_id=env.ev), "vendor_agent"
        )
    acct = env.policy.execute(
        ApproveAccountAction(account_id=acct.id, approval_event_id=env.ev), "controller"
    )
    assert acct.approved_by == "controller" and acct.approved_at == NOW
    with pytest.raises(PolicyViolation, match="hold_not_elapsed"):
        env.policy.execute(ActivateIfReadyAction(account_id=acct.id), "vendor_agent")
    env.clock["now"] = NOW + timedelta(hours=72)
    acct = env.policy.execute(ActivateIfReadyAction(account_id=acct.id), "vendor_agent")
    assert acct.status == "active"
    old = env.vendors.get_account("VBA-000001")
    assert (
        old is not None and old.status == "retired" and old.retired_at == NOW + timedelta(hours=72)
    )
    assert env.vendors.active_account("V-0001").id == acct.id
    assert len([a for a in env.vendors.accounts(vendor_id="V-0001") if a.status == "active"]) == 1
    # suspend the new one and restore the prior (R9)
    suspended = env.policy.execute(
        SuspendAccountAction(account_id=acct.id, reason="chip rejected not_allowlisted"),
        "vendor_agent",
    )
    assert suspended.status == "suspended"
    restored = env.policy.execute(RestorePriorAction(vendor_id="V-0001"), "vendor_agent")
    assert (
        restored.id == "VBA-000001" and restored.status == "active" and restored.retired_at is None
    )
    assert env.vendors.active_account("V-0001").id == "VBA-000001"


def test_requested_by_must_differ_from_approved_by(env: Env) -> None:
    acct = env.vendors.insert_account(
        VendorBankAccount(
            id="VBA-000077",
            vendor_id="V-0001",
            iban_or_acct="CZ5500000000000000000001",
            account_hash=account_hash("x"),
            status="pending",
            requested_at=NOW,
            requested_via="portal",
            requested_by="controller",
            request_event_id=env.ev,
            verified_via="phone",
            verified_event_id=env.ev,
            verified_at=NOW,
            hold_until=NOW,
            approved_by=None,
            approval_event_id=None,
            approved_at=None,
            on_chip=False,
            retired_at=None,
        )
    )
    with pytest.raises(PolicyViolation, match="requested_by"):
        env.policy.execute(
            ApproveAccountAction(account_id=acct.id, approval_event_id=env.ev), "controller"
        )
    assert violations(env)[-1]["decision"]["reason"] == "requested_by_equals_approved_by"


def test_activation_precondition_same_channel(env: Env) -> None:
    acct = env.policy.execute(
        OpenPendingAccountAction(
            vendor_id="V-0001",
            iban_or_acct="CZ6220100000005459621954",
            requested_via="email",
            request_event_id=env.ev,
        ),
        "vendor_agent",
    )
    env.policy.execute(
        MarkVerifiedAction(account_id=acct.id, verified_via="email", verified_event_id=env.ev),
        "vendor_agent",
    )
    env.policy.execute(
        ApproveAccountAction(account_id=acct.id, approval_event_id=env.ev), "controller"
    )
    env.clock["now"] = NOW + timedelta(days=4)
    with pytest.raises(PolicyViolation, match="same_channel"):
        env.policy.execute(ActivateIfReadyAction(account_id=acct.id), "vendor_agent")
    assert env.vendors.get_account(acct.id).status == "pending"


def test_on_chip_only_for_provisioner_with_jumper_on_an_active_account(env: Env) -> None:
    pending = env.policy.execute(
        OpenPendingAccountAction(
            vendor_id="V-0001",
            iban_or_acct="CZ6220100000005459621954",
            requested_via="email",
            request_event_id=env.ev,
        ),
        "vendor_agent",
    )
    with pytest.raises(PolicyViolation, match="edit_allowlist"):
        env.policy.execute(SetOnChipAction(account_id="VBA-000001"), "vendor_agent")
    with pytest.raises(PolicyViolation, match="edit_allowlist"):
        env.policy.execute(SetOnChipAction(account_id="VBA-000001"), "controller")
    with pytest.raises(PolicyViolation, match="jumper"):
        env.policy.execute(SetOnChipAction(account_id="VBA-000001"), "provisioner")
    env.jumper["on"] = True
    with pytest.raises(PolicyViolation, match="not_active"):
        env.policy.execute(SetOnChipAction(account_id=pending.id), "provisioner")
    done = env.policy.execute(SetOnChipAction(account_id="VBA-000001"), "provisioner")
    assert done.on_chip is True
    assert len(violations(env)) == 4


def test_close_period_requires_approve_scope(env: Env) -> None:
    with pytest.raises(PolicyViolation, match="approve"):
        env.policy.execute(ClosePeriodAction(month="2026-09"), "invoice_agent")
    assert violations(env)[-1]["agent"] == "invoice_agent"
    env.policy.execute(ClosePeriodAction(month="2026-09"), "controller")
    assert env.ledger.period_status("2026-09") == "closed"
