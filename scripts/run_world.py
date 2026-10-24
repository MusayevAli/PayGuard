"""Run the generated world with the invoice agent only: bootstrap, step the clock day by day,
drain agents after every step, score, and report API usage. Used by the integration test and
for the Part 3 demo (``uv run python -m scripts.run_world --until 2026-11-05``)."""

from __future__ import annotations

import argparse
import json
import sys
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

import structlog
from pydantic import BaseModel, ConfigDict

from agents.invoice_agent import InvoiceAgent
from agents.llm import LLMClient, UsageReport
from agents.runner import Runner
from agents.state import AgentState
from api.main import load_world_events
from core.bootstrap import bootstrap
from core.events import EventStore
from core.ledger import Ledger
from core.packets import PacketStore
from core.policy import Policy
from core.score import Scoreboard, ScoreReport
from core.vendors import VendorMaster
from sim.clock import SimClock
from sim.fx import load_fx_rates
from sim.models import AnswerKey, Invoice, Person, Supplier
from sim.people import Responders

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


def run_world(
    world_dir: Path,
    run_dir: Path,
    *,
    until: str,
    cache_db: Path | None = None,
    llm: LLMClient | None = None,
) -> RunResult:
    run_dir.mkdir(parents=True, exist_ok=True)
    db = run_dir / "payguard.db"
    for suffix in ("", "-wal", "-shm"):
        p = Path(str(db) + suffix)
        if p.exists():
            p.unlink()
    bean = run_dir / "ledger" / "payguard.beancount"
    store = EventStore(db)
    events = load_world_events(world_dir / "world.jsonl")
    clock = SimClock(store, events, start=START, speed=1)
    fx = load_fx_rates(world_dir / "fx_rates.json")
    ledger = Ledger(db, events=store, fx=fx, now=lambda: clock.sim_now, beancount_path=bean)
    vendors = VendorMaster(db)
    packets = PacketStore(db, events=store, now=lambda: clock.sim_now)
    state = AgentState(db)
    policy = Policy(
        ledger=ledger,
        vendors=vendors,
        events=store,
        jumper_present=lambda: False,
        now=lambda: clock.sim_now,
    )
    suppliers = [
        Supplier.model_validate(s) for s in json.loads((world_dir / "suppliers.json").read_text())
    ]
    people = [Person.model_validate(p) for p in json.loads((world_dir / "people.json").read_text())]
    truth = [
        Invoice.model_validate(i)
        for i in json.loads((world_dir / "invoices_truth.json").read_text())
    ]
    key = AnswerKey.model_validate(json.loads((world_dir / "answer_key.json").read_text()))
    bootstrap(ledger, store, vendors, suppliers=suppliers)
    client = llm or LLMClient(cache_db or (run_dir / "llm_cache.db"), now=lambda: clock.sim_now)
    responders = Responders(
        people, events_source=lambda: events + clock.pending_events(), clock=clock, store=store
    )
    scoreboard = Scoreboard(
        key,
        store=store,
        ledger=ledger,
        vendors=vendors,
        truth=truth,
        fx=fx,
        now=lambda: clock.sim_now,
    )
    agent = InvoiceAgent(
        world_dir=world_dir,
        store=store,
        ledger=ledger,
        vendors=vendors,
        packets=packets,
        state=state,
        llm=client,
        suppliers=suppliers,
        now=lambda: clock.sim_now,
    )
    runner = Runner(
        store=store,
        policy=policy,
        agents=[agent],
        now=lambda: clock.sim_now,
        explainer=client,
        messenger=responders,
        scoreboard=scoreboard,
    )
    runner.attach(clock)
    daily: list[bool] = []
    end = date.fromisoformat(until)
    while clock.sim_now.date() <= end:
        clock.step()
        daily.append(ledger.ap_control_reconciles().ok)
    report = scoreboard.evaluate()
    actions = store.query(type="agent_action", limit=1_000_000)
    violations = sum(1 for e in actions if e.payload["action_type"] == "policy_violation")
    scope_rejections = sum(
        1
        for e in actions
        if e.payload["action_type"] == "post_rejected"
        and e.payload["decision"].get("reason_code") == "out_of_scope"
    )
    usage = client.usage_report()
    result = RunResult(
        report=report,
        usage=usage,
        daily_reconciliation=daily,
        policy_violations=violations,
        post_rejections_scope=scope_rejections,
        beancount_path=bean,
        db_path=db,
    )
    ledger.close()
    vendors.close()
    packets.close()
    state.close()
    if llm is None:
        client.close()
    store.close()
    return result


def _evidence(db: Path, bean: Path) -> dict[str, Any]:
    """The verbatim items the Part 3 completion message pastes."""
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
    parser.add_argument("--out", type=Path, default=Path("runs/part3"))
    parser.add_argument("--until", default="2026-11-05")
    parser.add_argument("--cache-db", type=Path, default=Path(".cache/llm_cache.db"))
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
    result = run_world(args.world, args.out, until=args.until, cache_db=args.cache_db)
    out = {
        "scoreboard": result.report.model_dump(mode="json"),
        "usage": result.usage.model_dump(),
        "daily_reconciliation_ok": all(result.daily_reconciliation),
        "policy_violations": result.policy_violations,
        "post_rejections_scope": result.post_rejections_scope,
        **_evidence(result.db_path, result.beancount_path),
    }
    sys.stdout.write(json.dumps(out, ensure_ascii=False, indent=2) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
