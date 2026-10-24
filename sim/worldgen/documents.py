"""Render invoice documents: structured JSON with supplier-language field names, PDFs from the
eight templates, PNG scans, and .eml messages with inconsistently named PDF attachments."""

from __future__ import annotations

import hashlib
import json
import random
from datetime import UTC
from email.message import EmailMessage
from email.policy import SMTP
from email.utils import format_datetime, formataddr
from pathlib import Path

from sim.models import Attachment, Invoice, InvoiceFormat, Language, Supplier
from sim.templates.base import LineView, Party, RenderContext, VatRow, render_pdf
from sim.worldgen.catalog import AP_MAILBOX, UOM_LABEL, PlantInfo
from sim.worldgen.formats import (
    format_date,
    format_money,
    format_number,
    format_qty,
    format_rate_bp,
)
from sim.worldgen.materialize import InvoiceDraft, World
from sim.worldgen.plan import AdHocSupplier
from sim.worldgen.scans import rasterize_scan
from sim.worldgen.suppliers import strip_diacritics

COUNTRY_NAME: dict[str, dict[str, str]] = {
    "cs": {"CZ": "Česká republika", "DE": "Německo", "PL": "Polsko", "AT": "Rakousko"},
    "pl": {"PL": "Polska", "DE": "Niemcy", "CZ": "Czechy", "AT": "Austria"},
    "de": {"DE": "Deutschland", "CZ": "Tschechien", "PL": "Polen", "AT": "Österreich"},
    "en": {
        "DE": "Germany",
        "CZ": "Czech Republic",
        "PL": "Poland",
        "US": "USA",
        "EE": "Estonia",
        "AT": "Austria",
    },
    "ja": {"DE": "ドイツ", "CZ": "チェコ", "PL": "ポーランド", "JP": "日本"},
}
VAT_LABEL: dict[str, str] = {
    "CZ": "DIČ",
    "PL": "NIP",
    "DE": "USt-IdNr.",
    "AT": "UID",
    "JP": "登録番号",
    "US": "EIN",
    "EE": "KMKR",
}
BUYER_VAT_LABEL: dict[Language, str] = {
    "cs": "DIČ",
    "pl": "NIP",
    "de": "USt-IdNr.",
    "en": "VAT ID",
    "ja": "VAT ID",
}


def _supplier_party(d: InvoiceDraft) -> Party:
    s = d.supplier
    lang = d.language
    if isinstance(s, Supplier):
        country = COUNTRY_NAME.get(lang, COUNTRY_NAME["en"]).get(s.country, s.country)
        if lang == "ja":
            lines = (s.address.street, f"〒{s.address.postal_code} {s.address.city}")
        elif lang == "en":
            lines = (s.address.street, f"{s.address.city} {s.address.postal_code}", country)
        else:
            lines = (s.address.street, f"{s.address.postal_code} {s.address.city}", country)
        ids: list[tuple[str, str]] = []
        if s.country == "CZ":
            ids.append(("IČO", s.registration_id))
        elif s.country in ("PL", "DE", "AT", "US"):
            ids.append(
                (
                    {"PL": "KRS", "DE": "Handelsregister", "AT": "Firmenbuch", "US": "Reg."}[
                        s.country
                    ],
                    s.registration_id,
                )
            )
        ids.append((VAT_LABEL[s.country], s.vat_id))
        return Party(
            name=d.supplier_name_as_written,
            address_lines=lines,
            ids=tuple(ids),
            contact_lines=(s.email, s.phone),
        )
    assert isinstance(s, AdHocSupplier)
    return Party(
        name=s.name,
        address_lines=(s.street, f"{s.postal_code} {s.city}", "Estonia"),
        ids=(("KMKR", s.vat_id),),
        contact_lines=(s.email, s.phone),
    )


def _buyer_party(d: InvoiceDraft) -> Party:
    p: PlantInfo = d.ship_to
    lang = d.language
    country = COUNTRY_NAME.get(lang, COUNTRY_NAME["en"]).get(p.country, p.country)
    if lang == "en":
        lines = (p.street, f"{p.city} {p.postal_code}", country)
    else:
        lines = (p.street, f"{p.postal_code} {p.city}", country)
    return Party(name=p.legal_name, address_lines=lines, ids=((BUYER_VAT_LABEL[lang], p.vat_id),))


