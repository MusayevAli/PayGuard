"""Classification of vendor messages (spec §7.4): bank-details change, onboarding or other; IBAN
and VAT-id extraction across the five locales; lookalike-domain detection. Pure functions."""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field
from typing import Any, Literal

from schwifty import IBAN
from stdnum.eu import vat as eu_vat

MessageKind = Literal["bank_change", "onboarding", "other"]

BANK_CHANGE_WORDS = (
    "bankovní spojení",
    "bankovního spojení",
    "bankovní účet",
    "bankovního účtu",
    "nový účet",
    "změna účtu",
    "rachunek bankowy",
    "rachunku bankowego",
    "zmiana rachunku",
    "nowy rachunek",
    "numer konta",
    "bankverbindung",
    "neue bankverbindung",
    "kontowechsel",
    "neues konto",
    "bank details",
    "bank account",
    "new account",
    "change of bank",
    "remit to",
    "remittance details",
    "iban",
    "口座",
    "振込先",
    "銀行口座",
)
ONBOARDING_WORDS = (
    "new supplier",
    "new vendor",
    "onboard",
    "onboarding",
    "supplier registration",
    "vendor registration",
    "register us",
    "nový dodavatel",
    "registrace dodavatele",
    "nowy dostawca",
    "rejestracja dostawcy",
    "neuer lieferant",
    "lieferantenanlage",
    "stammdaten",
    "unknown vendor",
    "first invoice",
)

_IBAN_RE = re.compile(r"\b([A-Z]{2}\d{2}(?:[  ]?[A-Z0-9]{4}){2,7}(?:[  ]?[A-Z0-9]{1,4})?)\b")
_VAT_RE = re.compile(
    r"\b((?:AT|BE|BG|CY|CZ|DE|DK|EE|EL|ES|FI|FR|HR|HU|IE|IT|LT|LU|LV|MT|NL|PL|PT|RO|SE|SI|SK)[ ]?[0-9A-Z]{8,12})\b"
)
_HOMOGLYPHS = str.maketrans(
    {
        "0": "o",
        "1": "l",
        "3": "e",
        "5": "s",
        "7": "t",
        "8": "b",
        "ı": "i",
        "ł": "l",
        "ö": "o",
        "ü": "u",
        "ä": "a",
    }
)


@dataclass(frozen=True)
class Classification:
    kind: MessageKind
    iban: str | None
    vat_id: str | None
    reasons: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class LookalikeResult:
    flagged: bool
    from_domain: str
    known_domain: str
    distance: int
    reason: str


def extract_ibans(text: str) -> list[str]:
    """Valid IBANs printed in ``text`` (spaces every four characters allowed), normalised. A match
    may run into a bank name, so the longest validating prefix of each match is taken."""
    out: list[str] = []
    for m in _IBAN_RE.finditer(text.upper()):
        tokens = re.split(r"[\s\u00a0]+", m.group(1).strip())
        for k in range(len(tokens), 0, -1):
            candidate = "".join(tokens[:k])
            try:
                iban = IBAN(candidate)
            except ValueError:
                continue
            if iban.compact not in out:
                out.append(iban.compact)
            break
    return out


def iban_is_valid(text: str) -> bool:
    try:
        IBAN(re.sub(r"\s", "", text))
    except ValueError:
        return False
    return True


def extract_vat_ids(text: str) -> list[str]:
    out: list[str] = []
    for m in _VAT_RE.finditer(text.upper()):
        candidate = m.group(1).replace(" ", "")
        if eu_vat.is_valid(candidate) and candidate not in out:
            out.append(candidate)
    return out


def vat_is_valid(text: str) -> bool:
    return bool(text) and eu_vat.is_valid(text.replace(" ", ""))


def classify(payload: dict[str, Any], *, known_vendor: bool) -> Classification:
    """``bank_change`` when a bank account is offered (structured or in the body) by a known vendor or
    a vendor naming itself; ``onboarding`` when an unknown vendor presents a VAT id or an account;
    ``other`` for dunning letters, remittance advices, contact changes."""
    body = f"{payload.get('subject') or ''}\n{payload.get('body') or ''}"
    details = payload.get("bank_details") or {}
    ibans = extract_ibans(body)
    if details.get("iban") and iban_is_valid(str(details["iban"])):
        ibans.insert(0, re.sub(r"\s", "", str(details["iban"]).upper()))
    iban = ibans[0] if ibans else None
    account_number = details.get("account_number")
    vats = extract_vat_ids(body)
    vat_id = vats[0] if vats else None
    lower = body.lower()
    reasons: list[str] = []
    talks_bank = any(w in lower for w in BANK_CHANGE_WORDS)
    talks_onboarding = any(w in lower for w in ONBOARDING_WORDS)
    has_account = iban is not None or bool(account_number)
    if (
        has_account
        and (known_vendor or payload.get("channel") in ("portal", "invoice"))
        and (talks_bank or details)
    ):
        reasons.append(
            "account offered by a known vendor"
            if known_vendor
            else f"account offered via {payload.get('channel')}"
        )
        return Classification("bank_change", iban, vat_id, reasons)
    if (
        has_account
        and known_vendor is False
        and (talks_onboarding or vat_id is not None or payload.get("channel") == "invoice")
    ):
        reasons.append("unknown vendor with account or VAT id")
        return Classification("onboarding", iban, vat_id, reasons)
    if has_account and talks_bank:
        reasons.append("account change requested by an unresolved sender")
        return Classification("bank_change", iban, vat_id, reasons)
    if not known_vendor and (talks_onboarding or vat_id is not None):
        reasons.append("onboarding request without an account")
        return Classification("onboarding", iban, vat_id, reasons)
    reasons.append("no account change or onboarding content")
    return Classification("other", iban, vat_id, reasons)


def _levenshtein(a: str, b: str) -> int:
    if a == b:
        return 0
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, start=1):
        cur = [i]
        for j, cb in enumerate(b, start=1):
            cur.append(min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (ca != cb)))
        prev = cur
    return prev[-1]


def _fold(domain: str) -> str:
    text = unicodedata.normalize("NFKD", domain.lower().strip())
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    text = (
        text.translate(_HOMOGLYPHS)
        .replace("rn", "m")
        .replace("vv", "w")
        .replace("-", "")
        .replace(".", "")
    )
    return text


def domain_of(address: str) -> str:
    addr = address.strip().lower()
    if "<" in addr and ">" in addr:
        addr = addr[addr.index("<") + 1 : addr.index(">")]
    return addr.rsplit("@", 1)[-1] if "@" in addr else addr


def lookalike(from_domain: str, known_domain: str) -> LookalikeResult:
    """A sender domain that imitates the vendor's known domain: within two edits, a dropped or
    added character or hyphen, or the same letters under homoglyph folding."""
    a, b = from_domain.lower().strip(), known_domain.lower().strip()
    if not a or not b or a == b:
        return LookalikeResult(False, a, b, 0, "same domain" if a == b else "no domain")
    distance = _levenshtein(a, b)
    if distance <= 2:
        return LookalikeResult(True, a, b, distance, f"edit distance {distance} from {b}")
    if _fold(a) == _fold(b):
        return LookalikeResult(True, a, b, distance, f"homoglyph or punctuation variant of {b}")
    a_name, b_name = a.rsplit(".", 1)[0], b.rsplit(".", 1)[0]
    if a_name and a_name == b_name:
        return LookalikeResult(
            True, a, b, distance, f"same name as {b} under another top-level domain"
        )
    return LookalikeResult(False, a, b, distance, "unrelated domain")
