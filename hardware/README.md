# PayGuard KV260 payment gate

SiliconSign places payment policy and authenticated authorization in FPGA logic.
An untrusted agent can request a payment; it cannot provision a vendor, approve
an invoice, replace its account, waive a cooldown, or invent a valid device MAC.

This directory contains the SiliconSign FPGA controller used by PayGuard and its
low-level serial/MMIO software. The agents run on the laptop and use their APIs
there; the FPGA checks and authenticates payment requests. The existing USB debug
UART carries requests between the laptop and Kria, so Ethernet is unnecessary.
See the repository README for the PayGuard application and controller workflow.

**Do not load or probe the R2 bitstream.** Its implemented auxiliary reset is
active-low (`C_AUX_RESET_HIGH=0`) and tied to zero, holding the design in reset.
R3/R4 overlays cannot repair this hardware wiring. A corrected R5 bitstream is
being rebuilt on the existing VM; completion and physical acceptance are pending.
Preserve the historical images, reports, private keys and journals. See the
[R5 replacement requirements](recovery/R5_REPLACEMENT.md).

## Included

- SHA-256 and HMAC-SHA256 RTL, verified against independent Python test vectors.
- Separate administrative, controller, and device-response keys.
- Sixty-four vendor slots and 256 immutable obligation slots per session; strict
  integer-cent amounts. An obligation may bind an approved payment batch.
- Approved destination accounts and versioned bank changes with hardware-timed holds.
- Immutable invoice bindings, controlled unpaid-invoice rebinding after bank changes,
  one-time execution, request sequences, currency checks, caps and payment-count limits.
- Transaction-specific, short-lived exceptional approval. Cumulative vendor spending
  triggers review, so splitting an amount does not bypass that threshold.
- Internal time and authenticated daily rollover; the agent cannot move the clock.
- AXI4-Lite bridge with atomic request capture and backpressure.
- Python serial/MMIO adapters, durable command journal, conservative restart recovery,
  and a SQLite simulated-bank gateway with atomic, idempotent settlement.
- Double-entry payable recognition and settlement, evidence digests and decision logs.
- Adversarial tests, scripted demonstration, Vivado build and Linux overlay preparation.

Capacity is finite: a paid slot is not recycled, and daily rollover does not clear
obligations. The host must refuse further commitments when all 256 slots are used.
The read-only AXI capability register at offset `0x00c` must return `0x01000040`
for this production build. An older image is not compatible with this capacity.
The actual fabric clock and seconds divider are both 50 MHz. Read-only offset
`0x010` reports the compiled divider (`50000000`); deployment also verifies the live
PL0 clock is 49.5–50 MHz. A 60-second hold remains 60 seconds at the nominal clock.

## Verification

Python 3.10+ and Icarus Verilog 12+ are sufficient; Python code uses the standard library.

```bash
python3 scripts/verify.py
```

The report is `build/verification/results.json`. RTL simulations exercise real
SystemVerilog; Python reference-model checks are reported separately. Hardware
synthesis, timing closure and physical-board behavior require their own results.

## Existing builder

Use the existing Google Cloud VM `starline-builder`, project `typer-491005`, zone
`us-west4-b`. No VM creation or resizing is needed. Work in a separate directory
under `/home/ali_official2808/siliconsign-*`; preserve all Starline projects.
The verified installed environment is:

```bash
source /home/ali_official2808/AMD/2026.1/Vivado/settings64.sh
```

The quarantined R2 **64-vendor / 256-obligation, 50 MHz** image completed synthesis, placement,
routing and bitstream generation on 2026-10-25 at 08:26:29 UTC. Final setup slack is
+0.255 ns, hold slack +0.011 ns and pulse-width slack +8.500 ns; all timing constraints
passed with zero bitstream DRC errors. It uses 42,030 LUTs, 26,085 registers and four
RAMB36 tiles. The completed run is `payguard-kv260-20261025/build-r2` on that VM.

RTL verification also passes: 102 policy checks, 13 authenticated core checks,
351 AXI transactions, 350 capacity/reset checks, 26 sequential-scan checks, 68 timer
checks and 205 independent response/HMAC comparisons. Captured requests use a bounded
block-RAM identity scan. **Physical payment operation remains unverified**. USB
archive transfer and R3/R4 overlay setup reached their documented checkpoints,
but register probes froze the board. The later XSA inspection identified the
persistent auxiliary reset in R2. Historical R3/R4 overlay corrections do not
change that implemented fault; R5 requires a new bitstream and USB transfer.
Exact evidence and source identity are recorded in `docs/BUILD.md`.
Do not load a new FPGA overlay during an active Starline board test.

## Security boundary

The payment-agent process is untrusted. The Kria boot/ARM bridge, administrator,
controller approver and payment gateway are trusted. A controller hosted on the same
laptop needs isolation from the agent process; a UI click alone does not create that
isolation. Each role receives only its own key.
The payment agent must never receive the three-key provisioning bundle. A response
MAC is a shared-secret authorization checked by the trusted gateway, not a public-key
signature or direct bank transfer. The gateway itself remains capable of forging MACs;
its isolation is a security assumption, not a property of the FPGA.

This development-board design does not protect against a privileged Kria user,
JTAG access, malicious bitstream replacement, power-glitch or side-channel attacks.
Keys embedded in a development bitstream are not a certified secure element.
Do not use this prototype with real money or production credentials.

Software approval on the controller screen is not physical button approval. KV260
onboard buttons serve firmware update and reset. No display, button, external LED,
or additional board is assumed in this implementation.

## Recovery

Hardware reserves an approved payment before returning its authorization. A timeout
never releases that reservation. The gateway atomically consumes authorizations and
invoice identities in SQLite; retries return an existing receipt.

In the standalone SiliconSign recovery coordinator, a bridge restart locks payment
serving. Recovery rotates the active session,
preserves reserved spend and payment counts, excludes consumed invoices, and
restarts any bank-change hold conservatively. An unresolved hardware command needs
reconciliation before resuming; it is never silently treated as unpaid. Previously
revoked sessions cannot be reactivated. Persistence uses the existing microSD and
trusted software, not rollback-resistant FPGA nonvolatile memory.
The PayGuard raw bridge also fails closed on restart; use PayGuard's documented
reconciliation workflow. Do not assume the standalone gateway's recovery or atomic
settlement guarantees automatically apply to the application's separate database.

See `docs/PROTOCOL.md` for the exact binary protocol and rules. Test data is synthetic;
any historical-attack example is a reconstruction, not the city's original evidence.
