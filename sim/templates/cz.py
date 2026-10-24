"""Czech templates: *Faktura – daňový doklad* (classic) and a variant with the totals block at
the top. Variabilní symbol, DIČ/IČO, DD.MM.YYYY dates, amounts like ``1 234,56 Kč``."""

from __future__ import annotations

from reportlab.pdfgen.canvas import Canvas

from sim.templates.base import (
    FONT,
    FONT_BOLD,
    Col,
    RenderContext,
    draw_table,
    hline,
    labelled_rows,
    paragraph,
    party_block,
    text,
    totals_box,
)

M = 42  # margin


def _title(ctx: RenderContext) -> str:
    if ctx.is_credit_note:
        return "DOBROPIS – OPRAVNÝ DAŇOVÝ DOKLAD"
    return "FAKTURA – DAŇOVÝ DOKLAD"


def _meta_rows(ctx: RenderContext) -> list[tuple[str, str]]:
    rows = [
        ("Datum vystavení:", ctx.invoice_date),
        ("DUZP:", ctx.service_date),
        ("Datum splatnosti:", ctx.due_date),
        ("Forma úhrady:", "převodem"),
        ("Variabilní symbol:", ctx.payment_ref),
        ("Konstantní symbol:", "0308"),
    ]
    if ctx.po_ref_text:
        rows.append(("Objednávka:", ctx.po_ref_text))
    if ctx.credit_note_for:
        rows.append(("K faktuře č.:", ctx.credit_note_for))
    return rows


def _lines_table(c: Canvas, ctx: RenderContext, y: float, width: float) -> float:
    cols = [
        Col("Označení dodávky", width - 70 - 40 - 85 - 60 - 95),
        Col("Množství", 70, "right"),
        Col("MJ", 40),
        Col("Cena za MJ", 85, "right"),
        Col("DPH", 60, "right"),
        Col("Celkem bez DPH", 95, "right"),
    ]
    rows = [
        [f"{ln.sku}  {ln.description}", ln.qty, ln.uom, ln.unit_price, ln.vat_rate, ln.net]
        for ln in ctx.lines
    ]
    y, _ = draw_table(c, M, y, cols, rows, wrap_col=0)
    return y


def _vat_summary(c: Canvas, ctx: RenderContext, y: float) -> float:
    text(c, M, y, "Rekapitulace DPH", font=FONT_BOLD, size=8.5)
    y -= 4
    cols = [
        Col("Sazba", 55),
        Col("Základ daně", 80, "right"),
        Col("DPH", 70, "right"),
        Col("Celkem", 80, "right"),
    ]
    rows = [[r.rate, r.base, r.vat, r.gross] for r in ctx.vat_rows]
    y, _ = draw_table(c, M, y, cols, rows, size=8, header_fill=None, wrap_col=-1)
    return y


def _footer(c: Canvas, ctx: RenderContext) -> None:
    y = 70
    hline(c, M, ctx.page_size[0] - M, y + 10, 0.4)
    for note in ctx.notes:
        y = paragraph(c, M, y, note, size=7.5, width=ctx.page_size[0] - 2 * M)
    text(
        c,
        M,
        32,
        f"Vystavil: {ctx.supplier.contact_lines[0] if ctx.supplier.contact_lines else ''}",
        size=7.5,
    )
    text(c, ctx.page_size[0] - M, 32, "Razítko a podpis", size=7.5, align="right")


def draw_classic(c: Canvas, ctx: RenderContext) -> None:
    w, h = ctx.page_size
    y = h - M
    text(c, M, y - 6, _title(ctx), font=FONT_BOLD, size=15)
    text(c, w - M, y - 6, f"č. {ctx.number}", font=FONT_BOLD, size=13, align="right")
    hline(c, M, w - M, y - 14, 1.2)
    y -= 34
    ya = party_block(c, M, y, "DODAVATEL", ctx.supplier, width=240)
    yb = party_block(c, w / 2 + 10, y, "ODBĚRATEL", ctx.buyer, width=240)
    y = min(ya, yb) - 10
    hline(c, M, w - M, y + 4, 0.5)
    y -= 8
    ym = labelled_rows(c, M, y, _meta_rows(ctx), label_width=110)
    bank_y = labelled_rows(
        c, w / 2 + 10, y, [("Bankovní spojení:", ""), *ctx.bank_rows], label_width=95
    )
    y = min(ym, bank_y) - 8
    y = _lines_table(c, ctx, y, w - 2 * M) - 14
    y_vat = _vat_summary(c, ctx, y)
    label = "Celkem k úhradě:" if not ctx.is_credit_note else "Celkem k vrácení:"
    y_tot = totals_box(
        c,
        w - M,
        y - 2,
        [("Celkem bez DPH:", ctx.net), ("DPH celkem:", ctx.vat), (label, ctx.gross)],
        width=200,
    )
    y = min(y_vat, y_tot) - 12
    if ctx.terms_text:
        y = paragraph(c, M, y, ctx.terms_text, size=8, width=w - 2 * M)
    _footer(c, ctx)
    c.showPage()


def draw_totals_top(c: Canvas, ctx: RenderContext) -> None:
    w, h = ctx.page_size
    y = h - M
    text(c, M, y - 4, _title(ctx), font=FONT_BOLD, size=14)
    text(c, M, y - 20, f"Číslo dokladu: {ctx.number}", font=FONT, size=10)
    # totals block top-right, framed
    bx, by, bw, bh = w - M - 230, y - 70, 230, 66
    c.setLineWidth(1)
    c.rect(bx, by, bw, bh, stroke=1, fill=0)
    text(
        c,
        bx + 10,
        by + bh - 16,
        "K ÚHRADĚ" if not ctx.is_credit_note else "K VRÁCENÍ",
        font=FONT_BOLD,
        size=9,
    )
    text(c, bx + bw - 10, by + bh - 18, ctx.gross, font=FONT_BOLD, size=16, align="right")
    text(c, bx + 10, by + 22, f"Splatnost: {ctx.due_date}", size=8.5)
    text(c, bx + 10, by + 9, f"VS: {ctx.payment_ref}", size=8.5)
    y -= 90
    ya = party_block(c, M, y, "Dodavatel", ctx.supplier, width=240)
    yb = party_block(c, w / 2 + 10, y, "Odběratel", ctx.buyer, width=240)
    y = min(ya, yb) - 6
    hline(c, M, w - M, y + 2, 0.4)
    y -= 10
    rows = _meta_rows(ctx)
    half = (len(rows) + 1) // 2
    y1 = labelled_rows(c, M, y, rows[:half], label_width=105, size=8.5)
    y2 = labelled_rows(c, w / 2 + 10, y, rows[half:], label_width=105, size=8.5)
    y = min(y1, y2) - 6
    y = _lines_table(c, ctx, y, w - 2 * M) - 12
    y_vat = _vat_summary(c, ctx, y)
    y_tot = totals_box(
        c,
        w - M,
        y - 2,
        [("Základ daně:", ctx.net), ("DPH:", ctx.vat), ("Celkem:", ctx.gross)],
        width=200,
    )
    y = min(y_vat, y_tot) - 14
    text(c, M, y, "Platební údaje", font=FONT_BOLD, size=8.5)
    y -= 12
    y = labelled_rows(c, M, y, ctx.bank_rows, label_width=70, size=8.5)
    if ctx.terms_text:
        y = paragraph(c, M, y - 4, ctx.terms_text, size=8, width=w - 2 * M)
    _footer(c, ctx)
    c.showPage()
