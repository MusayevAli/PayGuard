from __future__ import annotations

from datetime import date

import pytest

from agents.parsed import LocaleHints, ParsedInvoice, ParsedLine
from agents.validate import normalize_currency, parse_date, validate_parsed


def parsed(**overrides) -> ParsedInvoice:
    base = dict(
        supplier_name="Kovak Trim s.r.o.",
        supplier_vat_id="CZ71310983",
        invoice_number="KT-2291",
        invoice_date="12.10.2026",
        due_date="11.11.2026",
        service_date="09.10.2026",
        po_references=["PO-10051"],
        currency="€",
        lines=[
            ParsedLine(
                description="Rám sedadla přední levý",
                sku="SF-1001",
                qty="1 000",
                uom="ks",
                unit_price="13,90",
                line_total="13 900,00",
            )
        ],
        net="13 900,00",
        vat_rate="0 %",
        vat_amount="0,00",
        gross="13 900,00",
        bank_details=None,
        document_language="cs",
    )
    base.update(overrides)
    return ParsedInvoice(**base)


CS = LocaleHints(
    number_locale="1 234,56",
    date_format="DD.MM.YYYY",
    language="cs",
    supplier_id="V-0001",
    currency="EUR",
)


@pytest.mark.parametrize(
    ("notes", "expected_bp"),
    [
        (
            "Odwrotne obciążenie. Stawka 0 % (art. 42 ustawy o VAT). "
            "Kaucja gwarancyjna 10 % wartości faktury, zwalniana po odbiorze końcowym narzędzia.",
            1000,
        ),
        ("VAT 21%; 7,25 % retention until acceptance.", 725),
        ("VAT 0% and 20% retention until acceptance", 2000),
        ("10% retention and VAT 21%", 1000),
        ("Einbehalt 15 %. Skonto 2 %.", 1500),
        ("VAT 21%; retention until acceptance.", 1000),
        ("100% holdback until acceptance", 10000),
        ("VAT 21%; 2% early payment discount", None),
    ],
)
def test_retention_uses_its_clause_not_an_unrelated_vat_or_discount_rate(
    notes: str, expected_bp: int | None
) -> None:
    invoice, errors = validate_parsed(parsed(notes=notes), CS)
    assert errors == [] and invoice is not None
    assert invoice.retention_bp == expected_bp


@pytest.mark.parametrize("notes", ["Retention 110%", "Holdback 7.255%"])
def test_invalid_retention_percentage_holds_validation(notes: str) -> None:
    invoice, errors = validate_parsed(parsed(notes=notes), CS)
    assert invoice is None
    assert any("retention percentage" in error for error in errors)


def test_happy_path_under_the_supplier_locale() -> None:
    ok, errors = validate_parsed(parsed(), CS)
    assert errors == [] and ok is not None
    assert (
        ok.invoice_date == date(2026, 10, 12)
        and ok.due_date == date(2026, 11, 11)
        and ok.currency == "EUR"
    )
    assert ok.net_minor == 1_390_000 and ok.vat_minor == 0 and ok.gross_minor == 1_390_000
    assert (
        ok.lines[0].qty == 1000
        and ok.lines[0].unit_price_minor == 1390
        and ok.lines[0].line_total_minor == 1_390_000
    )
    assert ok.number_locale == "1 234,56" and ok.vat_rate_bp == 0


def test_heuristic_when_no_hints() -> None:
    ok, errors = validate_parsed(
        parsed(
            invoice_date="2026-10-12",
            net="13,900.00",
            gross="13,900.00",
            lines=[
                ParsedLine(
                    description="x",
                    sku=None,
                    qty="1,000",
                    uom="pc",
                    unit_price="13.90",
                    line_total="13,900.00",
                )
            ],
            document_language="en",
        ),
        LocaleHints(
            number_locale=None, date_format=None, language=None, supplier_id=None, currency=None
        ),
    )
    assert (
        errors == []
        and ok is not None
        and ok.net_minor == 1_390_000
        and ok.invoice_date == date(2026, 10, 12)
    )


