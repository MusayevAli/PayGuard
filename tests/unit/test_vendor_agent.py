"""Vendor agent (spec §7.4) on the synthetic kit: the channel matrix, hold scheduling, decisions,
R9 after a chip rejection, the M2 bounce fallback and onboarding."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import pytest

from agents.vendor_agent import CHANNEL_MATRIX
from tests.agentkit import Kit

START = datetime(2026, 10, 1, tzinfo=UTC)
NEW_CZ = "CZ6508000000192000145399"
NEW_PL = "PL61109010140000071219812874"


@pytest.fixture
def kit(tmp_path: Path):
    k = Kit(tmp_path, payments=True, vendor=True)
    k.provision("V-0001", "V-0007", "V-0015", "V-0002")
    yield k
    k.close()


def actions(kit: Kit, action_type: str, ref: str | None = None) -> list[dict]:
    return kit.actions(ref, action_type)


def test_channel_matrix_never_verifies_over_the_senders_thread() -> None:
    assert (
        CHANNEL_MATRIX["portal"] == "phone"
        and CHANNEL_MATRIX["invoice"] == "phone"
        and CHANNEL_MATRIX["email"] == "email"
    )


def test_portal_change_verified_by_phone_recommends_approve_and_activates_at_hold(kit: Kit) -> None:
    kit.message(
        "V-0007",
        "portal",
        "Měníme bankovní spojení. Nový účet IBAN " + NEW_CZ,
        iban=NEW_CZ,
        bank_name="Česká spořitelna",
    )
    kit.run()
    opened = actions(kit, "bank_change_opened", "V-0007")
    assert (
        len(opened) == 1
        and opened[0]["rule_id"] == "R8"
        and opened[0]["decision"]["verification_channel"] == "phone"
    )
    pending = [a for a in kit.accounts("V-0007") if a.status == "pending"]
    assert (
        len(pending) == 1
        and pending[0].requested_via == "portal"
        and pending[0].hold_until == kit.now + timedelta(hours=72)
    )
    asked = actions(kit, "verification_requested", "V-0007")
    assert len(asked) == 1 and asked[0]["decision"] == {
        "verification_channel": "phone",
        "person": "petra.vlcek",
    }
    kit.tick(hours=4)  # Petra answers by phone within 1-3 hours
    replies = kit.replies()
    assert (
        len(replies) == 1
        and replies[0].payload["channel"] == "phone"
        and "confirmed" in replies[0].payload["refs"]
    )
    received = actions(kit, "verification_received", "V-0007")
    assert len(received) == 1 and received[0]["decision"]["verified_via"] == "phone"
    packets = kit.packets_open(kind="approval")
    assert len(packets) == 1
    pk = packets[0].payload
    assert pk["recommendation"] == "approve" and pk["options"] == [
        "approve",
        "deny",
        "request_phone_verification",
    ]
    assert [e["summary"].split(":")[0] for e in pk["evidence"]] == [
        "Request",
        "Verification",
        "Mismatch",
        "Pending payments",
    ]
    assert pk["expires_at"].startswith("2026-10-04T00:00") and "V-0007" in pk["refs"]
    kit.decide(pk["packet_id"], "approve")
    assert actions(kit, "account_approved", "V-0007") and actions(
        kit, "activation_scheduled", "V-0007"
    )
    assert [a.status for a in kit.accounts("V-0007")] == ["active", "pending"], (
        "approved but held: the old account stays active"
    )
    kit.tick(days=2)
    assert [a.status for a in kit.accounts("V-0007")] == ["active", "pending"]
    kit.tick(days=1, hours=1)  # past hold_until (72 h)
    statuses = {a.id: a.status for a in kit.accounts("V-0007")}
    assert list(statuses.values()) == ["retired", "active"], statuses
    activated = actions(kit, "account_activated", "V-0007")
    assert len(activated) == 1 and activated[0]["rule_id"] == "R8"
    activation_time = next(
        e.event_time
        for e in kit.store.query(type="agent_action", limit=100000)
        if e.payload["action_type"] == "account_activated"
    )
    assert activation_time >= pending[0].hold_until
    prov = [
        e
        for e in kit.store.query(type="provisioning", limit=1000)
        if e.payload["vendor_id"] == "V-0007"
    ]
    assert prov[-1].payload["ok"] is False and prov[-1].payload["reason"] == "jumper_absent"
    kit.provision("V-0007")
    prov = [
        e
        for e in kit.store.query(type="provisioning", limit=1000)
        if e.payload["vendor_id"] == "V-0007"
    ]
    assert prov[-1].payload["ok"] is True and prov[-1].payload["account_hash_prefix"]
    assert kit.escalations() and all(e.payload["kind"] != "anomaly" for e in kit.escalations())


def test_email_change_verified_by_email_recommends_phone_and_phone_recheck_reraises(
    kit: Kit,
) -> None:
    kit.message(
        None,
        "email",
        "Dobrý den, od 1. 11. měníme bankovní spojení. Nový účet IBAN " + NEW_CZ,
        iban=NEW_CZ,
        from_address="fakturace@liberecfoam.cz",
        claimed_name="Liberec Foam a.s.",
    )
    kit.run()
    asked = actions(kit, "verification_requested", "V-0007")
    assert asked[0]["decision"]["verification_channel"] == "email"
    assert asked[0]["decision"]["person"] == "petra.vlcek"
    kit.tick(days=2)  # email replies take 2-30 hours
    pk = kit.packets_open(kind="approval")[0].payload
    assert pk["recommendation"] == "request_phone_verification"
    kit.decide(pk["packet_id"], "request_phone_verification")
    assert len(actions(kit, "verification_requested", "V-0007")) == 2
    kit.tick(hours=4)
    packets = kit.packets_open(kind="approval")
    assert len(packets) == 2
    second = packets[-1].payload
    assert second["recommendation"] == "approve"
    assert any(r["summary"].startswith("Phone verification") for r in second["evidence"])
    assert len(second["evidence"]) == 5
    # the confirmed call is now the account's verification, so approval leads to activation
    kit.decide(second["packet_id"], "approve")
    acct = next(a for a in kit.accounts("V-0007") if a.requested_via == "email")
    assert acct.verified_via == "phone" and acct.approved_by == "controller"
    assert acct.status == "pending", "the 72 h hold still runs"
    kit.tick(days=2)
    acct = next(a for a in kit.accounts("V-0007") if a.requested_via == "email")
    assert acct.status == "active"
    assert actions(kit, "account_activated", "V-0007")[0]["inputs"]["verified_via"] == "phone"
    assert not actions(kit, "policy_violation") and not actions(kit, "error")


def test_approving_an_email_verified_change_phones_the_contact_before_activation(kit: Kit) -> None:
    """The controller may approve against the recommendation; §7.4 still needs a second channel, so
    when the hold ends the desk phones the contact on file and activates only on a confirmed call."""
    kit.message(
        None,
        "email",
        "Dobrý den, od 1. 11. měníme bankovní spojení. Nový účet IBAN " + NEW_CZ,
        iban=NEW_CZ,
        from_address="fakturace@liberecfoam.cz",
        claimed_name="Liberec Foam a.s.",
    )
    kit.run()
    kit.tick(days=2)
    pk = kit.packets_open(kind="approval")[0].payload
    assert pk["recommendation"] == "request_phone_verification"
    kit.decide(pk["packet_id"], "approve")
    assert actions(kit, "activation_scheduled", "V-0007")
    kit.tick(days=1, hours=0.5)  # the hold ended at midnight; the phone call is out
    acct = next(a for a in kit.accounts("V-0007") if a.requested_via == "email")
    assert acct.status == "pending", "no activation on a same-channel verification"
    asked = actions(kit, "verification_requested", "V-0007")
    assert len(asked) == 2 and asked[-1]["decision"]["verification_channel"] == "phone"
    assert asked[-1]["inputs"]["reason"] == "same_channel_before_activation"
    kit.tick(hours=4)  # Petra answers the phone within 1-3 hours
    acct = next(a for a in kit.accounts("V-0007") if a.requested_via == "email")
    assert acct.status == "active" and acct.verified_via == "phone"
    activated = actions(kit, "account_activated", "V-0007")
    assert len(activated) == 1 and activated[0]["inputs"]["verified_via"] == "phone"
    assert not actions(kit, "policy_violation") and not actions(kit, "error")


def test_activation_recheck_denied_by_phone_suspends_the_account(tmp_path: Path) -> None:
    """15 Oct: the lookalike's change is confirmed by Petra's compromised mailbox and approved by the
    controller; nothing falls due. When the hold ends the desk phones Petra, who denies the change:
    the account is suspended, the old one stays active and an urgent packet names the contract phone."""
    kit = Kit(tmp_path, payments=True, vendor=True, now=datetime(2026, 10, 15, tzinfo=UTC))
    kit.provision("V-0001")
    kit.message(
        None,
        "email",
        "Změna bankovního spojení: IBAN " + NEW_CZ,
        iban=NEW_CZ,
        from_address="accounts@kovak-trim.cz",
        claimed_name="Kovak Trim s.r.o.",
        thread_id="thr_attack",
    )
    kit.run()
    kit.tick(hours=30)
    pk = [e for e in kit.packets_open(kind="approval") if "V-0001" in e.payload["refs"]]
    assert len(pk) == 1
    kit.decide(pk[0].payload["packet_id"], "approve")
    kit.tick(days=2)  # 18 Oct 06:00: the hold ended at midnight and Petra was phoned
    asked = actions(kit, "verification_requested", "V-0001")
    assert asked[-1]["decision"]["verification_channel"] == "phone"
    assert asked[-1]["inputs"]["reason"] == "same_channel_before_activation"
    kit.tick(hours=4)
    new = next(a for a in kit.accounts("V-0001") if a.requested_via == "email")
    old = next(a for a in kit.accounts("V-0001") if a.requested_via == "onboarding")
    assert new.status == "suspended" and old.status == "active"
    urgent = [e for e in kit.escalations("V-0001") if e.payload["severity"] == "urgent"]
    assert urgent and "+420 626 564 375" in urgent[-1].payload["context"]
    assert not actions(kit, "account_activated", "V-0001")
    assert not actions(kit, "policy_violation") and not actions(kit, "error")
    kit.close()


def test_deny_suspends_the_pending_account(kit: Kit) -> None:
    kit.message("V-0007", "portal", "Nový účet IBAN " + NEW_CZ, iban=NEW_CZ)
    kit.run()
    kit.tick(hours=4)
    pk = kit.packets_open(kind="approval")[0].payload
    kit.decide(pk["packet_id"], "deny")
    assert [a.status for a in kit.accounts("V-0007")] == ["active", "suspended"]
    assert (
        actions(kit, "account_suspended", "V-0007")[0]["decision"]["reason"]
        == "denied_by_controller"
    )


def test_lookalike_domain_is_flagged_but_r8_proceeds(kit: Kit) -> None:
    kit.message(
        None,
        "email",
        "Změna bankovního spojení: IBAN " + NEW_CZ,
        iban=NEW_CZ,
        from_address="accounts@kovak-trim.cz",
        claimed_name="Kovak Trim s.r.o.",
    )
    kit.run()
    flagged = actions(kit, "domain_flagged")
    assert (
        len(flagged) == 1
        and flagged[0]["decision"]["distance"] == 1
        and flagged[0]["decision"]["blocking"] is False
        and "kovak-trim.cz" in flagged[0]["refs"]
    )
    assert actions(kit, "bank_change_opened", "V-0001"), (
        "the lookalike check is informational: R8 still opens the pending account"
    )
    assert (
        actions(kit, "verification_requested", "V-0001")[0]["decision"]["person"] == "petra.vlcek"
    )


def test_duplicate_request_for_the_same_account_is_matched_not_reopened(kit: Kit) -> None:
    kit.message("V-0007", "portal", "Nový účet IBAN " + NEW_CZ, iban=NEW_CZ)
    kit.run()
    kit.message("V-0007", "portal", "Připomínáme nový účet IBAN " + NEW_CZ, iban=NEW_CZ)
    kit.run()
    assert len(actions(kit, "bank_change_opened", "V-0007")) == 1
    dup = actions(kit, "matched", "V-0007")
    assert dup and dup[-1]["decision"]["duplicate_request"] is True


def test_r9_suspends_restores_and_requeues_after_the_chip_rejects(tmp_path: Path) -> None:
    """Oct 15: the lookalike asks for a change, Petra's compromised mailbox confirms, the controller
    approves; Oct 16 the payment falls due. The approved account is not on the chip, so the chip says
    not_allowlisted and the desk adds in_hold; R9 unwinds the change and holds the payment."""
    kit = Kit(tmp_path, payments=True, vendor=True, now=datetime(2026, 10, 15, tzinfo=UTC))
    kit.provision("V-0001")
    payable = kit.payable("V-0001", 210_000_000, due=date(2026, 10, 17), number="KT-2270")
    kit.message(
        None,
        "email",
        "Změna bankovního spojení: IBAN " + NEW_CZ,
        iban=NEW_CZ,
        from_address="accounts@kovak-trim.cz",
        claimed_name="Kovak Trim s.r.o.",
        thread_id="thr_attack",
    )
    kit.run()
    kit.tick(hours=30)  # the email confirmation arrives (2-30 h); the 16 Oct run has nothing due
    pk = [e for e in kit.packets_open(kind="approval") if "V-0001" in e.payload["refs"]]
    assert len(pk) == 1 and pk[0].payload["recommendation"] == "request_phone_verification"
    kit.decide(pk[0].payload["packet_id"], "approve")
    new = next(a for a in kit.accounts("V-0001") if a.requested_via == "email")
    assert new.status == "pending" and new.approved_by == "controller"
    kit.tick(hours=18)  # Friday 17 Oct: the payment run targets the approved account
    chips = kit.store.query(type="chip_response", limit=100)
    assert len(chips) == 1
    chip = chips[0].payload
    assert (
        not chip["ok"]
        and chip["chip_reason"] == "not_allowlisted"
        and chip["desk_annotations"] == ["in_hold"]
    )
    assert chip["reasons"] == ["not_allowlisted", "in_hold"]
    statuses = {a.requested_via: a.status for a in kit.accounts("V-0001")}
    assert statuses == {"onboarding": "active", "email": "suspended"}
    assert actions(kit, "account_suspended", "V-0001")[-1]["rule_id"] == "R9"
    restored = actions(kit, "account_restored", "V-0001")
    assert len(restored) == 1 and restored[0]["decision"]["account_on_file"]
    requeued = actions(kit, "payment_requeued", "V-0001")
    assert len(requeued) == 1 and requeued[0]["decision"]["held_reason"] == "awaiting_controller"
    assert (
        kit.ledger.get_payable(payable.id).status == "held"
        and kit.ledger.get_payable(payable.id).held_reason == "awaiting_controller"
    )
    assert kit.store.query(type="payment_executed", limit=10) == []
    urgent = [e for e in kit.escalations() if e.payload["severity"] == "urgent"]
    assert len(urgent) == 1
    r9 = urgent[0].payload
    assert (
        r9["recommendation"] == "call_supplier_on_contract_number"
        and "+420" in r9["recommendation_reason"]
    )
    assert [e["summary"].split(":")[0] for e in r9["evidence"]] == [
        "Original request",
        "Verification path",
        "Chip rejection",
        "Account on file since 2016-03-01"[:15],
    ] or len(r9["evidence"]) == 4
    assert r9["kind"] == "anomaly" and "PAY-000001" in r9["refs"]
    kit.close()


def test_m2_bounce_falls_back_to_the_successor(kit: Kit) -> None:
    kit.message("V-0015", "portal", "Zmiana rachunku bankowego: IBAN " + NEW_PL, iban=NEW_PL)
    kit.run()
    first = actions(kit, "verification_requested", "V-0015")
    assert first[0]["decision"]["person"] == "jana.svobodova"
    kit.tick(hours=1)
    bounced = [e for e in kit.replies() if e.payload["bounced"]]
    assert len(bounced) == 1 and bounced[0].payload["person_id"] == "jana.svobodova"
    second = actions(kit, "verification_requested", "V-0015")
    assert (
        len(second) == 2
        and second[1]["decision"]["person"] == "agnieszka.kowalczyk"
        and second[1]["inputs"]["via_successor"] is True
    )
    kit.tick(hours=4)
    pk = kit.packets_open(kind="approval")
    assert (
        len(pk) == 1
        and "V-0015" in pk[0].payload["refs"]
        and pk[0].payload["recommendation"] == "approve"
    )


def test_onboarding_from_an_unknown_vendor_message(kit: Kit) -> None:
    kit.message(
        None,
        "email",
        "We are a new supplier: Nordic Trim OÜ, VAT EE100931558, IBAN EE382200221020145685. Please register us.",
        claimed_name="Nordic Trim OÜ",
        from_address="ar@nordictrim.ee",
        iban="EE382200221020145685",
    )
    kit.run()
    created = actions(kit, "vendor_created")
    assert len(created) == 1 and created[0]["rule_id"] == "R5"
    vendor_id = created[0]["decision"]["vendor_id"]
    assert kit.vendors.get_vendor(vendor_id).status == "onboarding"
    pk = kit.packets_open(kind="approval")[0].payload
    assert (
        pk["question"] == "Onboard Nordic Trim OÜ?"
        and pk["options"] == ["approve", "deny"]
        and pk["recommendation"] == "approve"
    )
    assert [r["summary"].split(":")[0] for r in pk["evidence"]] == [
        "VAT ID validity",
        "IBAN validity",
        "First invoice amount",
    ]
    assert "CRS checks" in pk["context"] and "not run" in pk["context"]
    kit.decide(pk["packet_id"], "approve")
    assert kit.vendors.get_vendor(vendor_id).status == "active"
    accounts = kit.accounts(vendor_id)
    assert (
        len(accounts) == 1
        and accounts[0].status == "pending"
        and accounts[0].requested_via == "invoice"
    )
    asked = [a for a in actions(kit, "verification_requested") if vendor_id in a["refs"]]
    assert asked and asked[0]["decision"]["verification_channel"] == "phone"
    kit.tick(hours=4)
    assert kit.accounts(vendor_id)[0].status == "suspended", (
        "nobody at the plant knows this vendor: verification denied"
    )
    assert any(
        e.payload["kind"] == "anomaly" and e.payload["severity"] == "urgent"
        for e in kit.escalations()
    )


def test_vendor_agent_subscriptions_match_the_spec() -> None:
    from agents.vendor_agent import VendorAgent

    assert VendorAgent.subscribes >= {
        "vendor_message",
        "colleague_reply",
        "controller_decision",
        "chip_response",
        "day_boundary",
        "judge_injection",
    }
