from __future__ import annotations

from agents.parsed import LocaleHints
from agents.structured_mapper import map_structured

DE = {
    "Rechnungsnummer": "RE-2026-0510",
    "Belegart": "Rechnung",
    "Rechnungsdatum": "25.09.2026",
    "Leistungsdatum": "18.09.2026",
    "Fälligkeitsdatum": "25.10.2026",
    "Lieferant": {
        "Name": "Fränkische Schrauben GmbH",
        "Straße": "Nürnberger Straße 88",
        "PLZ": "91126",
        "Ort": "Schwabach",
        "Land": "DE",
        "USt-IdNr": "DE583545556",
    },
    "Kunde": {"Name": "Toyota Boshoku Europe GmbH (simulated)", "USt-IdNr": "DE812345670"},
    "Bestellnummer": "PO-10009",
    "Währung": "EUR",
    "Positionen": [
        {
            "Pos": 1,
            "Artikelnummer": "FS-6010",
            "Bezeichnung": "Flanschmutter M8",
            "Menge": "10",
            "Einheit": "Karton 50",
            "Einzelpreis": "5,93",
            "Gesamt": "59,30",
        }
    ],
    "Nettobetrag": "59,30",
    "MwSt-Satz": "19 %",
    "MwSt-Betrag": "11,27",
    "Bruttobetrag": "70,57",
    "Zahlungsbedingungen": "Zahlungsziel: 30 Tage netto; 2 % Skonto bei Zahlung innerhalb von 10 Tagen.",
    "Bankverbindung": {
        "IBAN": "DE41760501012048795757",
        "BIC": "SSKNDE77XXX",
        "Bank": "Sparkasse Nürnberg",
    },
    "Hinweis": None,
}
CS = {
    "Číslo faktury": "KT-2291",
    "Typ dokladu": "Faktura – daňový doklad",
    "Datum vystavení": "12.10.2026",
    "DUZP": "09.10.2026",
    "Datum splatnosti": "11.11.2026",
    "Dodavatel": {
        "Název": "Kovak Trim s.r.o.",
        "Ulice": "Průmyslová 12",
        "Město": "Liberec",
        "PSČ": "460 01",
        "Země": "CZ",
        "IČO": "71310983",
        "DIČ": "CZ71310983",
    },
    "Odběratel": {"Název": "Toyota Boshoku Europe GmbH (simulated)", "DIČ": "DE812345670"},
    "Číslo objednávky": "PO-10051",
    "Variabilní symbol": "2291",
    "Měna": "EUR",
    "Položky": [
        {
            "Pořadí": 1,
            "Kód": "SF-1001",
            "Popis": "Rám sedadla přední levý",
            "Množství": "1 000",
            "MJ": "ks",
            "Cena za MJ": "13,90",
            "Celkem bez DPH": "13 900,00",
        }
    ],
    "Základ daně": "13 900,00",
    "Sazba DPH": "0 %",
    "DPH": "0,00",
    "Celkem k úhradě": "13 900,00",
    "Platební podmínky": "Splatnost 30 dnů od data vystavení.",
    "Bankovní spojení": {
        "IBAN": "CZ6701000000009198260449",
        "SWIFT": "KOMBCZPP",
        "Banka": "Komerční banka, a.s.",
    },
    "Poznámka": "Daň odvede zákazník – reverse charge",
}
PL = {
    "Numer faktury": "FV/2026/10/0098",
    "Rodzaj": "Faktura VAT",
    "Data wystawienia": "2026-10-09",
    "Data sprzedaży": "2026-10-05",
    "Termin płatności": "2026-11-23",
    "Sprzedawca": {
        "Nazwa": "Tkaniny Śląskie Sp. z o.o.",
        "Ulica": "ul. Partyzantów 44",
        "Miasto": "Bielsko-Biała",
        "Kod pocztowy": "43-300",
        "Kraj": "PL",
        "NIP": "PL8705137313",
    },
    "Nabywca": {"Nazwa": "Toyota Boshoku Europe GmbH (simulated)", "NIP": "DE812345670"},
    "Numer zamówienia": "Zam. nr 10040",
    "Waluta": "PLN",
    "Pozycje": [
        {
            "Lp": 1,
            "Kod": "TF-3001",
            "Nazwa": "Tkanina obiciowa antracyt",
            "Ilość": "800",
            "Jm": "m",
            "Cena netto": "62,50",
            "Wartość netto": "50 000,00",
            "Stawka VAT": "0%",
            "Kwota VAT": "0,00",
            "Wartość brutto": "50 000,00",
        }
    ],
    "Razem netto": "50 000,00",
    "Razem VAT": "0,00",
    "Razem brutto": "50 000,00",
    "Sposób zapłaty": "przelew",
    "Numer rachunku": "PL55105012149665884577738650",
    "Bank": "ING",
    "Uwagi": None,
}
EN = {
    "invoice_number": "INV-02408",
    "document_type": "invoice",
    "invoice_date": "09/30/2026",
    "service_date": "09/30/2026",
    "due_date": "10/30/2026",
    "vendor": {
        "name": "Great Lakes Wire & Cable Inc.",
        "address": "4120 36th Street SE",
        "city": "Grand Rapids, MI",
        "postal_code": "49512",
        "country": "US",
        "tax_id": "98-9945998",
    },
    "bill_to": {"name": "Toyota Boshoku Europe GmbH (simulated)", "vat_id": "DE812345670"},
    "po_number": "PO-10005",
    "currency": "USD",
    "line_items": [
        {
            "line": 1,
            "sku": "WH-5010",
            "description": "Occupant sensor lead",
            "quantity": "2,000",
            "unit": "pc",
            "unit_price": "5.82",
            "amount": "11,640.00",
        },
        {
            "line": 2,
            "sku": "WH-5020",
            "description": "Connector housing 12-pin",
            "quantity": "2,000",
            "unit": "pc",
            "unit_price": "1.03",
            "amount": "2,060.00",
        },
    ],
    "subtotal": "13,700.00",
    "tax_rate": "0%",
    "tax": "0.00",
    "total": "13,700.00",
    "terms": "Net 30",
    "remit_to": {
        "bank": "JPMorgan Chase Bank, N.A.",
        "routing_number": "021000021",
        "account_number": "1632254508",
        "iban": None,
    },
    "notes": "Export sale – no sales tax applied.",
}
JA = {
    "請求書番号": "INV-2026-3833",
    "発行日": "2026-10-02",
    "納品日": "2026-09-28",
    "支払期限": "2026-11-16",
    "発行者": {
        "名称": "株式会社ミナト精機",
        "住所": "220-0012 横浜市 神奈川県横浜市西区みなとみらい3-6-1",
        "登録番号": "T7762269906864",
    },
    "宛先": {"名称": "Toyota Boshoku Europe GmbH (simulated)", "住所": "x"},
    "注文番号": "PO-10034",
    "通貨": "JPY",
    "明細": [
        {
            "行": 1,
            "品番": "HM-4040",
            "品名": "折りたたみヘッドレスト機構",
            "数量": "250",
            "単位": "個",
            "単価": "9,162",
            "金額": "2,290,500",
        }
    ],
    "小計": "2,290,500",
    "税率": "0%",
    "消費税": "0",
    "合計": "2,290,500",
    "支払条件": "支払条件: 発行日より45日以内",
    "振込先": {
        "銀行": "みずほ銀行 (Mizuho Bank)",
        "支店": "422",
        "口座番号": "7502139",
        "口座名義": "株式会社ミナト精機",
    },
    "備考": None,
}


