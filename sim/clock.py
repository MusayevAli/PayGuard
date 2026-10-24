"""Simulation clock (spec §14).

``speed`` is sim-seconds per real-second (default 8640 = one sim day per ten real seconds).
World events are inserted into the store when ``sim_now >= known_at``; a ``day_boundary``
event is emitted at every sim midnight (including the start instant when it is midnight).
``step`` advances exactly one day synchronously. Pausing stops the real-time loop.
"""

from __future__ import annotations

import math
import threading
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from core.events import EventStore, make_event
from sim.models import Event

DEFAULT_SPEED = 8640.0
ONE_DAY = timedelta(days=1)


@dataclass(frozen=True, slots=True)
class StepResult:
    start: datetime
    end: datetime
    inserted: list[Event]


@dataclass(frozen=True, slots=True)
class ClockState:
    sim_now: datetime
    running: bool
    speed: float
    start: datetime
    day_index: int
    pending_world_events: int
    next_known_at: datetime | None
    latest_seq: int


def _midnight_on_or_after(t: datetime) -> datetime:
    floor = t.replace(hour=0, minute=0, second=0, microsecond=0)
    return floor if floor == t else floor + ONE_DAY


class SimClock:
    def __init__(
        self,
        store: EventStore,
        world_events: Sequence[Event],
        *,
        start: datetime,
        speed: float = DEFAULT_SPEED,
        monotonic: Callable[[], float] = time.monotonic,
        tick_seconds: float = 0.05,
    ) -> None:
        if start.tzinfo is None:
            raise ValueError("start must be timezone-aware")
        self._store = store
        self._start = start.astimezone(UTC)
        self._sim_now = self._start
        self._speed = DEFAULT_SPEED
        self.set_speed(speed)
        self._running = False
        self._monotonic = monotonic
        self._last_real = monotonic()
        self._tick_seconds = tick_seconds
        self._lock = threading.RLock()
        self._pending: list[Event] = sorted(world_events, key=lambda e: (e.known_at, e.id))
        self._cursor = 0
        self._next_boundary = _midnight_on_or_after(self._sim_now)
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self.after_advance: Callable[[list[Event]], None] | None = None

    # -- state ----------------------------------------------------------------------------

    @property
    def sim_now(self) -> datetime:
        return self._sim_now

    @property
    def speed(self) -> float:
        return self._speed

    @property
    def running(self) -> bool:
        return self._running

    @property
    def start(self) -> datetime:
        return self._start

    def state(self) -> ClockState:
        with self._lock:
            nxt = (
                self._pending[self._cursor].known_at if self._cursor < len(self._pending) else None
            )
            return ClockState(
                sim_now=self._sim_now,
                running=self._running,
                speed=self._speed,
                start=self._start,
                day_index=(self._sim_now - self._start) // ONE_DAY,
                pending_world_events=len(self._pending) - self._cursor,
                next_known_at=nxt,
                latest_seq=self._store.latest_seq(),
            )

    # -- controls -------------------------------------------------------------------------

    def set_speed(self, speed: float) -> None:
        if not isinstance(speed, int | float) or isinstance(speed, bool):
            raise ValueError("speed must be a number")
        if not math.isfinite(speed) or speed <= 0:
            raise ValueError(
                "speed must be a finite number greater than zero (sim-seconds per real-second)"
            )
        with getattr(self, "_lock", threading.RLock()):
            self._speed = float(speed)

    def pause(self) -> None:
        with self._lock:
            self._running = False

    def resume(self) -> None:
        with self._lock:
            self._last_real = self._monotonic()
            self._running = True

    def step(self) -> StepResult:
        """Advance exactly one sim day and insert everything that became known, synchronously."""
        with self._lock:
            start = self._sim_now
            end = start + ONE_DAY
            inserted = self.advance_to(end)
            self._last_real = self._monotonic()
            return StepResult(start=start, end=end, inserted=inserted)

    def tick(self) -> list[Event]:
        """Advance by the real time elapsed since the last tick, scaled by ``speed``."""
        with self._lock:
            now_real = self._monotonic()
            if not self._running:
                self._last_real = now_real
                return []
            elapsed = max(0.0, now_real - self._last_real)
            self._last_real = now_real
            target = self._sim_now + timedelta(seconds=elapsed * self._speed)
            return self.advance_to(target)

    def advance_to(self, target: datetime) -> list[Event]:
        """Move sim time forward to ``target`` (never backwards), inserting in known_at order
        and emitting day boundaries as midnights are crossed."""
        target = target.astimezone(UTC)
        inserted: list[Event] = []
        with self._lock:
            if target <= self._sim_now:
                return inserted
            while True:
                next_event = (
                    self._pending[self._cursor] if self._cursor < len(self._pending) else None
                )
                boundary_due = self._next_boundary <= target
                event_due = next_event is not None and next_event.known_at <= target
                if not boundary_due and not event_due:
                    break
                if boundary_due and (
                    next_event is None or self._next_boundary <= next_event.known_at
                ):
                    self._emit_boundary(inserted)
                    continue
                assert next_event is not None
                self._cursor += 1
                result = self._store.append(next_event)
                if result.inserted:
                    inserted.append(result.event)
            self._sim_now = target
        hook = self.after_advance
        if hook is not None:
            hook(inserted)
        return inserted

    def schedule(self, ev: Event) -> None:
        """Queue a future event (a colleague reply, a scheduled receipt). It is inserted when
        ``sim_now`` reaches its ``known_at`` like any world event; past-dated events go next."""
        with self._lock:
            tail = self._pending[self._cursor :]
            tail.append(ev)
            tail.sort(key=lambda e: (e.known_at, e.id))
            self._pending = self._pending[: self._cursor] + tail

    def pending_events(self) -> list[Event]:
        with self._lock:
            return list(self._pending[self._cursor :])

    def _emit_boundary(self, inserted: list[Event]) -> None:
        day = self._next_boundary
        previous = day - ONE_DAY
        payload = {
            "date": day.date().isoformat(),
            "day_index": (day - self._start) // ONE_DAY,
            "previous_date": previous.date().isoformat() if day > self._start else None,
            "month_closed": previous.strftime("%Y-%m")
            if day.day == 1 and day > self._start
            else None,
        }
        ev = make_event("day_boundary", payload, event_time=day, source="clock", actor=None)
        result = self._store.append(ev)
        if result.inserted:
            inserted.append(result.event)
        self._next_boundary = day + ONE_DAY

    # -- background loop ------------------------------------------------------------------

    def start_loop(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="sim-clock", daemon=True)
        self._thread.start()

    def stop_loop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=5)
            self._thread = None

    def _run(self) -> None:
        while not self._stop.is_set():
            self.tick()
            self._stop.wait(self._tick_seconds)
