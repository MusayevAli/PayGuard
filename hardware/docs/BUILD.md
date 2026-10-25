# Build and deployment record

## Current status: R2 quarantined; corrected R5 build pending

**Do not load or probe R2.** Its implemented XSA has `C_AUX_RESET_HIGH=0`
and `aux_reset_in` connected to constant zero. The auxiliary reset is active-low,
so the reset controller holds the downstream AXI design in reset. A PL register
read can consequently stall the processor. R3/R4 overlays cannot change that net.

R5 is being rebuilt on the existing `starline-builder` with the same private role
keys. It must verify resolved reset properties and actual tie-off nets, then pass
implementation/timing and separate physical checks. Completion is not yet
recorded. A new bitstream and USB transfer are required; see the
[R5 replacement requirements](../recovery/R5_REPLACEMENT.md).

## Historical R2 64/256 implementation results

Vivado 2026.1 completed the now-quarantined R2 build on the existing `starline-builder`
at **2026-10-25 08:26:29 UTC**. The completed run is
`/home/ali_official2808/payguard-kv260-20261025/build-r2`.
The final routed report confirms: **All user specified timing constraints are met.**

Public copies of the timing, pulse-width, utilization, DRC, synthesis hierarchy and
capacity reports are in `reports/current/routed-r2/`. Trailing whitespace is normalized
and any long hexadecimal strings are redacted. Original reports remain with the
private build and deployment package.

|Historical R2 implemented result|Value|
|---|---|
|Worst setup slack|+0.255 ns; TNS 0; zero failing endpoints|
|Worst hold slack|+0.011 ns; THS 0; zero failing endpoints|
|Worst pulse-width slack|+8.500 ns; TPWS 0; zero failing endpoints|
|CLB LUTs|42,030 / 117,120 (35.89%)|
|CLB registers|26,085 / 234,240 (11.14%)|
|Block RAM|4 RAMB36 tiles (2.78%)|
|Bitstream design-rule check|Zero errors|

The image has 64 vendor slots and 256 immutable obligation slots. Its actual PS
fabric-clock request, RTL seconds divider and Linux overlay all use **50 MHz**.
The completed capability manifest matches capacity `0x01000040` at AXI offset
`0x00c` and compiled divider `50000000` at offset `0x010`. Deployment requires the
live PL0 clock to be **49.5–50 MHz**. The 60-second waiting period is unchanged.
The build verified private role-key propagation and retained invoice-hash block RAM;
bitstream generation and overlay packaging completed, including the DTBO.

**Physical payment-gate operation remains unverified, and R2 is quarantined.** The first physical load attempt
hit a stale DMA-mode kernel failure; a fresh-boot retry with full-image flags was
followed by an RCU stall. The overlay-only correction in
[`recovery/r4`](../recovery/r4/README.md) supplies the PS-PL AXI-width and complete
reset setup while retaining the implemented r2 image. The earlier r3 overlay
loaded, but its incorrect single reset116 did not target `pl_resetn0`; the register
probe froze the board. R4 includes all four AMD-generated reset IDs, including119
for EMIO95/`pl_resetn0`, but cannot deassert the independently tied auxiliary
reset inside R2. R3/R4 records remain historical recovery attempts, not a repair
for the implemented fault.
Successful implementation is not evidence of live
clock, reset, UART, MMIO or persistent-journal operation.

The verified synthesis-source manifest is `reports/current/hardware-source-manifest.json`.
Its aggregate SHA-256 is
`08eecdfd5edef1587d17d1ebc6eeef0f0abac6232baa127a08a81307d43ff8e2`.
This identifies the original r2 build inputs; the later overlay packaging
correction is documented separately. Compiled images remain private because they
contain development role keys.

The implementation captures each request and scans invoice identities sequentially
from synchronous block RAM. Reset aborts an in-flight request and clears validity,
paid/approval flags and counters; retained payload/hash RAM cannot authorize anything
before reprovisioning. This replaced the first 64/256 attempt's parallel CAM, whose
100 MHz build failed placement timing (−4.836 ns) and routing (99.54% vertical-wire
usage). Those failed and earlier 16/64 images are not the current implementation.

## Current local verification

Run `python3 scripts/verify.py` from `hardware/`.

|Current check|Result|
|---|---|
|Hardware Python protocol, transport, gateway and recovery|71 tests passed|
|SHA-256/HMAC known vectors, reset and request snapshot|5 vectors passed|
|Hardware policy lifecycle and attacks|102 checks passed|
|Authenticated core with production defaults|13 checks passed|
|AXI including read-only capacity and clock divider|351 transactions passed|
|Full capacity, range limits, replay and stale-payload reset guards|350 checks passed|
|Sequential scan capture, busy isolation, reset and overflow edges|26 checks passed|
|Real 50,000,000-cycle second and 59/60-second hold boundaries|68 checks passed|
|Reference comparison with independent response HMAC verification|35 scenarios, 205 responses passed|

Public results are in `reports/current/`; detailed local logs are in
`build/scan-verification/` and `build/differential/`. The differential scenarios use
explicit 16/64 reference capacity; the production bench fills all 64/256 slots.
`tb_clock` counts every edge of one full production second at a 20 ns period, then
skips only idle divider interiors to test later boundaries. The hold is rejected
at policy second 59 and accepted at second 60. This is a full one-second timebase
simulation plus boundary verification, not a 60-second wall-time simulation.

