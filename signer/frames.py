"""Signer frames (``proto/FRAME.md``): 64-byte requests, responses, CRC-16/CCITT-FALSE, HMAC tags
and the reason-code table. Pure functions; no state."""

from __future__ import annotations

import hashlib
import hmac
import struct
from dataclasses import dataclass
from datetime import date
from typing import Final

FRAME_LEN: Final[int] = 64
BODY_LEN: Final[int] = 62
TYPE_PAY: Final[int] = 0x01
TYPE_PROVISION: Final[int] = 0x02
TYPE_DAY_TICK: Final[int] = 0x03
TYPE_NONCE_QUERY: Final[int] = 0x04
TYPE_STATUS: Final[int] = 0x05
FRAME_TYPES: Final[frozenset[int]] = frozenset(
    {TYPE_PAY, TYPE_PROVISION, TYPE_DAY_TICK, TYPE_NONCE_QUERY, TYPE_STATUS}
)

CURRENCY_CODES: Final[dict[str, int]] = {
    "EUR": 0x01,
    "PLN": 0x02,
    "CZK": 0x03,
    "JPY": 0x04,
    "USD": 0x05,
}
CODE_TO_CURRENCY: Final[dict[int, str]] = {v: k for k, v in CURRENCY_CODES.items()}

MAX_SLOTS: Final[int] = 64
SLOT_NONCE_TIMEOUT: Final[int] = 0xFC
SLOT_BUTTON_THRESHOLD: Final[int] = 0xFD
SLOT_DAILY_CAP: Final[int] = 0xFE
SLOT_KEY: Final[int] = 0xFF

RESP_OK: Final[int] = 0x00
RESP_REJECT: Final[int] = 0xFF

REASONS: Final[dict[int, str]] = {
    0x01: "bad_seq",
    0x02: "not_allowlisted",
    0x03: "in_hold",
    0x04: "over_account_cap",
    0x05: "over_daily_cap",
    0x06: "needs_button",
    0x07: "bad_nonce",
    0x08: "nonce_used",
    0x09: "malformed",
    0x0A: "bad_crc",
    0x0B: "provisioning_locked",
}
REASON_CODES: Final[dict[str, int]] = {v: k for k, v in REASONS.items()}

DAY_EPOCH: Final[date] = date(2026, 1, 1)
PAY_ID_LEN: Final[int] = 12
U16_MAX: Final[int] = 0xFFFF
U32_MAX: Final[int] = 0xFFFF_FFFF
U64_MAX: Final[int] = 0xFFFF_FFFF_FFFF_FFFF


class FrameError(ValueError):
    """A frame or response that cannot be decoded; ``reason`` is ``malformed`` or ``bad_crc``."""

    def __init__(self, reason: str, detail: str = "") -> None:
        self.reason = reason
        self.reason_code = REASON_CODES[reason]
        super().__init__(f"{reason}: {detail}" if detail else reason)


def crc16(data: bytes) -> int:
    """CRC-16/CCITT-FALSE: poly 0x1021, init 0xFFFF, no reflection, no final xor."""
    crc = 0xFFFF
    for byte in data:
        crc ^= byte << 8
        for _ in range(8):
            crc = ((crc << 1) ^ 0x1021) & 0xFFFF if crc & 0x8000 else (crc << 1) & 0xFFFF
    return crc


def day_index(d: date) -> int:
    """Days since 2026-01-01, clamped to the u16 range (2026-10-01 is 273)."""
    return min(max(0, (d - DAY_EPOCH).days), U16_MAX)


def date_of_day(day: int) -> date:
    return date.fromordinal(DAY_EPOCH.toordinal() + day)


@dataclass(frozen=True)
class PayFrame:
    seq: int
    account_hash: bytes
    amount_minor: int
    currency: str
    nonce: int = 0
    pay_id: str = ""


@dataclass(frozen=True)
class ProvisionFrame:
    slot: int
    account_hash: bytes | None = None
    account_cap: int | None = None
    hold_until_day: int | None = None
    daily_cap: int | None = None
    button_threshold: int | None = None
    nonce_timeout_days: int | None = None
    key: bytes | None = None


@dataclass(frozen=True)
class DayTickFrame:
    day: int


@dataclass(frozen=True)
class NonceQueryFrame:
    pass


@dataclass(frozen=True)
class StatusFrame:
    pass


Frame = PayFrame | ProvisionFrame | DayTickFrame | NonceQueryFrame | StatusFrame


def _check_range(name: str, value: int, maximum: int) -> None:
    if not 0 <= value <= maximum:
        raise ValueError(f"{name} {value} out of range 0..{maximum}")


def _finish(body: bytearray) -> bytes:
    if len(body) != BODY_LEN:
        raise ValueError("frame body must be 62 bytes")
    return bytes(body) + struct.pack(">H", crc16(bytes(body)))


