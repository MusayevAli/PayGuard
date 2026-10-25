# Start PayGuard on the existing Mac and KV260

The host runs Python, the agents, model API calls, dashboard and simulated bank. The KV260 runs the signing rules. USB UART connects them; Ethernet is not used. This launcher does not flash the FPGA or start a new cloud instance.

## Current delivery

**The `build-r2` FPGA image is quarantined: do not load or probe it again.** Its implemented XSA confirms `C_AUX_RESET_HIGH=0` while `aux_reset_in` is tied to zero, permanently asserting the auxiliary reset. Routing and timing passed, but that does not establish functional reset wiring. The R3/R4 Linux overlays cannot repair this gate-level fault. A corrected R5 bitstream is being rebuilt on the existing `starline-builder`; its successful build and physical acceptance are pending.

Preserve the existing private R2 operator bundle, role keys, transferred board archive, host database and board journals. The [artifact manifest](../hardware/reports/current/build-artifacts.json) and routed reports remain historical evidence for that quarantined image. R5 must reuse the existing matching role keys and be packaged into a new private destination; only its new board archive goes to the Kria. The host application remains a separate source checkout/archive, and admin/controller keys remain on the Mac.

R5 requires a **new bitstream and a new USB transfer**. A paste-only overlay update is insufficient. Follow the [R5 replacement requirements](../hardware/recovery/R5_REPLACEMENT.md) and [deployment guide](../hardware/docs/DEPLOY_KV260.md) once the corrected build has passed its reset-wiring, implementation and packaging checks. Begin with load-only inspection; physical payment acceptance remains separate.

## First hardware launch

1. Wait for the corrected R5 build and its new, verified private board archive. Do not substitute R2, R3/R4 overlay packages or an old 16-vendor image. Transfer the new image over USB, run load-only, review its reset/clock/AFI metadata, then explicitly verify the existing image once. Preserve the existing compiled role keys and all prior state.
2. On the Kria, complete the root-owned USB-console handoff with `hardware/scripts/run_uart_bridge.sh` in `--external-gateway` mode. Keep the board's bridge journal. Close the terminal emulator after the handoff so PayGuard alone opens the serial port.
3. On the Mac, put the matching existing key bundle in a private file (owner read/write only). No new signing keys should be generated for an existing image.
4. From the existing PayGuard checkout, run the following with your actual serial-device and private-key paths:

```bash
bash scripts/start_mac.sh \
  --device /dev/cu.YOUR_KRIA_UART \
  --keys /absolute/private/existing-keys.json \
  --initialize --provision --open
```

Initialization and provisioning occur only because those two flags were explicitly supplied. Use them only for the documented fresh-session setup, after verifying the physical FPGA and bridge. The launcher validates and splits that existing bundle into private one-role files; it never prints the keys, rotates them or overwrites conflicting key files. Alternatively supply `--admin-key`, `--controller-key` and `--response-key` paths separately.

The launcher defaults to `hw`. If no device or matching keys are available it stops; it never switches to the software signer. The bank is always simulated in this project. The controller credential is generated once under `private/controller-token` if not supplied through the environment. Its value is not printed. On the Mac, copy it with:

```bash
pbcopy < private/controller-token
```

Paste it into the dashboard controller field. Keep the service bound to its default `127.0.0.1`; the launcher does not expose it publicly. Verify the dashboard says hardware mode, shows 64 vendor/256 batch capacity, and has finished the real hold period before running the demo. Human authorization is the authenticated controller software path; this build does not have an independent physical approval button or status LEDs.

## Subsequent launches

Use the same host database, programmed FPGA session and bridge journal:

```bash
bash scripts/start_mac.sh --device /dev/cu.YOUR_KRIA_UART --open
```

The launcher reuses private role files and the controller token. It does not initialize or provision again. Startup intentionally refuses an uncertain or changed device session; preserve the journals and use the documented operator recovery procedure. Deleting a database to clear a failure can destroy replay/accounting history and is not a recovery procedure.

Defaults are `runtime/payguard-hw.db`, `sim/world`, port 8000, and a paused simulation clock. Use `--db`, `--world`, or `--port` to select other explicit paths/settings. `--check` checks local files without contacting the device, generating a world, writing a database or starting a server. It may create the private controller token and split an explicitly supplied key bundle.

## World preparation and offline rehearsal

When `sim/world` is absent, normal startup generates the existing 40-supplier/300-document seeded evaluation world. It uses the official ECB history, cached in `.cache/eurofxref-hist.zip`, with TLS verification enabled. An existing incomplete world directory is not overwritten. If a download fails, supply a trusted CA configuration or place a verified official [ECB history archive](https://www.ecb.europa.eu/stats/eurofxref/eurofxref-hist.zip) at `--fx-cache`; no rates are invented. After a failed initial generation, choose a new empty `--world` directory or explicitly restore the incomplete directory before retrying.

For an explicitly labeled software rehearsal:

```bash
bash scripts/start_mac.sh --mode stub --open
```

That uses the separate persistent `runtime/payguard-stub.db`. The three structured demo scenarios need no paid invoice extraction. Freeform PDFs/images and optional generated explanations require whatever model configuration is enabled in the application. A software rehearsal is useful for the desk flow but is not evidence of physical FPGA enforcement.

Use [DEMO.md](DEMO.md) for the live sequence, actual sponsor criteria and precise real-versus-simulated claims. The scenario controls never erase prior outcomes; repeated requests return the existing result from the same database.

## Live model calls

The laptop makes Anthropic API calls; no model or internet client is installed in FPGA logic. A live 60-document extraction run passed all 556 checked values; see [the verification record](VERIFICATION.md) for the exact scope and remaining checks. Structured demo invoices use deterministic extraction and can still exercise payment authorization.

The private configured application archive includes `private/anthropic.key`. The launcher loads this owner-private file automatically, so no manual `export` is required. A different `--private-dir` changes the automatic location to `<private-dir>/anthropic.key`. Public source archives and Git remain credential-free. Keep the configured archive private; do not upload it as the public project source.

To choose a different existing key file, use `--api-key-file /absolute/private/anthropic.key` or `PAYGUARD_ANTHROPIC_KEY_FILE`. Precedence is: the explicit flag, then that file-path environment variable, then existing `ANTHROPIC_API_KEY`/`ANTHROPIC_AUTH_TOKEN` credentials, then the automatically discovered file. An explicitly selected file replaces existing model credentials; an automatic file never overrides them. The file must contain one key, be owned by the current operator, have no group/other permissions (`chmod 600`), and not be a symlink. The launcher never prints its value. `--check` may read and validate it but makes no model API request. With no file or environment credentials, structured invoice flows remain available.

`PAYGUARD_MODEL` and `PAYGUARD_MODEL_TEXT` select supported model IDs; the existing default is `claude-opus-5`. Keep API keys out of the dashboard, invoice instructions, Git and public downloads. The dashboard distinguishes missing credentials, configured-but-unverified credentials and actual recorded API responses. New-vendor live credit, sanctions and identity vetting is unavailable; non-fixture vendors remain ineligible for hardware enrollment.

After hardware startup, run [the physical-path acceptance check](ACCEPTANCE.md). It verifies the three scenarios and their signed evidence, and stops for human review before the large payment.
