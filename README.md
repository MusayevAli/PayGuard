# PayGuard

Autonomous accounts-payable desk for a simulated Toyota Boshoku European supplier operation, with
payment authority enforced in hardware. Spec: `spec/PAYGUARD_SPEC.md` (single source of truth).

What is real: the agents, rules, ledger, hardware signer and the attack class. What is simulated:
the company, its suppliers, bank, documents and people.

## Status

| Part | Scope | State |
|---|---|---|
| 1 | World generator, append-only event store, simulation clock, streaming API | done |
| 2–8 | Ledger, agents, signer, bank, people, UI, hardware, rehearsal | not started |

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

## Layout

See `spec/PAYGUARD_SPEC.md` §3. Decisions the spec left open are recorded in `docs/DECISIONS.md`;
per-part plans live in `docs/plans/`.
