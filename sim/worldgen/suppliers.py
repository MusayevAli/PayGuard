"""The 40 suppliers of spec §2.2 with name variants, valid identifiers, locale settings,
price lists, and the planted master-data faults M1 (duplicate vendor), M2 (departed
contact) and M3 (price-list gap)."""

from __future__ import annotations

import random
import re
import unicodedata
from dataclasses import dataclass, field
from datetime import date
from decimal import ROUND_HALF_EVEN, Decimal

from sim.models import (
    Address,
    Category,
    Currency,
    DateFormat,
    Language,
    NumberLocale,
    PriceList,
    Supplier,
    SupplierCountry,
    TemplateId,
    Terms,
)
from sim.worldgen.catalog import PRICE_FACTOR, SKUS_BY_CATEGORY
from sim.worldgen.identifiers import gen_iban_account, gen_jp_account, gen_us_account, gen_vat_id
from sim.worldgen.rng import stream


@dataclass(frozen=True)
class SupplierSeed:
    id: str
    legal_name: str
    country: SupplierCountry
    category: Category
    city: str
    postal_code: str
    street: str
    currency: Currency
    template: TemplateId
    terms: Terms
    contact: str
    onboarded: date
    domain: str | None = None
    price_overrides: dict[str, int] = field(default_factory=dict)
    price_gap: bool = False
    variants: tuple[str, ...] = ()


