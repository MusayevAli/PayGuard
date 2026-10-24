"""WebSocket fan-out for ``/live``: every stored event in seq order, server pings every
``ping_interval`` seconds, and reconnect-safe catch-up from ``?since_seq=N``."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field

from starlette.websockets import WebSocket, WebSocketDisconnect

from core.events import EventStore
from sim.clock import SimClock
from sim.models import Event

CATCH_UP_PAGE = 500


@dataclass(eq=False)
class Client:
    loop: asyncio.AbstractEventLoop
    queue: asyncio.Queue[Event] = field(default_factory=asyncio.Queue)


class LiveHub:
    """Bridges synchronous store notifications (possibly from the clock thread) into each
    connected client's asyncio queue."""

    def __init__(self, store: EventStore) -> None:
        self._clients: set[Client] = set()
        self._unsubscribe = store.subscribe(self._on_event)

    def _on_event(self, ev: Event) -> None:
        for client in list(self._clients):
            client.loop.call_soon_threadsafe(client.queue.put_nowait, ev)

    def register(self) -> Client:
        client = Client(loop=asyncio.get_running_loop())
        self._clients.add(client)
        return client

    def unregister(self, client: Client) -> None:
        self._clients.discard(client)

    def close(self) -> None:
        self._unsubscribe()


def _iso(dt) -> str:
    return dt.isoformat().replace("+00:00", "Z")


def event_message(ev: Event) -> dict:
    return {"kind": "event", "event": ev.model_dump(mode="json")}


async def live_endpoint(
    ws: WebSocket, *, store: EventStore, clock: SimClock, hub: LiveHub, ping_interval: float
) -> None:
    await ws.accept()
    raw_since = ws.query_params.get("since_seq", "0")
    try:
        since = max(0, int(raw_since))
    except ValueError:
        await ws.send_json(
            {
                "kind": "error",
                "error": {
                    "code": "validation_error",
                    "message": f"since_seq must be an integer, got {raw_since!r}",
                    "hint": "connect with /live?since_seq=<last seq you processed>",
                },
            }
        )
        await ws.close(code=1008)
        return
    client = hub.register()
    closed = asyncio.Event()

    async def watch_client() -> None:
        try:
            while True:
                await ws.receive_text()
        except (WebSocketDisconnect, RuntimeError):
            closed.set()

    watcher = asyncio.create_task(watch_client())
    try:
        await ws.send_json(
            {
                "kind": "hello",
                "latest_seq": store.latest_seq(),
                "sim_now": _iso(clock.sim_now),
                "since_seq": since,
            }
        )
        last = since
        while True:
            page = store.query(since_seq=last, limit=CATCH_UP_PAGE)
            if not page:
                break
            for ev in page:
                await ws.send_json(event_message(ev))
                last = ev.seq or last
        while not closed.is_set():
            try:
                ev = await asyncio.wait_for(client.queue.get(), timeout=ping_interval)
            except TimeoutError:
                await ws.send_json(
                    {
                        "kind": "ping",
                        "latest_seq": store.latest_seq(),
                        "sim_now": _iso(clock.sim_now),
                    }
                )
                continue
            seq = ev.seq or 0
            if seq <= last:
                continue
            if seq > last + 1:  # a gap: replay from the store so order and completeness hold
                for missed in store.query(since_seq=last, limit=seq - last - 1):
                    await ws.send_json(event_message(missed))
                    last = missed.seq or last
            await ws.send_json(event_message(ev))
            last = seq
    except (WebSocketDisconnect, RuntimeError):
        pass
    finally:
        hub.unregister(client)
        watcher.cancel()
