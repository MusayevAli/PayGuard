"""The fixed scenario behind ``tests/vectors.json`` (spec §10.3): every reason code, the happy path
per currency, day ticks, the nonce lifecycle, hold expiry on the boundary day, caps at and one
unit over the limit, provisioning with and without the jumper, a malformed frame. The hardware
testbench replays the cases in order against a freshly reset device."""

from __future__ import annotations

import hashlib
from typing import Any

from signer.frames import (
    SLOT_BUTTON_THRESHOLD,
    SLOT_DAILY_CAP,
    SLOT_KEY,
    SLOT_NONCE_TIMEOUT,
    DayTickFrame,
    Frame,
    NonceQueryFrame,
    PayFrame,
    ProvisionFrame,
    StatusFrame,
    corrupt_crc,
    crc16,
    encode,
)
from signer.stub import StubSigner

SCHEMA = "payguard-vectors-1"
KEY = bytes.fromhex("5061794775617264207369676e6572206b65792030303031207465737476656320")[:32]
NONCE_SEED = 7
DAY0 = 273  # 2026-10-01

HASH_A = hashlib.sha256(b"DE44500105175407324931").digest()
HASH_B = hashlib.sha256(b"CZ4701000000190000259317").digest()
HASH_C = hashlib.sha256(b"PL10105000997603123456789123").digest()
HASH_D = hashlib.sha256(b"DE02120300000000202051").digest()
HASH_X = hashlib.sha256(b"GB33BUKB20201555555555").digest()  # never provisioned

CAP_A = 250_000
CAP_B = 500_000
CAP_D = 2_000_000
DAILY_CAP = 600_000
THRESHOLD = 300_000


def _pay(
    seq: int,
    account_hash: bytes,
    amount: int,
    currency: str = "EUR",
    nonce: int = 0,
    pay_id: str = "PAY-000001",
) -> Frame:
    return PayFrame(
        seq=seq,
        account_hash=account_hash,
        amount_minor=amount,
        currency=currency,
        nonce=nonce,
        pay_id=pay_id,
    )


