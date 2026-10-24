"""Pydantic v2 models for the simulated world, every event payload of spec §5.1, the event
envelope, and the two-tier answer key of §2.9.

Rules: amounts are integer minor units; datetimes are timezone-aware and normalised to UTC;
``known_at >= event_time`` is enforced on the envelope; payloads are validated against the
model registered for their event type.
"""

from datetime import UTC, date, datetime
from datetime import date as _Date
from enum import StrEnum
from typing import Annotated, Any, Literal, get_args

from pydantic import (
    AwareDatetime,
    BaseModel,
    ConfigDict,
    Field,
    field_validator,
    model_validator,
)

# --------------------------------------------------------------------------------------
# Shared vocabularies
# --------------------------------------------------------------------------------------

Currency = Literal["EUR", "CZK", "PLN", "USD", "JPY"]
CountryCode = Literal["CZ", "PL", "DE", "AT", "JP", "US", "EE"]
SupplierCountry = Literal["CZ", "PL", "DE", "AT", "JP", "US"]
Plant = Literal["DE", "CZ", "PL"]
Language = Literal["cs", "pl", "de", "en", "ja"]
NumberLocale = Literal["1.234,56", "1 234,56", "1,234.56"]
DateFormat = Literal["DD.MM.YYYY", "YYYY-MM-DD", "MM/DD/YYYY"]
Terms = Literal["net30", "net45", "2/10 net30"]
Uom = Literal["pc", "box50", "kg", "m"]
Category = Literal[
    "seat frames",
    "foam",
    "trim fabric",
    "headrest mechanisms",
    "wire harnesses",
    "fasteners",
    "tooling",
    "freight",
]
TemplateId = Literal[
    "cz_faktura_classic",
    "cz_faktura_totals_top",
    "pl_faktura_vat",
    "pl_faktura_vat_columns",
    "de_rechnung",
    "de_rechnung_two_page",
    "jp_seikyusho",
    "us_invoice",
]
TEMPLATE_IDS: tuple[str, ...] = get_args(TemplateId)
VatTreatment = Literal["domestic", "reverse_charge", "export", "wrongly_charged"]
ReceivedVia = Literal["edi", "portal", "email", "scan", "judge"]


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=False)


class InvoiceFormat(StrEnum):
    STRUCTURED = "structured"
    PDF = "pdf"
    PNG = "png"
    EML = "eml"


# --------------------------------------------------------------------------------------
# Master data
# --------------------------------------------------------------------------------------


class Address(StrictModel):
    street: str
    city: str
    postal_code: str
    country: CountryCode


class PriceList(StrictModel):
    id: str
    supplier_id: str
    effective_from: date
    effective_to: date | None
    currency: Currency
    prices: dict[str, int] = Field(description="sku -> unit price in minor units")


class BankAccount(StrictModel):
    kind: Literal["iban", "us_aba", "jp_domestic"]
    iban: str | None
    bic: str | None
    bank_name: str
    account_number: str | None
    routing_number: str | None
    bank_code: str | None
    branch_code: str | None

    @model_validator(mode="after")
    def _shape(self) -> "BankAccount":
        if self.kind == "iban" and not self.iban:
            raise ValueError("kind=iban requires an IBAN")
        if self.kind == "us_aba" and not (self.routing_number and self.account_number):
            raise ValueError("kind=us_aba requires routing_number and account_number")
        if self.kind == "jp_domestic" and not (
            self.bank_code and self.branch_code and self.account_number
        ):
            raise ValueError("kind=jp_domestic requires bank_code, branch_code and account_number")
        return self


class Supplier(StrictModel):
    id: str = Field(pattern=r"^V-\d{4}$")
    legal_name: str
    name_variants: list[str] = Field(min_length=3, max_length=5)
    country: SupplierCountry
    currency: Currency
    vat_id: str
    iban: str | None = None
    bank_account: BankAccount
    terms: Terms
    purchasing_contact: str
    onboarded_at: date
    invoice_template_id: TemplateId
    language: Language
    number_locale: NumberLocale
    date_format: DateFormat
    email: str
    domain: str
    phone: str
    address: Address
    category: Category
    registration_id: str
    price_lists: list[PriceList]

    @model_validator(mode="after")
    def _iban_matches_account(self) -> "Supplier":
        if self.iban is None:
            self.iban = self.bank_account.iban
        elif self.iban != self.bank_account.iban:
            raise ValueError("Supplier.iban must mirror bank_account.iban")
        return self


class POLine(StrictModel):
    line_no: int = Field(ge=1)
    sku: str
    description: str
    description_local: str
    qty: int = Field(ge=1)
    uom: Uom
    unit_price_minor: int = Field(ge=0)
    currency: Currency
    price_list_id: str
    requested_delivery: date


