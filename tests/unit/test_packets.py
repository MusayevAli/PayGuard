from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from core.events import EventStore, make_event
from core.packets import PacketDraft, PacketError, PacketEvidence, PacketOption, PacketStore

NOW = datetime(2026, 10, 16, 8, 0, tzinfo=UTC)


@pytest.fixture
def store(tmp_path: Path) -> EventStore:
    s = EventStore(tmp_path / "payguard.db")
    yield s
    s.close()


@pytest.fixture
def packets(store: EventStore, tmp_path: Path) -> PacketStore:
    p = PacketStore(tmp_path / "payguard.db", events=store, now=lambda: NOW)
    yield p
    p.close()


def sample(store: EventStore) -> PacketDraft:
    ev = store.append(
        make_event(
            "system_error",
            {"component": "t", "code": "c", "message": "m", "refs": [], "recoverable": True},
            event_time=NOW,
            source="test",
        )
    ).event
    return PacketDraft(
        agent="vendor_agent",
        question="Approve bank change for Kovak Trim?",
        context="New IBAN received by e-mail; Petra confirmed by e-mail.",
        severity="urgent",
        refs=["V-0001", "KT-2270"],
        evidence=[PacketEvidence(event_id=ev.id, role="message", summary="lookalike e-mail")],
        materiality_minor=210_000_000,
        currency="EUR",
        recommendation="request_phone_verification",
        recommendation_reason="verification came over the same medium as the request",
        options=[
            PacketOption(id="approve", label="Approve change"),
            PacketOption(id="deny", label="Deny change"),
            PacketOption(id="request_phone_verification", label="Request phone verification"),
        ],
        expires_at=NOW + timedelta(hours=24),
        consequence_of_silence="Payment stays held on the prior account.",
    )


def test_create_packet_emits_escalation_and_is_idempotent(
    store: EventStore, packets: PacketStore
) -> None:
    draft = sample(store)
    pk = packets.create_packet(draft)
    assert pk.id.startswith("PK-") and pk.status == "open" and pk.created_at == NOW
    events = store.query(type="escalation")
    assert (
        len(events) == 1
        and events[0].payload["packet_id"] == pk.id
        and events[0].payload["severity"] == "urgent"
    )
    assert events[0].payload["options"] == ["approve", "deny", "request_phone_verification"]
    assert events[0].payload["auto_action_on_expiry"] == "hold"
    again = packets.create_packet(draft)
    assert again.id == pk.id and len(store.query(type="escalation")) == 1
    assert [p.id for p in packets.open_packets()] == [pk.id]
    assert packets.get(pk.id) == pk


def test_decide_is_idempotent_and_emits_one_controller_decision(
    store: EventStore, packets: PacketStore
) -> None:
    pk = packets.create_packet(sample(store))
    first = packets.decide_packet(
        pk.id, "deny", "Caller could not confirm", "marta.nowak", via="phone"
    )
    assert (
        first.decision == "deny" and first.decided_by == "marta.nowak" and first.packet_id == pk.id
    )
    decisions = store.query(type="controller_decision")
    assert len(decisions) == 1 and decisions[0].id == first.event_id
    assert decisions[0].payload == {
        "packet_id": pk.id,
        "decision": "deny",
        "note": "Caller could not confirm",
        "decided_by": "marta.nowak",
        "via": "phone",
    }
    second = packets.decide_packet(pk.id, "approve", "changed my mind", "someone", via="dashboard")
    assert second == first
    assert len(store.query(type="controller_decision")) == 1
    assert packets.get(pk.id).status == "decided" and packets.get(pk.id).decision == first
    assert packets.open_packets() == []


def test_decide_rejects_unknown_option_and_unknown_packet(
    store: EventStore, packets: PacketStore
) -> None:
    pk = packets.create_packet(sample(store))
    with pytest.raises(PacketError, match="option"):
        packets.decide_packet(pk.id, "ignore", None, "x")
    with pytest.raises(PacketError, match="PK-nope"):
        packets.decide_packet("PK-nope", "approve", None, "x")
    assert store.query(type="controller_decision") == []


def test_recommendation_must_be_one_of_the_options(store: EventStore) -> None:
    draft = sample(store)
    with pytest.raises(ValueError):
        draft.model_copy(update={"recommendation": "escalate"}).model_validate(
            draft.model_copy(update={"recommendation": "escalate"}).model_dump()
        )


def test_expiry_applies_the_safe_hold(store: EventStore, packets: PacketStore) -> None:
    pk = packets.create_packet(sample(store))
    assert packets.expire_open_packets(NOW + timedelta(hours=1)) == []
    expired = packets.expire_open_packets(NOW + timedelta(hours=25))
    assert [d.packet_id for d in expired] == [pk.id]
    assert expired[0].decision == "hold" and expired[0].decided_by == "auto_expiry"
    assert packets.get(pk.id).status == "expired"
    assert store.query(type="controller_decision")[0].payload["decision"] == "hold"
    assert (
        packets.decide_packet(pk.id, "approve", None, "late") == expired[0]
    )  # idempotent after expiry too