def _bank_rows(d: InvoiceDraft) -> list[tuple[str, str]]:
    b = d.bank_details
    if b is None:
        return []
    lang = d.language
    rows: list[tuple[str, str]] = []
    if b.iban:
        rows.append(("IBAN:", b.iban))
        rows.append(
            (
                {
                    "cs": "SWIFT:",
                    "pl": "SWIFT/BIC:",
                    "de": "BIC:",
                    "en": "BIC/SWIFT:",
                    "ja": "SWIFT:",
                }[lang],
                b.bic or "",
            )
        )
        rows.append(
            (
                {"cs": "Banka:", "pl": "Bank:", "de": "Bank:", "en": "Bank:", "ja": "銀行:"}[lang],
                b.bank_name or "",
            )
        )
    elif b.routing_number:
        rows = [
            ("Bank:", b.bank_name or ""),
            ("ABA routing:", b.routing_number),
            ("Account no.:", b.account_number or ""),
        ]
    else:
        rows = [
            ("銀行名 / Bank:", b.bank_name or ""),
            ("支店 / Branch:", f"{b.branch_code} 支店"),
            ("口座 / Account:", f"普通 {b.account_number}"),
            ("口座名義 / Holder:", b.holder or ""),
        ]
    return rows


def render_context(d: InvoiceDraft) -> RenderContext:
    lang, cur, loc = d.language, d.currency, d.number_locale
    money = lambda m: format_money(m, cur, loc, lang)  # noqa: E731
    lines = [
        LineView(
            no=ln.line_no,
            sku=ln.sku or "",
            description=ln.description,
            qty=format_qty(ln.qty, loc),
            uom=UOM_LABEL[lang][ln.uom],
            unit_price=format_number(ln.unit_price_minor, cur, loc),
            net=format_number(ln.net_minor, cur, loc),
            vat_rate=format_rate_bp(ln.vat_rate_bp, lang),
            vat=format_number(ln.vat_minor, cur, loc),
            gross=format_number(ln.gross_minor, cur, loc),
        )
        for ln in d.lines
    ]
    by_rate: dict[int, list[int]] = {}
    for ln in d.lines:
        by_rate.setdefault(ln.vat_rate_bp, [0, 0])
        by_rate[ln.vat_rate_bp][0] += ln.net_minor
        by_rate[ln.vat_rate_bp][1] += ln.vat_minor
    extra = d.vat_minor - sum(ln.vat_minor for ln in d.lines)
    vat_rows = []
    for rate, (base, vat) in sorted(by_rate.items()):
        vat += extra
        extra = 0
        vat_rows.append(
            VatRow(
                rate=format_rate_bp(rate, lang),
                base=money(base),
                vat=money(vat),
                gross=money(base + vat),
            )
        )
    digits = "".join(ch for ch in d.invoice_number if ch.isdigit()) or "0"
    notes = list(d.notes)
    if d.prepayment_minor:
        notes.append(
            {
                "de": f"Abzüglich erhaltener Anzahlung vom 03.10.2026: {money(d.prepayment_minor)}. Restbetrag: {money(d.gross_minor - d.prepayment_minor)}.",
                "cs": f"Minus přijatá záloha: {money(d.prepayment_minor)}.",
                "pl": f"Minus zaliczka: {money(d.prepayment_minor)}.",
                "en": f"Less prepayment received: {money(d.prepayment_minor)}.",
                "ja": f"前払金控除: {money(d.prepayment_minor)}",
            }[lang]
        )
    return RenderContext(
        template_id=d.template_id or "us_invoice",  # type: ignore[arg-type]
        invoice=_truth_stub(d),
        language=lang,
        currency=cur,
        number_locale=loc,
        date_format=d.date_format,  # type: ignore[arg-type]
        supplier=_supplier_party(d),
        buyer=_buyer_party(d),
        lines=lines,
        vat_rows=vat_rows,
        net=money(d.net_minor),
        vat=money(d.vat_minor),
        gross=money(d.gross_minor),
        po_ref_text=d.po_ref_text,
        invoice_date=format_date(d.invoice_date, d.date_format),
        due_date=format_date(d.due_date, d.date_format),  # type: ignore[arg-type]
        service_date=format_date(d.service_date or d.invoice_date, d.date_format),
        payment_ref=digits[-10:],  # type: ignore[arg-type]
        bank_rows=_bank_rows(d),
        notes=notes,
        terms_text=d.terms_text,
        is_credit_note=d.kind == "credit_note",
        credit_note_for=d.credit_note_for,
    )