class PurchaseOrder(StrictModel):
    id: str = Field(pattern=r"^PO-\d{5}$")
    supplier_id: str
    buyer: str
    created_at: AwareDatetime
    order_date: date
    ship_to_plant: Plant
    currency: Currency
    lines: list[POLine] = Field(min_length=1, max_length=4)
    notes: str | None
    version: int = Field(default=1, ge=1)


class GoodsReceipt(StrictModel):
    id: str = Field(pattern=r"^GR-\d{4}$")
    po_id: str
    line_no: int = Field(ge=1)
    received_at: AwareDatetime
    booked_at: AwareDatetime
    qty: int = Field(ge=1)
    uom: Uom
    dock_clerk: str
    plant: Plant
    note: str | None

    @model_validator(mode="after")
    def _booked_after_received(self) -> "GoodsReceipt":
        if self.booked_at < self.received_at:
            raise ValueError("booked_at must be >= received_at")
        return self


class Attachment(StrictModel):
    filename: str
    content_type: str
    size_bytes: int = Field(ge=0)
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class BankDetails(StrictModel):
    iban: str | None = None
    bic: str | None = None
    bank_name: str | None = None
    account_number: str | None = None
    routing_number: str | None = None
    bank_code: str | None = None
    branch_code: str | None = None
    holder: str | None = None


class InvoiceLine(StrictModel):
    line_no: int = Field(ge=1)
    sku: str | None
    description: str
    qty: int = Field(ge=1)
    uom: Uom
    unit_price_minor: int
    net_minor: int
    vat_rate_bp: int = Field(ge=0)
    vat_minor: int
    gross_minor: int
    po_id: str | None
    po_line_no: int | None
    gr_ids: list[str]


class Invoice(StrictModel):
    """Ground truth for one invoice document. Agents never read this; the scoreboard does."""

    id: str = Field(pattern=r"^INV-\d{4}$")
    supplier_id: str | None
    supplier_name_as_written: str
    vat_id_as_written: str | None = None
    invoice_number: str
    kind: Literal["invoice", "credit_note"]
    format: InvoiceFormat
    document_path: str
    template_id: TemplateId | None
    language: Language
    currency: Currency
    number_locale: NumberLocale
    date_format: DateFormat
    invoice_date: date
    due_date: date
    service_date: date | None
    po_refs_as_written: list[str]
    po_ids: list[str]
    lines: list[InvoiceLine] = Field(min_length=1)
    net_minor: int
    vat_minor: int
    gross_minor: int
    vat_treatment: VatTreatment
    vat_note: str | None
    terms: Terms
    discount_bp: int = Field(ge=0)
    discount_deadline: date | None
    retention_minor: int = Field(ge=0)
    prepayment_minor: int = Field(default=0, ge=0)
    bank_details: BankDetails | None
    credit_note_for: str | None
    received_via: ReceivedVia
    attachments: list[Attachment]
    case_tags: list[str]
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def _totals_reconcile(self) -> "Invoice":
        if sum(ln.net_minor for ln in self.lines) != self.net_minor:
            raise ValueError("sum of line nets must equal net_minor")
        if self.net_minor + self.vat_minor != self.gross_minor:
            raise ValueError("net + VAT must equal gross")
        return self


class WrongPoAnswer(StrictModel):
    invoice_number: str
    answers_from_po: str
    answers_qty: int


class PersonScript(StrictModel):
    """Hidden script (spec §2.8). Read by sim/people.py in Part 5, never by agents."""

    email_reply_when_compromised: str | None = None
    phone_reply: str | None = None
    delayed_replies: dict[str, int] = Field(
        default_factory=dict, description="invoice number -> hours"
    )
    wrong_po_answer: WrongPoAnswer | None = None
    denies_incomplete_evidence: bool = False
    confirms_freight_up_to_minor: int | None = None
    reply_style: str | None = None


class Person(StrictModel):
    id: str
    name: str
    role: str
    email: str
    phone: str
    plant: Plant | None = None
    mailbox_compromised_from: date | None
    active: bool
    left_at: date | None
    persona: str
    script: PersonScript


# --------------------------------------------------------------------------------------
# Event payloads (spec §5.1)
# --------------------------------------------------------------------------------------

ActionType = Literal[
    "extracted",
    "matched",
    "posted",
    "post_rejected",
    "short_pay_notice",
    "duplicate_rejected",
    "asked_colleague",
    "held",
    "onboarding_opened",
    "bank_change_opened",
    "verification_requested",
    "verification_received",
    "account_activated",
    "account_suspended",
    "account_restored",
    "payment_run_built",
    "payment_held",
    "error",
]


class POCreated(StrictModel):
    po: PurchaseOrder


class POChange(StrictModel):
    line_no: int
    field: Literal["unit_price_minor", "qty"]
    old: int
    new: int


