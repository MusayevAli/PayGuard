"""LLM boundary (spec §7.2) — the only module that imports ``anthropic``.

Exactly three public functions: ``extract_invoice``, ``explain``, ``draft_message``. Extraction
uses a strict tool schema, a cached system prompt, the SDK's two retries with backoff on
429/5xx, an on-disk cache keyed by document sha256 (replays never re-call the API), and a
per-call log with model, tokens, latency and cost.
"""

from __future__ import annotations

import asyncio
import copy
import hashlib
import json
import os
import re
import sqlite3
import threading
import time
from collections.abc import Callable, Sequence
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

import anthropic
import structlog
from pydantic import BaseModel, ConfigDict, ValidationError

from core.events import canonical_json
from core.money import Money, UnknownCurrency

from .documents import Document
from .parsed import ExtractionFailure, ParsedInvoice

log = structlog.get_logger("llm")

DEFAULT_MODEL = "claude-opus-5"
PRICING_PER_MTOK: dict[
    str, tuple[Decimal, Decimal, Decimal]
] = {  # (input, output, cache-read factor)
    "claude-opus-5": (Decimal("5"), Decimal("25"), Decimal("0.1")),
    "claude-opus-4-8": (Decimal("5"), Decimal("25"), Decimal("0.1")),
    "claude-opus-4-7": (Decimal("5"), Decimal("25"), Decimal("0.1")),
    "claude-opus-4-6": (Decimal("5"), Decimal("25"), Decimal("0.1")),
    "claude-sonnet-5": (Decimal("2"), Decimal("10"), Decimal("0.1")),
    "claude-sonnet-4-6": (Decimal("3"), Decimal("15"), Decimal("0.1")),
    "claude-haiku-4-5": (Decimal("1"), Decimal("5"), Decimal("0.1")),
    "claude-fable-5-1": (Decimal("10"), Decimal("50"), Decimal("0.025")),
}
CACHE_WRITE_FACTOR = Decimal("1.25")
TEMPERATURE_MODELS = ("claude-haiku-4-5", "claude-sonnet-4-5", "claude-opus-4-5", "claude-3")

EXTRACTION_SYSTEM = """You are the extraction step of an accounts-payable desk. You read one supplier invoice (images of pages, a scanned image, an e-mail with its attachment, or a JSON export) and record exactly what is printed by calling the record_invoice tool once.

Rules:
- Copy values as printed: keep the document's number format (decimal comma or point, thousands separators) and date format. Do not convert, round or recompute anything.
- Amounts: net (subtotal before VAT), vat_amount (total VAT), gross (total payable). For a credit note or credit memo, set is_credit_note=true, write the amounts as negative numbers and put the referenced original invoice number in credit_note_for.
- Lines: one entry per billed line with description, article/SKU if printed, quantity, unit, unit price and line total. Keep the order of the document.
- po_references: every purchase-order or order reference printed (e.g. "PO-10234", "Obj. č. 10234", "Zam. nr 10234", "Bestell-Nr. 10234", "P.O. #10234"), as printed. A quotation or offer number is not a purchase order; leave it out.
- supplier_vat_id: the supplier's VAT/tax id (DIČ, NIP, USt-IdNr., EIN, 登録番号) as printed. supplier_name as printed on the document.
- bank_details: IBAN/BIC/account/routing if printed anywhere on the document, else null.
- payment_terms: the terms sentence if printed (e.g. "2/10 net 30", "Skonto"). prepayment_amount: an amount deducted as prepayment/Anzahlung/záloha if printed, else null.
- document_language: cs, pl, de, en or ja.
- confidence: 0..1 per area. If the document is unreadable, still call the tool with your best reading and low confidence.
Never invent values that are not on the document."""

