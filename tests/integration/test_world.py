"""Acceptance tests for `make world` (spec §17 Part 1, §2.2–§2.10)."""

from __future__ import annotations

import hashlib
import json
from collections import Counter
from datetime import date, datetime
from pathlib import Path

import pytest
from schwifty import IBAN
from stdnum.ee import kmkr
from stdnum.eu import vat as eu_vat
from stdnum.jp import cn as jp_cn
from stdnum.us import ein as us_ein

from sim.models import AnswerKey, Event, GoodsReceipt, Invoice, Person, PurchaseOrder, Supplier
from sim.mt940 import parse_mt940, write_mt940
from tests.conftest import load_json

START = date(2026, 10, 1)
END = date(2026, 11, 15)


def sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


@pytest.fixture(scope="module")
def suppliers(world_dir: Path) -> list[Supplier]:
    return [Supplier.model_validate(s) for s in load_json(world_dir / "suppliers.json")]


@pytest.fixture(scope="module")
def pos(world_dir: Path) -> list[PurchaseOrder]:
    return [PurchaseOrder.model_validate(p) for p in load_json(world_dir / "pos.json")]


@pytest.fixture(scope="module")
def receipts(world_dir: Path) -> list[GoodsReceipt]:
    return [GoodsReceipt.model_validate(r) for r in load_json(world_dir / "receipts.json")]


@pytest.fixture(scope="module")
def invoices(world_dir: Path) -> list[Invoice]:
    return [Invoice.model_validate(i) for i in load_json(world_dir / "invoices_truth.json")]


@pytest.fixture(scope="module")
def people(world_dir: Path) -> list[Person]:
    return [Person.model_validate(p) for p in load_json(world_dir / "people.json")]


@pytest.fixture(scope="module")
def answer_key(world_dir: Path) -> AnswerKey:
    return AnswerKey.model_validate(load_json(world_dir / "answer_key.json"))


def test_generation_under_60_seconds(world_dir: Path) -> None:
    elapsed = float((world_dir / "GENERATION_SECONDS").read_text())
    assert elapsed < 60, f"generation took {elapsed:.1f}s"


def test_outputs_exist(world_dir: Path) -> None:
    for name in ("suppliers.json", "pos.json", "receipts.json", "people.json", "fx_rates.json",
                 "answer_key.json", "world.jsonl", "invoices_truth.json"):  # fmt: skip
        assert (world_dir / name).is_file(), name
    for d in ("invoices", "emails", "statements"):
        assert (world_dir / d).is_dir(), d


def test_determinism_two_runs_identical(world_dir: Path, world_dir_second_run: Path) -> None:
    h1 = sha256_file(world_dir / "world.jsonl")
    h2 = sha256_file(world_dir_second_run / "world.jsonl")
    assert h1 == h2, "world.jsonl differs between two runs with the same seed"
    files1 = sorted(p.relative_to(world_dir) for p in world_dir.rglob("*") if p.is_file() and p.name != "GENERATION_SECONDS")
    files2 = sorted(p.relative_to(world_dir_second_run) for p in world_dir_second_run.rglob("*") if p.is_file())
    assert files1 == files2
    different = [str(rel) for rel in files1 if sha256_file(world_dir / rel) != sha256_file(world_dir_second_run / rel)]
    assert different == [], f"files differ between runs: {different[:10]}"


def test_counts_and_distributions(suppliers: list[Supplier], pos: list[PurchaseOrder], invoices: list[Invoice],
                                  world_events: list[Event]) -> None:  # fmt: skip
    assert len(suppliers) == 40
    assert Counter(s.country for s in suppliers) == {"CZ": 14, "PL": 12, "DE": 10, "AT": 2, "JP": 1, "US": 1}
    assert {s.language for s in suppliers} == {"cs", "pl", "de", "en", "ja"}
    assert {s.category for s in suppliers} == {"seat frames", "foam", "trim fabric", "headrest mechanisms",
                                               "wire harnesses", "fasteners", "tooling", "freight"}  # fmt: skip
    for s in suppliers:
        assert 3 <= len(s.name_variants) <= 5
        assert s.terms in {"net30", "net45", "2/10 net30"}
    assert len(pos) == 160
    assert all(1 <= len(p.lines) <= 4 for p in pos)
    assert {ln.uom for p in pos for ln in p.lines} <= {"pc", "box50", "kg", "m"}
    amended = [e for e in world_events if e.type == "po_amended"]
    assert len(amended) == 12
    assert len({e.payload["po_id"] for e in amended}) == 12
    assert len(invoices) == 300
    fmt = Counter(i.format.value for i in invoices)
    assert fmt["structured"] == 90, fmt
    assert fmt["eml"] == 60, fmt
    assert fmt["pdf"] + fmt["png"] == 150, fmt
    assert fmt["png"] / (fmt["pdf"] + fmt["png"]) >= 0.20, fmt
    rendered = [i for i in invoices if i.format.value in ("pdf", "png", "eml")]
    assert len({i.template_id for i in rendered}) == 8
    assert all(i.template_id is None for i in invoices if i.format.value == "structured")
    assert len({i.id for i in invoices}) == 300
    received = [e for e in world_events if e.type == "invoice_received"]
    assert len(received) == 300
    assert {e.payload["invoice_id"] for e in received} == {i.id for i in invoices}
    per_day = Counter(e.known_at.date() for e in received)
    assert min(per_day) >= START and max(per_day) <= END
    assert len(per_day) >= 40  # spread across the window, roughly 7 per sim-day


