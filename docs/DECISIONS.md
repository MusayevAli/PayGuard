# Decisions

Choices the spec left open, with the reasoning. Newest at the bottom of each part.

## Part 1 — world, event store, clock

1. **Spec location.** The spec was delivered at the repo root; it was moved to `spec/PAYGUARD_SPEC.md`
   to match §3. No content changed.
2. **`parse_locale_amount` is implemented now.** The prompt allowed deferring it to Part 2 but §0.4
   forbids placeholders, so it is complete and unit-tested (30 fixtures arrive in Part 2).
3. **Event ids.** Generator events use `ev_` + six digits assigned in `known_at` order. Runtime
   events (clock, agents) use `ev_` + the first 16 hex characters of their content hash, so the two
   namespaces cannot collide and a replayed runtime event maps to the same id.
4. **Content hash** is `sha256(type + "\n" + canonical JSON of the payload)` where canonical JSON
   has sorted keys, compact separators and raw UTF-8. A unique index on `content_hash` makes the
   idempotent append race-free; the same id with different content raises `EventConflict`.
5. **Payload normalisation.** `Event` validates the payload against the model registered for its
   type and stores the JSON-mode dump, so hashes do not depend on how a caller spelled a datetime.
6. **Clock semantics.** `step` advances exactly 24 h from the current `sim_now` (not to the next
   midnight). Day boundaries are emitted whenever sim time reaches a midnight, including the start
   instant, so after N steps from 2026-10-01 00:00 the boundaries 10-01 … 10-(N+1) exist. Events known
   before the start (purchase orders and receipts from August/September) are inserted on the first
   advance, before the first day boundary, because they were known first.
7. **`day_boundary` payload**: `date`, `day_index`, `previous_date`, `month_closed` (`YYYY-MM` when the
   boundary closes a month, used by R7 later).
8. **WebSocket pings** are application-level JSON messages every 15 s (browsers cannot observe
   protocol pings); `make run` also enables uvicorn's protocol pings. Catch-up is by `since_seq`; the
   server replays from the store if a queued event would leave a gap.
9. **`Idempotency-Key`** is honoured per route in an in-memory cache (good enough for one process;
   the natural keys of later POSTs do the heavy lifting).
10. **Error model**: `validation_error` (400) for bad bodies/queries, `invalid_speed` (400),
    `unknown_clock_action` (404 with the list of valid actions), `not_found` (404).
11. **Statement delivery.** The statement for day *d* is produced at *d+1* 05:30 and known at *d+1*
    06:00; each `:61:` line becomes a `bank_statement_line` event with `event_time` at noon of its value
    date. Remittance text is truncated to 140 characters and wrapped at 65 per SWIFT line. Statement ids
    are `<CCY><yymmdd>`, sequence numbers count from 1 on 1 Oct.
12. **The batch payment** on 2 Oct settles three September invoices of Katowice Metal Forming that
    predate the simulation (legacy backlog), so it cannot double-pay anything the agents will pay.
    Runtime payments (Part 4) append their own statement lines; world statements never include them.
13. **FX.** The ECB `eurofxref-hist.zip` is fetched with certifi's CA bundle (python-build-standalone
    ships none) and cached at `.cache/eurofxref-hist.zip`. The source window is pinned to
    2026-08-06 … 2026-09-20 (a Thursday start, so weekends line up with the target window) and shifted
    onto 2026-10-01 … 2026-11-15. Non-business days carry the last published rate and are flagged
    `carried_forward`. Any gap or missing currency raises `FxFetchError`; nothing is invented.
14. **VAT model.** Each plant is VAT-registered in its country. Supplier country == plant country gives
    domestic VAT (DE 19 %, CZ 21 %, PL 23 %); EU cross-border is reverse charge (0 % + note); JP/US are
    export (0 %). The Austrian tooling supplier wrongly charges 20 % on a reverse-charge supply (A8).
15. **`invoice_received` carries the document, not its contents** (path, format, sha256, size,
    attachments, channel hints). Ground truth lives in `invoices_truth.json` for tests and the
    scoreboard; agents must extract from the document.
