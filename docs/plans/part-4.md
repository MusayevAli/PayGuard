# Part 4 plan — signer stub, frames, mock bank, payment agent

Scope: `proto/FRAME.md`, `signer/` (frames, interface, stub, provisioning, vector generator),
`bank/mock_bank.py`, `agents/payment_agent.py` (+ `agents/payment_state.py`), `agents/runtime.py`
(shared wiring for `scripts/run_world.py` and the API), four API routes, `tests/vectors.json`.
No vendor agent, no hardware client.

## Check order (stub and hardware must match; the first failure answers, no state changes)
| # | Check | Reason byte | Note |
|---|---|---|---|
| 0 | length == 64, frame type known, currency known, pay_id ASCII | 09 malformed | wrong length is malformed before CRC can be read |
| 1 | CRC-16/CCITT-FALSE over bytes 0–61 == bytes 62–63 | 0A bad_crc | |
| 2 | provision frames: jumper present | 0B provisioning_locked | provision frames stop here (then slot validity → 09) |
| 3 | seq == last_seq + 1 | 01 bad_seq | pay frames only |
| 4 | account_hash in a provisioned slot | 02 not_allowlisted | |
| 5 | day >= slot.hold_until_day | 03 in_hold | boundary day passes |
| 6 | amount <= slot.cap | 04 over_account_cap | amount is EUR-equivalent minor units |
| 7 | daily_spent + amount <= daily_cap | 05 over_daily_cap | equal passes |
| 8 | amount > button_threshold → nonce != 0 | 06 needs_button | |
| 9 | nonce == stored nonce | 07 bad_nonce | |
| 10 | nonce_valid | 08 nonce_used | consumed or expired |
| 11 | commit: seq += 1, daily_spent += amount, nonce_valid = false | | any success clears the nonce |
| 12 | HMAC-SHA256(key, bytes 0–61) → `00` + tag | | |

`tick_day(day)` resets `daily_spent` when the day changes and expires the nonce after
`nonce_timeout_days` (provisionable, default 1). `day` = days since 2026-01-01.

## Files and interfaces
- `signer/frames.py`: `crc16`, `encode_pay/encode_provision/encode_day_tick/encode_nonce_query/encode_status`, `decode(frame) -> Frame`, `encode_response/decode_response`, `REASONS`, `FrameError(reason)`.
- `signer/interface.py`: `SignResult`, `ChipStatus`, `SlotInfo`, `Signer` protocol (`sign`, `nonce`, `tick_day`, `status`).
- `signer/stub.py`: `StubSigner(path | None, jumper_present, nonce_seed)`; state persisted in SQLite when a path is given; `press_button()`.
- `signer/provisioning.py`: `Provisioner.enqueue(vendor_id)`, `load_key`, `set_caps`; `provisioning` events; slot table in SQLite; `DEMO_VENDORS` (the 14 pre-demo accounts).
- `signer/vectors.py` + `scripts/gen_vectors.py`: fixed scenario → `tests/vectors.json` (schema `payguard-vectors-1`).
- `bank/mock_bank.py`: `MockBank(db, store, *, key, fx, now, schedule, out_dir)`: `balance`, `execute(frame, tag)`, `credit(...)` (intercompany draw), `ingest(statement_event)`, `publish_statements(through)`.
- `agents/payment_agent.py`, `agents/payment_state.py`: run, R10, T9, netting, reconciliation.
- `core/policy.py`: `RequestPaymentAction`; `core/packets.py`/`sim/models.py`: packet `kind` (anomaly | approval | notice), `bank_rejected` event; `core/accounts.py`: `Income:Discounts`.

## Tests
`tests/unit/test_frames.py`, `test_stub.py`, `test_vectors.py`, `test_mock_bank.py`, `test_payment_agent.py`
(kit-based: run building, R10 flow, T9 options, netting, reconciliation), `tests/integration/test_payment_run.py`.

## Risks
- Caps are EUR-equivalent: the pay frame carries the EUR-equivalent amount, the currency byte names
  the settlement currency; the bank settles the native amount recorded on `payment_requested`.
- The chip answers with one reason; `in_hold` alongside `not_allowlisted` (T8) is the host's
  annotation from the vendor master.
- 16 slots, 20 vendors due in the window: six vendors are held `not_allowlisted` until provisioned.

## Changes made while implementing (see DECISIONS 54–67)
- The payment run follows the morning statements (all three ingested), not the day boundary.
- Discounts are taken on the deadline day; legacy payables are `scheduled` and settle via the batch.
- Payables are `paid` at execution; the statement line confirms (`reconciled`).
- Chart gained `Income:Discounts` and `Liabilities:Intercompany`; packets gained `kind`.
- The bank settles the native amount from the latest `payment_requested`; frames carry EUR cents.
- `tests/vectors.json` has 51 cases; `create_app(desk=False)` keeps the Part 1 clock tests bare.
