"""German templates: *Rechnung* (DIN-letter style with USt-IdNr, Zahlungsziel, Leistungsdatum,
``1.234,56 €``) and a two-page variant with a carried-forward subtotal (*Übertrag*)."""

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

M = 50


def _letterhead(c: Canvas, ctx: RenderContext) -> float:
    w, h = ctx.page_size
    y = h - 40
    text(c, w - M, y, ctx.supplier.name, font=FONT_BOLD, size=12, align="right")
    y -= 13
    for line in ctx.supplier.address_lines:
        text(c, w - M, y, line, size=8, align="right")
        y -= 10
    for line in ctx.supplier.contact_lines:
        text(c, w - M, y, line, size=7.5, align="right")
        y -= 9.5
    # sender line + address window (DIN 5008)
    sender = f"{ctx.supplier.name} · {' · '.join(ctx.supplier.address_lines)}"
    text(c, M, h - 150, sender[:95], size=6.5)
    hline(c, M, M + 230, h - 153, 0.3)
    ay = h - 166
    text(c, M, ay, ctx.buyer.name, size=9.5)
    for line in ctx.buyer.address_lines:
        ay -= 12
        text(c, M, ay, line, size=9.5)
    return h - 240


def _meta(c: Canvas, ctx: RenderContext, y: float) -> float:
    w = ctx.page_size[0]
    rows = [
        ("Rechnungsnummer", ctx.number),
        ("Rechnungsdatum", ctx.invoice_date),
        ("Leistungsdatum", ctx.service_date),
        ("Kundennummer", "K-10477"),
        ("Ihre USt-IdNr.", dict(ctx.buyer.ids).get("USt-IdNr.", "")),
    ]
    if ctx.po_ref_text:
        rows.append(("Ihre Bestellung", ctx.po_ref_text))
    if ctx.credit_note_for:
        rows.append(("Zu Rechnung", ctx.credit_note_for))
    return labelled_rows(c, w - M - 230, y + 60, rows, label_width=105, size=8.5)


def _footer(c: Canvas, ctx: RenderContext, page: int, pages: int) -> None:
    w = ctx.page_size[0]
    hline(c, M, w - M, 64, 0.4)
    ids = " · ".join(f"{k}: {v}" for k, v in ctx.supplier.ids)
    bank = " · ".join(f"{k} {v}" for k, v in ctx.bank_rows)
    text(c, M, 52, f"{ctx.supplier.name} · {ids}", size=6.5)
    text(c, M, 42, f"Bankverbindung: {bank}", size=6.5)
    text(c, w - M, 42, f"Seite {page}/{pages}", size=6.5, align="right")


def _cols(width: float) -> list[Col]:
    return [
        Col("Pos.", 30, "right"),
        Col("Artikel-Nr.", 66),
        Col("Bezeichnung", width - 30 - 66 - 56 - 50 - 80 - 90),
        Col("Menge", 56, "right"),
        Col("Einheit", 50),
        Col("Einzelpreis", 80, "right"),
        Col("Gesamtpreis", 90, "right"),
    ]


def _rows(ctx: RenderContext) -> list[list[str]]:
    return [
        [str(ln.no), ln.sku, ln.description, ln.qty, ln.uom, ln.unit_price, ln.net]
        for ln in ctx.lines
    ]


def _totals_rows(ctx: RenderContext) -> list[tuple[str, str]]:
    rows = [("Nettobetrag", ctx.net)]
    for r in ctx.vat_rows:
        rows.append((f"zzgl. {r.rate} USt.", r.vat))
    rows.append(("Gutschriftsbetrag" if ctx.is_credit_note else "Rechnungsbetrag", ctx.gross))
    return rows


def _terms_and_notes(c: Canvas, ctx: RenderContext, y: float) -> float:
    w = ctx.page_size[0]
    if ctx.terms_text:
        y = paragraph(c, M, y, ctx.terms_text, size=8.5, width=w - 2 * M)
    for note in ctx.notes:
        y = paragraph(c, M, y - 2, note, size=8, width=w - 2 * M)
    y = paragraph(
        c,
        M,
        y - 6,
        "Bitte überweisen Sie den Betrag unter Angabe der Rechnungsnummer auf das unten genannte Konto.",
        size=8.5,
        width=w - 2 * M,
    )
    return y


