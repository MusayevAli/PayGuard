"""The full world through the invoice agent with a truth-backed extractor (no API needed).

Everything after extraction is exercised on the real world: vendor and PO resolution, receipts and
colleague replies, pricing, VAT, posting, payables, GRNI accruals, the scoreboard and the ledger
invariants. The API-backed twin is ``test_invoice_agent_world.py``."""

from __future__ import annotations

from pathlib import Path

import pytest
from beancount import loader

from scripts.run_world import RunResult, run_world
from tests.truth_llm import TruthLLM

WORLD = Path(__file__).resolve().parents[2] / "sim" / "world"
INVOICE_AGENT_CASES = ("T1", "T2", "T3", "T4", "T5", "T6", "T10")


@pytest.fixture(scope="module")
def result(tmp_path_factory: pytest.TempPathFactory) -> RunResult:
    out = tmp_path_factory.mktemp("world_offline")
    return run_world(WORLD, out, until="2026-11-05", llm=TruthLLM(WORLD), agents=("invoice",))


def _failed(case) -> list[tuple[str, str]]:
    return [(x.kind, x.detail) for x in case.checks if not x.ok]


def test_headline_cases_for_the_invoice_agent(result: RunResult) -> None:
    by_id = {c.id: c for c in result.report.cases}
    problems = {
        cid: (by_id[cid].status, _failed(by_id[cid]))
        for cid in INVOICE_AGENT_CASES
        if by_id[cid].status != "caught"
    }
    assert problems == {}, problems


def test_ambient_cases(result: RunResult) -> None:
    ambient = [c for c in result.report.cases if c.tier == "ambient"]
    caught = [c for c in ambient if c.status == "caught"]
    missed = {c.id: (c.status, _failed(c)) for c in ambient if c.status != "caught"}
    assert len(caught) >= 43, (len(caught), len(ambient), missed)
    assert result.report.ambient.fp == 0, result.report.ambient


def test_ledger_invariants_every_day(result: RunResult) -> None:
    assert result.report.entries_without_evidence == 0
    assert all(result.daily_reconciliation), result.daily_reconciliation
    assert result.policy_violations == 0
    assert result.post_rejections_scope == 0


def test_beancount_file_passes_bean_check(result: RunResult) -> None:
    _entries, errors, _options = loader.load_file(str(result.beancount_path))
    assert errors == [], [str(e) for e in errors[:5]]
