"""Post-validation of extracted invoices in code (spec §7.2): locale parsing, arithmetic,
dates, currency, quantities. Returns a typed invoice or a list of specific errors."""

from __future__ import annotations

import re
from datetime import date, datetime

from core.money import parse_locale_amount
from core.rules import RETENTION_BP, ROUNDING_TOL_MINOR
from sim.models import DateFormat, NumberLocale

from .parsed import LocaleHints, ParsedInvoice, ValidatedInvoice, ValidatedLine

ALLOWED_CURRENCIES = ("EUR", "CZK", "PLN", "USD", "JPY")
_CURRENCY_WORDS: dict[str, str] = {
    "€": "EUR",
    "eur": "EUR",
    "euro": "EUR",
    "kč": "CZK",
    "czk": "CZK",
    "zł": "PLN",
    "zl": "PLN",
    "pln": "PLN",
    "$": "USD",
    "usd": "USD",
    "us$": "USD",
    "¥": "JPY",
    "jpy": "JPY",
    "円": "JPY",
    "yen": "JPY",
}
LOCALE_BY_LANGUAGE: dict[str, NumberLocale] = {
    "de": "1.234,56",
    "cs": "1 234,56",
    "pl": "1 234,56",
    "en": "1,234.56",
    "ja": "1,234.56",
}
DATE_FORMAT_BY_LANGUAGE: dict[str, DateFormat] = {
    "de": "DD.MM.YYYY",
    "cs": "DD.MM.YYYY",
    "pl": "YYYY-MM-DD",
    "en": "MM/DD/YYYY",
    "ja": "YYYY-MM-DD",
}
_STRPTIME: dict[DateFormat, str] = {
    "DD.MM.YYYY": "%d.%m.%Y",
    "YYYY-MM-DD": "%Y-%m-%d",
    "MM/DD/YYYY": "%m/%d/%Y",
}
_JA_DATE = re.compile(r"(\d{4})年\s*(\d{1,2})月\s*(\d{1,2})日")


def normalize_currency(text: str | None) -> str | None:
    if not text:
        return None
    key = text.strip().lower()
    if key.upper() in ALLOWED_CURRENCIES:
        return key.upper()
    return _CURRENCY_WORDS.get(key)


def parse_date(text: str | None, hint: DateFormat | None) -> date | None:
    if not text:
        return None
    s = text.strip()
    m = _JA_DATE.search(s)
    if m:
        try:
            return date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
        except ValueError:
            return None
    s = re.sub(r"\.\s+", ".", s)  # "12. 10. 2026" -> "12.10.2026"
    order: list[DateFormat] = ([hint] if hint else []) + [
        f for f in ("YYYY-MM-DD", "DD.MM.YYYY", "MM/DD/YYYY") if f != hint
    ]
    for fmt in order:
        try:
            return datetime.strptime(s, _STRPTIME[fmt]).date()
        except ValueError:
            continue
    try:
        return date.fromisoformat(s[:10])
    except ValueError:
        return None


def parse_amount(text: str | None, currency: str, locale: NumberLocale | None) -> int | None:
    if text is None:
        return None
    s = text.strip()
    if not s:
        return None
    value = parse_locale_amount(s, currency, locale) if locale else None
    if value is None:
        value = parse_locale_amount(s, currency, None)
    return value


def parse_qty(text: str | None, locale: NumberLocale | None) -> int | None:
    if text is None:
        return None
    s = text.strip().replace(" ", " ")
    s = re.sub(r"[^\d.,\- ]", "", s).strip()
    if not s:
        return None
    value = parse_locale_amount(s, "EUR", locale)
    if value is None:
        value = parse_locale_amount(s, "EUR", None)
    if value is None:
        digits = re.sub(r"\D", "", s)
        return int(digits) if digits else None
    if value % 100:
        return None
    return value // 100


def _vat_rate_bp(text: str | None) -> int | None:
    if not text:
        return None
    m = re.search(r"(\d+(?:[.,]\d+)?)", text)
    if not m:
        return None
    return round(float(m.group(1).replace(",", ".")) * 100)


