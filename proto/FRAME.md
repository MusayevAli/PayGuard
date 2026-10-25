# PayGuard signer frame protocol (authoritative)

Every exchange with the signer (software stub, FPGA or MCU) is one **64-byte request frame** and one
short response. All integers are **big-endian**. The host builds frames in `signer/frames.py`; the
hardware must accept exactly what the stub accepts and answer exactly what the stub answers:
`tests/vectors.json` is the contract.

## Request frame (64 bytes)

| Bytes | Field | Notes |
|---|---|---|
| 0 | `frame_type` | `0x01` pay, `0x02` provision, `0x03` day_tick, `0x04` nonce_query, `0x05` status |
| 1–61 | type-specific body | zero-padded |
| 62–63 | `crc16` | CRC-16/CCITT-FALSE (poly `0x1021`, init `0xFFFF`, no reflection, xorout `0x0000`) over bytes 0–61 |

### `0x01` pay

| Bytes | Field | Notes |
|---|---|---|
| 1–4 | `seq` u32 | must equal the chip's last accepted seq + 1 |
| 5–36 | `account_hash` | SHA-256 of the normalised account id (whitespace removed, upper case) |
| 37–44 | `amount_minor` u64 | **EUR-equivalent minor units** (cents): the chip's unit of account. The host converts the settlement amount with `Money.convert` at the request date and logs the conversion on `payment_requested`; for EUR payments it equals the amount paid |
| 45 | `currency` | settlement currency: `0x01` EUR, `0x02` PLN, `0x03` CZK, `0x04` JPY, `0x05` USD |
| 46–49 | `approval_nonce` u32 | `0` = none; otherwise the button nonce |
| 50–61 | `pay_id` | 12 ASCII bytes (`0x20`–`0x7E`), space padded; binds the tag to the payment id |

### `0x02` provision (jumper required)

| Byte 1 `slot` | Body | Meaning |
|---|---|---|
| `0x00`–`0x3F` | 2–33 `account_hash`, 34–41 `account_cap` u64, 42–43 `hold_until_day` u16 | one of 64 allowlist slots; caps in EUR-equivalent minor units |
| `0xFE` | 2–9 `daily_cap` u64 | daily cap |
| `0xFD` | 2–9 `button_threshold` u64 | payments above it need a button nonce |
| `0xFC` | 2–3 `nonce_timeout_days` u16 | a button nonce expires after this many day ticks (default 1) |
| `0xFF` | 2–33 `key` | HMAC-SHA256 key shared with the bank |

Any other slot value is malformed (`09`). `seq` is not used by provision frames.

### `0x03` day_tick

Bytes 1–2: `day` u16 = days since **2026-01-01** (`2026-10-01` is day 273). Resets `daily_spent`
when the day changes and expires the button nonce once `day - nonce_day >= nonce_timeout_days`.

### `0x04` nonce_query, `0x05` status

No body.

## Responses

Success is `0x00` followed by a type-specific payload; rejection is `0xFF` followed by one reason byte.

| Request | Success payload |
|---|---|
| pay | `tag[32]` = HMAC-SHA256(key, request bytes 0–61) |
| provision | `slot` u8 |
| day_tick | `day` u16, `daily_spent` u64 |
| nonce_query | `nonce` u32, `nonce_valid` u8 |
| status | `seq` u32, `day` u16, `daily_cap` u64, `daily_spent` u64, `button_threshold` u64, `nonce_valid` u8, `slots_used` u8, `key_loaded` u8, `nonce_timeout_days` u16 |

### Reason codes

| Code | Name | Raised by |
|---|---|---|
| `01` | `bad_seq` | pay: `seq != last_seq + 1` |
| `02` | `not_allowlisted` | pay: hash in no slot |
| `03` | `in_hold` | pay: `day < hold_until_day` of the slot |
| `04` | `over_account_cap` | pay: `amount > account_cap` |
| `05` | `over_daily_cap` | pay: `daily_spent + amount > daily_cap` |
| `06` | `needs_button` | pay: `amount > button_threshold` and `nonce == 0` |
| `07` | `bad_nonce` | pay: `nonce != stored nonce` |
| `08` | `nonce_used` | pay: nonce matches but was consumed or expired |
| `09` | `malformed` | wrong length, unknown type, unknown currency, bad slot, non-ASCII pay_id |
| `0A` | `bad_crc` | CRC mismatch (checked after the length) |
| `0B` | `provisioning_locked` | provision frame while the jumper is absent |

## Check order (identical in stub and hardware)

length/type/currency/pay_id → CRC → (provision: jumper → slot) → seq → allowlist → hold →
account cap → daily cap → button → nonce value → nonce valid → **commit** (`seq += 1`,
`daily_spent += amount`, `nonce_valid = 0`) → HMAC → respond. The first failing check answers and
**a reject changes no state**. Any successful payment clears the nonce: a button press authorises
exactly one payment.

## State

`allow_hash[64][256]`, `allow_cap[64][64]`, `hold_until_day[64][16]` (the RTL keeps these in BRAM and
compares the 64 hashes sequentially; no parallel comparators), `daily_cap[64]`,
`daily_spent[64]`, `button_threshold[64]` (default EUR 50,000.00 = 5,000,000), `key[256]`
(all-zero until loaded; payments then carry an unverifiable tag and the bank rejects them),
`seq[32]`, `nonce[32]`, `nonce_valid`, `nonce_day[16]`, `nonce_timeout_days[16]`, `day[16]`.

## Host conventions

- The chip reports the first failing check. The host's `chip_response.reasons` lists that reason
  first and may append what the host can see in the vendor master (`in_hold` when the paying
  account's hold has not elapsed), which is how T8 reads `not_allowlisted` + `in_hold`.
- The bank recomputes the HMAC with the shared key, settles the native amount recorded on the
  `payment_requested` event for the frame's `pay_id`, and checks that the frame's EUR-equivalent
  amount and account hash match that event.

## Test vectors (`tests/vectors.json`)

Schema `payguard-vectors-1`: `{"schema", "key_hex", "nonce_seed", "cases": [...]}`. Each case has
`id`, `title`, `jumper` (bool: jumper state while the frame is processed), optional `before`
(`{"press_button": <nonce>}`: load this nonce into the one-shot register before sending),
`request_hex`, `response_hex`, `expect` (`{"ok": bool, "reason": name | null}`) and `state_after`
(`seq`, `day`, `daily_spent`, `nonce`, `nonce_valid`, `slots_used`, `key_loaded`). The hardware
testbench replays the cases in order against a freshly reset device and must match every response
byte for byte.
