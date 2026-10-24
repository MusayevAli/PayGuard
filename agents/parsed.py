"""What extraction produces (strings as printed) and what validation turns it into (typed)."""

from __future__ import annotations

from datetime import date
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from sim.models import DateFormat, Language, NumberLocale


class ParsedBankDetails(BaseModel):
    model_config = ConfigDict(extra="forbid")
    iban: str | None = None
    bic: str | None = None
    account_number: str | None = None
    routing_number: str | None = None
    bank_name: str | None = None
    bank_code: str | None = None
    branch_code: str | None = None


class ParsedLine(BaseModel):
    model_config = ConfigDict(extra="forbid")
    description: str
    sku: str | None = None
    qty: str
    uom: str | None = None
    unit_price: str
    line_total: str


class ParsedInvoice(BaseModel):
    """Extraction output: every amount and date is the string as printed on the document."""

    model_config = ConfigDict(extra="forbid")
    supplier_name: str
    supplier_vat_id: str | None = None
    invoice_number: str
    invoice_date: str
    due_date: str | None = None
    service_date: str | None = None
    po_references: list[str] = Field(default_factory=list)
    currency: str
    lines: list[ParsedLine]
    net: str
    vat_rate: str | None = None
    vat_amount: str
    gross: str
    bank_details: ParsedBankDetails | None = None
    document_language: str = "en"
    is_credit_note: bool = False
    credit_note_for: str | None = None
    payment_terms: str | None = None
    prepayment_amount: str | None = None
    notes: str | None = None
    confidence: dict[str, float] = Field(default_factory=dict)
    source: Literal["structured_mapper", "llm", "judge"] = "llm"


class ExtractionFailure(BaseModel):
    model_config = ConfigDict(extra="forbid")
    reason: str
    errors: list[str] = Field(default_factory=list)
    attempts: int = 1


class LocaleHints(BaseModel):
    model_config = ConfigDict(extra="forbid")
    number_locale: NumberLocale | None = None
    date_format: DateFormat | None = None
    language: Language | None = None
    supplier_id: str | None = None
    currency: str | None = None


class ValidatedLine(BaseModel):
    model_config = ConfigDict(extra="forbid")
    line_no: int
    description: str
    sku: str | None
    qty: int
    uom: str | None
    unit_price_minor: int
    line_total_minor: int


class ValidatedInvoice(BaseModel):
    model_config = ConfigDict(extra="forbid")
    supplier_name: str
    supplier_vat_id: str | None
    invoice_number: str
    invoice_date: date
    due_date: date | None
    service_date: date | None
    po_references: list[str]
    currency: str
    lines: list[ValidatedLine]
    net_minor: int
    vat_rate_bp: int | None
    vat_minor: int
    gross_minor: int
    bank_details: ParsedBankDetails | None
    document_language: str
    is_credit_note: bool
    credit_note_for: str | None
    payment_terms: str | None
    prepayment_minor: int | None
    notes: str | None
    retention_bp: int | None
    number_locale: NumberLocale
    date_format: DateFormat | None
    source: str
    confidence: dict[str, float]

    def to_json(self) -> dict[str, Any]:
        return self.model_dump(mode="json")