def scenario() -> list[dict[str, Any]]:
    """Ordered steps: ``frame`` (a Frame or raw bytes), ``jumper``, optional ``press_button``."""
    steps: list[dict[str, Any]] = []

    def step(
        id_: str,
        title: str,
        frame: Frame | bytes,
        *,
        jumper: bool = False,
        press_button: bool = False,
    ) -> None:
        steps.append(
            {
                "id": id_,
                "title": title,
                "frame": frame,
                "jumper": jumper,
                "press_button": press_button,
            }
        )

    step("status_fresh", "status of a freshly reset chip", StatusFrame())
    step(
        "key_without_jumper",
        "key load refused while the jumper is absent",
        ProvisionFrame(slot=SLOT_KEY, key=KEY),
    )
    step(
        "key_with_jumper",
        "key load accepted with the jumper",
        ProvisionFrame(slot=SLOT_KEY, key=KEY),
        jumper=True,
    )
    step(
        "prov_slot0",
        "allowlist slot 0 (A), cap 2,500.00, no hold",
        ProvisionFrame(slot=0, account_hash=HASH_A, account_cap=CAP_A, hold_until_day=DAY0),
        jumper=True,
    )
    step(
        "prov_slot1_hold",
        "allowlist slot 1 (B) in hold until day 280",
        ProvisionFrame(slot=1, account_hash=HASH_B, account_cap=CAP_B, hold_until_day=DAY0 + 7),
        jumper=True,
    )
    step(
        "prov_slot2",
        "allowlist slot 2 (D), cap 20,000.00",
        ProvisionFrame(slot=2, account_hash=HASH_D, account_cap=CAP_D, hold_until_day=DAY0),
        jumper=True,
    )
    step(
        "prov_slot3_c",
        "allowlist slot 3 (C) for the per-currency happy paths",
        ProvisionFrame(slot=3, account_hash=HASH_C, account_cap=CAP_D, hold_until_day=DAY0),
        jumper=True,
    )
    step(
        "prov_daily_cap",
        "daily cap 6,000.00",
        ProvisionFrame(slot=SLOT_DAILY_CAP, daily_cap=DAILY_CAP),
        jumper=True,
    )
    step(
        "prov_nonce_timeout",
        "nonce timeout 1 day",
        ProvisionFrame(slot=SLOT_NONCE_TIMEOUT, nonce_timeout_days=1),
        jumper=True,
    )
    bad_slot = bytearray(
        encode(ProvisionFrame(slot=0, account_hash=HASH_A, account_cap=1, hold_until_day=0))
    )
    bad_slot[1] = 0x10
    bad_slot[62:64] = crc16(bytes(bad_slot[:62])).to_bytes(2, "big")
    step("prov_bad_slot", "provisioning slot 0x10 is malformed", bytes(bad_slot), jumper=True)
    step("status_provisioned", "status after provisioning", StatusFrame())
    step("day_tick_273", "first day tick (2026-10-01)", DayTickFrame(day=DAY0))
    step(
        "pay_eur_ok",
        "happy path EUR: 1,000.00 to A",
        _pay(1, HASH_A, 100_000, "EUR", pay_id="PAY-000001"),
    )
    step(
        "pay_replay_seq",
        "replaying seq 1 is bad_seq",
        _pay(1, HASH_A, 100_000, "EUR", pay_id="PAY-000001"),
    )
    step(
        "pay_skip_seq",
        "skipping to seq 3 is bad_seq",
        _pay(3, HASH_A, 100_000, "EUR", pay_id="PAY-000002"),
    )
    step(
        "pay_unknown_hash",
        "unknown account hash is not_allowlisted",
        _pay(2, HASH_X, 100_000, "EUR", pay_id="PAY-000002"),
    )
    step(
        "pay_in_hold",
        "B is in hold until day 280",
        _pay(2, HASH_B, 100_000, "CZK", pay_id="PAY-000002"),
    )
    step(
        "pay_cap_exact",
        "A at exactly its cap 2,500.00 passes",
        _pay(2, HASH_A, CAP_A, "EUR", pay_id="PAY-000002"),
    )
    step(
        "pay_cap_over",
        "A one minor unit over its cap",
        _pay(3, HASH_A, CAP_A + 1, "EUR", pay_id="PAY-000003"),
    )
    step(
        "pay_daily_exact",
        "daily spent reaches exactly the daily cap",
        _pay(3, HASH_A, DAILY_CAP - 100_000 - CAP_A, "EUR", pay_id="PAY-000003"),
    )
    step(
        "pay_daily_over",
        "one minor unit over the daily cap",
        _pay(4, HASH_A, 1, "EUR", pay_id="PAY-000004"),
    )
    step("day_tick_274", "day tick resets the daily spend", DayTickFrame(day=DAY0 + 1))
    step(
        "pay_after_tick",
        "first payment after the tick",
        _pay(4, HASH_A, 1, "EUR", pay_id="PAY-000004"),
    )
    step(
        "prov_threshold",
        "button threshold 3,000.00 (jumper back in)",
        ProvisionFrame(slot=SLOT_BUTTON_THRESHOLD, button_threshold=THRESHOLD),
        jumper=True,
    )
    step(
        "prov_daily_cap_raised",
        "daily cap raised to 20,000.00 for the button cases",
        ProvisionFrame(slot=SLOT_DAILY_CAP, daily_cap=CAP_D),
        jumper=True,
    )
    step(
        "pay_needs_button",
        "4,000.00 to D above the threshold without a nonce",
        _pay(5, HASH_D, 400_000, "EUR", pay_id="PAY-000005"),
    )
    step("nonce_query_none", "no nonce loaded", NonceQueryFrame())
    step(
        "pay_bad_nonce",
        "wrong nonce after a button press",
        _pay(5, HASH_D, 400_000, "EUR", nonce=0xDEADBEEF, pay_id="PAY-000005"),
        press_button=True,
    )
    step("nonce_query_loaded", "nonce loaded and valid", NonceQueryFrame())
    step(
        "pay_with_nonce",
        "correct nonce signs the payment",
        _pay(5, HASH_D, 400_000, "EUR", nonce=-1, pay_id="PAY-000005"),
    )
    step(
        "pay_nonce_reuse",
        "the same nonce again is nonce_used",
        _pay(6, HASH_D, 400_000, "EUR", nonce=-1, pay_id="PAY-000006"),
    )
    step("day_tick_275", "day tick 275", DayTickFrame(day=DAY0 + 2))
    step(
        "press_then_two_ticks_a",
        "button pressed on day 275, tick 276",
        DayTickFrame(day=DAY0 + 3),
        press_button=True,
    )
    step(
        "pay_expired_nonce",
        "a nonce older than the timeout is nonce_used",
        _pay(6, HASH_D, 400_000, "EUR", nonce=-1, pay_id="PAY-000006"),
    )
    step(
        "pay_small_clears_nonce",
        "a payment below the threshold still consumes a fresh nonce",
        _pay(6, HASH_D, 1_000, "EUR", pay_id="PAY-000006"),
        press_button=True,
    )
    step("nonce_query_cleared", "nonce cleared by the commit", NonceQueryFrame())
    step("day_tick_279", "day 279: B still in hold", DayTickFrame(day=DAY0 + 6))
    step(
        "pay_hold_day_before",
        "B the day before its hold ends",
        _pay(7, HASH_B, 100_000, "CZK", pay_id="PAY-000007"),
    )
    step("day_tick_280", "day 280: hold boundary", DayTickFrame(day=DAY0 + 7))
    step(
        "pay_hold_boundary",
        "B on the boundary day passes",
        _pay(7, HASH_B, 100_000, "CZK", pay_id="PAY-000007"),
    )
    step("pay_pln", "happy path PLN", _pay(8, HASH_C, 50_000, "PLN", pay_id="PAY-000008"))
    step("pay_czk", "happy path CZK", _pay(9, HASH_C, 50_000, "CZK", pay_id="PAY-000009"))
    step("pay_jpy", "happy path JPY", _pay(10, HASH_C, 50_000, "JPY", pay_id="PAY-000010"))
    step("pay_usd", "happy path USD", _pay(11, HASH_C, 50_000, "USD", pay_id="PAY-000011"))
    step("malformed_short", "63-byte frame is malformed", encode(_pay(12, HASH_A, 1, "EUR"))[:63])
    bad_type = bytearray(encode(StatusFrame()))
    bad_type[0] = 0x09
    bad_type[62:64] = crc16(bytes(bad_type[:62])).to_bytes(2, "big")
    step("malformed_type", "unknown frame type 0x09", bytes(bad_type))
    bad_ccy = bytearray(encode(_pay(12, HASH_A, 1, "EUR", pay_id="PAY-000012")))
    bad_ccy[45] = 0x07
    bad_ccy[62:64] = crc16(bytes(bad_ccy[:62])).to_bytes(2, "big")
    step("malformed_currency", "unknown currency code 0x07", bytes(bad_ccy))
    bad_id = bytearray(encode(_pay(12, HASH_A, 1, "EUR", pay_id="PAY-000012")))
    bad_id[50] = 0x00
    bad_id[62:64] = crc16(bytes(bad_id[:62])).to_bytes(2, "big")
    step("malformed_pay_id", "non-ASCII pay_id", bytes(bad_id))
    step(
        "bad_crc",
        "corrupted CRC",
        corrupt_crc(encode(_pay(12, HASH_A, 1, "EUR", pay_id="PAY-000012"))),
    )
    step(
        "seq_unchanged_after_rejects",
        "seq 12 still next after all the rejects",
        _pay(12, HASH_A, 1, "EUR", pay_id="PAY-000012"),
    )
    step(
        "prov_locked_after_pull",
        "provisioning locked once the jumper is pulled",
        ProvisionFrame(slot=4, account_hash=HASH_X, account_cap=1, hold_until_day=0),
    )
    step("status_final", "final status", StatusFrame())
    return steps


