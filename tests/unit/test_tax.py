from __future__ import annotations

from pathlib import Path

from core.tax import (
    ENTITY_COUNTRY,
    ENTITY_VAT_ID,
    ENTITY_VAT_RATE_BP,
    expected_vat_minor,
    is_vat_wrongly_charged,
    self_assessed_vat,
    treatment_for_country,
    vat_lines,
)
from sim.worldgen.catalog import PLANTS

ROOT = Path(__file__).resolve().parents[2]


def test_entity_facts_match_spec() -> None:
    assert (
        ENTITY_COUNTRY == "DE"
        and ENTITY_VAT_RATE_BP == 1900
        and PLANTS["DE"].vat_id == ENTITY_VAT_ID
    )
    spec = (ROOT / "spec" / "PAYGUARD_SPEC.md").read_text(encoding="utf-8")
    assert (
        "VAT-registered in DE at 19%" in spec
        and "An intra-EU supplier charging VAT triggers R11" in spec
    )


def test_treatment_by_country() -> None:
    assert treatment_for_country("DE") == "domestic"
    for c in ("CZ", "PL", "AT", "EE", "de".upper()):
        assert treatment_for_country(c) in ("reverse_charge", "domestic")
    assert (
        treatment_for_country("CZ")
        == treatment_for_country("PL")
        == treatment_for_country("AT")
        == "reverse_charge"
    )
    assert treatment_for_country("JP") == treatment_for_country("US") == "import"


def test_vat_lines() -> None:
    assert (
        self_assessed_vat(100_000) == 19_000
        and self_assessed_vat(1_112_000) == 211_280
        and self_assessed_vat(5) == 1
    )
    assert vat_lines(100_000, "domestic", 19_000) == [("Expenses:VAT:Input", 19_000, 0)]
    assert vat_lines(100_000, "reverse_charge", 0) == [
        ("Expenses:VAT:Input", 19_000, 0),
        ("Liabilities:VAT:Output", 0, 19_000),
    ]
    assert vat_lines(100_000, "reverse_charge", 21_000) == vat_lines(
        100_000, "reverse_charge", 0
    )  # wrongly charged VAT is never posted
    assert vat_lines(100_000, "import", 0) == []
    assert (
        expected_vat_minor(100_000, "domestic") == 19_000
        and expected_vat_minor(100_000, "reverse_charge") == 0
    )
    assert is_vat_wrongly_charged("reverse_charge", 500) and is_vat_wrongly_charged("import", 1)
    assert not is_vat_wrongly_charged("domestic", 500) and not is_vat_wrongly_charged(
        "reverse_charge", 0
    )
