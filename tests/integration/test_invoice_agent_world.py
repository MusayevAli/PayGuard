"""Run the generated world through 2026-11-05 with the invoice agent only (spec §17 Part 3)."""

from __future__ import annotations

import os
import subprocess
import sys
from collections.abc import Sequence
from pathlib import Path

import pytest

from agents.llm import LLMClient
from core.score import CaseStatus, ScoreReport
from scripts.run_world import run_world
from sim.models import AnswerKey, Event

HEADLINE_IN_SCOPE = ("T1", "T2", "T3", "T4", "T5", "T6", "T10")


def _ambient_in_scope(
    report: ScoreReport, key: AnswerKey, world_events: Sequence[Event]
) -> list[CaseStatus]:
    """Score cases whose required evidence has arrived by the run's exact cutoff.

    A pending case can be an already-arrived invoice the agent did not handle,
    so status must never determine eligibility. Missing answer-key event IDs
    are invalid fixtures and deliberately raise instead of shrinking the scope.
    """
    events_by_id = {event.id: event for event in world_events}
    eligible_ids = {
        entry.id
        for entry in key.ambient
        if entry.case != "M2"
        and max(events_by_id[event_id].known_at for event_id in entry.injected_event_ids)
        <= report.as_of
    }
    return [case for case in report.cases if case.tier == "ambient" and case.id in eligible_ids]


def test_world_run_through_november_5(
    world_dir: Path, world_events: list[Event], tmp_path: Path
) -> None:
    if not LLMClient.credentials_available():
        pytest.fail(
            "Anthropic credentials are required for the world run: export ANTHROPIC_API_KEY (or `ant auth login`)"
        )
    result = run_world(
        world_dir,
        tmp_path / "run",
        until="2026-11-05",
        agents=("invoice",),
        cache_db=Path(
            os.environ.get("PAYGUARD_TEST_LLM_CACHE", world_dir.parent / "extraction_cache.db")
        ),
    )
    report = result.report
    by_id = {c.id: c for c in report.cases}
    failures = {
        cid: [c.detail for c in by_id[cid].checks if not c.ok]
        for cid in HEADLINE_IN_SCOPE
        if by_id[cid].status != "caught"
    }
    assert failures == {}, failures
    key = AnswerKey.model_validate_json((world_dir / "answer_key.json").read_bytes())
    ambient = _ambient_in_scope(report, key, world_events)
    caught = [c for c in ambient if c.status == "caught"]
    missed = {
        c.id: [x.detail for x in c.checks if not x.ok] for c in ambient if c.status != "caught"
    }
    assert len(caught) >= 43 and len(caught) / len(ambient) >= 43 / 45, (
        len(caught),
        len(ambient),
        missed,
    )
    assert report.entries_without_evidence == 0
    assert result.daily_reconciliation and all(result.daily_reconciliation), (
        result.daily_reconciliation
    )
    assert result.policy_violations == 0
    assert result.post_rejections_scope == 0
    proc = subprocess.run(
        [sys.executable, "-m", "beancount.scripts.check", str(result.beancount_path)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 0 and not proc.stdout.strip() and not proc.stderr.strip(), (
        proc.stdout + proc.stderr
    )
    print("\nscoreboard:", report.model_dump_json())
    print("usage:", result.usage.model_dump_json())
