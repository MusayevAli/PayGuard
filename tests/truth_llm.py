"""Test-only extractor that answers from ``invoices_truth.json``.

The ground truth may be read only by ``core/score.py`` and tests (spec §12); this class lives
under ``tests/`` so the full world can be run through the invoice agent without API credentials.
It exercises everything downstream of extraction (R0 cannot fire from it, by construction)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from agents.llm import UsageReport, template_explanation, template_message
from agents.parsed import ExtractionFailure, ParsedBankDetails, ParsedInvoice, ParsedLine
from tests.agentkit import RETENTION_NOTE, money_text


class TruthLLM:
    def __init__(self, world_dir: Path) -> None:
        rows = json.loads((Path(world_dir) / "invoices_truth.json").read_text(encoding="utf-8"))
        self.truth: dict[str, dict[str, Any]] = {r["id"]: r for r in rows}
        self.calls: list[str] = []

    def extract_invoice(
        self, doc: Any, feedback: list[str] | None = None
    ) -> ParsedInvoice | ExtractionFailure:
        self.calls.append(doc.invoice_id)
        t = self.truth.get(doc.invoice_id)
        if t is None:
            return ExtractionFailure(
                reason=f"{doc.invoice_id} is not in the fixture", errors=[], attempts=1
            )
        cur = t["currency"]
        lines = [
            ParsedLine(
                description=ln["description"],
                sku=ln["sku"],
                qty=str(ln["qty"]),
                uom=ln["uom"],
                unit_price=money_text(ln["unit_price_minor"], cur),
                line_total=money_text(ln["net_minor"], cur),
            )
            for ln in t["lines"]
        ]
        notes: list[str] = []
        if t.get("retention_minor"):
            notes.append(RETENTION_NOTE)
        if t.get("prepayment_minor"):
            notes.append(f"Less prepayment received: {money_text(t['prepayment_minor'], cur)}")
        bd = t.get("bank_details")
        bank = ParsedBankDetails(**{k: v for k, v in bd.items() if k != "holder"}) if bd else None
        net = t["net_minor"]
        rate_bp = round(t["vat_minor"] * 10_000 / net) if net else 0
        return ParsedInvoice(
            supplier_name=t["supplier_name_as_written"],
            supplier_vat_id=t.get("vat_id_as_written"),
            invoice_number=t["invoice_number"],
            invoice_date=t["invoice_date"],
            due_date=t.get("due_date"),
            service_date=t.get("service_date"),
            po_references=list(t.get("po_refs_as_written") or []),
            currency=cur,
            lines=lines,
            net=money_text(net, cur),
            vat_rate=f"{rate_bp / 100:g}%" if rate_bp else None,
            vat_amount=money_text(t["vat_minor"], cur),
            gross=money_text(t["gross_minor"], cur),
            bank_details=bank,
            document_language=t["language"],
            is_credit_note=t["kind"] == "credit_note",
            credit_note_for=t.get("credit_note_for"),
            payment_terms=t.get("terms"),
            prepayment_amount=money_text(t["prepayment_minor"], cur)
            if t.get("prepayment_minor")
            else None,
            notes=" ".join(notes) or None,
            source="llm",
        )

    def extract_many(self, docs: list[Any]) -> None:
        return None

    def explain(self, decision: dict[str, Any]) -> str:
        return template_explanation(decision)

    def draft_message(self, kind: str, facts: dict[str, Any]) -> str:
        return template_message(kind, facts)

    def usage_report(self) -> UsageReport:
        return UsageReport(
            calls=0,
            input_tokens=0,
            output_tokens=0,
            cache_write_tokens=0,
            cache_read_tokens=0,
            cost_usd="0.0000",
            by_purpose={},
        )

    def close(self) -> None:
        return None
