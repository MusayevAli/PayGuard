#!/usr/bin/env bash
# Prepare the existing host checkout without connecting to or initializing the FPGA.
set -euo pipefail
project_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd -P)
cd -- "$project_dir"
if ! command -v uv >/dev/null 2>&1; then
  printf 'PayGuard preparation requires uv in PATH. Install uv, then rerun this command.\n' >&2
  exit 2
fi
printf '[1/3] Installing dependencies from the existing lockfile...\n'
uv sync --frozen
exec uv run --frozen python -m scripts.prepare "$@"
