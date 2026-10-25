"""FastAPI application (spec §13, Part 1 subset): clock control, event query, live stream."""

from __future__ import annotations

import hashlib
import json
import os
import threading
from collections.abc import Callable
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

import structlog
from fastapi import FastAPI, Query, Request, WebSocket
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field
from starlette.exceptions import HTTPException as StarletteHTTPException

from api.schemas import SpeedIn
from api.ws import LiveHub, live_endpoint
from core.events import EventStore, make_event
from core.packets import render
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
        start_runtime(runtime, provision=provision)

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

    @app.get("/packets")
    async def list_packets(status: str | None = Query(default=None)) -> dict[str, Any]:
        if status is not None and status not in ("open", "decided", "expired"):
            raise ApiError(
                400,
                "validation_error",
                f"unknown packet status {status!r}",
                "use open, decided or expired",
            )
        rows = desk_rt.packets.packets(status=status)  # type: ignore[arg-type]
        rows.sort(key=lambda p: p.created_at, reverse=True)
        return {"packets": [render(p) for p in rows], "count": len(rows)}

    @app.get("/packets/{packet_id}")
    async def get_packet(packet_id: str) -> dict[str, Any]:
        packet = desk_rt.packets.get(packet_id)
        if packet is None:
            raise ApiError(404, "not_found", f"no packet {packet_id}", "GET /packets lists the ids")
        return render(packet)

    @app.post("/packets/{packet_id}/decide")
    async def decide(packet_id: str, body: DecisionIn, request: Request) -> JSONResponse:
        packet = desk_rt.packets.get(packet_id)
        if packet is None:
            raise ApiError(404, "not_found", f"no packet {packet_id}", "GET /packets lists the ids")
        option_ids = [o.id for o in packet.options]
        if body.decision not in option_ids:
            raise ApiError(
                400,
                "validation_error",
                f"{body.decision!r} is not an option of {packet_id}",
                "use one of: " + ", ".join(option_ids),
            )

        def compute() -> tuple[int, Any]:
            result = desk_rt.packets.decide_packet(
                packet_id,
                body.decision,
                body.note,
                body.decided_by,
                via="phone",
                decided_at=clock.sim_now,
            )
            desk_rt.runner.drain()
            fresh = desk_rt.packets.get(packet_id)
            return 200, {
                "decision": result.model_dump(mode="json"),
                "packet": render(fresh) if fresh else None,
            }

        status, payload = idem.run(request, compute)
        return JSONResponse(status_code=status, content=payload)

    @app.get("/vendors/{vendor_id}")
    async def get_vendor(vendor_id: str) -> dict[str, Any]:
        vendor = desk_rt.vendors.get_vendor(vendor_id)
        if vendor is None:
            raise ApiError(
                404, "not_found", f"no vendor {vendor_id}", "vendor ids look like V-0001"
            )
        accounts = desk_rt.vendors.accounts(vendor_id=vendor_id)
        return {
            "vendor": vendor.model_dump(mode="json"),
            "accounts": [_account_view(a) for a in accounts if a.status != "retired"],
            "active_account": next(
                (_account_view(a) for a in accounts if a.status == "active"), None
            ),
        }

    @app.get("/vendors/{vendor_id}/accounts/history")
    async def account_history(vendor_id: str) -> dict[str, Any]:
        if desk_rt.vendors.get_vendor(vendor_id) is None:
            raise ApiError(
                404, "not_found", f"no vendor {vendor_id}", "vendor ids look like V-0001"
            )
        rows = sorted(desk_rt.vendors.accounts(vendor_id=vendor_id), key=lambda a: a.requested_at)
        return {"vendor_id": vendor_id, "accounts": [_account_view(a) for a in rows]}

    @app.post("/judge/message")
    async def judge_message(form: MessageForm, request: Request) -> JSONResponse:
        supplier = None
        if form.supplier_id:
            supplier = next((s for s in desk_rt.suppliers if s.id == form.supplier_id), None)
            if supplier is None:
                raise ApiError(
                    400,
                    "validation_error",
                    f"unknown supplier {form.supplier_id}",
                    "use a supplier id from the world or give supplier_name",
                )
        name = form.supplier_name or (supplier.legal_name if supplier else None)
        if not name:
            raise ApiError(
                400,
                "validation_error",
                "supplier_id or supplier_name is required",
                "name the supplier the message claims to come from",
            )

        def compute() -> tuple[int, Any]:
            now = clock.sim_now
            thread = f"thr_judge_{hashlib.sha256((form.from_address + form.body).encode()).hexdigest()[:8]}"
            payload = {
                "message_id": f"<judge-{now:%Y%m%d%H%M%S}-{thread[-8:]}@{form.from_address.split('@')[-1]}>",
                "channel": form.channel,
                "vendor_id": supplier.id if (supplier and form.channel == "portal") else None,
                "claimed_supplier_name": name,
                "from_address": form.from_address,
                "reply_to": None,
                "to": "ap-invoices@tbeu.example",
                "subject": form.subject or f"Message from {name}",
                "body": form.body,
                "eml_path": None,
                "in_reply_to": None,
                "thread_id": thread,
                "attachments": [],
                "bank_details": {
                    "iban": form.iban,
                    "bic": None,
                    "bank_name": form.bank_name,
                    "account_number": None,
                    "routing_number": None,
                    "bank_code": None,
                    "branch_code": None,
                    "holder": name,
                }
                if form.iban
                else None,
            }
            message = store.append(
                make_event(
                    "vendor_message",
                    payload,
                    event_time=now,
                    source="judge",
                    actor=form.from_address,
                )
            ).event
            injection = store.append(
                make_event(
                    "judge_injection",
                    {
                        "kind": "message",
                        "submitted_by": form.submitted_by,
                        "form": form.model_dump(mode="json"),
                        "resulting_event_id": message.id,
                    },
                    event_time=now,
                    source="judge",
                    actor=form.submitted_by or "judge",
                )
            ).event
            desk_rt.runner.drain()
            return 200, {
                "vendor_message_id": message.id,
                "judge_injection_id": injection.id,
                "thread_id": thread,
            }

        status, payload = idem.run(request, compute)
        return JSONResponse(status_code=status, content=payload)

    return app