def draw_rechnung(c: Canvas, ctx: RenderContext) -> None:
    w = ctx.page_size[0]
    y = _letterhead(c, ctx)
    meta_bottom = _meta(c, ctx, y)
    title = "Gutschrift" if ctx.is_credit_note else "Rechnung"
    y = min(meta_bottom, y - 24) - 6
    text(c, M, y, f"{title} Nr. {ctx.number}", font=FONT_BOLD, size=13)
    y -= 30
    text(c, M, y + 14, "Sehr geehrte Damen und Herren,", size=9)
    text(c, M, y + 2, "wir erlauben uns, Ihnen folgende Leistungen in Rechnung zu stellen:", size=9)
    y -= 12
    y, _ = draw_table(c, M, y, _cols(w - 2 * M), _rows(ctx), wrap_col=2)
    y = totals_box(c, w - M, y - 14, _totals_rows(ctx), width=240)
    y = _terms_and_notes(c, ctx, y - 10)
    text(c, M, y - 14, "Mit freundlichen Grüßen", size=9)
    text(c, M, y - 26, ctx.supplier.name, size=9)
    _footer(c, ctx, 1, 1)
    c.showPage()


def draw_two_page(c: Canvas, ctx: RenderContext) -> None:
    w = ctx.page_size[0]
    y = _letterhead(c, ctx)
    meta_bottom = _meta(c, ctx, y)
    title = "Gutschrift" if ctx.is_credit_note else "Rechnung"
    y = min(meta_bottom, y - 24) - 6
    text(c, M, y, f"{title} Nr. {ctx.number}", font=FONT_BOLD, size=13)
    y -= 30
    text(c, M, y + 14, "Sehr geehrte Damen und Herren,", size=9)
    text(
        c,
        M,
        y + 2,
        "wir berechnen Ihnen gemäß Ihrer Bestellung wie folgt (Fortsetzung auf Seite 2):",
        size=9,
    )
    y -= 12
    y, _ = draw_table(c, M, y, _cols(w - 2 * M), _rows(ctx), wrap_col=2)
    hline(c, M, w - M, y - 6, 0.5)
    text(c, w - M - 240, y - 20, "Übertrag (Zwischensumme netto)", font=FONT_BOLD, size=9)
    text(c, w - M, y - 20, ctx.net, font=FONT_BOLD, size=9, align="right")
    text(c, M, 80, "– Fortsetzung auf Seite 2 –", size=8, align="left")
    _footer(c, ctx, 1, 2)
    c.showPage()
    # page 2
    h = ctx.page_size[1]
    y = h - 60
    text(c, M, y, f"{title} Nr. {ctx.number} · {ctx.supplier.name}", font=FONT_BOLD, size=10)
    text(c, w - M, y, f"Rechnungsdatum {ctx.invoice_date} · Seite 2/2", size=8.5, align="right")
    hline(c, M, w - M, y - 8, 0.8)
    y -= 30
    text(c, M, y, "Übertrag von Seite 1 (Zwischensumme netto)", font=FONT_BOLD, size=9)
    text(c, w - M, y, ctx.net, font=FONT_BOLD, size=9, align="right")
    y = totals_box(c, w - M, y - 22, _totals_rows(ctx), width=240)
    y = _terms_and_notes(c, ctx, y - 10)
    y -= 10
    text(c, M, y, "Hinweise", font=FONT_BOLD, size=9)
    y = paragraph(
        c,
        M,
        y - 12,
        "Es gelten unsere Allgemeinen Lieferbedingungen. Die Ware bleibt bis zur vollständigen Bezahlung unser Eigentum. Beanstandungen bitten wir innerhalb von 8 Tagen schriftlich mitzuteilen.",
        size=8,
        width=w - 2 * M,
    )
    text(c, M, y - 16, "Mit freundlichen Grüßen", size=9)
    text(c, M, y - 28, ctx.supplier.name, size=9)
    _footer(c, ctx, 2, 2)
    c.showPage()
