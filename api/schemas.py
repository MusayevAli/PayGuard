"""API bodies and responses (spec §13). Event payload models live in sim/models.py and are
re-exported here so API code has a single import point."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from sim.models import (  # noqa: F401 - re-exported
    PAYLOAD_MODELS,
    AgentAction,
    BankStatementLinePayload,
    BankStatementPayload,
    ChipResponse,
    ColleagueReply,
    ControllerDecision,
    DayBoundary,
    Escalation,
    Event,
    GoodsReceived,
    InvoiceReceived,
    JudgeInjection,
    PaymentExecuted,
    PaymentRequested,
    POAmended,
    POCreated,
    Provisioning,
    SystemError,
    VendorMessage,
)


class SpeedIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    speed: float = Field(description="sim-seconds per real-second")


class ClockStateOut(BaseModel):
    sim_now: str
    running: bool
    speed: float
    start: str
    day_index: int
    pending_world_events: int
    next_known_at: str | None
    latest_seq: int
    events_total: int


class StepOut(BaseModel):
    from_: str = Field(alias="from")
    to: str
    inserted: int
    first_seq: int | None
    last_seq: int | None
    model_config = ConfigDict(populate_by_name=True)


class EventsOut(BaseModel):
    events: list[Event]
    latest_seq: int


class ErrorBody(BaseModel):
    code: str
    message: str
    hint: str


class ErrorOut(BaseModel):
    error: ErrorBody
