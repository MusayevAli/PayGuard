"""Regenerate ``tests/vectors.json`` from the fixed stub scenario (``signer/vectors.py``)."""

from __future__ import annotations

import json
import sys
from pathlib import Path

from signer.vectors import generate

OUT = Path(__file__).resolve().parents[1] / "tests" / "vectors.json"


def main() -> int:
    data = generate()
    OUT.write_text(json.dumps(data, indent=1) + "\n", encoding="utf-8")
    sys.stdout.write(f"{OUT}: {len(data['cases'])} cases\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
