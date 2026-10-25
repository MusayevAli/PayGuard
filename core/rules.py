"""Rule constants and pure helpers (spec §6). The invoice agent decides; these functions only
compute. Everything is integer minor units; Decimal only for ratios with explicit rounding."""

from __future__ import annotations

import re
from datetime import date
from decimal import ROUND_CEILING, ROUND_HALF_EVEN, Decimal
from typing import Final

from core.money import Money, allocate
from sim.models import PriceList, Uom

PRICE_TOL_BP: Final[int] = 200
ROUNDING_TOL_MINOR: Final[int] = 5
DUP_WINDOW_DAYS: Final[int] = 3
DUP_TOTAL_TOL_BP: Final[int] = 50
WAREHOUSE_WAIT_HOURS: Final[int] = 48
BANK_CHANGE_HOLD_HOURS: Final[int] = 72
FREIGHT_CONFIRM_MINOR: Final[int] = 200000
BUTTON_THRESHOLD_MINOR: Final[int] = 5000000
MATERIALITY_MINOR: Final[int] = 1000000
RETENTION_BP: Final[int] = 1000
VAT_DISPUTE_ESCALATE_EUR_MINOR: Final[int] = 50000
# --- Part 4: signer caps and payment run parameters (EUR-equivalent minor units, spec §10)
ACCOUNT_CAP_EUR_MINOR: Final[int] = 250_000_000
"""Default per-slot cap provisioned for a legitimate account: EUR 2,500,000.00."""
DAILY_CAP_EUR_MINOR: Final[int] = 300_000_000
"""Default daily cap: EUR 3,000,000.00 of signed payments per chip day."""
NONCE_TIMEOUT_DAYS: Final[int] = 1
PAY_LEAD_DAYS: Final[int] = 0
"""A payable joins the run on its due date (value date = due date)."""
DISCOUNT_LEAD_DAYS: Final[int] = 0
"""A discount is taken on its deadline day, the latest value date that still earns it."""
RECON_GRACE_DAYS: Final[int] = 2
"""Unmatched statement lines become a notice packet after this many days."""
INTERCOMPANY_RATE_BP: Final[int] = 400
"""Assumed annual cost of an intercompany draw (4 %), used to quantify T9 options."""
OVERDRAFT_LIMIT_MINOR: Final[dict[str, int]] = {"EUR": 100_000_000, "CZK": 0, "PLN": 0}
"""Agreed overdraft per account (EUR 1,000,000.00 on the EUR account): due payments may use it,
discount payments may not (T9 asks first)."""

BOX50: Final[int] = 50


def variance_bp(invoice_price_minor: int, list_price_minor: int) -> int:
    """|invoice − list| / list in basis points, rounded up so a variance is never understated."""
    if list_price_minor == 0:
        return 0 if invoice_price_minor == 0 else 10_000
    bp = Decimal(abs(invoice_price_minor - list_price_minor)) * 10_000 / Decimal(list_price_minor)
    return int(bp.quantize(Decimal(1), rounding=ROUND_CEILING))


def price_within_tolerance(bp: int) -> bool:
    return bp <= PRICE_TOL_BP


def is_duplicate(
    total_a: int, date_a: date, number_a: str, total_b: int, date_b: date, number_b: str
) -> bool:
    """R4: same vendor is the caller's job; here: totals within 50 bp, dates within 3 days,
    different numbers."""
    if number_a.strip().upper() == number_b.strip().upper():
        return False
    if abs((date_a - date_b).days) > DUP_WINDOW_DAYS:
        return False
    base = max(abs(total_a), abs(total_b))
    if base == 0:
        return total_a == total_b
    diff_bp = Decimal(abs(total_a - total_b)) * 10_000 / Decimal(base)
    return diff_bp <= DUP_TOTAL_TOL_BP


def normalize_uom(qty: int, uom: str, po_uom: Uom) -> tuple[int, str] | None:
    """Express an invoiced quantity in the PO's unit. ``box50 -> 50 pc``; kg and m must match."""
    if uom == po_uom:
        return qty, po_uom
    if uom == "pc" and po_uom == "box50":
        if qty % BOX50:
            return None
        return qty // BOX50, "box50"
    if uom == "box50" and po_uom == "pc":
        return qty * BOX50, "pc"
    return None