# fmt: off
SUPPLIER_SEEDS: list[SupplierSeed] = [
    SupplierSeed("V-0001", "Kovak Trim s.r.o.", "CZ", "seat frames", "Liberec", "460 01", "Průmyslová 12", "EUR", "cz_faktura_classic", "net30", "petra.vlcek", date(2018, 3, 12), domain="kovaktrim.cz", price_overrides={"SF-1001": 1390, "SF-1002": 1390}),
    SupplierSeed("V-0002", "Foamtech GmbH", "DE", "foam", "Augsburg", "86159", "Haunstetter Straße 112", "EUR", "de_rechnung", "net30", "lukas.brandt", date(2016, 9, 1), price_overrides={"FM-2001": 1390}),
    SupplierSeed("V-0003", "Gliwice Fasteners Sp. z o.o.", "PL", "fasteners", "Gliwice", "44-100", "ul. Portowa 18", "PLN", "pl_faktura_vat", "net30", "agnieszka.kowalczyk", date(2017, 5, 22)),
    SupplierSeed("V-0004", "Lasertech Bohemia s.r.o.", "CZ", "wire harnesses", "Mladá Boleslav", "293 01", "Jičínská 1348", "EUR", "cz_faktura_totals_top", "net30", "petra.vlcek", date(2017, 2, 6)),
    SupplierSeed("V-0005", "Hofmann Mechanik GmbH", "DE", "headrest mechanisms", "Ingolstadt", "85055", "Manchinger Straße 80", "EUR", "de_rechnung_two_page", "net30", "lukas.brandt", date(2015, 11, 16)),
    SupplierSeed("V-0006", "Werkzeugbau Steiner GmbH", "AT", "tooling", "Linz", "4020", "Industriezeile 35", "EUR", "de_rechnung", "net45", "tomas.horak", date(2016, 4, 4)),
    SupplierSeed("V-0007", "Liberec Foam a.s.", "CZ", "foam", "Liberec", "460 10", "Ještědská 240", "CZK", "cz_faktura_classic", "net45", "petra.vlcek", date(2019, 6, 3)),
    SupplierSeed("V-0008", "Horák Sedačky s.r.o.", "CZ", "trim fabric", "Jablonec nad Nisou", "466 01", "Podhorská 62", "CZK", "cz_faktura_totals_top", "net30", "petra.vlcek", date(2016, 8, 15)),
    SupplierSeed("V-0009", "Jizera Pružiny a.s.", "CZ", "headrest mechanisms", "Semily", "513 01", "Nádražní 102", "CZK", "cz_faktura_classic", "net30", "petra.vlcek", date(2015, 1, 19)),
    SupplierSeed("V-0010", "Böhm Spedition GmbH", "DE", "freight", "Regensburg", "93055", "Donaustaufer Straße 150", "EUR", "de_rechnung", "net30", "tomas.horak", date(2018, 10, 8)),
    SupplierSeed("V-0011", "Plastika Turnov s.r.o.", "CZ", "fasteners", "Turnov", "511 01", "Přepeřská 1302", "CZK", "cz_faktura_totals_top", "2/10 net30", "petra.vlcek", date(2020, 2, 10)),
    SupplierSeed("V-0012", "Präzisionswerkzeuge Hahn GmbH", "DE", "tooling", "Nürnberg", "90441", "Sigmundstraße 220", "EUR", "de_rechnung_two_page", "net45", "tomas.horak", date(2014, 7, 7)),
    SupplierSeed("V-0013", "Česká Kabeláž s.r.o.", "CZ", "wire harnesses", "Kolín", "280 02", "Ovčárecká 312", "CZK", "cz_faktura_classic", "net30", "petra.vlcek", date(2018, 5, 28)),
    SupplierSeed("V-0014", "Pianki Śląskie Sp. z o.o.", "PL", "foam", "Zabrze", "41-800", "ul. Hagera 41", "PLN", "pl_faktura_vat_columns", "net30", "agnieszka.kowalczyk", date(2019, 9, 9)),
    SupplierSeed("V-0015", "Zabrze Fasteners Sp. z o.o.", "PL", "fasteners", "Zabrze", "41-806", "ul. Wolności 262", "PLN", "pl_faktura_vat", "net30", "jana.svobodova", date(2018, 1, 29)),
    SupplierSeed("V-0016", "Nástrojárna Vrchlabí s.r.o.", "CZ", "tooling", "Vrchlabí", "543 01", "Lánovská 1480", "EUR", "cz_faktura_totals_top", "net45", "tomas.horak", date(2014, 3, 3)),
    SupplierSeed("V-0017", "Metallbau Richter GmbH", "DE", "seat frames", "Landshut", "84030", "Siemensstraße 9", "EUR", "de_rechnung", "2/10 net30", "lukas.brandt", date(2017, 6, 12)),
    SupplierSeed("V-0018", "Tkaniny Śląskie Sp. z o.o.", "PL", "trim fabric", "Bielsko-Biała", "43-300", "ul. Partyzantów 44", "PLN", "pl_faktura_vat_columns", "net45", "agnieszka.kowalczyk", date(2016, 11, 21), price_gap=True),
    SupplierSeed("V-0019", "Textil Krkonoše a.s.", "CZ", "trim fabric", "Trutnov", "541 01", "Horská 54", "CZK", "cz_faktura_classic", "net30", "petra.vlcek", date(2017, 4, 10)),
    SupplierSeed("V-0020", "Katowice Metal Forming S.A.", "PL", "seat frames", "Katowice", "40-203", "ul. Roździeńskiego 188", "EUR", "pl_faktura_vat", "net30", "agnieszka.kowalczyk", date(2015, 10, 5)),
    SupplierSeed("V-0021", "TransGliwice Logistyka Sp. z o.o.", "PL", "freight", "Gliwice", "44-109", "ul. Toszecka 101", "PLN", "pl_faktura_vat", "net30", "tomas.horak", date(2020, 6, 1)),
    SupplierSeed("V-0022", "MoraviaFoam s.r.o.", "CZ", "foam", "Prostějov", "796 01", "Kralická 23", "EUR", "cz_faktura_totals_top", "net30", "petra.vlcek", date(2021, 3, 15)),
    SupplierSeed("V-0023", "Schwaben Kabel GmbH", "DE", "wire harnesses", "Ulm", "89079", "Einsteinstraße 55", "EUR", "de_rechnung", "net30", "lukas.brandt", date(2019, 2, 25)),
    SupplierSeed("V-0024", "Elektro-Wiązki Tychy Sp. z o.o.", "PL", "wire harnesses", "Tychy", "43-100", "ul. Strefowa 7", "PLN", "pl_faktura_vat_columns", "net30", "agnieszka.kowalczyk", date(2018, 8, 20)),
    SupplierSeed("V-0025", "Spojovací materiál Brno a.s.", "CZ", "fasteners", "Brno", "619 00", "Vídeňská 118", "CZK", "cz_faktura_classic", "net30", "petra.vlcek", date(2016, 2, 1)),
    SupplierSeed("V-0026", "Mechanika Zagłówków Sp. z o.o.", "PL", "headrest mechanisms", "Sosnowiec", "41-200", "ul. Narutowicza 59", "EUR", "pl_faktura_vat", "net45", "agnieszka.kowalczyk", date(2021, 1, 11)),
    SupplierSeed("V-0027", "Bayerische Textilwerke AG", "DE", "trim fabric", "Hof", "95028", "Kulmbacher Straße 300", "EUR", "de_rechnung_two_page", "net30", "lukas.brandt", date(2013, 9, 30)),
    SupplierSeed("V-0028", "Narzędziownia Opole Sp. z o.o.", "PL", "tooling", "Opole", "45-641", "ul. Oświęcimska 100", "PLN", "pl_faktura_vat_columns", "net45", "tomas.horak", date(2017, 12, 4)),
    SupplierSeed("V-0029", "Fränkische Schrauben GmbH", "DE", "fasteners", "Schwabach", "91126", "Nürnberger Straße 88", "EUR", "de_rechnung", "2/10 net30", "lukas.brandt", date(2018, 4, 16)),
    SupplierSeed("V-0030", "Čechotrans s.r.o.", "CZ", "freight", "Liberec", "460 07", "Dr. Milady Horákové 185", "CZK", "cz_faktura_classic", "net30", "tomas.horak", date(2019, 11, 25)),
    SupplierSeed("V-0031", "Wrocław Wire Technologies S.A.", "PL", "wire harnesses", "Wrocław", "54-156", "ul. Stargardzka 8", "EUR", "pl_faktura_vat", "2/10 net30", "agnieszka.kowalczyk", date(2019, 5, 13)),
    SupplierSeed("V-0032", "Isar Polsterschaum GmbH", "DE", "foam", "Freising", "85356", "Clemensänger-Ring 12", "EUR", "de_rechnung", "net30", "lukas.brandt", date(2020, 8, 3)),
    SupplierSeed("V-0033", "LIBEREC FOAM AS", "CZ", "foam", "Liberec", "460 10", "Ještědská 240", "CZK", "cz_faktura_classic", "net45", "petra.vlcek", date(2023, 2, 20)),
    SupplierSeed("V-0034", "Poliuretany Rybnik Sp. z o.o.", "PL", "foam", "Rybnik", "44-200", "ul. Podmiejska 95", "PLN", "pl_faktura_vat_columns", "net30", "agnieszka.kowalczyk", date(2022, 4, 25)),
    SupplierSeed("V-0035", "Allgäu Sitzsysteme GmbH", "DE", "seat frames", "Kempten", "87437", "Heisinger Straße 14", "EUR", "de_rechnung_two_page", "net45", "lukas.brandt", date(2021, 7, 19)),
    SupplierSeed("V-0036", "Ocelové Rámy Ostrava s.r.o.", "CZ", "seat frames", "Ostrava", "702 00", "Vratimovská 624", "CZK", "cz_faktura_totals_top", "net45", "petra.vlcek", date(2022, 9, 5)),
    SupplierSeed("V-0037", "Śruby Częstochowa S.A.", "PL", "fasteners", "Częstochowa", "42-202", "ul. Legionów 92", "PLN", "pl_faktura_vat", "net30", "agnieszka.kowalczyk", date(2016, 6, 27)),
    SupplierSeed("V-0038", "Alpenmech GmbH", "AT", "headrest mechanisms", "Graz", "8020", "Puchstraße 17", "EUR", "de_rechnung_two_page", "net30", "lukas.brandt", date(2019, 3, 18)),
    SupplierSeed("V-0039", "株式会社ミナト精機", "JP", "headrest mechanisms", "横浜市", "220-0012", "神奈川県横浜市西区みなとみらい3-6-1", "JPY", "jp_seikyusho", "net45", "lukas.brandt", date(2012, 10, 1), domain="minato-seiki.co.jp", variants=("Minato Seiki Co., Ltd.", "MINATO SEIKI CO., LTD.", "Minato Seiki K.K.", "ミナト精機株式会社")),
    SupplierSeed("V-0040", "Great Lakes Wire & Cable Inc.", "US", "wire harnesses", "Grand Rapids, MI", "49512", "4120 36th Street SE", "USD", "us_invoice", "net30", "lukas.brandt", date(2015, 5, 4), domain="greatlakeswire.com"),
]
# fmt: on

