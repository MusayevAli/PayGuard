"""Money in explanation prose must use currency-aware major units."""

from __future__ import annotations

import copy
import hashlib
from pathlib import Path
from typing import Any

import pytest

from agents.llm import LLMClient, explanation_facts, template_explanation, template_message
from core.events import canonical_json


def _decision(currency: str = "PLN", minor: int = 1_150_000) -> dict[str, Any]:
    return {
        "action_type": "posted",
        "rule_id": "R15",
        "refs": ["INV-0001"],
        "inputs": {"lines": [["PO-10001", 1, "SKU-0001", 2, 2, minor, minor]]},
        "decision": {"ap_minor": minor, "post_minor": minor, "currency": currency},
    }


def _stub_wording(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, response: str
) -> tuple[LLMClient, list[dict[str, str]]]:
    client = LLMClient(tmp_path / "llm.sqlite")
    calls: list[dict[str, str]] = []

    def wording(purpose: str, system: str, user: str, max_tokens: int = 300) -> str:
        calls.append({"purpose": purpose, "system": system, "user": user})
        return response

    monkeypatch.setattr(client, "_text_call", wording)
    monkeypatch.setattr(client, "credentials_available", lambda: True)
    return client, calls


@pytest.mark.parametrize(
    "currency,minor,major",
    [
        ("EUR", 1_150_000, "11500.00"),
        ("PLN", 1_150_000, "11500.00"),
        ("JPY", 1_150_000, "1150000"),
        ("PLN", -123, "-1.23"),
        ("EUR", 0, "0.00"),
        ("EUR", 9_007_199_254_740_993, "90071992547409.93"),
    ],
)
def test_fallback_formats_minor_units_without_float_rounding(
    currency: str, minor: int, major: str
) -> None:
    decision = _decision(currency, minor)
    original = copy.deepcopy(decision)
    text = template_explanation(decision)
    assert currency in text
    assert major in text.replace(",", "")
    assert decision == original


@pytest.mark.parametrize(
    "currency,minor,bad_text,major",
    [
        ("PLN", 1_150_000, "Posted PLN1,150,000 for INV-0001.", "11500.00"),
        ("EUR", 12_345, "Posted 12,345 EUR for INV-0001.", "123.45"),
        ("PLN", -123, "Posted PLN -123 for INV-0001.", "-1.23"),
    ],
)
def test_minor_as_major_model_wording_is_rejected_then_safe_fallback_is_used(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    currency: str,
    minor: int,
    bad_text: str,
    major: str,
) -> None:
    client, calls = _stub_wording(tmp_path, monkeypatch, bad_text)
    decision = _decision(currency, minor)
    original = copy.deepcopy(decision)
    try:
        actual = client.explain(decision)
        assert actual != bad_text
        assert currency in actual and major in actual.replace(",", "")
        assert len(calls) == 2
        assert decision == original
    finally:
        client.close()


@pytest.mark.parametrize(
    "currency,minor,text",
    [
        ("PLN", 1_150_000, "Posted PLN 11,500.00 for INV-0001."),
        ("JPY", 1_150_000, "Posted JPY 1,150,000 for INV-0001."),
        ("EUR", -123, "Posted EUR -1.23 for INV-0001."),
    ],
)
def test_correct_currency_qualified_major_amount_is_preserved(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    currency: str,
    minor: int,
    text: str,
) -> None:
    client, calls = _stub_wording(tmp_path, monkeypatch, text)
    try:
        assert client.explain(_decision(currency, minor)) == text
        assert len(calls) == 1
    finally:
        client.close()


