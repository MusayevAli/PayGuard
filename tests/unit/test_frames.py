"""Frame codec (proto/FRAME.md): round trips for every type, CRC, malformed inputs, responses."""

from __future__ import annotations

import hashlib
from datetime import date

import pytest

from signer.frames import (
    FRAME_LEN,
    REASON_CODES,
    REASONS,
    SLOT_BUTTON_THRESHOLD,
    SLOT_DAILY_CAP,
    SLOT_KEY,
    SLOT_NONCE_TIMEOUT,
    DayTickFrame,
    FrameError,
    NonceQueryFrame,
    PayFrame,
    ProvisionFrame,
    StatusFrame,
    corrupt_crc,
    crc16,
    day_index,
    decode,
    decode_response,
    encode,
    encode_ok,
    encode_reject,
    hmac_tag,
    pack_status,
    unpack_status,
    verify_tag,
)

HASH = hashlib.sha256(b"DE44500105175407324931").digest()
KEY = bytes(range(32))


def test_crc16_ccitt_false_check_value() -> None:
    assert crc16(b"123456789") == 0x29B1  # the published check value for CRC-16/CCITT-FALSE
    assert crc16(b"") == 0xFFFF


def test_day_index_epoch_and_clamp() -> None:
    assert day_index(date(2026, 1, 1)) == 0
    assert day_index(date(2026, 10, 1)) == 273
    assert day_index(date(2016, 11, 21)) == 0  # before the epoch clamps to 0 (seeded accounts)


@pytest.mark.parametrize(
    "frame",
    [
        PayFrame(
            seq=1,
            account_hash=HASH,
            amount_minor=1_112_000,
            currency="EUR",
            nonce=0,
            pay_id="PAY-000001",
        ),
        PayFrame(
            seq=0xFFFFFFFF,
            account_hash=HASH,
            amount_minor=2**64 - 1,
            currency="JPY",
            nonce=0xDEADBEEF,
            pay_id="X",
        ),
        ProvisionFrame(slot=0, account_hash=HASH, account_cap=250_000_000, hold_until_day=273),
        ProvisionFrame(slot=15, account_hash=HASH, account_cap=0, hold_until_day=0xFFFF),
        ProvisionFrame(slot=SLOT_DAILY_CAP, daily_cap=300_000_000),
        ProvisionFrame(slot=SLOT_BUTTON_THRESHOLD, button_threshold=5_000_000),
        ProvisionFrame(slot=SLOT_NONCE_TIMEOUT, nonce_timeout_days=2),
        ProvisionFrame(slot=SLOT_KEY, key=KEY),
        DayTickFrame(day=273),
        NonceQueryFrame(),
        StatusFrame(),
    ],
)
def test_encode_decode_round_trip(frame: object) -> None:
    raw = encode(frame)  # type: ignore[arg-type]
    assert len(raw) == FRAME_LEN
    assert int.from_bytes(raw[62:64], "big") == crc16(raw[:62])
    assert decode(raw) == frame


def test_pay_layout_is_big_endian_and_space_padded() -> None:
    raw = encode(
        PayFrame(
            seq=0x01020304,
            account_hash=HASH,
            amount_minor=0x0102030405060708,
            currency="CZK",
            nonce=0x0A0B0C0D,
            pay_id="PAY-1",
        )
    )
    assert raw[0] == 0x01
    assert raw[1:5] == bytes([1, 2, 3, 4])
    assert raw[5:37] == HASH
    assert raw[37:45] == bytes([1, 2, 3, 4, 5, 6, 7, 8])
    assert raw[45] == 0x03
    assert raw[46:50] == bytes([0x0A, 0x0B, 0x0C, 0x0D])
    assert raw[50:62] == b"PAY-1       "


def test_bad_crc_is_detected_after_length() -> None:
    raw = encode(DayTickFrame(day=1))
    with pytest.raises(FrameError) as exc:
        decode(corrupt_crc(raw))
    assert exc.value.reason == "bad_crc" and exc.value.reason_code == 0x0A


