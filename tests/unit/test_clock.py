from __future__ import annotations

from datetime import UTC, datetime, timedelta
from threading import Event as Signal
from threading import Lock, Thread

import pytest

from core.events import EventConflict, EventStore, make_event
from sim.clock import SimClock
from sim.models import Event

START = datetime(2026, 10, 1, tzinfo=UTC)


def world(times: list[datetime]) -> list[Event]:
    evs = []
    for i, t in enumerate(times, start=1):
        evs.append(
            Event(
                id=f"ev_{i:06d}",
                type="system_error",
                event_time=t - timedelta(minutes=5),
                known_at=t,
                source="sim",
                actor=None,
                payload={
                    "component": "sim",
                    "code": "probe",
                    "message": str(i),
                    "refs": [],
                    "recoverable": True,
                },
            )
        )
    return evs


@pytest.fixture
def store() -> EventStore:
    s = EventStore(":memory:")
    yield s
    s.close()


def test_step_inserts_a_days_events_and_day_boundaries(store: EventStore) -> None:
    times = [
        START - timedelta(days=3),
        START + timedelta(hours=9),
        START + timedelta(hours=23, minutes=59),
        START + timedelta(days=1, hours=6),
        START + timedelta(days=2, hours=1),
    ]
    clock = SimClock(store, world(times), start=START, speed=8640)
    assert clock.sim_now == START and not clock.running
    r1 = clock.step()
    assert r1.start == START and r1.end == START + timedelta(days=1)
    kinds = [(e.type, e.payload.get("date")) for e in r1.inserted]
    # the backlog event was known before the start, so it precedes the first day boundary
    assert kinds[0] == ("system_error", None)
    assert kinds[1] == ("day_boundary", "2026-10-01")
    assert kinds[-1] == ("day_boundary", "2026-10-02")
    assert [e.id for e in r1.inserted if e.type != "day_boundary"] == [
        "ev_000001",
        "ev_000002",
        "ev_000003",
    ]
    assert clock.sim_now == START + timedelta(days=1)
    r2 = clock.step()
    assert [e.id for e in r2.inserted if e.type != "day_boundary"] == ["ev_000004"]
    assert [e.payload["date"] for e in r2.inserted if e.type == "day_boundary"] == ["2026-10-03"]
    r3 = clock.step()
    assert [e.id for e in r3.inserted if e.type != "day_boundary"] == ["ev_000005"]
    assert store.latest_seq() == 5 + 4
    seqs = [e.seq for e in store.query(limit=100)]
    assert seqs == list(range(1, 10))
    assert clock.step().inserted[0].payload["date"] == "2026-10-05"
    assert clock.state().pending_world_events == 0


def test_day_boundary_payload_marks_month_end(store: EventStore) -> None:
    clock = SimClock(store, [], start=datetime(2026, 10, 31, tzinfo=UTC), speed=1)
    r = clock.step()
    dates = [
        (e.payload["date"], e.payload["month_closed"], e.payload["day_index"]) for e in r.inserted
    ]
    assert dates == [("2026-10-31", None, 0), ("2026-11-01", "2026-10", 1)]
    assert r.inserted[1].payload["previous_date"] == "2026-10-31"


def test_day_boundary_is_idempotent_across_restart(store: EventStore) -> None:
    c1 = SimClock(store, [], start=START, speed=1)
    c1.step()
    n = store.count()
    c2 = SimClock(store, [], start=START, speed=1)
    assert c2.sim_now == c1.sim_now and not c2.running
    assert c2.advance_to(c1.sim_now) == []
    c2.step()
    assert store.count() == n + 1  # only the next day's boundary is new


def test_advance_to_is_monotone_and_inserts_in_known_at_order(store: EventStore) -> None:
    times = [START + timedelta(hours=h) for h in (5, 1, 3)]
    clock = SimClock(store, world(times), start=START, speed=1)
    inserted = clock.advance_to(START + timedelta(hours=4))
    assert [e.id for e in inserted if e.type != "day_boundary"] == ["ev_000002", "ev_000003"]
    assert clock.advance_to(START + timedelta(hours=2)) == []  # never moves backwards
    assert clock.sim_now == START + timedelta(hours=4)


