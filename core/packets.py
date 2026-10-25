"""Escalation packets (spec §9): schema and persistence only. ``create_packet`` emits an
``escalation`` event, ``decide_packet`` emits ``controller_decision`` and is idempotent."""

from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, model_validator

from core.events import EventStore, canonical_json, make_event

Severity = Literal["info", "action", "urgent"]
PacketKind = Literal["anomaly", "approval", "notice"]
"""anomaly: the desk suspects something is wrong; approval: a routine control step (button, bank
change); notice: information for another desk. Only anomalies count as false positives."""
PacketStatus = Literal["open", "decided", "expired"]

SCHEMA = """
CREATE TABLE IF NOT EXISTS packets (
  id TEXT PRIMARY KEY, status TEXT NOT NULL, created_at TEXT NOT NULL, expires_at TEXT NULL,
  escalation_event_id TEXT NOT NULL, decision_event_id TEXT NULL, data TEXT NOT NULL
);
"""


class PacketError(Exception):
    pass


class PacketOption(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    id: str
    label: str


class PacketEvidence(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    event_id: str
    role: str
    summary: str


class PacketDraft(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    agent: str
    question: str
    context: str
    severity: Severity
    refs: list[str]
    evidence: list[PacketEvidence]
    materiality_minor: int | None
    currency: str | None
    recommendation: str
    recommendation_reason: str
    options: list[PacketOption] = Field(min_length=1)
    expires_at: AwareDatetime | None
    auto_action_on_expiry: Literal["hold"] = "hold"
    consequence_of_silence: str
    kind: PacketKind = "anomaly"

    @model_validator(mode="after")
    def _recommendation_is_an_option(self) -> PacketDraft:
        ids = [o.id for o in self.options]
        if len(set(ids)) != len(ids):
            raise ValueError("option ids must be unique")
        if self.recommendation not in ids:
            raise ValueError(
                f"recommendation {self.recommendation!r} must be one of the options {ids}"
            )
        return self


class PacketDecision(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    packet_id: str
    decision: str
    note: str | None
    decided_by: str
    decided_at: AwareDatetime
    event_id: str
    via: str | None


class Packet(PacketDraft):
    id: str
    status: PacketStatus
    created_at: AwareDatetime
    escalation_event_id: str
    decision: PacketDecision | None


def _iso(dt: datetime) -> str:
    return dt.astimezone(UTC).isoformat().replace("+00:00", "Z")


class PacketStore:
    def __init__(
        self,
        path: str | Path,
        *,
        events: EventStore,
        now: Callable[[], datetime] | None = None,
        timeout: float = 30.0,
    ) -> None:
        self._events = events
        self._now: Callable[[], datetime] = now or (lambda: datetime.now(UTC))
        self._lock = threading.RLock()
        self._con = sqlite3.connect(
            str(path), timeout=timeout, check_same_thread=False, isolation_level=None
        )
        self._con.row_factory = sqlite3.Row
        self._con.execute("PRAGMA journal_mode=WAL")
        self._con.execute("PRAGMA synchronous=NORMAL")
        self._con.executescript(SCHEMA)

    def close(self) -> None:
        with self._lock:
            self._con.close()

    # ---------------------------------------------------------------- create

    @staticmethod
    def packet_id(draft: PacketDraft) -> str:
        return (
            "PK-"
            + hashlib.sha256(
                canonical_json(draft.model_dump(mode="json")).encode("utf-8")
            ).hexdigest()[:12]
        )

    def create_packet(self, draft: PacketDraft, created_at: datetime | None = None) -> Packet:
        """Store the packet and emit its ``escalation`` event. Replays return the existing packet."""
        packet_id = self.packet_id(draft)
        existing = self.get(packet_id)
        if existing is not None:
            return existing
        when = created_at or self._now()
        payload = {
            "packet_id": packet_id,
            "agent": draft.agent,
            "question": draft.question,
            "severity": draft.severity,
            "refs": draft.refs,
            "context": draft.context,
            "evidence": [e.model_dump() for e in draft.evidence],
            "materiality_minor": draft.materiality_minor,
            "currency": draft.currency,
            "recommendation": draft.recommendation,
            "recommendation_reason": draft.recommendation_reason,
            "options": [o.id for o in draft.options],
            "expires_at": _iso(draft.expires_at) if draft.expires_at else None,
            "auto_action_on_expiry": draft.auto_action_on_expiry,
            "kind": draft.kind,
        }
        event = self._events.append(
            make_event("escalation", payload, event_time=when, source="agent", actor=draft.agent)
        ).event
        packet = Packet(
            **draft.model_dump(),
            id=packet_id,
            status="open",
            created_at=when,
            escalation_event_id=event.id,
            decision=None,
        )
        with self._lock:
            self._con.execute(
                "INSERT OR IGNORE INTO packets(id, status, created_at, expires_at, escalation_event_id, decision_event_id, data) VALUES (?,?,?,?,?,?,?)",
                (
                    packet.id,
                    packet.status,
                    _iso(when),
                    _iso(draft.expires_at) if draft.expires_at else None,
                    event.id,
                    None,
                    packet.model_dump_json(),
                ),
            )
        return packet

    # ---------------------------------------------------------------- decide

    def decide_packet(
        self,
        packet_id: str,
        decision: str,
        note: str | None,
        decided_by: str,
        via: str | None = "phone",
        decided_at: datetime | None = None,
    ) -> PacketDecision:
        """Record the controller's decision once; later calls return the first decision unchanged."""
        packet = self.get(packet_id)
        if packet is None:
            raise PacketError(f"no packet {packet_id}")
        if packet.decision is not None:
            return packet.decision
        option_ids = [o.id for o in packet.options]
        if decision not in option_ids and decision != packet.auto_action_on_expiry:
            raise PacketError(
                f"decision {decision!r} is not an option of {packet_id} ({option_ids})"
            )
        when = decided_at or self._now()
        event = self._events.append(
            make_event(
                "controller_decision",
                {
                    "packet_id": packet_id,
                    "decision": decision,
                    "note": note,
                    "decided_by": decided_by,
                    "via": via,
                },
                event_time=when,
                source="controller",
                actor=decided_by,
            )
        ).event
        result = PacketDecision(
            packet_id=packet_id,
            decision=decision,
            note=note,
            decided_by=decided_by,
            decided_at=when,
            event_id=event.id,
            via=via,
        )
        status: PacketStatus = "expired" if decided_by == "auto_expiry" else "decided"
        updated = packet.model_copy(update={"status": status, "decision": result})
        with self._lock:
            self._con.execute(
                "UPDATE packets SET status = ?, decision_event_id = ?, data = ? WHERE id = ? AND decision_event_id IS NULL",
                (status, event.id, updated.model_dump_json(), packet_id),
            )
        return result

    def expire_open_packets(self, now: datetime) -> list[PacketDecision]:
        """Apply the safe no-op (hold) to every open packet whose ``expires_at`` has passed."""
        out: list[PacketDecision] = []
        for packet in self.open_packets():
            if packet.expires_at is not None and packet.expires_at <= now:
                out.append(
                    self.decide_packet(
                        packet.id,
                        packet.auto_action_on_expiry,
                        "expired without a decision; holding",
                        "auto_expiry",
                        via=None,
                        decided_at=now,
                    )
                )
        return out

    # ---------------------------------------------------------------- reads

    def get(self, packet_id: str) -> Packet | None:
        with self._lock:
            row = self._con.execute(
                "SELECT data FROM packets WHERE id = ?", (packet_id,)
            ).fetchone()
        return None if row is None else Packet.model_validate(json.loads(row["data"]))

    def packets(self, status: PacketStatus | None = None) -> list[Packet]:
        sql = (
            "SELECT data FROM packets"
            + (" WHERE status = ?" if status else "")
            + " ORDER BY created_at, id"
        )
        with self._lock:
            rows = self._con.execute(sql, (status,) if status else ()).fetchall()
        return [Packet.model_validate(json.loads(r["data"])) for r in rows]

    def open_packets(self) -> list[Packet]:
        return self.packets(status="open")


def format_minor(minor: int | None, currency: str | None) -> str:
    if minor is None or currency is None:
        return ""
    if currency == "JPY":
        return f"{minor:,} JPY"
    sign = "-" if minor < 0 else ""
    return f"{sign}{abs(minor) // 100:,}.{abs(minor) % 100:02d} {currency}"


def render(packet: Packet) -> dict[str, Any]:
    """The packet as the controller phone shows it (spec §16.4): question, context, evidence rows,
    materiality, recommendation with its reason, options with outcome labels, what happens if
    nobody answers, and the decision once taken."""
    options = [
        {"id": o.id, "label": o.label, "recommended": o.id == packet.recommendation}
        for o in packet.options
    ]
    recommended = next((o for o in packet.options if o.id == packet.recommendation), None)
    return {
        "id": packet.id,
        "status": packet.status,
        "kind": packet.kind,
        "severity": packet.severity,
        "agent": packet.agent,
        "created_at": _iso(packet.created_at),
        "question": packet.question,
        "context": packet.context,
        "evidence_rows": [
            {"role": e.role, "summary": e.summary, "event_id": e.event_id} for e in packet.evidence
        ],
        "materiality": {
            "minor": packet.materiality_minor,
            "currency": packet.currency,
            "formatted": format_minor(packet.materiality_minor, packet.currency),
        },
        "recommendation": {
            "id": packet.recommendation,
            "label": recommended.label if recommended else packet.recommendation,
            "reason": packet.recommendation_reason,
        },
        "options": options,
        "if_no_answer": packet.consequence_of_silence,
        "expires_at": _iso(packet.expires_at) if packet.expires_at else None,
        "auto_action_on_expiry": packet.auto_action_on_expiry,
        "refs": list(packet.refs),
        "decision": None
        if packet.decision is None
        else {
            "decision": packet.decision.decision,
            "label": next(
                (o.label for o in packet.options if o.id == packet.decision.decision),
                packet.decision.decision,
            ),
            "note": packet.decision.note,
            "decided_by": packet.decision.decided_by,
            "decided_at": _iso(packet.decision.decided_at),
            "via": packet.decision.via,
        },
    }
