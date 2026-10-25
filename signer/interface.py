"""The signer boundary (spec §10.1): the stub and the hardware client implement ``Signer``."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal, Protocol

SignerKind = Literal["stub", "hw"]


@dataclass(frozen=True)
class SignResult:
    ok: bool
    raw: bytes
    tag: bytes | None = None
    reason: str | None = None
    reason_code: int | None = None
    payload: bytes = b""
    latency_ms: int = 0
    authorization: dict[str, object] | None = None


@dataclass(frozen=True)
class SlotInfo:
    slot: int
    account_hash_hex: str
    cap_minor: int
    hold_until_day: int


@dataclass(frozen=True)
class ChipStatus:
    kind: SignerKind
    seq: int
    day: int
    daily_cap_minor: int
    daily_spent_minor: int
    button_threshold_minor: int
    nonce_valid: bool
    slots_used: int
    key_loaded: bool
    nonce_timeout_days: int
    slots: list[SlotInfo] = field(default_factory=list)
    hardware: dict[str, object] | None = None

    def to_json(self) -> dict[str, object]:
        result: dict[str, object] = {
            "kind": self.kind,
            "seq": self.seq,
            "day": self.day,
            "daily_cap_minor": self.daily_cap_minor,
            "daily_spent_minor": self.daily_spent_minor,
            "button_threshold_minor": self.button_threshold_minor,
            "nonce_valid": self.nonce_valid,
            "slots_used": self.slots_used,
            "key_loaded": self.key_loaded,
            "nonce_timeout_days": self.nonce_timeout_days,
            "slots": [
                {
                    "slot": s.slot,
                    "account_hash_prefix": s.account_hash_hex[:12],
                    "cap_minor": s.cap_minor,
                    "hold_until_day": s.hold_until_day,
                }
                for s in self.slots
            ],
        }
        if self.hardware is not None:
            result["hardware"] = self.hardware
        return result


class Signer(Protocol):
    kind: SignerKind

    def sign(self, frame: bytes) -> SignResult: ...

    def nonce(self) -> int | None: ...

    def tick_day(self, day: int) -> None: ...

    def status(self) -> ChipStatus: ...
