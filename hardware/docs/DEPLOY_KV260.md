# Deploy PayGuard on the existing KV260 over USB UART

**R2 is quarantined. Do not load or probe the existing R2 image again.** Its implemented XSA confirms `C_AUX_RESET_HIGH=0` with `aux_reset_in` tied to zero, permanently asserting auxiliary reset. R3/R4 overlays cannot repair this gate-level fault. A corrected R5 bitstream is being rebuilt on the existing VM; successful build and physical acceptance are pending. Preserve existing images, reports, keys and journals.

The workflow below applies only after the corrected R5 build and new private package pass review. The old `/opt/payguard-kv260` installation remains historical; it must not be reused as the R5 image. The replacement package must supply distinct install/firmware paths. See [R5 replacement requirements](../recovery/R5_REPLACEMENT.md). Physical USB archive transfer succeeded, but no successful payment-gate register or payment run has been established.

Vivado runs on the existing **starline-builder**. The Mac runs the PayGuard agents, model API calls, controller and simulated bank. The KV260 runs the FPGA rules and a UART transport. Its Ethernet port is not required. No physical approval button or external LED is assumed; controller approval is authenticated software approval.

## 1. Package the completed current build

Use the completed, corrected **R5 64-vendor/256-obligation** output directory only. Timing and capacity checks alone also passed for the quarantined R2 image: R5 packaging must additionally verify implemented auxiliary-reset polarity and inactive tie-off. R5 completion is not yet recorded. Keep all existing Starline project directories untouched.

From a PayGuard checkout on the machine that has the completed build and its existing private keys:

```bash
uv run python -m scripts.package_operator \
  --build-dir /absolute/path/to/completed-64-256-build \
  --keys /absolute/private/existing-keys.json \
  --output-dir /absolute/private/new-payguard-transfer
```

The output directory must be new. R5 release must carry implemented reset-contract evidence and a new image hash; the R2 manifest is not evidence for R5. The packager requires the current capacity manifest, timing-success verdict, synthesis/utilization/DRC reports, Bootgen image and compiled overlay. It verifies the supplied keys match the build's private generated key header. If `siliconsign.dtbo` is missing, compile the generated DTS with `dtc -@` on the build machine before packaging; the package must not depend on an uninstalled compiler on an offline board.

This produces owner-only files:

| Archive | Destination | Contents |
|---|---|---|
| `payguard-operator.tar.gz` | Trusted Mac operator | Existing admin/controller/response keys, separate role files, board archive and setup guide |
| `payguard-kv260-board.tar.gz` | KV260 only | Deployment image/reports, Python runtime, board scripts and **response-only** JSON |
| `transfer-manifest.json` | Operator | Archive byte counts and SHA-256 checksums |

The archives are private **but unencrypted**. The FPGA image also contains compiled development keys. Do not publish either archive or upload it to a public artifact host. Transfer only the board archive to the Kria; the board transport does not need the admin/controller bundle. The package checks trusted local outputs; it is not a cryptographic attestation of arbitrary build provenance.

## 2. Copy the board archive without Ethernet

The file must arrive on the board **before** converting its login UART into a payment bridge. Download the private build/archive to the trusted Mac through the existing authenticated cloud workflow if it is still on `starline-builder`.

- **Existing removable storage:** use a microSD reader or USB storage only if already available and its data partition is actually readable on both systems. Do not assume macOS can mount the Kria's Linux root filesystem. Shut the board down normally before removing its boot microSD, preserve its partitions and boot files, and copy only the archive to an existing suitable data volume. Never reformat the card for this step. A FAT data volume does not preserve Unix permissions; keep the archive private and copy it into a private Linux directory before use.
- **Existing USB UART, Python only:** use the uploader below. It needs the current logged-in UART console and Python 3 on the Kria, plus Python 3 on the Mac. It does not need XMODEM, ZMODEM, pip packages, an SD reader, or Ethernet. At 115200 baud a multi-megabyte archive takes minutes. Do not send archive bytes to a running payment bridge.

### Transfer over the UART with no additional hardware

These scripts pass pseudo-TTY integration tests, and the original board archive completed physical KV260 UART transfer with matching SHA-256. That proves transport of that archive, not operation of its quarantined bitstream. R5 must be transferred and verified separately. Run the steps separately so the terminal emulator and uploader never compete for the port.

