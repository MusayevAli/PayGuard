"""Integer money and locale-aware amount parsing (spec §4).

All amounts are integer minor units. ``Decimal`` is used for scaling/FX math with an
explicit rounding mode; ``float`` never touches money.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from decimal import ROUND_HALF_EVEN, Decimal, InvalidOperation
from typing import Final, Literal

MINOR_DIGITS: Final[dict[str, int]] = {"EUR": 2, "CZK": 2, "PLN": 2, "USD": 2, "JPY": 0}

NumberLocale = Literal["1.234,56", "1 234,56", "1,234.56"]
NUMBER_LOCALES: Final[tuple[NumberLocale, ...]] = ("1.234,56", "1 234,56", "1,234.56")


class CurrencyMismatch(ValueError):
    """Arithmetic or comparison across currencies."""


class UnknownCurrency(ValueError):
    """Currency code outside ``MINOR_DIGITS``."""


def minor_digits(currency: str) -> int:
    try:
        return MINOR_DIGITS[currency]
    except KeyError as exc:
        raise UnknownCurrency(
            f"unknown currency {currency!r}; known: {sorted(MINOR_DIGITS)}"
        ) from exc


@dataclass(frozen=True, slots=True)
class Money:
    minor: int
    currency: str

    def __post_init__(self) -> None:
        if type(self.minor) is not int:
            raise TypeError(f"Money.minor must be an int, got {type(self.minor).__name__}")
        minor_digits(self.currency)

    @property
    def minor_digits(self) -> int:
        return MINOR_DIGITS[self.currency]

    def _same(self, other: object) -> Money:
        if not isinstance(other, Money):
            raise TypeError(f"expected Money, got {type(other).__name__}")
        if other.currency != self.currency:
            raise CurrencyMismatch(f"{self.currency} vs {other.currency}")
        return other

    def __add__(self, other: Money) -> Money:
        return Money(self.minor + self._same(other).minor, self.currency)

    def __sub__(self, other: Money) -> Money:
        return Money(self.minor - self._same(other).minor, self.currency)

    def __neg__(self) -> Money:
        return Money(-self.minor, self.currency)

    def __abs__(self) -> Money:
        return Money(abs(self.minor), self.currency)

    def __mul__(self, k: int) -> Money:
        if type(k) is not int:
            raise TypeError(
                "Money can only be multiplied by an int; use scale() for Decimal factors"
            )
        return Money(self.minor * k, self.currency)

    __rmul__ = __mul__

    def __lt__(self, other: Money) -> bool:
        return self.minor < self._same(other).minor

    def __le__(self, other: Money) -> bool:
        return self.minor <= self._same(other).minor

    def __gt__(self, other: Money) -> bool:
        return self.minor > self._same(other).minor

    def __ge__(self, other: Money) -> bool:
        return self.minor >= self._same(other).minor

    def scale(self, factor: Decimal, rounding: str = ROUND_HALF_EVEN) -> Money:
        """Multiply by a Decimal factor and round to minor units (default banker's rounding)."""
        scaled = (Decimal(self.minor) * factor).quantize(Decimal(1), rounding=rounding)
        return Money(int(scaled), self.currency)

    def as_decimal(self) -> Decimal:
        """Major units as a Decimal (display/export only; never for arithmetic on money)."""
        digits = self.minor_digits
        return Decimal(self.minor).scaleb(-digits).quantize(Decimal(1).scaleb(-digits))

    @classmethod
    def from_decimal(cls, amount: Decimal, currency: str, rounding: str | None = None) -> Money:
        """Build from major units. Without ``rounding`` the amount must be exact in minor units."""
        digits = minor_digits(currency)
        scaled = amount.scaleb(digits)
        if rounding is None:
            if scaled != scaled.to_integral_value():
                raise ValueError(
                    f"{amount} has more precision than {currency} allows ({digits} digits)"
                )
            return cls(int(scaled), currency)
        return cls(int(scaled.quantize(Decimal(1), rounding=rounding)), currency)

    def is_zero(self) -> bool:
        return self.minor == 0


_SPACES = re.compile(r"[     ]+")
_CURRENCY_TOKENS = re.compile(
    r"(?i)(€|\$|¥|£|Kč|zł|円|\bEUR\b|\bCZK\b|\bPLN\b|\bUSD\b|\bJPY\b)",
)
_NUMBER = re.compile(r"^[0-9][0-9 .,]*$")


def _strip_to_number(text: str) -> tuple[str, int] | None:
    """Return (digits-with-separators, sign) or None when the text is not an amount."""
    s = text.replace("−", "-").replace("‒", "-").replace("–", "-")
    s = _CURRENCY_TOKENS.sub(" ", s)
    s = _SPACES.sub(" ", s).strip()
    sign = 1
    if s.startswith("(") and s.endswith(")"):
        sign = -1
        s = s[1:-1].strip()
    if s.startswith("-"):
        sign = -sign
        s = s[1:].strip()
    elif s.endswith("-"):
        sign = -sign
        s = s[:-1].strip()
    if s.startswith("+"):
        s = s[1:].strip()
    if not s or not _NUMBER.match(s):
        return None
    return s, sign


def _grouped_int(part: str, thou: str | None) -> int | None:
    """Validate thousands grouping and return the integer value, or None if malformed."""
    if thou is None or thou not in part:
        return int(part) if part.isdigit() else None
    groups = part.split(thou)
    if not groups[0] or len(groups[0]) > 3 or not groups[0].isdigit():
        return None
    if any(len(g) != 3 or not g.isdigit() for g in groups[1:]):
        return None
    return int("".join(groups))


def _assemble(s: str, dec: str | None, thou: str | None, digits: int) -> int | None:
    if dec is not None and dec in s:
        int_part, frac = s.rsplit(dec, 1)
        if not frac.isdigit() or len(frac) > digits or not int_part:
            return None
        whole = _grouped_int(int_part, thou)
        if whole is None:
            return None
        scale: int = 10**digits
        return whole * scale + int(frac.ljust(digits, "0"))
    for sep in (",", ".", " "):
        if sep != thou and sep in s:
            return None
    whole = _grouped_int(s, thou)
    scale_int: int = 10**digits
    return None if whole is None else whole * scale_int


_HINT_SEPARATORS: Final[dict[str, tuple[str, str]]] = {
    "1.234,56": (",", "."),
    "1 234,56": (",", " "),
    "1,234.56": (".", ","),
}


def _heuristic(s: str, digits: int) -> int | None:
    has_space, has_comma, has_dot = " " in s, "," in s, "." in s
    if has_space:
        if has_comma and has_dot:
            return None
        dec = "," if has_comma else ("." if has_dot else None)
        return _assemble(s, dec, " ", digits)
    if has_comma and has_dot:
        dec = "," if s.rfind(",") > s.rfind(".") else "."
        thou = "." if dec == "," else ","
        return _assemble(s, dec, thou, digits)
    sep = "," if has_comma else ("." if has_dot else None)
    if sep is None:
        return _assemble(s, None, None, digits)
    if s.count(sep) > 1:
        return _assemble(s, None, sep, digits)
    after = s.rsplit(sep, 1)[1]
    if len(after) != 3:
        return _assemble(s, sep, None, digits)
    if digits == 0:
        return _assemble(s, None, sep, digits)
    return None  # "1,234" with a decimal currency: thousands or decimals? ambiguous


def parse_locale_amount(
    text: str, currency: str, locale_hint: NumberLocale | None = None
) -> int | None:
    """Parse a locale-formatted amount into integer minor units.

    ``1.234,56`` / ``1 234,56`` / ``1,234.56`` / ``¥123,456`` are handled, with the supplier's
    locale as an explicit hint and a heuristic fallback. Ambiguous or malformed input returns
    ``None`` so the caller can force a re-extraction instead of guessing.
    """
    digits = minor_digits(currency)
    stripped = _strip_to_number(text)
    if stripped is None:
        return None
    s, sign = stripped
    value: int | None = None
    if locale_hint is not None:
        if locale_hint not in _HINT_SEPARATORS:
            raise ValueError(f"unknown locale hint {locale_hint!r}")
        dec, thou = _HINT_SEPARATORS[locale_hint]
        value = _assemble(s, dec, thou, digits)
    if value is None:
        value = _heuristic(s, digits)
    return None if value is None else sign * value


def decimal_or_none(text: str) -> Decimal | None:
    try:
        d = Decimal(text)
    except InvalidOperation:
        return None
    return d if d.is_finite() else None
