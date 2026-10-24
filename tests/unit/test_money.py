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


# ---------------------------------------------------------------------------------------
# Part 2: convert() and allocate()
# ---------------------------------------------------------------------------------------

from datetime import date  # noqa: E402

from core.money import FxRateUnavailable, allocate, convert  # noqa: E402
from sim.fx import write_fx_rates  # noqa: E402
from tests.conftest import synthetic_fx  # noqa: E402


def test_convert_uses_the_requested_date_and_direction() -> None:
    fx = synthetic_fx()
    r = convert(Money(243450, "CZK"), "EUR", date(2026, 10, 1), fx)
    assert r.money == Money(10000, "EUR")
    assert r.rate_date == date(2026, 10, 1)
    assert r.from_rate == Decimal("24.3450") and r.to_rate is None
    assert r.rate == Decimal(1) / Decimal("24.3450")
    r2 = convert(Money(243450, "CZK"), "EUR", date(2026, 10, 2), fx)
    assert r2.money == Money(9937, "EUR")  # 2434.50 / 24.5 = 99.3673...
    assert convert(Money(10000, "EUR"), "CZK", date(2026, 10, 1), fx).money == Money(243450, "CZK")
    cross = convert(Money(243450, "CZK"), "PLN", date(2026, 10, 1), fx)
    assert cross.money == Money(42680, "PLN")
    assert cross.from_rate == Decimal("24.3450") and cross.to_rate == Decimal("4.2680")
    same = convert(Money(5, "EUR"), "EUR", date(2026, 10, 1), fx)
    assert same.money == Money(5, "EUR") and same.rate == Decimal(1) and same.from_rate is None


def test_convert_rounds_half_even() -> None:
    fx = synthetic_fx()  # 3 Oct: 4 CZK per EUR
    assert convert(Money(10, "CZK"), "EUR", date(2026, 10, 3), fx).money == Money(
        2, "EUR"
    )  # 2.5 -> 2
    assert convert(Money(30, "CZK"), "EUR", date(2026, 10, 3), fx).money == Money(
        8, "EUR"
    )  # 7.5 -> 8
    assert convert(Money(50, "CZK"), "EUR", date(2026, 10, 3), fx).money == Money(
        12, "EUR"
    )  # 12.5 -> 12


def test_convert_jpy_has_no_minor_unit() -> None:
    fx = synthetic_fx()
    assert convert(Money(10000, "EUR"), "JPY", date(2026, 10, 1), fx).money == Money(16230, "JPY")
    assert convert(Money(1, "JPY"), "EUR", date(2026, 10, 1), fx).money == Money(1, "EUR")
    assert convert(Money(0, "JPY"), "EUR", date(2026, 10, 1), fx).money == Money(0, "EUR")


def test_convert_missing_date_or_rate_raises_instead_of_guessing() -> None:
    fx = synthetic_fx()
    with pytest.raises(FxRateUnavailable, match="2026-12-01"):
        convert(Money(1, "CZK"), "EUR", date(2026, 12, 1), fx)
    with pytest.raises(FxRateUnavailable, match="USD"):
        convert(Money(1, "CZK"), "USD", date(2026, 10, 9), fx)  # no USD that day
    with pytest.raises(FxRateUnavailable, match="2026-10-04"):
        convert(Money(1, "EUR"), "CZK", date(2026, 10, 4), fx)  # day absent from the table


def test_convert_default_table_comes_from_env_or_world(tmp_path, monkeypatch) -> None:
    path = tmp_path / "fx_rates.json"
    write_fx_rates(synthetic_fx(), path)
    monkeypatch.setenv("PAYGUARD_FX_RATES", str(path))
    assert convert(Money(243450, "CZK"), "EUR", date(2026, 10, 1)).money == Money(10000, "EUR")
    monkeypatch.setenv("PAYGUARD_FX_RATES", str(tmp_path / "missing.json"))
    with pytest.raises(FxRateUnavailable, match=r"missing\.json"):
        convert(Money(1, "CZK"), "EUR", date(2026, 10, 1))


def test_allocate_largest_remainder_is_exact_and_deterministic() -> None:
    assert allocate(Money(100, "EUR"), [1, 1, 1]) == [
        Money(34, "EUR"),
        Money(33, "EUR"),
        Money(33, "EUR"),
    ]
    assert allocate(Money(1000, "EUR"), [90, 10]) == [Money(900, "EUR"), Money(100, "EUR")]
    assert allocate(Money(-1000, "EUR"), [90, 10]) == [Money(-900, "EUR"), Money(-100, "EUR")]
    assert allocate(Money(5, "EUR"), [1, 2, 3]) == [
        Money(1, "EUR"),
        Money(2, "EUR"),
        Money(2, "EUR"),
    ]
    assert allocate(Money(1, "EUR"), [1, 1]) == [
        Money(1, "EUR"),
        Money(0, "EUR"),
    ]  # tie -> earlier index
    assert allocate(Money(7, "JPY"), [0, 1]) == [Money(0, "JPY"), Money(7, "JPY")]
    assert allocate(Money(0, "CZK"), [3, 5]) == [Money(0, "CZK"), Money(0, "CZK")]
    assert allocate(Money(1000, "EUR"), [Decimal("0.9"), Decimal("0.1")]) == [
        Money(900, "EUR"),
        Money(100, "EUR"),
    ]


def test_allocate_random_splits_sum_exactly() -> None:
    import random

    rng = random.Random(2026)
    for _ in range(50):
        total = rng.randrange(-5_000_000, 5_000_000)
        n = rng.randrange(1, 9)
        weights = [rng.randrange(0, 1000) for _ in range(n)]
        if sum(weights) == 0:
            weights[0] = 1
        parts = allocate(Money(total, "CZK"), weights)
        assert len(parts) == n
        assert sum(p.minor for p in parts) == total
        assert all(p.currency == "CZK" for p in parts)
        assert all(p.minor == 0 for p, w in zip(parts, weights, strict=True) if w == 0)
        assert allocate(Money(total, "CZK"), weights) == parts


def test_allocate_rejects_bad_weights() -> None:
    for bad in ([], [0, 0], [-1, 2]):
        with pytest.raises(ValueError):
            allocate(Money(10, "EUR"), bad)
