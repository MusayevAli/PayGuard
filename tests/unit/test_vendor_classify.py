"""Message classification (spec §7.4): bank change / onboarding / other, IBAN extraction across the
five locales, lookalike-domain detection with true and false cases."""

from __future__ import annotations

import pytest

from agents.vendor_classify import classify, domain_of, extract_ibans, extract_vat_ids, lookalike

KNOWN = {
    "channel": "email",
    "vendor_id": "V-0001",
    "claimed_supplier_name": "Kovak Trim s.r.o.",
    "from_address": "fakturace@kovaktrim.cz",
    "subject": "",
    "body": "",
    "bank_details": None,
}


def msg(**over):
    return {**KNOWN, **over}


@pytest.mark.parametrize(
    ("body", "iban"),
    [
        (
            "Dobrý den,\nod 1. 11. 2026 měníme bankovní spojení. Nový účet: IBAN CZ65 0800 0000 1920 0014 5399, banka ČS.",
            "CZ6508000000192000145399",
        ),
        (
            "Dzień dobry, informujemy o zmianie rachunku bankowego: IBAN PL61 1090 1014 0000 0712 1981 2874 mBank.",
            "PL61109010140000071219812874",
        ),
        (
            "Guten Tag, bitte beachten Sie unsere neue Bankverbindung: IBAN DE89 3704 0044 0532 0130 00, BIC COBADEFFXXX.",
            "DE89370400440532013000",
        ),
        (
            "Please update our bank details. New account IBAN GB29NWBK60161331926819.",
            "GB29NWBK60161331926819",
        ),
        ("新しい口座への振込先変更のお願い: IBAN AT61 1904 3002 3457 3201", "AT611904300234573201"),
    ],
)
def test_iban_extraction_across_locales(body: str, iban: str) -> None:
    assert extract_ibans(body) == [iban]
    assert classify(msg(body=body), known_vendor=True).kind == "bank_change"


def test_invalid_ibans_are_ignored() -> None:
    assert extract_ibans("IBAN CZ65 0800 0000 1920 0014 5390 is wrong") == []
    assert (
        classify(msg(body="Our new IBAN is CZ6508000000192000145390."), known_vendor=True).kind
        == "other"
    )


def test_structured_bank_details_win_over_body_text() -> None:
    c = classify(
        msg(
            channel="portal",
            body="Změna bankovního účtu",
            bank_details={"iban": "CZ87 0100 0000 0044 3139 5006", "bank_name": "KB"},
        ),
        known_vendor=True,
    )
    assert c.kind == "bank_change" and c.iban == "CZ8701000000004431395006"


@pytest.mark.parametrize(
    "body",
    [
        "Upomínka č. 1 – faktura 20260389 ze dne 28. 8. 2026 je po splatnosti. Prosíme o úhradu.",
        "Zahlungsavis zu Rechnung FT-112: wir haben am 06.10. einen Zahlungseingang ohne Verwendungszweck erhalten.",
        "Nowa osoba kontaktowa – od 1 listopada Państwa opiekunem handlowym będzie Pan Marcin Nowak.",
    ],
)
def test_other_messages(body: str) -> None:
    assert classify(msg(body=body), known_vendor=True).kind == "other"


def test_onboarding_from_an_unknown_vendor() -> None:
    body = "We are a new supplier. Nordic Trim OÜ, VAT EE100931558, IBAN EE38 2200 2210 2014 5685. Please register us."
    c = classify(
        msg(
            vendor_id=None,
            claimed_supplier_name="Nordic Trim OÜ",
            from_address="ar@nordictrim.ee",
            body=body,
        ),
        known_vendor=False,
    )
    assert c.kind == "onboarding" and c.vat_id == "EE100931558" and c.iban == "EE382200221020145685"
    forwarded = classify(
        msg(
            channel="invoice",
            vendor_id=None,
            claimed_supplier_name="Nordic Trim OÜ",
            body="Invoice NT-1 from Nordic Trim OÜ. VAT ID EE100931558. IBAN none. New supplier: onboarding needed.",
        ),
        known_vendor=False,
    )
    assert forwarded.kind == "onboarding"


def test_vat_extraction() -> None:
    assert extract_vat_ids("DIČ: CZ71310983, USt-IdNr. DE100000008") == [
        "CZ71310983",
        "DE100000008",
    ]
    assert extract_vat_ids("not a vat: XX12345678") == []


@pytest.mark.parametrize(
    ("sender", "known", "flagged"),
    [
        ("accounts@kovak-trim.cz", "kovaktrim.cz", True),  # the T8 lookalike: one inserted hyphen
        ("ar@kovaktrim.co", "kovaktrim.cz", True),  # tld swap
        ("ar@kovaktrlm.cz", "kovaktrim.cz", True),  # homoglyph l/i
        ("ar@k0vaktrim.cz", "kovaktrim.cz", True),  # zero for o
        ("ar@kovaktrim.cz", "kovaktrim.cz", False),  # the real domain
        ("ar@foamtech.de", "kovaktrim.cz", False),  # unrelated
        ("ar@liberecfoam.cz", "kovaktrim.cz", False),
        ("Kovak Trim <accounts@kovaktrimm.cz>", "kovaktrim.cz", True),
    ],
)
def test_lookalike_detection(sender: str, known: str, flagged: bool) -> None:
    result = lookalike(domain_of(sender), known)
    assert result.flagged is flagged, result
