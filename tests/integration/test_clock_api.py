"""Clock + API + WebSocket acceptance (spec §13, §14, §17 Part 1)."""

from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path

import pytest
from starlette.testclient import TestClient

from api.main import create_app
from sim.models import Event

pytestmark = pytest.mark.filterwarnings("ignore::DeprecationWarning")


def all_events(client: TestClient) -> list[dict]:
    out: list[dict] = []
    since = 0
    while True:
        r = client.get("/events", params={"since_seq": since, "limit": 500})
        assert r.status_code == 200, r.text
        batch = r.json()["events"]
        if not batch:
            return out
        out.extend(batch)
        since = batch[-1]["seq"]


@pytest.fixture
def client(world_dir: Path, tmp_path: Path):
    app = create_app(world_dir=world_dir, db_path=tmp_path / "events.db", ping_interval=0.5)
    with TestClient(app) as c:
        yield c


def test_three_steps_insert_exactly_the_right_events(client: TestClient, world_events: list[Event]) -> None:
    state = client.get("/state").json()
    start = datetime.fromisoformat(state["sim_now"])
    assert start.isoformat() == "2026-10-01T00:00:00+00:00"
    assert state["running"] is False and state["speed"] == 8640
    assert state["latest_seq"] == 0 and state["pending_world_events"] == len(world_events)

    per_step: list[list[dict]] = []
    before = 0
    for _ in range(3):
        r = client.post("/clock/step")
        assert r.status_code == 200, r.text
        body = r.json()
        evs = [e for e in all_events(client) if e["seq"] > before]
        assert body["inserted"] == len(evs)
        before = evs[-1]["seq"]
        per_step.append(evs)

    day = timedelta(days=1)
    for k, evs in enumerate(per_step):
        lo = start + k * day
        hi = lo + day
        got = [e["id"] for e in evs if e["type"] != "day_boundary"]
        expected = [e.id for e in world_events if (e.known_at < hi if k == 0 else lo <= e.known_at < hi)]
        assert got == expected, f"step {k + 1}: inserted {len(got)} events, expected {len(expected)}"
        for e in evs:
            if e["type"] != "day_boundary":
                assert datetime.fromisoformat(e["known_at"]) < hi
        boundaries = [e["payload"]["date"] for e in evs if e["type"] == "day_boundary"]
        assert boundaries == (["2026-10-01", "2026-10-02"] if k == 0 else [(lo + day).date().isoformat()])

    seqs = [e["seq"] for e in all_events(client)]
    assert seqs == list(range(1, len(seqs) + 1))
    state = client.get("/state").json()
    assert state["sim_now"] == (start + 3 * day).isoformat().replace("+00:00", "Z")
    assert state["pending_world_events"] == len(world_events) - sum(
        1 for e in world_events if e.known_at < start + 3 * day
    )
    assert state["day_index"] == 3


def test_ws_live_delivers_in_seq_order_and_catches_up(client: TestClient) -> None:
    with client.websocket_connect("/live") as ws:
        hello = ws.receive_json()
        assert hello["kind"] == "hello" and hello["latest_seq"] == 0
        total = 0
        for _ in range(3):
            total += client.post("/clock/step").json()["inserted"]
        got: list[dict] = []
        pings = 0
        while len(got) < total:
            msg = ws.receive_json()
            if msg["kind"] == "ping":
                pings += 1
                assert pings < 40, "events never arrived on the socket"
                continue
            assert msg["kind"] == "event"
            got.append(msg["event"])
        seqs = [e["seq"] for e in got]
        assert seqs == list(range(1, total + 1))
        assert [e["id"] for e in got] == [e["id"] for e in all_events(client)]
    # reconnect-safe catch-up: a client that saw seq N asks for everything after it
    with client.websocket_connect("/live?since_seq=5") as ws:
        hello = ws.receive_json()
        assert hello["kind"] == "hello" and hello["latest_seq"] == total
        first = ws.receive_json()
        assert first["kind"] == "event" and first["event"]["seq"] == 6
        # live events after catch-up keep arriving in order with no gaps
        more = client.post("/clock/step").json()["inserted"]
        seen = [6]
        pings = 0
        while seen[-1] < total + more:
            msg = ws.receive_json()
            if msg["kind"] == "ping":
                pings += 1
                assert pings < 40
                continue
            seen.append(msg["event"]["seq"])
        assert seen == list(range(6, total + more + 1))


def test_ws_pings_every_interval(client: TestClient) -> None:
    with client.websocket_connect("/live") as ws:
        assert ws.receive_json()["kind"] == "hello"
        msg = ws.receive_json()
        assert msg["kind"] == "ping" and "latest_seq" in msg and "sim_now" in msg


def test_pause_resume_speed_and_error_model(client: TestClient) -> None:
    r = client.post("/clock/speed", json={"speed": 100})
    assert r.status_code == 200 and r.json()["speed"] == 100
    r = client.post("/clock/speed", json={"speed": -3})
    assert r.status_code == 400
    err = r.json()["error"]
    assert set(err) == {"code", "message", "hint"} and err["code"] == "invalid_speed"
    r = client.post("/clock/speed", json={})
    assert r.status_code == 400 and r.json()["error"]["hint"]
    r = client.post("/clock/speed", content=b"not json", headers={"content-type": "application/json"})
    assert r.status_code == 400 and r.json()["error"]["code"] == "validation_error"
    r = client.post("/clock/bogus")
    assert r.status_code == 404 and r.json()["error"]["code"] == "unknown_clock_action"
    assert "pause" in r.json()["error"]["hint"]
    r = client.get("/events", params={"limit": "many"})
    assert r.status_code == 400 and r.json()["error"]["code"] == "validation_error"
    r = client.get("/events", params={"limit": 0})
    assert r.status_code == 400
    r = client.get("/nope")
    assert r.status_code == 404 and r.json()["error"]["code"] == "not_found"
    assert client.post("/clock/resume").json()["running"] is True
    assert client.post("/clock/pause").json()["running"] is False
    assert client.post("/clock/pause").json()["running"] is False  # idempotent


def test_step_is_idempotent_with_idempotency_key(client: TestClient) -> None:
    h = {"Idempotency-Key": "demo-step-1"}
    first = client.post("/clock/step", headers=h)
    second = client.post("/clock/step", headers=h)
    assert first.status_code == second.status_code == 200
    assert first.json() == second.json()
    assert client.get("/state").json()["day_index"] == 1
    third = client.post("/clock/step", headers={"Idempotency-Key": "demo-step-2"})
    assert third.json()["from"] == first.json()["to"]
    assert client.get("/state").json()["day_index"] == 2


def test_events_filter_by_type(client: TestClient) -> None:
    client.post("/clock/step")
    r = client.get("/events", params={"type": "day_boundary", "limit": 10})
    assert r.status_code == 200
    evs = r.json()["events"]
    assert evs and all(e["type"] == "day_boundary" for e in evs)
    assert r.json()["latest_seq"] >= evs[-1]["seq"]
