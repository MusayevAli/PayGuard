# KV260 r4: complete PS-PL reset sequence

This overlay replaces r3 while retaining the original private r2 FPGA image.
There is no Vivado rebuild, key change, policy change, address change, or clock
change. Physical register and payment operation have **not** been verified.

The r3 overlay loaded with a 50 MHz enabled/prepared clock and bound clock/AFI
drivers, but the first register probe froze the board. The user power-cycled it.
The r3 reset mapping was wrong: Linux names reset 116 `PS_PL0`, but that firmware
ID controls EMIO92. Vivado's `pl_resetn0` uses EMIO95, which is firmware reset119.
The r3 files remain unchanged as a record of that failed attempt.

R4 uses the exact four-entry reset sequence emitted by AMD's device-tree
generator: **116, 117, 118, 119**, attached to `siliconsign_afi`. This includes
EMIO95, which drives the connected `pl_resetn0`. All four entries belong to the
PL reset bank; this design connects only `pl_resetn0` to its reset controller.
The AFI width configuration remains `<14 0>`, matching the active 32-bit GP0
interface and assigning 32 bits to the disabled GP1 interface. No other AFI
registers are written. The clock remains 50 MHz and the AXI base remains
`0xA0000000`.

Primary source evidence:

- [AMD device-tree generator `gen_afi_node`](https://github.com/Xilinx/system-device-tree-xlnx/blob/xlnx_rel_v2025.2/device_tree/data/device_tree.tcl)
  emits the four reset entries 116–119 on the AFI node.
- [AMD firmware `pm_defs.h`, 2024.1](https://github.com/Xilinx/embeddedsw/blob/xlnx_rel_v2024.1/lib/sw_apps/zynqmp_pmufw/src/pm_defs.h)
  maps reset offsets 116/117/118/119 to GPIO5 EMIO92/93/94/95.
  The [2022.1 definitions](https://github.com/Xilinx/embeddedsw/blob/xlnx_rel_v2022.1/lib/sw_apps/zynqmp_pmufw/src/pm_defs.h)
  use the same mapping.
- [AMD firmware `pm_reset.c`, 2024.1](https://github.com/Xilinx/embeddedsw/blob/xlnx_rel_v2024.1/lib/sw_apps/zynqmp_pmufw/src/pm_reset.c)
  dispatches those IDs to their individual GPIO reset lines.
- [AMD Answer Record 68962](https://adaptivesupport.amd.com/s/article/68962)
  identifies `pl_resetn0` as EMIO95.
- [AMD Linux `xilinx-afi.c`, 6.6](https://github.com/Xilinx/linux-xlnx/blob/xlnx_rebase_v6.6_LTS/drivers/fpga/xilinx-afi.c)
  applies the AFI configuration and calls the reset-array pulse operation. It
  does not check the reset call's return value, so driver binding alone cannot
  prove that firmware completed the pulse.

The unchanged image baseline is `payguard-kv260-20261025/build-r2`, completed
2026-10-25 at 08:26:29 UTC, with source manifest
`08eecdfd5edef1587d17d1ebc6eeef0f0abac6232baa127a08a81307d43ff8e2`.
Use its existing `siliconsign.bit.bin`; this directory contains no image or
secrets. `manifest.json` identifies the overlay artifacts independently of that
original build. The production image still has 64 vendors, 256 obligation
slots, and the 50 MHz seconds divider.

The deployment helper must require all four reset entries and bound AFI/clock
drivers, and verify an enabled/prepared 49.5–50 MHz fabric clock. Load-only
operation is separate from the subsequent register probe. A timeout cannot
recover a processor stalled on a nonresponding AXI read. These source checks and
compiled-tree tests do not establish physical success.

Regenerate without Vivado or the private image:

```tcl
source hardware/vivado/package_overlay.tcl
siliconsign_write_overlay hardware/recovery/r4
```

The focused test compiles and applies the overlay to a synthetic base tree,
verifies all four resolved reset references, AFI width and clock, and checks that
the committed r4 artifacts match the current generator. A separate check keeps
the historical r3 binary and its single reset116 unchanged:

```sh
python -m unittest discover -s hardware/tests -p test_overlay.py -v
```
