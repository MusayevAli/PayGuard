"""Reference policy engine (spec §10.3, §10.5): the same state and check order the hardware must
implement. Optional SQLite persistence so an API restart does not forget the allowlist."""

from __future__ import annotations

import json
import random
import sqlite3
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

import structlog

from core.policy import jumper_present as default_jumper
from core.rules import BUTTON_THRESHOLD_MINOR, NONCE_TIMEOUT_DAYS
from signer.frames import (
    MAX_SLOTS,
    SLOT_BUTTON_THRESHOLD,
    SLOT_DAILY_CAP,
    SLOT_KEY,
    SLOT_NONCE_TIMEOUT,
    DayTickFrame,
    Frame,
    FrameError,
    NonceQueryFrame,
    PayFrame,
    ProvisionFrame,
    decode,
    encode,
    encode_ok,
    encode_reject,
    hmac_tag,
    pack_day_tick,
    pack_nonce,
    pack_status,
)
from signer.interface import ChipStatus, SignResult, SlotInfo

log = structlog.get_logger("signer.stub")

ZERO_KEY = bytes(32)


@dataclass
class Slot:
    account_hash: bytes
    cap: int
    hold_until_day: int


class StubSigner:
    """Software signer. ``sign()`` is the only way state changes; a reject changes nothing."""

    kind: Literal["stub", "hw"] = "stub"

    def __init__(
        self,
        path: str | Path | None = None,
        *,
        jumper_present: Callable[[], bool] = default_jumper,
        nonce_seed: int = 1,
        button_threshold: int = BUTTON_THRESHOLD_MINOR,
        nonce_timeout_days: int = NONCE_TIMEOUT_DAYS,
    ) -> None:
        self._jumper = jumper_present
        self._nonce_seed = nonce_seed
        self.slots: dict[int, Slot] = {}
        self.daily_cap = 0
        self.daily_spent = 0
        self.button_threshold = button_threshold
        self.key = ZERO_KEY
        self.seq = 0
        self.nonce_value = 0
        self.nonce_valid = False
        self.nonce_day = 0
        self.nonce_timeout_days = nonce_timeout_days
        self.day = 0
        self.presses = 0
        self._con: sqlite3.Connection | None = None
        if path is not None:
            self._con = sqlite3.connect(str(path), check_same_thread=False, isolation_level=None)
            self._con.execute("PRAGMA journal_mode=WAL")
            self._con.execute(
                "CREATE TABLE IF NOT EXISTS signer_stub_state (id INTEGER PRIMARY KEY CHECK (id = 1), state TEXT NOT NULL)"
            )
            self._load()

    # ------------------------------------------------------------------ persistence

    def _load(self) -> None:
        assert self._con is not None
        row = self._con.execute("SELECT state FROM signer_stub_state WHERE id = 1").fetchone()
        if row is None:
            return
        state = json.loads(row[0])
        self.slots = {
            int(k): Slot(bytes.fromhex(v["hash"]), int(v["cap"]), int(v["hold"]))
            for k, v in state["slots"].items()
        }
        self.daily_cap = int(state["daily_cap"])
        self.daily_spent = int(state["daily_spent"])
        self.button_threshold = int(state["button_threshold"])
        self.key = bytes.fromhex(state["key"])
        self.seq = int(state["seq"])
        self.nonce_value = int(state["nonce"])
        self.nonce_valid = bool(state["nonce_valid"])
        self.nonce_day = int(state["nonce_day"])
        self.nonce_timeout_days = int(state["nonce_timeout_days"])
        self.day = int(state["day"])
        self.presses = int(state["presses"])

    def _save(self) -> None:
        if self._con is None:
            return
        state = {
            "slots": {
                str(k): {"hash": s.account_hash.hex(), "cap": s.cap, "hold": s.hold_until_day}
                for k, s in self.slots.items()
            },
            "daily_cap": self.daily_cap,
            "daily_spent": self.daily_spent,
            "button_threshold": self.button_threshold,
            "key": self.key.hex(),
            "seq": self.seq,
            "nonce": self.nonce_value,
            "nonce_valid": self.nonce_valid,
            "nonce_day": self.nonce_day,
            "nonce_timeout_days": self.nonce_timeout_days,
            "day": self.day,
            "presses": self.presses,
        }
        self._con.execute(
            "INSERT INTO signer_stub_state(id, state) VALUES (1, ?) ON CONFLICT(id) DO UPDATE SET state = excluded.state",
            (json.dumps(state),),
        )

    def close(self) -> None:
        if self._con is not None:
            self._con.close()
            self._con = None

    # ------------------------------------------------------------------ Signer protocol

    def sign(self, frame: bytes) -> SignResult:
        started = time.perf_counter()
        try:
            decoded = decode(frame)
        except FrameError as exc:
            return self._reject(exc.reason, started)
        if isinstance(decoded, PayFrame):
            result = self._pay(frame, decoded, started)
        elif isinstance(decoded, ProvisionFrame):
            result = self._provision(decoded, started)
        elif isinstance(decoded, DayTickFrame):
            result = self._day_tick(decoded, started)
        elif isinstance(decoded, NonceQueryFrame):
            result = self._ok(pack_nonce(self.nonce_value, self.nonce_valid), started)
        else:
            result = self._ok(self._status_payload(), started)
        if result.ok:
            self._save()
        return result

    def nonce(self) -> int | None:
        return self.nonce_value if self.nonce_valid else None

    def tick_day(self, day: int) -> None:
        result = self.sign(encode(DayTickFrame(day=day)))
        if not result.ok:
            raise RuntimeError(f"day tick rejected: {result.reason}")

    def status(self) -> ChipStatus:
        return ChipStatus(
            kind="stub",
            seq=self.seq,
            day=self.day,
            daily_cap_minor=self.daily_cap,
            daily_spent_minor=self.daily_spent,
            button_threshold_minor=self.button_threshold,
            nonce_valid=self.nonce_valid,
            slots_used=len(self.slots),
            key_loaded=self.key != ZERO_KEY,
            nonce_timeout_days=self.nonce_timeout_days,
            slots=[
                SlotInfo(
                    slot=k,
                    account_hash_hex=s.account_hash.hex(),
                    cap_minor=s.cap,
                    hold_until_day=s.hold_until_day,
                )
                for k, s in sorted(self.slots.items())
            ],
        )

    # ------------------------------------------------------------------ stub-only operations

    def press_button(self) -> int:
        """Load a fresh one-shot nonce (the physical button on hardware)."""
        self.presses += 1
        value = random.Random(f"{self._nonce_seed}:{self.presses}").getrandbits(32) or 1
        self.nonce_value = value
        self.nonce_valid = True
        self.nonce_day = self.day
        self._save()
        log.info(
            "button_pressed",
            nonce=value,
            day=self.day,
            event_id=None,
            principal="signer",
            rule_id="R10",
        )
        return value

    def export_state(self) -> dict[str, Any]:
        return {
            "seq": self.seq,
            "day": self.day,
            "daily_spent": self.daily_spent,
            "nonce": self.nonce_value,
            "nonce_valid": self.nonce_valid,
            "slots_used": len(self.slots),
            "key_loaded": self.key != ZERO_KEY,
        }

    def slot_of(self, account_hash: bytes) -> int | None:
        for k, s in self.slots.items():
            if s.account_hash == account_hash:
                return k
        return None

    # ------------------------------------------------------------------ checks (order per proto/FRAME.md)

    def _pay(self, frame: bytes, pay: PayFrame, started: float) -> SignResult:
        if pay.seq != self.seq + 1:
            return self._reject("bad_seq", started)
        slot = self.slot_of(pay.account_hash)
        if slot is None:
            return self._reject("not_allowlisted", started)
        entry = self.slots[slot]
        if self.day < entry.hold_until_day:
            return self._reject("in_hold", started)
        if pay.amount_minor > entry.cap:
            return self._reject("over_account_cap", started)
        if self.daily_spent + pay.amount_minor > self.daily_cap:
            return self._reject("over_daily_cap", started)
        if pay.amount_minor > self.button_threshold:
            if pay.nonce == 0:
                return self._reject("needs_button", started)
            if pay.nonce != self.nonce_value:
                return self._reject("bad_nonce", started)
            if not self.nonce_valid:
                return self._reject("nonce_used", started)
        # commit
        self.seq += 1
        self.daily_spent += pay.amount_minor
        self.nonce_valid = False
        tag = hmac_tag(self.key, frame)
        log.info(
            "payment_signed",
            pay_id=pay.pay_id,
            seq=self.seq,
            amount_eur_minor=pay.amount_minor,
            currency=pay.currency,
            slot=slot,
            event_id=None,
            principal="signer",
            rule_id=None,
        )
        return SignResult(
            ok=True, raw=encode_ok(tag), tag=tag, payload=tag, latency_ms=self._ms(started)
        )

    def _provision(self, frame: ProvisionFrame, started: float) -> SignResult:
        if not self._jumper():
            return self._reject("provisioning_locked", started)
        if frame.slot < MAX_SLOTS:
            assert (
                frame.account_hash is not None
                and frame.account_cap is not None
                and frame.hold_until_day is not None
            )
            self.slots[frame.slot] = Slot(
                frame.account_hash, frame.account_cap, frame.hold_until_day
            )
        elif frame.slot == SLOT_DAILY_CAP:
            assert frame.daily_cap is not None
            self.daily_cap = frame.daily_cap
        elif frame.slot == SLOT_BUTTON_THRESHOLD:
            assert frame.button_threshold is not None
            self.button_threshold = frame.button_threshold
        elif frame.slot == SLOT_NONCE_TIMEOUT:
            assert frame.nonce_timeout_days is not None
            self.nonce_timeout_days = frame.nonce_timeout_days
        elif frame.slot == SLOT_KEY:
            assert frame.key is not None
            self.key = frame.key
        log.info(
            "provisioned", slot=frame.slot, event_id=None, principal="provisioner", rule_id=None
        )
        return self._ok(bytes([frame.slot]), started)

    def _day_tick(self, frame: DayTickFrame, started: float) -> SignResult:
        if frame.day != self.day:
            self.daily_spent = 0
            self.day = frame.day
        if self.nonce_valid and self.day - self.nonce_day >= self.nonce_timeout_days:
            self.nonce_valid = False
        return self._ok(pack_day_tick(self.day, self.daily_spent), started)

    def _status_payload(self) -> bytes:
        return pack_status(
            seq=self.seq,
            day=self.day,
            daily_cap=self.daily_cap,
            daily_spent=self.daily_spent,
            button_threshold=self.button_threshold,
            nonce_valid=self.nonce_valid,
            slots_used=len(self.slots),
            key_loaded=self.key != ZERO_KEY,
            nonce_timeout_days=self.nonce_timeout_days,
        )

    # ------------------------------------------------------------------ helpers

    @staticmethod
    def _ms(started: float) -> int:
        return int((time.perf_counter() - started) * 1000)

    def _ok(self, payload: bytes, started: float) -> SignResult:
        return SignResult(
            ok=True, raw=encode_ok(payload), payload=payload, latency_ms=self._ms(started)
        )

    def _reject(self, reason: str, started: float) -> SignResult:
        raw = encode_reject(reason)
        log.info("frame_rejected", reason=reason, event_id=None, principal="signer", rule_id=None)
        return SignResult(
            ok=False, raw=raw, reason=reason, reason_code=raw[1], latency_ms=self._ms(started)
        )


def decode_frame(frame: bytes) -> Frame:
    """Re-exported for callers that only import the stub module."""
    return decode(frame)
