from __future__ import annotations

import re
from pathlib import Path

import pytest

from core.accounts import (
    CHART,
    EXPENSE_ACCOUNT_BY_CATEGORY,
    UnknownAccount,
    account_type,
    expense_account_for_category,
    is_account,
    validate_account,
)

ROOT = Path(__file__).resolve().parents[2]
SPEC_CHART = """Assets:Bank:EUR  Assets:Bank:CZK  Assets:Bank:PLN
Assets:Prepayments
Liabilities:AP  Liabilities:AP:Retention  Liabilities:GRNI  Liabilities:VAT:Output
Expenses:Materials:Frames  Expenses:Materials:Foam  Expenses:Materials:Trim
Expenses:Materials:Mechanisms  Expenses:Materials:Harness  Expenses:Materials:Fasteners
Expenses:Tooling  Expenses:Freight  Expenses:VAT:Input  Expenses:Bank:Fees  Expenses:FX:Rounding  Expenses:FX:Realized
Equity:Opening"""


def test_chart_matches_the_spec_verbatim() -> None:
    assert tuple(SPEC_CHART.split()) == CHART
    assert len(set(CHART)) == 21
    spec = (ROOT / "spec" / "PAYGUARD_SPEC.md").read_text(encoding="utf-8")
    assert SPEC_CHART in spec


def test_validate_and_is_account() -> None:
    assert validate_account("Liabilities:AP") == "Liabilities:AP"
    assert is_account("Expenses:FX:Rounding")
    for bad in ("Assets:VAT:Input", "liabilities:ap", "", "Liabilities:AP:", "Expenses"):
        assert not is_account(bad)
        with pytest.raises(UnknownAccount):
            validate_account(bad)


def test_account_type() -> None:
    assert account_type("Assets:Bank:CZK") == "Assets"
    assert account_type("Liabilities:GRNI") == "Liabilities"
    assert account_type("Expenses:Tooling") == "Expenses"
    assert account_type("Equity:Opening") == "Equity"


def test_expense_account_for_every_supplier_category() -> None:
    categories = {
        "seat frames",
        "foam",
        "trim fabric",
        "headrest mechanisms",
        "wire harnesses",
        "fasteners",
        "tooling",
        "freight",
    }
    assert set(EXPENSE_ACCOUNT_BY_CATEGORY) == categories
    for cat in categories:
        assert expense_account_for_category(cat) in CHART
    with pytest.raises(UnknownAccount):
        expense_account_for_category("widgets")


def test_answer_key_generator_only_references_chart_accounts() -> None:
    source = (ROOT / "sim" / "worldgen" / "answer_key.py").read_text(encoding="utf-8")
    names = set(re.findall(r'"((?:Assets|Liabilities|Expenses|Equity):[A-Za-z:]+)"', source))
    assert names, "expected account names in the answer-key generator"
    assert names <= set(CHART), names - set(CHART)
