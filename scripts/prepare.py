"""Prepare host files, then run the existing launcher's local configuration checks.

This does not open the serial device, create a payment database, start a server,
initialize/provision the FPGA, or make a model API request. World preparation may
fetch the official ECB exchange-rate archive using the launcher's existing guard.
"""

# ruff: noqa: T201 -- operator-facing progress; credential values are never printed.
from __future__ import annotations

import sys

from scripts.launch import SetupError, configure, ensure_world, parser


def main(argv: list[str] | None = None) -> int:
    command = parser()
    command.description = __doc__
    args = command.parse_args(argv)
    if args.initialize or args.provision or args.open:
        print(
            "PayGuard preparation does not accept --initialize, --provision or --open. "
            "Use scripts/start_mac.sh separately after checking the intended mode and device session.",
            file=sys.stderr,
        )
        return 2
    args.check = True
    try:
        world = args.world.expanduser().resolve()
        print("[2/3] Preparing or reusing the seeded invoice world...", flush=True)
        ensure_world(world, fx_cache=args.fx_cache.expanduser().resolve(), generate=True)
        print("[3/3] Checking local configuration only; USB will not be opened...", flush=True)
        db, token_path = configure(args)
    except (SetupError, OSError) as exc:
        print(f"PayGuard preparation stopped: {exc}", file=sys.stderr)
        return 2
    print(f"Host preparation complete. Selected signer: {args.mode}; bank: simulated.")
    print(f"Prepared world: {world}")
    print(f"Payment database selected but not opened or created: {db}")
    if token_path is not None:
        print(f"Controller credential stored privately at: {token_path}")
    print(
        "No USB connection, FPGA initialization/provisioning, model API call or server startup occurred. "
        "This is not a hardware acceptance result."
    )
    print("Start the chosen mode separately with scripts/start_mac.sh when ready.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
