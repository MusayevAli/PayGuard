"""Build the vendored font subsets in sim/templates/fonts from the upstream font files.

Upstream sources (all free licenses, copied next to the subsets):
  DejaVu 2.37  https://github.com/dejavu-fonts/dejavu-fonts/releases/download/version_2_37/dejavu-fonts-ttf-2.37.zip
  Noto Sans JP https://github.com/google/fonts/raw/main/ofl/notosansjp/NotoSansJP%5Bwght%5D.ttf (+ OFL.txt)
  Caveat       https://github.com/google/fonts/raw/main/ofl/caveat/Caveat%5Bwght%5D.ttf (+ OFL.txt)

Usage: uv run python scripts/prepare_fonts.py --src <dir with the downloaded files>
The Japanese subset keeps kana, CJK punctuation, full-width forms and every non-ASCII
character found in sim/**/*.py, so re-run this after adding Japanese strings.
"""

from __future__ import annotations

import argparse
import shutil
from pathlib import Path

from fontTools import subset
from fontTools.ttLib import TTFont
from fontTools.varLib import instancer

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "sim" / "templates" / "fonts"

LATIN_RANGES = [(0x0020, 0x024F), (0x02B0, 0x036F), (0x2000, 0x206F), (0x20A0, 0x20CF),
                (0x2100, 0x214F), (0x2190, 0x21FF), (0x2200, 0x22FF), (0x25A0, 0x25FF), (0x2600, 0x26FF)]  # fmt: skip
JP_RANGES = [(0x0020, 0x00FF), (0x2000, 0x206F), (0x20A0, 0x20CF), (0x3000, 0x30FF), (0xFF00, 0xFFEF)]


def unicodes(ranges: list[tuple[int, int]]) -> set[int]:
    out: set[int] = set()
    for lo, hi in ranges:
        out.update(range(lo, hi + 1))
    return out


def used_non_ascii() -> set[int]:
    chars: set[int] = set()
    for p in (ROOT / "sim").rglob("*.py"):
        chars.update(ord(c) for c in p.read_text(encoding="utf-8") if ord(c) > 127)
    return chars


def write_subset(src: Path, dst: Path, codepoints: set[int], *, static_weight: float | None = None) -> None:
    font = TTFont(src)
    if static_weight is not None and "fvar" in font:
        font = instancer.instantiateVariableFont(font, {"wght": static_weight}, inplace=False)
    options = subset.Options()
    options.layout_features = ["*"]
    options.name_IDs = ["*"]
    options.notdef_outline = True
    options.recalc_bounds = True
    options.recalc_timestamp = False
    options.drop_tables += ["DSIG"]
    options.ignore_missing_unicodes = True
    sub = subset.Subsetter(options=options)
    sub.populate(unicodes=sorted(codepoints))
    sub.subset(font)
    dst.parent.mkdir(parents=True, exist_ok=True)
    font.save(dst)
    print(f"{dst.name}: {dst.stat().st_size // 1024} KiB, {len(font.getBestCmap())} glyphs mapped")  # noqa: T201


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--src", required=True, type=Path)
    args = ap.parse_args()
    src: Path = args.src
    dejavu = src / "dejavu-fonts-ttf-2.37"
    latin = unicodes(LATIN_RANGES)
    write_subset(dejavu / "ttf" / "DejaVuSans.ttf", OUT / "DejaVuSans.ttf", latin)
    write_subset(dejavu / "ttf" / "DejaVuSans-Bold.ttf", OUT / "DejaVuSans-Bold.ttf", latin)
    shutil.copyfile(dejavu / "LICENSE", OUT / "LICENSE-DejaVu.txt")
    jp = unicodes(JP_RANGES) | used_non_ascii()
    write_subset(src / "NotoSansJP.ttf", OUT / "NotoSansJP-Regular.ttf", jp, static_weight=400)
    shutil.copyfile(src / "NotoSansJP-OFL.txt", OUT / "OFL-NotoSansJP.txt")
    write_subset(src / "Caveat.ttf", OUT / "Caveat-Regular.ttf", unicodes([(0x0020, 0x024F), (0x2010, 0x2027)]),
                 static_weight=400)  # fmt: skip
    shutil.copyfile(src / "Caveat-OFL.txt", OUT / "OFL-Caveat.txt")


if __name__ == "__main__":
    main()
