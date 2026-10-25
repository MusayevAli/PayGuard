# Verification record

Recorded on 2026-10-25 for the local `codex/fpga-usb-demo` working branch. Commands below run from the repository root. These results distinguish application tests, software models, RTL simulation, synthesis and physical hardware; passing one does not establish the others. Counts overlap and must not be added together.

## Application and API

Latest broad offline regression run, before the subsequent r3 overlay recovery changes: **626 passed in 42.84 seconds**, including the full-image loader-mode and kernel-OOPS guards, UART handoff fixes, monetary wording and retention parsing.

```bash
uv run --frozen pytest -q tests/unit \
  tests/integration/test_controller_auth.py \
  tests/integration/test_dashboard.py \
  tests/integration/test_demo_api.py \
  tests/integration/test_judge_invoice_api.py \
  tests/integration/test_hardware_demo.py \
  tests/integration/test_clock_api.py \
  tests/integration/test_hardware_acceptance.py \
  tests/integration/test_uart_upload.py --tb=short
```

Coverage includes controller authentication, evidence review before authorization, concurrent packet decisions and recovery, native-currency proof binding, malformed/replayed proofs, atomic bank debits, cross-batch obligation/credit limits, interrupted-payment holds, judge-written invoices, the three demo outcomes, acceptance-checker restrictions, UART transfer recovery, and the real SDK's local HTTP transport lifecycle. Hardware integration tests use explicitly identified protocol/model fixtures; they do not access the KV260.

Earlier full run: **483 passed, 1 failed in 1517.56 seconds**.

```bash
uv run --frozen pytest -q \
  --ignore=tests/integration/test_extraction_accuracy.py \
  --ignore=tests/integration/test_invoice_agent_world.py --tb=short
```

All long financial-world runs passed, including double-entry reconciliation and both T7/T8 bank-change/fraud variants. Their test-only extractor reads fixture truth; this validates the downstream workflow, not live model extraction accuracy. The sole failure was world reproducibility: PO ordering used process-specific object addresses. Stable seeded ordering replaced that tie-breaker. The focused rerun below passed; the complete 25-minute suite was not repeated after that fix.

## Live API

Private credentials subsequently passed an authenticated model-list request and a real `claude-opus-5` Messages smoke request (16 input tokens, 4 output tokens; 0.804 seconds).

The live extraction sample passed with **556/556 checked fields and line values (100.00%) across 60 documents**, eight templates and 21 template/format combinations. The checked values are invoice number, invoice date, currency, net, VAT, gross, and each line's quantity and unit price. This is an extraction score on the generated sample, not a claim about arbitrary real-world invoices or vendor authenticity. The sample used 60 successful API responses, with **$2.4505 estimated usage cost** from the repository's pricing table.

Both live test files now pass: **2 passed in 352.46 seconds** on the corrected replay. The invoice-agent workflow through November 5 processed all **241 invoices available by the cutoff**, caught all **7 in-scope headline cases and 48/48 eligible ambient cases**, and reported **zero false positives or entries without evidence**. Every daily AP reconciliation, the policy/scope checks and Beancount validation passed. This run uses the stub signer and simulated bank; it does not establish physical FPGA operation.

The first complete API-backed workflow failed its ambient assertion. Three cases required invoices arriving after the test cutoff, and a Polish warranty-retention clause was correctly extracted but not recognized by the code. Eligibility now depends on required inputs' `known_at` timestamps, never a case's result; the original minimum of 43 caught cases and minimum rate of 43/45 are unchanged. Retention parsing recognizes the Polish clause and selects its percentage rather than a nearby VAT/discount percentage. The corrected journal retains 10% and leaves 90% payable. **48 focused transport, scope, validation and payment tests passed in 3.69 seconds.**

A separate real-SDK/local-HTTP regression found that reusing an async transport after its event loop closed could duplicate a request on retry. Owned async clients now close within each prefetch batch's loop. The final replay reused genuine stored model outputs and needed only **one new explanation response**, estimated at **$0.005755**; no documents or model extraction results were rewritten and no answer-key values were sent to the model. The 60-document accuracy test passed again from that cache. Cumulative recorded usage across the live attempts is **1,553 responses** (178 extraction, 1,365 explanation, 10 message drafts), estimated at **$13.423352**, plus the separate **$0.000180** smoke request. These are repository-rate estimates, not a provider billing statement. The initial failed attempt and corrected score/usage records are retained separately; the private cache backup contains no model credential and is not published.