def applicable_price_list(
    lists: list[PriceList], on: date
) -> tuple[PriceList | None, list[PriceList]]:
    """The list in force on ``on``; when none (M3 gap), the lists just before and after it."""
    for pl in lists:
        if pl.effective_from <= on and (pl.effective_to is None or on <= pl.effective_to):
            return pl, []
    before = [pl for pl in lists if pl.effective_to is not None and pl.effective_to < on]
    after = [pl for pl in lists if pl.effective_from > on]
    candidates: list[PriceList] = []
    if before:
        candidates.append(max(before, key=lambda p: p.effective_to or on))
    if after:
        candidates.append(min(after, key=lambda p: p.effective_from))
    return None, candidates


def rounding_delta(expected_minor: int, printed_minor: int) -> int | None:
    """printed − expected when within ROUNDING_TOL_MINOR (R12), else None."""
    delta = printed_minor - expected_minor
    return delta if abs(delta) <= ROUNDING_TOL_MINOR else None


def freight_needs_confirmation(eur_equiv_minor: int) -> bool:
    return eur_equiv_minor > FREIGHT_CONFIRM_MINOR


def retention_split(
    gross_minor: int, currency: str = "EUR", bp: int = RETENTION_BP
) -> tuple[int, int]:
    """R15: (due now, retained) = (100 % − bp) / bp, exact to the minor unit (default 90 % / 10 %)."""
    due, retained = allocate(Money(gross_minor, currency), [10_000 - bp, bp])
    return due.minor, retained.minor


_TERMS = re.compile(
    r"(?<![\d.])(\d{1,2})\s*(?:/|%\s*(?:skonto|discount|sleva|rabatt|skonta)?[^\d]{0,40}?)(\d{1,2})\s*(?:dn|days|tag|dni|net|$)",
    re.I,
)
_SIMPLE = re.compile(r"\b(\d{1,2})\s*/\s*(\d{1,2})\b")


def discount_terms(terms: str | None) -> tuple[int, int] | None:
    """``2/10 net30`` or prose like ``2 % Skonto ... 10 Tagen`` → (basis points, days)."""
    if not terms:
        return None
    m = _SIMPLE.search(terms) or _TERMS.search(terms)
    if not m:
        return None
    pct, days = int(m.group(1)), int(m.group(2))
    if pct <= 0 or pct > 10 or days <= 0 or days > 30:
        return None
    return pct * 100, days


def short_pay(qty_billed: int, qty_received: int) -> tuple[int, int]:
    """R1: (quantity to post, balance still expected)."""
    posted = min(qty_billed, qty_received)
    return posted, max(0, qty_billed - posted)


def discount_minor(amount_minor: int, discount_bp: int) -> int:
    """Early-payment discount on ``amount_minor`` at ``discount_bp``, rounded half-even."""
    value = Decimal(amount_minor) * Decimal(discount_bp) / Decimal(10_000)
    return int(value.quantize(Decimal(1), rounding=ROUND_HALF_EVEN))


def financing_cost_minor(principal_minor: int, rate_bp: int, days: int) -> int:
    """Simple interest for ``days`` at ``rate_bp`` per year, rounded half-even."""
    if days <= 0 or principal_minor <= 0:
        return 0
    value = (
        Decimal(principal_minor) * Decimal(rate_bp) / Decimal(10_000) * Decimal(days) / Decimal(365)
    )
    return int(value.quantize(Decimal(1), rounding=ROUND_HALF_EVEN))


def is_fee_line(txn_type: str) -> bool:
    return txn_type == "NCHG"


def is_fx_line(txn_type: str) -> bool:
    return txn_type == "NFEX"


_REF_TOKEN = re.compile(r"[A-Za-z0-9][A-Za-z0-9/\-]{3,}")


def references_in(remittance: str) -> list[str]:
    """Document-like tokens in a remittance text (invoice numbers, batch and payment ids)."""
    out: list[str] = []
    for token in _REF_TOKEN.findall(remittance.replace("/REMI/", " ").replace("/ORDP/", " ")):
        if any(ch.isdigit() for ch in token) and token not in out:
            out.append(token)
    return out
