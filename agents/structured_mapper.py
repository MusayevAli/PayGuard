"""Structured (EDI/portal JSON) invoices never go to the LLM: a multilingual field dictionary
maps supplier-specific keys (``Menge``, ``Množství``, ``Ilość`` ...) onto ``ParsedInvoice``.
Returns None when a required field cannot be mapped (the caller falls back to extraction)."""

from __future__ import annotations

import re
from typing import Any

from .parsed import LocaleHints, ParsedBankDetails, ParsedInvoice, ParsedLine

FIELD_SYNONYMS: dict[str, tuple[str, ...]] = {
    "invoice_number": (
        "invoice_number",
        "Rechnungsnummer",
        "Číslo faktury",
        "Numer faktury",
        "請求書番号",
        "invoice no",
        "number",
    ),
    "invoice_date": (
        "invoice_date",
        "Rechnungsdatum",
        "Datum vystavení",
        "Data wystawienia",
        "発行日",
        "date",
    ),
    "due_date": (
        "due_date",
        "Fälligkeitsdatum",
        "Datum splatnosti",
        "Termin płatności",
        "支払期限",
        "Zahlungsziel",
    ),
    "service_date": ("service_date", "Leistungsdatum", "DUZP", "Data sprzedaży", "納品日"),
    "po_reference": (
        "po_number",
        "Bestellnummer",
        "Číslo objednávky",
        "Numer zamówienia",
        "注文番号",
        "po_reference",
        "order_number",
    ),
    "currency": ("currency", "Währung", "Měna", "Waluta", "通貨"),
    "net": ("subtotal", "Nettobetrag", "Základ daně", "Razem netto", "小計", "net", "net_total"),
    "vat_rate": ("tax_rate", "MwSt-Satz", "Sazba DPH", "税率", "vat_rate"),
    "vat": ("tax", "MwSt-Betrag", "DPH", "Razem VAT", "消費税", "vat", "vat_amount"),
    "gross": (
        "total",
        "Bruttobetrag",
        "Celkem k úhradě",
        "Razem brutto",
        "合計",
        "gross",
        "amount_due",
    ),
    "terms": ("terms", "Zahlungsbedingungen", "Platební podmínky", "支払条件", "payment_terms"),
    "notes": ("notes", "Hinweis", "Poznámka", "Uwagi", "備考"),
    "document_type": ("document_type", "Belegart", "Typ dokladu", "Rodzaj"),
    "lines": ("line_items", "Positionen", "Položky", "Pozycje", "明細", "lines", "items"),
    "supplier": ("vendor", "Lieferant", "Dodavatel", "Sprzedawca", "発行者", "supplier", "seller"),
    "bank": ("remit_to", "Bankverbindung", "Bankovní spojení", "振込先", "bank_details", "bank"),
    "account_number_top": ("Numer rachunku", "IBAN", "iban"),
    "bank_name_top": ("Bank", "Banka"),
}
LINE_SYNONYMS: dict[str, tuple[str, ...]] = {
    "description": (
        "description",
        "Bezeichnung",
        "Popis",
        "Nazwa",
        "品名",
        "name",
        "item_description",
    ),
    "sku": ("sku", "Artikelnummer", "Kód", "Kod", "品番", "item", "part_no", "article"),
    "qty": ("quantity", "Menge", "Množství", "Ilość", "数量", "qty"),
    "uom": ("unit", "Einheit", "MJ", "Jm", "単位", "uom"),
    "unit_price": ("unit_price", "Einzelpreis", "Cena za MJ", "Cena netto", "単価", "price"),
    "line_total": (
        "amount",
        "Gesamt",
        "Celkem bez DPH",
        "Wartość netto",
        "金額",
        "total",
        "line_total",
        "net",
    ),
}
SUPPLIER_SYNONYMS: dict[str, tuple[str, ...]] = {
    "name": ("name", "Name", "Název", "Nazwa", "名称"),
    "vat_id": ("tax_id", "USt-IdNr", "DIČ", "NIP", "登録番号", "vat_id", "vat"),
}
BANK_SYNONYMS: dict[str, tuple[str, ...]] = {
    "iban": ("iban", "IBAN"),
    "bic": ("bic", "BIC", "SWIFT", "swift"),
    "bank_name": ("bank", "Bank", "Banka", "銀行", "bank_name"),
    "account_number": ("account_number", "口座番号", "account"),
    "routing_number": ("routing_number", "aba"),
    "branch_code": ("支店", "branch"),
}
LANGUAGE_OF_KEY: dict[str, str] = {
    "Rechnungsnummer": "de",
    "Číslo faktury": "cs",
    "Numer faktury": "pl",
    "請求書番号": "ja",
    "invoice_number": "en",
}
_CREDIT = re.compile(r"credit|gutschrift|dobropis|koryguj|訂正", re.I)
_APPLIES_TO = re.compile(
    r"(?:invoice|faktur[aeyę]|rechnung|請求書)\s*(?:č\.|nr\.?|no\.?|number|#)?\s*:?\s*([A-Z]{1,4}-\d{2,6}[A-Z]?|[A-Z0-9][\w/.-]*\d)",
    re.I,
)
_PREPAYMENT = re.compile(
    r"(?:prepayment|anzahlung|záloh|zaliczk|前払)[^\d-]{0,60}?(-?[\d][\d .,]*\d)", re.I
)
_REF_SPLIT = re.compile(r"[;,]|\s{2,}")


