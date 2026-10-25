"""The live run's scoring denominator follows arrival time, never its results."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from core.events import make_event
from core.score import CaseStatus, ScoreReport, Tally
from sim.models import AnswerKey, AnswerKeyEntry, Event
from tests.integration.test_invoice_agent_world import _ambient_in_scope

CUTOFF = datetime(2026, 11, 6, tzinfo=UTC)


def _invoice(invoice_id: str, known_at: datetime) -> Event:
    return make_event(
        "invoice_received",
        {
            "invoice_id": invoice_id,
            "format": "pdf",
            "document_path": f"{invoice_id}.pdf",
            "received_via": "portal",
            "sender": None,
            "supplier_hint": "V-0001",
            "attachments": [],
            "size_bytes": 1,
            "sha256": "0" * 64,
            "pages": 1,
        },
        event_time=CUTOFF - timedelta(days=7),
        known_at=known_at,
        source="scope-test",
    )


def _fixture(
    event_ids: list[str], *, status: str = "missed", case: str = "A12"
) -> tuple[ScoreReport, AnswerKey]:
    entry = AnswerKeyEntry(
        id=f"{case}-1",
        tier="ambient",
        case=case,
        title="A case with required invoice evidence",
        expected_outcome="Invoice is posted correctly",
        injected_event_ids=event_ids,
        refs=["INV-0001"],
        checks=[
            {
                "kind": "event_exists",
                "type": "agent_action",
                "where": {"action_type": "posted", "refs_contains": "INV-0001"},
            }
        ],
    )
    report = ScoreReport(
        as_of=CUTOFF,
        headline=Tally(),
        ambient=Tally(),
        euros_at_risk_prevented_minor=0,
        entries_without_evidence=0,
        cases=[CaseStatus(id=entry.id, tier="ambient", case=case, status=status, checks=[])],
    )
    return report, AnswerKey(seed=42, headline=[], ambient=[entry])


@pytest.mark.parametrize("status", ["caught", "missed", "pending"])
@pytest.mark.parametrize("offset", [-1, 0, 1])
def test_scope_uses_known_at_and_includes_exact_cutoff_regardless_of_status(
    status: str, offset: int
) -> None:
    event = _invoice("INV-0001", CUTOFF + timedelta(microseconds=offset))
    report, key = _fixture([event.id], status=status)
    # All occurrences predate the cutoff. Only when the evidence became known
    # decides scope; an unhandled, already-arrived pending case still counts.
    expected = report.cases if offset <= 0 else []
    assert _ambient_in_scope(report, key, [event]) == expected


def test_case_requires_all_injected_evidence_to_have_arrived() -> None:
    arrived = _invoice("INV-0001", CUTOFF - timedelta(days=1))
    future = _invoice("INV-0002", CUTOFF + timedelta(hours=10))
    report, key = _fixture([arrived.id, future.id])
    assert _ambient_in_scope(report, key, [arrived, future]) == []


@pytest.mark.parametrize("future_first", [False, True])
def test_unknown_required_event_is_a_fixture_error_not_an_excluded_case(
    future_first: bool,
) -> None:
    future = _invoice("INV-0001", CUTOFF + timedelta(days=1))
    events = [future] if future_first else []
    report, key = _fixture([event.id for event in events] + ["ev_ffffff"])
    with pytest.raises(KeyError, match="ev_ffffff"):
        _ambient_in_scope(report, key, events)


def test_vendor_agent_case_remains_outside_invoice_only_scope() -> None:
    event = _invoice("INV-0001", CUTOFF)
    report, key = _fixture([event.id], case="M2")
    assert _ambient_in_scope(report, key, [event]) == []