def test_every_iban_and_vat_id_is_valid(suppliers: list[Supplier], invoices: list[Invoice]) -> None:
    for s in suppliers:
        if s.country in {"DE", "CZ", "PL", "AT"}:
            assert eu_vat.is_valid(s.vat_id), (s.id, s.vat_id)
            assert s.vat_id.startswith(s.country)
            assert s.iban is not None and IBAN(s.iban, validate_bban=True).country_code == s.country, s.id
        elif s.country == "JP":
            assert s.vat_id.startswith("T") and jp_cn.is_valid(s.vat_id[1:]), s.vat_id
            assert s.iban is None and s.bank_account.kind == "jp_domestic"
        elif s.country == "US":
            assert us_ein.is_valid(s.vat_id), s.vat_id
            assert s.iban is None and s.bank_account.kind == "us_aba"
    assert len({s.iban for s in suppliers if s.iban}) == len([s for s in suppliers if s.iban])
    nordic = [i for i in invoices if i.supplier_name_as_written.startswith("Nordic Trim")]
    assert nordic and nordic[0].supplier_id is None
    assert nordic[0].vat_id_as_written and kmkr.is_valid(nordic[0].vat_id_as_written)
    assert nordic[0].bank_details is not None and IBAN(nordic[0].bank_details.iban).country_code == "EE"


def test_documents_exist_and_match_truth(world_dir: Path, invoices: list[Invoice], world_events: list[Event]) -> None:
    by_id = {e.payload["invoice_id"]: e for e in world_events if e.type == "invoice_received"}
    png_sizes = []
    for inv in invoices:
        path = world_dir / inv.document_path
        assert path.is_file(), inv.document_path
        ev = by_id[inv.id]
        assert ev.payload["format"] == inv.format.value
        assert ev.payload["document_path"] == inv.document_path
        assert ev.payload["sha256"] == hashlib.sha256(path.read_bytes()).hexdigest() == inv.sha256
        if inv.format.value == "structured":
            assert path.suffix == ".json"
            doc = json.loads(path.read_text(encoding="utf-8"))
            assert isinstance(doc, dict) and len(doc) >= 8
        elif inv.format.value == "pdf":
            assert path.suffix == ".pdf" and path.read_bytes()[:5] == b"%PDF-"
        elif inv.format.value == "png":
            assert path.suffix == ".png" and path.read_bytes()[:8] == b"\x89PNG\r\n\x1a\n"
            png_sizes.append(path.stat().st_size)
        else:
            assert path.suffix == ".eml"
            text = path.read_text(encoding="utf-8", errors="replace")
            for header in ("From:", "To:", "Subject:", "Message-ID:", "Date:", "Content-Type:"):
                assert header in text, (inv.id, header)
            assert ev.payload["attachments"], inv.id
    assert png_sizes and min(png_sizes) > 50_000  # real 200-dpi rasters, not thumbnails


def test_structured_invoices_use_supplier_language_field_names(world_dir: Path, invoices: list[Invoice],
                                                              suppliers: list[Supplier]) -> None:  # fmt: skip
    lang = {s.id: s.language for s in suppliers}
    qty_names = {"de": "Menge", "cs": "Množství", "pl": "Ilość", "en": "quantity", "ja": "数量"}
    seen = set()
    for inv in invoices:
        if inv.format.value != "structured" or inv.supplier_id is None:
            continue
        text = (world_dir / inv.document_path).read_text(encoding="utf-8")
        assert qty_names[lang[inv.supplier_id]] in text, (inv.id, lang[inv.supplier_id])
        seen.add(lang[inv.supplier_id])
    assert {"de", "cs", "pl"} <= seen


