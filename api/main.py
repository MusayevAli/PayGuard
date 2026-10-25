"""FastAPI application (spec §13, Part 1 subset): clock control, event query, live stream."""

from __future__ import annotations

import json
import os
import threading
from collections.abc import Callable
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import structlog
from fastapi import FastAPI, Query, Request, WebSocket
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from api.schemas import SpeedIn
from api.ws import LiveHub, live_endpoint
from core.events import EventStore
from sim.clock import DEFAULT_SPEED, SimClock
from sim.models import Event

SIM_START = datetime(2026, 10, 1, tzinfo=UTC)
CLOCK_ACTIONS = ("pause", "resume", "step", "speed")
log = structlog.get_logger("api")


def error_response(status: int, code: str, message: str, hint: str) -> JSONResponse:
    return JSONResponse(
        status_code=status, content={"error": {"code": code, "message": message, "hint": hint}}
    )


class ApiError(Exception):
    def __init__(self, status: int, code: str, message: str, hint: str) -> None:
        super().__init__(message)
        self.status, self.code, self.message, self.hint = status, code, message, hint


def load_world_events(path: Path) -> list[Event]:
    events: list[Event] = []
    with path.open(encoding="utf-8") as fh:
        for line in fh:
            if line.strip():
                events.append(Event.model_validate(json.loads(line)))
    return events


def _iso(dt: datetime | None) -> str | None:
    return None if dt is None else dt.astimezone(UTC).isoformat().replace("+00:00", "Z")


