"""Vendor resolution (spec §6 name normalisation, VAT-ID precedence, merged_into) and purchase
order reference parsing (the five formats of A2, quote numbers ignored)."""

from __future__ import annotations

import re
import unicodedata
from difflib import SequenceMatcher
from typing import Literal

from pydantic import BaseModel, ConfigDict

from core.vendors import VendorMaster
from sim.models import PurchaseOrder, Supplier

from .parsed import LocaleHints, ValidatedLine

LEGAL_SUFFIXES: tuple[str, ...] = (
    "spol. s r.o.",
    "s. r. o.",
    "s.r.o.",
    "sro",
    "akciová společnost",
    "a.s.",
    "as",
    "sp. z o. o.",
    "sp. z o.o.",
    "sp z oo",
    "spółka z o.o.",
    "spółka akcyjna",
    "s.a.",
    "sa",
    "gesellschaft mit beschränkter haftung",
    "g.m.b.h.",
    "gmbh",
    "aktiengesellschaft",
    "ag",
    "incorporated",
    "inc.",
    "inc",
    "oü",
    "ou",
    "ltd.",
    "ltd",
    "co., ltd.",
    "k.k.",
    "kk",
    "llc",
    "株式会社",
)
_SUFFIX_TOKENS: list[tuple[str, ...]] = []


def _ascii(text: str) -> str:
    return "".join(c for c in unicodedata.normalize("NFKD", text) if not unicodedata.combining(c))


def _tokens(text: str) -> list[str]:
    t = _ascii(text).lower().replace("&", " ")
    t = re.sub(r"[^\w\s]", " ", t)
    return t.split()


for _suffix in sorted(LEGAL_SUFFIXES, key=len, reverse=True):
    toks = tuple(_tokens(_suffix)) if _suffix != "株式会社" else ("株式会社",)
    if toks and toks not in _SUFFIX_TOKENS:
        _SUFFIX_TOKENS.append(toks)


def normalize_name(name: str) -> str:
    """lowercase, strip legal suffixes, strip punctuation/diacritics, collapse whitespace (§6)."""
    raw = name.strip()
    if raw.startswith("株式会社"):
        raw = raw[len("株式会社") :]
    if raw.endswith("株式会社"):
        raw = raw[: -len("株式会社")]
    toks = _tokens(raw)
    changed = True
    while changed and toks:
        changed = False
        for suffix in sorted(_SUFFIX_TOKENS, key=len, reverse=True):
            n = len(suffix)
            if len(toks) > n and tuple(toks[-n:]) == suffix:
                toks = toks[:-n]
                changed = True
                break
    return " ".join(toks)


def normalize_vat(vat_id: str | None) -> str | None:
    if not vat_id:
        return None
    return re.sub(r"[\s\-.]", "", vat_id).upper() or None


class ResolvedVendor(BaseModel):
    model_config = ConfigDict(extra="forbid", arbitrary_types_allowed=True)
    vendor_id: str
    original_id: str
    matched_by: Literal["vat_id", "name", "variant"]