def validate_parsed(
    parsed: ParsedInvoice, hints: LocaleHints
) -> tuple[ValidatedInvoice | None, list[str]]:
    errors: list[str] = []
    language = hints.language or (
        parsed.document_language if parsed.document_language in LOCALE_BY_LANGUAGE else None
    )
    locale: NumberLocale = hints.number_locale or LOCALE_BY_LANGUAGE.get(language or "", "1,234.56")
    date_format = hints.date_format or (DATE_FORMAT_BY_LANGUAGE.get(language) if language else None)
    currency = normalize_currency(parsed.currency) or (
        hints.currency if parsed.currency.strip() == "" else None
    )
    if currency is None:
        errors.append(f"currency {parsed.currency!r} is not one of {', '.join(ALLOWED_CURRENCIES)}")
        currency = hints.currency or "EUR"
    invoice_date = parse_date(parsed.invoice_date, date_format)
    if invoice_date is None:
        errors.append(
            f"invoice_date {parsed.invoice_date!r} does not parse under {date_format or 'any known format'}"
        )
    due_date = parse_date(parsed.due_date, date_format) if parsed.due_date else None
    if parsed.due_date and due_date is None:
        errors.append(f"due_date {parsed.due_date!r} does not parse")
    service_date = parse_date(parsed.service_date, date_format) if parsed.service_date else None
    net = parse_amount(parsed.net, currency, locale)
    vat = parse_amount(parsed.vat_amount, currency, locale)
    gross = parse_amount(parsed.gross, currency, locale)
    for name, value, raw in (
        ("net", net, parsed.net),
        ("vat_amount", vat, parsed.vat_amount),
        ("gross", gross, parsed.gross),
    ):
        if value is None:
            errors.append(f"{name} {raw!r} is not a {currency} amount under locale {locale!r}")
    lines: list[ValidatedLine] = []
    if not parsed.lines:
        errors.append("no line items")
    for n, ln in enumerate(parsed.lines, start=1):
        qty = parse_qty(ln.qty, locale)
        unit = parse_amount(ln.unit_price, currency, locale)
        total = parse_amount(ln.line_total, currency, locale)
        if qty is None or qty <= 0:
            errors.append(f"line {n}: quantity {ln.qty!r} must be a positive whole number")
        if unit is None:
            errors.append(f"line {n}: unit_price {ln.unit_price!r} is not an amount")
        if total is None:
            errors.append(f"line {n}: line_total {ln.line_total!r} is not an amount")
        if qty is not None and unit is not None and total is not None and qty > 0:
            lines.append(
                ValidatedLine(
                    line_no=n,
                    description=ln.description.strip(),
                    sku=ln.sku.strip() if ln.sku else None,
                    qty=qty,
                    uom=ln.uom,
                    unit_price_minor=unit,
                    line_total_minor=total,
                )
            )
    if net is not None and lines and len(lines) == len(parsed.lines):
        total_lines = sum(ln.line_total_minor for ln in lines)
        if abs(total_lines - net) > ROUNDING_TOL_MINOR:
            errors.append(
                f"sum of line totals {total_lines} differs from net {net} by more than {ROUNDING_TOL_MINOR} minor units"
            )
    if (
        net is not None
        and vat is not None
        and gross is not None
        and abs(net + vat - gross) > ROUNDING_TOL_MINOR
    ):
        errors.append(f"net {net} + VAT {vat} != gross {gross}")
    prepayment = (
        parse_amount(parsed.prepayment_amount, currency, locale)
        if parsed.prepayment_amount
        else None
    )
    if parsed.prepayment_amount and prepayment is None:
        errors.append(f"prepayment_amount {parsed.prepayment_amount!r} is not an amount")
    if errors or net is None or vat is None or gross is None or invoice_date is None:
        return None, errors
    return ValidatedInvoice(
        supplier_name=parsed.supplier_name.strip(),
        supplier_vat_id=parsed.supplier_vat_id.strip() if parsed.supplier_vat_id else None,
        invoice_number=parsed.invoice_number.strip(),
        invoice_date=invoice_date,
        due_date=due_date,
        service_date=service_date,
        po_references=[p for p in parsed.po_references if p and p.strip()],
        currency=currency,
        lines=lines,
        net_minor=net,
        vat_rate_bp=_vat_rate_bp(parsed.vat_rate),
        vat_minor=vat,
        gross_minor=gross,
        bank_details=parsed.bank_details,
        document_language=language or parsed.document_language,
        is_credit_note=parsed.is_credit_note,
        credit_note_for=parsed.credit_note_for,
        payment_terms=parsed.payment_terms,
        prepayment_minor=abs(prepayment) if prepayment is not None else None,
        notes=parsed.notes,
        retention_bp=retention_bp_from(parsed.notes, parsed.payment_terms),
        number_locale=locale,
        date_format=date_format,
        source=parsed.source,
        confidence=dict(parsed.confidence),
    ), []


_RETENTION = re.compile(
    r"(retention|holdback|einbehalt|zádržn|zatrzyman|留保|検収後に支払|kauce|kaucj)", re.I
)
"""Retention clauses as printed: en/de ("Sicherheitseinbehalt"), cs ("Zádržné", "kauce"),
pl ("Kaucja gwarancyjna", "zatrzymanie"), ja ("留保", "…%は検収後に支払い")."""
_PERCENT = re.compile(r"(\d{1,2}(?:[.,]\d)?)\s*%")
_SENTENCE = re.compile(r"(?<=[.。;!?])\s+|[。\n]")


def retention_bp_from(*texts: str | None) -> int | None:
    """Basis points withheld when the document states a retention clause (R15). The percentage
    printed in the same sentence as the clause wins; a percentage elsewhere in the notes (a VAT
    rate such as "stawka 0 %") is not the retention. Without a figure, the contractual default
    ``RETENTION_BP`` applies."""
    for text in texts:
        if not text or not _RETENTION.search(text):
            continue
        for sentence in _SENTENCE.split(text):
            if _RETENTION.search(sentence):
                m = _PERCENT.search(sentence)
                if m:
                    bp = round(float(m.group(1).replace(",", ".")) * 100)
                    if bp > 0:
                        return bp
                    continue
                return RETENTION_BP
        return RETENTION_BP
    return None
