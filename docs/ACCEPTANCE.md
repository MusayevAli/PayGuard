# Physical-path acceptance on the Mac

**Hardware acceptance is blocked on the corrected R5 build.** The R2 bitstream is
quarantined for a permanently asserted auxiliary reset; R3/R4 overlays cannot
repair it. Do not start this checker against that image. Preserve prior state
and follow the [R5 replacement requirements](../hardware/recovery/R5_REPLACEMENT.md).

Run this after deploying the matching FPGA image, handing the Kria USB console to the external-gateway bridge, and starting PayGuard locally with `SIGNER=hw`. This checks the running path and its evidence. It does not flash the board, initialize a new session, reset spending, enroll accounts, or authorize a human decision.

From the PayGuard checkout, use your existing private controller token and response-only key files:

```sh
python3 scripts/check_hardware_demo.py \
  --base-url http://127.0.0.1:8000 \
  --token-file /absolute/private/controller-token.txt \
  --response-key-file /absolute/private/response.json \
  --report reports/hardware-acceptance.json
```

The token file contains only the controller token. Both credential files must be owned by the current operator and private (`chmod 600`). The response file may contain `{"response":"64 hexadecimal characters"}` or the raw hexadecimal key. Existing `PAYGUARD_CONTROLLER_TOKEN` and `PAYGUARD_RESPONSE_KEY_FILE` environment variables can supply these values instead. Never put token/key values in command-line arguments. The script uses Python's standard library plus the checked-in protocol helpers; it does not need a new package or service.

The checker requires `hw`, a live USB UART backend, the `external-gateway` bridge, at least 64 vendor slots and 256 batch slots, the 50 MHz compiled clock divider, and the simulated bank. It rejects the stub and the explicit test-model transport before starting any scenario.

It starts each missing normal, forged-account and large-payment fixture once. Previously started fixtures are reused. It checks the normal payment's full signed response, native settlement and EUR budget binding, simulated-bank execution reference and balanced accounting entries. The attack must produce a signed FPGA `ACCOUNT` rejection for the known vendor's forged account, with no payment execution or settlement entry.

For the large payment, expect **INCOMPLETE** and **PENDING FULL-EVIDENCE REVIEW**. The script prints the exact packet URL. Open that packet in the dashboard, inspect the invoice, purchase order, receipt, amount, destination and refusal evidence, and decide whether to approve it. If approved, use the dashboard's separate authorization control for that exact payment. The checker never calls either approval endpoint, presses a simulated button, or creates approval evidence.

Then run the read-only verification:

```sh
python3 scripts/check_hardware_demo.py --verify \
  --response-key-file /absolute/private/response.json \
  --report reports/hardware-acceptance.json
```

`--verify` issues GET requests only. Status reads still produce ordinary device/audit records; they do not change payment authority, balances, caps or the FPGA clock. The final check requires both legitimate payments to have successful signed authorizations in the current session, distinct authorization counters, matching execution references, and balanced ledger entries. The large payment must also have a recorded packet approval. It does not authorize or retry a missing payment.

Exit codes are `0` for **PASS**, `3` for **INCOMPLETE**, and `1` for **FAIL**. Without the response-only key, the checker cannot independently authenticate the returned MACs and always reports **INCOMPLETE**, even if the dashboard reports paid. A transport, cap, proof, accounting or session error is a failure; inspect it without resetting or resubmitting an uncertain payment.

Fixture-start POSTs allow up to 180 seconds for sequential model explanations; GETs retain a 30-second timeout. Each request is attempted once. If a POST times out or disconnects, its outcome is **UNKNOWN**: the server may still be processing it. Read `GET /demo` or run `--verify` after the service finishes to inspect the recorded outcome. Do not resend the mutation, reset the FPGA, delete its database, or treat the timeout as a rejection. `--verify` will report any stages that have not started without starting them.

The JSON report contains sanitized identifiers, amounts, proof fingerprints, results and limitations. It omits credentials, raw proofs, account hashes and bank-account numbers. An existing report can be updated; an unrelated file will not be overwritten.

**Scope of PASS:** physical-path acceptance from a trusted local host, with independently checked response MACs and API evidence. The host, bridge and operator keys remain trusted. This is not silicon attestation, protection from a malicious host administrator, physical-button verification, or a test of real banking. The checker sees API bank references and ledger entries; it does not directly inspect the bank's internal durable receipt table. Automated tests exercise these checks with explicit model fixtures and do not establish a physical-board result.