def test_old_explanation_cache_cannot_return_wrongly_scaled_money(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    decision = _decision()
    correct = "Posted PLN 11,500.00 for INV-0001."
    client, calls = _stub_wording(tmp_path, monkeypatch, correct)
    legacy_key = "explain:" + hashlib.sha256(canonical_json(decision).encode()).hexdigest()
    client._store(
        legacy_key,
        None,
        "explain",
        client.text_model,
        "Posted PLN1,150,000 for INV-0001.",
    )
    try:
        assert client.explain(decision) == correct
        assert len(calls) == 1
        assert client.explain(decision) == correct
        assert len(calls) == 1
    finally:
        client.close()


def test_display_facts_are_separate_from_unchanged_canonical_evidence() -> None:
    decision = _decision()
    original = copy.deepcopy(decision)
    facts = explanation_facts(decision)
    assert facts["canonical_decision"] == original
    assert facts["canonical_decision"] is not decision
    rows = {row["path"]: row for row in facts["money"]}
    assert rows["decision.ap_minor"] == {
        "path": "decision.ap_minor",
        "minor": 1_150_000,
        "currency": "PLN",
        "major": "11500.00",
        "display": "11,500.00 PLN",
    }
    # Positional PO/quantity/unit-price rows are left as canonical evidence;
    # numbers without declared money semantics must not become invented totals.
    assert not any(path.startswith("inputs.lines") for path in rows)
    facts["canonical_decision"]["decision"]["ap_minor"] = 1
    facts["canonical_decision"]["inputs"]["lines"][0][-1] = 1
    assert decision == original


def test_native_settlement_and_eur_values_keep_their_own_currency() -> None:
    decision = _decision("JPY", 81_000)
    decision["decision"].update(
        {
            "settlement_currency": "EUR",
            "settlement_minor": 50_000,
            "eur_equiv_minor": 50_000,
            "cash_currency": "EUR",
            "cash_minor": 20_000,
        }
    )
    rows = {row["path"]: row for row in explanation_facts(decision)["money"]}
    assert (rows["decision.ap_minor"]["currency"], rows["decision.ap_minor"]["major"]) == (
        "JPY",
        "81000",
    )
    for name in ("settlement_minor", "eur_equiv_minor"):
        assert (rows[f"decision.{name}"]["currency"], rows[f"decision.{name}"]["major"]) == (
            "EUR",
            "500.00",
        )
    assert (rows["decision.cash_minor"]["currency"], rows["decision.cash_minor"]["major"]) == (
        "EUR",
        "200.00",
    )


def test_foreign_exchange_currency_minor_tuples_and_negative_eur_result() -> None:
    decision = _decision()
    decision["decision"] = {
        "debit": ["USD", 12_345],
        "credit": ["JPY", 98_765],
        "realized_eur_minor": -7,
    }
    rows = {row["path"]: row for row in explanation_facts(decision)["money"]}
    assert (rows["decision.debit"]["currency"], rows["decision.debit"]["major"]) == (
        "USD",
        "123.45",
    )
    assert (rows["decision.credit"]["currency"], rows["decision.credit"]["major"]) == (
        "JPY",
        "98765",
    )
    assert (
        rows["decision.realized_eur_minor"]["currency"],
        rows["decision.realized_eur_minor"]["major"],
    ) == ("EUR", "-0.07")


def test_nearest_currency_overrides_envelope_hint() -> None:
    decision = _decision()
    decision["inputs"]["hints"] = {"currency": "EUR"}
    decision["decision"] = {
        "post_minor": 123,
        "adjustments": [{"currency": "JPY", "amount_minor": 123}],
    }
    rows = {row["path"]: row for row in explanation_facts(decision)["money"]}
    assert (rows["decision.post_minor"]["currency"], rows["decision.post_minor"]["major"]) == (
        "EUR",
        "1.23",
    )
    adjustment = next(row for row in rows.values() if row["path"].endswith("amount_minor"))
    assert (adjustment["currency"], adjustment["major"]) == ("JPY", "123")


@pytest.mark.parametrize("currency", [None, "XYZ"])
def test_unknown_currency_remains_explicit_minor_units(currency: str | None) -> None:
    decision = _decision()
    decision["decision"] = {"post_minor": 123}
    if currency is not None:
        decision["decision"]["currency"] = currency
    rows = explanation_facts(decision)["money"]
    assert len(rows) == 1 and rows[0]["minor"] == 123
    assert rows[0]["major"] is None and rows[0]["display"] is None
    assert rows[0]["currency"] in (None, "XYZ")
    fallback = template_explanation(decision)
    assert "123" in fallback and "minor" in fallback
    assert not any(ccy in fallback for ccy in ("EUR", "PLN", "USD", "JPY", "CZK"))


def test_cash_does_not_inherit_native_currency_without_settlement_metadata() -> None:
    decision = _decision("JPY", 81_000)
    decision["decision"]["cash_minor"] = 20_000
    cash = next(
        row for row in explanation_facts(decision)["money"] if row["path"] == "decision.cash_minor"
    )
    assert cash["minor"] == 20_000 and cash["major"] is None and cash["display"] is None


def test_prompt_contains_currency_aware_values_and_retains_canonical_evidence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    decision = _decision()
    client, calls = _stub_wording(tmp_path, monkeypatch, "Posted PLN 11,500.00 for INV-0001.")
    original = copy.deepcopy(decision)
    try:
        client.explain(decision)
        assert len(calls) == 1
        prompt = calls[0]["user"]
        assert '"major": "11500.00"' in prompt
        assert '"minor": 1150000' in prompt
        assert '"currency": "PLN"' in prompt
        assert '"canonical_decision"' in prompt
        assert decision == original
    finally:
        client.close()


def test_paid_and_cap_fields_use_explicit_currency_not_native_invoice_currency() -> None:
    decision = _decision("JPY", 81_000)
    decision["inputs"] = {
        "daily_cap_remaining_minor": 5_000_000,
        "daily_cap_remaining_currency": "EUR",
    }
    decision["decision"].update(
        {
            "paid_minor": 50_000,
            "paid_currency": "EUR",
            "settlement_currency": "EUR",
            "remaining_cap_minor": 4_950_000,
            "remaining_cap_currency": "EUR",
        }
    )
    rows = {row["path"]: row for row in explanation_facts(decision)["money"]}
    assert (rows["decision.post_minor"]["currency"], rows["decision.post_minor"]["major"]) == (
        "JPY",
        "81000",
    )
    for path, expected in (
        ("decision.paid_minor", "500.00"),
        ("decision.remaining_cap_minor", "49500.00"),
        ("inputs.daily_cap_remaining_minor", "50000.00"),
    ):
        assert (rows[path]["currency"], rows[path]["major"]) == ("EUR", expected)


def test_model_cannot_relabel_native_amount_as_settlement_currency(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    decision = _decision("JPY", 81_000)
    decision["decision"].update(
        {"paid_minor": 50_000, "paid_currency": "EUR", "settlement_currency": "EUR"}
    )
    bad_text = "Posted EUR 81,000 for INV-0001."
    client, calls = _stub_wording(tmp_path, monkeypatch, bad_text)
    try:
        actual = client.explain(decision)
        assert actual != bad_text
        assert "81,000 JPY" in actual
        assert len(calls) == 2
    finally:
        client.close()


def _message_case(kind: str) -> tuple[dict[str, Any], str, str, list[str]]:
    common = {
        "supplier_name": "Local Supplier",
        "invoice_number": "INV-0001",
        "currency": "PLN",
    }
    if kind == "buyer_confirmation":
        return (
            {
                **common,
                "to_name": "Buyer",
                "description": "Freight",
                "amount_minor": 1_150_000,
                "amount": "11500.00",
            },
            "Please confirm invoice INV-0001 for 1150000 PLN.",
            "Please confirm invoice INV-0001 for 11500.00 PLN.",
            ["11500.00"],
        )
    return (
        {
            **common,
            "qty_billed": 5,
            "qty_received": 3,
            "qty_posted": 3,
            "sku": "SF-1010",
            "po_id": "PO-10001",
            "gr_ids": "GR-1001",
            "amount_posted_minor": 37_035,
            "amount_posted": "370.35",
            "short_pay_minor": 24_690,
            "short_pay": "246.90",
            "balance_qty": 2,
        },
        "For invoice INV-0001, posted 37035 PLN and held 24690 PLN.",
        "For invoice INV-0001, posted 370.35 PLN and held 246.90 PLN.",
        ["370.35", "246.90"],
    )


@pytest.mark.parametrize("kind", ["short_pay_notice", "buyer_confirmation"])
def test_drafted_message_rejects_raw_minor_as_major_and_keeps_safe_skeleton(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, kind: str
) -> None:
    facts, wrong, _, majors = _message_case(kind)
    original = copy.deepcopy(facts)
    client, calls = _stub_wording(tmp_path, monkeypatch, wrong)
    try:
        text = client.draft_message(kind, facts)
        assert text == template_message(kind, facts)
        assert text != wrong
        assert all(f"{major} PLN" in text for major in majors)
        assert len(calls) == 1
        assert calls[0]["purpose"] == "draft_message"
        assert all(f'"major": "{major}"' in calls[0]["user"] for major in majors)
        assert '"canonical_decision"' in calls[0]["user"]
        assert facts == original
    finally:
        client.close()


@pytest.mark.parametrize("kind", ["short_pay_notice", "buyer_confirmation"])
def test_drafted_message_preserves_correct_major_wording(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, kind: str
) -> None:
    facts, _, correct, _ = _message_case(kind)
    client, calls = _stub_wording(tmp_path, monkeypatch, correct)
    try:
        assert client.draft_message(kind, facts) == correct
        assert len(calls) == 1
    finally:
        client.close()


@pytest.mark.parametrize("kind", ["short_pay_notice", "buyer_confirmation"])
def test_legacy_drafted_message_cache_cannot_return_wrongly_scaled_amounts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, kind: str
) -> None:
    facts, wrong, correct, _ = _message_case(kind)
    client, calls = _stub_wording(tmp_path, monkeypatch, correct)
    legacy_key = (
        "message:"
        + hashlib.sha256(canonical_json({"kind": kind, "facts": facts}).encode()).hexdigest()
    )
    client._store(legacy_key, None, "draft_message", client.text_model, wrong)
    try:
        assert client.draft_message(kind, facts) == correct
        assert len(calls) == 1
        assert client.draft_message(kind, facts) == correct
        assert len(calls) == 1
    finally:
        client.close()