def test_sum_of_lines_must_equal_net_within_tolerance() -> None:
    ok, errors = validate_parsed(
        parsed(
            lines=[
                ParsedLine(
                    description="x",
                    sku=None,
                    qty="1 000",
                    uom="ks",
                    unit_price="13,90",
                    line_total="13 000,00",
                )
            ]
        ),
        CS,
    )
    assert ok is None and any("net" in e for e in errors)
    ok, errors = validate_parsed(
        parsed(net="13 900,04", gross="13 900,04"), CS
    )  # 4 minor units: inside ROUNDING_TOL_MINOR
    assert errors == [] and ok is not None


def test_net_plus_vat_must_equal_gross() -> None:
    ok, errors = validate_parsed(parsed(gross="14 000,00"), CS)
    assert ok is None and any("gross" in e for e in errors)


def test_bad_dates_currency_and_quantities() -> None:
    assert validate_parsed(parsed(invoice_date="31.02.2026"), CS)[0] is None
    assert validate_parsed(parsed(invoice_date="someday"), CS)[0] is None
    assert validate_parsed(parsed(currency="XYZ"), CS)[0] is None
    assert (
        validate_parsed(
            parsed(
                lines=[
                    ParsedLine(
                        description="x",
                        sku=None,
                        qty="0",
                        uom="ks",
                        unit_price="13,90",
                        line_total="0,00",
                    )
                ],
                net="0,00",
                gross="0,00",
            ),
            CS,
        )[0]
        is None
    )
    assert (
        validate_parsed(
            parsed(
                lines=[
                    ParsedLine(
                        description="x",
                        sku=None,
                        qty="abc",
                        uom="ks",
                        unit_price="13,90",
                        line_total="13 900,00",
                    )
                ]
            ),
            CS,
        )[0]
        is None
    )
    assert validate_parsed(parsed(net="n/a"), CS)[0] is None
    errors = validate_parsed(parsed(net="n/a"), CS)[1]
    assert errors and all(isinstance(e, str) and e for e in errors)


def test_currency_and_date_helpers() -> None:
    assert (
        normalize_currency("€") == "EUR"
        and normalize_currency("Kč") == "CZK"
        and normalize_currency("zł") == "PLN"
    )
    assert (
        normalize_currency("$") == "USD"
        and normalize_currency("¥") == "JPY"
        and normalize_currency("円") == "JPY"
        and normalize_currency("eur") == "EUR"
    )
    assert normalize_currency("GBP") is None
    assert parse_date("12.10.2026", "DD.MM.YYYY") == date(2026, 10, 12)
    assert parse_date("10/12/2026", "MM/DD/YYYY") == date(2026, 10, 12)
    assert parse_date("2026-10-12", "DD.MM.YYYY") == date(
        2026, 10, 12
    )  # other formats are tried after the hint
    assert parse_date("2026年10月12日", None) == date(2026, 10, 12)
    assert parse_date("12. 10. 2026", "DD.MM.YYYY") == date(2026, 10, 12)
    assert parse_date("nonsense", None) is None


def test_credit_note_amounts_may_be_negative() -> None:
    cn = parsed(
        invoice_number="CN-12",
        is_credit_note=True,
        credit_note_for="LB-301",
        lines=[
            ParsedLine(
                description="rebate",
                sku=None,
                qty="1",
                uom="ks",
                unit_price="-807,00",
                line_total="-807,00",
            )
        ],
        net="-807,00",
        vat_amount="0,00",
        gross="-807,00",
    )
    ok, errors = validate_parsed(cn, CS)
    assert (
        errors == []
        and ok is not None
        and ok.gross_minor == -80_700
        and ok.is_credit_note
        and ok.credit_note_for == "LB-301"
    )