class IdempotencyCache:
    """Replays the stored response for a repeated ``Idempotency-Key`` on the same route."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._seen: dict[tuple[str, str], tuple[int, Any]] = {}

    def run(self, request: Request, compute: Callable[[], tuple[int, Any]]) -> tuple[int, Any]:
        key = request.headers.get("Idempotency-Key")
        if not key:
            return compute()
        ident = (request.url.path, key)
        with self._lock:
            if ident in self._seen:
                return self._seen[ident]
            result = compute()
            self._seen[ident] = result
            return result


def create_app(
    *,
    world_dir: Path,
    db_path: Path | str,
    speed: float = DEFAULT_SPEED,
    autostart: bool = False,
    ping_interval: float = 15.0,
    start: datetime = SIM_START,
    llm: Any | None = None,
    provision: bool = True,
    desk: bool = True,
) -> FastAPI:
    signer_kind = os.environ.get("SIGNER", "stub")
    if signer_kind != "stub":
        raise RuntimeError(
            f"SIGNER={signer_kind!r}: the hardware client arrives in Part 7; only the stub signer exists"
        )
    store = EventStore(db_path)
    world_events = load_world_events(Path(world_dir) / "world.jsonl")
    clock = SimClock(store, world_events, start=start, speed=speed)
    hub = LiveHub(store)
    idem = IdempotencyCache()
    from agents.runtime import Runtime, build_runtime, start_runtime
    from signer.provisioning import DEMO_VENDORS

    runtime: Runtime | None = None
    if desk:
        out_dir = Path(db_path).resolve().parent / "runtime"
        runtime = build_runtime(
            world_dir=Path(world_dir),
            db_path=Path(db_path),
            store=store,
            clock=clock,
            out_dir=out_dir,
            world_events=world_events,
            llm=llm,
        )
        start_runtime(runtime, provision=DEMO_VENDORS if provision else None)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        clock.start_loop()
        if autostart:
            clock.resume()
        log.info(
            "api_started",
            world_dir=str(world_dir),
            db=str(db_path),
            world_events=len(world_events),
            autostart=autostart,
        )
        try:
            yield
        finally:
            clock.stop_loop()
            hub.close()
            if runtime is not None:
                runtime.close()
            else:
                store.close()

    app = FastAPI(title="PayGuard", version="0.1.0", lifespan=lifespan)
    app.state.store, app.state.clock, app.state.hub = store, clock, hub

    @app.exception_handler(ApiError)
    async def _api_error(_: Request, exc: ApiError) -> JSONResponse:
        return error_response(exc.status, exc.code, exc.message, exc.hint)

    @app.exception_handler(RequestValidationError)
    async def _validation(_: Request, exc: RequestValidationError) -> JSONResponse:
        first = exc.errors()[0] if exc.errors() else {}
        loc = ".".join(str(p) for p in first.get("loc", []) if p != "body")
        return error_response(
            400,
            "validation_error",
            f"invalid request: {loc or 'body'}: {first.get('msg', 'invalid')}",
            "fix the named field; see GET /docs for the schema",
        )

    @app.exception_handler(StarletteHTTPException)
    async def _http(_: Request, exc: StarletteHTTPException) -> JSONResponse:
        if exc.status_code == 404:
            return error_response(
                404, "not_found", "no such route", "see GET /docs for the available routes"
            )
        if exc.status_code == 405:
            return error_response(
                405, "method_not_allowed", "method not allowed on this route", "see GET /docs"
            )
        return error_response(exc.status_code, "http_error", str(exc.detail), "see GET /docs")

    def state_body() -> dict[str, Any]:
        st = clock.state()
        return {
            "sim_now": _iso(st.sim_now),
            "running": st.running,
            "speed": st.speed,
            "start": _iso(st.start),
            "day_index": st.day_index,
            "pending_world_events": st.pending_world_events,
            "next_known_at": _iso(st.next_known_at),
            "latest_seq": st.latest_seq,
            "events_total": store.count(),
        }

    @app.get("/state")
    async def get_state() -> dict[str, Any]:
        return state_body()

    @app.post("/clock/{action}")
    async def clock_action(action: str, request: Request) -> JSONResponse:
        if action not in CLOCK_ACTIONS:
            raise ApiError(
                404,
                "unknown_clock_action",
                f"unknown clock action {action!r}",
                "use one of: " + ", ".join(CLOCK_ACTIONS),
            )
        body: dict[str, Any] = {}
        if action == "speed":
            raw = await request.body()
            try:
                body = json.loads(raw or b"{}")
            except json.JSONDecodeError as exc:
                raise ApiError(
                    400,
                    "validation_error",
                    "body is not valid JSON",
                    'send {"speed": <sim-seconds per real-second>}',
                ) from exc
            try:
                speed_in = SpeedIn.model_validate(body)
            except ValueError as exc:
                raise ApiError(
                    400,
                    "validation_error",
                    f"invalid body: {exc.errors()[0]['msg'] if hasattr(exc, 'errors') else exc}",  # type: ignore[attr-defined]
                    'send {"speed": <sim-seconds per real-second, > 0>}',
                ) from exc

        def compute() -> tuple[int, Any]:
            if action == "pause":
                clock.pause()
                log.info("clock_paused", sim_now=_iso(clock.sim_now))
                return 200, state_body()
            if action == "resume":
                clock.resume()
                log.info("clock_resumed", sim_now=_iso(clock.sim_now), speed=clock.speed)
                return 200, state_body()
            if action == "speed":
                try:
                    clock.set_speed(speed_in.speed)
                except ValueError as exc:
                    raise ApiError(
                        400,
                        "invalid_speed",
                        str(exc),
                        "send a positive finite number of sim-seconds per real-second, e.g. 8640",
                    ) from exc
                log.info("clock_speed", speed=clock.speed)
                return 200, state_body()
            result = clock.step()
            seqs = [e.seq for e in result.inserted if e.seq is not None]
            log.info(
                "clock_step",
                from_=_iso(result.start),
                to=_iso(result.end),
                inserted=len(result.inserted),
            )
            return 200, {
                "from": _iso(result.start),
                "to": _iso(result.end),
                "inserted": len(result.inserted),
                "first_seq": min(seqs) if seqs else None,
                "last_seq": max(seqs) if seqs else None,
            }

        status, payload = idem.run(request, compute)
        return JSONResponse(status_code=status, content=payload)

    @app.get("/events")
    async def get_events(
        type: str | None = Query(default=None, alias="type"),
        since_seq: int = Query(default=0, ge=0),
        limit: int = Query(default=200, ge=1, le=1000),
    ) -> dict[str, Any]:
        events = store.query(type=type, since_seq=since_seq, limit=limit)
        return {
            "events": [e.model_dump(mode="json") for e in events],
            "latest_seq": store.latest_seq(),
        }

    @app.websocket("/live")
    async def live(ws: WebSocket) -> None:
        await live_endpoint(ws, store=store, clock=clock, hub=hub, ping_interval=ping_interval)

    if runtime is None:
        return app
    desk_rt: Runtime = runtime

    @app.get("/chip/status")
    async def chip_status() -> dict[str, Any]:
        return {
            **desk_rt.signer.status().to_json(),
            "slots_host": desk_rt.provisioner.slots(),
            "provisioning_queue": desk_rt.provisioner.queued(),
        }

    @app.post("/stub/button")
    async def stub_button(request: Request) -> JSONResponse:
        if signer_kind != "stub" or not hasattr(desk_rt.signer, "press_button"):
            raise ApiError(
                404,
                "not_found",
                "the button is physical on the hardware signer",
                "press the button on the device; this route exists only with SIGNER=stub",
            )

        def compute() -> tuple[int, Any]:
            nonce = desk_rt.signer.press_button()  # type: ignore[attr-defined]
            resent = 0
            if desk_rt.payment_agent is not None:
                agent = desk_rt.payment_agent
                resent = desk_rt.runner.run_task(agent, agent.retry_waiting) or 0
            log.info("button_pressed", nonce=nonce, resent=resent)
            return 200, {
                "nonce": nonce,
                "nonce_valid": desk_rt.signer.nonce() is not None,
                "payments_resent": resent,
            }

        status, payload = idem.run(request, compute)
        return JSONResponse(status_code=status, content=payload)

    @app.get("/payables")
    async def payables(status: str | None = Query(default=None)) -> dict[str, Any]:
        valid = ("open", "scheduled", "paid", "held", "rejected", "netted")
        if status is not None and status not in valid:
            raise ApiError(
                400,
                "validation_error",
                f"unknown payable status {status!r}",
                "use one of: " + ", ".join(valid),
            )
        rows = desk_rt.ledger.payables(status=status)  # type: ignore[arg-type]
        return {"payables": [p.model_dump(mode="json") for p in rows], "count": len(rows)}

    @app.get("/bank/accounts")
    async def bank_accounts() -> dict[str, Any]:
        return {"accounts": desk_rt.bank.accounts()}

    return app


def _app_from_env() -> FastAPI:
    world_dir = Path(os.environ.get("PAYGUARD_WORLD_DIR", "sim/world"))
    db_path = os.environ.get("PAYGUARD_DB", "payguard.db")
    speed = float(os.environ.get("PAYGUARD_SPEED", str(DEFAULT_SPEED)))
    autostart = os.environ.get("PAYGUARD_AUTOSTART", "0") == "1"
    return create_app(world_dir=world_dir, db_path=db_path, speed=speed, autostart=autostart)


if (
    Path(os.environ.get("PAYGUARD_WORLD_DIR", "sim/world")).joinpath("world.jsonl").exists()
    or "PAYGUARD_DB" in os.environ
):
    app = _app_from_env()
else:  # pragma: no cover - import without a generated world (e.g. tooling); `make world` first
    app = FastAPI(title="PayGuard (no world generated)")

    @app.get("/state")
    async def _no_world() -> JSONResponse:
        return error_response(
            503, "no_world", "no world generated", "run `make world` first, then `make run`"
        )


__all__ = ["app", "create_app", "load_world_events"]