def encode(frame: Frame) -> bytes:
    """Serialise a frame and append its CRC. Raises ``ValueError`` on out-of-range fields."""
    body = bytearray(BODY_LEN)
    if isinstance(frame, PayFrame):
        if len(frame.account_hash) != 32:
            raise ValueError("account_hash must be 32 bytes")
        _check_range("seq", frame.seq, U32_MAX)
        _check_range("amount_minor", frame.amount_minor, U64_MAX)
        _check_range("nonce", frame.nonce, U32_MAX)
        if frame.currency not in CURRENCY_CODES:
            raise ValueError(f"unknown currency {frame.currency!r}")
        pay_id = frame.pay_id.strip()
        if (
            not pay_id
            or len(pay_id) > PAY_ID_LEN
            or not all(0x20 <= ord(c) <= 0x7E for c in pay_id)
        ):
            raise ValueError("pay_id must be 1..12 printable ASCII characters")
        body[0] = TYPE_PAY
        body[1:5] = struct.pack(">I", frame.seq)
        body[5:37] = frame.account_hash
        body[37:45] = struct.pack(">Q", frame.amount_minor)
        body[45] = CURRENCY_CODES[frame.currency]
        body[46:50] = struct.pack(">I", frame.nonce)
        body[50:62] = pay_id.ljust(PAY_ID_LEN).encode("ascii")
    elif isinstance(frame, ProvisionFrame):
        body[0] = TYPE_PROVISION
        body[1] = frame.slot
        if 0 <= frame.slot < MAX_SLOTS:
            if (
                frame.account_hash is None
                or frame.account_cap is None
                or frame.hold_until_day is None
            ):
                raise ValueError("a slot frame needs account_hash, account_cap and hold_until_day")
            if len(frame.account_hash) != 32:
                raise ValueError("account_hash must be 32 bytes")
            _check_range("account_cap", frame.account_cap, U64_MAX)
            _check_range("hold_until_day", frame.hold_until_day, U16_MAX)
            body[2:34] = frame.account_hash
            body[34:42] = struct.pack(">Q", frame.account_cap)
            body[42:44] = struct.pack(">H", frame.hold_until_day)
        elif frame.slot == SLOT_DAILY_CAP:
            if frame.daily_cap is None:
                raise ValueError("daily cap frame needs daily_cap")
            _check_range("daily_cap", frame.daily_cap, U64_MAX)
            body[2:10] = struct.pack(">Q", frame.daily_cap)
        elif frame.slot == SLOT_BUTTON_THRESHOLD:
            if frame.button_threshold is None:
                raise ValueError("button threshold frame needs button_threshold")
            _check_range("button_threshold", frame.button_threshold, U64_MAX)
            body[2:10] = struct.pack(">Q", frame.button_threshold)
        elif frame.slot == SLOT_NONCE_TIMEOUT:
            if frame.nonce_timeout_days is None:
                raise ValueError("nonce timeout frame needs nonce_timeout_days")
            _check_range("nonce_timeout_days", frame.nonce_timeout_days, U16_MAX)
            body[2:4] = struct.pack(">H", frame.nonce_timeout_days)
        elif frame.slot == SLOT_KEY:
            if frame.key is None or len(frame.key) != 32:
                raise ValueError("key frame needs a 32-byte key")
            body[2:34] = frame.key
        else:
            raise ValueError(f"unknown provisioning slot {frame.slot:#04x}")
    elif isinstance(frame, DayTickFrame):
        _check_range("day", frame.day, U16_MAX)
        body[0] = TYPE_DAY_TICK
        body[1:3] = struct.pack(">H", frame.day)
    elif isinstance(frame, NonceQueryFrame):
        body[0] = TYPE_NONCE_QUERY
    else:
        body[0] = TYPE_STATUS
    return _finish(body)


