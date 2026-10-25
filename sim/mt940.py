"""MT940 statement writer and parser (spec §2.7, §11).

The writer produces SWIFT-shaped text (``:20:``, ``:25:``, ``:28C:``, ``:60F:``, ``:61:``,
``:86:``, ``:62F:``) with CRLF line ends; remittance text is truncated to 140 characters and
wrapped at 65 characters per ``:86:`` continuation line. ``parse_mt940(write_mt940(s)) == s``.
"""

from __future__ import annotations

import re
from datetime import date, datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from core.money import minor_digits

REMITTANCE_MAX = 140
LINE_86_WIDTH = 65
CRLF = "\r\n"


class MT940Error(ValueError):
    """Malformed or inconsistent MT940 text."""


def truncate_remittance(text: str) -> str:
    return text[:REMITTANCE_MAX]


class StatementLine(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    value_date: date
    entry_date: date | None
    dc: Literal["D", "C"]
    amount_minor: int = Field(ge=0)
    txn_type: str = Field(pattern=r"^[A-Z0-9]{4}$")
    customer_ref: str = Field(min_length=1, max_length=16, pattern=r"^[A-Za-z0-9./ -]+$")
    bank_ref: str | None = Field(default=None, max_length=16, pattern=r"^[A-Za-z0-9./-]+$")
    remittance: str

    @field_validator("remittance")
    @classmethod
    def _truncate(cls, value: str) -> str:
        if CRLF in value or "\n" in value:
            raise ValueError("remittance must be a single line")
        return truncate_remittance(value)

    @field_validator("customer_ref")
    @classmethod
    def _no_double_slash(cls, value: str) -> str:
        if "//" in value:
            raise ValueError("customer reference must not contain '//'")
        return value


class Statement(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    statement_id: str = Field(min_length=1, max_length=16, pattern=r"^[A-Za-z0-9./-]+$")
    account_iban: str = Field(min_length=5, max_length=35)
    currency: str
    statement_date: date
    sequence_no: int = Field(ge=1, le=99999)
    opening_balance_minor: int
    lines: list[StatementLine]
    closing_balance_minor: int

    @model_validator(mode="after")
    def _balanced(self) -> Statement:
        minor_digits(self.currency)
        if self.compute_closing() != self.closing_balance_minor:
            raise ValueError(
                f"closing balance {self.closing_balance_minor} does not equal opening plus lines"
                f" ({self.compute_closing()})"
            )
        return self

    def compute_closing(self) -> int:
        total = self.opening_balance_minor
        for ln in self.lines:
            total += ln.amount_minor if ln.dc == "C" else -ln.amount_minor
        return total


def _fmt_amount(minor: int, currency: str) -> str:
    digits = minor_digits(currency)
    if digits == 0:
        return f"{minor},"
    whole, frac = divmod(minor, 10**digits)
    return f"{whole},{frac:0{digits}d}"


def _parse_amount(text: str, currency: str) -> int:
    digits = minor_digits(currency)
    if "," not in text:
        raise MT940Error(f"amount {text!r} lacks the decimal comma")
    whole, frac = text.split(",", 1)
    if not whole.isdigit() or (frac and not frac.isdigit()):
        raise MT940Error(f"bad amount {text!r}")
    if len(frac) > digits:
        raise MT940Error(f"amount {text!r} has more decimals than {currency} allows")
    scale = int(10**digits)
    return int(whole) * scale + (int(frac.ljust(digits, "0")) if digits else 0)


def _fmt_balance(minor: int, on: date, currency: str) -> str:
    sign = "C" if minor >= 0 else "D"
    return f"{sign}{on:%y%m%d}{currency}{_fmt_amount(abs(minor), currency)}"


_BALANCE_RE = re.compile(r"^([CD])(\d{6})([A-Z]{3})(\d+,\d*)$")
_61_RE = re.compile(
    r"^(?P<value>\d{6})(?P<entry>\d{4})?(?P<dc>[CD])(?P<amount>\d+,\d*)"
    r"(?P<type>[A-Z0-9]{4})(?P<cref>[^/\r\n]{1,16}?)(?://(?P<bref>[A-Za-z0-9./-]{1,16}))?$"
)


def write_mt940(stmt: Statement) -> str:
    out: list[str] = [
        f":20:{stmt.statement_id}",
        f":25:{stmt.account_iban}",
        f":28C:{stmt.sequence_no:05d}/001",
        f":60F:{_fmt_balance(stmt.opening_balance_minor, stmt.statement_date, stmt.currency)}",
    ]
    for ln in stmt.lines:
        entry = f"{ln.entry_date:%m%d}" if ln.entry_date is not None else ""
        line61 = (
            f":61:{ln.value_date:%y%m%d}{entry}{ln.dc}{_fmt_amount(ln.amount_minor, stmt.currency)}"
            f"{ln.txn_type}{ln.customer_ref}"
        )
        if ln.bank_ref is not None:
            line61 += f"//{ln.bank_ref}"
        out.append(line61)
        text = truncate_remittance(ln.remittance)
        chunks = [text[i : i + LINE_86_WIDTH] for i in range(0, len(text), LINE_86_WIDTH)] or [""]
        out.append(f":86:{chunks[0]}")
        out.extend(chunks[1:])
    out.append(
        f":62F:{_fmt_balance(stmt.closing_balance_minor, stmt.statement_date, stmt.currency)}"
    )
    out.append("-")
    return CRLF.join(out) + CRLF


def _yymmdd(text: str) -> date:
    return datetime.strptime(text, "%y%m%d").date()


def parse_mt940(text: str) -> Statement:
    raw_lines = text.replace("\r\n", "\n").split("\n")
    if raw_lines and raw_lines[-1] == "":
        raw_lines.pop()
    if not raw_lines or not raw_lines[0].startswith(":20:"):
        raise MT940Error("statement must start with :20:")
    fields: list[tuple[str, str]] = []
    for raw in raw_lines:
        if raw == "-":
            fields.append(("-", ""))
        elif raw.startswith(":"):
            tag, _, value = raw[1:].partition(":")
            fields.append((tag, value))
        elif fields and fields[-1][0] == "86":
            tag, value = fields[-1]
            fields[-1] = (tag, value + raw)
        else:
            raise MT940Error(f"unexpected continuation line {raw!r}")
    head = {tag: value for tag, value in fields if tag in {"20", "25", "28C", "60F", "62F"}}
    for required in ("20", "25", "28C", "60F", "62F"):
        if required not in head:
            raise MT940Error(f"missing :{required}:")
    opening = _BALANCE_RE.match(head["60F"])
    closing = _BALANCE_RE.match(head["62F"])
    if opening is None or closing is None:
        raise MT940Error("bad :60F:/:62F: balance")
    currency = opening.group(3)
    if closing.group(3) != currency:
        raise MT940Error("currency differs between opening and closing balance")
    statement_date = _yymmdd(opening.group(2))

    def balance(m: re.Match[str]) -> int:
        value = _parse_amount(m.group(4), currency)
        return value if m.group(1) == "C" else -value

    lines: list[StatementLine] = []
    pending: re.Match[str] | None = None
    for tag, value in fields:
        if tag == "61":
            if pending is not None:
                raise MT940Error(":61: without :86:")
            pending = _61_RE.match(value)
            if pending is None:
                raise MT940Error(f"bad :61: line {value!r}")
        elif tag == "86":
            if pending is None:
                raise MT940Error(":86: without :61:")
            entry = pending.group("entry")
            year = _yymmdd(pending.group("value")).year
            entry_date = datetime.strptime(f"{year}{entry}", "%Y%m%d").date() if entry else None
            lines.append(
                StatementLine(
                    value_date=_yymmdd(pending.group("value")),
                    entry_date=entry_date,
                    dc=pending.group("dc"),  # type: ignore[arg-type]
                    amount_minor=_parse_amount(pending.group("amount"), currency),
                    txn_type=pending.group("type"),
                    customer_ref=pending.group("cref"),
                    bank_ref=pending.group("bref"),
                    remittance=value,
                )
            )
            pending = None
    if pending is not None:
        raise MT940Error("trailing :61: without :86:")
    seq_text = head["28C"].split("/", 1)[0]
    if not seq_text.isdigit():
        raise MT940Error("bad :28C:")
    try:
        return Statement(
            statement_id=head["20"],
            account_iban=head["25"],
            currency=currency,
            statement_date=statement_date,
            sequence_no=int(seq_text),
            opening_balance_minor=balance(opening),
            lines=lines,
            closing_balance_minor=balance(closing),
        )
    except ValueError as exc:
        raise MT940Error(str(exc)) from exc
