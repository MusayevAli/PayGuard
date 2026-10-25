"""Build the small in-place Mac dashboard update, without credentials or world files."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile, ZipInfo

ROOT = Path(__file__).resolve().parents[1]
FILES = (
    "ui/index.html",
    "ui/app.js",
    "ui/style.css",
    "ui/mark.svg",
    "api/dataset.py",
    "api/main.py",
    "api/dashboard.py",
    "api/authorization.py",
    "api/demo.py",
    "api/judge.py",
    "api/live_jobs.py",
    "agents/runner.py",
    "signer/hardware.py",
    "hardware/python/siliconsign/transport.py",
    "sim/clock.py",
    "core/bootstrap.py",
    "core/events.py",
)


def build(output: Path) -> None:
    sources = {name: (ROOT / name).read_bytes() for name in FILES}
    installer = (ROOT / "scripts/apply_demo_update.py").read_bytes()
    compile(installer, "__main__.py", "exec")
    for name, data in sources.items():
        if name.endswith(".py"):
            compile(data, name, "exec")
    manifest = {
        "schema": "payguard.demo-update.v1",
        "files": {
            name: {"bytes": len(data), "sha256": hashlib.sha256(data).hexdigest()}
            for name, data in sources.items()
        },
    }
    entries = {
        "__main__.py": installer,
        "manifest.json": json.dumps(manifest, sort_keys=True, indent=2).encode() + b"\n",
        **{f"payload/{name}": data for name, data in sources.items()},
    }
    output.parent.mkdir(parents=True, exist_ok=True)
    with ZipFile(output, "x", compression=ZIP_DEFLATED) as archive:
        for name, data in sorted(entries.items()):
            info = ZipInfo(name, date_time=(2026, 10, 25, 0, 0, 0))
            info.compress_type = ZIP_DEFLATED
            info.external_attr = 0o100644 << 16
            archive.writestr(info, data)
    print(f"Created {output} ({output.stat().st_size:,} bytes)")  # noqa: T201
    print(f"SHA256 {hashlib.sha256(output.read_bytes()).hexdigest()}")  # noqa: T201


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    build(args.output)


if __name__ == "__main__":
    main()