The live command uses a private environment-provided credential and an explicitly reusable cache:

```bash
PAYGUARD_TEST_LLM_CACHE=/path/to/preserved/extraction_cache.db \
  uv run --frozen pytest -q -s \
  tests/integration/test_extraction_accuracy.py \
  tests/integration/test_invoice_agent_world.py --tb=short
```

Subsequent monetary-wording checks found raw minor-unit integers being described as whole currency amounts. Explanations and message drafts now receive separate exact minor/major/currency facts, check currency-qualified amounts against those facts, and use versioned prose caches. Deterministic invoice questions and evidence packets use the same currency-aware formatter; native, settlement, cash and cap metadata identify their currencies. Canonical financial integers and journal calculations remain unchanged. **86 focused tests passed in 6.26 seconds**, including JPY, negative values, large exact integers, foreign settlement, wrong model amounts and stale prose caches:

```bash
.venv/bin/pytest -q tests/unit/test_agent_money_wording.py \
  tests/unit/test_llm_money_wording.py tests/unit/test_rules.py \
  tests/unit/test_payment_agent.py tests/unit/test_llm_transport.py
```

One additional live wording check correctly returned **11,500.00 PLN posted, 1,150.00 PLN retained and 10,350.00 PLN payable**, using one response (1,207 input / 124 output tokens; estimated **$0.009135**). These wording and currency-metadata changes were made after the full workflow replay above; that workflow was not rerun for this subsequent change. Older explanation/message cache entries are not reused by the new wording paths; cached invoice extractions remain valid.

## Reproducible world

**19 passed in 27.44 seconds**, including byte-for-byte comparison of two complete worlds generated in separate processes.

```bash
uv run --frozen pytest -q tests/integration/test_world.py --tb=short
```

The generated seed-42 world contains 40 vendors, 160 purchase orders, 476 receipts, 300 invoices, 1,148 events, 138 bank statements, 10 headline cases and 52 ambient cases. Invoice formats are 120 PDFs, 90 structured documents, 60 emails and 30 PNGs. FX data comes from the official ECB archive; TLS verification remains enabled. Intermediate PDFs use a private temporary directory and are not retained as world artifacts.

## Hardware-package software and UART

**71 software tests passed** for protocol encoding, roles, journal recovery, the reference policy, gateway settlement and serial transport.

```bash
PYTHONPATH=hardware/python uv run --frozen python -m unittest discover -s hardware/tests -v
```

**17 uploader/receiver tests passed** after correcting the console bootstrap to open its terminal read/write. They cover byte-exact transfer, corruption, lost acknowledgements and completion receipts, mismatched receipt recovery, existing destinations/partials, size limits, explicit console return and receipt-save failure. A controlling-terminal regression also runs the pasted bootstrap through real Bash, transfers the archive and returns to the shell. A pseudo-terminal is not a physical USB cable or board.

```bash
uv run --frozen pytest -q tests/integration/test_uart_upload.py
```

The receiver only accepts its bounded transfer protocol. It checks chunk and whole-file SHA-256 hashes, publishes a private file without overwriting an existing path, and preserves failed partial transfers. It does not extract or execute an uploaded archive. Returning to the login shell is a separate operator action after a successful transfer. Transfer hashes detect corruption; they do not authenticate a hostile serial endpoint or establish physical presence.

## RTL, implementation and remaining validation

**Current hardware disposition: R2 quarantined; corrected R5 build pending.**
The implemented R2 XSA has an active-low auxiliary reset (`C_AUX_RESET_HIGH=0`)
tied to zero. This permanently asserts reset despite passing timing and DRC.
R3/R4 overlays cannot repair a gate-level tie-off. R5 is being rebuilt on the same
VM with the existing keys; it requires new reset-contract/implementation evidence,
a new USB transfer, load-only inspection and explicit physical verification.
No successful payment-gate register or payment run has been established. Preserve
historical reports, packages and journals; see the
[R5 replacement requirements](../hardware/recovery/R5_REPLACEMENT.md).