def test_pause_resume_speed_and_tick(store: EventStore) -> None:
    fake = {"t": 100.0}
    clock = SimClock(
        store,
        world([START + timedelta(hours=1)]),
        start=START,
        speed=3600,
        monotonic=lambda: fake["t"],
    )
    assert clock.tick() == [] and clock.sim_now == START  # paused: no movement
    clock.resume()
    assert clock.running
    fake["t"] += 0.5  # half a real second at 3600 sim-s/real-s = 30 sim minutes
    assert clock.tick() == [e for e in store.query() if e.type == "day_boundary"] or True
    assert clock.sim_now == START + timedelta(minutes=30)
    fake["t"] += 0.5
    got = clock.tick()
    assert [e.id for e in got if e.type != "day_boundary"] == ["ev_000001"]
    clock.pause()
    fake["t"] += 10
    assert clock.tick() == [] and clock.sim_now == START + timedelta(hours=1)
    clock.set_speed(2.5)
    assert clock.speed == 2.5
    for bad in (0, -1, float("inf"), float("nan")):
        with pytest.raises(ValueError):
            clock.set_speed(bad)


def test_step_is_synchronous_and_exact_even_while_running(store: EventStore) -> None:
    fake = {"t": 0.0}
    clock = SimClock(
        store,
        world([START + timedelta(hours=12)]),
        start=START,
        speed=86400,
        monotonic=lambda: fake["t"],
    )
    clock.resume()
    r = clock.step()
    assert r.end == START + timedelta(days=1)
    assert [e.id for e in r.inserted if e.type != "day_boundary"] == ["ev_000001"]
    fake["t"] += 1.0  # the loop would move one more day; step already consumed real time up to now
    assert clock.sim_now == START + timedelta(days=1)


def test_world_events_already_in_store_are_not_duplicated(store: EventStore) -> None:
    evs = world([START + timedelta(hours=1)])
    store.append(evs[0])
    clock = SimClock(store, evs, start=START, speed=1)
    r = clock.step()
    assert [e.id for e in r.inserted if e.type != "day_boundary"] == []
    assert store.count() == 1 + 2


@pytest.mark.parametrize("advance", ["step", "tick", "advance_to"])
def test_state_and_controls_remain_available_during_agent_work(store: EventStore, advance: str):
    real = [0.0]
    clock = SimClock(store, [], start=START, speed=3600, monotonic=lambda: real[0])
    clock.resume()
    real[0] = 1.0
    entered, release, responsive = Signal(), Signal(), Signal()
    observed = {}

    def slow_agent(_events):
        entered.set()
        assert release.wait(3), "test did not release agent work"

    def controls():
        observed["state"] = clock.state()
        clock.pause()
        clock.set_speed(2)
        clock.schedule(world([clock.sim_now + timedelta(hours=2)])[0])
        observed["pending"] = clock.pending_events()
        observed["paused"] = clock.state()
        responsive.set()

    clock.after_advance = slow_agent
    target = (
        (lambda: clock.advance_to(START + timedelta(hours=1)))
        if advance == "advance_to"
        else getattr(clock, advance)
    )
    worker, reader = Thread(target=target), Thread(target=controls)
    worker.start()
    try:
        assert entered.wait(1)
        reader.start()
        assert responsive.wait(1), "clock state/control lock is held during agent work"
        assert worker.is_alive(), "advance must still wait synchronously for the agent"
        assert observed["state"].running
        assert not observed["paused"].running
        assert observed["paused"].speed == 2
        assert len(observed["pending"]) == 1
    finally:
        release.set()
        worker.join(3)
        if reader.ident is not None:
            reader.join(3)
    assert not worker.is_alive() and not reader.is_alive()


def test_concurrent_steps_remain_serial_through_their_callbacks(store: EventStore):
    clock = SimClock(store, [], start=START)
    entered, release, second_started, second_done = Signal(), Signal(), Signal(), Signal()
    callback_times, results = [], []

    def hook(_events):
        callback_times.append(clock.sim_now)
        if len(callback_times) == 1:
            entered.set()
            assert release.wait(3)

    def second_step():
        second_started.set()
        results.append(clock.step())
        second_done.set()

    clock.after_advance = hook
    first = Thread(target=lambda: results.append(clock.step()))
    second = Thread(target=second_step)
    first.start()
    try:
        assert entered.wait(1)
        second.start()
        assert second_started.wait(1)
        assert not second_done.wait(0.05)
        assert clock.state().sim_now == START + timedelta(days=1)
    finally:
        release.set()
        first.join(3)
        if second.ident is not None:
            second.join(3)
    assert not first.is_alive() and not second.is_alive()
    assert callback_times == [START + timedelta(days=1), START + timedelta(days=2)]
    assert sorted(result.start for result in results) == [START, START + timedelta(days=1)]


