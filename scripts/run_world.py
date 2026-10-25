"""Run the generated world through the desk: bootstrap, provision, step the clock one sim day at
a time, drain the agents after every step, score, and report API usage and bank balances.
``uv run python -m scripts.run_world --until 2026-11-05``."""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Callable, Sequence
from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path
from typing import Any

import structlog
from pydantic import BaseModel, ConfigDict

from agents.llm import UsageReport
from agents.runtime import Runtime, build_runtime, start_runtime
from api.main import load_world_events
from core.events import EventStore
from core.score import ScoreReport
from sim.clock import SimClock

START = datetime(2026, 10, 1, tzinfo=UTC)


class RunResult(BaseModel):
    model_config = ConfigDict(extra="forbid", arbitrary_types_allowed=True)
    report: ScoreReport
    usage: UsageReport
    daily_reconciliation: list[bool]
    policy_violations: int
    post_rejections_scope: int
    beancount_path: Path
    db_path: Path
    bank_balances: dict[str, int]
    chip_status: dict[str, Any]


def run_world(
    world_dir: Path,
    run_dir: Path,
    *,
    until: str,
    cache_db: Path | None = None,
    llm: Any | None = None,
    agents: Sequence[str] = ("invoice", "payment"),
    before_start: Callable[[Runtime], None] | None = None,
    each_day: Callable[[Runtime, date], None] | None = None,
) -> RunResult:
    run_dir.mkdir(parents=True, exist_ok=True)
    db = run_dir / "payguard.db"
    for suffix in ("", "-wal", "-shm"):
        p = Path(str(db) + suffix)
        if p.exists():
            p.unlink()
    store_events = load_world_events(world_dir / "world.jsonl")
    store = EventStore(db)
    bean = run_dir / "ledger" / "payguard.beancount"
    clock = SimClock(store, store_events, start=START, speed=1)
    rt = build_runtime(
        world_dir=world_dir,
        db_path=db,
        store=store,
        clock=clock,
        out_dir=run_dir,
        world_events=store_events,
        llm=llm,
        agents=agents,
        cache_db=cache_db,
        beancount_path=bean,
    )
    try:
        start_runtime(rt)
        if before_start is not None:
            before_start(rt)
        daily: list[bool] = []
        end = date.fromisoformat(until)
        while clock.sim_now.date() <= end:
            day = clock.sim_now.date()
            # the morning: statements arrive at 06:00 and the payment run follows them
            clock.advance_to(datetime.combine(day, time(6, 30), tzinfo=UTC))
            clock.advance_to(datetime.combine(day + timedelta(days=1), time(0, 0), tzinfo=UTC))
            daily.append(rt.ledger.ap_control_reconciles().ok)
            if each_day is not None:
                each_day(rt, clock.sim_now.date())
        assert rt.scoreboard is not None
        report = rt.scoreboard.evaluate()
        actions = store.query(type="agent_action", limit=1_000_000)
        violations = sum(1 for e in actions if e.payload["action_type"] == "policy_violation")
        scope_rejections = sum(
            1
            for e in actions
            if e.payload["action_type"] == "post_rejected"
            and e.payload["decision"].get("reason_code") == "out_of_scope"
        )
        usage = rt.llm.usage_report()
        return RunResult(
            report=report,
            usage=usage,
            daily_reconciliation=daily,
            policy_violations=violations,
            post_rejections_scope=scope_rejections,
            beancount_path=bean,
            db_path=db,
            bank_balances=rt.bank.balances(),
            chip_status=rt.signer.status().to_json(),
        )
    finally:
        rt.close()


def _evidence(db: Path, bean: Path) -> dict[str, Any]:
    """The verbatim items the completion messages paste."""
    store = EventStore(db)
    t1 = [
        e.payload
        for e in store.query(type="agent_action", limit=1_000_000)
        if "KT-2291" in e.payload["refs"]
        and e.payload["action_type"] in ("matched", "posted", "short_pay_notice")
    ]
    text = bean.read_text(encoding="utf-8")
    blocks = [
        b
        for b in text.split("\n\n")
        if "GRNI" in b and ("#accrual" in b or "#accrual_reversal" in b)
    ]
    store.close()
    return {"t1_actions": t1, "t10_entries": blocks}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--world", type=Path, default=Path("sim/world"))
    parser.add_argument("--out", type=Path, default=Path("runs/part4"))
    parser.add_argument("--until", default="2026-11-05")
    parser.add_argument("--cache-db", type=Path, default=Path(".cache/llm_cache.db"))
    parser.add_argument("--agents", default="invoice,payment")
    args = parser.parse_args(argv)
    structlog.configure(
        processors=[
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso", utc=True),
            structlog.processors.JSONRenderer(ensure_ascii=False),
        ],
        logger_factory=structlog.PrintLoggerFactory(file=sys.stderr),
        cache_logger_on_first_use=True,
    )
    result = run_world(
        args.world,
        args.out,
        until=args.until,
        cache_db=args.cache_db,
        agents=tuple(args.agents.split(",")),
    )
    out = {
        "scoreboard": result.report.model_dump(mode="json"),
        "usage": result.usage.model_dump(),
        "daily_reconciliation_ok": all(result.daily_reconciliation),
        "policy_violations": result.policy_violations,
        "post_rejections_scope": result.post_rejections_scope,
        "bank_balances": result.bank_balances,
        "chip": result.chip_status,
        **_evidence(result.db_path, result.beancount_path),
    }
    sys.stdout.write(json.dumps(out, ensure_ascii=False, indent=2) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
