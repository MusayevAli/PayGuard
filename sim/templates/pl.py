"""Polish templates: *Faktura VAT* with a VAT summary, and a variant with net/VAT/gross columns
per line. NIP, termin płatności, sposób zapłaty: przelew, amounts like ``1 234,56 zł``."""

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
    party_block,
    text,
    totals_box,
)

M = 40


def _title(ctx: RenderContext) -> str:
    return "Faktura korygująca" if ctx.is_credit_note else "Faktura VAT"


def _header(c: Canvas, ctx: RenderContext) -> float:
    w, h = ctx.page_size
    y = h - M
    text(c, M, y - 8, f"{_title(ctx)} nr {ctx.number}", font=FONT_BOLD, size=14)
    hline(c, M, w - M, y - 16, 1)
    lines = ctx.supplier.address_lines
    city = lines[1].split(" ", 1)[-1] if len(lines) > 1 else ""
    rows = [
        ("Data wystawienia:", ctx.invoice_date),
        ("Data sprzedaży:", ctx.service_date),
        ("Miejsce wystawienia:", city),
    ]
    y_meta = labelled_rows(c, w - M - 240, y - 32, rows, label_width=110, size=8.5)
    return y_meta - 8


def _parties(c: Canvas, ctx: RenderContext, y: float) -> float:
    w = ctx.page_size[0]
    ya = party_block(c, M, y, "Sprzedawca", ctx.supplier, width=240)
    yb = party_block(c, w / 2 + 10, y, "Nabywca", ctx.buyer, width=240)
    return min(ya, yb) - 10


def _payment_block(c: Canvas, ctx: RenderContext, y: float) -> float:
    rows = [("Sposób zapłaty:", "przelew"), ("Termin płatności:", ctx.due_date)]
    if ctx.po_ref_text:
        rows.append(("Zamówienie:", ctx.po_ref_text))
    if ctx.credit_note_for:
        rows.append(("Do faktury:", ctx.credit_note_for))
    y = labelled_rows(c, M, y, rows, label_width=100, size=8.5)
    y = labelled_rows(c, M, y, ctx.bank_rows, label_width=100, size=8.5)
    return y


def _footer(c: Canvas, ctx: RenderContext) -> None:
    w = ctx.page_size[0]
    y = 92
    hline(c, M, w - M, y + 10, 0.4)
    for note in ctx.notes:
        y = paragraph(c, M, y, note, size=7.5, width=w - 2 * M)
    text(c, M, 40, "Osoba upoważniona do wystawienia faktury", size=7.5)
    text(c, w - M, 40, "Osoba upoważniona do odbioru faktury", size=7.5, align="right")
    hline(c, M, M + 180, 52, 0.4)
    hline(c, w - M - 180, w - M, 52, 0.4)


def draw_vat(c: Canvas, ctx: RenderContext) -> None:
    w = ctx.page_size[0]
    y = _header(c, ctx)
    y = _parties(c, ctx, y)
    width = w - 2 * M
    cols = [
        Col("Lp", 26, "right"),
        Col("Nazwa towaru / usługi", width - 26 - 60 - 40 - 90 - 60 - 100),
        Col("Ilość", 60, "right"),
        Col("Jm", 40),
        Col("Cena jedn. netto", 90, "right"),
        Col("VAT", 60, "right"),
        Col("Wartość netto", 100, "right"),
    ]
    rows = [
        [
            str(ln.no),
            f"{ln.description} ({ln.sku})",
            ln.qty,
            ln.uom,
            ln.unit_price,
            ln.vat_rate,
            ln.net,
        ]
        for ln in ctx.lines
    ]
    y, _ = draw_table(c, M, y, cols, rows)
    y -= 14
    text(c, M, y, "Podsumowanie według stawek VAT", font=FONT_BOLD, size=8.5)
    y -= 4
    vcols = [
        Col("Stawka", 55),
        Col("Netto", 80, "right"),
        Col("VAT", 70, "right"),
        Col("Brutto", 80, "right"),
    ]
    y_vat, _ = draw_table(
        c,
        M,
        y,
        vcols,
        [[r.rate, r.base, r.vat, r.gross] for r in ctx.vat_rows],
        size=8,
        wrap_col=-1,
    )
    y_tot = totals_box(
        c,
        w - M,
        y - 2,
        [("Razem netto:", ctx.net), ("Razem VAT:", ctx.vat), ("Do zapłaty:", ctx.gross)],
        width=200,
    )
    y = min(y_vat, y_tot) - 16
    y = _payment_block(c, ctx, y)
    if ctx.terms_text:
        y = paragraph(c, M, y - 4, ctx.terms_text, size=8, width=width)
    _footer(c, ctx)
    c.showPage()


def draw_vat_columns(c: Canvas, ctx: RenderContext) -> None:
    w = ctx.page_size[0]
    y = _header(c, ctx)
    y = _parties(c, ctx, y)
    width = w - 2 * M
    cols = [
        Col("Lp", 22, "right"),
        Col("Nazwa", width - 22 - 44 - 30 - 62 - 72 - 40 - 62 - 74),
        Col("Ilość", 44, "right"),
        Col("Jm", 30),
        Col("Cena netto", 62, "right"),
        Col("Wartość netto", 72, "right"),
        Col("VAT %", 40, "right"),
        Col("Kwota VAT", 62, "right"),
        Col("Wartość brutto", 74, "right"),
    ]
    rows = [
        [
            str(ln.no),
            ln.description,
            ln.qty,
            ln.uom,
            ln.unit_price,
            ln.net,
            ln.vat_rate,
            ln.vat,
            ln.gross,
        ]
        for ln in ctx.lines
    ]
    y, _ = draw_table(c, M, y, cols, rows, size=8, header_size=7.5)
    y -= 12
    vcols = [
        Col("Stawka VAT", 60),
        Col("Wartość netto", 78, "right"),
        Col("Kwota VAT", 70, "right"),
        Col("Wartość brutto", 78, "right"),
    ]
    y_vat, _ = draw_table(
        c,
        M,
        y,
        vcols,
        [[r.rate, r.base, r.vat, r.gross] for r in ctx.vat_rows],
        size=8,
        wrap_col=-1,
    )
    y_tot = totals_box(
        c,
        w - M,
        y - 2,
        [("Razem netto:", ctx.net), ("Razem VAT:", ctx.vat), ("Razem brutto:", ctx.gross)],
        width=200,
    )
    y = min(y_vat, y_tot) - 16
    text(c, M, y, "Płatność", font=FONT_BOLD, size=8.5)
    y -= 12
    y = _payment_block(c, ctx, y)
    if ctx.terms_text:
        y = paragraph(c, M, y - 4, ctx.terms_text, size=8, width=width)
    _footer(c, ctx)
    c.showPage()