@pytest.mark.parametrize(
    ("mutate", "detail"),
    [
        (lambda raw: raw[:63], "short"),
        (lambda raw: raw + b"\x00", "long"),
        (lambda raw: _recrc(bytes([0x07]) + raw[1:62]), "unknown type"),
        (lambda raw: _recrc(raw[:45] + bytes([0x09]) + raw[46:62]), "unknown currency"),
        (lambda raw: _recrc(raw[:50] + b"\x00" * 12), "non-ascii pay id"),
        (lambda raw: _recrc(raw[:50] + b" " * 12), "empty pay id"),
    ],
)
def test_malformed_frames(mutate, detail: str) -> None:
    raw = encode(
        PayFrame(seq=1, account_hash=HASH, amount_minor=1, currency="EUR", pay_id="PAY-000001")
    )
    with pytest.raises(FrameError) as exc:
        decode(mutate(raw))
    assert exc.value.reason == "malformed", detail


def test_malformed_provision_slot() -> None:
    raw = bytearray(
        encode(ProvisionFrame(slot=0, account_hash=HASH, account_cap=1, hold_until_day=0))
    )
    raw[1] = 0x10
    with pytest.raises(FrameError) as exc:
        decode(_recrc(bytes(raw[:62])))
    assert exc.value.reason == "malformed"


def test_encode_rejects_out_of_range_fields() -> None:
    with pytest.raises(ValueError):
        encode(PayFrame(seq=2**32, account_hash=HASH, amount_minor=1, currency="EUR", pay_id="P"))
    with pytest.raises(ValueError):
        encode(PayFrame(seq=1, account_hash=HASH, amount_minor=1, currency="GBP", pay_id="P"))
    with pytest.raises(ValueError):
        encode(
            PayFrame(
                seq=1, account_hash=HASH, amount_minor=1, currency="EUR", pay_id="thirteen-chars"
            )
        )
    with pytest.raises(ValueError):
        encode(PayFrame(seq=1, account_hash=HASH, amount_minor=1, currency="EUR", pay_id="payé"))
    with pytest.raises(ValueError):
        encode(ProvisionFrame(slot=16, account_hash=HASH, account_cap=1, hold_until_day=0))
    with pytest.raises(ValueError):
        encode(ProvisionFrame(slot=SLOT_KEY, key=b"short"))


def test_reason_table_matches_the_spec() -> None:
    assert REASONS == {
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
    assert REASON_CODES["bad_crc"] == 0x0A


def test_responses_round_trip() -> None:
    tag = hmac_tag(KEY, encode(StatusFrame()))
    assert len(tag) == 32
    assert decode_response(encode_ok(tag)) == (True, tag, None)
    assert decode_response(encode_reject("needs_button")) == (False, b"", "needs_button")
    assert encode_reject("in_hold") == b"\xff\x03"
    for bad in (b"", b"\xff", b"\xff\x55", b"\x01\x00"):
        with pytest.raises(FrameError):
            decode_response(bad)


def test_status_payload_round_trip() -> None:
    payload = pack_status(
        seq=7,
        day=273,
        daily_cap=300_000_000,
        daily_spent=12,
        button_threshold=5_000_000,
        nonce_valid=True,
        slots_used=14,
        key_loaded=True,
        nonce_timeout_days=1,
    )
    assert unpack_status(payload) == {
        "seq": 7,
        "day": 273,
        "daily_cap": 300_000_000,
        "daily_spent": 12,
        "button_threshold": 5_000_000,
        "nonce_valid": True,
        "slots_used": 14,
        "key_loaded": True,
        "nonce_timeout_days": 1,
    }


def test_hmac_tag_covers_bytes_0_to_61_only() -> None:
    frame = encode(
        PayFrame(seq=1, account_hash=HASH, amount_minor=5, currency="EUR", pay_id="PAY-000001")
    )
    tag = hmac_tag(KEY, frame)
    assert verify_tag(KEY, frame, tag)
    assert verify_tag(KEY, corrupt_crc(frame), tag), "the CRC bytes are not part of the tag"
    assert not verify_tag(bytes(32), frame, tag)
    tampered = frame[:37] + (6).to_bytes(8, "big") + frame[45:]
    assert not verify_tag(KEY, tampered, tag)


def _recrc(body: bytes) -> bytes:
    return body + crc16(body).to_bytes(2, "big")
