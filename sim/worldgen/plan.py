"""Planning model for the world: purchase orders, shipments (receipts) and the invoices that
bill them. Cases (spec §2.6, A1–A15, M1–M6) are expressed as plans too, so one materialiser
turns everything into purchase orders, goods receipts, invoice ground truth and documents."""

from __future__ import annotations

import random
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from typing import Literal

from sim.models import InvoiceFormat, Plant, Supplier, Uom
from sim.worldgen.catalog import SKUS_BY_CATEGORY, Sku
from sim.worldgen.rng import weighted_choice
from sim.worldgen.suppliers import applicable_price_list

START = date(2026, 10, 1)
END = date(2026, 11, 15)
WINDOW_DAYS = (END - START).days + 1

PoRefStyle = Literal["standard", "spaced", "bare", "hash", "local", "missing", "quote", "year"]


@dataclass
class LinePlan:
    sku: Sku
    qty: int
    uom: Uom
    unit_price_minor: int
    price_list_id: str
    deliveries: dict[int, int] = field(default_factory=dict)  # shipment index -> qty received
    receipt_line_override: int | None = None  # M5: receipt booked under this other line number
    receipt_uom_override: Uom | None = None  # M6
    receipt_qty_override: int | None = None  # M6


@dataclass
class BilledLine:
    line_index: int
    qty: int
    unit_price_minor: int
    po_plan: POPlan | None = None  # A5: the line lives on another PO of the same supplier
    description_override: str | None = None
    uom_override: Uom | None = None


@dataclass
class InvoicePlan:
    known_at: datetime
    format: InvoiceFormat
    billed: list[BilledLine]
    invoice_date: date | None = None
    service_date: date | None = None
    number: str | None = None
    po_ref_style: PoRefStyle = "standard"
    case_tags: list[str] = field(default_factory=list)
    vat_override_bp: int | None = None
    rounding_delta_minor: int = 0
    retention: bool = False
    prepayment_minor: int = 0
    name_variant: int | None = None
    kind: Literal["invoice", "credit_note"] = "invoice"
    credit_note_for: str | None = None
    delivery_delay_days: int | None = None
    scan_annotated: bool = False
    format_fixed: bool = False
    eml_thread: bool = False
    eml_reply_to_differs: bool = False
    extra_note: str | None = None
    # filled by the materialiser
    invoice_id: str | None = None


@dataclass
class ShipmentPlan:
    index: int
    receipt_date: date | None  # None: nothing booked for this shipment
    invoices: list[InvoicePlan] = field(default_factory=list)
    receipt_known_at: datetime | None = None  # override (T5: booked 30 h after the invoice)
    receipt_ids: list[str] = field(default_factory=list)
    receipt_by_line: dict[int, str] = field(default_factory=dict)  # line index -> GR id (as booked)


@dataclass
class AmendmentPlan:
    days_after_order: int
    line_index: int
    field: Literal["unit_price_minor", "qty"]
    old_value: int
    new_value: int
    reason: str


@dataclass
class POPlan:
    supplier: Supplier
    ship_to_plant: Plant
    lines: list[LinePlan]
    shipments: list[ShipmentPlan]
    buyer: str
    order_date: date | None = None
    amendment: AmendmentPlan | None = None
    tags: list[str] = field(default_factory=list)
    po_id: str | None = None


@dataclass
class AdHocSupplier:
    """A sender that is not in the vendor master (T6)."""

    name: str
    country: str
    vat_id: str
    iban: str
    bic: str
    bank_name: str
    street: str
    city: str
    postal_code: str
    email: str
    domain: str
    phone: str
    language: Literal["en"]
    currency: Literal["EUR"]


@dataclass
class StandaloneLine:
    sku: Sku | None
    description: str
    qty: int
    uom: Uom
    unit_price_minor: int


@dataclass
class StandalonePlan:
    """An invoice without a purchase order (freight, credit notes, unknown vendors)."""

    supplier: Supplier | AdHocSupplier
    lines: list[StandaloneLine]
    invoice: InvoicePlan
    ship_to_plant: Plant
    po_ref_as_written: str | None = None


@dataclass
class Plan:
    pos: list[POPlan]
    standalone: list[StandalonePlan]


# --------------------------------------------------------------------------------------
# Baseline
# --------------------------------------------------------------------------------------

