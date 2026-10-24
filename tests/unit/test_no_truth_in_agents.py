"""Ground truth is for the scoreboard and tests only; the agents must never touch it."""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
FORBIDDEN = re.compile(r"invoices_truth|ground_truth|answer_key|case_tags|mailbox_compromised")


def test_agents_do_not_reference_ground_truth() -> None:
    offenders = []
    for path in sorted((ROOT / "agents").rglob("*.py")):
        for n, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
            if FORBIDDEN.search(line):
                offenders.append(f"{path.relative_to(ROOT)}:{n}: {line.strip()}")
    assert offenders == [], "\n".join(offenders)


def test_only_score_and_tests_read_truth_files() -> None:
    readers = []
    for path in sorted(ROOT.rglob("*.py")):
        rel = path.relative_to(ROOT)
        if (
            ".venv" in rel.parts
            or rel.parts[0] in ("tests", "scripts")
            or rel == Path("core/score.py")
            or rel.parts[0] == "sim"
        ):
            continue
        text = path.read_text(encoding="utf-8")
        if "invoices_truth" in text or "answer_key.json" in text:
            readers.append(str(rel))
    assert readers == [], readers