EXTRACTION_TOOL: dict[str, Any] = {
    "name": "record_invoice",
    "description": "Record the fields of the invoice exactly as printed.",
    "strict": True,
    "input_schema": {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "supplier_name": {"type": "string"},
            "supplier_vat_id": {"type": ["string", "null"]},
            "invoice_number": {"type": "string"},
            "invoice_date": {"type": "string"},
            "due_date": {"type": ["string", "null"]},
            "service_date": {"type": ["string", "null"]},
            "po_references": {"type": "array", "items": {"type": "string"}},
            "currency": {"type": "string"},
            "lines": {
                "type": "array",
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "properties": {
                        "description": {"type": "string"},
                        "sku": {"type": ["string", "null"]},
                        "qty": {"type": "string"},
                        "uom": {"type": ["string", "null"]},
                        "unit_price": {"type": "string"},
                        "line_total": {"type": "string"},
                    },
                    "required": ["description", "sku", "qty", "uom", "unit_price", "line_total"],
                },
            },
            "net": {"type": "string"},
            "vat_rate": {"type": ["string", "null"]},
            "vat_amount": {"type": "string"},
            "gross": {"type": "string"},
            "bank_details": {
                "type": ["object", "null"],
                "additionalProperties": False,
                "properties": {
                    "iban": {"type": ["string", "null"]},
                    "bic": {"type": ["string", "null"]},
                    "account_number": {"type": ["string", "null"]},
                    "routing_number": {"type": ["string", "null"]},
                    "bank_name": {"type": ["string", "null"]},
                    "bank_code": {"type": ["string", "null"]},
                    "branch_code": {"type": ["string", "null"]},
                },
                "required": [
                    "iban",
                    "bic",
                    "account_number",
                    "routing_number",
                    "bank_name",
                    "bank_code",
                    "branch_code",
                ],
            },
            "document_language": {"type": "string", "enum": ["cs", "pl", "de", "en", "ja"]},
            "is_credit_note": {"type": "boolean"},
            "credit_note_for": {"type": ["string", "null"]},
            "payment_terms": {"type": ["string", "null"]},
            "prepayment_amount": {"type": ["string", "null"]},
            "notes": {
                "type": ["string", "null"],
                "description": "Other printed remarks verbatim: retention/holdback clauses, prepayment references, credit-memo references",
            },
            "confidence": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "supplier": {"type": "number"},
                    "invoice_number": {"type": "number"},
                    "dates": {"type": "number"},
                    "totals": {"type": "number"},
                    "lines": {"type": "number"},
                },
                "required": ["supplier", "invoice_number", "dates", "totals", "lines"],
            },
        },
        "required": [
            "supplier_name",
            "supplier_vat_id",
            "invoice_number",
            "invoice_date",
            "due_date",
            "service_date",
            "po_references",
            "currency",
            "lines",
            "net",
            "vat_rate",
            "vat_amount",
            "gross",
            "bank_details",
            "document_language",
            "is_credit_note",
            "credit_note_for",
            "payment_terms",
            "prepayment_amount",
            "notes",
            "confidence",
        ],
    },
}

EXPLAIN_SYSTEM = """You write one short, plain-language sentence or two (at most 60 words) explaining a decision an accounts-payable agent already made. Use only canonical_decision.refs and the supplied facts; never add new identifiers, amounts or reasons. Canonical evidence remains authoritative.
Money integers in canonical_decision are MINOR units, not whole currency units. The separate money records supply minor, currency, exact major and display values already converted by trusted code. For every monetary amount, copy a supplied display value (for example, 11,500.00 PLN), preserving its currency and sign. Never present raw minor integers as currency amounts, calculate a conversion yourself, or infer money from unlabeled positional rows. If major/display is null, omit that monetary amount. Do not spell amounts out in words. Quantities and basis points are not currency amounts.
Write for a finance colleague, present tense, no preamble."""

MESSAGE_SYSTEM = """You rewrite a short internal business message in a natural, courteous tone. Keep the identifiers, quantities, dates and monetary amounts in the supplied draft, add nothing new, and stay under 90 words. Audit fields ending in _minor are integer MINOR units, not whole currency units. Separate money records give the exact major and display values: use those values with their currency, never the raw minor integers. Do not calculate conversions or spell monetary amounts out in words. Return only the message text."""

ID_PATTERN = re.compile(
    r"\b(?:[A-Z]{1,4}-\d{2,6}[A-Z]?|ev_[0-9a-f]{6,16}|PK-[0-9a-f]{12}|JE-\d{6}|AP-\d{6}|VBA-\d{6})\b"
)
AMOUNT_PATTERN = re.compile(r"\d[\d .,]*\d")
_MONEY_CODES = r"EUR|CZK|PLN|USD|JPY|GBP|CHF|CAD|AUD|HUF|SEK|NOK|DKK|CNY|RMB|€|\$|¥|£"
_MONEY_NUMBER = r"[+\-−]?\d+(?:[ ,.\u00a0\u202f]\d+)*"
_MONEY_MENTION = re.compile(
    rf"(?<!\w)(?:(?P<before>{_MONEY_CODES})\s*(?P<after_amount>{_MONEY_NUMBER})"
    rf"|(?P<before_amount>{_MONEY_NUMBER})\s*(?P<after>{_MONEY_CODES}))(?!\w)",
    re.I,
)

