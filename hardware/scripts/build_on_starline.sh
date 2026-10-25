#!/usr/bin/env bash
set -euo pipefail
project_root="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
if [[ "$(hostname -s)" != starline-builder ]]; then
  echo 'Run this on the existing starline-builder VM; no instance is created.' >&2
  exit 1
fi
key_file="${1:?Usage: build_on_starline.sh /absolute/private/keys.json /absolute/new/output}"
output_dir="${2:?Supply a new build output directory}"
source /home/ali_official2808/AMD/2026.1/Vivado/settings64.sh
export SILICONSIGN_BUILD_JOBS="${SILICONSIGN_BUILD_JOBS:-8}"
cd "$project_root"
exec vivado -mode batch -notrace -source vivado/build_kv260.tcl -tclargs "$key_file" "$output_dir"