class VendorIndex:
    def __init__(self, suppliers: list[Supplier], vendors: VendorMaster | None = None) -> None:
        self.suppliers = list(suppliers)
        self._by_id = {s.id: s for s in suppliers}
        self._canonical: dict[str, str] = {}
        if vendors is not None:
            for v in vendors.vendors():
                if v.merged_into:
                    self._canonical[v.id] = v.merged_into
        else:
            by_vat: dict[str, Supplier] = {}
            for s in sorted(suppliers, key=lambda x: (x.onboarded_at, x.id)):
                by_vat.setdefault(normalize_vat(s.vat_id) or s.id, s)
            for s in suppliers:
                canon = by_vat[normalize_vat(s.vat_id) or s.id]
                if canon.id != s.id:
                    self._canonical[s.id] = canon.id
        self._by_vat: dict[str, list[str]] = {}
        self._by_name: dict[str, str] = {}
        self._by_exact: dict[str, str] = {}
        self._by_variant: dict[str, str] = {}
        self._by_email: dict[str, Supplier] = {}
        for s in sorted(suppliers, key=lambda x: (x.onboarded_at, x.id)):
            vat = normalize_vat(s.vat_id)
            if vat:
                self._by_vat.setdefault(vat, []).append(s.id)
            key = normalize_name(s.legal_name)
            self._by_name.setdefault(key, s.id)
            self._by_exact.setdefault(" ".join(s.legal_name.split()), s.id)
            for variant in s.name_variants:
                self._by_exact.setdefault(" ".join(variant.split()), s.id)
                self._by_variant.setdefault(normalize_name(variant), s.id)
            self._by_email[s.email.lower()] = s
            self._by_email.setdefault(s.domain.lower(), s)

    def canonical(self, vendor_id: str) -> str:
        return self._canonical.get(vendor_id, vendor_id)

    def supplier(self, vendor_id: str) -> Supplier | None:
        return self._by_id.get(vendor_id)

    def resolve(self, name: str | None, vat_id: str | None) -> ResolvedVendor | None:
        vat = normalize_vat(vat_id)
        key = normalize_name(name or "")
        exact = " ".join((name or "").split())
        if vat and vat in self._by_vat:
            # a duplicate record shares the VAT id (M1): keep the record whose printed name it is,
            # then the one whose normalised name matches, else the earliest onboarded
            candidates = self._by_vat[vat]
            original = next(
                (c for c in candidates if self._by_exact.get(exact) == c),
                next((c for c in candidates if self._by_name.get(key) == c), candidates[0]),
            )
            return ResolvedVendor(
                vendor_id=self.canonical(original), original_id=original, matched_by="vat_id"
            )
        if not key:
            return None
        if exact in self._by_exact:
            original = self._by_exact[exact]
            return ResolvedVendor(
                vendor_id=self.canonical(original), original_id=original, matched_by="name"
            )
        if key in self._by_name:
            original = self._by_name[key]
            return ResolvedVendor(
                vendor_id=self.canonical(original), original_id=original, matched_by="name"
            )
        if key in self._by_variant:
            original = self._by_variant[key]
            return ResolvedVendor(
                vendor_id=self.canonical(original), original_id=original, matched_by="variant"
            )
        return None

    def by_email(self, address: str | None) -> Supplier | None:
        if not address:
            return None
        addr = address.strip().lower()
        if "<" in addr and ">" in addr:
            addr = addr[addr.index("<") + 1 : addr.index(">")]
        if addr in self._by_email:
            return self._by_email[addr]
        domain = addr.split("@")[-1]
        return self._by_email.get(domain)

    def hints_for(self, supplier_hint: str | None, sender: str | None) -> LocaleHints:
        sup = self._by_id.get(supplier_hint) if supplier_hint else None
        if sup is None:
            sup = self.by_email(sender)
        if sup is None:
            return LocaleHints()
        return LocaleHints(
            number_locale=sup.number_locale,
            date_format=sup.date_format,
            language=sup.language,
            supplier_id=sup.id,
            currency=sup.currency,
        )


_PO_NUMBER = re.compile(r"(?<!\d)(\d{5})(?!\d)")
_QUOTE = re.compile(r"\b(?:Q|AN|OF|QUOTE|NAB[IÍ]DKA|ANGEBOT|OFERTA|見積)\b", re.I)


def parse_po_refs(texts: list[str]) -> list[str]:
    """Canonical ``PO-NNNNN`` ids from references printed in any of the A2 styles."""
    out: list[str] = []
    for text in texts:
        if not text:
            continue
        cleaned = re.sub(r"\b\d{4}\b", " ", text)  # years like /2026 never carry the PO number
        if _QUOTE.search(cleaned) and "PO" not in cleaned.upper():
            continue
        for m in _PO_NUMBER.finditer(cleaned):
            ref = f"PO-{m.group(1)}"
            if ref not in out:
                out.append(ref)
    return out


def _desc_key(text: str) -> str:
    return " ".join(_tokens(text))


def line_similarity(
    invoice_line: ValidatedLine, po_line_sku: str, descriptions: list[str]
) -> float:
    if invoice_line.sku and invoice_line.sku.strip().upper() == po_line_sku.upper():
        return 1.0
    inv_text = _desc_key(invoice_line.description)
    if po_line_sku.lower() in inv_text:
        return 0.95
    best = 0.0
    for d in descriptions:
        best = max(best, SequenceMatcher(None, inv_text, _desc_key(d)).ratio())
    return best


def score_po(lines: list[ValidatedLine], po: PurchaseOrder) -> float:
    """Fraction of invoice lines that match a PO line by SKU or description."""
    if not lines:
        return 0.0
    hits = 0
    for ln in lines:
        if (
            max(
                (
                    line_similarity(ln, pl.sku, [pl.description, pl.description_local])
                    for pl in po.lines
                ),
                default=0.0,
            )
            >= 0.6
        ):
            hits += 1
    return hits / len(lines)
