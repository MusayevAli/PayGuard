from __future__ import annotations

from datetime import UTC, date, datetime
from pathlib import Path

import pytest

from agents.base import Context
from agents.runner import Runner
from core.events import EventStore, make_event
from core.ledger import Evidence, JournalEntryDraft, JournalLineDraft, Ledger
from core.policy import Policy, PostAction
from core.vendors import VendorMaster
from sim.clock import SimClock
from sim.models import Event
from tests.agentkit import FakeLLM, full_synthetic_fx

NOW = datetime(2026, 10, 2, tzinfo=UTC)


class NullMessenger:
    def ask(self, query, *, asked_event_id: str, sim_time: datetime) -> None:
        return None


class Env:
    def __init__(self, tmp_path: Path) -> None:
        db = tmp_path / "pg.db"
        self.store = EventStore(db)
        self.ledger = Ledger(db, events=self.store, fx=full_synthetic_fx(), now=lambda: NOW)
        self.vendors = VendorMaster(db)
        self.policy = Policy(
            ledger=self.ledger,
            vendors=self.vendors,
            events=self.store,
            jumper_present=lambda: False,
            now=lambda: NOW,
        )
        self.llm = FakeLLM()
        self.ev = self.store.append(
            make_event(
                "system_error",
                {"component": "t", "code": "seed", "message": "m", "refs": [], "recoverable": True},
                event_time=NOW,
                source="test",
            )
        ).event

    def runner(self, agents: list) -> Runner:
        return Runner(
            store=self.store,
            policy=self.policy,
            agents=agents,
            now=lambda: NOW,
            explainer=self.llm,
            messenger=NullMessenger(),
        )


class ExplodingAgent:
    principal = "invoice_agent"
    subscribes = frozenset({"system_error"})

    def handle(self, ev: Event, ctx: Context) -> list[Event]:
        raise RuntimeError("boom on " + ev.id)


class PostingAgent:
    principal = "invoice_agent"
    subscribes = frozenset({"system_error"})

    def __init__(self) -> None:
        self.results: list = []

    def handle(self, ev: Event, ctx: Context) -> list[Event]:
        draft = JournalEntryDraft(
            effective_date=date(2026, 10, 2),
            description="test",
            cause_type="invoice",
            cause_id="INV-1",
            lines=[
                JournalLineDraft(
                    account="Expenses:Materials:Foam", debit_minor=100, currency="EUR"
                ),
                JournalLineDraft(account="Liabilities:AP", credit_minor=100, currency="EUR"),
            ],
            evidence=[Evidence(event_id=ev.id, role="invoice")],
        )
        res = ctx.act(
            action_type="posted",
            rule_id="R2a",
            refs=["INV-1", ev.id],
            inputs={},
            decision={"post_minor": 100},
            policy_action=PostAction(draft=draft),
        )
        self.results.append(res)
        return [res.event]


class Emitter:
    principal = "invoice_agent"
    subscribes = frozenset({"system_error"})

    def handle(self, ev: Event, ctx: Context) -> list[Event]:
        out = ctx.emit(
            "vendor_message",
            {
                "message_id": "m-" + ev.id,
                "channel": "invoice",
                "vendor_id": "V-0001",
                "claimed_supplier_name": "x",
                "from_address": "a@b",
                "reply_to": None,
                "to": "ap",
                "subject": "s",
                "body": "b",
                "eml_path": None,
                "in_reply_to": None,
                "thread_id": "t",
                "attachments": [],
                "bank_details": None,
            },
            source="invoice_agent",
            actor="invoice_agent",
        )
        return [out]


class Listener:
    principal = "vendor_agent"
    subscribes = frozenset({"vendor_message"})

    def __init__(self) -> None:
        self.seen: list[str] = []

    def handle(self, ev: Event, ctx: Context) -> list[Event]:
        self.seen.append(ev.id)
        return []


def test_exceptions_become_events_and_the_cursor_moves_on(tmp_path: Path) -> None:
    env = Env(tmp_path)
    runner = env.runner([ExplodingAgent()])
    handled = runner.drain()
    assert handled == 1
    errors = [
        e for e in env.store.query(type="agent_action") if e.payload["action_type"] == "error"
    ]
    assert (
        len(errors) == 1
        and "boom" in errors[0].payload["explanation"]
        and env.ev.id in errors[0].payload["refs"]
    )
    sys_errors = env.store.query(type="system_error")
    assert any(
        e.payload["code"] == "RuntimeError" and e.payload["component"] == "invoice_agent"
        for e in sys_errors
    )
    assert env.store.is_handled("invoice_agent", env.ev.id)
    assert runner.drain() == 0  # nothing is retried blindly


def test_duplicate_post_is_a_successful_no_op(tmp_path: Path) -> None:
    env = Env(tmp_path)
    agent = PostingAgent()
    runner = env.runner([agent])
    runner.drain()
    first = agent.results[0]
    assert first.result.id == "JE-000001" and first.duplicate is False
    # replay: a second event with the same cause produces the same draft -> duplicate -> no-op with the existing entry
    env.store.append(
        make_event(
            "system_error",
            {"component": "t", "code": "seed2", "message": "m", "refs": [], "recoverable": True},
            event_time=NOW,
            source="test",
        )
    )
    agent.results.clear()
    runner.drain()
    assert (
        len(env.ledger.entries()) == 2
    )  # different evidence -> a different entry; now replay the *same* draft
    draft = JournalEntryDraft(
        effective_date=date(2026, 10, 2),
        description="test",
        cause_type="invoice",
        cause_id="INV-1",
        lines=[
            JournalLineDraft(account="Expenses:Materials:Foam", debit_minor=100, currency="EUR"),
            JournalLineDraft(account="Liabilities:AP", credit_minor=100, currency="EUR"),
        ],
        evidence=[Evidence(event_id=env.ev.id, role="invoice")],
    )
    ctx = Context(
        principal="invoice_agent",
        policy=env.policy,
        store=env.store,
        now=lambda: NOW,
        explainer=env.llm,
        messenger=NullMessenger(),
    )
    res = ctx.act(
        action_type="posted",
        rule_id="R2a",
        refs=["INV-1"],
        inputs={},
        decision={},
        policy_action=PostAction(draft=draft),
    )
    assert res.duplicate is True and res.result.id == "JE-000001"
    assert len(env.ledger.entries()) == 2
    assert not [
        e
        for e in env.store.query(type="agent_action")
        if e.payload["action_type"] == "post_rejected"
    ]


def test_drain_reaches_a_fixpoint_across_agents_and_clock_hook(tmp_path: Path) -> None:
    env = Env(tmp_path)
    listener = Listener()
    runner = env.runner([Emitter(), listener])
    clock = SimClock(env.store, [], start=NOW, speed=1)
    runner.attach(clock)
    clock.step()
    assert listener.seen, "the emitted vendor_message was handled in the same drain"
    assert env.store.is_handled("vendor_agent", listener.seen[0])
    actions = env.store.query(type="agent_action")
    assert all(a.payload["explanation"] for a in actions)


def test_context_rejects_unknown_action_types(tmp_path: Path) -> None:
    env = Env(tmp_path)
    ctx = Context(
        principal="invoice_agent",
        policy=env.policy,
        store=env.store,
        now=lambda: NOW,
        explainer=env.llm,
        messenger=NullMessenger(),
    )
    with pytest.raises(ValueError):
        ctx.act(action_type="did_something", rule_id=None, refs=[], inputs={}, decision={})
