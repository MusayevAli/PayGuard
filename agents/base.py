"""Agent skeleton (spec §7.1). ``Context`` is the only route from an agent to side effects: it
executes policy actions, logs every action as an ``agent_action`` event with an explanation
generated *after* the decision, and relays colleague queries to the messenger."""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Protocol, get_args

from core.events import EventStore, make_event
from core.ledger import PostRejected
from core.policy import Action as PolicyAction
from core.policy import Policy
from sim.models import ActionType, ColleagueQuery, Event

VALID_ACTION_TYPES: frozenset[str] = frozenset(get_args(ActionType))


class Explainer(Protocol):
    def explain(self, decision: dict[str, Any]) -> str: ...


class Messenger(Protocol):
    def ask(self, query: ColleagueQuery, *, asked_event_id: str, sim_time: datetime) -> None: ...


@dataclass(frozen=True)
class ActResult:
    event: Event
    result: Any
    duplicate: bool


def jsonable(obj: Any) -> Any:
    return json.loads(json.dumps(obj, default=str, ensure_ascii=False))


class Context:
    def __init__(
        self,
        *,
        principal: str,
        policy: Policy,
        store: EventStore,
        now: Callable[[], datetime],
        explainer: Explainer,
        messenger: Messenger,
    ) -> None:
        self.principal = principal
        self._policy = policy
        self._store = store
        self._now = now
        self._explainer = explainer
        self._messenger = messenger

    @property
    def now(self) -> datetime:
        return self._now()

    @property
    def policy(self) -> Policy:
        return self._policy

    def act(
        self,
        *,
        action_type: str,
        rule_id: str | None,
        refs: list[str],
        inputs: dict[str, Any],
        decision: dict[str, Any],
        policy_action: PolicyAction | None = None,
        explanation: str | None = None,
    ) -> ActResult:
        """Execute ``policy_action`` (if any) through the policy, then record the action.

        A ``PostRejected(duplicate)`` is a successful no-op: the existing entry is returned. Other
        rejections propagate after the policy has logged them."""
        if action_type not in VALID_ACTION_TYPES:
            raise ValueError(f"unknown action_type {action_type!r}")
        result: Any = None
        duplicate = False
        if policy_action is not None:
            try:
                result = self._policy.execute(policy_action, self.principal)
            except PostRejected as exc:
                if exc.reason_code != "duplicate" or exc.existing_entry_id is None:
                    raise
                result = self._policy.ledger.get_entry(exc.existing_entry_id)
                duplicate = True
        inputs_j, decision_j = jsonable(inputs), jsonable(decision)
        text = (
            explanation
            if explanation is not None
            else self._explainer.explain(
                {
                    "action_type": action_type,
                    "rule_id": rule_id,
                    "refs": list(refs),
                    "inputs": inputs_j,
                    "decision": decision_j,
                }
            )
        )
        payload = {
            "agent": self.principal,
            "action_type": action_type,
            "refs": list(dict.fromkeys(refs)),
            "rule_id": rule_id,
            "inputs": inputs_j,
            "decision": decision_j,
            "explanation": text,
        }
        ev = self._store.append(
            make_event(
                "agent_action", payload, event_time=self.now, source="agent", actor=self.principal
            )
        ).event
        return ActResult(event=ev, result=result, duplicate=duplicate)

    def with_principal(self, principal: str) -> Context:
        """The same session acting as another principal (a controller decision the agent carries
        out); every action is still logged, under that principal."""
        return Context(
            principal=principal,
            policy=self._policy,
            store=self._store,
            now=self._now,
            explainer=self._explainer,
            messenger=self._messenger,
        )

    def emit(
        self,
        type_: str,
        payload: dict[str, Any],
        *,
        source: str,
        actor: str | None,
        event_time: datetime | None = None,
    ) -> Event:
        return self._store.append(
            make_event(
                type_,
                jsonable(payload),
                event_time=event_time or self.now,
                source=source,
                actor=actor,
            )
        ).event

    def ask(self, query: ColleagueQuery, *, asked_event_id: str) -> None:
        self._messenger.ask(query, asked_event_id=asked_event_id, sim_time=self.now)


class Agent(Protocol):
    principal: str
    subscribes: frozenset[str]

    def handle(self, ev: Event, ctx: Context) -> list[Event]: ...