1. On the **Mac**, from the PayGuard checkout, copy a fresh receiver bootstrap:

   ```bash
   python3 scripts/uart_upload.py --print-bootstrap | pbcopy
   ```

   If using the extracted private operator archive instead, the adjacent scripts are under `payguard-operator/transfer/`; use that `uart_upload.py` path. The generated script contains the receiver code, not signing keys. It is multiline with bounded line lengths. Paste the entire clipboard into the **already logged-in Kria UART terminal**, not the Mac shell. It creates a new private `payguard-transfer-*` directory under the existing board user's home, writes the receiver, replaces the login shell, and waits. Wait for `PGUP1 READY 134217728 1024` before continuing. Keep its leading `exec` and both heredoc terminators intact.

2. Disconnect the terminal emulator to release the serial port. In a separate **Mac** terminal, send only the board archive:

   ```bash
   python3 scripts/uart_upload.py \
     --device /dev/cu.YOUR_KRIA_UART \
     --file /absolute/private/payguard-kv260-board.tar.gz \
     --receipt /absolute/private/new-uart-receipt.json
   ```

   Use the existing USB serial device name. The archive must be a private owner-only regular file and at most 128 MiB. The receipt path must be new, in an existing private directory. The sender requests exclusive UART access and shows byte progress. Each 1024-byte chunk has a sequence number, SHA-256 and acknowledgement; retries do not duplicate bytes. The receiver reserves a private `.partial` file without overwriting anything and publishes the final filename only after the complete file hash matches. It never extracts, imports or executes uploaded data.

3. On success the uploader **closes the UART first**, then prints/saves `board_path`, byte count, SHA-256 and a one-time `console_command`. Reopen the existing serial terminal at 115200 baud. Paste that exact `PGUP1 CONSOLE ...` command and press Return; input is not echoed while the receiver holds the port. Only the correct completion token after success restores TTY settings and starts the fixed login shell. The uploader never sends this command automatically. No uploaded text is interpreted as shell commands.

4. At the restored **Kria** prompt, use the receipt's actual `board_path` for `sha256sum` and the extraction command below. Transfer success does not mean deployment success. The archive has not been executed or unpacked.

If the transfer finished but its receipt was lost, leave the completed receiver running and rerun the uploader with the **same unchanged file and filename**, choosing a new receipt path. It verifies the existing completion's byte count, SHA-256 and filename, recovers the same console-return token, and does not rewrite the board file. A different file is rejected. This recovers a lost receipt, not an interrupted partial transfer or a restarted board receiver.

The receiver ignores hangup during terminal handoff. On a transfer/receiver failure, the uploader closes and the board retains its receiver/input sink and any private partial file. It does not fall back to a shell. Preserve the error/partial file and use an existing separate administration session or the board's normal operator restart/login procedure before installing a fresh bootstrap. No automatic reset or overwrite is attempted.

On the Mac, `shasum -a 256 payguard-kv260-board.tar.gz` must match the packager's checksum. On Linux, verify the received file with `sha256sum /absolute/path/payguard-kv260-board.tar.gz`. The digest is public verification metadata; the archive contents are private.

Extract into a new location on the Kria. This guarded command refuses to extract over an existing `/opt/payguard-kv260`:

```bash
sudo sh -c 'set -eu
umask 077
test ! -e /opt/payguard-kv260
tar --no-same-owner -xzf /absolute/path/payguard-kv260-board.tar.gz -C /opt
'
sudo python3 /opt/payguard-kv260/verify_package.py
```

Use the archive's actual path. Root extraction creates private root-owned files, including `/opt/payguard-kv260/private/response.json`. Its JSON contains exactly one `response` key; do not paste or print its value. The operator keys stay on the trusted Mac.

## 3. Inspect, then deliberately load the board

On the **KV260**, inspect first:

```bash
sudo python3 /opt/payguard-kv260/hardware/scripts/deploy_kv260.py --inspect
```

Inspection does not load an image or access the PL register aperture. Confirm an aarch64 KV260/K26, Linux FPGA manager, resolvable device-tree symbols `fpga_full`, `amba`, `zynqmp_clk`, `zynqmp_reset`, and the current overlays. The helper never unloads an existing overlay or rewrites boot firmware. An already-operating unmanaged image could be Starline; it is not replaced by default. Do not run Starline and SiliconSign concurrently against the FPGA. Arrange any existing image's replacement explicitly before using the helper's `--replace-unmanaged-image` option.

Before loading, both platform drivers must already be registered: `xilinx_fclk` and `afi-fpga`. These read-only checks inspect their availability without touching PL registers:

```bash
uname -r
grep -E '^(CONFIG_XILINX_FCLK|CONFIG_XILINX_AFI_FPGA|CONFIG_FPGA_MGR_ZYNQMP_FPGA)=' /boot/config-"$(uname -r)"
ls -d /sys/bus/platform/drivers/xilinx_fclk /sys/bus/platform/drivers/afi-fpga
```

