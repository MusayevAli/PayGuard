"""Turn plans into purchase orders, goods receipts and invoice ground truth (dates, numbers,
VAT, totals). Documents are rendered afterwards by ``documents.py``."""

from __future__ import annotations

import random
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from decimal import ROUND_HALF_EVEN, Decimal

from core.tax import ENTITY_VAT_RATE_BP, treatment_for_country
from sim.models import (
    BankDetails,
    GoodsReceipt,
    InvoiceFormat,
    InvoiceLine,
    Language,
    Plant,
    POLine,
    PurchaseOrder,
    ReceivedVia,
    Supplier,
    Terms,
    Uom,
    VatTreatment,
)
from sim.worldgen.catalog import PLANTS, PlantInfo
from sim.worldgen.plan import (
    END,
    AdHocSupplier,
    InvoicePlan,
    Plan,
    POPlan,
    StandalonePlan,
)
from sim.worldgen.rng import business_time

TERM_DAYS: dict[Terms, int] = {"net30": 30, "net45": 45, "2/10 net30": 30}
RECEIVED_VIA: dict[InvoiceFormat, ReceivedVia] = {
    InvoiceFormat.STRUCTURED: "edi",
    InvoiceFormat.PDF: "portal",
    InvoiceFormat.PNG: "scan",
    InvoiceFormat.EML: "email",
}
DELIVERY_DELAY: dict[InvoiceFormat, tuple[int, int]] = {
    InvoiceFormat.STRUCTURED: (0, 1),
    InvoiceFormat.EML: (0, 2),
    InvoiceFormat.PDF: (1, 4),
    InvoiceFormat.PNG: (2, 7),
}

REVERSE_CHARGE_NOTE: dict[Language, str] = {
    "cs": "Daň odvede zákazník – reverse charge (§ 92a zákona o DPH). Osvobozené plnění: dodání zboží do jiného členského státu (§ 64).",
    "pl": "Odwrotne obciążenie – reverse charge. Wewnątrzwspólnotowa dostawa towarów, stawka 0 % (art. 42 ustawy o VAT).",
    "de": "Steuerschuldnerschaft des Leistungsempfängers (Reverse-Charge-Verfahren); steuerfreie innergemeinschaftliche Lieferung gemäß § 6a UStG.",
    "en": "Reverse charge: VAT to be accounted for by the recipient (intra-Community supply, 0 %).",
    "ja": "輸出免税 / Export – consumption tax exempt.",
}
EXPORT_NOTE: dict[Language, str] = {
    "ja": "輸出免税取引 / Export sale – consumption tax exempt (0 %).",
    "en": "Export sale – no sales tax applied.",
    "cs": "Vývoz – osvobozeno.",
    "pl": "Eksport – stawka 0 %.",
    "de": "Ausfuhrlieferung – steuerfrei.",
}
RETENTION_NOTE: dict[Language, str] = {
    "cs": "Zádržné 10 % z celkové částky bude uvolněno po převzetí nástroje (dle smlouvy).",
    "pl": "Kaucja gwarancyjna 10 % wartości faktury, zwalniana po odbiorze końcowym narzędzia.",
    "de": "Sicherheitseinbehalt 10 % des Rechnungsbetrags, fällig nach Endabnahme des Werkzeugs.",
    "en": "10 % retention on the invoice total, released after final tool acceptance.",
    "ja": "請求額の10%は検収後に支払い。",
}
TERMS_TEXT: dict[Language, dict[Terms, str]] = {
    "cs": {
        "net30": "Splatnost 30 dnů od data vystavení.",
        "net45": "Splatnost 45 dnů od data vystavení.",
        "2/10 net30": "Splatnost 30 dnů; skonto 2 % při úhradě do 10 dnů.",
    },
    "pl": {
        "net30": "Termin płatności: 30 dni od daty wystawienia.",
        "net45": "Termin płatności: 45 dni od daty wystawienia.",
        "2/10 net30": "Termin płatności 30 dni; skonto 2 % przy zapłacie do 10 dni.",
    },
    "de": {
        "net30": "Zahlungsziel: 30 Tage netto ab Rechnungsdatum.",
        "net45": "Zahlungsziel: 45 Tage netto ab Rechnungsdatum.",
        "2/10 net30": "Zahlungsziel: 30 Tage netto; 2 % Skonto bei Zahlung innerhalb von 10 Tagen.",
    },
    "en": {"net30": "Net 30", "net45": "Net 45", "2/10 net30": "2/10 Net 30"},
    "ja": {
        "net30": "支払条件: 発行日より30日以内",
        "net45": "支払条件: 発行日より45日以内",
        "2/10 net30": "支払条件: 30日以内 (10日以内2%割引)",
    },
}
LOCAL_PO_PREFIX: dict[Language, str] = {
    "cs": "Obj. č. ",
    "pl": "Zam. nr ",
    "de": "Bestell-Nr. ",
    "ja": "注文番号 ",
    "en": "Order no. ",
}
QUOTE_REF: dict[Language, str] = {
    "cs": "Nabídka Q-2026-118",
    "pl": "Oferta OF/2026/118",
    "de": "Angebot AN-2026-118",
    "ja": "見積番号 Q-2026-118",
    "en": "Quote Q-2026-118",
}