def decode(raw: bytes) -> Frame:
    """Parse a 64-byte frame. Length, type, currency and pay_id problems are ``malformed``; a CRC
    mismatch is ``bad_crc`` (checked right after the length, before any field)."""
    if len(raw) != FRAME_LEN:
        raise FrameError("malformed", f"frame is {len(raw)} bytes, expected {FRAME_LEN}")
    (crc_given,) = struct.unpack(">H", raw[62:64])
    if crc_given != crc16(raw[:BODY_LEN]):
        raise FrameError("bad_crc", f"crc {crc_given:#06x} != {crc16(raw[:BODY_LEN]):#06x}")
    frame_type = raw[0]
    if frame_type == TYPE_PAY:
        (seq,) = struct.unpack(">I", raw[1:5])
        (amount,) = struct.unpack(">Q", raw[37:45])
        code = raw[45]
        if code not in CODE_TO_CURRENCY:
            raise FrameError("malformed", f"unknown currency code {code:#04x}")
        (nonce,) = struct.unpack(">I", raw[46:50])
        pay_raw = raw[50:62]
        if any(b < 0x20 or b > 0x7E for b in pay_raw):
            raise FrameError("malformed", "pay_id is not printable ASCII")
        pay_id = pay_raw.decode("ascii").rstrip(" ")
        if not pay_id:
            raise FrameError("malformed", "pay_id is empty")
        return PayFrame(
            seq=seq,
            account_hash=bytes(raw[5:37]),
            amount_minor=amount,
            currency=CODE_TO_CURRENCY[code],
            nonce=nonce,
            pay_id=pay_id,
        )
    if frame_type == TYPE_PROVISION:
        slot = raw[1]
        if slot < MAX_SLOTS:
            (cap,) = struct.unpack(">Q", raw[34:42])
            (hold,) = struct.unpack(">H", raw[42:44])
            return ProvisionFrame(
                slot=slot, account_hash=bytes(raw[2:34]), account_cap=cap, hold_until_day=hold
            )
        if slot == SLOT_DAILY_CAP:
            return ProvisionFrame(slot=slot, daily_cap=struct.unpack(">Q", raw[2:10])[0])
        if slot == SLOT_BUTTON_THRESHOLD:
            return ProvisionFrame(slot=slot, button_threshold=struct.unpack(">Q", raw[2:10])[0])
        if slot == SLOT_NONCE_TIMEOUT:
            return ProvisionFrame(slot=slot, nonce_timeout_days=struct.unpack(">H", raw[2:4])[0])
        if slot == SLOT_KEY:
            return ProvisionFrame(slot=slot, key=bytes(raw[2:34]))
        raise FrameError("malformed", f"unknown provisioning slot {slot:#04x}")
    if frame_type == TYPE_DAY_TICK:
        return DayTickFrame(day=struct.unpack(">H", raw[1:3])[0])
    if frame_type == TYPE_NONCE_QUERY:
        return NonceQueryFrame()
    if frame_type == TYPE_STATUS:
        return StatusFrame()
    raise FrameError("malformed", f"unknown frame type {frame_type:#04x}")


# ----------------------------------------------------------------------------- responses


def encode_ok(payload: bytes = b"") -> bytes:
    return bytes([RESP_OK]) + payload


def encode_reject(reason: str) -> bytes:
    return bytes([RESP_REJECT, REASON_CODES[reason]])


def decode_response(raw: bytes) -> tuple[bool, bytes, str | None]:
    """``(ok, payload, reason)``. Raises ``FrameError(malformed)`` for anything else."""
    if not raw:
        raise FrameError("malformed", "empty response")
    if raw[0] == RESP_OK:
        return True, bytes(raw[1:]), None
    if raw[0] == RESP_REJECT and len(raw) == 2 and raw[1] in REASONS:
        return False, b"", REASONS[raw[1]]
    raise FrameError("malformed", f"unrecognised response {raw.hex()}")


def pack_day_tick(day: int, daily_spent: int) -> bytes:
    return struct.pack(">HQ", day, daily_spent)


def unpack_day_tick(payload: bytes) -> tuple[int, int]:
    day, spent = struct.unpack(">HQ", payload)
    return int(day), int(spent)


def pack_nonce(nonce: int, valid: bool) -> bytes:
    return struct.pack(">IB", nonce, 1 if valid else 0)


def unpack_nonce(payload: bytes) -> tuple[int, bool]:
    nonce, valid = struct.unpack(">IB", payload)
    return int(nonce), bool(valid)


STATUS_FORMAT: Final[str] = ">IHQQQBBBH"


def pack_status(
    *,
    seq: int,
    day: int,
    daily_cap: int,
    daily_spent: int,
    button_threshold: int,
    nonce_valid: bool,
    slots_used: int,
    key_loaded: bool,
    nonce_timeout_days: int,
) -> bytes:
    return struct.pack(
        STATUS_FORMAT,
        seq,
        day,
        daily_cap,
        daily_spent,
        button_threshold,
        1 if nonce_valid else 0,
        slots_used,
        1 if key_loaded else 0,
        nonce_timeout_days,
    )


def unpack_status(payload: bytes) -> dict[str, int | bool]:
    seq, day, cap, spent, threshold, valid, used, loaded, timeout = struct.unpack(
        STATUS_FORMAT, payload
    )
    return {
        "seq": int(seq),
        "day": int(day),
        "daily_cap": int(cap),
        "daily_spent": int(spent),
        "button_threshold": int(threshold),
        "nonce_valid": bool(valid),
        "slots_used": int(used),
        "key_loaded": bool(loaded),
        "nonce_timeout_days": int(timeout),
    }


# ----------------------------------------------------------------------------- tags


def hmac_tag(key: bytes, frame: bytes) -> bytes:
    """HMAC-SHA256 over the frame's first 62 bytes (everything but the CRC)."""
    return hmac.new(key, frame[:BODY_LEN], hashlib.sha256).digest()


def verify_tag(key: bytes, frame: bytes, tag: bytes) -> bool:
    return hmac.compare_digest(hmac_tag(key, frame), tag)


def corrupt_crc(frame: bytes) -> bytes:
    """Flip the low CRC bit (test helper for the ``bad_crc`` path)."""
    return frame[:63] + bytes([frame[63] ^ 0x01])
