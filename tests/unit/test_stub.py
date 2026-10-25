"""Stub signer (spec §10.3/10.5): one test per reason code, reject changes no state, daily reset,
nonce lifecycle, provisioning gate, persistence."""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from signer.frames import (
    SLOT_BUTTON_THRESHOLD,
    SLOT_DAILY_CAP,
    SLOT_KEY,
    SLOT_NONCE_TIMEOUT,
    DayTickFrame,
    NonceQueryFrame,
    PayFrame,
    ProvisionFrame,
    StatusFrame,
    corrupt_crc,
    encode,
    hmac_tag,
    unpack_nonce,
    unpack_status,
)
from signer.stub import StubSigner

A = hashlib.sha256(b"A").digest()
B = hashlib.sha256(b"B").digest()
X = hashlib.sha256(b"X").digest()
KEY = bytes.fromhex("aa" * 32)
DAY = 273


class Rig:
    def __init__(
        self,
        path: Path | None = None,
        *,
        cap: int = 100_000,
        daily_cap: int = 150_000,
        threshold: int = 10_000_000,
    ) -> None:
        self.jumper = True
        self.stub = StubSigner(path, jumper_present=lambda: self.jumper, nonce_seed=3)
        for frame in (
            ProvisionFrame(slot=SLOT_KEY, key=KEY),
            ProvisionFrame(slot=0, account_hash=A, account_cap=cap, hold_until_day=DAY),
            ProvisionFrame(slot=1, account_hash=B, account_cap=cap, hold_until_day=DAY + 3),
            ProvisionFrame(slot=SLOT_DAILY_CAP, daily_cap=daily_cap),
            ProvisionFrame(slot=SLOT_BUTTON_THRESHOLD, button_threshold=threshold),
            ProvisionFrame(slot=SLOT_NONCE_TIMEOUT, nonce_timeout_days=1),
        ):
            assert self.stub.sign(encode(frame)).ok
        self.jumper = False
        self.stub.tick_day(DAY)

    def pay(self, seq: int, h: bytes, amount: int, *, nonce: int = 0, currency: str = "EUR"):
        frame = encode(
            PayFrame(
                seq=seq,
                account_hash=h,
                amount_minor=amount,
                currency=currency,
                nonce=nonce,
                pay_id=f"PAY-{seq:06d}",
            )
        )
        return frame, self.stub.sign(frame)


@pytest.fixture
def rig() -> Rig:
    return Rig()


@pytest.fixture
def button_rig() -> Rig:
    return Rig(cap=1_000_000, daily_cap=1_000_000, threshold=60_000)


def test_happy_path_signs_with_the_key(rig: Rig) -> None:
    frame, res = rig.pay(1, A, 50_000)
    assert res.ok and res.tag == hmac_tag(KEY, frame) and res.raw == b"\x00" + res.tag
    assert rig.stub.seq == 1 and rig.stub.daily_spent == 50_000


def test_bad_seq_replay_and_skip(rig: Rig) -> None:
    rig.pay(1, A, 1)
    assert rig.pay(1, A, 1)[1].reason == "bad_seq"
    assert rig.pay(3, A, 1)[1].reason == "bad_seq"
    assert rig.pay(2, A, 1)[1].ok


def test_not_allowlisted(rig: Rig) -> None:
    assert rig.pay(1, X, 1)[1].reason == "not_allowlisted"


def test_in_hold_until_boundary_day(rig: Rig) -> None:
    assert rig.pay(1, B, 1)[1].reason == "in_hold"
    rig.stub.tick_day(DAY + 2)
    assert rig.pay(1, B, 1)[1].reason == "in_hold"
    rig.stub.tick_day(DAY + 3)
    assert rig.pay(1, B, 1)[1].ok


def test_account_cap_exact_and_over(rig: Rig) -> None:
    assert rig.pay(1, A, 100_001)[1].reason == "over_account_cap"
    assert rig.pay(1, A, 100_000)[1].ok


def test_daily_cap_and_reset(rig: Rig) -> None:
    assert rig.pay(1, A, 100_000)[1].ok
    assert rig.pay(2, A, 50_001)[1].reason == "over_daily_cap"
    assert rig.pay(2, A, 50_000)[1].ok  # exactly at the cap
    assert rig.pay(3, A, 1)[1].reason == "over_daily_cap"
    rig.stub.tick_day(DAY)  # same day: no reset
    assert rig.pay(3, A, 1)[1].reason == "over_daily_cap"
    rig.stub.tick_day(DAY + 1)
    assert rig.pay(3, A, 1)[1].ok and rig.stub.daily_spent == 1


