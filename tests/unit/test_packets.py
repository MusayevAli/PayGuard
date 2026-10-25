from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from pathlib import Path
from threading import Barrier

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


def test_conflicting_concurrent_decisions_publish_only_the_persisted_winner(
    store: EventStore, packets: PacketStore, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Two independent connections both read an undecided packet before racing."""
    pk = packets.create_packet(sample(store))
    other_events = EventStore(tmp_path / "payguard.db")
    other = PacketStore(tmp_path / "payguard.db", events=other_events, now=lambda: NOW)
    both_read = Barrier(2)

    def race_read(owner: PacketStore) -> None:
        original = owner.get
        first = True

        def get(packet_id: str):
            nonlocal first
            result = original(packet_id)
            if first:
                first = False
                both_read.wait(timeout=5)
            return result

        monkeypatch.setattr(owner, "get", get)

    race_read(packets)
    race_read(other)
    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            approve = pool.submit(packets.decide_packet, pk.id, "approve", "A", "alice")
            deny = pool.submit(other.decide_packet, pk.id, "deny", "B", "bob")
            first, second = approve.result(timeout=10), deny.result(timeout=10)
        assert first == second == packets.get(pk.id).decision
        events = store.query(type="controller_decision")
        assert len(events) == 1
        assert events[0].id == first.event_id
        assert events[0].payload["decision"] == first.decision
    finally:
        other.close()
        other_events.close()


def test_restart_publishes_a_committed_decision_after_event_write_failed(
    store: EventStore, packets: PacketStore, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pk = packets.create_packet(sample(store))
    original = store.append

    def interrupted(event):
        if event.type == "controller_decision":
            raise OSError("simulated interruption before event persistence")
        return original(event)

    monkeypatch.setattr(store, "append", interrupted)
    with pytest.raises(OSError, match="interruption"):
        packets.decide_packet(pk.id, "deny", "Keep the account held", "alice")
    committed = packets.get(pk.id).decision
    assert committed is not None and committed.decision == "deny"
    assert store.query(type="controller_decision") == []
    monkeypatch.setattr(store, "append", original)

    restarted = PacketStore(tmp_path / "payguard.db", events=store, now=lambda: NOW)
    try:
        assert restarted.decide_packet(pk.id, "approve", None, "bob") == committed
        decisions = store.query(type="controller_decision")
        assert len(decisions) == 1 and decisions[0].id == committed.event_id
        assert decisions[0].payload["decision"] == "deny"
    finally:
        restarted.close()


def test_decision_subscriber_observes_the_committed_packet(
    store: EventStore, packets: PacketStore
) -> None:
    pk = packets.create_packet(sample(store))
    observed = []
    unsubscribe = store.subscribe(
        lambda event: observed.append(packets.get(event.payload["packet_id"]).decision),
        types={"controller_decision"},
    )
    try:
        result = packets.decide_packet(pk.id, "deny", None, "alice")
        assert observed == [result]
    finally:
        unsubscribe()


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