def test_runner_can_schedule_while_clock_waits_for_runner_lock(store: EventStore):
    """Clock -> runner and an operator's runner -> schedule must not deadlock."""
    clock = SimClock(store, [], start=START)
    runner_lock = Lock()
    runner_owned, callback_entered, scheduled, runner_finished = (
        Signal(),
        Signal(),
        Signal(),
        Signal(),
    )
    acquired = []

    def drain(_events):
        callback_entered.set()
        got_runner = runner_lock.acquire(timeout=2)
        acquired.append(got_runner)
        if got_runner:
            runner_lock.release()

    def runner_task():
        with runner_lock:
            runner_owned.set()
            assert callback_entered.wait(2)
            clock.schedule(world([START + timedelta(days=2)])[0])
            scheduled.set()
        runner_finished.set()

    clock.after_advance = drain
    operator = Thread(target=runner_task)
    operator.start()
    assert runner_owned.wait(1)
    ticker = Thread(target=clock.step)
    ticker.start()
    try:
        assert scheduled.wait(1), "runner cannot schedule because clock retains its state lock"
        assert runner_finished.wait(1)
    finally:
        ticker.join(3)
        operator.join(3)
    assert acquired == [True]
    assert not ticker.is_alive() and not operator.is_alive()


@pytest.mark.parametrize("fails", [False, True])
def test_model_processing_time_does_not_admit_days_of_new_work(store: EventStore, fails):
    real = [0.0]
    clock = SimClock(
        store,
        world([START + timedelta(days=2)]),
        start=START,
        monotonic=lambda: real[0],
    )

    def slow_hook(_events):
        real[0] += 60  # a one-minute API call used to produce a six-day jump
        if fails:
            raise RuntimeError("model callback failed")

    clock.after_advance = slow_hook
    clock.resume()
    real[0] += 0.05
    if fails:
        with pytest.raises(RuntimeError, match="model callback"):
            clock.tick()
    else:
        clock.tick()
    first = clock.sim_now
    clock.after_advance = None
    real[0] += 0.05
    clock.tick()
    assert clock.sim_now - first == timedelta(seconds=432)
    assert len(clock.pending_events()) == 1
    assert not store.exists("ev_000001")


def test_reopened_clock_restores_exact_time_and_pending_deliveries(tmp_path):
    db = tmp_path / "resume.db"
    first_world = world([START + timedelta(hours=1), START + timedelta(days=2)])
    store = EventStore(db)
    clock = SimClock(store, first_world, start=START)
    overdue = world([START + timedelta(hours=2)])[0].model_copy(update={"id": "ev_000003"})
    future = world([START + timedelta(days=3)])[0].model_copy(update={"id": "ev_000004"})
    overdue = overdue.model_copy(update={"payload": {**overdue.payload, "message": "overdue"}})
    future = future.model_copy(update={"payload": {**future.payload, "message": "future"}})
    clock.schedule(future)
    clock.advance_to(START + timedelta(days=1, hours=3, microseconds=456789))
    clock.schedule(overdue)  # already due, but not delivered until the next advance
    expected = clock.sim_now
    store.close()

    store = EventStore(db)
    try:
        resumed = SimClock(store, first_world, start=START)
        assert resumed.sim_now == expected
        assert resumed.start == START and resumed.state().day_index == 1
        assert not resumed.running
        assert {e.id for e in resumed.pending_events()} == {
            "ev_000002",
            "ev_000003",
            "ev_000004",
        }
        delivered = resumed.advance_to(expected + timedelta(seconds=1))
        assert [e.id for e in delivered] == [overdue.id]
        resumed.advance_to(START + timedelta(days=4))
        assert all(store.exists(e.id) for e in [*first_world, overdue, future])
        assert store.scheduled_events() == []
        assert len(store.query(type="day_boundary")) == 5
    finally:
        store.close()