CATEGORY_WEIGHT = {
    "seat frames": 1.3,
    "foam": 1.2,
    "trim fabric": 1.0,
    "headrest mechanisms": 1.0,
    "wire harnesses": 1.0,
    "fasteners": 1.2,
    "tooling": 0.35,
    "freight": 0.3,
}
QTY_CHOICES: dict[Uom, list[int]] = {
    "pc": [100, 200, 250, 400, 500, 800, 1000, 1200, 1500, 2000, 2500, 4000],
    "box50": [10, 20, 24, 40, 50, 80, 100, 120],
    "kg": [50, 100, 200, 250, 400, 500, 800],
    "m": [100, 200, 300, 500, 800, 1000, 1200, 1500],
}
PLANT_BY_COUNTRY: dict[str, list[tuple[Plant, float]]] = {
    "CZ": [("CZ", 0.7), ("DE", 0.3)],
    "PL": [("PL", 0.7), ("DE", 0.3)],
    "DE": [("DE", 0.85), ("CZ", 0.1), ("PL", 0.05)],
    "AT": [("DE", 0.8), ("CZ", 0.2)],
    "JP": [("DE", 1.0)],
    "US": [("DE", 1.0)],
}


def day_weight(d: date) -> float:
    return {5: 0.15, 6: 0.08}.get(d.weekday(), 1.0)


def sample_known_at(
    rng: random.Random, fmt: InvoiceFormat, *, earliest: date = START, latest: date = END
) -> datetime:
    days = [earliest + timedelta(days=i) for i in range((latest - earliest).days + 1)]
    day = weighted_choice(rng, days, [day_weight(d) for d in days])
    if fmt in (InvoiceFormat.EML, InvoiceFormat.STRUCTURED):
        minute = rng.randrange(6 * 60, 20 * 60)
    else:
        minute = rng.randrange(8 * 60, 17 * 60)
    return datetime(
        day.year, day.month, day.day, minute // 60, minute % 60, rng.randrange(60), tzinfo=UTC
    )


def at(day: date, hour: int, minute: int = 0, second: int = 0) -> datetime:
    return datetime(day.year, day.month, day.day, hour, minute, second, tzinfo=UTC)


def pick_plant(rng: random.Random, supplier: Supplier) -> Plant:
    options = PLANT_BY_COUNTRY[supplier.country]
    return weighted_choice(rng, [p for p, _ in options], [w for _, w in options])


def po_price(supplier: Supplier, sku: str, on: date) -> tuple[int, str]:
    pl = applicable_price_list(supplier, on)
    if pl is None:
        # order falls in a price-list gap: use the latest list that started before the date
        earlier = [p for p in supplier.price_lists if p.effective_from <= on]
        pl = max(earlier, key=lambda p: p.effective_from)
    return pl.prices[sku], pl.id


def make_line(
    rng: random.Random, supplier: Supplier, sku: Sku, order_date: date, qty: int | None = None
) -> LinePlan:
    if qty is None:
        if supplier.category == "tooling":
            qty = rng.choice([1, 1, 1, 2])
        elif supplier.category == "freight":
            qty = rng.choice([1, 1, 2, 3])
        else:
            qty = rng.choice(QTY_CHOICES[sku.uom])
    price, list_id = po_price(supplier, sku.sku, order_date)
    return LinePlan(sku=sku, qty=qty, uom=sku.uom, unit_price_minor=price, price_list_id=list_id)


def split_qty(rng: random.Random, qty: int, parts: int) -> list[int]:
    """Split ``qty`` into ``parts`` positive integers with a lot-like structure."""
    if parts == 1:
        return [qty]
    cuts = sorted(rng.sample(range(1, qty), parts - 1)) if qty > parts else list(range(1, parts))
    pieces = [b - a for a, b in zip([0, *cuts], [*cuts, qty], strict=True)]
    return [max(1, p) for p in pieces]


def baseline_po(
    rng: random.Random, supplier: Supplier, n_shipments: int, first_known_at: datetime
) -> POPlan:
    """A clean purchase order: lines delivered over ``n_shipments`` shipments, each invoiced."""
    n_lines = weighted_choice(rng, [1, 2, 3, 4], [0.3, 0.35, 0.2, 0.15])
    skus = rng.sample(
        SKUS_BY_CATEGORY[supplier.category], min(n_lines, len(SKUS_BY_CATEGORY[supplier.category]))
    )
    provisional_order_date = first_known_at.date() - timedelta(days=rng.randrange(12, 45))
    lines = [make_line(rng, supplier, sku, provisional_order_date) for sku in skus]
    for line in lines:
        if rng.random() < 0.15 and len(lines) > 1:
            continue  # not delivered yet
        if n_shipments > 1 and rng.random() < 0.35:
            parts = rng.randrange(2, n_shipments + 1)
            idxs = sorted(rng.sample(range(n_shipments), parts))
            for idx, q in zip(idxs, split_qty(rng, line.qty, parts), strict=True):
                line.deliveries[idx] = q
        else:
            line.deliveries[rng.randrange(n_shipments)] = line.qty
    if not any(line.deliveries for line in lines):
        lines[0].deliveries[0] = lines[0].qty
    shipments: list[ShipmentPlan] = []
    known = first_known_at
    for idx in range(n_shipments):
        delivered = [
            (i, line.deliveries[idx]) for i, line in enumerate(lines) if idx in line.deliveries
        ]
        if not delivered:
            continue
        if idx > 0:
            known = known + timedelta(days=rng.randrange(4, 15), hours=rng.randrange(-3, 4))
        fmt = InvoiceFormat.PDF  # reassigned later by assign_formats
        inv = InvoicePlan(
            known_at=known,
            format=fmt,
            billed=[BilledLine(i, q, lines[i].unit_price_minor) for i, q in delivered],
        )
        shipments.append(ShipmentPlan(index=idx, receipt_date=None, invoices=[inv]))
    for new_idx, sh in enumerate(shipments):  # compact indexes after dropping empty shipments
        for line in lines:
            if sh.index in line.deliveries and new_idx != sh.index:
                line.deliveries[new_idx] = line.deliveries.pop(sh.index)
        sh.index = new_idx
    return POPlan(
        supplier=supplier,
        ship_to_plant=pick_plant(rng, supplier),
        lines=lines,
        shipments=shipments,
        buyer=supplier.purchasing_contact,
    )