class _Stub:
    def __init__(self, number: str) -> None:
        self.invoice_number = number


def _truth_stub(d: InvoiceDraft):
    return _Stub(d.invoice_number)


# --------------------------------------------------------------------------------------
# Structured JSON
# --------------------------------------------------------------------------------------


def structured_document(d: InvoiceDraft) -> dict:
    lang, cur, loc, df = d.language, d.currency, d.number_locale, d.date_format
    num = lambda m: format_number(m, cur, loc)  # noqa: E731
    dt = lambda x: format_date(x, df)  # noqa: E731
    sup = d.supplier
    bank = d.bank_details
    if isinstance(sup, Supplier):
        addr = {
            "street": sup.address.street,
            "city": sup.address.city,
            "postal": sup.address.postal_code,
            "country": sup.country,
        }
    else:
        addr = {
            "street": sup.street,
            "city": sup.city,
            "postal": sup.postal_code,
            "country": sup.country,
        }
    buyer = d.ship_to
    po_ref = ", ".join(d.po_refs_as_written)
    note = " ".join(d.notes)
    rate = format_rate_bp(d.vat_rate_bp, lang)
    if lang == "de":
        return {
            "Rechnungsnummer": d.invoice_number,
            "Belegart": "Gutschrift" if d.kind == "credit_note" else "Rechnung",
            "Rechnungsdatum": dt(d.invoice_date),
            "Leistungsdatum": dt(d.service_date or d.invoice_date),
            "Fälligkeitsdatum": dt(d.due_date),
            "Lieferant": {
                "Name": d.supplier_name_as_written,
                "Straße": addr["street"],
                "PLZ": addr["postal"],
                "Ort": addr["city"],
                "Land": addr["country"],
                "USt-IdNr": d.vat_id_as_written,
            },
            "Kunde": {
                "Name": buyer.legal_name,
                "Straße": buyer.street,
                "PLZ": buyer.postal_code,
                "Ort": buyer.city,
                "USt-IdNr": buyer.vat_id,
            },
            "Bestellnummer": po_ref,
            "Währung": cur,
            "Positionen": [
                {
                    "Pos": ln.line_no,
                    "Artikelnummer": ln.sku,
                    "Bezeichnung": ln.description,
                    "Menge": format_qty(ln.qty, loc),
                    "Einheit": UOM_LABEL[lang][ln.uom],
                    "Einzelpreis": num(ln.unit_price_minor),
                    "Gesamt": num(ln.net_minor),
                }
                for ln in d.lines
            ],
            "Nettobetrag": num(d.net_minor),
            "MwSt-Satz": rate,
            "MwSt-Betrag": num(d.vat_minor),
            "Bruttobetrag": num(d.gross_minor),
            "Zahlungsbedingungen": d.terms_text,
            "Bankverbindung": {
                "IBAN": bank.iban if bank else None,
                "BIC": bank.bic if bank else None,
                "Bank": bank.bank_name if bank else None,
            },
            "Hinweis": note or None,
        }
    if lang == "cs":
        return {
            "Číslo faktury": d.invoice_number,
            "Typ dokladu": "Dobropis" if d.kind == "credit_note" else "Faktura – daňový doklad",
            "Datum vystavení": dt(d.invoice_date),
            "DUZP": dt(d.service_date or d.invoice_date),
            "Datum splatnosti": dt(d.due_date),
            "Dodavatel": {
                "Název": d.supplier_name_as_written,
                "Ulice": addr["street"],
                "Město": addr["city"],
                "PSČ": addr["postal"],
                "Země": addr["country"],
                "IČO": sup.registration_id if isinstance(sup, Supplier) else None,
                "DIČ": d.vat_id_as_written,
            },
            "Odběratel": {
                "Název": buyer.legal_name,
                "Ulice": buyer.street,
                "Město": buyer.city,
                "PSČ": buyer.postal_code,
                "DIČ": buyer.vat_id,
            },
            "Číslo objednávky": po_ref,
            "Variabilní symbol": "".join(ch for ch in d.invoice_number if ch.isdigit())[-10:],
            "Měna": cur,
            "Položky": [
                {
                    "Pořadí": ln.line_no,
                    "Kód": ln.sku,
                    "Popis": ln.description,
                    "Množství": format_qty(ln.qty, loc),
                    "MJ": UOM_LABEL[lang][ln.uom],
                    "Cena za MJ": num(ln.unit_price_minor),
                    "Celkem bez DPH": num(ln.net_minor),
                }
                for ln in d.lines
            ],
            "Základ daně": num(d.net_minor),
            "Sazba DPH": rate,
            "DPH": num(d.vat_minor),
            "Celkem k úhradě": num(d.gross_minor),
            "Platební podmínky": d.terms_text,
            "Bankovní spojení": {
                "IBAN": bank.iban if bank else None,
                "SWIFT": bank.bic if bank else None,
                "Banka": bank.bank_name if bank else None,
            },
            "Poznámka": note or None,
        }
    if lang == "pl":
        return {
            "Numer faktury": d.invoice_number,
            "Rodzaj": "Faktura korygująca" if d.kind == "credit_note" else "Faktura VAT",
            "Data wystawienia": dt(d.invoice_date),
            "Data sprzedaży": dt(d.service_date or d.invoice_date),
            "Termin płatności": dt(d.due_date),
            "Sprzedawca": {
                "Nazwa": d.supplier_name_as_written,
                "Ulica": addr["street"],
                "Miasto": addr["city"],
                "Kod pocztowy": addr["postal"],
                "Kraj": addr["country"],
                "NIP": d.vat_id_as_written,
            },
            "Nabywca": {
                "Nazwa": buyer.legal_name,
                "Ulica": buyer.street,
                "Miasto": buyer.city,
                "Kod pocztowy": buyer.postal_code,
                "NIP": buyer.vat_id,
            },
            "Numer zamówienia": po_ref,
            "Waluta": cur,
            "Pozycje": [
                {
                    "Lp": ln.line_no,
                    "Kod": ln.sku,
                    "Nazwa": ln.description,
                    "Ilość": format_qty(ln.qty, loc),
                    "Jm": UOM_LABEL[lang][ln.uom],
                    "Cena netto": num(ln.unit_price_minor),
                    "Wartość netto": num(ln.net_minor),
                    "Stawka VAT": format_rate_bp(ln.vat_rate_bp, lang),
                    "Kwota VAT": num(ln.vat_minor),
                    "Wartość brutto": num(ln.gross_minor),
                }
                for ln in d.lines
            ],
            "Razem netto": num(d.net_minor),
            "Razem VAT": num(d.vat_minor),
            "Razem brutto": num(d.gross_minor),
            "Sposób zapłaty": "przelew",
            "Numer rachunku": bank.iban if bank else None,
            "Bank": bank.bank_name if bank else None,
            "Uwagi": note or None,
        }
    if lang == "ja":
        return {
            "請求書番号": d.invoice_number,
            "発行日": dt(d.invoice_date),
            "納品日": dt(d.service_date or d.invoice_date),
            "支払期限": dt(d.due_date),
            "発行者": {
                "名称": d.supplier_name_as_written,
                "住所": f"{addr['postal']} {addr['city']} {addr['street']}",
                "登録番号": d.vat_id_as_written,
            },
            "宛先": {
                "名称": buyer.legal_name,
                "住所": f"{buyer.street}, {buyer.postal_code} {buyer.city}",
            },
            "注文番号": po_ref,
            "通貨": cur,
            "明細": [
                {
                    "行": ln.line_no,
                    "品番": ln.sku,
                    "品名": ln.description,
                    "数量": format_qty(ln.qty, loc),
                    "単位": UOM_LABEL[lang][ln.uom],
                    "単価": num(ln.unit_price_minor),
                    "金額": num(ln.net_minor),
                }
                for ln in d.lines
            ],
            "小計": num(d.net_minor),
            "税率": rate,
            "消費税": num(d.vat_minor),
            "合計": num(d.gross_minor),
            "支払条件": d.terms_text,
            "振込先": {
                "銀行": bank.bank_name if bank else None,
                "支店": bank.branch_code if bank else None,
                "口座番号": bank.account_number if bank else None,
                "口座名義": bank.holder if bank else None,
            },
            "備考": note or None,
        }
    return {
        "invoice_number": d.invoice_number,
        "document_type": "credit_memo" if d.kind == "credit_note" else "invoice",
        "invoice_date": dt(d.invoice_date),
        "service_date": dt(d.service_date or d.invoice_date),
        "due_date": dt(d.due_date),
        "vendor": {
            "name": d.supplier_name_as_written,
            "address": addr["street"],
            "city": addr["city"],
            "postal_code": addr["postal"],
            "country": addr["country"],
            "tax_id": d.vat_id_as_written,
        },
        "bill_to": {
            "name": buyer.legal_name,
            "address": buyer.street,
            "city": buyer.city,
            "postal_code": buyer.postal_code,
            "vat_id": buyer.vat_id,
        },
        "po_number": po_ref,
        "currency": cur,
        "line_items": [
            {
                "line": ln.line_no,
                "sku": ln.sku,
                "description": ln.description,
                "quantity": format_qty(ln.qty, loc),
                "unit": UOM_LABEL[lang][ln.uom],
                "unit_price": num(ln.unit_price_minor),
                "amount": num(ln.net_minor),
            }
            for ln in d.lines
        ],
        "subtotal": num(d.net_minor),
        "tax_rate": rate,
        "tax": num(d.vat_minor),
        "total": num(d.gross_minor),
        "terms": d.terms_text,
        "remit_to": {
            "bank": bank.bank_name if bank else None,
            "routing_number": bank.routing_number if bank else None,
            "account_number": bank.account_number if bank else None,
            "iban": bank.iban if bank else None,
        },
        "notes": note or None,
    }


