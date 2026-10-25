"""Field-level extraction accuracy on a deterministic 60-document sample of the world (spec §17
Part 3: >= 97 % on invoice number, date, currency, net, VAT, gross and line qty/unit price).
Needs Anthropic credentials; the extraction cache makes reruns free."""

from __future__ import annotations

import os
import random
from collections import defaultdict
from pathlib import Path

import pytest

from agents.documents import load_document
from agents.llm import LLMClient
from agents.resolve import VendorIndex
from agents.validate import validate_parsed
from core.vendors import VendorMaster
from sim.models import Event, Invoice, Supplier
from tests.conftest import load_json

FIELDS = ("invoice_number", "invoice_date", "currency", "net_minor", "vat_minor", "gross_minor")


def sample_documents(invoices: list[Invoice], n: int = 60) -> list[Invoice]:
    rng = random.Random(2026)
    rendered = [i for i in invoices if i.format.value != "structured"]
    by_template: dict[str, list[Invoice]] = defaultdict(list)
    for inv in rendered:
        by_template[f"{inv.template_id}:{inv.format.value}"].append(inv)
    picked: list[Invoice] = []
    keys = sorted(by_template)
    while len(picked) < n:
        for key in keys:
            if by_template[key] and len(picked) < n:
                picked.append(by_template[key].pop(rng.randrange(len(by_template[key]))))
    return sorted(picked, key=lambda i: i.id)


def test_extraction_accuracy_on_60_documents(
    world_dir: Path, tmp_path: Path, world_events: list[Event]
) -> None:
    if not LLMClient.credentials_available():
        pytest.fail(
            "Anthropic credentials are required for the extraction accuracy run: export ANTHROPIC_API_KEY (or `ant auth login`)"
        )
    invoices = [Invoice.model_validate(i) for i in load_json(world_dir / "invoices_truth.json")]
    suppliers = [Supplier.model_validate(s) for s in load_json(world_dir / "suppliers.json")]
    vendors = VendorMaster(tmp_path / "v.db")
    from datetime import UTC, datetime

    vendors.seed_from_suppliers(suppliers, now=datetime(2026, 10, 1, tzinfo=UTC))
    index = VendorIndex(suppliers, vendors)
    llm = LLMClient(
        Path(os.environ.get("PAYGUARD_TEST_LLM_CACHE", world_dir.parent / "extraction_cache.db"))
    )
    events = {e.payload["invoice_id"]: e for e in world_events if e.type == "invoice_received"}
    docs = sample_documents(invoices)
    assert len(docs) == 60 and len({d.template_id for d in docs}) == 8
    assert (
        sum(1 for d in docs if d.format.value == "png") >= 8
        and sum(1 for d in docs if d.format.value == "eml") >= 10
    )
    correct: dict[str, int] = defaultdict(int)
    total: dict[str, int] = defaultdict(int)
    for inv in docs:
        ev = events[inv.id]
        hints = index.hints_for(ev.payload.get("supplier_hint"), ev.payload.get("sender"))
        doc = load_document(world_dir, ev.payload, hints)
        parsed = llm.extract_invoice(doc)
        validated = None
        if not hasattr(parsed, "reason"):
            validated, errors = validate_parsed(parsed, hints)
            if validated is None:
                retry = llm.extract_invoice(doc, feedback=errors)
                if not hasattr(retry, "reason"):
                    validated, _ = validate_parsed(retry, hints)
        key = f"{inv.template_id}:{inv.format.value}"
        got = {
            "invoice_number": validated.invoice_number.strip() if validated else None,
            "invoice_date": validated.invoice_date if validated else None,
            "currency": validated.currency if validated else None,
            "net_minor": validated.net_minor if validated else None,
            "vat_minor": validated.vat_minor if validated else None,
            "gross_minor": validated.gross_minor if validated else None,
        }
        want = {
            "invoice_number": inv.invoice_number,
            "invoice_date": inv.invoice_date,
            "currency": inv.currency,
            "net_minor": inv.net_minor,
            "vat_minor": inv.vat_minor,
            "gross_minor": inv.gross_minor,
        }
        for f in FIELDS:
            total[key] += 1
            correct[key] += int(got[f] == want[f])
        for n, line in enumerate(inv.lines):
            v = validated.lines[n] if validated and n < len(validated.lines) else None
            total[key] += 2
            correct[key] += int(v is not None and v.qty == line.qty) + int(
                v is not None and v.unit_price_minor == line.unit_price_minor
            )
    print("\nextraction accuracy per template (fields correct / fields total):")
    for key in sorted(total):
        print(f"  {key:<32} {correct[key]:>4}/{total[key]:<4} {correct[key] / total[key]:.3f}")
    overall = sum(correct.values()) / sum(total.values())
    usage = llm.usage_report()
    print(
        f"  overall {sum(correct.values())}/{sum(total.values())} = {overall:.4f}; API calls {usage.calls}, cost USD {usage.cost_usd}"
    )
    vendors.close()
    assert overall >= 0.97, f"extraction accuracy {overall:.4f} < 0.97"
