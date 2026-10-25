#!/usr/bin/env bash
# Run from the existing Mac checkout. This never creates infrastructure or resets state.
set -euo pipefail
project_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd -P)
cd -- "$project_dir"
if ! command -v uv >/dev/null 2>&1; then
  printf 'PayGuard requires uv in PATH. Install uv, then rerun this launcher.\n' >&2
  exit 2
fi
exec uv run --frozen python -m scripts.launch "$@"