16. **People.** Besides the four people of §2.8, the roster has two more internal buyers (Lukas
    Brandt, Agnieszka Kowalczyk) because the supplier master needs purchasing contacts for DE/AT/JP/US and
    PL suppliers, and the departed contact Jana Svobodová (left 2026-08-31) for M2. Jonas's wrong-PO
    script is filled by the generator with the M5 case planted for it.
17. **T8 data.** The lookalike thread is three world events: the attacker's request from
    `accounts@kovak-trim.cz`, Petra's in-thread "Confirmed, they told me last week." sent from her real
    mailbox during the compromise window (a `colleague_reply` without any compromise flag), and the
    attacker's follow-up. The EUR 2,100,000 invoice KT-2270 is a legitimate structured invoice due Friday
    17 Oct. Runtime verification replies come from `sim/people.py` in Part 5.
18. **T5.** The goods receipt for HM-09 is a world event booked by Jonas exactly 30 sim-hours after
    the invoice is known; his reply is produced at runtime in Part 5.
19. **Scans** use pypdfium2 at 200 dpi, rotation 0.5–2°, numpy-seeded Gaussian noise and a slight
    blur; only single-page templates are scanned; one Czech scan carries the "OK – JW" annotation and a
    received stamp. numpy was added for seeded noise (Pillow's noise is not seedable).
20. **Fonts** are vendored subsets with their licenses (DejaVu Sans, Noto Sans JP, Caveat); rebuild
    with `make fonts` after adding Japanese strings. A unit test checks glyph coverage.
21. **Answer-key `where` semantics**: plain keys match payload fields; `*_contains` means substring
    or list membership; `*_contains_all` requires every listed value. Account names assumed for
    Part 2: `Liabilities:AP`, `Liabilities:AP:Retention`, `Liabilities:GRNI`, `Expenses:VAT:Input`,
    `Assets:Prepayments`, `Expenses:Freight`, `Expenses:FX:Rounding` (all in the §2.1 chart; the key originally
    said `Assets:VAT:Input` and was regenerated in Part 2 when the chart was fixed).
22. **Receipt and invoice numbering.** GR ids are chronological with `GR-4471` anchored to T1's
    receipt; invoice numbers are per-supplier sequences anchored so that the spec's fixed numbers
    (KT-2291, KT-2270, FT-118, GF-77, LB-301, HM-09) fall in place; GF-77A and CN-12 are fixed.
23. **Company identity.** Internal mail uses `tbeu.example`; supplier domains derive from their
    names; the real Kovak Trim domain is `kovaktrim.cz` and the lookalike `kovak-trim.cz`, as specified.
24. **Formats** are forced to exactly 90/120/30/60 and the JP and US suppliers are guaranteed rendered
    invoices so all eight templates are used.
25. **`make world` wipes the output directory first** so stale documents cannot survive a regeneration.
26. **mypy scope** is `core/` and `sim/models.py` as instructed (pydantic plugin, strict); the rest
    of `sim/` and `api/` is typed but only ruff-checked in Part 1.

## Part 2 — money, ledger, policy, packets

27. **Chart of accounts** lives in spec §2.1 and `core/accounts.py`; posting to any other name is
    `PostRejected(unknown_account)`. The Part 1 answer-key generator referenced `Assets:VAT:Input`; it now
    emits `Expenses:VAT:Input` and the world was regenerated (`world.jsonl` unchanged, `answer_key.json`
    updated). Supplier categories map to `Expenses:Materials:*`, `Expenses:Tooling`, `Expenses:Freight`.
28. **`parse_locale_amount`, `convert`, `allocate`.** `convert` goes through EUR (ECB rates are units per
    EUR), rounds ROUND_HALF_EVEN into the target currency's minor unit and returns the rate and date used;
    a missing date or currency raises `FxRateUnavailable`. `allocate` uses the largest-remainder method
    with ties broken by position, so splits always sum exactly and are deterministic.
29. **Check (2) and multi-currency entries.** "Per currency, Σdebit = Σcredit" is enforced exactly for
    single-currency entries. A multi-currency entry (an FX conversion, a EUR-funded CZK settlement) cannot
    balance per currency by definition, so it is governed by check (3): EUR equivalents must agree within
    ±1 cent and the residual is posted to `Expenses:FX:Rounding` on the same entry. `unknown_account` is
    raised during check (1) (line validation). A missing FX rate surfaces as `unbalanced_eur`.
30. **Controller posting scope.** The spec's `controller: Scope(approve=True)` has no `post` globs.
    `approve` grants posting to every account: the controller is the human with final say and must be
    able to post opening balances and reversals. `Scope.post` accepts set literals (the table is verbatim)
    and normalises to a frozenset.
31. **Duplicates raise.** Check (7) raises `PostRejected(duplicate)` carrying `existing_entry_id`; callers
    that replay can treat it as success. Nothing is written twice.
32. **Evidence must exist.** When the ledger is attached to an event store, every evidence event id must
    exist there (`no_evidence` otherwise). The bootstrap's evidence is its own `controller_decision`
    event (`BOOTSTRAP-OPENING`).
33. **`posted_at` is sim time.** The ledger takes a `now()` callable (the clock in later parts) and
    `post()` accepts `posted_at`; `balance(..., as_of_known_at)` answers "what did we believe at X".
34. **Periods.** `close_period` needs `approve` scope and `ap_control_reconciles()` (Σ open payables
    with status open/scheduled/held equals the `Liabilities:AP` credit balance per currency, and Σ
    payable retention equals `Liabilities:AP:Retention`). R7 accruals are the invoice agent's job in
    Part 3 and run before the close. Reopening requires a `controller_decision` event with
    `decision="reopen_period"` naming the month, is never exposed through the API, and is recorded in a
    `period_log` table (an addition; the four spec tables are unchanged). A `payables` table per §5.4
    and a `requested_by` column on `vendor_bank_accounts` (needed for §8's requester ≠ approver check)
    are the only other schema additions.
35. **Beancount export** annotates every non-EUR posting with `@@ <abs EUR equivalent> EUR`, opens all
    chart accounts on 2026-01-01 (bank accounts constrained to their currency), uses the entry id as a
    link (`^JE-000001`) and the cause type as a tag, and is written via temp file + `os.replace`. The
    ledger title option is `payguard`, so Fava serves it at `/payguard/`.
36. **Opening balances** are dated 2026-09-30 and valued for EUR equivalence at the first published
    rate of the window (2026-10-01), recorded as the lines' `rate_date`; the converter itself never
    falls back. The legacy Katowice Metal Forming invoices (KMF/2026/09/0412, 0413, 0417; EUR 62,480.00,
    71,150.12 and 53,800.00) are posted against `Equity:Opening` with open payables due 30 days after
    their September dates, so Part 4 reconciles `PAY-BATCH-0001` (EUR 187,430.12) instead of paying again.
    The bank generator now imports the §2.7 opening balances from `core/bootstrap.py` (single source).
37. **Vendor master seeding.** The bootstrap loads the 40 suppliers as vendors with one active, approved
    account each (`requested_via=onboarding`, `verified_via=contract`, approved by the controller, hold
    already elapsed, `on_chip=False` until Part 7 provisions). The duplicate record V-0033 is stored with
    `status=merged`, `merged_into=V-0007` (same VAT ID, later onboarding), which is how the agents resolve
    M1.
38. **Packets.** Packet ids derive from the draft's content hash, so a replayed `create_packet` returns
    the existing packet without a second `escalation` event. `decide_packet` records the first decision
    only; later calls return it unchanged. Expiry applies the safe no-op `hold` as a `controller_decision`
    by `auto_expiry`. `escalation.options` carries option ids; labels live in the packet record.
39. **New action type** `policy_violation` was added to the §5.1 `agent_action` enum for refusals that
    are not postings (scope, invariants, jumper).
