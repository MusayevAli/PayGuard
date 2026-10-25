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

    def drain_events(
        self, events: Sequence[Event], *, progress: Callable[[dict[str, Any]], None] | None = None
    ) -> int:
        """Process stored inputs and events emitted by their handlers, without draining the inbox.

        The normal clock drain retains its ordering. Interactive submissions can
        process their own event tree while unrelated queued invoices remain queued.
        Only synchronous events emitted on this worker thread are descendants;
        arrivals concurrently appended by the clock never enter this queue.
        """
        with self._lock:
            pending: dict[str, Event] = {}
            for event in events:
                stored = self._store.get(event.id)
                if stored is None:
                    raise ValueError("Scoped processing requires a stored input event")
                pending[stored.id] = stored
            thread_id = threading.get_ident()
            capturing = False

            def capture(event: Event) -> None:
                if capturing and threading.get_ident() == thread_id:
                    pending[event.id] = event

            unsubscribe = self._store.subscribe(capture)
            handled = 0
            try:
                for _ in range(self._max_rounds):
                    count = 0
                    for agent in self._agents:
                        selected = sorted(
                            (
                                event
                                for event in list(pending.values())
                                if event.type in agent.subscribes
                                and not self._store.is_handled(agent.principal, event.id)
                            ),
                            key=lambda event: event.seq or 0,
                        )
                        for event in selected:
                            activity = {
                                "agent": agent.principal,
                                "event_id": event.id,
                                "event_type": event.type,
                                "refs": [event.id],
                            }
                            self._progress(progress, {**activity, "state": "working"})
                            capturing = True
                            try:
                                self._handle(agent, event)
                            finally:
                                capturing = False
                            self._progress(progress, {**activity, "state": "finished"})
                            count += 1
                    handled += count
                    if count == 0:
                        break
                if self._scoreboard is not None:
                    self._scoreboard.publish()
                return handled
            finally:
                unsubscribe()

    @staticmethod
    def _progress(
        callback: Callable[[dict[str, Any]], None] | None, activity: dict[str, Any]
    ) -> None:
        """A presentation observer must never change the result of an agent action."""
        if callback is not None:
            try:
                callback(activity)
            except Exception:
                log.warning("progress_observer_failed", agent=activity["agent"])

    def run_task(
        self,
        agent: Agent,
        task: Callable[[Context], Any],
        *,
        scoped: bool = False,
        progress: Callable[[dict[str, Any]], None] | None = None,
        refs: Sequence[str] = (),
    ) -> Any:
        """Run ``task(ctx)`` for ``agent`` outside an event (a button press, an operator action)
        with the same policy context and error-to-event handling as ``drain``; then drain."""
        with self._lock:
            activity = {
                "agent": agent.principal,
                "event_id": None,
                "event_type": "operator_task",
                "refs": list(refs),
            }
            self._progress(progress, {**activity, "state": "working"})
            created: list[Event] = []
            thread_id = threading.get_ident()

            def capture(event: Event) -> None:
                if threading.get_ident() == thread_id:
                    created.append(event)

            unsubscribe = self._store.subscribe(capture) if scoped else lambda: None
            ctx = Context(
                principal=agent.principal,
                policy=self._policy,
                store=self._store,
                now=self._now,
                explainer=self._explainer,
                messenger=self._messenger,
            )
            error: str | None = None
            try:
                result = task(ctx)
            except Exception as exc:
                log.error(
                    "agent_task_failed",
                    agent=agent.principal,
                    error=repr(exc),
                    event_id=None,
                    principal=agent.principal,
                    rule_id=None,
                )
                self._system_error(agent.principal, exc, refs=list(refs), code=type(exc).__name__)
                error = f"{type(exc).__name__}: {exc}"[:1000]
                result = None
            finally:
                unsubscribe()
            self._progress(
                progress,
                {**activity, "state": "failed", "error": error}
                if error is not None
                else {**activity, "state": "finished"},
            )
            if scoped:
                self.drain_events(created, progress=progress)
            else:
                self.drain()
            return result

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
