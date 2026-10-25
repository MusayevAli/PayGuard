"""Packet rendering for the controller phone (spec §16.4)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

from core.events import EventStore
from core.packets import (
    PacketDraft,
    PacketEvidence,
    PacketOption,
    PacketStore,
    format_minor,
    render,
)

NOW = datetime(2026, 10, 15, 10, 40, tzinfo=UTC)


def test_render_shows_everything_the_phone_needs(tmp_path: Path) -> None:
    store = EventStore(tmp_path / "pg.db")
    packets = PacketStore(tmp_path / "pg.db", events=store, now=lambda: NOW)
    draft = PacketDraft(
        agent="vendor_agent",
        question="Approve bank change for Kovak Trim s.r.o.?",
        context="Requested via email, verified via email.",
        severity="action",
        refs=["V-0001", "VBA-000041"],
        evidence=[
            PacketEvidence(
                event_id="ev_1",
                role="message",
                summary="Request: email from accounts@kovak-trim.cz",
            ),
            PacketEvidence(
                event_id="ev_2",
                role="message",
                summary="Verification: petra.vlcek by email 'confirmed'",
            ),
            PacketEvidence(
                event_id="ev_1",
                role="decision",
                summary="Mismatch: on file CZ67 **** 0449 vs requested CZ62 **** 1954",
            ),
            PacketEvidence(
                event_id="ev_3",
                role="decision",
                summary="Pending payments: 1 payable, 2,100,000.00 EUR",
            ),
        ],
        materiality_minor=210_000_000,
        currency="EUR",
        recommendation="request_phone_verification",
        recommendation_reason="an email confirmation can come from a compromised mailbox",
        options=[
            PacketOption(id="approve", label="Approve change"),
            PacketOption(id="deny", label="Deny change"),
            PacketOption(id="request_phone_verification", label="Request phone verification"),
        ],
        expires_at=NOW + timedelta(hours=70),
        consequence_of_silence="Change stays pending. Payments continue to the account on file.",
        kind="approval",
    )
    packet = packets.create_packet(draft, created_at=NOW)
    view = render(packet)
    assert (
        view["question"].startswith("Approve bank change")
        and view["kind"] == "approval"
        and view["status"] == "open"
    )
    assert [r["summary"][:12] for r in view["evidence_rows"]] == [
        "Request: ema",
        "Verification",
        "Mismatch: on",
        "Pending paym",
    ]
    assert view["materiality"] == {
        "minor": 210_000_000,
        "currency": "EUR",
        "formatted": "2,100,000.00 EUR",
    }
    assert view["recommendation"] == {
        "id": "request_phone_verification",
        "label": "Request phone verification",
        "reason": "an email confirmation can come from a compromised mailbox",
    }
    assert [o["label"] for o in view["options"]] == [
        "Approve change",
        "Deny change",
        "Request phone verification",
    ]
    assert [o["recommended"] for o in view["options"]] == [False, False, True]
    assert view["if_no_answer"] == "Change stays pending. Payments continue to the account on file."
    assert view["expires_at"] == "2026-10-18T08:40:00Z" and view["decision"] is None
    packets.decide_packet(
        packet.id, "approve", "ok", "controller", via="phone", decided_at=NOW + timedelta(hours=1)
    )
    decided = render(packets.get(packet.id))  # type: ignore[arg-type]
    assert (
        decided["status"] == "decided"
        and decided["decision"]["label"] == "Approve change"
        and decided["decision"]["decided_by"] == "controller"
    )
    assert store.query(type="controller_decision")[0].payload["decision"] == "approve"
    packets.close()
    store.close()


def test_format_minor() -> None:
    assert format_minor(4_284_00, "EUR") == "4,284.00 EUR"
    assert format_minor(-150, "CZK") == "-1.50 CZK"
    assert format_minor(2_290_500, "JPY") == "2,290,500 JPY"
    assert format_minor(None, "EUR") == ""
