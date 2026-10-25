"""Chart of accounts (spec §2.1). Every posting is validated against this registry."""

from __future__ import annotations

from typing import Final, Literal

AccountType = Literal["Assets", "Liabilities", "Expenses", "Income", "Equity"]

CHART: Final[tuple[str, ...]] = (
    "Assets:Bank:EUR",
    "Assets:Bank:CZK",
    "Assets:Bank:PLN",
    "Assets:Prepayments",
    "Liabilities:AP",
    "Liabilities:AP:Retention",
    "Liabilities:GRNI",
    "Liabilities:VAT:Output",
    "Liabilities:Intercompany",
    "Expenses:Materials:Frames",
    "Expenses:Materials:Foam",
    "Expenses:Materials:Trim",
    "Expenses:Materials:Mechanisms",
    "Expenses:Materials:Harness",
    "Expenses:Materials:Fasteners",
    "Expenses:Tooling",
    "Expenses:Freight",
    "Expenses:VAT:Input",
    "Expenses:Bank:Fees",
    "Expenses:FX:Rounding",
    "Expenses:FX:Realized",
    "Income:Discounts",
    "Equity:Opening",
)
_CHART_SET: Final[frozenset[str]] = frozenset(CHART)

BANK_ACCOUNT_BY_CURRENCY: Final[dict[str, str]] = {
    "EUR": "Assets:Bank:EUR",
    "CZK": "Assets:Bank:CZK",
    "PLN": "Assets:Bank:PLN",
}

EXPENSE_ACCOUNT_BY_CATEGORY: Final[dict[str, str]] = {
    "seat frames": "Expenses:Materials:Frames",
    "foam": "Expenses:Materials:Foam",
    "trim fabric": "Expenses:Materials:Trim",
    "headrest mechanisms": "Expenses:Materials:Mechanisms",
    "wire harnesses": "Expenses:Materials:Harness",
    "fasteners": "Expenses:Materials:Fasteners",
    "tooling": "Expenses:Tooling",
    "freight": "Expenses:Freight",
}

AP_ACCOUNT: Final[str] = "Liabilities:AP"
AP_RETENTION_ACCOUNT: Final[str] = "Liabilities:AP:Retention"
GRNI_ACCOUNT: Final[str] = "Liabilities:GRNI"
VAT_INPUT_ACCOUNT: Final[str] = "Expenses:VAT:Input"
VAT_OUTPUT_ACCOUNT: Final[str] = "Liabilities:VAT:Output"
PREPAYMENTS_ACCOUNT: Final[str] = "Assets:Prepayments"
FX_ROUNDING_ACCOUNT: Final[str] = "Expenses:FX:Rounding"
FX_REALIZED_ACCOUNT: Final[str] = "Expenses:FX:Realized"
BANK_FEES_ACCOUNT: Final[str] = "Expenses:Bank:Fees"
DISCOUNT_INCOME_ACCOUNT: Final[str] = "Income:Discounts"
INTERCOMPANY_ACCOUNT: Final[str] = "Liabilities:Intercompany"
OPENING_EQUITY_ACCOUNT: Final[str] = "Equity:Opening"


class UnknownAccount(ValueError):
    """The account is not in the chart of accounts."""


def is_account(name: str) -> bool:
    return name in _CHART_SET


def validate_account(name: str) -> str:
    """Return ``name`` if it is in the chart, else raise ``UnknownAccount``."""
    if name not in _CHART_SET:
        raise UnknownAccount(f"unknown account {name!r}; see spec §2.1 for the chart of accounts")
    return name


def account_type(name: str) -> AccountType:
    validate_account(name)
    head = name.split(":", 1)[0]
    if head == "Assets":
        return "Assets"
    if head == "Liabilities":
        return "Liabilities"
    if head == "Expenses":
        return "Expenses"
    if head == "Income":
        return "Income"
    return "Equity"


def expense_account_for_category(category: str) -> str:
    try:
        return EXPENSE_ACCOUNT_BY_CATEGORY[category]
    except KeyError as exc:
        raise UnknownAccount(f"no expense account for supplier category {category!r}") from exc