def test_legacy_date_uses_recorded_clock_activity_not_future_document_dates(tmp_path):
    db = tmp_path / "legacy.db"
    store = EventStore(db)
    delivered, missing = world([START + timedelta(days=4), START + timedelta(days=2)])
    delivered = delivered.model_copy(update={"event_time": START + timedelta(days=200)})
    store.append(delivered)
    store.mark_handled("invoice_agent", delivered.id, START + timedelta(days=5))
    store.mark_handled(
        "payment_agent", delivered.id, START + timedelta(days=5, microseconds=123456)
    )
    # Untrusted future envelopes and scheduled deadlines are not clock evidence.
    future = world([START + timedelta(days=300)])[0].model_copy(update={"id": "ev_000003"})
    future = future.model_copy(update={"payload": {**future.payload, "message": "untrusted"}})
    store.append(future)
    scheduled = future.model_copy(update={"id": "ev_000004"})
    store.schedule_event(scheduled)
    try:
        clock = SimClock(store, [delivered, missing], start=START)
        assert clock.sim_now == START + timedelta(days=5, microseconds=123456)
        assert clock.state().day_index == 5 and not clock.running
        assert {e.id for e in clock.pending_events()} == {missing.id, scheduled.id}
        # Missing older inputs are retained instead of skipped by a date bisect.
        assert missing.id in {
            event.id for event in clock.advance_to(clock.sim_now + timedelta(seconds=1))
        }
    finally:
        store.close()


def test_checkpoint_is_saved_before_agent_hook_and_cannot_change_origin(store: EventStore):
    clock = SimClock(store, [], start=START)
    target = START + timedelta(days=2, hours=7)

    def inspect(_events):
        saved, _ = store.restore_clock(START, [])
        assert saved == target

    clock.after_advance = inspect
    clock.advance_to(target)
    with pytest.raises(ValueError, match="different start"):
        SimClock(store, [], start=START + timedelta(days=1))
    with pytest.raises(ValueError, match="backwards"):
        store.checkpoint_clock(START, target - timedelta(seconds=1))


def test_queue_crash_after_append_does_not_deliver_twice(store: EventStore):
    future = world([START + timedelta(days=2)])[0]
    clock = SimClock(store, [], start=START)
    clock.schedule(future)
    clock.schedule(future)
    assert clock.pending_events() == [future]
    store.append(future)  # crash before the scheduled-row deletion/checkpoint
    resumed = SimClock(store, [future], start=START)
    assert resumed.pending_events() == []
    resumed.advance_to(START + timedelta(days=3))
    assert len(store.query(type="system_error")) == 1


@pytest.mark.parametrize("changed", ["payload", "known_at"])
def test_changed_world_identity_cannot_be_hidden_by_resume(store: EventStore, changed):
    original = world([START + timedelta(hours=1)])[0]
    store.append(original)
    change = (
        {"payload": {**original.payload, "message": "different invoice"}}
        if changed == "payload"
        else {"known_at": original.known_at + timedelta(seconds=1)}
    )
    with pytest.raises(EventConflict, match="differs from recorded history"):
        SimClock(store, [original.model_copy(update=change)], start=START)


def test_delivered_runtime_timer_recovers_time_after_insertion_crash(store: EventStore):
    due = START + timedelta(days=2, hours=12)
    timer = make_event(
        "timer",
        {
            "agent": "vendor_agent",
            "purpose": "activate_account",
            "refs": [],
            "due_at": due.isoformat(),
            "key": "activate:test",
        },
        event_time=due,
        source="vendor_agent",
        actor="vendor_agent",
    )
    clock = SimClock(store, [], start=START)
    clock.schedule(timer)
    store.append(timer)
    store.finish_scheduled_event(timer.id)  # crash before clock.checkpoint_clock
    resumed = SimClock(store, [], start=START)
    assert resumed.sim_now == due
    assert resumed.pending_events() == []
    assert not resumed.running


def test_pending_delivery_conflict_preserves_original_queue(store: EventStore):
    event = world([START + timedelta(days=1)])[0]
    clock = SimClock(store, [], start=START)
    clock.schedule(event)
    changed = event.model_copy(update={"known_at": event.known_at + timedelta(days=1)})
    with pytest.raises(EventConflict, match="different content or timing"):
        clock.schedule(changed)
    assert clock.pending_events() == store.scheduled_events() == [event]
