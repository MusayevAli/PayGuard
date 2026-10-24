"""Scanned-paper simulation: rasterize a PDF page at 200 dpi, rotate 0.5–2°, add Gaussian
noise and slight blur; optionally a handwritten-style annotation and a received stamp."""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pypdfium2 as pdfium
from PIL import Image, ImageDraw, ImageFilter, ImageFont

from sim.templates.base import FONT_DIR

DPI = 200


def rasterize_scan(
    pdf_bytes: bytes,
    out_path: Path,
    seed: int,
    *,
    annotated: bool = False,
    annotation: str = "OK – JW",
    stamp_date: str = "15 OCT 2026",
) -> None:
    rng = np.random.default_rng(seed)
    doc = pdfium.PdfDocument(pdf_bytes)
    page = doc[0]
    img = page.render(scale=DPI / 72, grayscale=True).to_pil().convert("L")
    doc.close()
    angle = float(rng.uniform(0.5, 2.0)) * (1 if rng.random() < 0.5 else -1)
    img = img.rotate(angle, resample=Image.BICUBIC, expand=False, fillcolor=255)
    # paper tone + Gaussian noise + slight blur
    arr = np.asarray(img, dtype=np.float32) * float(rng.uniform(0.93, 0.99))
    sigma = float(rng.uniform(6.0, 13.0))
    arr = arr + rng.normal(0.0, sigma, arr.shape).astype(np.float32)
    arr = np.clip(arr, 0, 255).astype(np.uint8)
    img = Image.fromarray(arr, mode="L").filter(
        ImageFilter.GaussianBlur(radius=float(rng.uniform(0.5, 1.1)))
    )
    if annotated:
        img = _annotate(img, annotation, stamp_date, rng)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    img.save(out_path, format="PNG", compress_level=1)


def _annotate(
    img: Image.Image, annotation: str, stamp_date: str, rng: np.random.Generator
) -> Image.Image:
    rgb = img.convert("RGB")
    w, h = rgb.size
    hand = ImageFont.truetype(str(FONT_DIR / "Caveat-Regular.ttf"), 96)
    note = Image.new("RGBA", (700, 180), (0, 0, 0, 0))
    ImageDraw.Draw(note).text((20, 20), annotation, font=hand, fill=(28, 58, 160, 225))
    note = note.rotate(-7, resample=Image.BICUBIC, expand=True)
    rgb.paste(note, (int(w * 0.55), int(h * 0.62)), note)
    bold = ImageFont.truetype(str(FONT_DIR / "DejaVuSans-Bold.ttf"), 34)
    small = ImageFont.truetype(str(FONT_DIR / "DejaVuSans.ttf"), 24)
    stamp = Image.new("RGBA", (620, 190), (0, 0, 0, 0))
    d = ImageDraw.Draw(stamp)
    red = (190, 30, 30, 170)
    d.rounded_rectangle((6, 6, 613, 183), radius=18, outline=red, width=6)
    d.text((40, 28), "RECEIVED", font=bold, fill=red)
    d.text((40, 84), stamp_date, font=bold, fill=red)
    d.text((40, 134), "TOYOTA BOSHOKU EUROPE · AP", font=small, fill=red)
    stamp = stamp.rotate(float(rng.uniform(6, 12)), resample=Image.BICUBIC, expand=True)
    rgb.paste(stamp, (int(w * 0.08), int(h * 0.74)), stamp)
    return rgb
