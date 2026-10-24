"""Locale formatting for documents: numbers, money, dates, quantities (spec §2.2, A1)."""

from __future__ import annotations

from datetime import date

from core.money import MINOR_DIGITS
from sim.models import DateFormat, Language, NumberLocale

CURRENCY_SYMBOL: dict[str, str] = {"EUR": "€", "CZK": "Kč", "PLN": "zł", "USD": "$", "JPY": "¥"}
_SEPARATORS: dict[NumberLocale, tuple[str, str]] = {
    "1.234,56": (".", ","),
    "1 234,56": (" ", ","),
    "1,234.56": (",", "."),
}


def group_thousands(digits: str, sep: str) -> str:
    out: list[str] = []
    while len(digits) > 3:
        out.insert(0, digits[-3:])
        digits = digits[:-3]
    out.insert(0, digits)
    return sep.join(out)


def format_number(
    minor: int, currency: str, locale: NumberLocale, *, decimals: int | None = None
) -> str:
    digits = MINOR_DIGITS[currency] if decimals is None else decimals
    thou, dec = _SEPARATORS[locale]
    sign = "-" if minor < 0 else ""
    whole, frac = divmod(abs(minor), 10**digits)
    text = group_thousands(str(whole), thou)
    if digits:
        text += f"{dec}{frac:0{digits}d}"
    return sign + text


def format_money(minor: int, currency: str, locale: NumberLocale, language: Language) -> str:
    number = format_number(minor, currency, locale)
    symbol = CURRENCY_SYMBOL[currency]
    if currency == "USD":
        return f"-${number[1:]}" if minor < 0 else f"${number}"
    if currency == "JPY":
        return f"-¥{number[1:]}" if minor < 0 else f"¥{number}"
    if currency == "EUR" and language == "en":
        return f"-€{number[1:]}" if minor < 0 else f"€{number}"
    return f"{number} {symbol}"


def format_date(d: date, fmt: DateFormat) -> str:
    if fmt == "DD.MM.YYYY":
        return f"{d.day:02d}.{d.month:02d}.{d.year}"
    if fmt == "YYYY-MM-DD":
        return d.isoformat()
    return f"{d.month:02d}/{d.day:02d}/{d.year}"


def format_qty(qty: int, locale: NumberLocale) -> str:
    thou, _ = _SEPARATORS[locale]
    return group_thousands(str(qty), thou)


def format_rate_bp(rate_bp: int, language: Language) -> str:
    pct = rate_bp // 100 if rate_bp % 100 == 0 else rate_bp / 100
    if language in ("cs", "de"):
        return f"{pct} %"
    return f"{pct}%"
