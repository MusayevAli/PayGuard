# KV260 r3 Linux overlay correction

This is an overlay-only correction for the existing, private r2 FPGA image. It
does not rebuild or change that image, its keys, the 64-vendor/256-obligation
policy engine, or the 50 MHz clock and seconds divider. Physical loading and
payment tests have **not** succeeded yet. The original overlay omitted the
runtime PS-PL AXI-width and reset setup; the latest corrected-flags load attempt
was followed by an RCU stall. That timing is consistent with a blocked MMIO read,
but does not prove the cause.

The image baseline is `payguard-kv260-20261025/build-r2`, completed on
2026-10-25 at 08:26:29 UTC, from source manifest
`08eecdfd5edef1587d17d1ebc6eeef0f0abac6232baa127a08a81307d43ff8e2`.
Use the existing `siliconsign.bit.bin` from the verified board archive; this
directory intentionally contains no FPGA image or secrets. The original archive
fingerprints remain in `../../reports/current/build-artifacts.json`.
`package_overlay.tcl` has changed since that baseline only to generate this
Linux configuration and allow overlay generation independently of Bootgen.

The correction adds `siliconsign_afi`, compatible with `xlnx,afi-fpga`, after the
existing clock node and before UIO. Its `config-afi = <14 0>` selects 32 bits on
the active HPM0_FPD/GP0 interface. Index 14 also covers GP1: zero deliberately
selects 32 bits for that disabled interface. GP2 and the unused HP/HPC ports are
not written. `resets = <&zynqmp_reset 116>` belongs on the AFI device, which
programs the interface widths and then pulses the connected PS-to-PL reset0.

This follows these AMD sources, rather than guessed register writes:

- [`build_kv260.tcl`](../../vivado/build_kv260.tcl) enables GP0 at 32 bits,
  disables GP1/GP2, and connects `pl_resetn0` through `proc_sys_reset`.
- [AMD `common_proc.tcl`, `get_max_afi_val`](https://github.com/Xilinx/system-device-tree-xlnx/blob/xlnx_rel_v2025.2/device_tree/data/common_proc.tcl)
  encodes 32/64/128 bits as 0/1/2, shifted by 8 for GP0 and 10 for GP1.
- [AMD `device_tree.tcl`, `gen_afi_node`](https://github.com/Xilinx/system-device-tree-xlnx/blob/xlnx_rel_v2025.2/device_tree/data/device_tree.tcl)
  combines GP0/GP1 into AFI index 14 and puts PS-PL resets on the AFI node.
- [AMD 6.6 `xilinx-afi.c`](https://github.com/Xilinx/linux-xlnx/blob/xlnx_rebase_v6.6_LTS/drivers/fpga/xilinx-afi.c)
  accepts register/value pairs, calls the firmware AFI API, then resets the
  node's optional reset array.
- [AMD 6.6 `reset-zynqmp.c`](https://github.com/Xilinx/linux-xlnx/blob/xlnx_rebase_v6.6_LTS/drivers/reset/reset-zynqmp.c)
  implements that reset through the firmware's pulse operation.

The exact Ubuntu `6.8.0-1015-xilinx` source was not independently retrieved.
The board must export `fpga_full`, `amba`, `zynqmp_clk`, and `zynqmp_reset`, and
have the clock and AFI drivers available and bound. Before any MMIO read, the
deployment helper must verify those bindings and an enabled, prepared fabric
clock in the accepted 49.5–50 MHz range. A child-process timeout cannot recover a
CPU blocked on an unresponsive AXI transaction. Loading this overlay alone is
not evidence that the FPGA registers or payment operations work.

## Fresh-boot recovery paste

After the observed RCU stall, **reboot the board before using this recovery
installer**. A clean kernel-taint check does not establish that an earlier RCU
stall or blocked AXI transaction has recovered. Use the new
[`PayGuard-KV260-r3.txt`](PayGuard-KV260-r3.txt) paste installer, which embeds the
corrected helper; do not rerun the old installed deployment helper.

The installer verifies its public payload and the original private image, then
creates `/opt/payguard-recovery-r3` without changing `/opt/payguard-kv260`. It
accepts only the standard `k26-starter-kits_image_1` startup overlay or no
overlay, checks the installed clock/AFI drivers before unloading that starter,
and performs a load-only operation. It does not upload an image or keys, read
FPGA registers, start the payment bridge, or establish physical verification.
An existing mismatched recovery target or any other active overlay stops it;
an identical existing target permits inspection only, never a reload.

Regenerate without Vivado or a bitstream, using Tcl and `dtc`:

```tcl
source hardware/vivado/package_overlay.tcl
siliconsign_write_overlay hardware/recovery/r3
```

The focused test compiles the generated DTS, applies it to a synthetic base
tree, checks resolved reset/clock references and the exact AFI field, and verifies
that the checked-in DTS/DTBO match the generator. It makes no hardware accesses:

```sh
python -m unittest discover -s hardware/tests -p test_overlay.py -v
```
