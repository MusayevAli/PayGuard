# PayGuard

Autonomous accounts-payable desk for a simulated Toyota Boshoku European supplier operation, with
payment authority enforced in hardware. Spec: `spec/PAYGUARD_SPEC.md` (single source of truth).

What is real: the agents, rules, ledger, hardware signer and the attack class. What is simulated:
the company, its suppliers, bank, documents and people.

## Status

| Part | Scope | State |
|---|---|---|
| 1 | World generator, append-only event store, simulation clock, streaming API | done |
| 2 | Money/FX, chart of accounts, append-only ledger, policy, packets, bootstrap, Beancount + Fava | done |
| 3 | Agent runner, LLM boundary, invoice agent (R0–R7, R11–R15), scripted colleagues, scoreboard | done (API-backed runs need credentials, see below) |
| 4 | Frame protocol, signer stub + test vectors, provisioning, mock bank with MT940, payment agent | done |
| 5–8 | Vendor agent, people, UI, hardware, rehearsal | not started |

## Running the simulation

Prerequisites: [`uv`](https://docs.astral.sh/uv/) (it installs Python 3.12 itself) and network access
the first time you generate a world (ECB reference rates are fetched, then cached under `.cache/`).

```bash
uv sync                      # create .venv with all dependencies
make world SEED=42           # generate sim/world/ (about 6 s; byte-identical for the same seed)
make run                     # API on http://127.0.0.1:8000 (clock starts paused at 2026-10-01)
make test                    # pytest: unit + integration (generates two throw-away worlds)
make lint                    # ruff check, ruff format --check, mypy --strict on core/ and sim/models.py
```

`make world` writes:

| File | Content |
|---|---|
| `suppliers.json` | 40 suppliers with name variants, valid VAT IDs / IBANs, locale settings, price lists |
| `pos.json`, `receipts.json` | 160 purchase orders (12 later amended), goods receipts with faults M5/M6 |
| `invoices/`, `emails/` | 300 invoice documents: 90 structured JSON, 120 PDF, 30 PNG scans, 60 `.eml` |
| `statements/` | 46 daily MT940 statements per account (EUR, CZK, PLN) |
| `people.json` | the internal people, their contact details, hidden scripts and compromise windows |
| `fx_rates.json` | ECB daily reference rates for the window (EUR base) |
| `answer_key.json` | headline (T1–T10) and ambient (A1–A15, M1–M6) cases with machine-checkable checks |
| `invoices_truth.json` | ground truth per invoice; for tests and the scoreboard only, never read by agents |
| `world.jsonl` | every world event, ordered by `known_at`, ids `ev_000001…` |

### Clock and API

The clock runs at `speed` sim-seconds per real-second (default 8640, one sim day every ten seconds)
and inserts a world event into the store when `sim_now >= known_at`. A `day_boundary` event is
emitted at every sim midnight.

```
GET  /state                      sim_now, running, speed, day_index, pending_world_events, latest_seq
POST /clock/pause | resume       idempotent
POST /clock/step                 advances exactly one sim day synchronously; honours Idempotency-Key
POST /clock/speed  {"speed": N}  sim-seconds per real-second
GET  /events?type=&since_seq=&limit=
WS   /live?since_seq=N           hello, then every event in seq order, pings every 15 s
```

Errors are `{"error": {"code", "message", "hint"}}` with 4xx for client mistakes.

WebSocket messages: `{"kind":"hello","latest_seq":…,"sim_now":…}`, `{"kind":"event","event":{…}}`,
`{"kind":"ping","latest_seq":…,"sim_now":…}`. A client that reconnects with the last `seq` it saw
receives exactly the events it missed, in order.

Environment for `make run`: `PAYGUARD_WORLD_DIR` (default `sim/world`), `PAYGUARD_DB` (default
`payguard.db`), `PAYGUARD_SPEED`, `PAYGUARD_AUTOSTART=1` to start the clock running.

### Determinism

Everything under `sim/` derives from `--seed`: suppliers, orders, documents (ReportLab invariant
mode, seeded noise for scans, fixed MIME boundaries), statements and the answer key. Two runs with the
same seed produce byte-identical output directories. The only external input is the ECB rate file,
which is pinned to a fixed historical window and cached.

## Ledger

`core/ledger.py` is an append-only double-entry ledger in the same SQLite file as the event store
(`payguard.db` by default). Amounts are integer minor units; every non-EUR line carries the ECB rate
and date used for its EUR equivalent (`core/money.py: convert`). `post(entry, principal)` enforces
spec §5.2 in order: one side per line and a known account (chart in spec §2.1, `core/accounts.py`),
per-currency balance, EUR balance within ±1 cent with the residual posted to `Expenses:FX:Rounding`,
open period, principal scope (spec §8), at least one evidence event that exists in the event store,
and no duplicate content hash. Any failure raises `PostRejected(reason_code, detail)`.

`core/policy.py` is the single side-effect path for agents (`Policy.execute(action, principal)`);
refusals are logged as `agent_action{post_rejected | policy_violation}` events. It also enforces the
vendor bank-account invariants of spec §5.3 (one active account, verification over a different channel,
72 h hold, approval by someone other than the requester, `on_chip` only by the provisioner with the
jumper present, `PAYGUARD_JUMPER=1`).

`core/packets.py` stores escalation packets and records controller decisions idempotently;
`core/bootstrap.py` posts the opening balances (spec §2.7) and the legacy AP that the 2 October batch
payment settles, and seeds the vendor master from the generated suppliers.

### Beancount and Fava

The ledger rewrites `ledger/payguard.beancount` atomically after every post. Non-EUR postings carry
`@@ <EUR equivalent> EUR`, so Beancount balances each transaction exactly as the ledger does.

```bash
uv run bean-check ledger/payguard.beancount   # zero output means zero errors
make fava                                      # http://127.0.0.1:5000 (ledger slug: payguard)
```

Fava watches the file, so it refreshes as the ledger posts.

## Invoice agent (Part 3)

Run the world through the invoice agent only, from 1 Oct to 5 Nov 2026:

```bash
export ANTHROPIC_API_KEY=...                    # or `ant auth login`; nothing else reads the key
uv run python -m scripts.run_world --until 2026-11-05 --out runs/part3
```

The script bootstraps the ledger, steps the clock one sim day at a time, drains the agents after
every step, scores against the answer key and prints the scoreboard, API usage and cost. Without
credentials the two API-backed tests (`tests/integration/test_extraction_accuracy.py` and
`tests/integration/test_invoice_agent_world.py`) **fail** with a message saying so; they never
skip. `tests/integration/test_invoice_agent_world_offline.py` runs the same world with a
truth-backed extractor that lives under `tests/` (the ground truth is readable only from
`core/score.py` and tests; `tests/unit/test_no_truth_in_agents.py` enforces that).

### What the LLM decides, and what it does not

The model is a document reader and a writer. It **does**:

- extract fields from PDFs (rendered at 150 dpi), scans and e-mails into a strict JSON schema
  (`agents/llm.py`, the only module that imports `anthropic`), with locale hints when the sender is
  known; every call is logged with model, tokens and latency and cached by document SHA-256;
- write the one-sentence explanation attached to every `agent_action` (≤ 60 words, only ids that
  appear in the decision; a deterministic template is used when it fails twice or when no
  credentials are present);
- phrase short-pay notices and warehouse queries from a template skeleton (amounts and ids are
  validated against the facts after generation).

It does **not** decide anything: amounts are re-parsed under the supplier locale and reconciled
(Σ lines = net, net + VAT = gross, within `ROUNDING_TOL_MINOR`) by `agents/validate.py`;
structured invoices (30 %) never reach the model (`agents/structured_mapper.py`); vendor and PO
resolution, receipts, prices, VAT, duplicates, postings and payables are deterministic Python in
`agents/invoice_agent.py`, and every side effect goes through `Context.act` → `policy.execute`.

### Rules

| Rule | Trigger | Action |
|---|---|---|
| R0 | totals do not reconcile after one re-extraction | hold `unreadable_invoice`, packet |
| R1 | billed quantity > received | post min(billed, received), `short_pay_notice`, balance kept on the PO line |
| R2a | unit price within 200 bp of the agreed PO price | post at invoice price |
| R2b | variance above 200 bp | hold `price_variance`, packet |
| R3 | no goods receipt | ask Jonas (`asked_colleague`), wait 48 h, re-run on reply or receipt, escalate on expiry |
| R4 | same vendor, total within 50 bp, dates within 3 days, different number | `duplicate_rejected` citing the original |
| R5 | vendor not in the master (VAT id, then normalised name) | hold `held_unknown_vendor`, `onboarding_opened` |
| R6 | credit note | proportional reversal of the original entry (`Money.allocate`), payable reduced |
| R7 | month end: receipts not invoiced | one GRNI accrual per PO at PO price; reversed when the invoice posts |
| R11 | intra-EU supplier charges VAT | post net, self-assess 19 %, dispute VAT, packet above EUR 500 |
| R12 | rounding difference ≤ 5 minor units | post, difference to `Expenses:FX:Rounding` |
| R13 | no PO, freight-like | post to `Expenses:Freight`; buyer confirmation above EUR 2,000 |
| R14 | invoice dated before the PO | hold `invoice_dated_before_po`, packet |
| R15 | retention clause on the document | split `Liabilities:AP` / `Liabilities:AP:Retention`, two payables |
| M3 | invoice date in a price-list gap | hold `price_list_gap`, packet naming both candidate lists |
| M5/M6 | receipt on the wrong line / in the wrong unit | ask Jonas, reassign the receipt on confirmation |

Entity tax facts (spec §2.1): DE suppliers charge 19 % (`Expenses:VAT:Input`); CZ/PL/AT supplies
are reverse charge and the entity self-assesses `Expenses:VAT:Input` / `Liabilities:VAT:Output` on
the same entry; JP/US are imports without VAT.

## Payments (Part 4)

The payment desk runs every morning once the bank's three statements for the previous day have
been ingested (06:00 in sim time), so it knows yesterday's closing balances. The run is built from
open payables grouped by vendor and currency, ordered by discount deadline and due date: payables
due within two days, discounts on their deadline day, credit notes netted against the next payment,
retention payables never. For each payment: `payment_requested` (with the frame and the EUR
conversion) → `signer.sign` → `chip_response` → `bank.execute` → `payment_executed` → one journal
entry `Dr Liabilities:AP / Cr Assets:Bank:<ccy>` whose evidence is those three events. The bank's
own MT940 for the day arrives the next morning and the line is reconciled to the payment by
reference (`reconciled`). Fees post to `Expenses:Bank:Fees`, FX conversion legs pair up by
reference, batch lines settle the payables named in the remittance (the 2 Oct legacy batch), and
anything still unmatched two days after it became known becomes an informational packet.

- **Signer** (`signer/`): `proto/FRAME.md` is the byte-level contract: 64-byte frames, CRC-16/CCITT-FALSE,
  one response byte plus a 32-byte HMAC-SHA256 tag or a reason code. `signer/stub.py` is the
  reference policy engine (allowlist of 16 account hashes with caps and holds, daily cap, button
  nonce, provisioning only with the jumper) and `tests/vectors.json` (51 cases, regenerated by
  `uv run python -m scripts.gen_vectors`) is what the FPGA and MCU implementations must reproduce
  byte for byte. The chip's unit of account is EUR cents: the host converts and logs the conversion.
- **R10**: a payment above EUR 50,000 comes back `needs_button`; the agent holds it and opens an
  approval packet. After `POST /packets/{id}/decide` approves, `POST /stub/button` loads a nonce
  (the physical button on hardware) and the payment is re-sent with it. Only that path consumes a
  nonce.
- **T9**: when a discount is worth taking but cash in that currency would go negative, the agent
  opens a packet with three quantified options (pay what cash allows now, draw intercompany at
  4 % p.a., skip the discount) and recommends the largest net benefit; each option is implemented.
- **Provisioning** (`signer/provisioning.py`): `start_runtime` loads the key and caps and the 14
  pre-demo accounts (`DEMO_VENDORS`) with the jumper raised, then pulls it. Payments to other
  vendors are rejected by the chip (`not_allowlisted`), held, and queued for the next provisioning
  session; a later successful provisioning reopens them.
- **Bank** (`bank/mock_bank.py`): balances start from the opening balances and follow the world's
  statements; runtime payments and credits appear on the bank's own statements
  (`<CCY>yymmddP`). The HMAC key is shared with the stub (`PAYGUARD_CHIP_KEY` in hex, else the
  development key).
- **API**: `GET /chip/status`, `POST /stub/button` (404 under `SIGNER=hw`), `GET /payables?status=`,
  `GET /bank/accounts`. `SIGNER=hw` is refused until the hardware client lands in Part 7.

Run the world with both agents: `uv run python -m scripts.run_world --until 2026-11-05`;
`tests/integration/test_payment_run.py` is the offline acceptance run (truth-backed extraction,
a controller hook that approves button packets and presses the button).

## Layout

See `spec/PAYGUARD_SPEC.md` §3. Decisions the spec left open are recorded in `docs/DECISIONS.md`;
per-part plans live in `docs/plans/`.