def build_baseline(
    rng: random.Random, suppliers: list[Supplier], *, n_pos: int, n_invoices: int
) -> list[POPlan]:
    weights = []
    for s in suppliers:
        w = CATEGORY_WEIGHT[s.category]
        if s.country in ("JP", "US") or s.id == "V-0033":
            w *= 0.35
        weights.append(w)
    counts = [weighted_choice(rng, [1, 2, 3], [0.3, 0.4, 0.3]) for _ in range(n_pos)]
    while sum(counts) < n_invoices:
        i = rng.randrange(n_pos)
        if counts[i] < 3:
            counts[i] += 1
    while sum(counts) > n_invoices:
        i = rng.randrange(n_pos)
        if counts[i] > 1:
            counts[i] -= 1
    picks = [weighted_choice(rng, suppliers, weights) for _ in counts]
    by_id = {s.id: s for s in suppliers}
    for sid in ("V-0039", "V-0040"):  # the single JP and US suppliers must show up in the world
        while sum(1 for s in picks if s.id == sid) < 3:
            i = rng.randrange(len(picks))
            if picks[i].id not in ("V-0039", "V-0040"):
                picks[i] = by_id[sid]
    plans: list[POPlan] = []
    for supplier, k in zip(picks, counts, strict=True):
        latest = END - timedelta(days=5 * (k - 1))
        first = sample_known_at(rng, InvoiceFormat.PDF, latest=latest)
        plan = baseline_po(rng, supplier, k, first)
        guard = 0
        while sum(len(sh.invoices) for sh in plan.shipments) != k and guard < 20:
            plan = baseline_po(rng, supplier, k, first)
            guard += 1
        plans.append(plan)
    # make sure the later shipments stay inside the window
    for plan in plans:
        for sh in plan.shipments:
            for inv in sh.invoices:
                if inv.known_at.date() > END:
                    inv.known_at = at(END, 9, rng.randrange(60), rng.randrange(60)) - timedelta(
                        days=rng.randrange(0, 3)
                    )
    return plans


def open_pos(rng: random.Random, suppliers: list[Supplier], n: int) -> list[POPlan]:
    """Orders placed late in the window with nothing delivered yet."""
    out: list[POPlan] = []
    for _ in range(n):
        supplier = rng.choice(
            [s for s in suppliers if s.category not in ("freight", "tooling") and s.id != "V-0033"]
        )
        order_date = date(2026, 10, 25) + timedelta(days=rng.randrange(0, 15))
        skus = rng.sample(SKUS_BY_CATEGORY[supplier.category], rng.randrange(1, 4))
        lines = [make_line(rng, supplier, sku, order_date) for sku in skus]
        out.append(
            POPlan(
                supplier=supplier,
                ship_to_plant=pick_plant(rng, supplier),
                lines=lines,
                shipments=[],
                buyer=supplier.purchasing_contact,
                order_date=order_date,
                tags=["open"],
            )
        )
    return out


# --------------------------------------------------------------------------------------
# Formats: 90 structured / 120 pdf / 30 png / 60 eml
# --------------------------------------------------------------------------------------

SINGLE_PAGE_TEMPLATES = {
    "cz_faktura_classic",
    "cz_faktura_totals_top",
    "pl_faktura_vat",
    "pl_faktura_vat_columns",
    "de_rechnung",
    "jp_seikyusho",
    "us_invoice",
}


def all_invoice_plans(plan: Plan) -> list[tuple[InvoicePlan, Supplier | AdHocSupplier]]:
    out: list[tuple[InvoicePlan, Supplier | AdHocSupplier]] = []
    for po in plan.pos:
        for sh in po.shipments:
            for inv in sh.invoices:
                out.append((inv, po.supplier))
    for st in plan.standalone:
        out.append((st.invoice, st.supplier))
    return out