class POAmended(StrictModel):
    po_id: str
    amendment_no: int = Field(ge=1)
    reason: str
    changes: list[POChange] = Field(min_length=1)
    po_after: PurchaseOrder


class GoodsReceived(StrictModel):
    receipt: GoodsReceipt


class InvoiceReceived(StrictModel):
    """What the mailroom knows: the document, not its contents."""

    invoice_id: str
    format: InvoiceFormat
    document_path: str
    received_via: ReceivedVia
    sender: str | None
    supplier_hint: str | None
    attachments: list[Attachment]
    size_bytes: int = Field(ge=0)
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    pages: int | None = None


class VendorMessage(StrictModel):
    message_id: str
    channel: Literal["email", "portal", "phone", "judge"]
    vendor_id: str | None
    claimed_supplier_name: str
    from_address: str
    reply_to: str | None
    to: str
    subject: str
    body: str
    eml_path: str | None
    in_reply_to: str | None
    thread_id: str
    attachments: list[Attachment]
    bank_details: BankDetails | None


class ColleagueReply(StrictModel):
    message_id: str
    person_id: str
    channel: Literal["email", "phone", "chat"]
    in_reply_to: str | None
    thread_id: str | None
    subject: str | None
    body: str
    eml_path: str | None
    refs: list[str]
    bounced: bool = False


class BankStatementPayload(StrictModel):
    statement_id: str
    account_iban: str
    currency: Currency
    statement_date: date
    sequence_no: int = Field(ge=1)
    path: str
    mt940: str


class BankStatementLinePayload(StrictModel):
    statement_id: str
    account_iban: str
    currency: Currency
    line_no: int = Field(ge=1)
    value_date: date
    entry_date: date | None
    dc: Literal["D", "C"]
    amount_minor: int = Field(ge=0)
    txn_type: str
    customer_ref: str
    bank_ref: str | None
    remittance: str


class ControllerDecision(StrictModel):
    packet_id: str
    decision: str
    note: str | None
    decided_by: str
    via: Literal["phone", "dashboard", "api"] | None = None


class AgentAction(StrictModel):
    agent: str
    action_type: ActionType
    refs: list[str]
    rule_id: str | None
    inputs: dict[str, Any]
    decision: dict[str, Any]
    explanation: str


class Escalation(StrictModel):
    packet_id: str
    agent: str
    question: str
    severity: Literal["info", "action", "urgent"]
    refs: list[str]
    context: str
    evidence: list[dict[str, Any]]
    materiality_minor: int | None
    currency: Currency | None
    recommendation: str
    recommendation_reason: str
    options: list[str]
    expires_at: AwareDatetime | None
    auto_action_on_expiry: Literal["hold"] = "hold"


class PaymentRequested(StrictModel):
    pay_id: str
    payable_ids: list[str]
    vendor_id: str
    account_hash: str
    account_masked: str
    amount_minor: int = Field(ge=0)
    currency: Currency
    eur_equiv_minor: int
    fx_rate: str | None
    rate_date: date | None
    requested_by: str
    frame_hex: str | None


class ChipResponse(StrictModel):
    pay_id: str
    ok: bool
    tag_hex: str | None
    reasons: list[str]
    latency_ms: int | None
    raw_hex: str | None
    signer: Literal["stub", "hw"]


class PaymentExecuted(StrictModel):
    pay_id: str
    bank_ref: str
    vendor_id: str
    amount_minor: int = Field(ge=0)
    currency: Currency
    account_masked: str
    executed_at: AwareDatetime
    statement_date: date


class Provisioning(StrictModel):
    vendor_id: str
    account_id: str
    slot: int | None
    account_hash_prefix: str
    ok: bool
    reason: str | None


class DayBoundary(StrictModel):
    date: _Date
    day_index: int = Field(ge=0)
    previous_date: _Date | None
    month_closed: str | None = Field(default=None, pattern=r"^\d{4}-\d{2}$")


class JudgeInjection(StrictModel):
    kind: Literal["invoice", "message"]
    submitted_by: str | None
    form: dict[str, Any]
    resulting_event_id: str | None


class SystemError(StrictModel):
    component: str
    code: str
    message: str
    refs: list[str]
    recoverable: bool


EventType = Literal[
    "po_created",
    "po_amended",
    "goods_received",
    "invoice_received",
    "vendor_message",
    "colleague_reply",
    "bank_statement",
    "bank_statement_line",
    "controller_decision",
    "agent_action",
    "escalation",
    "payment_requested",
    "chip_response",
    "payment_executed",
    "provisioning",
    "day_boundary",
    "judge_injection",
    "system_error",
]
EVENT_TYPES: tuple[str, ...] = get_args(EventType)