## Archived 16/64 source checks

The complete test command is `python3 scripts/verify.py`.

|Check|Result|
|---|---|
|Python protocol, roles, settlement, concurrency and recovery|65 tests passed|
|SHA-256/HMAC known vectors and reset behavior|Passed|
|Hardware policy lifecycle and attacks|102 checks passed|
|Authenticated core integration|13 checks passed|
|AXI independent channel ordering and backpressure|344 transactions passed|
|Independent model versus RTL comparison|35 scenarios, 205 full responses passed|
|Independent HMAC verification for those responses|205 passed|
|Synthetic end-to-end demonstration|13/13 scenarios; balanced double-entry books|

Individual logs and machine-readable reports are in `reports/`.
The testbench clocks and public fixture keys are simulation inputs, not deployment keys.

## Existing Vivado host and archived 16/64 build

- VM: `starline-builder`
- Project: `typer-491005`
- Zone: `us-west4-b`
- Source directory: `/home/ali_official2808/siliconsign-20261024`
- Vivado: `/home/ali_official2808/AMD/2026.1/Vivado/settings64.sh`
- FPGA: `xck26-sfvc784-2LV-c`
- Existing board repository:
  `/home/ali_official2808/starline-v4-projection.0AFH6GNU/final-source/vivado/boards`
- Completed run: `build-kv260-r4`; log: `vivado-build-r4.log`

The earlier 16/64 Vivado run finished on 2026-10-24 at 21:22:21 UTC. Synthesis, placement,
routing and bitstream generation completed. The final timing report says:
"All user specified timing constraints are met."

|Implemented result|Value|
|---|---|
|Worst setup slack|+1.537 ns; zero failing endpoints|
|Worst hold slack|+0.012 ns; zero failing endpoints|
|Worst pulse-width slack|+3.500 ns; zero failing endpoints|
|CLB LUTs|24,435 / 117,120 (20.86%)|
|CLB registers|8,352 / 234,240 (3.57%)|
|Block RAM / DSPs|0 / 0|
|Bitstream design-rule check|Zero errors|

That archived output directory contains `siliconsign.bit`, `siliconsign.bit.bin`,
`siliconsign.xsa`, `siliconsign.dts`, `siliconsign.dtbo`, and timing/utilization/DRC
reports. Treat compiled images as private: they contain development role keys.
The source archive contains no deployment key bundle or compiled private images.

No new compute instance was created. The source is separate from all Starline builds.
Three private role keys were generated on the existing VM; they are not included in
the source archive. The private provisioning bundle is `secrets/keys.json` on the VM.

Build the current PayGuard sources using a new output directory on that same VM:

```bash
cd /absolute/PayGuard/hardware
export SILICONSIGN_BOARD_REPO=/home/ali_official2808/starline-v4-projection.0AFH6GNU/final-source/vivado/boards
bash scripts/build_on_starline.sh "$PWD/secrets/keys.json" "$PWD/build-kv260-new"
```

The builder refuses to overwrite an existing Vivado project. It validates the block
design, synthesizes, places and routes, checks setup/hold/pulse-width timing, and
exports the bitstream, XSA and Linux overlay. It does not flash the physical board.

The archived 16/64 run requested 100 MHz and reported 99,999,001 Hz. That clock and
its timing results are historical. The current 64/256 source uses a matched 50 MHz
PS clock, 50,000,000-cycle divider and 50 MHz overlay; live-clock verification remains
mandatory because a faster clock would shorten an enforced waiting period.

## Physical board

The board must be identified and the live device tree/clock checked before loading.
Physical USB transfer and load-only clock/AFI metadata checkpoints have been
observed, while PL register probes froze the board. R2 is quarantined for its
implemented reset fault. Simulation does not establish live reset/AXI behavior,
microSD persistence or payment-bridge operation.

The source includes a synthetic `example-bootstrap.json` and a real MMIO bridge.
The following is the standalone SiliconSign gateway example. PayGuard uses its own
signer and bank integration; follow the repository startup instructions for that path.
After the verified overlay is loaded and device address/clock/capacity are checked:

```bash
sudo env PYTHONPATH=python python3 -m siliconsign bridge \
  --keys /secure/siliconsign/keys.json --state-dir /var/lib/siliconsign \
  --mmio-device /dev/mem --base 0xA0000000 \
  --config example-bootstrap.json
```

The key bundle on the trusted Kria must match the compiled bitstream. Keep it out of
the agent process. The bridge speaks JSON lines over stdin/stdout. The existing USB
debug port normally provides a login console; do not present an ordinary login
console as a raw payment transport. Use the existing SSH session or deliberately
configure the documented UART endpoint. See the repository startup instructions for
approval evidence, role-specific signing, and recovery. The physical-board inspection,
overlay load, and existing USB UART handoff are documented in `docs/DEPLOY_KV260.md`.

The simulated bank always uses fake funds. Live provider checks, bank-account ownership
verification and three LLM agents belong to the surrounding application and are not
fabricated in this FPGA package.
