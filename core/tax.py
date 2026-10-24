"""Entity tax facts (spec §2.1). The ledger entity is Toyota Boshoku Europe GmbH, VAT-registered
in Germany at 19 %.

* DE suppliers charge 19 % VAT → ``Dr Expenses:VAT:Input``.
* CZ/PL/AT (any other EU) suppliers are intra-EU B2B reverse charge: the invoice shows 0 % with a
  note and the entity self-assesses ``Dr Expenses:VAT:Input`` / ``Cr Liabilities:VAT:Output`` at 19 %.
* JP/US suppliers are imports with no VAT on the invoice (import VAT is out of scope).
* An intra-EU supplier charging VAT triggers R11 (post net, dispute the VAT).
"""

from __future__ import annotations

from decimal import ROUND_HALF_EVEN, Decimal
from typing import Final, Literal

from core.accounts import VAT_INPUT_ACCOUNT, VAT_OUTPUT_ACCOUNT

ENTITY_NAME: Final[str] = "Toyota Boshoku Europe GmbH"
ENTITY_COUNTRY: Final[str] = "DE"
ENTITY_VAT_ID: Final[str] = "DE812345670"
ENTITY_VAT_RATE_BP: Final[int] = 1900

EU_COUNTRIES: Final[frozenset[str]] = frozenset(
    [
        "AT",
        "BE",
        "BG",
        "HR",
        "CY",
        "CZ",
        "DK",
        "EE",
        "FI",
        "FR",
        "DE",
        "GR",
        "HU",
        "IE",
        "IT",
        "LV",
        "LT",
        "LU",
        "MT",
        "NL",
        "PL",
        "PT",
        "RO",
        "SK",
        "SI",
        "ES",
        "SE",
    ]
)

Treatment = Literal["domestic", "reverse_charge", "import"]


def treatment_for_country(country: str) -> Treatment:
    code = country.upper()
    if code == ENTITY_COUNTRY:
        return "domestic"
    if code in EU_COUNTRIES:
        return "reverse_charge"
    return "import"


def self_assessed_vat(net_minor: int) -> int:
    """19 % of the net amount, half-even, in the invoice currency's minor units."""
    return int(
        (Decimal(net_minor) * Decimal(ENTITY_VAT_RATE_BP) / Decimal(10000)).quantize(
            Decimal(1), rounding=ROUND_HALF_EVEN
        )
    )


def expected_vat_minor(net_minor: int, treatment: Treatment) -> int:
    """VAT the supplier should print: 19 % for domestic supplies, nothing otherwise."""
    return self_assessed_vat(net_minor) if treatment == "domestic" else 0


def is_vat_wrongly_charged(treatment: Treatment, printed_vat_minor: int) -> bool:
    return treatment != "domestic" and printed_vat_minor > 0


def vat_lines(
    net_minor: int, treatment: Treatment, printed_vat_minor: int
) -> list[tuple[str, int, int]]:
    """Journal lines (account, debit, credit) for the VAT side of a purchase.

    Domestic: the printed VAT is recoverable input VAT. Reverse charge: self-assess 19 % on the net
    (input and output, same entry). Import: nothing. Wrongly charged VAT on a reverse-charge supply is
    never posted (R11 disputes it) and the self-assessment still applies.
    """
    if treatment == "domestic":
        return [(VAT_INPUT_ACCOUNT, printed_vat_minor, 0)] if printed_vat_minor else []
    if treatment == "reverse_charge":
        vat = self_assessed_vat(net_minor)
        return [(VAT_INPUT_ACCOUNT, vat, 0), (VAT_OUTPUT_ACCOUNT, 0, vat)] if vat else []
    return []