# --------------------------------------------------------------------------------------
# E-mail
# --------------------------------------------------------------------------------------

SUBJECTS: dict[Language, list[str]] = {
    "cs": ["Faktura č. {n} – {s}", "Faktura {n}", "{s}: faktura {n} k úhradě", "Fwd: faktura {n}"],
    "pl": [
        "Faktura VAT nr {n}",
        "{s} – faktura {n}",
        "Faktura {n} – prosimy o potwierdzenie",
        "FW: faktura {n}",
    ],
    "de": [
        "Rechnung Nr. {n}",
        "Ihre Rechnung {n} – {s}",
        "Rechnung {n} / Lieferung",
        "WG: Rechnung {n}",
    ],
    "en": ["Invoice {n} from {s}", "Invoice {n}", "{s} – invoice {n} attached", "FW: Invoice {n}"],
    "ja": ["請求書送付のご案内 ({n})", "【{s}】請求書 {n}", "請求書 {n} 送付の件"],
}
BODIES: dict[Language, list[str]] = {
    "cs": [
        "Dobrý den,\n\nv příloze zasíláme fakturu č. {n} za dodávku dle objednávky {po}. Splatnost {due}.\n\nS pozdravem\n{signer}\n{s}",
        "Dobrý den,\n\nposíláme fakturu {n}. Částka {gross}, splatnost {due}. V případě dotazů nás kontaktujte.\n\nDěkujeme,\n{signer}\nfakturace, {s}",
        "Dobrý den,\n\nk vaší objednávce {po} přikládáme fakturu {n}. Prosíme o úhradu na účet uvedený na faktuře.\n\nS pozdravem,\n{signer}",
    ],
    "pl": [
        "Dzień dobry,\n\nw załączeniu przesyłamy fakturę nr {n} do zamówienia {po}. Termin płatności: {due}.\n\nZ poważaniem\n{signer}\n{s}",
        "Dzień dobry,\n\nprzesyłamy fakturę {n} na kwotę {gross}. Prosimy o płatność na rachunek wskazany na fakturze.\n\nPozdrawiam,\n{signer}\nDział księgowości, {s}",
    ],
    "de": [
        "Sehr geehrte Damen und Herren,\n\nanbei erhalten Sie unsere Rechnung Nr. {n} zu Ihrer Bestellung {po}. Zahlbar bis {due}.\n\nMit freundlichen Grüßen\n{signer}\n{s}",
        "Guten Tag,\n\nim Anhang finden Sie die Rechnung {n} über {gross}. Bitte überweisen Sie den Betrag auf das angegebene Konto.\n\nFreundliche Grüße\n{signer}\nBuchhaltung, {s}",
    ],
    "en": [
        "Hello,\n\nPlease find attached invoice {n} for PO {po}. Payment is due {due}.\n\nBest regards,\n{signer}\n{s}",
        "Hi,\n\nAttached is invoice {n} ({gross}). Let us know if you need anything else for processing.\n\nThanks,\n{signer}\nAccounts Receivable, {s}",
    ],
    "ja": [
        "いつもお世話になっております。\n{s}の{signer}です。\n\n注文番号 {po} の請求書 ({n}) を添付いたします。支払期限は {due} です。\n\nよろしくお願いいたします。",
    ],
}
SIGNERS: dict[Language, list[str]] = {
    "cs": ["Jana Horáková", "Petr Novák", "Lenka Dvořáková"],
    "pl": ["Anna Kowalska", "Piotr Wiśniewski", "Magdalena Lewandowska"],
    "de": ["Sabine Müller", "Thomas Becker", "Katrin Hoffmann"],
    "en": ["Dana Whitfield", "Marcus Lee"],
    "ja": ["佐藤", "田中"],
}
ATTACHMENT_STYLES = [
    "faktura_{digits}.pdf",
    "INV {prefix} {digits} FINAL(2).pdf",
    "Rechnung-{number}.PDF",
    "scan{seq:04d}.pdf",
    "FV_{digits}.pdf",
    "{number}.pdf",
    "invoice {number} copy.pdf",
    "Faktura {number} - {short}.pdf",
    "{short}_{digits}_signed.pdf",
    "doc{seq:03d} ({digits}).pdf",
]


