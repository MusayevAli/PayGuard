from __future__ import annotations

import sqlite3
from datetime import UTC, datetime, timedelta

import pytest

from core.events import EventConflict, EventStore, canonical_json, content_hash, make_event
from sim.models import Event

T0 = datetime(2026, 10, 1, 8, 0, tzinfo=UTC)


def db_payload(i: int) -> dict:
    return {"date": f"2026-10-{i:02d}", "day_index": i - 1, "previous_date": None, "month_closed": None}


def mk(i: int, **kw) -> Event:
    return Event(id=f"ev_{i:06d}", type="day_boundary", event_time=T0 + timedelta(days=i),
                 known_at=T0 + timedelta(days=i), source="clock", actor=None, payload=db_payload(i), **kw)  # fmt: skip


@pytest.fixture
def store(tmp_path) -> EventStore:
    s = EventStore(tmp_path / "events.db")
    yield s
    s.close()


def test_schema_and_pragmas(store: EventStore, tmp_path) -> None:
    con = sqlite3.connect(tmp_path / "events.db")
    try:
        assert con.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
        tables = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        assert {"events", "agent_cursors"} <= tables
        cols = {r[1] for r in con.execute("PRAGMA table_info(events)")}
        assert cols == {"id", "seq", "type", "event_time", "known_at", "source", "actor", "payload", "content_hash"}
        idx = {r[1] for r in con.execute("PRAGMA index_list(events)")}
        assert "ix_events_type_seq" in idx
        triggers = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='trigger'")}
        assert len(triggers) >= 2
    finally:
        con.close()


def test_append_assigns_sequential_seq_and_get_query(store: EventStore) -> None:
    results = [store.append(mk(i)) for i in range(1, 6)]
    assert [r.event.seq for r in results] == [1, 2, 3, 4, 5]
    assert all(r.inserted for r in results)
    assert store.latest_seq() == 5
    assert store.count() == 5
    got = store.get("ev_000003")
    assert got is not None and got.seq == 3 and got.payload == db_payload(3)
    assert store.get("missing") is None
    page = store.query(since_seq=2, limit=2)
    assert [e.seq for e in page] == [3, 4]
    assert [e.id for e in store.query(type="day_boundary")] == [f"ev_{i:06d}" for i in range(1, 6)]
    assert store.query(type="po_created") == []


def test_append_is_idempotent_on_content_hash(store: EventStore) -> None:
    first = store.append(mk(1))
    again = store.append(mk(1))
    assert again.inserted is False
    assert again.event.seq == first.event.seq == 1
    # same content under a different id is still the same event (idempotent), nothing new stored
    dup = Event(id="ev_999999", type="day_boundary", event_time=T0, known_at=T0, source="clock",
                actor=None, payload=db_payload(1))  # fmt: skip
    res = store.append(dup)
    assert res.inserted is False and res.event.id == "ev_000001"
    assert store.count() == 1
    # same id with different content is a conflict, never silently accepted
    with pytest.raises(EventConflict):
        store.append(Event(id="ev_000001", type="day_boundary", event_time=T0, known_at=T0,
                           source="clock", actor=None, payload=db_payload(2)))  # fmt: skip
    assert store.count() == 1


def test_append_many_keeps_order(store: EventStore) -> None:
    res = store.append_many([mk(3), mk(1), mk(2)])
    assert [r.event.id for r in res] == ["ev_000003", "ev_000001", "ev_000002"]
    assert [r.event.seq for r in res] == [1, 2, 3]


def test_update_and_delete_are_rejected_by_triggers(store: EventStore, tmp_path) -> None:
    store.append(mk(1))
    con = sqlite3.connect(tmp_path / "events.db")
    try:
        with pytest.raises(sqlite3.DatabaseError, match="append-only"):
            con.execute("UPDATE events SET actor='mallory' WHERE id='ev_000001'")
        with pytest.raises(sqlite3.DatabaseError, match="append-only"):
            con.execute("DELETE FROM events WHERE id='ev_000001'")
        with pytest.raises(sqlite3.DatabaseError, match="append-only"):
            con.execute("DELETE FROM events")
        row = con.execute("SELECT actor FROM events WHERE id='ev_000001'").fetchone()
        assert row[0] is None
    finally:
        con.close()
    assert store.count() == 1


def test_content_hash_is_sha256_over_type_and_canonical_payload() -> None:
    h1 = content_hash("day_boundary", {"b": 1, "a": [1, 2]})
    h2 = content_hash("day_boundary", {"a": [1, 2], "b": 1})
    assert h1 == h2 and len(h1) == 64
    assert content_hash("other_type", {"a": [1, 2], "b": 1}) != h1
    assert canonical_json({"b": 1, "a": "ž"}) == '{"a":"ž","b":1}'


def test_make_event_derives_a_stable_id() -> None:
    e1 = make_event("day_boundary", db_payload(1), event_time=T0, source="clock")
    e2 = make_event("day_boundary", db_payload(1), event_time=T0 + timedelta(hours=5), source="clock")
    assert e1.id == e2.id and e1.id.startswith("ev_")
    assert e1.known_at == e1.event_time
    assert make_event("day_boundary", db_payload(2), event_time=T0, source="clock").id != e1.id


def test_cursors(store: EventStore) -> None:
    for i in range(1, 4):
        store.append(mk(i))
    assert store.unhandled("invoice_agent", types=["day_boundary"]) == store.query()
    assert store.mark_handled("invoice_agent", "ev_000002", T0) is True
    assert store.mark_handled("invoice_agent", "ev_000002", T0) is False  # idempotent
    assert store.is_handled("invoice_agent", "ev_000002")
    assert not store.is_handled("vendor_agent", "ev_000002")
    assert [e.id for e in store.unhandled("invoice_agent")] == ["ev_000001", "ev_000003"]
    assert store.unhandled("invoice_agent", types=["po_created"]) == []


def test_subscribers_get_only_newly_inserted_events(store: EventStore) -> None:
    seen: list[str] = []
    typed: list[str] = []
    unsub = store.subscribe(lambda ev: seen.append(ev.id))
    store.subscribe(lambda ev: typed.append(ev.id), types=["po_created"])
    store.append(mk(1))
    store.append(mk(1))  # duplicate: no notification
    assert seen == ["ev_000001"]
    assert typed == []
    unsub()
    store.append(mk(2))
    assert seen == ["ev_000001"]


def test_store_survives_reopen(tmp_path) -> None:
    path = tmp_path / "e.db"
    s = EventStore(path)
    s.append(mk(1))
    s.close()
    s2 = EventStore(path)
    try:
        assert s2.latest_seq() == 1
        assert s2.append(mk(2)).event.seq == 2
    finally:
        s2.close()


def test_memory_store_works() -> None:
    s = EventStore(":memory:")
    try:
        assert s.append(mk(1)).event.seq == 1
    finally:
        s.close()