def test_emails_have_threads_and_inconsistent_attachment_names(world_dir: Path, invoices: list[Invoice],
                                                              world_events: list[Event]) -> None:  # fmt: skip
    by_id = {e.payload["invoice_id"]: e for e in world_events if e.type == "invoice_received"}
    emls = [i for i in invoices if i.format.value == "eml"]
    threaded = 0
    names = set()
    reply_to_differs = 0
    for inv in emls:
        text = (world_dir / inv.document_path).read_text(encoding="utf-8", errors="replace")
        if "In-Reply-To:" in text:
            threaded += 1
            assert "References:" in text
        if "Reply-To:" in text:
            reply_to_differs += 1
        for att in by_id[inv.id].payload["attachments"]:
            names.add(att["filename"])
    assert threaded >= 5
    assert reply_to_differs >= 3
    assert any(" " in n for n in names) and any("(" in n for n in names) and any(n.lower().endswith(".pdf") for n in names)
    assert len({n.lower() for n in names}) >= 30


def test_scans_are_rotated_noisy_and_one_is_annotated(world_dir: Path, invoices: list[Invoice]) -> None:
    from PIL import Image

    pngs = [i for i in invoices if i.format.value == "png"]
    assert len(pngs) >= 30
    annotated = [i for i in pngs if "annotated" in i.case_tags]
    assert len(annotated) == 1
    modes = Counter()
    for inv in pngs:
        with Image.open(world_dir / inv.document_path) as im:
            modes[im.mode] += 1
            w, h = im.size
            assert 1600 <= w <= 1700 and 2300 <= h <= 2400, (inv.id, im.size)  # A4 at 200 dpi
            hist = im.convert("L").histogram()
            # a clean render has almost only pure black and white; a scan spreads tones across the range
            mid = sum(hist[40:215])
            assert mid / (w * h) > 0.01, inv.id
    assert modes["RGB"] >= 1  # the annotated/stamped scan keeps colour


def test_receipts_faults_m5_m6(pos: list[PurchaseOrder], receipts: list[GoodsReceipt], answer_key: AnswerKey) -> None:
    po_lines = {(p.id, ln.line_no): ln for p in pos for ln in p.lines}
    assert all((r.po_id, r.line_no) in po_lines for r in receipts)
    assert all(r.dock_clerk for r in receipts)
    assert len(receipts) >= 300
    lines_with_receipts = {(r.po_id, r.line_no) for r in receipts}
    share = len(lines_with_receipts) / len(po_lines)
    assert 0.78 <= share <= 0.92, share
    m6 = [e for e in answer_key.ambient if e.case == "M6"]
    assert len(m6) == 1
    gr_id = next(r for r in m6[0].refs if r.startswith("GR-"))
    r6 = next(r for r in receipts if r.id == gr_id)
    assert r6.uom == "pc" and po_lines[(r6.po_id, r6.line_no)].uom == "box50"
    assert len([e for e in answer_key.ambient if e.case == "M5"]) == 3
    gr = next(r for r in receipts if r.id == "GR-4471")
    assert gr.qty == 800


def test_headline_trap_ids_exist(invoices: list[Invoice], pos: list[PurchaseOrder]) -> None:
    by_number = {i.invoice_number: i for i in invoices}
    for number in ("KT-2291", "FT-118", "GF-77", "GF-77A", "CN-12", "LB-301", "HM-09"):
        assert number in by_number, number
    kt = by_number["KT-2291"]
    assert kt.lines[0].qty == 1000 and kt.lines[0].unit_price_minor == 1390 and kt.currency == "EUR"
    assert "GR-4471" in kt.lines[0].gr_ids
    ft = by_number["FT-118"]
    po_line = next(ln for p in pos if p.id == ft.lines[0].po_id for ln in p.lines if ln.line_no == ft.lines[0].po_line_no)
    assert ft.lines[0].unit_price_minor == 1420 and po_line.unit_price_minor == 1390
    gf, gfa = by_number["GF-77"], by_number["GF-77A"]
    assert gf.gross_minor == gfa.gross_minor and gf.supplier_id == gfa.supplier_id
    assert abs((gfa.invoice_date - gf.invoice_date).days) == 2
    cn = by_number["CN-12"]
    assert cn.kind == "credit_note" and cn.credit_note_for == "LB-301" and cn.gross_minor < 0
    assert by_number["LB-301"].due_date < date(2026, 10, 12)
    hm = by_number["HM-09"]
    assert hm.lines[0].gr_ids == []
    assert kt.format.value in {"pdf", "png", "eml"}


