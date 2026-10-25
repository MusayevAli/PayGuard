"""tests/vectors.json is the hardware contract: regenerating it must give the committed file."""

from __future__ import annotations

import json
from pathlib import Path

from signer.frames import REASONS, decode_response
from signer.stub import StubSigner
from signer.vectors import KEY, NONCE_SEED, SCHEMA, generate

VECTORS = Path(__file__).resolve().parents[1] / "vectors.json"


def test_committed_vectors_match_the_generator() -> None:
    committed = json.loads(VECTORS.read_text(encoding="utf-8"))
    assert committed == generate(), "run `uv run python -m scripts.gen_vectors` and review the diff"


def test_vectors_cover_the_contract() -> None:
    data = generate()
    cases = data["cases"]
    assert (
        data["schema"] == SCHEMA
        and data["key_hex"] == KEY.hex()
        and data["nonce_seed"] == NONCE_SEED
    )
    assert len(cases) >= 40
    reasons = {c["expect"]["reason"] for c in cases if not c["expect"]["ok"]}
    assert reasons == set(REASONS.values()), sorted(set(REASONS.values()) - reasons)
    ids = [c["id"] for c in cases]
    assert len(ids) == len(set(ids))
    for required in (
        "pay_eur_ok",
        "pay_pln",
        "pay_czk",
        "pay_jpy",
        "pay_usd",
        "pay_after_tick",
        "pay_with_nonce",
        "pay_nonce_reuse",
        "pay_hold_boundary",
        "pay_cap_exact",
        "pay_cap_over",
        "pay_daily_exact",
        "pay_daily_over",
        "key_with_jumper",
        "key_without_jumper",
        "malformed_short",
        "bad_crc",
        "pay_slot63",
    ):
        assert required in ids
    for c in cases:
        assert len(bytes.fromhex(c["request_hex"])) in (63, 64, 65)
        ok, _payload, reason = decode_response(bytes.fromhex(c["response_hex"]))
        assert ok == c["expect"]["ok"] and reason == c["expect"]["reason"]
        assert set(c["state_after"]) == {
            "seq",
            "day",
            "daily_spent",
            "nonce",
            "nonce_valid",
            "slots_used",
            "key_loaded",
        }


def test_replaying_the_vectors_on_a_fresh_stub_reproduces_every_response() -> None:
    data = generate()
    jumper = {"on": False}
    stub = StubSigner(None, jumper_present=lambda: jumper["on"], nonce_seed=NONCE_SEED)
    for case in data["cases"]:
        jumper["on"] = case["jumper"]
        if "press_button" in case["before"]:
            assert stub.press_button() == case["before"]["press_button"]
        res = stub.sign(bytes.fromhex(case["request_hex"]))
        assert res.raw.hex() == case["response_hex"], case["id"]
        assert stub.export_state() == case["state_after"], case["id"]