def _account_view(a: Any) -> dict[str, Any]:
    compact = a.iban_or_acct.replace(" ", "")
    masked = f"{compact[:4]} **** {compact[-4:]}" if len(compact) > 8 else "****"
    return {
        "id": a.id,
        "iban_masked": masked,
        "account_hash_prefix": a.account_hash[:12],
        "status": a.status,
        "requested_at": _iso(a.requested_at),
        "requested_via": a.requested_via,
        "requested_by": a.requested_by,
        "verified_via": a.verified_via,
        "verified_at": _iso(a.verified_at),
        "hold_until": _iso(a.hold_until),
        "approved_by": a.approved_by,
        "approved_at": _iso(a.approved_at),
        "on_chip": a.on_chip,
        "retired_at": _iso(a.retired_at),
    }


class DecisionIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    decision: str = Field(min_length=1, max_length=64)
    note: str | None = Field(default=None, max_length=2000)
    decided_by: str = Field(min_length=1, max_length=64)


class MessageForm(BaseModel):
    model_config = ConfigDict(extra="forbid")
    supplier_id: str | None = None
    supplier_name: str | None = Field(default=None, max_length=200)
    channel: Literal["email", "portal", "phone"]
    from_address: str = Field(min_length=3, max_length=200)
    subject: str | None = Field(default=None, max_length=300)
    body: str = Field(min_length=1, max_length=20000)
    iban: str | None = Field(default=None, max_length=40)
    bank_name: str | None = Field(default=None, max_length=120)
    submitted_by: str | None = Field(default=None, max_length=120)


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
