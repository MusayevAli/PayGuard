"""Load an invoice document for extraction: structured JSON stays data; PDFs are rendered to
150 dpi page images; PNG scans are sent as images; .eml bodies are text plus their attachments."""

from __future__ import annotations

import base64
import email
import email.policy
import hashlib
import io
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pypdfium2 as pdfium
import structlog
from PIL import Image

from .parsed import LocaleHints

log = structlog.get_logger("documents")

RENDER_DPI = 150
MAX_PAGES = 3
MAX_EDGE_PX = 1600


@dataclass(frozen=True)
class ContentPart:
    kind: str  # "text" | "image"
    text: str = ""
    media_type: str = ""
    data_b64: str = ""


@dataclass
class Document:
    invoice_id: str
    format: str
    path: Path
    sha256: str
    hints: LocaleHints
    parts: list[ContentPart] = field(default_factory=list)
    structured: dict[str, Any] | None = None
    pages: int = 0
    text_preview: str = ""


def _png_part(png_bytes: bytes) -> ContentPart:
    return ContentPart(
        kind="image",
        media_type="image/png",
        data_b64=base64.standard_b64encode(png_bytes).decode("ascii"),
    )


def _shrink(png_bytes: bytes) -> bytes:
    with Image.open(io.BytesIO(png_bytes)) as im:
        w, h = im.size
        if max(w, h) <= MAX_EDGE_PX:
            return png_bytes
        scale = MAX_EDGE_PX / max(w, h)
        out = im.convert("L") if im.mode in ("1", "L", "I;16") else im.convert("RGB")
        out = out.resize((max(1, int(w * scale)), max(1, int(h * scale))), Image.LANCZOS)
        buf = io.BytesIO()
        out.save(buf, format="PNG", compress_level=6)
        return buf.getvalue()


def render_pdf_pages(
    pdf_bytes: bytes, dpi: int = RENDER_DPI, max_pages: int = MAX_PAGES
) -> list[bytes]:
    """PNG pages at ``dpi``; a file PDFium cannot open yields no pages (the agent then holds it as R0)."""
    try:
        doc = pdfium.PdfDocument(pdf_bytes)
    except pdfium.PdfiumError as exc:
        log.warning(
            "pdf_unreadable",
            error=str(exc),
            size=len(pdf_bytes),
            event_id=None,
            principal="invoice_agent",
            rule_id="R0",
        )
        return []
    out: list[bytes] = []
    try:
        for n in range(min(len(doc), max_pages)):
            image = doc[n].render(scale=dpi / 72).to_pil()
            buf = io.BytesIO()
            image.save(buf, format="PNG", compress_level=6)
            out.append(_shrink(buf.getvalue()))
    finally:
        doc.close()
    return out


def parse_eml(raw: bytes) -> tuple[str, list[tuple[str, str, bytes]]]:
    msg = email.message_from_bytes(raw, policy=email.policy.default)
    headers = [
        f"{h}: {msg.get(h, '')}"
        for h in ("From", "Reply-To", "To", "Subject", "Date", "Message-ID", "In-Reply-To")
    ]
    body = msg.get_body(preferencelist=("plain", "html"))
    text = body.get_content() if body is not None else ""
    attachments: list[tuple[str, str, bytes]] = []
    for part in msg.iter_attachments():
        payload = part.get_payload(decode=True)
        if payload:
            attachments.append(
                (part.get_filename() or "attachment", part.get_content_type(), payload)
            )
    return "\n".join(headers) + "\n\n" + str(text), attachments


def load_document(world_dir: Path, payload: dict[str, Any], hints: LocaleHints) -> Document:
    path = world_dir / payload["document_path"]
    raw = path.read_bytes()
    doc = Document(
        invoice_id=payload["invoice_id"],
        format=payload["format"],
        path=path,
        sha256=hashlib.sha256(raw).hexdigest(),
        hints=hints,
    )
    fmt = payload["format"]
    if fmt == "structured":
        doc.structured = json.loads(raw.decode("utf-8"))
        text = json.dumps(doc.structured, ensure_ascii=False, indent=2)
        doc.parts = [ContentPart(kind="text", text=text)]
        doc.text_preview = text[:4000]
    elif fmt == "pdf":
        pages = render_pdf_pages(raw) if raw.startswith(b"%PDF") else []
        doc.pages = len(pages)
        doc.parts = [_png_part(p) for p in pages] or [
            ContentPart(kind="text", text=raw[:4000].decode("latin-1", "replace"))
        ]
    elif fmt == "png":
        doc.pages = 1
        doc.parts = [_png_part(_shrink(raw))]
    else:
        text, attachments = parse_eml(raw)
        doc.parts = [ContentPart(kind="text", text=text[:12000])]
        doc.text_preview = text[:4000]
        for name, ctype, data in attachments:
            if ctype == "application/pdf" or name.lower().endswith(".pdf"):
                for png in render_pdf_pages(data):
                    doc.parts.append(_png_part(png))
                    doc.pages += 1
            elif ctype.startswith("image/"):
                doc.parts.append(_png_part(_shrink(data)))
                doc.pages += 1
    return doc


def judge_document(invoice_id: str, form: dict[str, Any], hints: LocaleHints) -> Document:
    text = json.dumps(form, ensure_ascii=False, indent=2)
    return Document(
        invoice_id=invoice_id,
        format="structured",
        path=Path(f"judge/{invoice_id}.json"),
        sha256=hashlib.sha256(text.encode()).hexdigest(),
        hints=hints,
        parts=[ContentPart(kind="text", text=text)],
        structured=form,
        text_preview=text[:4000],
    )