def generate() -> dict[str, Any]:
    """Run the scenario against a fresh stub and record request, response and state."""
    jumper = {"on": False}
    stub = StubSigner(None, jumper_present=lambda: jumper["on"], nonce_seed=NONCE_SEED)
    cases: list[dict[str, Any]] = []
    for s in scenario():
        jumper["on"] = bool(s["jumper"])
        before: dict[str, Any] = {}
        if s["press_button"]:
            before["press_button"] = stub.press_button()
        frame = s["frame"]
        if isinstance(frame, PayFrame) and frame.nonce == -1:
            frame = PayFrame(
                frame.seq,
                frame.account_hash,
                frame.amount_minor,
                frame.currency,
                stub.nonce_value,
                frame.pay_id,
            )
        raw = frame if isinstance(frame, bytes) else encode(frame)
        result = stub.sign(raw)
        cases.append(
            {
                "id": s["id"],
                "title": s["title"],
                "jumper": bool(s["jumper"]),
                "before": before,
                "request_hex": raw.hex(),
                "response_hex": result.raw.hex(),
                "expect": {"ok": result.ok, "reason": result.reason},
                "state_after": stub.export_state(),
            }
        )
    return {
        "schema": SCHEMA,
        "key_hex": KEY.hex(),
        "nonce_seed": NONCE_SEED,
        "day0": DAY0,
        "cases": cases,
    }