@dataclass
class InvoiceDraft:
    id: str
    plan: InvoicePlan
    supplier: Supplier | AdHocSupplier
    supplier_id: str | None
    supplier_name_as_written: str
    vat_id_as_written: str | None
    invoice_number: str
    kind: str
    language: Language
    currency: str
    number_locale: str
    date_format: str
    invoice_date: date
    due_date: date
    service_date: date | None
    po_refs_as_written: list[str]
    po_ids: list[str]
    lines: list[InvoiceLine]
    net_minor: int
    vat_minor: int
    gross_minor: int
    vat_treatment: VatTreatment
    vat_note: str | None
    terms: Terms
    discount_bp: int
    discount_deadline: date | None
    retention_minor: int
    prepayment_minor: int
    bank_details: BankDetails | None
    credit_note_for: str | None
    received_via: ReceivedVia
    case_tags: list[str]
    ship_to: PlantInfo
    template_id: str | None
    po_ref_text: str
    notes: list[str] = field(default_factory=list)
    terms_text: str = ""
    vat_rate_bp: int = 0


@dataclass
class World:
    seed: int
    suppliers: list[Supplier]
    pos: list[PurchaseOrder]
    po_plans: list[POPlan]
    receipts: list[GoodsReceipt]
    receipt_known_at: dict[str, datetime]
    drafts: list[InvoiceDraft]
    amendment_times: dict[str, datetime]


def _weekday_back(d: date) -> date:
    while d.weekday() >= 5:
        d -= timedelta(days=1)
    return d


def _round(value: Decimal) -> int:
    return int(value.quantize(Decimal(1), rounding=ROUND_HALF_EVEN))


def derive_dates(rng: random.Random, po: POPlan) -> None:
    """Fill receipt dates, invoice dates and the order date for a baseline PO plan."""
    prev: date | None = None
    for sh in po.shipments:
        for inv in sh.invoices:
            if inv.invoice_date is None:
                lo, hi = DELIVERY_DELAY[inv.format]
                inv.invoice_date = _weekday_back(
                    inv.known_at.date() - timedelta(days=rng.randrange(lo, hi + 1))
                )
        first_inv = (
            min(inv.invoice_date for inv in sh.invoices if inv.invoice_date is not None)
            if sh.invoices
            else None
        )
        if sh.receipt_date is None and first_inv is not None:
            sh.receipt_date = _weekday_back(first_inv - timedelta(days=rng.randrange(0, 7)))
        if sh.receipt_date is not None and prev is not None and sh.receipt_date <= prev:
            sh.receipt_date = prev + timedelta(days=1)
            while sh.receipt_date.weekday() >= 5:
                sh.receipt_date += timedelta(days=1)
            for inv in sh.invoices:
                if inv.invoice_date is not None and inv.invoice_date < sh.receipt_date:
                    inv.invoice_date = sh.receipt_date
        if sh.receipt_date is not None:
            prev = sh.receipt_date
    receipt_dates = [sh.receipt_date for sh in po.shipments if sh.receipt_date is not None]
    if po.order_date is None:
        first = min(receipt_dates) if receipt_dates else date(2026, 10, 1)
        order = _weekday_back(first - timedelta(days=rng.randrange(5, 31)))
        last = max(receipt_dates) if receipt_dates else first
        if last - order > timedelta(days=40):
            order = _weekday_back(last - timedelta(days=40))
            if order >= first:
                order = _weekday_back(first - timedelta(days=1))
        po.order_date = order


