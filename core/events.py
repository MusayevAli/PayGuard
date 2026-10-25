"""Append-only SQLite event store (spec §5.1).

* WAL journal, ``synchronous=NORMAL``.
* ``events`` rows can never be updated or deleted: triggers RAISE on both.
* ``append`` is idempotent on ``content_hash = sha256(type + canonical payload)``.
* ``agent_cursors`` records which (agent, event) pairs were handled.
* In-process subscribers are notified synchronously for newly inserted events only.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
from collections.abc import Callable, Collection, Iterable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from sim.models import Event

SCHEMA = """
CREATE TABLE IF NOT EXISTS events (
  id TEXT PRIMARY KEY, seq INTEGER UNIQUE NOT NULL,
  type TEXT NOT NULL, event_time TEXT NOT NULL, known_at TEXT NOT NULL,
  source TEXT NOT NULL, actor TEXT, payload TEXT NOT NULL,
  content_hash TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_events_type_seq ON events(type, seq);
CREATE UNIQUE INDEX IF NOT EXISTS ix_events_content_hash ON events(content_hash);
CREATE TABLE IF NOT EXISTS agent_cursors (
  agent TEXT, event_id TEXT, handled_at TEXT, PRIMARY KEY(agent, event_id)
);
CREATE TABLE IF NOT EXISTS simulation_clock (
  singleton INTEGER PRIMARY KEY CHECK(singleton = 1), start TEXT NOT NULL, sim_now TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS scheduled_events (
  event_id TEXT PRIMARY KEY, event_json TEXT NOT NULL
);
CREATE TRIGGER IF NOT EXISTS trg_events_no_update BEFORE UPDATE ON events
BEGIN SELECT RAISE(ABORT, 'events is append-only: UPDATE forbidden'); END;
CREATE TRIGGER IF NOT EXISTS trg_events_no_delete BEFORE DELETE ON events
BEGIN SELECT RAISE(ABORT, 'events is append-only: DELETE forbidden'); END;
"""

Subscriber = Callable[[Event], None]


class EventConflict(RuntimeError):
    """An event id already exists with different content."""


@dataclass(frozen=True, slots=True)
class Appended:
    event: Event
    inserted: bool


def canonical_json(obj: Any) -> str:
    """Deterministic JSON: sorted keys, compact separators, UTF-8 preserved."""
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def content_hash(type_: str, payload: Mapping[str, Any]) -> str:
    return hashlib.sha256((type_ + "\n" + canonical_json(payload)).encode("utf-8")).hexdigest()


def new_event_id(type_: str, payload: Mapping[str, Any]) -> str:
    """Runtime events get ids derived from their content hash: ``ev_`` + 16 hex characters.

    Generator events use ``ev_`` + 6 digits, so the two namespaces cannot collide.
    """
    return "ev_" + content_hash(type_, payload)[:16]


def make_event(
    type_: str,
    payload: Mapping[str, Any],
    *,
    event_time: datetime,
    source: str,
    known_at: datetime | None = None,
    actor: str | None = None,
) -> Event:
    data = dict(payload)
    return Event(
        id=new_event_id(type_, data),
        type=type_,  # type: ignore[arg-type]
        event_time=event_time,
        known_at=known_at if known_at is not None else event_time,
        source=source,
        actor=actor,
        payload=data,
    )


def _iso(dt: datetime) -> str:
    return dt.astimezone(UTC).isoformat().replace("+00:00", "Z")


class EventStore:
    def __init__(self, path: str | Path, *, timeout: float = 30.0) -> None:
        self._path = str(path)
        self._lock = threading.RLock()
        self._subscribers: list[tuple[int, Subscriber, frozenset[str] | None]] = []
        self._next_token = 1
        self._con = sqlite3.connect(
            self._path, timeout=timeout, check_same_thread=False, isolation_level=None
        )
        self._con.row_factory = sqlite3.Row
        self._con.execute("PRAGMA journal_mode=WAL")
        self._con.execute("PRAGMA synchronous=NORMAL")
        self._con.execute("PRAGMA foreign_keys=ON")
        self._con.executescript(SCHEMA)

    @property
    def path(self) -> str:
        return self._path

    def close(self) -> None:
        with self._lock:
            self._con.close()

    # -- writes ---------------------------------------------------------------------------

    def append(self, ev: Event) -> Appended:
        """Insert ``ev`` unless an event with the same content hash exists. Never raises on
        a replay; raises ``EventConflict`` if the id is taken by different content."""
        digest = content_hash(ev.type, ev.payload)
        with self._lock:
            self._con.execute("BEGIN IMMEDIATE")
            try:
                row = self._con.execute(
                    "SELECT * FROM events WHERE content_hash = ?", (digest,)
                ).fetchone()
                if row is not None:
                    self._con.execute("COMMIT")
                    return Appended(_row_to_event(row), inserted=False)
                clash = self._con.execute("SELECT 1 FROM events WHERE id = ?", (ev.id,)).fetchone()
                if clash is not None:
                    self._con.execute("ROLLBACK")
                    raise EventConflict(f"event id {ev.id} already exists with different content")
                seq_row = self._con.execute(
                    "SELECT COALESCE(MAX(seq), 0) + 1 FROM events"
                ).fetchone()
                seq = int(seq_row[0])
                self._con.execute(
                    "INSERT INTO events(id, seq, type, event_time, known_at, source, actor, payload, content_hash)"
                    " VALUES (?,?,?,?,?,?,?,?,?)",
                    (
                        ev.id,
                        seq,
                        ev.type,
                        _iso(ev.event_time),
                        _iso(ev.known_at),
                        ev.source,
                        ev.actor,
                        canonical_json(ev.payload),
                        digest,
                    ),
                )
                self._con.execute("COMMIT")
            except BaseException:
                if self._con.in_transaction:
                    self._con.execute("ROLLBACK")
                raise
            stored = ev.model_copy(update={"seq": seq})
            subscribers = list(self._subscribers)
        for _, callback, types in subscribers:
            if types is None or stored.type in types:
                callback(stored)
        return Appended(stored, inserted=True)

    def append_many(self, events: Iterable[Event]) -> list[Appended]:
        return [self.append(ev) for ev in events]

    # -- reads ----------------------------------------------------------------------------

    def get(self, event_id: str) -> Event | None:
        with self._lock:
            row = self._con.execute("SELECT * FROM events WHERE id = ?", (event_id,)).fetchone()
        return None if row is None else _row_to_event(row)

    def query(
        self, *, type: str | None = None, since_seq: int = 0, limit: int = 200
    ) -> list[Event]:
        if limit < 1:
            raise ValueError("limit must be >= 1")
        sql = "SELECT * FROM events WHERE seq > ?"
        params: list[Any] = [since_seq]
        if type is not None:
            sql += " AND type = ?"
            params.append(type)
        sql += " ORDER BY seq LIMIT ?"
        params.append(limit)
        with self._lock:
            rows = self._con.execute(sql, params).fetchall()
        return [_row_to_event(r) for r in rows]

    def latest_seq(self) -> int:
        with self._lock:
            row = self._con.execute("SELECT COALESCE(MAX(seq), 0) FROM events").fetchone()
        return int(row[0])

    def count(self) -> int:
        with self._lock:
            row = self._con.execute("SELECT COUNT(*) FROM events").fetchone()
        return int(row[0])

    def has_type(self, type_: str) -> bool:
        with self._lock:
            row = self._con.execute(
                "SELECT 1 FROM events WHERE type = ? LIMIT 1", (type_,)
            ).fetchone()
        return row is not None

    def exists(self, event_id: str) -> bool:
        with self._lock:
            return (
                self._con.execute("SELECT 1 FROM events WHERE id = ?", (event_id,)).fetchone()
                is not None
            )

    # -- durable simulation time and future deliveries -----------------------------------

    def restore_clock(
        self, start: datetime, world_events: Collection[Event]
    ) -> tuple[datetime, set[str]]:
        """Recover from checkpoints and recorded clock activity, never document dates.

        Legacy databases have no checkpoint. Delivered world arrival times, runner
        cursor times and clock/agent/score envelopes establish a conservative lower
        bound. Looking at those records also covers a crash during event insertion
        before the next checkpoint. Future scheduled events are deliberately excluded.
        """
        if start.tzinfo is None:
            raise ValueError("clock start must be timezone-aware")
        start = start.astimezone(UTC)
        world = {event.id: event for event in world_events}
        with self._lock:
            checkpoint = self._con.execute("SELECT start, sim_now FROM simulation_clock").fetchone()
            if checkpoint and datetime.fromisoformat(checkpoint["start"]) != start:
                raise ValueError("existing simulation clock has a different start")
            times = [start]
            if checkpoint:
                times.append(datetime.fromisoformat(checkpoint["sim_now"]))
            rows = self._con.execute(
                "SELECT id, type, source, event_time, known_at, content_hash FROM events"
            ).fetchall()
            recorded_ids = {row["id"] for row in rows}
            trusted_pairs = {
                ("day_boundary", "clock"),
                ("agent_action", "agent"),
                ("score_update", "scoreboard"),
                ("colleague_reply", "people"),
                ("timer", "vendor_agent"),
                ("bank_statement", "bank"),
                ("bank_statement_line", "bank"),
            }
            for row in rows:
                expected = world.get(row["id"])
                if expected is not None and (
                    row["content_hash"] != content_hash(expected.type, expected.payload)
                    or datetime.fromisoformat(row["event_time"]) != expected.event_time
                    or datetime.fromisoformat(row["known_at"]) != expected.known_at
                ):
                    raise EventConflict(f"world event {expected.id} differs from recorded history")
                if expected is not None or (row["type"], row["source"]) in trusted_pairs:
                    times.append(datetime.fromisoformat(row["known_at"]))
            times.extend(
                datetime.fromisoformat(row[0])
                for row in self._con.execute("SELECT handled_at FROM agent_cursors")
            )
        # ISO strings with/without fractional seconds do not sort chronologically.
        recovered = max(times)
        self.checkpoint_clock(start, recovered)
        return recovered, recorded_ids

    def checkpoint_clock(self, start: datetime, sim_now: datetime) -> None:
        if start.tzinfo is None or sim_now.tzinfo is None or sim_now < start:
            raise ValueError("clock checkpoint must contain aware, ordered timestamps")
        with self._lock:
            self._con.execute("BEGIN IMMEDIATE")
            try:
                prior = self._con.execute("SELECT start, sim_now FROM simulation_clock").fetchone()
                if prior and (
                    datetime.fromisoformat(prior["start"]) != start
                    or datetime.fromisoformat(prior["sim_now"]) > sim_now
                ):
                    raise ValueError("clock checkpoint cannot change start or move backwards")
                self._con.execute(
                    "INSERT INTO simulation_clock(singleton, start, sim_now) VALUES (1,?,?) "
                    "ON CONFLICT(singleton) DO UPDATE SET sim_now=excluded.sim_now",
                    (_iso(start), _iso(sim_now)),
                )
                self._con.execute("COMMIT")
            except BaseException:
                if self._con.in_transaction:
                    self._con.execute("ROLLBACK")
                raise

    def schedule_event(self, event: Event) -> bool:
        """Persist a future delivery before its caller can report it scheduled."""
        event = event.model_copy(update={"seq": None})
        encoded = canonical_json(event.model_dump(mode="json"))
        with self._lock:
            self._con.execute("BEGIN IMMEDIATE")
            try:
                delivered = self._con.execute(
                    "SELECT content_hash FROM events WHERE id=?", (event.id,)
                ).fetchone()
                queued = self._con.execute(
                    "SELECT event_json FROM scheduled_events WHERE event_id=?", (event.id,)
                ).fetchone()
                if delivered and delivered["content_hash"] != content_hash(
                    event.type, event.payload
                ):
                    raise EventConflict(
                        f"event id {event.id} already exists with different content"
                    )
                if queued and queued["event_json"] != encoded:
                    raise EventConflict(
                        f"scheduled event {event.id} has different content or timing"
                    )
                inserted = delivered is None and queued is None
                if inserted:
                    self._con.execute(
                        "INSERT INTO scheduled_events(event_id, event_json) VALUES (?,?)",
                        (event.id, encoded),
                    )
                self._con.execute("COMMIT")
                return inserted
            except BaseException:
                if self._con.in_transaction:
                    self._con.execute("ROLLBACK")
                raise

    def scheduled_events(self) -> list[Event]:
        with self._lock:
            rows = self._con.execute(
                "SELECT event_json FROM scheduled_events WHERE event_id NOT IN (SELECT id FROM events)"
            ).fetchall()
        return [Event.model_validate_json(row[0]) for row in rows]

    def finish_scheduled_event(self, event_id: str) -> None:
        """Remove queue metadata only after successful idempotent delivery."""
        with self._lock:
            self._con.execute("DELETE FROM scheduled_events WHERE event_id=?", (event_id,))

    # -- cursors --------------------------------------------------------------------------

    def mark_handled(self, agent: str, event_id: str, handled_at: datetime) -> bool:
        """Record that ``agent`` handled ``event_id``. Returns False if it was already recorded."""
        with self._lock:
            cur = self._con.execute(
                "INSERT OR IGNORE INTO agent_cursors(agent, event_id, handled_at) VALUES (?,?,?)",
                (agent, event_id, _iso(handled_at)),
            )
            return cur.rowcount == 1

    def handled_by_any(self, event_id: str) -> bool:
        with self._lock:
            row = self._con.execute(
                "SELECT 1 FROM agent_cursors WHERE event_id = ? LIMIT 1", (event_id,)
            ).fetchone()
        return row is not None

    def is_handled(self, agent: str, event_id: str) -> bool:
        with self._lock:
            row = self._con.execute(
                "SELECT 1 FROM agent_cursors WHERE agent = ? AND event_id = ?", (agent, event_id)
            ).fetchone()
        return row is not None

    def unhandled(
        self, agent: str, types: Collection[str] | None = None, limit: int = 1000
    ) -> list[Event]:
        sql = (
            "SELECT e.* FROM events e LEFT JOIN agent_cursors c ON c.event_id = e.id AND c.agent = ?"
            " WHERE c.event_id IS NULL"
        )
        params: list[Any] = [agent]
        if types is not None:
            wanted = sorted(set(types))
            if not wanted:
                return []
            sql += " AND e.type IN (" + ",".join("?" * len(wanted)) + ")"
            params.extend(wanted)
        sql += " ORDER BY e.seq LIMIT ?"
        params.append(limit)
        with self._lock:
            rows = self._con.execute(sql, params).fetchall()
        return [_row_to_event(r) for r in rows]

    # -- subscribers ----------------------------------------------------------------------

    def subscribe(
        self, callback: Subscriber, types: Collection[str] | None = None
    ) -> Callable[[], None]:
        with self._lock:
            token = self._next_token
            self._next_token += 1
            self._subscribers.append((token, callback, None if types is None else frozenset(types)))

        def unsubscribe() -> None:
            with self._lock:
                self._subscribers = [s for s in self._subscribers if s[0] != token]

        return unsubscribe


def _row_to_event(row: sqlite3.Row) -> Event:
    return Event(
        id=row["id"],
        seq=row["seq"],
        type=row["type"],
        event_time=datetime.fromisoformat(row["event_time"]),
        known_at=datetime.fromisoformat(row["known_at"]),
        source=row["source"],
        actor=row["actor"],
        payload=json.loads(row["payload"]),
    )