SCHEMA = """
CREATE TABLE IF NOT EXISTS extraction_cache (key TEXT PRIMARY KEY, sha256 TEXT, kind TEXT, model TEXT, result TEXT NOT NULL, created_at TEXT);
CREATE TABLE IF NOT EXISTS llm_calls (id INTEGER PRIMARY KEY AUTOINCREMENT, created_at TEXT, purpose TEXT, model TEXT, sha256 TEXT NULL,
  input_tokens INTEGER, output_tokens INTEGER, cache_write_tokens INTEGER, cache_read_tokens INTEGER, latency_ms INTEGER, cost_usd TEXT, request_id TEXT NULL, status TEXT);
"""


class LLMUnavailable(RuntimeError):
    """No credentials or the API cannot be reached; the caller decides how to hold the work."""


class UsageReport(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    calls: int
    input_tokens: int
    output_tokens: int
    cache_write_tokens: int
    cache_read_tokens: int
    cost_usd: str
    by_purpose: dict[str, dict[str, int]]


def _supports_temperature(model: str) -> bool:
    return model.startswith(TEMPERATURE_MODELS)


def call_cost(
    model: str, input_tokens: int, output_tokens: int, cache_write: int, cache_read: int
) -> Decimal | None:
    price = PRICING_PER_MTOK.get(model)
    if price is None:
        return None
    inp, out, read_factor = price
    million = Decimal(1_000_000)
    total = (
        Decimal(input_tokens) * inp
        + Decimal(output_tokens) * out
        + Decimal(cache_write) * inp * CACHE_WRITE_FACTOR
        + Decimal(cache_read) * inp * read_factor
    ) / million
    return total.quantize(Decimal("0.000001"))


def explanation_facts(decision: dict[str, Any]) -> dict[str, Any]:
    """Separate display money from canonical evidence without changing either's units."""
    canonical = copy.deepcopy(decision)
    money: list[dict[str, Any]] = []
    facts = canonical.get("decision") or {}
    inputs = canonical.get("inputs") or {}
    default_currency = facts.get("currency") or inputs.get("currency")
    if default_currency is None:
        default_currency = (inputs.get("hints") or {}).get("currency")

    def record(path: str, minor: int, currency: Any) -> None:
        currency = currency.upper() if isinstance(currency, str) else None
        major = display = None
        if currency is not None:
            try:
                value = Money(minor, currency)
                major = str(value.as_decimal())
                display = f"{value.as_decimal():,.{value.minor_digits}f} {currency}"
            except UnknownCurrency:
                pass
        money.append(
            {"path": path, "minor": minor, "currency": currency, "major": major, "display": display}
        )

    def visit(node: Any, path: str, inherited_currency: Any) -> None:
        if isinstance(node, dict):
            currency = node.get("currency", inherited_currency)
            for key, value in node.items():
                child_path = f"{path}.{key}" if path else key
                if key.endswith("_minor") and type(value) is int:
                    if "_eur_" in key or key.startswith("eur_"):
                        amount_currency = "EUR"
                    elif node.get(key.removesuffix("_minor") + "_currency"):
                        amount_currency = node[key.removesuffix("_minor") + "_currency"]
                    elif key == "settlement_minor":
                        amount_currency = node.get("settlement_currency")
                    elif key == "cash_minor":
                        amount_currency = node.get("cash_currency") or node.get(
                            "settlement_currency"
                        )
                    elif key in (
                        "paid_minor",
                        "payment_minor",
                        "shortfall_minor",
                        "available_minor",
                    ):
                        amount_currency = node.get("settlement_currency", currency)
                    else:
                        amount_currency = currency
                    record(child_path, value, amount_currency)
                else:
                    visit(value, child_path, currency)
        elif isinstance(node, (list, tuple)):
            if (
                len(node) == 2
                and isinstance(node[0], str)
                and re.fullmatch(r"[A-Z]{3}", node[0])
                and type(node[1]) is int
            ):
                record(path, node[1], node[0])
            else:
                for index, value in enumerate(node):
                    visit(value, f"{path}[{index}]", inherited_currency)

    visit(canonical, "", default_currency)
    return {"canonical_decision": canonical, "money": money}


def _money_mentions_match(text: str, money: Sequence[dict[str, Any]]) -> bool:
    allowed = {
        (item["currency"], Decimal(item["major"])) for item in money if item["major"] is not None
    }
    aliases = {"€": "EUR", "$": "USD", "¥": "JPY", "£": "GBP"}
    for match in _MONEY_MENTION.finditer(text):
        currency = (match.group("before") or match.group("after")).upper()
        currency = aliases.get(currency, currency)
        raw = match.group("after_amount") or match.group("before_amount")
        # Wording uses the supplied English display convention; ambiguous local
        # separators are rejected rather than guessed.
        normalized = re.sub(r"[,\s\u202f]", "", raw).replace("−", "-")
        try:
            amount = Decimal(normalized)
        except ArithmeticError:
            return False
        if (currency, amount) not in allowed:
            return False
    return True


def template_explanation(decision: dict[str, Any]) -> str:
    """Deterministic fallback prose built only from the structured decision."""
    action = str(decision.get("action_type", "action")).replace("_", " ")
    rule = decision.get("rule_id")
    refs = [str(r) for r in decision.get("refs", []) if not str(r).startswith("ev_")][:3]
    parts = [action.capitalize()]
    if rule:
        parts.append(f"under {rule}")
    if refs:
        parts.append("for " + ", ".join(refs))
    body = " ".join(parts) + "."
    facts = decision.get("decision") or {}
    money = {item["path"]: item for item in explanation_facts(decision)["money"]}
    details = []
    for key in (
        "qty_posted",
        "qty_billed",
        "qty_received",
        "post_minor",
        "short_pay_minor",
        "variance_bp",
        "reason",
    ):
        if key in facts and facts[key] not in (None, ""):
            amount = money.get(f"decision.{key}")
            if amount is not None:
                label = key.removesuffix("_minor").replace("_", " ")
                display = (
                    amount["display"] or f"{amount['minor']} minor units (currency unavailable)"
                )
                details.append(f"{label} {display}")
            else:
                details.append(f"{key.replace('_', ' ')} {facts[key]}")
    if details:
        body += " " + "; ".join(details[:3]) + "."
    return body


def template_message(kind: str, facts: dict[str, Any]) -> str:
    f = {k: ("" if v is None else v) for k, v in facts.items()}
    if kind == "short_pay_notice":
        return (
            f"Dear {f.get('supplier_name', 'supplier')}, invoice {f.get('invoice_number')} bills {f.get('qty_billed')} units of {f.get('sku')} against "
            f"{f.get('po_id')}, but our receipt {f.get('gr_ids')} shows {f.get('qty_received')}. We have posted {f.get('qty_posted')} units "
            f"({f.get('amount_posted')} {f.get('currency')}) and hold {f.get('short_pay')} {f.get('currency')} until the balance of {f.get('balance_qty')} units is delivered. "
            f"Please send the balance or a credit note."
        )
    if kind == "warehouse_query":
        return (
            f"Hi {f.get('to_name', 'Jonas')}, invoice {f.get('invoice_number')} bills {f.get('qty_billed')} x {f.get('sku')} on {f.get('po_id')} line {f.get('line_no')}, "
            f"but I have no goods receipt for that line. {f.get('detail', '')} Could you check what was received and book it?"
        )
    if kind == "buyer_confirmation":
        return (
            f"Hi {f.get('to_name', 'Tomáš')}, {f.get('supplier_name')} sent invoice {f.get('invoice_number')} for {f.get('amount')} {f.get('currency')} "
            f"({f.get('description')}) without a purchase order. Did you order this, and may I post it to freight?"
        )
    if kind == "verification_request":
        return (
            f"Hi {f.get('to_name')}, we received new bank details for {f.get('supplier_name')} ({f.get('iban')}). "
            f"Please confirm with the supplier on the phone number we have on file."
        )
    if kind == "onboarding_request":
        return f"We received invoice {f.get('invoice_number')} from {f.get('supplier_name')}, who is not in the vendor master. Please start onboarding."
    return " ".join(f"{k}: {v}" for k, v in f.items())


def validate_explanation(text: str, refs: Sequence[str]) -> bool:
    if len(text.split()) > 60:
        return False
    allowed = " ".join(refs)
    return all(token in allowed for token in ID_PATTERN.findall(text))


def validate_message(text: str, facts: dict[str, Any]) -> bool:
    blob = " ".join(str(v) for v in facts.values())
    for ident in ID_PATTERN.findall(text):
        if ident not in blob:
            return False
    for amount in AMOUNT_PATTERN.findall(text):
        digits = re.sub(r"\D", "", amount)
        if len(digits) >= 3 and digits not in re.sub(r"\D", "", blob):
            return False
    return True


class LLMClient:
    def __init__(
        self,
        db_path: str | Path,
        *,
        model: str | None = None,
        text_model: str | None = None,
        client: Any | None = None,
        async_client: Any | None = None,
        concurrency: int = 4,
        now: Callable[[], datetime] | None = None,
        timeout: float = 120.0,
    ) -> None:
        self.model = model or os.environ.get("PAYGUARD_MODEL", DEFAULT_MODEL)
        self.text_model = text_model or os.environ.get("PAYGUARD_MODEL_TEXT", self.model)
        self._client = client
        self._async_client = async_client
        self._concurrency = concurrency
        self._timeout = timeout
        self._now: Callable[[], datetime] = now or (lambda: datetime.now(UTC))
        self._lock = threading.RLock()
        self._con = sqlite3.connect(
            str(db_path), timeout=30.0, check_same_thread=False, isolation_level=None
        )
        self._con.row_factory = sqlite3.Row
        self._con.execute("PRAGMA journal_mode=WAL")
        self._con.executescript(SCHEMA)

    def close(self) -> None:
        with self._lock:
            self._con.close()

    # ------------------------------------------------------------------ credentials

    @staticmethod
    def credentials_available() -> bool:
        if os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN"):
            return True
        profile_dir = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "anthropic"
        return profile_dir.is_dir() and any(profile_dir.iterdir())

    def _sync(self) -> Any:
        if self._client is None:
            if not self.credentials_available():
                raise LLMUnavailable(
                    "no Anthropic credentials: export ANTHROPIC_API_KEY or run `ant auth login`"
                )
            self._client = anthropic.Anthropic(max_retries=2, timeout=self._timeout)
        return self._client

    def _async(self) -> Any:
        if self._async_client is not None:
            return self._async_client
        if not self.credentials_available():
            raise LLMUnavailable(
                "no Anthropic credentials: export ANTHROPIC_API_KEY or run `ant auth login`"
            )
        # Each prefetch batch owns an asyncio.run loop. Its pooled sockets must
        # be created and closed within that loop, never reused by the next one.
        return anthropic.AsyncAnthropic(max_retries=2, timeout=self._timeout)

    # ------------------------------------------------------------------ cache + log

    def _cached(self, key: str) -> str | None:
        with self._lock:
            row = self._con.execute(
                "SELECT result FROM extraction_cache WHERE key = ?", (key,)
            ).fetchone()
        return None if row is None else str(row["result"])

    def _store(self, key: str, sha256: str | None, kind: str, model: str, result: str) -> None:
        with self._lock:
            self._con.execute(
                "INSERT OR REPLACE INTO extraction_cache VALUES (?,?,?,?,?,?)",
                (key, sha256, kind, model, result, self._now().isoformat()),
            )

    def _log_call(
        self,
        purpose: str,
        model: str,
        sha256: str | None,
        response: Any,
        latency_ms: int,
        status: str,
    ) -> None:
        usage = getattr(response, "usage", None)
        inp = int(getattr(usage, "input_tokens", 0) or 0)
        out = int(getattr(usage, "output_tokens", 0) or 0)
        cw = int(getattr(usage, "cache_creation_input_tokens", 0) or 0)
        cr = int(getattr(usage, "cache_read_input_tokens", 0) or 0)
        cost = call_cost(model, inp, out, cw, cr)
        request_id = getattr(response, "_request_id", None)
        with self._lock:
            self._con.execute(
                "INSERT INTO llm_calls(created_at, purpose, model, sha256, input_tokens, output_tokens, cache_write_tokens, cache_read_tokens, latency_ms, cost_usd, request_id, status)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    self._now().isoformat(),
                    purpose,
                    model,
                    sha256,
                    inp,
                    out,
                    cw,
                    cr,
                    latency_ms,
                    str(cost) if cost is not None else None,
                    request_id,
                    status,
                ),
            )
        log.info(
            "llm_call",
            purpose=purpose,
            model=model,
            input_tokens=inp,
            output_tokens=out,
            cache_write_tokens=cw,
            cache_read_tokens=cr,
            latency_ms=latency_ms,
            cost_usd=str(cost) if cost is not None else "unknown",
            request_id=request_id,
            status=status,
            event_id=None,
            principal="llm",
            rule_id=None,
        )

    def usage_report(self) -> UsageReport:
        with self._lock:
            rows = self._con.execute(
                "SELECT purpose, COUNT(*) AS n, SUM(input_tokens) AS i, SUM(output_tokens) AS o, SUM(cache_write_tokens) AS cw, SUM(cache_read_tokens) AS cr FROM llm_calls GROUP BY purpose"
            ).fetchall()
            costs = self._con.execute(
                "SELECT cost_usd FROM llm_calls WHERE cost_usd IS NOT NULL"
            ).fetchall()
        total = sum((Decimal(r["cost_usd"]) for r in costs), Decimal(0))
        by_purpose = {
            r["purpose"]: {
                "calls": int(r["n"]),
                "input_tokens": int(r["i"] or 0),
                "output_tokens": int(r["o"] or 0),
                "cache_write_tokens": int(r["cw"] or 0),
                "cache_read_tokens": int(r["cr"] or 0),
            }
            for r in rows
        }
        return UsageReport(
            calls=sum(v["calls"] for v in by_purpose.values()),
            input_tokens=sum(v["input_tokens"] for v in by_purpose.values()),
            output_tokens=sum(v["output_tokens"] for v in by_purpose.values()),
            cache_write_tokens=sum(v["cache_write_tokens"] for v in by_purpose.values()),
            cache_read_tokens=sum(v["cache_read_tokens"] for v in by_purpose.values()),
            cost_usd=str(total.quantize(Decimal("0.0001"))),
            by_purpose=by_purpose,
        )

    # ------------------------------------------------------------------ request shaping

    def _kwargs(self, model: str, purpose: str) -> dict[str, Any]:
        if _supports_temperature(model):
            return {"temperature": 0}
        return {"output_config": {"effort": "high" if purpose == "extract" else "low"}}

    @staticmethod
    def _user_content(doc: Document, feedback: list[str] | None) -> list[dict[str, Any]]:
        content: list[dict[str, Any]] = []
        for part in doc.parts:
            if part.kind == "image":
                content.append(
                    {
                        "type": "image",
                        "source": {
                            "type": "base64",
                            "media_type": part.media_type,
                            "data": part.data_b64,
                        },
                    }
                )
            else:
                content.append({"type": "text", "text": part.text})
        h = doc.hints
        hint_lines = [f"Document id: {doc.invoice_id}; delivery format: {doc.format}."]
        if h.supplier_id:
            hint_lines.append(
                f"The sender is known: supplier {h.supplier_id}, language {h.language}, number format like {h.number_locale}, dates like {h.date_format}, usual currency {h.currency}."
            )
        else:
            hint_lines.append(
                "The sender is not identified from the envelope; infer the locale from the document itself."
            )
        hint_lines.append("Call record_invoice exactly once with the values as printed.")
        if feedback:
            hint_lines.append(
                "A previous reading failed validation. Fix these specific problems, re-reading the document carefully:\n- "
                + "\n- ".join(feedback)
            )
        content.append({"type": "text", "text": "\n".join(hint_lines)})
        return content

    def _extract_request(self, doc: Document, feedback: list[str] | None) -> dict[str, Any]:
        return {
            "model": self.model,
            "max_tokens": 4096,
            "system": [
                {"type": "text", "text": EXTRACTION_SYSTEM, "cache_control": {"type": "ephemeral"}}
            ],
            "messages": [{"role": "user", "content": self._user_content(doc, feedback)}],
            "tools": [EXTRACTION_TOOL],
            "tool_choice": {"type": "auto", "disable_parallel_tool_use": True},
            **self._kwargs(self.model, "extract"),
        }

    @staticmethod
    def _parse_extraction(response: Any) -> ParsedInvoice | ExtractionFailure:
        if getattr(response, "stop_reason", None) == "refusal":
            return ExtractionFailure(reason="model refused the request", errors=[])
        for block in response.content:
            if (
                getattr(block, "type", None) == "tool_use"
                and getattr(block, "name", None) == "record_invoice"
            ):
                data = (
                    block.input
                    if isinstance(block.input, dict)
                    else json.loads(json.dumps(block.input))
                )
                try:
                    return ParsedInvoice.model_validate({**data, "source": "llm"})
                except ValidationError as exc:
                    return ExtractionFailure(
                        reason="tool input did not match the schema", errors=[str(exc)[:500]]
                    )
        return ExtractionFailure(reason="the model did not call record_invoice", errors=[])

    @staticmethod
    def _cache_key(doc: Document, feedback: list[str] | None) -> str:
        if not feedback:
            return doc.sha256
        return (
            doc.sha256
            + ":retry:"
            + hashlib.sha256(canonical_json(feedback).encode()).hexdigest()[:16]
        )

    # ------------------------------------------------------------------ public: extraction

    def extract_invoice(
        self, doc: Document, feedback: list[str] | None = None
    ) -> ParsedInvoice | ExtractionFailure:
        key = self._cache_key(doc, feedback)
        cached = self._cached(key)
        if cached is not None:
            data = json.loads(cached)
            return (
                ParsedInvoice.model_validate(data)
                if "invoice_number" in data
                else ExtractionFailure.model_validate(data)
            )
        client = self._sync()
        result: ParsedInvoice | ExtractionFailure = ExtractionFailure(
            reason="no attempt", errors=[]
        )
        for attempt in range(2):
            started = time.perf_counter()
            try:
                response = client.messages.create(**self._extract_request(doc, feedback))
            except anthropic.APIConnectionError as exc:
                raise LLMUnavailable(f"API unreachable: {exc}") from exc
            except anthropic.AuthenticationError as exc:
                raise LLMUnavailable(f"authentication failed: {exc}") from exc
            latency = int((time.perf_counter() - started) * 1000)
            result = self._parse_extraction(response)
            self._log_call(
                "extract",
                self.model,
                doc.sha256,
                response,
                latency,
                "ok" if isinstance(result, ParsedInvoice) else result.reason,
            )
            if isinstance(result, ParsedInvoice):
                break
            feedback = (feedback or []) + [result.reason]
            result = result.model_copy(update={"attempts": attempt + 1})
        self._store(key, doc.sha256, "extract", self.model, result.model_dump_json())
        return result

    def extract_many(self, docs: Sequence[Document]) -> None:
        """Warm the cache for many documents concurrently (bounded by the semaphore)."""
        pending = [d for d in docs if self._cached(d.sha256) is None]
        if not pending:
            return
        if not self.credentials_available():
            raise LLMUnavailable(
                "no Anthropic credentials: export ANTHROPIC_API_KEY or run `ant auth login`"
            )

        async def run() -> None:
            client = self._async()
            sem = asyncio.Semaphore(self._concurrency)

            async def one(doc: Document) -> None:
                async with sem:
                    started = time.perf_counter()
                    try:
                        response = await client.messages.create(**self._extract_request(doc, None))
                    except (
                        anthropic.APIConnectionError,
                        anthropic.AuthenticationError,
                        anthropic.APIStatusError,
                    ) as exc:
                        log.warning(
                            "llm_prefetch_failed",
                            sha256=doc.sha256,
                            error=repr(exc),
                            event_id=None,
                            principal="llm",
                            rule_id=None,
                        )
                        return
                    latency = int((time.perf_counter() - started) * 1000)
                    result = self._parse_extraction(response)
                    self._log_call(
                        "extract",
                        self.model,
                        doc.sha256,
                        response,
                        latency,
                        "ok" if isinstance(result, ParsedInvoice) else result.reason,
                    )
                    if isinstance(result, ParsedInvoice):
                        self._store(
                            doc.sha256, doc.sha256, "extract", self.model, result.model_dump_json()
                        )

            try:
                await asyncio.gather(*(one(d) for d in pending))
            finally:
                if self._async_client is None:
                    await client.close()

        worker = threading.Thread(target=lambda: asyncio.run(run()), name="llm-prefetch")
        worker.start()
        worker.join()

    # ------------------------------------------------------------------ public: prose

    def _text_call(self, purpose: str, system: str, user: str, max_tokens: int = 300) -> str | None:
        client = self._sync()
        started = time.perf_counter()
        try:
            response = client.messages.create(
                model=self.text_model,
                max_tokens=max_tokens,
                system=[{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}],
                messages=[{"role": "user", "content": user}],
                **self._kwargs(self.text_model, purpose),
            )
        except anthropic.APIConnectionError as exc:
            raise LLMUnavailable(f"API unreachable: {exc}") from exc
        except anthropic.AuthenticationError as exc:
            raise LLMUnavailable(f"authentication failed: {exc}") from exc
        latency = int((time.perf_counter() - started) * 1000)
        self._log_call(purpose, self.text_model, None, response, latency, "ok")
        if getattr(response, "stop_reason", None) == "refusal":
            return None
        return (
            " ".join(
                b.text.strip() for b in response.content if getattr(b, "type", None) == "text"
            ).strip()
            or None
        )

    def explain(self, decision: dict[str, Any]) -> str:
        """<= 60 words citing only ids in decision.refs; regenerate once, then template fallback."""
        refs = [str(r) for r in decision.get("refs", [])]
        facts = explanation_facts(decision)
        key = "explain:money-v1:" + hashlib.sha256(canonical_json(facts).encode()).hexdigest()
        cached = self._cached(key)
        if cached is not None and _money_mentions_match(cached, facts["money"]):
            return cached
        fallback = template_explanation(decision)
        if not self.credentials_available():
            return fallback
        prompt = "Decision and explicit monetary units (JSON):\n" + json.dumps(
            facts, ensure_ascii=False, indent=2
        )
        text: str | None = None
        try:
            for attempt in range(2):
                extra = (
                    ""
                    if attempt == 0
                    else "\nYour previous attempt used an unapproved identifier, an unsupported monetary amount, or too many words. Copy monetary display values exactly and use only these identifiers: "
                    + ", ".join(refs)
                )
                text = self._text_call("explain", EXPLAIN_SYSTEM, prompt + extra)
                if (
                    text
                    and validate_explanation(text, refs)
                    and _money_mentions_match(text, facts["money"])
                ):
                    break
                text = None
        except LLMUnavailable:
            text = None
        result = text or fallback
        self._store(key, None, "explain", self.text_model, result)
        return result

    def persona_reply(self, person: dict[str, Any], context: dict[str, Any]) -> str | None:
        """Word a colleague's reply in their voice. Only the wording is the model's: the facts
        (``context["facts"]``) and the template to paraphrase are given. Without credentials, or
        when the model drops a required fact, returns None so the caller keeps the template."""
        if not self.credentials_available():
            return None
        facts = context.get("facts") or {}
        template = str(context.get("template") or "")
        key = (
            "persona:"
            + hashlib.sha256(
                canonical_json(
                    {
                        "person": person.get("id"),
                        "channel": context.get("channel"),
                        "template": template,
                        "facts": facts,
                    }
                ).encode("utf-8")
            ).hexdigest()
        )
        cached = self._cached(key)
        if cached is not None:
            return str(cached) if cached else None
        system = (
            f"You are {person.get('name')}, {person.get('role')} at the company. Persona: {person.get('persona')}. "
            f"Style: {(person.get('script') or {}).get('reply_style') or 'plain'}. Rewrite the given reply in your own "
            "voice for the named channel. Keep every fact, identifier, amount and the verdict exactly; add nothing. "
            "Under 70 words. Return only the message."
        )
        user = json.dumps(
            {
                "channel": context.get("channel"),
                "question": context.get("message"),
                "reply_to_rephrase": template,
                "facts": facts,
            },
            ensure_ascii=False,
        )
        text = self._text_call("persona_reply", system, user, max_tokens=250)
        ok = (
            text is not None
            and str(facts.get("verdict", "")).lower()[:6] in text.lower().replace("’", "'")
            if text
            else False
        )
        if facts.get("verdict") == "confirmed" and text and "confirm" not in text.lower():
            ok = False
        if (
            facts.get("verdict") == "denied"
            and text
            and not any(w in text.lower() for w in ("no", "not", "never", "do not"))
        ):
            ok = False
        self._store(key, None, "persona_reply", self.text_model, text if ok else "")
        return text if ok and text else None

    def draft_message(self, kind: str, facts: dict[str, Any]) -> str:
        """Templated skeleton; the model only adjusts tone. Every amount and id must come from facts."""
        skeleton = template_message(kind, facts)
        wording_facts = explanation_facts({"decision": facts})
        key = (
            "message:money-v1:"
            + hashlib.sha256(canonical_json({"kind": kind, "facts": facts}).encode()).hexdigest()
        )
        cached = self._cached(key)
        if cached is not None and _money_mentions_match(cached, wording_facts["money"]):
            return cached
        if not self.credentials_available():
            return skeleton
        prompt = (
            f"Message kind: {kind}\nFacts with explicit monetary units (JSON): "
            f"{json.dumps(wording_facts, ensure_ascii=False)}\n\nDraft to polish:\n{skeleton}"
        )
        try:
            text = self._text_call("draft_message", MESSAGE_SYSTEM, prompt, max_tokens=400)
        except LLMUnavailable:
            text = None
        result = (
            text
            if text
            and validate_message(text, facts)
            and _money_mentions_match(text, wording_facts["money"])
            else skeleton
        )
        self._store(key, None, "draft_message", self.text_model, result)
        return result


_DEFAULT: LLMClient | None = None


def _default() -> LLMClient:
    global _DEFAULT
    if _DEFAULT is None:
        _DEFAULT = LLMClient(os.environ.get("PAYGUARD_DB", "payguard.db"))
    return _DEFAULT


def extract_invoice(
    doc: Document, feedback: list[str] | None = None
) -> ParsedInvoice | ExtractionFailure:
    return _default().extract_invoice(doc, feedback)


def explain(decision: dict[str, Any]) -> str:
    return _default().explain(decision)


def draft_message(kind: str, facts: dict[str, Any]) -> str:
    return _default().draft_message(kind, facts)
