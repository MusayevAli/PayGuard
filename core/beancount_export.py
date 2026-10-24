"""Beancount rendering of the journal (spec §5.2). Non-EUR postings carry ``@@`` EUR totals so
every transaction balances in EUR exactly as the ledger does; written atomically."""

from __future__ import annotations

import os
import tempfile
from collections.abc import Sequence
from decimal import Decimal
from pathlib import Path
from typing import TYPE_CHECKING

from core.accounts import BANK_ACCOUNT_BY_CURRENCY, CHART
from core.money import minor_digits

if TYPE_CHECKING:
    from core.ledger import JournalEntry

OPEN_DATE = "2026-01-01"
OPERATING_CURRENCIES = ("EUR", "CZK", "PLN")


def _amount(minor: int, currency: str) -> str:
    digits = minor_digits(currency)
    value = Decimal(minor).scaleb(-digits)
    return f"{value:.{digits}f} {currency}"


def _quote(text: str) -> str:
    cleaned = text.replace("\\", "\\\\").replace('"', '\\"').replace("\n", " ").replace("\r", " ")
    return f'"{cleaned}"'


def _tag(text: str) -> str:
    safe = "".join(ch if ch.isalnum() or ch in "-_/." else "-" for ch in text)
    return safe or "entry"


def render_beancount(entries: Sequence[JournalEntry]) -> str:
    out: list[str] = [
        'option "title" "payguard"',
        # multi-currency postings carry @@ EUR totals; EUR then needs an explicit tolerance
        'option "inferred_tolerance_default" "EUR:0.005"',
    ]
    out.extend(f'option "operating_currency" "{ccy}"' for ccy in OPERATING_CURRENCIES)
    out.append("")
    bank_currency = {acct: ccy for ccy, acct in BANK_ACCOUNT_BY_CURRENCY.items()}
    for account in CHART:
        constraint = f" {bank_currency[account]}" if account in bank_currency else ""
        out.append(f"{OPEN_DATE} open {account}{constraint}")
    out.append("")
    for entry in entries:
        tags = f" #{_tag(entry.cause_type)}" + (" #reversal" if entry.reverses else "")
        out.append(
            f"{entry.effective_date.isoformat()} * {_quote(entry.description)} ^{entry.id}{tags}"
        )
        out.append(f"  cause_type: {_quote(entry.cause_type)}")
        out.append(f"  cause_id: {_quote(entry.cause_id)}")
        out.append(f"  posted_by: {_quote(entry.posted_by)}")
        out.append(f"  posted_at: {_quote(entry.posted_at.isoformat())}")
        if entry.reverses:
            out.append(f"  reverses: {_quote(entry.reverses)}")
        out.append(f"  evidence: {_quote(' '.join(e.event_id for e in entry.evidence))}")
        for line in entry.lines:
            signed = line.debit_minor - line.credit_minor
            posting = f"  {line.account}  {_amount(signed, line.currency)}"
            if line.currency != "EUR":
                posting += f" @@ {_amount(abs(line.eur_equiv_minor), 'EUR')}"
            out.append(posting)
        out.append("")
    return "\n".join(out) + "\n"


def write_atomic(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp_name, path)
    except BaseException:
        if os.path.exists(tmp_name):
            os.unlink(tmp_name)
        raise
