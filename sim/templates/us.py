"""US-style template on Letter paper: INVOICE, MM/DD/YYYY, ``$1,234.56``, Bill To / Ship To,
terms and a *Remit to* block."""

from __future__ import annotations

from reportlab.pdfgen.canvas import Canvas

from sim.templates.base import (
    FONT_BOLD,
    Col,
    RenderContext,
    draw_table,
    hline,
    labelled_rows,
    paragraph,
    text,
    totals_box,
)

M = 48


def draw(c: Canvas, ctx: RenderContext) -> None:
    w, h = ctx.page_size
    y = h - M
    text(c, M, y - 10, ctx.supplier.name, font=FONT_BOLD, size=14)
    ly = y - 24
    for line in ctx.supplier.address_lines + ctx.supplier.contact_lines:
        text(c, M, ly, line, size=8)
        ly -= 10
    text(
        c,
        w - M,
        y - 14,
        "CREDIT MEMO" if ctx.is_credit_note else "INVOICE",
        font=FONT_BOLD,
        size=22,
        align="right",
    )
    rows = [
        ("Invoice #", ctx.number),
        ("Date", ctx.invoice_date),
        ("Due Date", ctx.due_date),
        ("Terms", ctx.terms_text or "Net 30"),
    ]
    if ctx.po_ref_text:
        rows.append(("P.O. Number", ctx.po_ref_text))
    if ctx.credit_note_for:
        rows.append(("Applies to", ctx.credit_note_for))
    ry = labelled_rows(c, w - M - 200, y - 44, rows, label_width=80, size=9)
    y = min(ly, ry) - 16
    hline(c, M, w - M, y + 6, 1)
    text(c, M, y - 8, "BILL TO", font=FONT_BOLD, size=8.5)
    text(c, w / 2 + 10, y - 8, "SHIP TO", font=FONT_BOLD, size=8.5)
    by = y - 22
    text(c, M, by, ctx.buyer.name, size=9)
    text(c, w / 2 + 10, by, ctx.buyer.name, size=9)
    for line in ctx.buyer.address_lines:
        by -= 11
        text(c, M, by, line, size=9)
        text(c, w / 2 + 10, by, line, size=9)
    for label, value in ctx.buyer.ids:
        by -= 11
        text(c, M, by, f"{label} {value}", size=8.5)
    y = by - 20
    width = w - 2 * M
    cols = [
        Col("Item", 70),
        Col("Description", width - 70 - 50 - 50 - 90 - 100),
        Col("Qty", 50, "right"),
        Col("Unit", 50),
        Col("Unit Price", 90, "right"),
        Col("Amount", 100, "right"),
    ]
    rows2 = [[ln.sku, ln.description, ln.qty, ln.uom, ln.unit_price, ln.net] for ln in ctx.lines]
    y, _ = draw_table(c, M, y, cols, rows2, header_fill=(0.2, 0.2, 0.2), header_text=(1, 1, 1))
    y = totals_box(
        c,
        w - M,
        y - 14,
        [
            ("Subtotal", ctx.net),
            (f"Sales Tax ({ctx.vat_rows[0].rate if ctx.vat_rows else '0%'})", ctx.vat),
            ("Total Due" if not ctx.is_credit_note else "Credit Total", ctx.gross),
        ],
        width=200,
    )
    y -= 10
    text(c, M, y, "REMIT TO", font=FONT_BOLD, size=8.5)
    y -= 13
    text(c, M, y, ctx.supplier.name, size=8.5)
    y -= 11
    y = labelled_rows(c, M, y, ctx.bank_rows, label_width=95, size=8.5)
    for note in ctx.notes:
        y = paragraph(c, M, y - 4, note, size=8, width=width)
    text(c, w / 2, 48, "Thank you for your business.", size=9, align="center")
    c.showPage()