class Numbering:
    """Per-supplier invoice numbering with fixed trap numbers folded into the sequence."""

    STYLES: dict[str, tuple[str, int]] = {
        "V-0001": ("KT-", 0),
        "V-0002": ("FT-", 0),
        "V-0003": ("GF-", 0),
        "V-0004": ("LB-", 0),
        "V-0005": ("HM-", 2),
        "V-0017": ("RE-2026-", 4),
        "V-0038": ("AR-26-", 4),
    }
    LANG_STYLES: dict[Language, list[tuple[str, int]]] = {
        "cs": [("2026", 4), ("FV-2026/", 3), ("2026-", 4)],
        "pl": [("FV/2026/10/", 4), ("FS ", 3), ("F/2026/", 4)],
        "de": [("RE-2026-", 4), ("2026-", 5), ("R", 6)],
        "ja": [("INV-2026-", 4)],
        "en": [("", 5), ("INV-", 5)],
    }

    def __init__(self, rng: random.Random) -> None:
        self.rng = rng

    def assign(self, supplier_key: str, language: Language, invoices: list[InvoiceDraft]) -> None:
        seq_members = [
            d for d in sorted(invoices, key=lambda d: (d.invoice_date, d.id)) if d.kind == "invoice"
        ]
        fixed = {d.id: d.plan.number for d in seq_members if d.plan.number is not None}
        if supplier_key in self.STYLES:
            prefix, width = self.STYLES[supplier_key]
        else:
            prefix, width = self.rng.choice(self.LANG_STYLES[language])
        gaps = [self.rng.randrange(1, 4) for _ in seq_members]
        cumulative = []
        total = 0
        for g in gaps:
            total += g
            cumulative.append(total)
        base = self.rng.randrange(40, 900) if width <= 3 else self.rng.randrange(100, 4000)
        skip: set[str] = set()
        for i, d in enumerate(seq_members):
            if d.id in fixed:
                digits = "".join(ch for ch in fixed[d.id] if ch.isdigit())
                if digits and fixed[d.id].startswith(prefix) and fixed[d.id] == prefix + digits:
                    base = int(digits) - cumulative[i]
                    break
                skip.add(d.id)
        used: set[str] = set(fixed.values())
        for i, d in enumerate(seq_members):
            if d.id in fixed:
                d.invoice_number = fixed[d.id]
                continue
            n = base + cumulative[i]
            if n <= 0:
                n = cumulative[i]
            while True:
                number = f"{prefix}{n:0{width}d}" if width else f"{prefix}{n}"
                if prefix == "FS ":
                    number = f"FS {n}/{d.invoice_date.month:02d}/2026"
                if number not in used:
                    break
                n += 1
            used.add(number)
            d.invoice_number = number
        for d in invoices:
            if d.kind == "credit_note":
                d.invoice_number = d.plan.number or f"CN-{self.rng.randrange(1, 40)}"


def vat_for(
    supplier: Supplier | AdHocSupplier,
    plant: PlantInfo,
    language: Language,
    override_bp: int | None,
) -> tuple[int, VatTreatment, str | None]:
    """Entity-based VAT (spec §2.1): DE suppliers domestic 19 %, other EU reverse charge, JP/US import."""
    if override_bp is not None:
        return override_bp, "wrongly_charged", None
    treatment = treatment_for_country(supplier.country)
    if treatment == "import":
        return 0, "export", EXPORT_NOTE[language]
    if treatment == "domestic":
        return ENTITY_VAT_RATE_BP, "domestic", None
    return 0, "reverse_charge", REVERSE_CHARGE_NOTE[language]


def po_ref_text(style: str, po_ids: list[str], language: Language) -> list[str]:
    if style == "missing":
        return []
    if style == "quote":
        return [QUOTE_REF[language]]
    out = []
    for po_id in po_ids:
        digits = po_id.split("-")[1]
        out.append(
            {
                "standard": po_id,
                "spaced": f"PO {digits}",
                "bare": digits,
                "hash": f"P.O. #{digits}",
                "local": f"{LOCAL_PO_PREFIX[language]}{digits}",
                "year": f"{po_id}/2026",
            }[style]
        )
    return out