def assign_formats(
    rng: random.Random,
    plan: Plan,
    *,
    structured: int = 90,
    pdf: int = 120,
    png: int = 30,
    eml: int = 60,
) -> None:
    pairs = all_invoice_plans(plan)
    assert len(pairs) == structured + pdf + png + eml, len(pairs)
    primary: dict[str, InvoiceFormat] = {}
    for _, sup in pairs:
        key = sup.id if isinstance(sup, Supplier) else sup.name
        if key not in primary:
            primary[key] = weighted_choice(
                rng,
                [InvoiceFormat.STRUCTURED, InvoiceFormat.PDF, InvoiceFormat.EML],
                [0.3, 0.5, 0.2],
            )
    flexible: list[tuple[InvoicePlan, Supplier | AdHocSupplier]] = []
    counts = {
        InvoiceFormat.STRUCTURED: 0,
        InvoiceFormat.PDF: 0,
        InvoiceFormat.PNG: 0,
        InvoiceFormat.EML: 0,
    }
    for inv, sup in pairs:
        if inv.format_fixed:
            counts[inv.format] += 1
        else:
            flexible.append((inv, sup))
    for inv, sup in flexible:
        key = sup.id if isinstance(sup, Supplier) else sup.name
        inv.format = (
            primary[key]
            if rng.random() < 0.8
            else weighted_choice(
                rng,
                [InvoiceFormat.STRUCTURED, InvoiceFormat.PDF, InvoiceFormat.EML],
                [0.3, 0.5, 0.2],
            )
        )
        counts[inv.format] += 1
    targets = {
        InvoiceFormat.STRUCTURED: structured,
        InvoiceFormat.PDF: pdf + png,
        InvoiceFormat.EML: eml,
    }
    # rebalance by moving flexible invoices from over-full formats to under-full ones
    rng.shuffle(flexible)
    for fmt_from in (InvoiceFormat.STRUCTURED, InvoiceFormat.PDF, InvoiceFormat.EML):
        for fmt_to in (InvoiceFormat.STRUCTURED, InvoiceFormat.PDF, InvoiceFormat.EML):
            if fmt_from == fmt_to:
                continue
            for inv, _ in flexible:
                if counts[fmt_from] <= targets[fmt_from] or counts[fmt_to] >= targets[fmt_to]:
                    break
                if inv.format == fmt_from and not inv.format_fixed:
                    inv.format = fmt_to
                    counts[fmt_from] -= 1
                    counts[fmt_to] += 1
    assert counts[InvoiceFormat.STRUCTURED] == structured, counts
    assert counts[InvoiceFormat.EML] == eml, counts
    # every rendered template must be used at least once (JP and US suppliers have one supplier each)
    for sup_key in ("V-0039", "V-0040"):
        if not any(
            inv.format != InvoiceFormat.STRUCTURED
            for inv, sup in pairs
            if isinstance(sup, Supplier) and sup.id == sup_key
        ):
            candidates = [
                inv for inv, sup in flexible if isinstance(sup, Supplier) and sup.id == sup_key
            ]
            swap_out = next(
                inv
                for inv, sup in flexible
                if inv.format == InvoiceFormat.PDF
                and isinstance(sup, Supplier)
                and sup.id not in (sup_key,)
                and not inv.format_fixed
            )
            if candidates:
                swap_out.format = candidates[0].format
                candidates[0].format = InvoiceFormat.PDF
    # scans: 30 of the PDFs, single-page templates only, one annotated
    scan_candidates = [
        inv
        for inv, sup in pairs
        if inv.format == InvoiceFormat.PDF
        and not inv.format_fixed
        and isinstance(sup, Supplier)
        and sup.invoice_template_id in SINGLE_PAGE_TEMPLATES
    ]
    scan_candidates.sort(key=lambda inv: inv.known_at)
    chosen = rng.sample(scan_candidates, png)
    for inv in chosen:
        inv.format = InvoiceFormat.PNG
    annotated = next(
        inv
        for inv in sorted(chosen, key=lambda i: i.known_at)
        if isinstance(next(s for i, s in pairs if i is inv), Supplier)
        and next(s for i, s in pairs if i is inv).language == "cs"
    )
    annotated.scan_annotated = True
    final = {f: 0 for f in InvoiceFormat}
    for inv, _ in pairs:
        final[inv.format] += 1
    assert final == {
        InvoiceFormat.STRUCTURED: structured,
        InvoiceFormat.PDF: pdf,
        InvoiceFormat.PNG: png,
        InvoiceFormat.EML: eml,
    }, final
