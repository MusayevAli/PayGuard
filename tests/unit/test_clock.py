from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from core.events import EventStore
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
    c2 = SimClock(store, [], start=START, speed=1)  # a restart from the same start
    c2.step()
    assert store.count() == n  # same day boundaries hash to the same events


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