def materialize(rng: random.Random, seed: int, suppliers: list[Supplier], plan: Plan) -> World:
    # ---- dates for baseline plans
    for po in plan.pos:
        derive_dates(rng, po)
    # ---- purchase orders (ids by order date)
    ordered = sorted(plan.pos, key=lambda p: (p.order_date or END, p.supplier.id, id(p)))
    pos: list[PurchaseOrder] = []
    amendment_times: dict[str, datetime] = {}
    for n, po in enumerate(ordered, start=10001):
        po.po_id = f"PO-{n}"
        assert po.order_date is not None
        created = business_time(rng, po.order_date, 8, 16)
        lines = []
        for i, line in enumerate(po.lines, start=1):
            price, qty = line.unit_price_minor, line.qty
            if po.amendment is not None and po.amendment.line_index == i - 1:
                if po.amendment.field == "unit_price_minor":
                    price = po.amendment.old_value
                else:
                    qty = po.amendment.old_value
            lines.append(
                POLine(
                    line_no=i,
                    sku=line.sku.sku,
                    description=line.sku.name("en"),
                    description_local=line.sku.name(po.supplier.language),
                    qty=qty,
                    uom=line.uom,
                    unit_price_minor=price,
                    currency=po.supplier.currency,
                    price_list_id=line.price_list_id,
                    requested_delivery=po.order_date + timedelta(days=rng.randrange(10, 36)),
                )
            )
        pos.append(
            PurchaseOrder(
                id=po.po_id,
                supplier_id=po.supplier.id,
                buyer=po.buyer,
                created_at=created,
                order_date=po.order_date,
                ship_to_plant=po.ship_to_plant,
                currency=po.supplier.currency,
                lines=lines,
                notes=None,
                version=1,
            )
        )
        if po.amendment is not None:
            first_receipt = min(
                (sh.receipt_date for sh in po.shipments if sh.receipt_date is not None),
                default=None,
            )
            day = po.order_date + timedelta(days=po.amendment.days_after_order)
            if first_receipt is not None and day >= first_receipt:
                day = max(po.order_date, first_receipt - timedelta(days=1))
            t = business_time(rng, day, 9, 17)
            if t <= created:
                t = created + timedelta(hours=2)
            amendment_times[po.po_id] = t
    # ---- goods receipts (ids chronological; GR-4471 reserved for T1)
    receipt_specs: list[tuple[datetime, datetime, POPlan, int, int, int, Uom, str]] = []
    for po in plan.pos:
        for sh in po.shipments:
            if sh.receipt_date is None:
                continue
            for i, line in enumerate(po.lines):
                qty = line.deliveries.get(sh.index)
                if qty is None:
                    continue
                if sh.receipt_known_at is not None:
                    booked = sh.receipt_known_at
                    received = booked - timedelta(minutes=50)
                else:
                    received = business_time(rng, sh.receipt_date, 6, 15)
                    booked = received + timedelta(minutes=rng.randrange(20, 300))
                line_no = line.receipt_line_override or (i + 1)
                uom = line.receipt_uom_override or line.uom
                qty_booked = (
                    line.receipt_qty_override if line.receipt_qty_override is not None else qty
                )
                receipt_specs.append(
                    (
                        booked,
                        received,
                        po,
                        sh.index,
                        line_no,
                        qty_booked,
                        uom,
                        f"{po.po_id}:{i}:{sh.index}",
                    )
                )
    receipt_specs.sort(key=lambda r: (r[0], r[7]))
    t1_index = next(i for i, r in enumerate(receipt_specs) if "T1" in r[2].tags)
    base = 4471 - t1_index
    receipts: list[GoodsReceipt] = []
    receipt_known_at: dict[str, datetime] = {}
    clerks = {
        "DE": ["jonas.weber", "jonas.weber", "stefan.koch"],
        "CZ": ["pavel.dvorak", "lucie.novotna"],
        "PL": ["krzysztof.zielinski", "marek.wojcik"],
    }
    for i, (booked, received, po, sh_index, line_no, qty, uom, key) in enumerate(receipt_specs):
        gr_id = f"GR-{base + i:04d}"
        sh = next(s for s in po.shipments if s.index == sh_index)
        sh.receipt_ids.append(gr_id)
        sh.receipt_by_line[int(key.split(":")[1])] = gr_id
        clerk = "jonas.weber" if "T5" in po.tags else rng.choice(clerks[po.ship_to_plant])
        receipts.append(
            GoodsReceipt(
                id=gr_id,
                po_id=po.po_id or "",
                line_no=line_no,
                received_at=received,
                booked_at=booked,
                qty=qty,
                uom=uom,
                dock_clerk=clerk,
                plant=po.ship_to_plant,
                note=None,
            )
        )
        receipt_known_at[gr_id] = booked
    # ---- invoices
    drafts: list[InvoiceDraft] = []
    all_plans: list[tuple[InvoicePlan, POPlan | None, StandalonePlan | None]] = []
    for po in plan.pos:
        for sh in po.shipments:
            for inv in sh.invoices:
                all_plans.append((inv, po, None))
    for st in plan.standalone:
        all_plans.append((st.invoice, None, st))
    all_plans.sort(key=lambda t: (t[0].known_at, t[1].po_id if t[1] else "", t[0].number or ""))
    for n, (inv, po, st) in enumerate(all_plans, start=1):
        inv.invoice_id = f"INV-{n:04d}"
        drafts.append(build_draft(rng, inv, po, st))
    numbering = Numbering(rng)
    by_supplier: dict[str, list[InvoiceDraft]] = {}
    for d in drafts:
        key = d.supplier_id or d.supplier_name_as_written
        by_supplier.setdefault(key, []).append(d)
    for key, group in by_supplier.items():
        numbering.assign(key, group[0].language, group)
    for d in drafts:
        if d.credit_note_for is None and d.plan.credit_note_for:
            d.credit_note_for = d.plan.credit_note_for
    return World(
        seed=seed,
        suppliers=suppliers,
        pos=pos,
        po_plans=plan.pos,
        receipts=receipts,
        receipt_known_at=receipt_known_at,
        drafts=drafts,
        amendment_times=amendment_times,
    )