LANGUAGE: dict[str, Language] = {
    "CZ": "cs",
    "PL": "pl",
    "DE": "de",
    "AT": "de",
    "JP": "ja",
    "US": "en",
}
NUMBER_LOCALE: dict[str, NumberLocale] = {
    "CZ": "1 234,56",
    "PL": "1 234,56",
    "DE": "1.234,56",
    "AT": "1.234,56",
    "JP": "1,234.56",
    "US": "1,234.56",
}
DATE_FORMAT: dict[str, DateFormat] = {
    "CZ": "DD.MM.YYYY",
    "PL": "YYYY-MM-DD",
    "DE": "DD.MM.YYYY",
    "AT": "DD.MM.YYYY",
    "JP": "YYYY-MM-DD",
    "US": "MM/DD/YYYY",
}
BILLING_MAILBOX: dict[Language, str] = {
    "cs": "fakturace",
    "pl": "faktury",
    "de": "rechnung",
    "en": "billing",
    "ja": "seikyu",
}
TLD: dict[str, str] = {"CZ": "cz", "PL": "pl", "DE": "de", "AT": "at", "JP": "co.jp", "US": "com"}
PHONE_PREFIX: dict[str, str] = {
    "CZ": "+420",
    "PL": "+48",
    "DE": "+49",
    "AT": "+43",
    "JP": "+81",
    "US": "+1",
}