The source configuration names are `CONFIG_XILINX_FCLK` and `CONFIG_XILINX_AFI_FPGA`. Module names, when those drivers are modular in the installed kernel, are `xilinx_fclk` and `xilinx_afi` (the latter source object is `xilinx-afi.o`). A built-in driver needs no module loading. The helper does not automatically load modules or change kernel configuration; missing registration stops before applying an overlay.

With a compatible, available FPGA and reviewed existing state:

```bash
sudo python3 /opt/payguard-kv260/hardware/scripts/deploy_kv260.py \
  --build-dir /opt/payguard-kv260/build --load-only
```

The helper preserves unrelated firmware and refuses to overwrite a different existing SiliconSign firmware file. After its board and overlay preconditions pass, it explicitly writes **`0`** to the FPGA manager's `flags` attribute and requires zero readback before loading. This selects full-image firmware loading: the AMD manager can retain hexadecimal `20` (DMA-buffer mode) from a previous application even after its overlay is unloaded. Missing, unreadable, malformed or unwritable flags stop deployment. Read-only inspection includes the flags as an integer and the kernel taint value.

Deployment also refuses a kernel that has recorded an OOPS/BUG (taint bit 7, value 128), or whose OOPS state cannot be read. A kernel warning or external driver alone does not trigger this guard. Preserve crash diagnostics and recover with an intentional clean boot; the helper never reboots the board or clears kernel taint. The flag selection follows [AMD's full-bitstream programming instructions](https://xilinx-wiki.atlassian.net/wiki/spaces/A/pages/18841847/Solution+ZynqMP+PL+Programming); the OOPS flag is defined in the [Linux taint documentation](https://docs.kernel.org/admin-guide/tainted-kernels.html).

Loading now stops **before every PL register read**, whether or not `--load-only` is supplied. Flushed phase messages identify entry to and return from kernel overlay application. The result is explicitly **UNVERIFIED**, with the payment bridge stopped. It requires the overlay to report `applied`, FPGA manager to report `operating`, and PL0 to have positive prepare/enable counts at **49.5–50 MHz**. The exact SiliconSign clock node must bind to `xilinx_fclk`; `siliconsign_afi` must bind to `afi-fpga`, configure AFI index 14 to 0 (GP0 width32), and reference **all four resets 116, 117, 118 and 119**, in order, on the actual `zynqmp_reset` controller. The earlier reset116-only overlay is rejected.

The complete reset array follows AMD's device-tree generator; the [r4 recovery notes](../recovery/r4/README.md) link the source evidence. Firmware IDs 116–119 map to EMIO92–95, while this design's `pl_resetn0` uses EMIO95, so reset116 alone does not address that line. Driver binding confirms the configured driver completed its probe; the reviewed AFI driver does not propagate the reset-pulse return value, so binding alone cannot prove that firmware pulsed or released the line. These are necessary configuration checks, not proof that an AXI transaction will complete.

After reviewing the successful load-only result, explicitly verify the existing image once, without reloading it:

```bash
sudo python3 /opt/payguard-kv260/hardware/scripts/deploy_kv260.py --verify-existing
```

This command repeats the kernel, overlay, clock, AFI and reset checks before reading the fixed PL register aperture. A child process can contain a SIGBUS fault, but **cannot contain a CPU stalled on a nonresponding AXI bus**; a timeout does not make an unconfigured bus safe. Verification requires IP ID `0x53494c31` at `0xA0000000`, valid keys, capacity register `0xA000000c` equal to **`0x01000040`**, and divider register `0xA0000010` equal to **50000000**. An old image can share the IP ID and still be incompatible. The actual PS clock, nominal divider and overlay request are all 50 MHz; a slightly slower accepted clock lengthens waits conservatively.

If post-load verification fails, stop. No bridge starts automatically. A successfully applied overlay remains installed for inspection; the helper does not claim to have restored a previous design.

An older installed helper automatically performs the PL probe and lacks these guards. After an OOPS or RCU stall, preserve the log and recover the board deliberately; do not repeat that helper's deployment command. A helper/overlay-only recovery directory is insufficient for R2's auxiliary-reset fault. R5 must include a newly rebuilt bitstream as well as the reviewed helper and AFI/reset overlay, preserving the original package. Start with read-only inspection, then a single load-only operation. Do not start the UART payment bridge until explicit existing-image verification succeeds.

## 4. Hand the existing login UART to PayGuard

Run this exact **PayGuard external-gateway** mode from the Kria's existing logged-in USB UART console:

```bash
exec sudo bash /opt/payguard-kv260/hardware/scripts/run_uart_bridge.sh \
  /opt/payguard-kv260/private/response.json \
  /var/lib/payguard-fpga \
  --external-gateway
```

Keep the leading `exec`: it replaces the login shell. The final argument is `--external-gateway`, **not** a bootstrap configuration and **not** the standalone `--recover` mode. PayGuard on the Mac owns initialization, vendor enrollment, approvals and the simulated bank; this board process only journals and forwards authenticated frames to real MMIO.

The wrapper requires root and the actual `/dev/ttyPS*` console, checks board identity and clock, stores TTY settings, uses 115200 raw/no-echo UART and logs to `/var/lib/payguard-fpga/uart-bridge.log`. It resolves the controlling UART from kernel process ancestry and its device number, including `/dev/tty` aliases left by the upload receiver and a PTY inserted by sudo. It refuses missing or mismatched identities and never guesses a UART port. It does not change sudo/getty settings or open a network listener. Preserve the **entire** `/var/lib/payguard-fpga` directory, including the transport journal and session manifest.

For a separately verified board runtime lacking `hardware/scripts/uart_console.py`, this handoff resolves its UART alias from the original logged-in shell. It only fixes terminal naming; it cannot repair R2 or replace the required R5 rebuild. Substitute the reviewed R5 runtime paths before invoking it:

```bash
pg_uart=$(python3 - <<'PY'
import os, re, stat
from pathlib import Path
fields = Path('/proc/self/stat').read_text().rsplit(')', 1)[1].split()
number = int(fields[4]) & 0xffffffff
if not number:
    raise SystemExit('No controlling UART; bridge not started')
name = (Path('/sys/dev/char') / f'{os.major(number)}:{os.minor(number)}').resolve(strict=True).name
if not re.fullmatch(r'ttyPS[0-9]+', name):
    raise SystemExit('Controlling terminal is not a Kria UART; bridge not started')
node = Path('/dev') / name
info = node.lstat()
if not stat.S_ISCHR(info.st_mode) or info.st_rdev != number:
    raise SystemExit('UART node does not match kernel identity; bridge not started')
print(node)
PY
) && {
  exec < "$pg_uart" > "$pg_uart" 2> "$pg_uart"
  exec sudo env "SUDO_TTY=$pg_uart" bash /opt/payguard-kv260/hardware/scripts/run_uart_bridge.sh \
    /opt/payguard-kv260/private/response.json \
    /var/lib/payguard-fpga \
    --external-gateway
}
```

An identification error leaves the console at the prompt without starting the bridge. Do not replace the resolved path with an assumed `/dev/ttyPS0` or pass a `/dev/pts/*` terminal. The inline resolver is intended for the original hardware-console login, before sudo introduces a PTY.

After the handoff message, disconnect/close the terminal emulator so it releases the Mac serial device. Do not keep the terminal and PayGuard connected to the same port. The wrapper ignores terminal-disconnect HUP. If the bridge exits, it remains attached as an input sink and returns `BRIDGE_STOPPED`; raw payment data is never interpreted as shell commands.

For a transport-only check on the Mac, from the PayGuard repository:

```bash
PYTHONPATH=hardware/python uv run python -m siliconsign serial-info \
  --device /dev/cu.YOUR_KRIA_UART
```

This read-only handshake should identify `mode: external-gateway` and the actual 64/256 capacity. It is not a payment authorization. The UART path still requires physical verification; an informational handshake alone does not establish the full demo has passed.

## 5. Start the host and verify the complete path

Follow [the host setup guide](../../docs/SETUP.md). For a verified fresh board/bridge session, explicitly choose initialization and provisioning once. On later starts, reuse the same host database, keys and device session and omit those flags. Wait for the real bank-change hold before presenting normal payments.

Verify one normal request, one account-mismatch refusal and one separately approved large request through the actual device, with matching audit receipts and balanced journal entries. Until those checks pass, report the board integration as pending. The bank remains simulated even when the FPGA is physical.

## Recovery boundaries

Do not use the standalone SiliconSign bootstrap/local-bank commands for this PayGuard connection. Do not run two session owners. A bridge or host restart with uncertain hardware state must stop for reconciliation; deleting a journal or adding `--initialize` is not a recovery procedure. Preserve both the host database and `/var/lib/payguard-fpga`.

An administrator can stop the UART wrapper with SIGTERM from an independent existing administration session; it restores saved TTY settings. Without one, normal restart/login is an operator recovery action, not a shortcut that authorizes resuming payments. A stopped wrapper cannot undo an existing hardware authorization. Reconcile any outstanding request before returning the desk to service.
