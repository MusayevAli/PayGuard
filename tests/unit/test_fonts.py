"""Every non-ASCII character the templates and catalog use must exist in the vendored fonts."""

from __future__ import annotations

from pathlib import Path

from fontTools.ttLib import TTFont

ROOT = Path(__file__).resolve().parents[2]
FONTS = ROOT / "sim" / "templates" / "fonts"


def chars_in(paths: list[Path]) -> set[str]:
    out: set[str] = set()
    for p in paths:
        out.update(ch for ch in p.read_text(encoding="utf-8") if ord(ch) > 127)
    return out


def cmap(path: Path) -> set[int]:
    with TTFont(path) as f:
        return set(f.getBestCmap())


def test_fonts_exist_with_licenses() -> None:
    for name in (
        "DejaVuSans.ttf",
        "DejaVuSans-Bold.ttf",
        "NotoSansJP-Regular.ttf",
        "Caveat-Regular.ttf",
        "LICENSE-DejaVu.txt",
        "OFL-NotoSansJP.txt",
        "OFL-Caveat.txt",
    ):
        assert (FONTS / name).is_file(), name


def test_latin_coverage() -> None:
    sources = list((ROOT / "sim").rglob("*.py"))
    used = chars_in(sources)
    latin = {c for c in used if ord(c) < 0x2E80}
    missing = sorted(c for c in latin if ord(c) not in cmap(FONTS / "DejaVuSans.ttf"))
    assert not missing, missing
    missing_b = sorted(c for c in latin if ord(c) not in cmap(FONTS / "DejaVuSans-Bold.ttf"))
    assert not missing_b, missing_b


def test_cjk_coverage() -> None:
    sources = list((ROOT / "sim").rglob("*.py"))
    used = chars_in(sources)
    cjk = {c for c in used if ord(c) >= 0x2E80}
    assert cjk, "expected Japanese strings in the catalog/templates"
    jp = cmap(FONTS / "NotoSansJP-Regular.ttf")
    missing = sorted(c for c in cjk | {"¥", "円"} if ord(c) not in jp)
    assert not missing, missing
