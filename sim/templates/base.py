"""Shared machinery for the eight invoice templates: fonts, a render context built from the
invoice ground truth, drawing helpers and the dispatcher. PDFs are written with ReportLab's
``invariant`` mode so identical input produces identical bytes."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path

from reportlab.lib.pagesizes import A4, LETTER
from reportlab.lib.utils import simpleSplit
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.pdfgen.canvas import Canvas

from sim.models import DateFormat, Invoice, Language, NumberLocale, TemplateId

FONT_DIR = Path(__file__).resolve().parent / "fonts"
FONT = "DejaVu"
FONT_BOLD = "DejaVu-Bold"
FONT_JP = "NotoJP"
_registered = False


def register_fonts() -> None:
    global _registered
    if _registered:
        return
    pdfmetrics.registerFont(TTFont(FONT, str(FONT_DIR / "DejaVuSans.ttf")))
    pdfmetrics.registerFont(TTFont(FONT_BOLD, str(FONT_DIR / "DejaVuSans-Bold.ttf")))
    pdfmetrics.registerFont(TTFont(FONT_JP, str(FONT_DIR / "NotoSansJP-Regular.ttf")))
    _registered = True


@dataclass(frozen=True)
class Party:
    name: str
    address_lines: tuple[str, ...]
    ids: tuple[tuple[str, str], ...]  # (label, value) e.g. ("DIČ", "CZ12345678")
    contact_lines: tuple[str, ...] = ()


@dataclass(frozen=True)
class LineView:
    no: int
    sku: str
    description: str
    qty: str
    uom: str
    unit_price: str
    net: str
    vat_rate: str
    vat: str
    gross: str


@dataclass(frozen=True)
class VatRow:
    rate: str
    base: str
    vat: str
    gross: str


@dataclass
class RenderContext:
    template_id: TemplateId
    invoice: Invoice
    language: Language
    currency: str
    number_locale: NumberLocale
    date_format: DateFormat
    supplier: Party
    buyer: Party
    lines: list[LineView]
    vat_rows: list[VatRow]
    net: str
    vat: str
    gross: str
    po_ref_text: str
    invoice_date: str
    due_date: str
    service_date: str
    payment_ref: str
    bank_rows: list[tuple[str, str]]
    notes: list[str] = field(default_factory=list)
    terms_text: str = ""
    is_credit_note: bool = False
    credit_note_for: str | None = None
    page_size: tuple[float, float] = A4

    @property
    def number(self) -> str:
        return self.invoice.invoice_number


def wrap(text: str, font: str, size: float, width: float) -> list[str]:
    lines: list[str] = simpleSplit(text, font, size, width)
    return lines or [""]


def text(
    c: Canvas, x: float, y: float, s: str, *, font: str = FONT, size: float = 9, align: str = "left"
) -> None:
    c.setFont(font, size)
    if align == "right":
        c.drawRightString(x, y, s)
    elif align == "center":
        c.drawCentredString(x, y, s)
    else:
        c.drawString(x, y, s)


def paragraph(
    c: Canvas,
    x: float,
    y: float,
    s: str,
    *,
    font: str = FONT,
    size: float = 9,
    width: float = 200,
    leading: float | None = None,
) -> float:
    lead = leading or size * 1.25
    for line in wrap(s, font, size, width):
        text(c, x, y, line, font=font, size=size)
        y -= lead
    return y


def labelled_rows(
    c: Canvas,
    x: float,
    y: float,
    rows: Sequence[tuple[str, str]],
    *,
    label_width: float = 90,
    size: float = 9,
    font: str = FONT,
    bold_values: bool = False,
    leading: float | None = None,
) -> float:
    lead = leading or size * 1.35
    for label, value in rows:
        text(c, x, y, label, font=font, size=size)
        text(c, x + label_width, y, value, font=FONT_BOLD if bold_values else font, size=size)
        y -= lead
    return y


def party_block(
    c: Canvas,
    x: float,
    y: float,
    title: str,
    party: Party,
    *,
    width: float = 230,
    size: float = 9,
    title_size: float = 8,
) -> float:
    text(c, x, y, title, font=FONT_BOLD, size=title_size)
    y -= size * 1.5
    for name_line in wrap(party.name, FONT_BOLD, size + 1, width):
        text(c, x, y, name_line, font=FONT_BOLD, size=size + 1)
        y -= size * 1.4
    for line in party.address_lines:
        text(c, x, y, line, size=size)
        y -= size * 1.3
    for label, value in party.ids:
        text(c, x, y, f"{label}: {value}", size=size)
        y -= size * 1.3
    for line in party.contact_lines:
        text(c, x, y, line, size=size - 1)
        y -= size * 1.25
    return y


@dataclass(frozen=True)
class Col:
    label: str
    width: float
    align: str = "left"


def draw_table(
    c: Canvas,
    x: float,
    y: float,
    cols: Sequence[Col],
    rows: Sequence[Sequence[str]],
    *,
    size: float = 8.5,
    header_size: float = 8,
    header_fill: tuple[float, float, float] | None = (0.92, 0.92, 0.92),
    grid: bool = True,
    wrap_col: int = 1,
    max_rows: int | None = None,
    header_text: tuple[float, float, float] = (0, 0, 0),
) -> tuple[float, int]:
    """Draw a table; returns (y below the table, number of rows drawn)."""
    lead = size * 1.3
    total_w = sum(col.width for col in cols)
    header_h = lead + 4
    if header_fill is not None:
        c.setFillColorRGB(*header_fill)
        c.rect(x, y - header_h, total_w, header_h, stroke=0, fill=1)
    c.setFillColorRGB(*header_text)
    cx = x
    for col in cols:
        pad = 3
        if col.align == "right":
            text(
                c,
                cx + col.width - pad,
                y - header_h + 5,
                col.label,
                font=FONT_BOLD,
                size=header_size,
                align="right",
            )
        else:
            text(c, cx + pad, y - header_h + 5, col.label, font=FONT_BOLD, size=header_size)
        cx += col.width
    c.setFillColorRGB(0, 0, 0)
    if grid:
        c.setLineWidth(0.5)
        c.rect(x, y - header_h, total_w, header_h, stroke=1, fill=0)
    y -= header_h
    drawn = 0
    for row in rows:
        if max_rows is not None and drawn >= max_rows:
            break
        cells = [
            wrap(cell, FONT, size, cols[i].width - 6) if i == wrap_col else [cell]
            for i, cell in enumerate(row)
        ]
        row_h = max(len(cell) for cell in cells) * lead + 4
        cx = x
        for i, col in enumerate(cols):
            ty = y - lead + 1
            for line in cells[i]:
                if col.align == "right":
                    text(c, cx + col.width - 3, ty, line, size=size, align="right")
                else:
                    text(c, cx + 3, ty, line, size=size)
                ty -= lead
            if grid:
                c.rect(cx, y - row_h, col.width, row_h, stroke=1, fill=0)
            cx += col.width
        y -= row_h
        drawn += 1
    return y, drawn


def hline(c: Canvas, x1: float, x2: float, y: float, width: float = 0.6) -> None:
    c.setLineWidth(width)
    c.line(x1, y, x2, y)


def totals_box(
    c: Canvas,
    x_right: float,
    y: float,
    rows: Sequence[tuple[str, str]],
    *,
    width: float = 220,
    size: float = 9,
    bold_last: bool = True,
) -> float:
    lead = size * 1.45
    for i, (label, value) in enumerate(rows):
        bold = bold_last and i == len(rows) - 1
        f = FONT_BOLD if bold else FONT
        sz = size + 2 if bold else size
        text(c, x_right - width, y, label, font=f, size=sz)
        text(c, x_right, y, value, font=f, size=sz, align="right")
        y -= lead if not bold else lead + 2
    return y


def new_canvas(path: Path, page_size: tuple[float, float], title: str, author: str) -> Canvas:
    register_fonts()
    c = Canvas(str(path), pagesize=page_size, invariant=1, pageCompression=1)
    c.setTitle(title)
    c.setAuthor(author)
    c.setSubject("Invoice")
    c.setCreator("PayGuard world generator")
    return c


def render_pdf(ctx: RenderContext, out_path: Path) -> bytes:
    """Render ``ctx`` with the template named by ``ctx.template_id`` and return the PDF bytes."""
    from sim.templates import cz, de, jp, pl, us

    drawers = {
        "cz_faktura_classic": cz.draw_classic,
        "cz_faktura_totals_top": cz.draw_totals_top,
        "pl_faktura_vat": pl.draw_vat,
        "pl_faktura_vat_columns": pl.draw_vat_columns,
        "de_rechnung": de.draw_rechnung,
        "de_rechnung_two_page": de.draw_two_page,
        "jp_seikyusho": jp.draw,
        "us_invoice": us.draw,
    }
    if ctx.template_id == "us_invoice":
        ctx.page_size = LETTER
    out_path.parent.mkdir(parents=True, exist_ok=True)
    c = new_canvas(out_path, ctx.page_size, title=f"{ctx.number}", author=ctx.supplier.name)
    drawers[ctx.template_id](c, ctx)
    c.save()
    return out_path.read_bytes()
