from __future__ import annotations

from threading import Thread

import pytest

from core.events import make_event
from tests.unit.test_runner import NOW, Emitter, Env, Listener


def seed(env, code):
    return env.store.append(
        make_event(
            "system_error",
            {"component": "test", "code": code, "message": code, "refs": [], "recoverable": True},
            event_time=NOW,
            source="test",
        )
    ).event


def test_scoped_drain_handles_descendants_without_processing_backlog(tmp_path):
    env = Env(tmp_path)
    selected = seed(env, "selected")
    listener = Listener()
    runner = env.runner([Emitter(), listener])
    progress = []
    assert runner.drain_events([selected], progress=progress.append) == 2
    assert not env.store.is_handled("invoice_agent", env.ev.id)
    assert env.store.is_handled("invoice_agent", selected.id)
    assert len(listener.seen) == 1
    assert [p["state"] for p in progress] == ["working", "finished", "working", "finished"]
    assert [p["agent"] for p in progress] == ["invoice_agent"] * 2 + ["vendor_agent"] * 2
    assert runner.drain_events([selected]) == 0
    assert runner.drain() == 2


def test_concurrent_arrivals_and_observer_events_are_not_descendants(tmp_path):
    env = Env(tmp_path)
    outside = []

    class ConcurrentEmitter(Emitter):
        def handle(self, event, ctx):
            thread = Thread(target=lambda: outside.append(seed(env, "clock-arrival")))
            thread.start()
            thread.join()
            return super().handle(event, ctx)

    def observer(activity):
        outside.append(seed(env, "observer-" + activity["agent"] + activity["state"]))

    listener = Listener()
    runner = env.runner([ConcurrentEmitter(), listener])
    assert runner.drain_events([env.ev], progress=observer) == 2
    assert len(listener.seen) == 1
    assert all(not env.store.is_handled("invoice_agent", event.id) for event in outside)


def test_observer_failure_does_not_change_processing_and_unstored_seed_rejected(tmp_path):
    env = Env(tmp_path)
    listener = Listener()
    runner = env.runner([Emitter(), listener])

    def observer(_):
        raise RuntimeError("disconnected browser")

    assert runner.drain_events([env.ev], progress=observer) == 2
    assert len(listener.seen) == 1
    unknown = env.ev.model_copy(update={"id": "missing-event"})
    with pytest.raises(ValueError, match="stored input"):
        runner.drain_events([unknown])


def test_scoped_task_processes_only_its_emitted_events(tmp_path):
    env = Env(tmp_path)
    emitter, listener = Emitter(), Listener()
    runner = env.runner([emitter, listener])
    activity = []
    runner.run_task(
        emitter,
        lambda ctx: emitter.handle(env.ev, ctx),
        scoped=True,
        progress=activity.append,
        refs=["LIVE-example"],
    )
    assert not env.store.is_handled("invoice_agent", env.ev.id)
    assert len(listener.seen) == 1
    assert activity[0]["event_type"] == "operator_task"
    assert activity[0]["refs"] == ["LIVE-example"]
    assert activity[-1]["agent"] == "vendor_agent"


def test_failed_task_retains_submission_refs_and_never_reports_finished(tmp_path):
    env = Env(tmp_path)
    emitter = Emitter()
    runner = env.runner([emitter])
    activity = []

    def payment_failure(_):
        raise RuntimeError("signer transport disconnected")

    result = runner.run_task(
        emitter,
        payment_failure,
        scoped=True,
        progress=activity.append,
        refs=["LIVE-submission", "PAY-000001"],
    )
    assert result is None
    assert [step["state"] for step in activity] == ["working", "failed"]
    assert activity[-1]["error"] == "RuntimeError: signer transport disconnected"
    errors = [event for event in env.store.query(type="system_error") if event.source == "runner"]
    assert len(errors) == 1
    assert errors[0].payload["refs"] == ["LIVE-submission", "PAY-000001"]
    assert env.store.is_handled(emitter.principal, errors[0].id)
    assert not env.store.is_handled(emitter.principal, env.ev.id)
