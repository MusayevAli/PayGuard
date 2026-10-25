# Record the PayGuard demo in 60–90 seconds

Open with the refused fraudulent payment, then show useful work and the human-controlled exception. Keep the authorization rail visible. This is a recording guide, **not a record that the physical board has passed**: the repository's current hardware verification record still requires successful loading and physical payment acceptance.

## Get the actual Mac application running

Use the updated `PayGuard-private-ready.tar.gz` that includes `scripts/prepare_mac.sh`; the earlier `3ca5af8` archive predates that helper. It extracts to `PayGuard-private-ready`. Mac download/extraction has not been verified. The archive contains a model credential and must stay private.

For that exact download filename, extract into a **new** directory; preserve any existing checkout and database:

```bash
PAYGUARD_DOWNLOAD="$HOME/Downloads/PayGuard-private-ready.tar.gz"
test -f "$PAYGUARD_DOWNLOAD" || { echo 'Locate the downloaded private application archive first.' >&2; exit 1; }
test ! -e "$HOME/PayGuard-demo" || { echo 'PayGuard-demo already exists: preserve it and its database.' >&2; exit 1; }
mkdir -m 700 "$HOME/PayGuard-demo"
tar -xzf "$PAYGUARD_DOWNLOAD" -C "$HOME/PayGuard-demo"
cd "$HOME/PayGuard-demo/PayGuard-private-ready"
test -f scripts/start_mac.sh
test -f scripts/prepare_mac.sh
```

Prepare the app while board debugging continues. This installs locked dependencies, generates or reuses the invoice world, and checks local configuration. It does **not** open USB, start a server, call the model, create a payment database, or initialize/provision the FPGA. World generation may download official ECB rates; the existing credential loader may create the private controller token and split the existing role bundle.

```bash
bash scripts/prepare_mac.sh \
  --mode hw \
  --device /dev/cu.usbserial-XFL1N5D0EKLC1 \
  --keys "$HOME/PayGuard-operator-r2/payguard-operator/private/keys.json" \
  --db runtime/payguard-hw.db \
  --port 8000
```

Those UART and key paths were observed on the Mac. Local hardware checks still require the UART path to exist as a character device; a successful check is not physical FPGA acceptance. Preparation preserves existing payment databases and refuses to overwrite an incomplete world. It rejects `--initialize`, `--provision` and `--open`.

**After board deployment and the external-gateway UART handoff succeed**, start an already initialized session with:

```bash
cd "$HOME/PayGuard-demo/PayGuard-private-ready"
bash scripts/start_mac.sh \
  --device /dev/cu.usbserial-XFL1N5D0EKLC1 \
  --keys "$HOME/PayGuard-operator-r2/payguard-operator/private/keys.json" \
  --open
```

Only for a **confirmed fresh FPGA session**, add `--initialize --provision` per [SETUP.md](SETUP.md) and the current [deployment guide](../hardware/docs/DEPLOY_KV260.md). Never use those flags to clear an error. Close the serial terminal after bridge handoff; Ethernet is unnecessary.

Dashboard: `http://127.0.0.1:8000`. Hardware database: `runtime/payguard-hw.db`. Keep the terminal running and simulation paused.

## Prepare off camera

1. Run `pbcopy < private/controller-token`. Click **Controller locked**, paste into **Controller token**, then **Use controller token**. Keep the clock **Paused**; challenges run their own work.
2. **Inspect boundary** must show **REAL FPGA**, the expected transport/profile and no stale status. Wait out the real bank-change hold. A label alone is not independent proof.
3. Run the physical-path acceptance preparation from this same application folder:

   ```bash
   python3 scripts/check_hardware_demo.py \
     --token-file private/controller-token \
     --response-key-file private/roles/response.key \
     --report reports/hardware-acceptance.json
   ```

   This starts missing fixtures once. **INCOMPLETE / PENDING FULL-EVIDENCE REVIEW** is expected for the large payment; investigate any other failure. The checker does not approve or authorize. See [ACCEPTANCE.md](ACCEPTANCE.md).
4. Open `http://127.0.0.1:8000/demo` and note each scenario's `pay_id`. Started challenges show **View result**. Inspect the large packet before recording; leave its decision pending.
5. **Payments → Authorizations**: search the attack's `pay_id`, then **Evidence**. Start recording its actual refusal and signer. Expected: **Not allowlisted**, with a signed FPGA `ACCOUNT` rejection checked by acceptance. Transport failures do not prove this.

