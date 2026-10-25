# R5 replacement requirements — build and physical acceptance pending

**The R2 image is quarantined. Do not load or probe it again.** Its implemented
XSA confirms an active-low auxiliary reset (`C_AUX_RESET_HIGH=0`) connected to
constant zero. This keeps the reset controller asserted. The R3/R4 overlay work
does not change that hardware net; a rebuilt bitstream is required. The original
images, archives, reports, keys and journals remain preserved as historical
evidence. Historical R3/R4 instructions are not a current deployment procedure.

R5 is being rebuilt on the existing `starline-builder`, with the same private
role keys. No successful R5 build or physical acceptance is claimed here.

## Minimal replacement package

1. Complete the new build in its separate R5 directory. Verify the implemented
   reset polarity and wiring: inactive auxiliary reset, inactive debug reset,
   asserted clock-lock input and the intended active-low PS reset connection.
   Retain that evidence alongside fresh timing, DRC, utilization, capability and
   source/image hashes. R2's passing reports cannot substitute for these checks.
2. Reuse the existing admin/controller/response keys; do not generate a new key
   bundle. Verify their propagation into the new image without logging values.
   Package the new bitstream, compiled all-four-reset overlay, guarded loader and
   necessary board runtime into a new private board archive. Compile the DTBO on
   the builder so the offline Kria does not need DTC. The board receives only the
   response-role file; admin/controller files remain on the trusted Mac.
3. Use a new install directory, such as `/opt/payguard-kv260-r5`, and a versioned
   firmware basename, such as `siliconsign-r5.bit.bin`, consistently in the loader
   and DTS. Those names are packaging requirements, not an already-installed
   artifact. The existing loader refuses to overwrite a different firmware file;
   keep that protection and preserve `/lib/firmware/siliconsign.bit.bin`.
4. Transfer the **new archive** over the existing USB UART, verify its whole-file
   SHA-256 on the Mac and Kria, extract only into the new directory, then verify
   its manifest. An overlay-only paste installer that copies the old bitstream
   cannot deliver this repair. No Ethernet, new VM or additional hardware is
   required for this transfer.

The ordinary operator packager already supplies the runtime, role separation,
private archive permissions and integrity manifest. Its R5 release path must
add the implemented reset-contract gate and versioned image/install identity;
this document does not claim those packager changes have been implemented.

## Staged physical checks

Use a clean, identified board and deliberately stop the existing application.
Run read-only inspection first. Require full-image manager mode, the expected
registered clock/AFI drivers, valid device-tree symbols and a kernel without an
OOPS. Load the reviewed R5 package **without PL MMIO**, retaining phase output.
Inspect the enabled 50 MHz clock, AFI width and all four resolved reset references.
The result remains **UNVERIFIED** until an explicitly requested existing-image
probe completes; that probe must not reload the image. A child-process timeout
cannot contain an unresponsive AXI transaction.

Preserve the host database and all board bridge directories, including session
manifests and journals. Reprogramming resets volatile FPGA state, so an existing
application session must not be silently resumed or reinitialized. Reconcile any
uncertain command first; keep old records and choose a new session only through
the documented fresh-session procedure. Matching keys do not make old session
sequences valid in a freshly programmed device.

Only after explicit register verification succeeds should the external-gateway
UART handoff and the normal/forged-account/large-payment acceptance checks run.
The bank remains simulated and a successful build alone is not a physical pass.