LEGAL_SUFFIXES = [
    "s.r.o.",
    "spol. s r.o.",
    "a.s.",
    "Sp. z o.o.",
    "S.A.",
    "GmbH",
    "AG",
    "Inc.",
    "OÜ",
    "株式会社",
    "AS",
]


def strip_diacritics(text: str) -> str:
    return "".join(c for c in unicodedata.normalize("NFKD", text) if not unicodedata.combining(c))


def base_name(legal_name: str) -> str:
    name = legal_name
    for suffix in sorted(LEGAL_SUFFIXES, key=len, reverse=True):
        if name.endswith(" " + suffix):
            name = name[: -len(suffix) - 1]
        if name.startswith(suffix):
            name = name[len(suffix) :]
    return name.strip()


def slug(text: str) -> str:
    ascii_text = strip_diacritics(text).lower()
    ascii_text = ascii_text.replace("&", "and")
    return re.sub(r"[^a-z0-9]+", "", ascii_text)


ALT_SUFFIX: dict[str, list[str]] = {
    "s.r.o.": ["spol. s r.o.", "sro", "s. r. o."],
    "a.s.": ["akciová společnost", "as"],
    "Sp. z o.o.": ["sp. z o. o.", "Sp z oo", "Spółka z o.o."],
    "S.A.": ["SA", "Spółka Akcyjna"],
    "GmbH": ["Gesellschaft mit beschränkter Haftung", "G.m.b.H."],
    "AG": ["Aktiengesellschaft"],
    "Inc.": ["Inc", "Incorporated"],
    "AS": ["a.s."],
}


def name_variants(rng: random.Random, legal_name: str) -> list[str]:
    base = base_name(legal_name)
    suffix = legal_name[len(base) :].strip()
    candidates: list[str] = [base, legal_name.upper(), strip_diacritics(legal_name)]
    if suffix in ALT_SUFFIX:
        candidates.extend(f"{base} {alt}" for alt in ALT_SUFFIX[suffix])
    candidates.append(base.upper())
    candidates.append(strip_diacritics(base).lower())
    if "&" in base:
        candidates.append(base.replace("&", "and"))
    seen: list[str] = []
    for c in candidates:
        c = re.sub(r"\s+", " ", c).strip()
        if c and c != legal_name and c not in seen:
            seen.append(c)
    count = rng.randrange(3, 6)
    picked = seen[:count]
    while len(picked) < 3:
        picked.append(f"{base} ({len(picked)})")
    return picked


