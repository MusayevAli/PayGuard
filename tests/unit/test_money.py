from __future__ import annotations

from decimal import ROUND_HALF_EVEN, Decimal

import pytest

from core.money import MINOR_DIGITS, CurrencyMismatch, Money, parse_locale_amount


def test_minor_digits_table() -> None:
    assert MINOR_DIGITS == {"EUR": 2, "CZK": 2, "PLN": 2, "USD": 2, "JPY": 0}


def test_money_arithmetic_same_currency() -> None:
    a = Money(1000, "EUR")
    b = Money(250, "EUR")
    assert a + b == Money(1250, "EUR")
    assert a - b == Money(750, "EUR")
    assert -b == Money(-250, "EUR")
    assert abs(Money(-5, "EUR")) == Money(5, "EUR")
    assert a * 3 == Money(3000, "EUR")
    assert b < a and a > b and a >= a and b <= b
    assert sum([a, b], Money(0, "EUR")) == Money(1250, "EUR")


def test_money_rejects_cross_currency_and_floats() -> None:
    with pytest.raises(CurrencyMismatch):
        Money(1, "EUR") + Money(1, "CZK")
    with pytest.raises(CurrencyMismatch):
        _ = Money(1, "EUR") < Money(1, "PLN")
    with pytest.raises(TypeError):
        Money(1.5, "EUR")  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        Money(1, "EUR") * 1.5  # type: ignore[operator]
    with pytest.raises(ValueError):
        Money(1, "XXX")


def test_money_jpy_has_no_minor_unit() -> None:
    y = Money(123456, "JPY")
    assert y.minor_digits == 0
    assert y.as_decimal() == Decimal("123456")
    assert Money.from_decimal(Decimal("123456"), "JPY") == y
    with pytest.raises(ValueError):
        Money.from_decimal(
            Decimal("12.5"), "JPY"
        )  # no rounding mode given: sub-unit precision is an error
    assert Money.from_decimal(Decimal("12.5"), "JPY", rounding=ROUND_HALF_EVEN) == Money(12, "JPY")
    assert Money(1000, "EUR").as_decimal() == Decimal("10.00")
    with pytest.raises(ValueError):
        Money.from_decimal(Decimal("10.005"), "EUR")
    assert Money.from_decimal(Decimal("10.005"), "EUR", rounding=ROUND_HALF_EVEN) == Money(
        1000, "EUR"
    )
    assert Money.from_decimal(Decimal("10.015"), "EUR", rounding=ROUND_HALF_EVEN) == Money(
        1002, "EUR"
    )


def test_money_scale_uses_half_even() -> None:
    assert Money(1000, "EUR").scale(Decimal("0.125")) == Money(125, "EUR")
    assert Money(1001, "EUR").scale(Decimal("0.5")) == Money(500, "EUR")  # 500.5 -> 500
    assert Money(1003, "EUR").scale(Decimal("0.5")) == Money(502, "EUR")  # 501.5 -> 502


@pytest.mark.parametrize(
    ("text", "currency", "hint", "expected"),
    [
        ("1.234,56", "EUR", "1.234,56", 123456),
        ("1 234,56", "CZK", "1 234,56", 123456),
        ("1 234,56", "CZK", "1 234,56", 123456),
        ("1 234,56", "PLN", "1 234,56", 123456),
        ("1,234.56", "USD", "1,234.56", 123456),
        ("¥123,456", "JPY", None, 123456),
        ("¥123,456", "JPY", "1,234.56", 123456),
        ("123,456 円", "JPY", "1,234.56", 123456),
        ("1 234 567,89 Kč", "CZK", "1 234,56", 123456789),
        ("€ 1.234,56", "EUR", "1.234,56", 123456),
        ("1.234,56 €", "EUR", None, 123456),
        ("EUR 1,234.56", "EUR", None, 123456),
        ("12,5", "EUR", "1.234,56", 1250),
        ("12.5", "USD", "1,234.56", 1250),
        ("-1.234,56", "EUR", "1.234,56", -123456),
        ("−1 234,56", "CZK", "1 234,56", -123456),
        ("(1,234.56)", "USD", "1,234.56", -123456),
        ("1,234", "EUR", "1,234.56", 123400),
        ("1.234", "EUR", "1.234,56", 123400),
        ("1 234", "CZK", "1 234,56", 123400),
        ("0,05", "EUR", "1.234,56", 5),
        ("1234", "EUR", None, 123400),
        ("1234.5", "EUR", None, 123450),
        ("1,50", "EUR", None, 150),
        ("12 345,00 zł", "PLN", None, 1234500),
    ],
)
def test_parse_locale_amount_known_cases(text: str, currency: str, hint, expected: int) -> None:
    assert parse_locale_amount(text, currency, hint) == expected


@pytest.mark.parametrize(
    ("text", "currency", "hint"),
    [
        ("1,234", "EUR", None),  # thousands or decimal? ambiguous without a hint
        ("1.234", "EUR", None),
        ("abc", "EUR", "1.234,56"),
        ("", "EUR", "1.234,56"),
        ("1.234,567", "EUR", "1.234,56"),  # too many decimals
        ("123,45", "JPY", "1,234.56"),  # JPY has no minor unit and 45 is not a group of three
        ("12,34.56", "USD", "1,234.56"),  # malformed grouping
        ("1.23.4", "EUR", "1.234,56"),
    ],
)
def test_parse_locale_amount_ambiguous_or_invalid_returns_none(
    text: str, currency: str, hint
) -> None:
    assert parse_locale_amount(text, currency, hint) is None