def attachment_name(rng: random.Random, d: InvoiceDraft, seq: int) -> str:
    digits = "".join(ch for ch in d.invoice_number if ch.isdigit()) or str(seq)
    prefix = "".join(ch for ch in d.invoice_number if ch.isalpha())[:3] or "INV"
    short = strip_diacritics(d.supplier_name_as_written.split(" ")[0])
    style = rng.choice(ATTACHMENT_STYLES)
    name = style.format(
        digits=digits,
        prefix=prefix,
        number=d.invoice_number.replace("/", "-"),
        seq=seq,
        short=short,
    )
    return name.replace("..", ".")


def build_eml(
    rng: random.Random, d: InvoiceDraft, pdf_bytes: bytes, seq: int
) -> tuple[bytes, Attachment, dict[str, str]]:
    lang = d.language
    sup = d.supplier
    domain = sup.domain
    from_addr = sup.email
    display = d.supplier_name_as_written
    number = d.invoice_number
    po = ", ".join(d.po_refs_as_written) or "—"
    due = format_date(d.due_date, d.date_format)  # type: ignore[arg-type]
    gross = format_money(d.gross_minor, d.currency, d.number_locale, lang)  # type: ignore[arg-type]
    signer = rng.choice(SIGNERS[lang])
    subject = rng.choice(SUBJECTS[lang]).format(n=number, s=display)
    body = rng.choice(BODIES[lang]).format(
        n=number, po=po, due=due, gross=gross, signer=signer, s=display
    )
    if d.kind == "credit_note":
        body = {
            "cs": f"Dobrý den,\n\nv příloze zasíláme dobropis {number} k faktuře {d.credit_note_for}.\n\nS pozdravem\n{signer}\n{display}",
            "pl": f"Dzień dobry,\n\nw załączeniu faktura korygująca {number} do faktury {d.credit_note_for}.\n\n{signer}",
            "de": f"Guten Tag,\n\nanbei die Gutschrift {number} zur Rechnung {d.credit_note_for}.\n\nMit freundlichen Grüßen\n{signer}",
            "en": f"Hello,\n\nattached is credit memo {number} applying to invoice {d.credit_note_for}.\n\n{signer}",
            "ja": f"請求書 {d.credit_note_for} に対する訂正 ({number}) を添付します。",
        }[lang]
        subject = {
            "cs": f"Dobropis {number} k faktuře {d.credit_note_for}",
            "pl": f"Faktura korygująca {number}",
            "de": f"Gutschrift {number} zu Rechnung {d.credit_note_for}",
            "en": f"Credit memo {number}",
            "ja": f"訂正請求書 {number}",
        }[lang]
    if "T3" in d.case_tags and number.endswith("A"):
        subject = "Re: Faktura GF-77 – ponowne przesłanie"
        body = f"Dzień dobry,\n\nponownie przesyłamy fakturę za dostawę z 10.10 (nr {number}, wersja z poprawionym numerem). Prosimy o zaksięgowanie.\n\nPozdrawiam,\n{signer}\n{display}"
    if "A15" in d.case_tags and d.plan.eml_thread:
        subject = f"Re: Faktura {number} – oprava"
        body = f"Dobrý den,\n\nomlouváme se, v původní faktuře chyběla položka dopravy. V příloze zasíláme opravenou verzi ({number}, celkem {gross}). Původní prosím stornujte.\n\n{signer}\n{display}"
    if "T6" in d.case_tags:
        subject = f"Invoice {number} – Nordic Trim OÜ"
        body = (
            f"Dear Accounts Payable team,\n\nPlease find attached our invoice {number} for the woven trim fabric delivered to your Munich plant "
            f"under {po}. Total {gross}, payable within 30 days to the IBAN stated on the invoice.\n\nKind regards,\nKristiina Tamm\nNordic Trim OÜ, Tallinn"
        )
    msg = EmailMessage()
    msg["From"] = formataddr((display, from_addr))
    msg["To"] = AP_MAILBOX
    if d.plan.eml_reply_to_differs or (not d.plan.format_fixed and rng.random() < 0.3):
        msg["Reply-To"] = formataddr(
            (
                f"{display} – účtárna" if lang == "cs" else display,
                f"{rng.choice(['uctarna', 'ar', 'accounting', 'ksiegowosc'])}@{domain}",
            )
        )
    msg["Subject"] = subject
    msg["Date"] = format_datetime(d.plan.known_at.astimezone(UTC))
    message_id = f"<{d.plan.known_at:%Y%m%d%H%M%S}.{rng.randrange(10**9):09d}@{domain}>"
    msg["Message-ID"] = message_id
    threaded = d.plan.eml_thread or (not d.plan.format_fixed and rng.random() < 0.25)
    parent = None
    if threaded:
        parent = f"<{(d.plan.known_at.replace(hour=9) - _days(rng.randrange(2, 12))):%Y%m%d%H%M%S}.{rng.randrange(10**9):09d}@{domain}>"
        msg["In-Reply-To"] = parent
        msg["References"] = parent
        if not subject.lower().startswith(("re:", "fwd:", "fw:", "wg:")):
            msg.replace_header("Subject", f"Re: {subject}")
    msg["X-Mailer"] = rng.choice(
        [
            "Microsoft Outlook 16.0",
            "Mozilla Thunderbird",
            "SAP Business One",
            "Pohoda 2026",
            "Comarch ERP",
        ]
    )
    msg.set_content(body)
    name = attachment_name(rng, d, seq)
    msg.add_attachment(pdf_bytes, maintype="application", subtype="pdf", filename=name)
    msg.set_boundary(f"===============PAYGUARD{rng.randrange(10**12):012d}==")
    raw = msg.as_bytes(policy=SMTP)
    att = Attachment(
        filename=name,
        content_type="application/pdf",
        size_bytes=len(pdf_bytes),
        sha256=hashlib.sha256(pdf_bytes).hexdigest(),
    )
    return (
        raw,
        att,
        {
            "message_id": message_id,
            "subject": str(msg["Subject"]),
            "from": from_addr,
            "in_reply_to": parent or "",
        },
    )