def hints(lang: str, locale: str, df: str, ccy: str) -> LocaleHints:
    return LocaleHints(
        number_locale=locale, date_format=df, language=lang, supplier_id=None, currency=ccy
    )  # type: ignore[arg-type]


def test_maps_all_five_languages() -> None:
    de = map_structured(DE, hints("de", "1.234,56", "DD.MM.YYYY", "EUR"))
    assert (
        de is not None
        and de.invoice_number == "RE-2026-0510"
        and de.invoice_date == "25.09.2026"
        and de.net == "59,30"
        and de.vat_amount == "11,27"
        and de.gross == "70,57"
    )
    assert (
        de.lines[0].qty == "10"
        and de.lines[0].unit_price == "5,93"
        and de.lines[0].sku == "FS-6010"
        and de.po_references == ["PO-10009"]
    )
    assert (
        de.supplier_vat_id == "DE583545556"
        and de.bank_details is not None
        and de.bank_details.iban == "DE41760501012048795757"
    )
    assert de.payment_terms and "Skonto" in de.payment_terms and de.source == "structured_mapper"
    cs = map_structured(CS, hints("cs", "1 234,56", "DD.MM.YYYY", "EUR"))
    assert (
        cs is not None
        and cs.invoice_number == "KT-2291"
        and cs.lines[0].qty == "1 000"
        and cs.gross == "13 900,00"
        and cs.supplier_vat_id == "CZ71310983"
    )
    pl = map_structured(PL, hints("pl", "1 234,56", "YYYY-MM-DD", "PLN"))
    assert (
        pl is not None
        and pl.po_references == ["Zam. nr 10040"]
        and pl.lines[0].line_total == "50 000,00"
        and pl.bank_details.iban == "PL55105012149665884577738650"
    )
    en = map_structured(EN, hints("en", "1,234.56", "MM/DD/YYYY", "USD"))
    assert (
        en is not None
        and len(en.lines) == 2
        and en.lines[1].unit_price == "1.03"
        and en.bank_details.routing_number == "021000021"
        and en.currency == "USD"
    )
    ja = map_structured(JA, hints("ja", "1,234.56", "YYYY-MM-DD", "JPY"))
    assert (
        ja is not None
        and ja.gross == "2,290,500"
        and ja.supplier_vat_id == "T7762269906864"
        and ja.bank_details.account_number == "7502139"
    )


def test_credit_memo_flag_and_missing_required_field() -> None:
    cm = dict(EN, document_type="credit_memo", notes="Credit memo applies to invoice INV-02400")
    out = map_structured(cm, hints("en", "1,234.56", "MM/DD/YYYY", "USD"))
    assert out is not None and out.is_credit_note and out.credit_note_for == "INV-02400"
    broken = dict(DE)
    del broken["Rechnungsnummer"]
    assert map_structured(broken, hints("de", "1.234,56", "DD.MM.YYYY", "EUR")) is None
    no_lines = dict(DE, Positionen=[])
    assert map_structured(no_lines, hints("de", "1.234,56", "DD.MM.YYYY", "EUR")) is None