def test_every_known_at_is_not_before_event_time_and_file_is_ordered(world_events: list[Event]) -> None:
    assert world_events, "empty world"
    assert all(e.known_at >= e.event_time for e in world_events)
    known = [e.known_at for e in world_events]
    assert known == sorted(known)
    ids = [e.id for e in world_events]
    assert ids == [f"ev_{i:06d}" for i in range(1, len(ids) + 1)]
    assert all(e.seq is None for e in world_events)
    assert all(e.known_at.time() != datetime.min.time() for e in world_events), "no event exactly at midnight"
    types = Counter(e.type for e in world_events)
    assert types["po_created"] == 160 and types["invoice_received"] == 300 and types["bank_statement"] == 138
    assert types["goods_received"] >= 300 and types["bank_statement_line"] >= 50
    assert types["vendor_message"] >= 3 and types["colleague_reply"] >= 1


def test_answer_key_structure_and_references(answer_key: AnswerKey, world_events: list[Event]) -> None:
    ids = {e.id for e in world_events}
    assert [e.id for e in answer_key.headline] == [f"T{i}" for i in range(1, 11)]
    assert len(answer_key.ambient) >= 45
    cases = Counter(e.case for e in answer_key.ambient)
    assert set(cases) == {f"A{i}" for i in range(1, 16)} | {f"M{i}" for i in range(1, 7)}
    assert cases["M4"] >= 1 and cases["M5"] == 3
    for entry in answer_key.headline + answer_key.ambient:
        assert entry.injected_event_ids, entry.id
        missing = [i for i in entry.injected_event_ids if i not in ids]
        assert not missing, (entry.id, missing)
        assert entry.checks, entry.id
    assert len({e.id for e in answer_key.ambient}) == len(answer_key.ambient)
    t1 = answer_key.headline[0]
    assert any(c.kind == "journal_amount" and c.credit_minor == 1112000 for c in t1.checks)
    t8 = answer_key.headline[7]
    assert any(c.kind == "chip_response" for c in t8.checks)
    assert any(c.kind == "packet_exists" for c in t8.checks)


def test_t8_lookalike_domain_and_compromise_window(suppliers: list[Supplier], people: list[Person],
                                                   world_events: list[Event], answer_key: AnswerKey) -> None:  # fmt: skip
    kovak = next(s for s in suppliers if s.legal_name.startswith("Kovak Trim"))
    assert kovak.domain == "kovaktrim.cz" and kovak.email.endswith("@kovaktrim.cz")
    attack = [e for e in world_events if e.type == "vendor_message"
              and e.payload["from_address"].split("@")[1] == "kovak-trim.cz"]  # fmt: skip
    assert attack, "no lookalike-domain vendor message"
    first = attack[0]
    assert first.payload["channel"] == "email" and first.payload["vendor_id"] is None
    assert "IBAN" in first.payload["body"] or "iban" in first.payload["body"].lower()
    assert first.payload["bank_details"]["iban"] != kovak.iban
    petra = next(p for p in people if p.id == "petra.vlcek")
    assert petra.mailbox_compromised_from == date(2026, 10, 14)
    assert petra.script.email_reply_when_compromised == "Confirmed, they told me last week."
    assert petra.script.phone_reply
    assert first.known_at.date() >= petra.mailbox_compromised_from
    replies = [e for e in world_events if e.type == "colleague_reply" and e.payload["in_reply_to"] == first.payload["message_id"]]
    assert replies and replies[0].payload["person_id"] == "petra.vlcek"
    assert "confirmed" in replies[0].payload["body"].lower()
    assert replies[0].known_at >= first.known_at
    t8 = next(e for e in answer_key.headline if e.id == "T8")
    assert first.id in t8.injected_event_ids and replies[0].id in t8.injected_event_ids
    big = [e for e in world_events if e.type == "invoice_received" and e.id in t8.injected_event_ids]
    assert big, "T8 must reference the EUR 2.1M invoice"
    jonas = next(p for p in people if p.id == "jonas.weber")
    assert jonas.script.delayed_replies.get("HM-09") == 30
    assert {p.id for p in people} >= {"petra.vlcek", "jonas.weber", "marta.nowak", "tomas.horak"}