def _norm_key(key: str) -> str:
    return re.sub(r"[\s_\-.:]", "", key).lower()


def _get(d: dict[str, Any], field: str, synonyms: dict[str, tuple[str, ...]]) -> Any:
    wanted = {_norm_key(k) for k in synonyms[field]}
    for key, value in d.items():
        if _norm_key(str(key)) in wanted:
            return value
    return None


def _text(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, bool):
        return None
    if isinstance(value, int | float):
        return str(value)
    s = str(value).strip()
    return s or None


def _language(doc: dict[str, Any], hints: LocaleHints) -> str:
    if hints.language:
        return hints.language
    for key in doc:
        if key in LANGUAGE_OF_KEY:
            return LANGUAGE_OF_KEY[key]
    return "en"


def map_structured(doc: dict[str, Any], hints: LocaleHints) -> ParsedInvoice | None:
    supplier_raw = _get(doc, "supplier", FIELD_SYNONYMS)
    supplier = supplier_raw if isinstance(supplier_raw, dict) else {}
    name = _text(_get(supplier, "name", SUPPLIER_SYNONYMS)) if supplier else _text(supplier_raw)
    vat_id = _text(_get(supplier, "vat_id", SUPPLIER_SYNONYMS)) if supplier else None
    number = _text(_get(doc, "invoice_number", FIELD_SYNONYMS))
    inv_date = _text(_get(doc, "invoice_date", FIELD_SYNONYMS))
    currency = _text(_get(doc, "currency", FIELD_SYNONYMS)) or hints.currency
    net = _text(_get(doc, "net", FIELD_SYNONYMS))
    vat = _text(_get(doc, "vat", FIELD_SYNONYMS))
    gross = _text(_get(doc, "gross", FIELD_SYNONYMS))
    raw_lines = _get(doc, "lines", FIELD_SYNONYMS)
    if not (
        name
        and number
        and inv_date
        and currency
        and net is not None
        and vat is not None
        and gross
        and isinstance(raw_lines, list)
        and raw_lines
    ):
        return None
    lines: list[ParsedLine] = []
    for raw in raw_lines:
        if not isinstance(raw, dict):
            return None
        qty = _text(_get(raw, "qty", LINE_SYNONYMS))
        unit_price = _text(_get(raw, "unit_price", LINE_SYNONYMS))
        total = _text(_get(raw, "line_total", LINE_SYNONYMS))
        desc = _text(_get(raw, "description", LINE_SYNONYMS)) or ""
        if qty is None or unit_price is None or total is None:
            return None
        lines.append(
            ParsedLine(
                description=desc,
                sku=_text(_get(raw, "sku", LINE_SYNONYMS)),
                qty=qty,
                uom=_text(_get(raw, "uom", LINE_SYNONYMS)),
                unit_price=unit_price,
                line_total=total,
            )
        )
    po_raw = _text(_get(doc, "po_reference", FIELD_SYNONYMS))
    po_refs = [p.strip() for p in _REF_SPLIT.split(po_raw) if p.strip()] if po_raw else []
    bank_raw = _get(doc, "bank", FIELD_SYNONYMS)
    bank: ParsedBankDetails | None = None
    if isinstance(bank_raw, dict):
        fields = {k: _text(_get(bank_raw, k, BANK_SYNONYMS)) for k in BANK_SYNONYMS}
        if any(fields.values()):
            bank = ParsedBankDetails(**fields)
    if bank is None:
        acct = _text(_get(doc, "account_number_top", FIELD_SYNONYMS))
        if acct:
            bank = ParsedBankDetails(
                iban=acct if acct[:2].isalpha() else None,
                account_number=None if acct[:2].isalpha() else acct,
                bank_name=_text(_get(doc, "bank_name_top", FIELD_SYNONYMS)),
            )
    doc_type = _text(_get(doc, "document_type", FIELD_SYNONYMS)) or ""
    notes = _text(_get(doc, "notes", FIELD_SYNONYMS)) or ""
    is_credit = bool(_CREDIT.search(doc_type))
    credit_for = None
    if is_credit:
        m = _APPLIES_TO.search(notes)
        credit_for = m.group(1) if m else None
    prepay = None
    m = _PREPAYMENT.search(notes)
    if m:
        prepay = m.group(1).strip()
    return ParsedInvoice(
        supplier_name=name,
        supplier_vat_id=vat_id,
        invoice_number=number,
        invoice_date=inv_date,
        due_date=_text(_get(doc, "due_date", FIELD_SYNONYMS)),
        service_date=_text(_get(doc, "service_date", FIELD_SYNONYMS)),
        po_references=po_refs,
        currency=currency,
        lines=lines,
        net=net,
        vat_rate=_text(_get(doc, "vat_rate", FIELD_SYNONYMS)),
        vat_amount=vat,
        gross=gross,
        bank_details=bank,
        document_language=_language(doc, hints),
        is_credit_note=is_credit,
        credit_note_for=credit_for,
        payment_terms=_text(_get(doc, "terms", FIELD_SYNONYMS)),
        prepayment_amount=prepay,
        notes=notes or None,
        confidence={"structured": 1.0},
        source="structured_mapper",
    )