def build_draft(
    rng: random.Random, inv: InvoicePlan, po: POPlan | None, st: StandalonePlan | None
) -> InvoiceDraft:
    supplier = po.supplier if po is not None else st.supplier  # type: ignore[union-attr]
    plant = PLANTS[po.ship_to_plant if po is not None else st.ship_to_plant]  # type: ignore[union-attr]
    if isinstance(supplier, Supplier):
        language, currency, locale, date_format, terms = (
            supplier.language,
            supplier.currency,
            supplier.number_locale,
            supplier.date_format,
            supplier.terms,
        )
        template = supplier.invoice_template_id
        supplier_id: str | None = supplier.id
        name = supplier.legal_name
        if inv.name_variant is not None:
            name = supplier.name_variants[inv.name_variant % len(supplier.name_variants)]
        elif not inv.format_fixed and rng.random() < 0.2:
            name = rng.choice(supplier.name_variants)
        vat_written = supplier.vat_id
        bank = BankDetails(
            iban=supplier.bank_account.iban,
            bic=supplier.bank_account.bic,
            bank_name=supplier.bank_account.bank_name,
            account_number=supplier.bank_account.account_number,
            routing_number=supplier.bank_account.routing_number,
            bank_code=supplier.bank_account.bank_code,
            branch_code=supplier.bank_account.branch_code,
            holder=supplier.legal_name,
        )
    else:
        language, currency, locale, date_format, terms = (
            "en",
            "EUR",
            "1,234.56",
            "YYYY-MM-DD",
            "net30",
        )
        template = "us_invoice"
        supplier_id = None
        name = supplier.name
        vat_written = supplier.vat_id
        bank = BankDetails(
            iban=supplier.iban, bic=supplier.bic, bank_name=supplier.bank_name, holder=supplier.name
        )
    assert inv.invoice_date is not None
    invoice_date = inv.invoice_date
    due = invoice_date + timedelta(days=TERM_DAYS[terms])
    rate_bp, treatment, note = vat_for(supplier, plant, language, inv.vat_override_bp)
    lines: list[InvoiceLine] = []
    po_ids: list[str] = []
    service: date | None = inv.service_date
    if po is not None:
        for b in inv.billed:
            src = b.po_plan or po
            line = src.lines[b.line_index]
            uom = b.uom_override or line.uom
            net = b.qty * b.unit_price_minor
            vat = _round(Decimal(net) * Decimal(rate_bp) / Decimal(10000))
            if src.po_id not in po_ids:
                po_ids.append(src.po_id or "")
            sh = (
                next(s for s in src.shipments if any(i is inv for i in s.invoices))
                if src is po
                else src.shipments[0]
            )
            booked_right = line.receipt_line_override is None and b.line_index in sh.receipt_by_line
            gr_ids = [sh.receipt_by_line[b.line_index]] if booked_right else []
            desc = b.description_override or line.sku.name(language)
            lines.append(
                InvoiceLine(
                    line_no=len(lines) + 1,
                    sku=None if b.description_override else line.sku.sku,
                    description=desc,
                    qty=b.qty,
                    uom=uom,
                    unit_price_minor=b.unit_price_minor,
                    net_minor=net,
                    vat_rate_bp=rate_bp,
                    vat_minor=vat,
                    gross_minor=net + vat,
                    po_id=src.po_id,
                    po_line_no=b.line_index + 1,
                    gr_ids=gr_ids,
                )
            )
        sh0 = next(s for s in po.shipments if any(i is inv for i in s.invoices))
        if service is None:
            service = (
                sh0.receipt_date
                if sh0.receipt_date is not None and sh0.receipt_date <= invoice_date
                else invoice_date
            )
    else:
        assert st is not None
        for sl in st.lines:
            net = sl.qty * sl.unit_price_minor
            vat = _round(Decimal(net) * Decimal(rate_bp) / Decimal(10000))
            lines.append(
                InvoiceLine(
                    line_no=len(lines) + 1,
                    sku=sl.sku.sku if sl.sku else None,
                    description=sl.description,
                    qty=sl.qty,
                    uom=sl.uom,
                    unit_price_minor=sl.unit_price_minor,
                    net_minor=net,
                    vat_rate_bp=rate_bp,
                    vat_minor=vat,
                    gross_minor=net + vat,
                    po_id=None,
                    po_line_no=None,
                    gr_ids=[],
                )
            )
        if service is None:
            service = invoice_date - timedelta(days=rng.randrange(1, 4))
    net_total = sum(ln.net_minor for ln in lines)
    vat_total = sum(ln.vat_minor for ln in lines) + inv.rounding_delta_minor
    gross_total = net_total + vat_total
    refs = (
        po_ref_text(inv.po_ref_style, po_ids, language)
        if po is not None
        else ([st.po_ref_as_written] if st and st.po_ref_as_written else [])
    )
    discount_bp = 200 if terms == "2/10 net30" else 0
    deadline = invoice_date + timedelta(days=10) if discount_bp else None
    retention = _round(Decimal(gross_total) * Decimal("0.10")) if inv.retention else 0
    notes: list[str] = []
    if note:
        notes.append(note)
    if inv.retention:
        notes.append(RETENTION_NOTE[language])
    if inv.extra_note:
        notes.append(inv.extra_note)
    return InvoiceDraft(
        id=inv.invoice_id or "",
        plan=inv,
        supplier=supplier,
        supplier_id=supplier_id,
        supplier_name_as_written=name,
        vat_id_as_written=vat_written,
        invoice_number=inv.number or "",
        kind=inv.kind,
        language=language,
        currency=currency,
        number_locale=locale,
        date_format=date_format,
        invoice_date=invoice_date,
        due_date=due,
        service_date=service,
        po_refs_as_written=refs,
        po_ids=po_ids,
        lines=lines,
        net_minor=net_total,
        vat_minor=vat_total,
        gross_minor=gross_total,
        vat_treatment=treatment,
        vat_note=note,
        terms=terms,
        discount_bp=discount_bp,
        discount_deadline=deadline,
        retention_minor=retention,
        prepayment_minor=inv.prepayment_minor,
        bank_details=bank,
        credit_note_for=inv.credit_note_for,
        received_via=RECEIVED_VIA[inv.format],
        case_tags=list(inv.case_tags),
        ship_to=plant,
        template_id=None if inv.format == InvoiceFormat.STRUCTURED else template,
        po_ref_text=", ".join(refs),
        notes=notes,
        terms_text=TERMS_TEXT[language][terms],
        vat_rate_bp=rate_bp,
    )


def plant_for(draft: InvoiceDraft) -> Plant:
    return draft.ship_to.code


def utc(dt: datetime) -> datetime:
    return dt.astimezone(UTC)