The subsequent r3 Linux-overlay recovery adds explicit AFI width/reset configuration, verifies clock enable/prepare counts and driver bindings, and separates no-MMIO loading from an explicit existing-image register probe. **41 focused deployment tests**, a real DTC compilation/synthetic-overlay application test, and **four mocked recovery-installer tests** passed. These checks do not access the physical KV260. The correction preserves the r2 FPGA image; it has not yet resolved the observed physical startup stall on a verified board run. See [the r3 recovery record](../hardware/recovery/r3/README.md).

The current target is a **64-vendor / 256-batch KV260 design with a 50 MHz PS fabric clock**, a matching hardware time divider and sequential invoice-hash lookup. Its current [RTL verification record](../hardware/reports/current/rtl-verification.json) passes policy, HMAC, AXI, sequential scan, capacity, core, differential and hardware-clock simulation checks, alongside the 71 software tests.

The quarantined R2 image was synthesized and fully routed using Vivado 2026.1 on the existing `starline-builder` instance for `xck26-sfvc784-2LV-c`. The [routed timing report](../hardware/reports/current/routed-r2/timing_summary.rpt) meets all specified timing constraints: **setup WNS +0.255 ns, hold WHS +0.011 ns, pulse-width slack +8.500 ns**, with zero failing endpoints, unclocked register/latch pins or unconstrained internal endpoints. The [DRC report](../hardware/reports/current/routed-r2/drc.rpt) reports zero violations. [Resource use](../hardware/reports/current/routed-r2/utilization.rpt) is 42,030 LUTs, 26,085 registers and four RAMB36 blocks. See [the hardware build record](../hardware/docs/BUILD.md) for artifact/source binding. Earlier parallel-lookup or 16/64 reports are not the evidence for this image.

The combined software/RTL verification entry point is:

```bash
uv run --frozen python hardware/scripts/verify.py
```

Its generated `hardware/build/verification/results.json` reports the checks that actually ran. The separate routed reports above establish synthesis and implementation results; neither those reports nor RTL simulation establishes that the image has been programmed onto the physical board.

Delivery has two separate parts. The **PayGuard source checkout/ZIP** supplies the Mac's host application, including the LLM transport and API responsiveness fixes. The **private operator/board package** supplies the board runtime, bitstream, private hardware keys, USB transfer scripts and setup files; it does not contain the host accounts-payable application. Live model credentials are absent from the public source and the operator/board package. A separately named **PayGuard-private-ready.tar.gz** application archive intentionally includes the user's existing model credential in `private/anthropic.key`, with private permissions; it must not be published. Initiating a browser download does not verify its final Mac location or establish that board installation succeeded.

- Physical Mac-to-KV260 USB-UART transfer completed with the exact nested archive SHA-256 recorded in [the build metadata](../hardware/reports/current/build-artifacts.json); extraction and package integrity checks also passed on the board. The first programming attempt hit a Linux kernel BUG in the DMA-buffer loading path. The operator's screenshot confirms manager flags `20` (hexadecimal DMA mode) remained set. The deployment helper omitted full-image flag initialization; recovery must start with a clean boot and verified flags `0` after unloading the starter application. Subsequent load-only checkpoints confirmed programming, a 50 MHz enabled clock and AFI binding, but register probes froze the board. The implemented auxiliary-reset fault now quarantines R2. Physical register/payment execution and bridge restart behavior remain **unverified**.
- Live extraction and the through-November-5 invoice-agent workflow passed as described above. This is not a live test of vendor credit/sanctions services or all three agents on the physical FPGA. Structured demo invoices do not require live extraction.
- Business-credit, sanctions and identity services have not returned live verification evidence in this build. The bank is simulated and no real money moves.
- Human approval uses authenticated controller software. There is no independent physical approval button. The operator service, its host, key files and bank verifier remain trusted.
- Source changes are on the local branch. Authenticated GitHub write access was unavailable, so this record does not claim the branch has been pushed or a pull request opened.