def test_m1_duplicate_vendor(suppliers: list[Supplier], answer_key: AnswerKey) -> None:
    by_id = {s.id: s for s in suppliers}
    a, b = by_id["V-0007"], by_id["V-0033"]
    assert a.legal_name == "Liberec Foam a.s." and b.legal_name == "LIBEREC FOAM AS"
    assert a.onboarded_at.year == 2019 and b.onboarded_at.year == 2023
    assert a.vat_id == b.vat_id
    assert a.iban != b.iban
    m1 = [e for e in answer_key.ambient if e.case == "M1"]
    assert m1 and {"V-0007", "V-0033"} <= set(m1[0].refs)


def test_m2_departed_contact_and_m3_price_gap(suppliers: list[Supplier], people: list[Person], answer_key: AnswerKey) -> None:
    departed = [p for p in people if not p.active]
    assert departed and departed[0].left_at is not None
    assert any(s.purchasing_contact == departed[0].id for s in suppliers)
    gap_entries = [e for e in answer_key.ambient if e.case == "M3"]
    assert gap_entries
    sup = next(s for s in suppliers if s.id in gap_entries[0].refs)
    lists = sorted(sup.price_lists, key=lambda p: p.effective_from)
    assert len(lists) >= 2
    gaps = [(lists[i].effective_to, lists[i + 1].effective_from) for i in range(len(lists) - 1)
            if lists[i].effective_to is not None and (lists[i + 1].effective_from - lists[i].effective_to).days > 1]  # fmt: skip
    assert gaps


def test_statements_round_trip_and_bank_facts(world_dir: Path, world_events: list[Event]) -> None:
    stmts = [e for e in world_events if e.type == "bank_statement"]
    assert len(stmts) == 138
    per_account = Counter(e.payload["account_iban"] for e in stmts)
    assert len(per_account) == 3 and set(per_account.values()) == {46}
    currencies = {e.payload["currency"] for e in stmts}
    assert currencies == {"EUR", "CZK", "PLN"}
    opening = {}
    fee_days, fx_days, batch = set(), set(), []
    for e in stmts:
        text = e.payload["mt940"]
        parsed = parse_mt940(text)
        assert write_mt940(parsed) == text
        assert (world_dir / e.payload["path"]).read_text(encoding="utf-8") == text
        assert parsed.statement_date == date.fromisoformat(e.payload["statement_date"])
        assert e.known_at > e.event_time
        if parsed.statement_date == START:
            opening[parsed.currency] = parsed.opening_balance_minor
        for ln in parsed.lines:
            assert len(ln.remittance) <= 140
            if ln.txn_type == "NCHG":
                fee_days.add(parsed.statement_date)
            if ln.txn_type == "NFEX":
                fx_days.add((parsed.currency, parsed.statement_date))
            if ln.customer_ref.startswith("PAY-BATCH"):
                batch.append(ln)
    assert opening == {"EUR": 196_200_000, "CZK": 840_000_000, "PLN": 210_000_000}
    assert date(2026, 10, 1) in fee_days and date(2026, 11, 1) in fee_days
    assert len({c for c, _ in fx_days}) >= 2
    assert len(batch) == 1 and batch[0].dc == "D"
    dates = sorted({date.fromisoformat(e.payload["statement_date"]) for e in stmts})
    assert dates[0] == START and dates[-1] == END and len(dates) == 46
    lines = [e for e in world_events if e.type == "bank_statement_line"]
    assert sum(len(parse_mt940(e.payload["mt940"]).lines) for e in stmts) == len(lines)


def test_fx_rates_file(world_dir: Path) -> None:
    from decimal import Decimal

    fx = load_json(world_dir / "fx_rates.json")
    assert fx["base"] == "EUR"
    assert len(fx["rates"]) == 46
    assert sorted(fx["rates"]) == [str(START + (date(2026, 10, 1) - START)) if False else d for d in sorted(fx["rates"])]
    assert min(fx["rates"]) == "2026-10-01" and max(fx["rates"]) == "2026-11-15"
    for d, day in fx["rates"].items():
        assert {"CZK", "PLN", "USD", "JPY"} <= set(day["rates"]), d
        for v in day["rates"].values():
            assert isinstance(v, str) and Decimal(v).is_finite() and Decimal(v) > 0
    assert fx["source"].startswith("ECB")
