"""Japanese template: 請求書 with English subtitles, JPY without decimals, 登録番号 (T + 13
digits), a 振込先 bank block and 御中 addressing."""

from __future__ import annotations

from reportlab.pdfgen.canvas import Canvas

from sim.templates.base import FONT_JP, Col, RenderContext, hline, text

M = 44


def _t(c: Canvas, x: float, y: float, s: str, size: float = 9, align: str = "left") -> None:
    text(c, x, y, s, font=FONT_JP, size=size, align=align)


def draw(c: Canvas, ctx: RenderContext) -> None:
    w, h = ctx.page_size
    y = h - M
    _t(c, w / 2, y - 10, "請 求 書", 20, "center")
    _t(c, w / 2, y - 26, "INVOICE", 9, "center")
    hline(c, M, w - M, y - 34, 1)
    y -= 56
    # right column: invoice meta, then the issuer block
    rows = [
        ("請求書番号 / Invoice No.", ctx.number),
        ("発行日 / Issue date", ctx.invoice_date),
        ("支払期限 / Due date", ctx.due_date),
        ("登録番号 / Registration No.", dict(ctx.supplier.ids).get("登録番号", "")),
    ]
    ry = y
    for label, value in rows:
        _t(c, w - M - 250, ry, label, 8)
        _t(c, w - M, ry, value, 9, "right")
        ry -= 13
    sy = ry - 10
    _t(c, w - M - 250, sy, ctx.supplier.name, 10)
    sy -= 12
    for line in ctx.supplier.address_lines + ctx.supplier.contact_lines:
        _t(c, w - M - 250, sy, line, 7.5)
        sy -= 10
    # left column: customer
    _t(c, M, y, f"{ctx.buyer.name} 御中", 11)
    by = y - 14
    for line in ctx.buyer.address_lines:
        _t(c, M, by, line, 8)
        by -= 11
    y = min(by, sy) - 18
    _t(c, M, y, "下記の通りご請求申し上げます。 / We hereby invoice you as follows.", 8.5)
    y -= 24
    # total box
    _t(c, M, y, "合計金額 / Total amount", 9)
    c.setLineWidth(0.8)
    c.rect(M + 130, y - 8, 180, 24, stroke=1, fill=0)
    _t(c, M + 130 + 172, y - 1, ctx.gross, 14, "right")
    y -= 34
    width = w - 2 * M
    cols = [
        Col("品番 / Part No.", 80),
        Col("品名 / Description", width - 80 - 60 - 44 - 90 - 100),
        Col("数量 / Qty", 60, "right"),
        Col("単位 / Unit", 44),
        Col("単価 / Unit price", 90, "right"),
        Col("金額 / Amount", 100, "right"),
    ]
    rows2 = [[ln.sku, ln.description, ln.qty, ln.uom, ln.unit_price, ln.net] for ln in ctx.lines]
    # draw_table uses the Latin font for cells; switch the header/body font to Noto for Japanese
    c.setFont(FONT_JP, 8)
    y = _table(c, M, y, cols, rows2)
    y -= 16
    for label, value in [
        ("小計 / Subtotal", ctx.net),
        (f"消費税 / Consumption tax ({ctx.vat_rows[0].rate if ctx.vat_rows else '0%'})", ctx.vat),
        ("合計 / Total", ctx.gross),
    ]:
        _t(c, w - M - 230, y, label, 9)
        _t(c, w - M, y, value, 10, "right")
        y -= 14
    y -= 8
    _t(c, M, y, "振込先 / Bank account", 9.5)
    y -= 13
    for label, value in ctx.bank_rows:
        _t(c, M, y, f"{label}  {value}", 8.5)
        y -= 11.5
    y -= 6
    _t(c, M, y, "備考 / Remarks", 9.5)
    y -= 13
    remarks = [
        f"注文番号 / PO No.: {ctx.po_ref_text}" if ctx.po_ref_text else "注文番号 / PO No.: —"
    ]
    remarks.extend(ctx.notes)
    if ctx.terms_text:
        remarks.append(ctx.terms_text)
    for r in remarks:
        _t(c, M, y, r, 8)
        y -= 11
    _t(
        c,
        w - M,
        40,
        "振込手数料は貴社にてご負担ください。 / Bank charges to be borne by the payer.",
        7.5,
        "right",
    )
    c.showPage()


def _table(c: Canvas, x: float, y: float, cols: list[Col], rows: list[list[str]]) -> float:
    size = 8.5
    lead = size * 1.35
    total_w = sum(col.width for col in cols)
    header_h = lead + 4
    c.setFillColorRGB(0.93, 0.93, 0.93)
    c.rect(x, y - header_h, total_w, header_h, stroke=0, fill=1)
    c.setFillColorRGB(0, 0, 0)
    cx = x
    for col in cols:
        if col.align == "right":
            _t(c, cx + col.width - 3, y - header_h + 5, col.label, 7.5, "right")
        else:
            _t(c, cx + 3, y - header_h + 5, col.label, 7.5)
        cx += col.width
    c.setLineWidth(0.5)
    c.rect(x, y - header_h, total_w, header_h, stroke=1, fill=0)
    y -= header_h
    for row in rows:
        row_h = lead + 4
        cx = x
        for i, col in enumerate(cols):
            if col.align == "right":
                _t(c, cx + col.width - 3, y - lead + 1, row[i], size, "right")
            else:
                _t(c, cx + 3, y - lead + 1, row[i], size)
            c.rect(cx, y - row_h, col.width, row_h, stroke=1, fill=0)
            cx += col.width
        y -= row_h
    return y