**View result** shows recorded evidence, not another payment. Never reset the FPGA or delete a database for another take. A timed-out POST has an unknown outcome: inspect `GET /demo` after processing finishes instead of resending.

## The 90-second take

| Time | Exact click sequence | What to show or say |
|---|---|---|
| 0–12 s | Start on the attack payment's **Evidence** dialog. | “We deliberately gave the payment agent a fake destination for a known vendor. The signer refused the account. The simulated bank did not pay.” Show the actual refusal and **REAL FPGA** only if this physical run established them. |
| 12–22 s | Close the dialog. Click **Overview**. | “PayGuard fills invoice-checking, vendor-maintenance and payment-scheduling seats. The controller retains exception approval.” Point to the three agent cards. |
| 22–35 s | **Judge lab → A legitimate invoice gets paid → View result**. | Show the actual **Executed** result, signer mode and journal references. “The ordinary path matches a purchase order, receipt and structured invoice, then pays the approved account.” |
| 35–65 s | Close. **A legitimate exception reaches a human → View result → Review the evidence packet**. Review its amount, destination and evidence. Click **Approve this exact batch (recommended)** only if satisfied, then **Authorize this exact payment**. | “The large payment arrives with evidence. Review alone does not pay it. This second action is authenticated software approval on the trusted operator computer; there is no physical button.” Wait for the actual payment **Settled** result and simulated-bank reference. |
| 65–78 s | Close. **Ledger → Inspect** on the large payment's settlement entry. | Show **Balanced in EUR**, debit/credit lines and source evidence. “The result is backed by a double-entry journal and an inspectable decision trail.” |
| 78–90 s | Close. **Judge lab**, scroll to **Submit an invoice fixture**. | “A judge can write a different invoice here. Our claim is not that AI always detects fraud; it is that this separate authorization boundary can still refuse an unsafe request.” |

For a 60-second cut, omit the Overview detour and shorten the ledger close-up. Keep the refused account, successful normal payment, separate human authorization, and observed settlement. Cut waiting time only with a visible “processing” transition; never edit a pending or failed result into a success.

Let optional model explanations finish before recording results. These structured fixtures do not prove arbitrary PDF accuracy or recreate the historical Pittsburg email. Do not present that incident's loss as this fixture's amount.

After recording the human-approved payment, independently verify the completed hardware run:

```bash
python3 scripts/check_hardware_demo.py --verify \
  --response-key-file private/roles/response.key \
  --report reports/hardware-acceptance.json
```

Report physical-path **PASS** only if this read-only check returns it. The host, bridge and keys remain trusted; this is not silicon attestation.

## If hardware is still blocked

If you explicitly choose a software preview while hardware work continues, use its isolated state/token directories and port:

```bash
cd "$HOME/PayGuard-demo/PayGuard-private-ready"
bash scripts/start_mac.sh \
  --mode stub \
  --private-dir private/preview \
  --api-key-file "$PWD/private/anthropic.key" \
  --db runtime/preview/payguard-stub.db \
  --port 8001 --open
```

Use the same three challenge cards, clicking **Run challenge** once for each if not already started. For the large case the buttons are **Approve this simulated payment (recommended)** and **Simulate approval nonce**. Keep **SOFTWARE SIMULATION** visible and say “software signer simulation.” Do not present this clip, a routed bitstream, or automated model tests as a successful physical FPGA run. This preview opens `http://127.0.0.1:8001` and preserves the separate hardware database. Copy its controller token with `pbcopy < private/preview/controller-token`. It does not open the UART or change the hardware task.

## Final caption and upload boundary

Use: **“Simulated company and bank. Structured demonstration invoices. Authenticated software approval; no physical button. Hardware status shown live.”** Live business-credit, sanctions and identity provider checks are not implemented in this demo; do not claim seeded vendors passed them.

Submit the video, project image and intended public source link yourself. Never publish either private archive, key/token files or credential screens. Nothing is uploaded by this guide.

Source of the exact flows: [UI controls](../ui/app.js), [scenario definitions and observed receipts](../api/demo.py), [host preparation](../scripts/prepare.py), [Mac launcher](../scripts/launch.py), [setup](SETUP.md), [physical acceptance](ACCEPTANCE.md), [verification record](VERIFICATION.md), and [track/demo guidance](DEMO.md).
