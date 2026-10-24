# Part 1 plan — world, event store, clock, streaming API

Scope: `sim/` generator + `core/events.py` + `sim/clock.py` + minimal `api/`. No agents, ledger, or LLM.

## Files
- `pyproject.toml`, `Makefile` (`world`, `run`, `test`, `lint`), `.gitignore`, `README.md`, `docs/DECISIONS.md`
- `core/money.py` — `Money`, `MINOR_DIGITS`, `parse_locale_amount` (implemented; no stubs allowed by §0.4)
- `core/events.py` — `EventStore` (SQLite WAL, append-only triggers, `agent_cursors`, subscribers, idempotent append)
- `sim/models.py` — pydantic v2: `Supplier`, `PriceList`, `PurchaseOrder`/`POLine`, `GoodsReceipt`, `Invoice` (+`InvoiceFormat`), `Person`, 18 payload models of §5.1, `Event` envelope (`known_at >= event_time`), answer-key models
- `sim/people.py` — roster + hidden scripts (data only; `reply()` is Part 5)
- `sim/fx.py` — ECB `eurofxref-hist.zip` fetch, pinned 46-day source window shifted onto 2026-10-01..11-15, carry-forward for non-business days, `FxFetchError`
- `sim/mt940.py` — `Statement`/`StatementLine` models, `write_mt940`, `parse_mt940` (round-trip exact)
- `sim/templates/` — 8 ReportLab templates (`cz_classic`, `cz_totals_top`, `pl_vat`, `pl_vat_columns`, `de_rechnung`, `de_two_page`, `jp_seikyusho`, `us_invoice`), vendored fonts
- `sim/worldgen/` — `rng.py`, `catalog.py` (SKUs + cs/pl/de/ja names), `suppliers.py` (VAT via stdnum, IBAN via schwifty), `orders.py` (POs, receipts, M4–M6), `invoices.py` (documents: structured/pdf/png/eml, locale formats, A-cases), `scans.py` (pdfium + numpy noise), `bank.py` (statements), `traps.py` (T1–T10, T8 thread), `answer_key.py`
- `sim/generate_world.py` — CLI `--seed --out`; orchestrates, assigns `ev_NNNNNN` in `known_at` order, writes all outputs
- `sim/clock.py` — `SimClock(store, world_events, start, speed)`: `pause/resume/step/set_speed/advance_to`, background loop, `day_boundary`
- `api/main.py`, `api/ws.py`, `api/schemas.py` — `/state`, `/clock/{action}`, `/events`, `WS /live`, §13 error model, `Idempotency-Key`

## Interfaces
- `EventStore(path).append(ev) -> Appended(event, inserted)`; `append_many`; `get(id)`; `query(type=None, since_seq=0, limit=200)`; `latest_seq()`; `subscribe(cb, types=None) -> unsubscribe`; `mark_handled(agent, event_id)`, `is_handled`, `unhandled(agent, types)`
- `SimClock.step() -> StepResult(from_, to, inserted: list[Event])`; `advance_to(t)`; `state() -> ClockState`
- `generate(seed, out) -> WorldSummary`; `write_mt940(stmt) -> str`; `parse_mt940(text) -> Statement`; `build_fx_table(rows, source_start, target_start, days, currencies)`

## Tests (written first)
unit: `test_models`, `test_money`, `test_events` (triggers, idempotency, cursors, subscribers), `test_mt940` (round-trip, truncation), `test_fx` (46 dates, EUR base, no NaN, error path), `test_clock` (boundaries, step, pause/speed).
integration: `test_world` (determinism via two subprocess runs; counts 40/160/300; 90/120/30/60 split; 8 templates; 5 languages; ≥20% scans; IBAN/VAT valid; 10 headline + ≥45 ambient entries referencing real event ids; known_at ≥ event_time; T8 lookalike + compromise window; M1 same VAT), `test_clock_api` (3 steps insert exactly the right events; WS seq order; catch-up; error model).

## Risks
- ECB fetch needs network; cache the zip under `.cache/` and fail loudly otherwise. No skips.
- Byte-identical outputs: ReportLab `invariant=1`, numpy-seeded noise, no sets iterated for output, no wall-clock reads. Verified by two subprocess runs.
- 60 s budget: 30 PNG rasterizations at 200 dpi dominate; keep PNG compression low.
- Pre-sim backlog (POs/receipts known before 2026-10-01) is inserted on the first step; test accounts for it.
- Statement lines are world-level only; runtime payments (Part 4) will append their own lines (documented seam).