def _days(n: int):
    from datetime import timedelta

    return timedelta(days=n)


# --------------------------------------------------------------------------------------
# Driver
# --------------------------------------------------------------------------------------


def render_documents(
    rng: random.Random, world: World, out_dir: Path
) -> tuple[list[Invoice], dict[str, dict[str, str]], dict[str, int]]:
    inv_dir = out_dir / "invoices"
    eml_dir = out_dir / "emails"
    tmp_dir = out_dir / ".tmp"
    inv_dir.mkdir(parents=True, exist_ok=True)
    eml_dir.mkdir(parents=True, exist_ok=True)
    tmp_dir.mkdir(parents=True, exist_ok=True)
    invoices: list[Invoice] = []
    email_meta: dict[str, dict[str, str]] = {}
    sizes: dict[str, int] = {}
    for seq, d in enumerate(sorted(world.drafts, key=lambda x: x.id), start=1):
        fmt = d.plan.format
        attachments: list[Attachment] = []
        if fmt == InvoiceFormat.STRUCTURED:
            rel = f"invoices/{d.id}.json"
            data = json.dumps(structured_document(d), ensure_ascii=False, indent=2) + "\n"
            (out_dir / rel).write_text(data, encoding="utf-8")
        else:
            ctx = render_context(d)
            if fmt == InvoiceFormat.PDF:
                rel = f"invoices/{d.id}.pdf"
                render_pdf(ctx, out_dir / rel)
            elif fmt == InvoiceFormat.PNG:
                rel = f"invoices/{d.id}.png"
                tmp = tmp_dir / f"{d.id}.pdf"
                pdf_bytes = render_pdf(ctx, tmp)
                rasterize_scan(
                    pdf_bytes,
                    out_dir / rel,
                    seed=world.seed * 1000 + seq,
                    annotated=d.plan.scan_annotated,
                )
                tmp.unlink()
                if d.plan.scan_annotated and "annotated" not in d.case_tags:
                    d.case_tags.append("annotated")
            else:
                rel = f"emails/{d.id}.eml"
                tmp = tmp_dir / f"{d.id}.pdf"
                pdf_bytes = render_pdf(ctx, tmp)
                tmp.unlink()
                raw, att, meta = build_eml(rng, d, pdf_bytes, seq)
                (out_dir / rel).write_bytes(raw)
                attachments.append(att)
                email_meta[d.id] = meta
        content = (out_dir / rel).read_bytes()
        sizes[d.id] = len(content)
        invoices.append(
            Invoice(
                id=d.id,
                supplier_id=d.supplier_id,
                supplier_name_as_written=d.supplier_name_as_written,
                vat_id_as_written=d.vat_id_as_written,
                invoice_number=d.invoice_number,
                kind=d.kind,
                format=fmt,
                document_path=rel,
                template_id=d.template_id,  # type: ignore[arg-type]
                language=d.language,
                currency=d.currency,
                number_locale=d.number_locale,
                date_format=d.date_format,  # type: ignore[arg-type]
                invoice_date=d.invoice_date,
                due_date=d.due_date,
                service_date=d.service_date,
                po_refs_as_written=d.po_refs_as_written,
                po_ids=d.po_ids,
                lines=d.lines,
                net_minor=d.net_minor,
                vat_minor=d.vat_minor,
                gross_minor=d.gross_minor,
                vat_treatment=d.vat_treatment,
                vat_note=d.vat_note,
                terms=d.terms,
                discount_bp=d.discount_bp,
                discount_deadline=d.discount_deadline,
                retention_minor=d.retention_minor,
                prepayment_minor=d.prepayment_minor,
                bank_details=d.bank_details,
                credit_note_for=d.credit_note_for,
                received_via=d.received_via,
                attachments=attachments,
                case_tags=list(d.case_tags),
                sha256=hashlib.sha256(content).hexdigest(),
            )
        )
    tmp_dir.rmdir()
    return invoices, email_meta, sizes