def test_button_lifecycle(button_rig: Rig) -> None:
    rig = button_rig
    assert rig.pay(1, A, 60_001)[1].reason == "needs_button"
    assert rig.stub.nonce() is None
    nonce = rig.stub.press_button()
    assert nonce != 0 and rig.stub.nonce() == nonce
    assert rig.pay(1, A, 60_001, nonce=nonce ^ 1)[1].reason == "bad_nonce"
    assert rig.pay(1, A, 60_001, nonce=nonce)[1].ok
    assert rig.stub.nonce() is None
    assert rig.pay(2, A, 60_001, nonce=nonce)[1].reason == "nonce_used"
    assert rig.pay(2, A, 60_001)[1].reason == "needs_button"


def test_nonce_expires_after_the_timeout(button_rig: Rig) -> None:
    rig = button_rig
    nonce = rig.stub.press_button()
    rig.stub.tick_day(DAY + 1)
    assert rig.stub.nonce() is None
    assert rig.pay(1, A, 60_001, nonce=nonce)[1].reason == "nonce_used"
    ok, payload, _ = (lambda r: (r.ok, r.payload, r.reason))(
        rig.stub.sign(encode(NonceQueryFrame()))
    )
    assert ok and unpack_nonce(payload) == (nonce, False)


def test_any_commit_clears_the_nonce(button_rig: Rig) -> None:
    rig = button_rig
    rig.stub.press_button()
    assert rig.pay(1, A, 10)[1].ok  # below the threshold
    assert rig.stub.nonce() is None


def test_malformed_and_bad_crc(rig: Rig) -> None:
    frame = encode(
        PayFrame(seq=1, account_hash=A, amount_minor=1, currency="EUR", pay_id="PAY-000001")
    )
    assert rig.stub.sign(frame[:63]).reason == "malformed"
    assert rig.stub.sign(corrupt_crc(frame)).reason == "bad_crc"
    assert rig.stub.sign(frame).ok, "the good frame still signs: rejects changed nothing"


def test_reject_changes_no_state(rig: Rig) -> None:
    rig.pay(1, A, 10)
    before = rig.stub.export_state()
    for _frame, res in (
        rig.pay(1, A, 1),
        rig.pay(2, X, 1),
        rig.pay(2, B, 1),
        rig.pay(2, A, 100_001),
        rig.pay(2, A, 149_991),
    ):
        assert not res.ok
        assert rig.stub.export_state() == before


def test_provisioning_needs_the_jumper(rig: Rig) -> None:
    res = rig.stub.sign(
        encode(ProvisionFrame(slot=2, account_hash=X, account_cap=1, hold_until_day=0))
    )
    assert res.reason == "provisioning_locked" and rig.stub.slot_of(X) is None
    rig.jumper = True
    res = rig.stub.sign(
        encode(ProvisionFrame(slot=2, account_hash=X, account_cap=1, hold_until_day=0))
    )
    assert res.ok and res.payload == b"\x02" and rig.stub.slot_of(X) == 2


def test_status_frame_reports_state(rig: Rig) -> None:
    rig.pay(1, A, 5)
    res = rig.stub.sign(encode(StatusFrame()))
    assert res.ok
    assert unpack_status(res.payload) == {
        "seq": 1,
        "day": DAY,
        "daily_cap": 150_000,
        "daily_spent": 5,
        "button_threshold": 10_000_000,
        "nonce_valid": False,
        "slots_used": 2,
        "key_loaded": True,
        "nonce_timeout_days": 1,
    }
    st = rig.stub.status()
    assert st.kind == "stub" and st.seq == 1 and [s.slot for s in st.slots] == [0, 1]


def test_unloaded_key_signs_with_zeros() -> None:
    jumper = {"on": True}
    stub = StubSigner(None, jumper_present=lambda: jumper["on"])
    stub.sign(encode(ProvisionFrame(slot=0, account_hash=A, account_cap=10, hold_until_day=0)))
    stub.sign(encode(ProvisionFrame(slot=SLOT_DAILY_CAP, daily_cap=10)))
    frame = encode(
        PayFrame(seq=1, account_hash=A, amount_minor=1, currency="EUR", pay_id="PAY-000001")
    )
    res = stub.sign(frame)
    assert res.ok and res.tag == hmac_tag(bytes(32), frame) and not stub.status().key_loaded


def test_state_persists_across_restarts(tmp_path: Path) -> None:
    rig = Rig(tmp_path / "stub.db")
    rig.pay(1, A, 7)
    nonce = rig.stub.press_button()
    rig.stub.close()
    again = StubSigner(tmp_path / "stub.db", jumper_present=lambda: False, nonce_seed=3)
    assert (
        again.seq == 1
        and again.daily_spent == 7
        and again.nonce() == nonce
        and again.slot_of(A) == 0
    )
    assert again.sign(encode(DayTickFrame(day=DAY))).ok
    frame = encode(
        PayFrame(seq=2, account_hash=A, amount_minor=1, currency="EUR", pay_id="PAY-000002")
    )
    assert again.sign(frame).tag == hmac_tag(KEY, frame)
