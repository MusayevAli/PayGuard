"""Run the generated world through 2026-11-05 with the invoice agent only (spec §17 Part 3)."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from agents.llm import LLMClient, extraction_cache_path
from scripts.run_world import run_world

pytestmark = pytest.mark.api  # spends real money; keeps the real environment and .env

HEADLINE_IN_SCOPE = ("T1", "T2", "T3", "T4", "T5", "T6", "T10")


def test_world_run_through_november_5(world_dir: Path, tmp_path: Path) -> None:
    if not LLMClient.credentials_available():
        pytest.fail(
            "Anthropic credentials are required for the world run: export ANTHROPIC_API_KEY (or `ant auth login`)"
        )
    result = run_world(
        world_dir,
        tmp_path / "run",
        until="2026-11-05",
        agents=("invoice",),
        cache_db=extraction_cache_path(),  # persistent: a document is paid for once per machine
    )
    report = result.report
    by_id = {c.id: c for c in report.cases}
    failures = {
        cid: [c.detail for c in by_id[cid].checks if not c.ok]
        for cid in HEADLINE_IN_SCOPE
        if by_id[cid].status != "caught"
    }
    assert failures == {}, failures
    ambient = [c for c in report.cases if c.tier == "ambient" and c.case != "M2"]
    caught = [c for c in ambient if c.status == "caught"]
    # a case whose injected events fall after 5 Nov is pending, not missed: it cannot be caught
    # inside the window and does not belong in the denominator
    catchable = [c for c in ambient if c.status != "pending"]
    missed = {
        c.id: [x.detail for x in c.checks if not x.ok] for c in catchable if c.status != "caught"
    }
    assert len(caught) >= 43 and len(caught) / len(catchable) >= 43 / 45, (
        len(caught),
        len(catchable),
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
