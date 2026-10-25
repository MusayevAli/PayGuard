"""The spend cap and the .env loader: no call leaves the machine once PAYGUARD_MAX_USD is reached,
every priced call lands in the machine-wide ledger, and .env never overrides the environment."""

from __future__ import annotations

import os
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from agents.llm import (
    LLMClient,
    LLMUnavailable,
    load_dotenv,
    present_for_prose,
    validate_explanation,
)


class _Fake:
    """A stand-in for anthropic.Anthropic 2.x: ``create`` takes the SDK's keyword arguments only
    (no ``temperature``), answers with a short text and fixed usage."""

    def __init__(self) -> None:
        self.calls = 0
        self.messages = SimpleNamespace(create=self._create)

    def _create(
        self,
        *,
        model: str,
        max_tokens: int,
        messages: list[Any],
        system: Any = None,
        tools: Any = None,
        tool_choice: Any = None,
        output_config: Any = None,
    ) -> Any:
        self.calls += 1
        return SimpleNamespace(
            stop_reason="end_turn",
            content=[SimpleNamespace(type="text", text="Posted under R1 for INV-0001.")],
            usage=SimpleNamespace(
                input_tokens=1_000_000,
                output_tokens=0,
                cache_creation_input_tokens=0,
                cache_read_input_tokens=0,
            ),
            _request_id="req_1",
        )


@pytest.fixture
def key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
    monkeypatch.delenv("PAYGUARD_MAX_USD", raising=False)
    monkeypatch.delenv("PAYGUARD_MODEL_TEXT", raising=False)


def test_cap_stops_calls_and_the_ledger_spans_clients(tmp_path: Path, key: None) -> None:
    spend_db = tmp_path / "spend.db"
    fake = _Fake()
    # Haiku: one million input tokens cost USD 1.00, so a USD 2.50 cap allows three calls
    llm = LLMClient(
        tmp_path / "a.db",
        client=fake,
        text_model="claude-haiku-4-5",
        max_usd="2.5",
        spend_db=spend_db,
    )
    for i in range(3):
        assert llm._text_call("explain", "s", f"u{i}") == "Posted under R1 for INV-0001."
    assert fake.calls == 3 and llm.spent_usd() == Decimal("3.000000")
    with pytest.raises(LLMUnavailable, match="spend cap reached"):
        llm._text_call("explain", "s", "u3")
    assert fake.calls == 3, "the capped call never reached the API"
    # explain() degrades to the template instead of failing the agent
    text = llm.explain({"action_type": "posted", "rule_id": "R1", "refs": ["INV-0001"]})
    assert "INV-0001" in text and fake.calls == 3
    llm.close()
    # a second client with another cache sees the same ledger and is capped at once
    other = LLMClient(tmp_path / "b.db", client=fake, max_usd="2.5", spend_db=spend_db)
    assert other.spent_usd() == Decimal("3.000000")
    with pytest.raises(LLMUnavailable):
        other._text_call("explain", "s", "u4")
    other.close()


def test_no_cap_means_no_limit_and_env_cap_is_read(
    tmp_path: Path, key: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = _Fake()
    free = LLMClient(tmp_path / "c.db", client=fake, spend_db=tmp_path / "s1.db")
    assert free.max_usd is None
    free._text_call("explain", "s", "u")
    free.close()
    monkeypatch.setenv("PAYGUARD_MAX_USD", "0")
    capped = LLMClient(tmp_path / "d.db", client=fake, spend_db=tmp_path / "s2.db")
    assert capped.max_usd == Decimal("0")
    with pytest.raises(LLMUnavailable):
        capped._text_call("explain", "s", "u")
    capped.close()


def test_dotenv_fills_gaps_without_overriding(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    env = tmp_path / ".env"
    env.write_text(
        "# comment\nANTHROPIC_API_KEY=sk-ant-from-file\nexport PAYGUARD_MAX_USD='7'\nPAYGUARD_MODEL_TEXT=\"claude-haiku-4-5\"\n"
    )
    monkeypatch.setenv("PAYGUARD_ENV_FILE", str(env))
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.setenv("PAYGUARD_MAX_USD", "3")
    monkeypatch.delenv("PAYGUARD_MODEL_TEXT", raising=False)
    loaded = load_dotenv()
    assert loaded == {
        "ANTHROPIC_API_KEY": "sk-ant-from-file",
        "PAYGUARD_MODEL_TEXT": "claude-haiku-4-5",
    }
    assert os.environ["PAYGUARD_MAX_USD"] == "3", "the environment wins over the file"
    assert LLMClient.credentials_available()
    llm = LLMClient(tmp_path / "e.db", client=_Fake(), spend_db=tmp_path / "s3.db")
    assert llm.max_usd == Decimal("3") and llm.text_model == "claude-haiku-4-5"
    llm.close()


def test_request_options_match_the_installed_sdk(tmp_path: Path, key: None) -> None:
    """anthropic 2.x has no temperature argument: legacy models get no options, current models get
    an effort level (high for documents, low for prose)."""
    llm = LLMClient(tmp_path / "f.db", client=_Fake(), spend_db=tmp_path / "s4.db")
    assert llm._kwargs("claude-haiku-4-5", "explain") == {}
    assert llm._kwargs("claude-sonnet-4-5", "extract") == {}
    assert llm._kwargs("claude-opus-5", "extract") == {"output_config": {"effort": "high"}}
    assert llm._kwargs("claude-opus-5", "explain") == {"output_config": {"effort": "low"}}
    assert llm._kwargs("claude-sonnet-5", "persona") == {"output_config": {"effort": "low"}}
    llm.close()


def test_prose_sees_formatted_amounts_and_the_validator_rejects_misread_cents() -> None:
    decision = {
        "action_type": "posted",
        "rule_id": "R1",
        "refs": ["INV-0001", "PO-10001"],
        "inputs": {"gross_minor": 123400, "currency": "EUR", "qty_billed": 1000},
        "decision": {"post_minor": -5, "lines": [{"net_minor": 1200, "currency": "JPY"}]},
    }
    shown = present_for_prose(decision)
    assert shown["inputs"] == {"gross": "EUR 1,234.00", "currency": "EUR", "qty_billed": 1000}
    assert shown["decision"]["post"] == "EUR -0.05", (
        "the currency is found anywhere in the decision"
    )
    assert shown["decision"]["lines"] == [{"net": "JPY 1,200", "currency": "JPY"}]
    import json

    blob = json.dumps(shown)
    refs = decision["refs"]
    assert validate_explanation(
        "INV-0001 posts EUR 1,234.00 against PO-10001 under R1.", refs, blob
    )
    assert validate_explanation("1000 pieces billed on INV-0001.", refs, blob)
    assert not validate_explanation("The agent posts EUR 123,400 for INV-0001.", refs, blob)
    assert not validate_explanation("Posted EUR 1,234.00 on INV-0002.", refs, blob), "unknown id"
    assert not validate_explanation("Paid 999 units on INV-0001.", refs, blob), "unknown number"
    assert not validate_explanation(" ".join(["word"] * 61), refs, blob)