def _scale_price(eur_minor: int, currency: Currency, factor: Decimal) -> int:
    value = Decimal(eur_minor) * Decimal(PRICE_FACTOR[currency]) * factor
    if currency == "JPY":
        value = value / Decimal(100)  # EUR cents -> whole yen at the static factor
        return int(value.quantize(Decimal(1), rounding=ROUND_HALF_EVEN))
    rounded = int(value.quantize(Decimal(1), rounding=ROUND_HALF_EVEN))
    if rounded >= 100_000:
        rounded = (rounded // 1000) * 1000  # tooling-sized prices end in round hundreds
    elif rounded >= 10_000:
        rounded = (rounded // 10) * 10
    return max(rounded, 1)


def build_price_lists(rng: random.Random, seed: SupplierSeed) -> list[PriceList]:
    skus = SKUS_BY_CATEGORY[seed.category]
    variation = Decimal(rng.randrange(92, 109)) / Decimal(100)
    h1: dict[str, int] = {}
    h2: dict[str, int] = {}
    for sku in skus:
        p1 = _scale_price(sku.eur_minor, seed.currency, variation)
        uplift = Decimal(rng.randrange(100, 107)) / Decimal(100)
        p2 = _scale_price(sku.eur_minor, seed.currency, variation * uplift)
        if sku.sku in seed.price_overrides:
            p2 = seed.price_overrides[sku.sku]
            p1 = int((Decimal(p2) * Decimal("0.97")).quantize(Decimal(1), rounding=ROUND_HALF_EVEN))
        h1[sku.sku] = p1
        h2[sku.sku] = p2
    if seed.price_gap:
        return [
            PriceList(
                id=f"PL-{seed.id}-2026A",
                supplier_id=seed.id,
                effective_from=date(2026, 1, 1),
                effective_to=date(2026, 9, 30),
                currency=seed.currency,
                prices=h1,
            ),
            PriceList(
                id=f"PL-{seed.id}-2026B",
                supplier_id=seed.id,
                effective_from=date(2026, 10, 15),
                effective_to=None,
                currency=seed.currency,
                prices=h2,
            ),
        ]
    return [
        PriceList(
            id=f"PL-{seed.id}-2026H1",
            supplier_id=seed.id,
            effective_from=date(2026, 1, 1),
            effective_to=date(2026, 6, 30),
            currency=seed.currency,
            prices=h1,
        ),
        PriceList(
            id=f"PL-{seed.id}-2026H2",
            supplier_id=seed.id,
            effective_from=date(2026, 7, 1),
            effective_to=None,
            currency=seed.currency,
            prices=h2,
        ),
    ]


def applicable_price_list(supplier: Supplier, on: date) -> PriceList | None:
    for pl in supplier.price_lists:
        if pl.effective_from <= on and (pl.effective_to is None or on <= pl.effective_to):
            return pl
    return None


def build_suppliers(seed: int) -> list[Supplier]:
    rng = stream(seed, "suppliers")
    suppliers: list[Supplier] = []
    shared_vat: dict[str, str] = {}
    used_vat: set[str] = set()
    for s in SUPPLIER_SEEDS:
        lang = LANGUAGE[s.country]
        if s.id == "V-0033":
            vat_id = shared_vat["V-0007"]  # M1: the duplicate record carries the same VAT ID
        else:
            vat_id = gen_vat_id(rng, s.country)
            while vat_id in used_vat:
                vat_id = gen_vat_id(rng, s.country)
            used_vat.add(vat_id)
        shared_vat[s.id] = vat_id
        if s.country == "US":
            account = gen_us_account(rng)
        elif s.country == "JP":
            account = gen_jp_account(rng)
        else:
            exclude = None
            if s.id == "V-0033":
                exclude = next(x.bank_account.bank_code for x in suppliers if x.id == "V-0007")
            account = gen_iban_account(rng, s.country, exclude_bank=exclude)
        domain = s.domain or f"{slug(base_name(s.legal_name))}.{TLD[s.country]}"
        if s.id == "V-0033":
            domain = "liberecfoam.cz"
        registration = {
            "CZ": vat_id[2:],
            "PL": f"{rng.randrange(100000, 999999):010d}",
            "DE": f"HRB {rng.randrange(10000, 99999)}",
            "AT": f"FN {rng.randrange(100000, 499999)}k",
            "JP": vat_id[1:],
            "US": f"MI {rng.randrange(100000, 999999)}",
        }[s.country]
        phone_digits = " ".join(str(rng.randrange(100, 999)) for _ in range(3))
        suppliers.append(
            Supplier(
                id=s.id,
                legal_name=s.legal_name,
                name_variants=list(s.variants) if s.variants else name_variants(rng, s.legal_name),
                country=s.country,
                currency=s.currency,
                vat_id=vat_id,
                bank_account=account,
                terms=s.terms,
                purchasing_contact=s.contact,
                onboarded_at=s.onboarded,
                invoice_template_id=s.template,
                language=lang,
                number_locale=NUMBER_LOCALE[s.country],
                date_format=DATE_FORMAT[s.country],
                email=f"{BILLING_MAILBOX[lang]}@{domain}",
                domain=domain,
                phone=f"{PHONE_PREFIX[s.country]} {phone_digits}",
                address=Address(
                    street=s.street, city=s.city, postal_code=s.postal_code, country=s.country
                ),
                category=s.category,
                registration_id=registration,
                price_lists=build_price_lists(rng, s),
            )
        )
    return suppliers
