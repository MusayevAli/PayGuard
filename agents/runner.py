"""Runner (spec §7.1): drains subscribed events not yet in ``agent_cursors`` in seq order,
turns exceptions into ``agent_action{error}`` + ``system_error`` events, records the cursor and
continues. Hooks into the clock so a synchronous step drains everything before returning."""

from __future__ import annotations

import threading
import traceback
from collections.abc import Callable, Sequence
from datetime import datetime
from typing import Any

import structlog

from core.events import EventStore, make_event
from core.policy import Policy
from sim.clock import SimClock
from sim.models import Event

from .base import Agent, Context, Explainer, Messenger

log = structlog.get_logger("runner")


class Runner:
    def __init__(
        self,
        *,
        store: EventStore,
        policy: Policy,
        agents: Sequence[Agent],
        now: Callable[[], datetime],
        explainer: Explainer,
        messenger: Messenger,
        scoreboard: Any | None = None,
        max_rounds: int = 50,
    ) -> None:
        self._store = store
        self._policy = policy
        self._agents = list(agents)
        self._now = now
        self._explainer = explainer
        self._messenger = messenger
        self._scoreboard = scoreboard
        self._max_rounds = max_rounds
        self._lock = threading.RLock()

    def attach(self, clock: SimClock) -> None:
        clock.after_advance = lambda _inserted: self.drain()

    def drain(self) -> int:
        """Handle every pending subscribed event until no agent has work left; returns the count."""
        with self._lock:
            handled = 0
            for _ in range(self._max_rounds):
                progress = 0
                for agent in self._agents:
                    pending = self._store.unhandled(
                        agent.principal, types=agent.subscribes, limit=10000
                    )
                    if not pending:
                        continue
                    prefetch = getattr(agent, "prefetch", None)
                    if callable(prefetch):
                        try:
                            prefetch(pending)
                        except Exception as exc:
                            self._system_error(
                                agent.principal,
                                exc,
                                refs=[e.id for e in pending[:20]],
                                code="prefetch_failed",
                            )
                    for ev in pending:
                        self._handle(agent, ev)
                        progress += 1
                handled += progress
                if progress == 0:
                    break
            if self._scoreboard is not None:
                self._scoreboard.publish()
            return handled

    def _handle(self, agent: Agent, ev: Event) -> None:
        ctx = Context(
            principal=agent.principal,
            policy=self._policy,
            store=self._store,
            now=self._now,
            explainer=self._explainer,
            messenger=self._messenger,
        )
        try:
            agent.handle(ev, ctx)
        except Exception as exc:
            log.error(
                "agent_failed",
                agent=agent.principal,
                event_id=ev.id,
                error=repr(exc),
                rule_id=None,
                principal=agent.principal,
            )
            detail = f"{type(exc).__name__}: {exc}"
            logged = self._store.append(
                make_event(
                    "agent_action",
                    {
                        "agent": agent.principal,
                        "action_type": "error",
                        "refs": [ev.id],
                        "rule_id": None,
                        "inputs": {"event_type": ev.type},
                        "decision": {
                            "error": type(exc).__name__,
                            "traceback": traceback.format_exc()[-2000:],
                        },
                        "explanation": f"{agent.principal} failed on {ev.id}: {detail}",
                    },
                    event_time=self._now(),
                    source="runner",
                    actor=agent.principal,
                )
            ).event
            self._mark_internal(logged)
            self._system_error(agent.principal, exc, refs=[ev.id], code=type(exc).__name__)
        finally:
            self._store.mark_handled(agent.principal, ev.id, self._now())

    def _system_error(self, component: str, exc: Exception, *, refs: list[str], code: str) -> None:
        ev = self._store.append(
            make_event(
                "system_error",
                {
                    "component": component,
                    "code": code,
                    "message": str(exc)[:1000],
                    "refs": refs,
                    "recoverable": True,
                },
                event_time=self._now(),
                source="runner",
                actor=component,
            )
        ).event
        self._mark_internal(ev)

    def _mark_internal(self, ev: Event) -> None:
        """Events the runner writes about an agent's failure are for humans and the scoreboard; no
        agent re-processes them (that would loop an exploding agent forever)."""
        for agent in self._agents:
            self._store.mark_handled(agent.principal, ev.id, self._now())
