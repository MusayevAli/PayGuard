from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from agents.resolve import VendorIndex, normalize_name, parse_po_refs
from core.vendors import VendorMaster
from sim.worldgen.suppliers import build_suppliers


@pytest.fixture
def index(tmp_path: Path) -> VendorIndex:
    suppliers = build_suppliers(42)
    vendors = VendorMaster(tmp_path / "v.db")
    vendors.seed_from_suppliers(suppliers, now=datetime(2026, 10, 1, tzinfo=UTC))
    idx = VendorIndex(suppliers, vendors)
    yield idx
    vendors.close()


def test_normalize_name_per_spec() -> None:
    assert (
        normalize_name("Kovak Trim s.r.o.")
        == normalize_name("KOVAK TRIM S.R.O.")
        == normalize_name("Kovak-Trim sro")
        == "kovak trim"
    )
    assert (
        normalize_name("Liberec Foam a.s.") == normalize_name("LIBEREC FOAM AS") == "liberec foam"
    )
    assert normalize_name("Česká Kabeláž s.r.o.") == "ceska kabelaz"
    assert (
        normalize_name("Gliwice Fasteners Sp. z o.o.")
        == normalize_name("Gliwice Fasteners sp. z o. o.")
        == "gliwice fasteners"
    )
    assert (
        normalize_name("Foamtech GmbH")
        == normalize_name("Foamtech Gesellschaft mit beschränkter Haftung")
        == "foamtech"
    )
    assert normalize_name("Great Lakes Wire & Cable Inc.") == "great lakes wire cable"
    assert normalize_name("Nordic Trim OÜ") == "nordic trim"
    assert normalize_name("  Alpenmech   GmbH ") == "alpenmech"


def test_vat_id_beats_name_and_variants_resolve(index: VendorIndex) -> None:
    kovak = next(s for s in index.suppliers if s.id == "V-0001")
    r = index.resolve("KOVAK TRIM S.R.O.", None)
    assert r is not None and r.vendor_id == "V-0001" and r.matched_by in ("name", "variant")
    r = index.resolve("Kovak-Trim sro", None)
    assert r is not None and r.vendor_id == "V-0001"
    r = index.resolve("Some Other Name", kovak.vat_id)
    assert r is not None and r.vendor_id == "V-0001" and r.matched_by == "vat_id"
    r = index.resolve("Some Other Name", kovak.vat_id.replace("CZ", "CZ ").lower())
    assert r is not None and r.vendor_id == "V-0001"
    foam = next(s for s in index.suppliers if s.id == "V-0002")
    r = index.resolve(kovak.legal_name, foam.vat_id)  # name says Kovak, VAT says Foamtech: VAT wins
    assert r is not None and r.vendor_id == "V-0002"


def test_merged_into_resolves_to_the_canonical_vendor(index: VendorIndex) -> None:
    r = index.resolve("LIBEREC FOAM AS", None)
    assert r is not None and r.vendor_id == "V-0007" and r.original_id == "V-0033"
    dup = next(s for s in index.suppliers if s.id == "V-0033")
    r = index.resolve("whatever", dup.vat_id)
    assert r is not None and r.vendor_id == "V-0007"


def test_unknown_vendor_is_none(index: VendorIndex) -> None:
    assert index.resolve("Nordic Trim OÜ", "EE100931558") is None
    assert index.resolve("", None) is None
    assert (
        index.by_email("fakturace@kovaktrim.cz") is not None
        and index.by_email("accounts@kovak-trim.cz") is None
    )


@pytest.mark.parametrize(
    ("texts", "expected"),
    [
        (["PO-10234"], ["PO-10234"]),
        (["PO 10234"], ["PO-10234"]),
        (["10234"], ["PO-10234"]),
        (["P.O. #10234"], ["PO-10234"]),
        (["Obj. č. 10234"], ["PO-10234"]),
        (["Zam. nr 10234"], ["PO-10234"]),
        (["Bestell-Nr. 10234"], ["PO-10234"]),
        (["Order no. 10234"], ["PO-10234"]),
        (["注文番号 10234"], ["PO-10234"]),
        (["PO-10234/2026"], ["PO-10234"]),
        (["PO-10234, PO-10235"], ["PO-10234", "PO-10235"]),
        (["Nabídka Q-2026-118"], []),
        (["Angebot AN-2026-118"], []),
        ([""], []),
        ([], []),
    ],
)
def test_parse_po_refs_all_formats(texts: list[str], expected: list[str]) -> None:
    assert parse_po_refs(texts) == expected
