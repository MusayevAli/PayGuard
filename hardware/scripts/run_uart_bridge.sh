#!/usr/bin/env bash
# Launch from the EXISTING logged-in KV260 USB UART console:
#   exec sudo bash /absolute/siliconsign/scripts/run_uart_bridge.sh \
#     /secure/siliconsign/keys.json /var/lib/siliconsign /secure/approved-bootstrap.json
# Third argument may instead be --recover after a trusted FPGA reset.
# "exec" is essential: it replaces the login shell, avoiding a shell fallback.
# This script never changes getty configuration and never opens a network port.
set -euo pipefail
umask 077

if [[ $# -ne 3 ]]; then
    printf 'Usage: exec sudo bash %s KEYS_JSON STATE_DIRECTORY CONFIG_JSON_OR_--recover_OR_--external-gateway\n' "$0" >&2
    exit 2
fi
if [[ ${EUID} -ne 0 ]]; then
    printf 'Run as root on the Kria using the documented exec sudo handoff.\n' >&2
    exit 2
fi
# Ignore terminal-disconnect HUP before the bridge is spawned; Python inherits
# this disposition. A normal terminal-emulator handoff must not expose a shell.
trap '' HUP
# A receiver may return the shell with /dev/tty aliases, and sudo may add a PTY.
# Resolve only kernel controlling-TTY identities in our own ancestry. The helper
# cross-checks the real device's major/minor and never guesses a ttyPS number.
project_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd -P)
uart_device=$(python3 "$project_dir/scripts/uart_console.py") || exit 2
exec < "$uart_device" > "$uart_device" 2> "$uart_device"
if [[ ! -t 0 || ! -t 1 ]]; then
    printf 'This launcher requires the existing interactive Kria serial TTY on stdin/stdout.\n' >&2
    exit 2
fi
if [[ ! $uart_device =~ ^/dev/ttyPS[0-9]+$ || ! -c $uart_device || -L $uart_device || $(tty) != "$uart_device" ]]; then
    printf 'Expected an existing Kria hardware UART (/dev/ttyPS plus digits), got %s. No terminal settings changed.\n' "$uart_device" >&2
    exit 2
fi
keys_file=$1
state_dir=$2
bootstrap=$3
if [[ ! -f "$keys_file" ]]; then
    printf 'Private key bundle not found.\n' >&2; exit 2
fi
if [[ "$bootstrap" != --recover && "$bootstrap" != --external-gateway && ! -f "$bootstrap" ]]; then
    printf 'Reviewed bootstrap configuration not found.\n' >&2; exit 2
fi
mkdir -p -m 0700 -- "$state_dir"
if [[ -L "$state_dir" || $(stat -c '%u' -- "$state_dir") -ne 0 ]]; then
    printf 'Durable state directory must be a root-owned directory, not a symlink.\n' >&2; exit 2
fi
if [[ $(( 8#$(stat -c '%a' -- "$state_dir") & 077 )) -ne 0 ]]; then
    printf 'Durable state directory must be private (chmod 700).\n' >&2; exit 2
fi

# Validate board, role-key bundle and timer clock before switching the console.
# The bridge independently checks the MMIO IP ID and KEYVALID flag on open.
PYTHONPATH="$project_dir/python" python3 - "$project_dir" "$keys_file" "$bootstrap" <<'PY'
import importlib.util
from pathlib import Path
import sys
from siliconsign.__main__ import read_keys, read_role_key
spec=importlib.util.spec_from_file_location('siliconsign_deployment', Path(sys.argv[1])/'scripts/deploy_kv260.py')
module=importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
module.require_board(module.inspection())
rate=module.read_clock()
if rate is None or not module.MIN_CLOCK_HZ <= rate <= module.MAX_CLOCK_HZ:
    raise SystemExit('PL0 clock is unknown or unsafe; run deployment verification first.')
read_role_key(sys.argv[2], "response") if sys.argv[3] == "--external-gateway" else read_keys(sys.argv[2])
PY

saved_tty=$(stty -g)
printf '%s\n' "$saved_tty" > "$state_dir/uart-stty.previous"
printf '%s\n' "$uart_device" > "$state_dir/uart-device.previous"
log_file="$state_dir/uart-bridge.log"
printf 'Starting physical FPGA UART bridge at 115200 baud; simulated bank only.\n' >&2
printf 'Disconnect the terminal emulator, then connect the laptop serial client.\n' >&2
printf 'Any bridge failure holds this UART closed; it does not return to a shell.\n' >&2
printf 'Diagnostics/session manifest: %s and %s/session.json\n' "$log_file" "$state_dir" >&2

restore_tty() { stty "$saved_tty" < "$uart_device" 2>/dev/null || true; }
bridge_pid=
stop_bridge() {
    if [[ -n "$bridge_pid" ]]; then
        kill -TERM "$bridge_pid" 2>/dev/null || true
        wait "$bridge_pid" 2>/dev/null || true
    fi
}
trap restore_tty EXIT
trap 'stop_bridge; exit 130' INT
trap 'stop_bridge; exit 143' TERM
stty 115200 raw -echo -ixon -ixoff min 1 time 0

args=(bridge --keys "$keys_file" --state-dir "$state_dir"
      --mmio-device /dev/mem --base 0xA0000000)
if [[ "$bootstrap" == --external-gateway ]]; then
    args=(external-bridge --response-key "$keys_file" --state-dir "$state_dir" --mmio-device /dev/mem --base 0xA0000000)
elif [[ "$bootstrap" == --recover ]]; then
    args+=(--recover)
else
    args+=(--config "$bootstrap")
fi

# Foreground supervision intentionally remains alive after Python exits: a
# failed bridge must never expose a shell to a laptop still sending raw data.
set +e
PYTHONPATH="$project_dir/python" python3 -u -m siliconsign "${args[@]}" <&0 >&1 2>> "$log_file" &
bridge_pid=$!
wait "$bridge_pid"
bridge_status=$?
bridge_pid=
printf 'Bridge stopped with status %s; UART is in fail-closed input sink.\n' "$bridge_status" >> "$log_file"
trap '' INT QUIT HUP
printf '{"error":"BRIDGE_STOPPED","payment_authority":"revoked_or_unavailable","operator_recovery_required":true}\n'
while true; do
    if IFS= read -r -n 2048 discarded_line; then
        printf '{"error":"BRIDGE_STOPPED","operator_recovery_required":true}\n'
    else
        sleep 1
    fi
done