PAYLOAD_MODELS: dict[str, type[BaseModel]] = {
    "po_created": POCreated,
    "po_amended": POAmended,
    "goods_received": GoodsReceived,
    "invoice_received": InvoiceReceived,
    "vendor_message": VendorMessage,
    "colleague_reply": ColleagueReply,
    "bank_statement": BankStatementPayload,
    "bank_statement_line": BankStatementLinePayload,
    "controller_decision": ControllerDecision,
    "agent_action": AgentAction,
    "escalation": Escalation,
    "payment_requested": PaymentRequested,
    "chip_response": ChipResponse,
    "payment_executed": PaymentExecuted,
    "provisioning": Provisioning,
    "day_boundary": DayBoundary,
    "judge_injection": JudgeInjection,
    "system_error": SystemError,
}


class Event(BaseModel):
    """Envelope stored in the append-only event table and streamed over the WebSocket."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str = Field(pattern=r"^ev_[0-9a-f]{6,16}$")
    seq: int | None = None
    type: EventType
    event_time: AwareDatetime
    known_at: AwareDatetime
    source: str = Field(min_length=1)
    actor: str | None
    payload: dict[str, Any]

    @field_validator("event_time", "known_at")
    @classmethod
    def _to_utc(cls, value: datetime) -> datetime:
        return value.astimezone(UTC)

    @model_validator(mode="before")
    @classmethod
    def _normalise_payload(cls, data: Any) -> Any:
        if not isinstance(data, dict):
            return data
        type_ = data.get("type")
        payload = data.get("payload")
        model = PAYLOAD_MODELS.get(type_) if isinstance(type_, str) else None
        if model is None or payload is None:
            return data
        if isinstance(payload, BaseModel):
            payload = payload.model_dump(mode="json")
        if not isinstance(payload, dict):
            raise ValueError("payload must be a mapping")
        try:
            typed = model.model_validate(payload)
        except ValueError as exc:
            raise ValueError(f"payload invalid for {type_}: {exc}") from exc
        return {**data, "payload": typed.model_dump(mode="json")}

    @model_validator(mode="after")
    def _known_at_not_before_event_time(self) -> "Event":
        if self.known_at < self.event_time:
            raise ValueError("known_at must be >= event_time")
        return self

    def typed_payload(self) -> Any:
        return PAYLOAD_MODELS[self.type].model_validate(self.payload)


# --------------------------------------------------------------------------------------
# Answer key (spec §2.9)
# --------------------------------------------------------------------------------------


class EntryMatch(StrictModel):
    evidence_contains: str | None = None
    cause_id: str | None = None
    description_contains: str | None = None


class JournalAmountCheck(StrictModel):
    kind: Literal["journal_amount"]
    entry_match: EntryMatch
    account: str
    debit_minor: int | None = None
    credit_minor: int | None = None
    currency: str | None = None


class JournalCountCheck(StrictModel):
    kind: Literal["journal_count"]
    entry_match: EntryMatch
    count: int = Field(ge=0)


class EventExistsCheck(StrictModel):
    kind: Literal["event_exists"]
    type: EventType
    where: dict[str, Any]


class NoEventCheck(StrictModel):
    kind: Literal["no_event"]
    type: EventType
    where: dict[str, Any]


class PayableStatusCheck(StrictModel):
    kind: Literal["payable_status"]
    inv_id: str
    status: Literal["open", "scheduled", "paid", "held", "rejected", "netted"]
    discount_bp: int | None = None
    retention_minor: int | None = None


class VendorAccountStatusCheck(StrictModel):
    kind: Literal["vendor_account_status"]
    vendor_id: str
    iban: str
    status: Literal["pending", "active", "suspended", "retired"]


class ChipResponseCheck(StrictModel):
    kind: Literal["chip_response"]
    where: dict[str, Any]
    ok: bool
    reasons_contains: list[str] = Field(default_factory=list)


class PacketExistsCheck(StrictModel):
    kind: Literal["packet_exists"]
    where: dict[str, Any]
    recommendation: str | None = None


Check = Annotated[
    JournalAmountCheck
    | JournalCountCheck
    | EventExistsCheck
    | NoEventCheck
    | PayableStatusCheck
    | VendorAccountStatusCheck
    | ChipResponseCheck
    | PacketExistsCheck,
    Field(discriminator="kind"),
]


class AnswerKeyEntry(StrictModel):
    id: str
    tier: Literal["headline", "ambient"]
    case: str
    title: str
    expected_outcome: str
    injected_event_ids: list[str] = Field(min_length=1)
    refs: list[str]
    checks: list[Check] = Field(min_length=1)


class AnswerKey(StrictModel):
    seed: int
    headline: list[AnswerKeyEntry]
    ambient: list[AnswerKeyEntry]
