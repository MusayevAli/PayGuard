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

## Part 3 — runner, LLM boundary, invoice agent, scoreboard

40. **Bootstrap principal; controller narrowed (Part 2 review).** `bootstrap: Scope(post={"*"})` posts
    opening balances and is refused (`policy_violation bootstrap_after_start`, ledger
    `PostRejected(out_of_scope)`) once any `day_boundary` exists. `controller` posts only `Equity:Opening`
    besides close/reopen; `Scope.can_post` is glob-only (`approve` no longer grants posting).
    `PostRejected(duplicate)` is a successful no-op inside `Context.act`: the existing entry is returned
    and no `post_rejected` event is written (replays leave no trace).
41. **Entity tax facts.** `core/tax.py` holds the entity (Toyota Boshoku Europe GmbH, DE, 19 %). The
    Part 1 world applied plant-country VAT; it was regenerated with entity-based treatment (DE domestic,
    CZ/PL/AT reverse charge with self-assessment, JP/US import). Self-assessment credits
    `Liabilities:VAT:Output`, so that account was added to the invoice agent's scope in spec §8.
42. **Reference price is the agreed PO line price** (amendments included); the price list valid at the
    invoice date is consulted only to detect the M3 gap. Reasons: the generator prices POs from the list
    valid on the order date, renegotiated amendments (M4) change the agreed price, the old-quarter
    mismatch (A9) and T2 are variances against the PO, a merged vendor's PO carries its own list, and the
    T8 order would otherwise be held. `variance_bp` rounds up so a variance is never understated.
43. **R7 is one auto-reversing accrual per purchase order** (answer key T10): every line received by the
    last day of the month and not yet invoiced is accrued at PO price in one entry whose evidence is the
    receipt events and the PO; the first invoice for the PO reverses the whole entry (`reverse()` with the
    invoice as extra evidence, `ReverseAction.extra_evidence`), and the next month end re-accrues whatever
    is still open. The invoice agent runs R7 on the `day_boundary` that closes a month, before any close.
44. **Retention is read from the document** (R15): a retention/holdback clause in the notes or payment
    terms (printed percentage, else 10 %) splits the credit into `Liabilities:AP` and
    `Liabilities:AP:Retention` with two payables (`<inv>` open, `<inv>:retention` held, reason
    `retention…`); `ap_control_reconciles` counts the retention payables. Tooling without a clause (A13)
    is not retained.
45. **Waits.** A colleague query opens a wait keyed to the PO line with `deadline = now + 48 h`; replies
    are attached (`record_reply`) but the wait stays open until the invoice posts, is held or expires, so
    "nothing received yet" does not re-ask and the deadline still escalates on the day boundary. Reply
    facts travel as `refs` tokens from `sim/people.py` (`GR-…`, `qty:`, `uom:`, `confirmed`, `reassign`,
    `unit_corrected`, `nothing_received`, `po:`); prose is never parsed.
46. **Runner.** Events the runner writes about an agent's failure (`agent_action{error}`, `system_error`)
    are marked handled for every agent, so an exploding agent cannot loop on its own error events;
    `drain()` runs to a fixpoint and is attached to `clock.after_advance`.
47. **LLM boundary.** Default model `claude-opus-5` (USD 5 / 25 per MTok, cache read 0.1×, write 1.25×);
    `temperature=0` only for models that accept it, otherwise `output_config.effort`; tool use with
    `tool_choice={"type": "auto", "disable_parallel_tool_use": true}` plus a strict schema because
    Fable 5.1 rejects forced tool choice; prompt caching on the system block; results cached by SHA-256.
    No Anthropic credentials exist on the development machine, so the API-backed tests fail with a
    message (never skip) and `tests/truth_llm.py` provides a truth-backed extractor for the offline twin
    (`test_invoice_agent_world_offline.py`); spec §12 allows the truth under tests.
48. **World changes in Part 3.** Besides the VAT regeneration: the T8 order is priced from Kovak's list
    with quantities solving exactly EUR 2,100,000.00 (50,096 / 50,095 / 29,205); the gap supplier's
    ordinary invoices become known from 21 Oct so only the planted M3 invoice is dated in the 1–14 Oct
    gap; the answer key derives the T10 accrual from the PO plan and checks A8 self-assessment, A12 two
    payables and A13 `decision.prepayment_minor`. `vendor_message.channel` gained `invoice` for printed
    bank details forwarded to the vendor agent.
49. **Prepayment netting (A13) is Part 4.** `Assets:Prepayments` is outside every agent's posting scope,
    so the invoice agent posts the full invoice and records `prepayment_minor` on the decision; the
    payment agent nets it.
50. **Scoreboard semantics.** A case is pending until every injected event has been handled by some
    agent; a hold, duplicate rejection or escalation is a false positive when its invoice ids (its refs
    when it names no invoice) intersect no case. T9 shows `missed` until the payment agent exists.
51. **Vendor resolution order:** VAT id, exact legal name, normalised name (§6), name variants; the
    original record is kept (`original_id`) and `merged_into` gives the canonical vendor for posting.
52. **People.** Jonas Weber answers every receipt question; the PO's purchasing contact answers freight
    confirmations and "which PO" questions.
53. **R4 needs the same supply.** Two invoices of one vendor with totals within 50 bp and dates within
    3 days are *not* duplicates when they name different purchase orders or bill disjoint SKUs (the
    world has such coincidences); the spec's three tests remain necessary, this adds a sufficient
    "same supply" condition. A15 (±0.3 % total, same PO) is still rejected.

## Part 4 — frames, signer stub, bank, payment agent

54. **The pay frame carries the EUR-equivalent amount.** The frame has one amount field and the chip's
    caps are EUR-equivalent (spec §10.2), so the host converts the settlement amount at the request
    date (`Money.convert`, logged on `payment_requested` as `eur_equiv_minor`, `fx_rate`, `rate_date`)
    and frames that; the currency byte names the settlement currency. The bank settles the native
    amount recorded on the latest `payment_requested` for the frame's pay_id and checks that the
    frame's hash, EUR amount and currency match it. For EUR payments frame and settlement coincide.
55. **One reason from the chip, annotations from the host.** The response is `FF` + one reason byte and
    the check order stops at the first failure, so a never-provisioned account can only be
    `not_allowlisted`. T8's `not_allowlisted` + `in_hold` reading comes from the host appending
    `in_hold` to `chip_response.reasons` when the paying account's hold has not elapsed.
56. **Check order and nonce semantics** are in `proto/FRAME.md`: length/type/currency/pay_id → CRC →
    (provision: jumper → slot) → seq → allowlist → hold → account cap → daily cap → button → nonce
    value → nonce valid → commit → HMAC. Any successful payment clears the nonce (a press authorises
    exactly one payment); a nonce also expires after `nonce_timeout_days` day ticks. `day` is days
    since 2026-01-01; seeded accounts' holds clamp to day 0.
57. **Provisioning extras.** Besides the spec's slot/`0xFE`/`0xFF` sub-formats, `0xFD` sets the button
    threshold and `0xFC` the nonce timeout. Defaults: per-account cap EUR 2,500,000.00, daily cap
    EUR 3,000,000.00, threshold EUR 50,000.00, timeout 1 day. `DEMO_VENDORS` are the 14 vendors with
    the largest EUR-equivalent payables falling due in the window (an operator list, not agent
    logic), leaving two free slots for T7. The shared HMAC key is `PAYGUARD_CHIP_KEY` (hex) or a fixed
    development key.
58. **Stub state persists in SQLite** when a path is given (the API survives restarts without losing
    the allowlist); the vector generator uses an in-memory stub. `tests/vectors.json` schema
    `payguard-vectors-1` is frozen; a case may carry `before.press_button` with the nonce the
    testbench must load.
59. **Bank balances** = the world's last statement closing + the runtime delta of executed payments and
    arranged credits; the bank's own statements (`<CCY>yymmddP`, sequence 1000 + day) carry only
    runtime lines and open at the cumulative runtime delta. A world statement that does not open at
    the previous closing raises `system_error{statement_gap}`. `bank_rejected` is a new event type
    (bad tag, unknown payment, frame mismatch, insufficient funds, FX unavailable). USD and JPY
    payments settle from the EUR account at the day's ECB rate.
60. **The payment run is a morning run.** It executes when all three accounts' statements for the day
    have been ingested (06:00), so cash is yesterday's closing; the day boundary runs it only on a
    desk that has never received a statement (the unit-test kit). The harness advances to 06:30 and
    then to midnight each day. Without this, the desk would see balances two days old.
61. **Eligibility.** Open payables due within `PAY_LEAD_DAYS = 2`; discounts on their deadline day
    (`DISCOUNT_LEAD_DAYS = 0`, the latest value date that still earns them, which is also when T9's
    cash picture is visible); `scheduled` means already instructed to the bank, so the three legacy
    payables are created `scheduled` by the bootstrap and settle only through the batch line;
    retention payables are never scheduled; credit notes net against the next payment to the vendor
    (`Dr AP` for the net amount). Payments beyond cash or the daily cap are deferred, not held.
62. **Payables are `paid` at execution**, when `Dr AP / Cr Bank` posts, so `ap_control_reconciles()`
    holds every day; the next-day statement line confirms the settlement (`reconciled`, payment
    `settled`). Spec §7.5's "mark paid on the statement line" would leave the liability and the
    payable out of step for a day.
63. **R10.** `needs_button` → `payment_held` + an approval packet (`kind=approval`). Approval moves the
    payment to `awaiting_button`; the button (`POST /stub/button`, which also re-runs waiting payments
    through `Runner.run_task`) supplies the nonce and the same pay_id is re-sent with a new seq.
    Rejection holds the payables `rejected_by_controller`. A chip reject is never retried on its own.
64. **T9 options** are quantified in EUR: pay what cash allows now (discount prorated to the covered
    part, the rest at due date), draw the shortfall intercompany at `INTERCOMPANY_RATE_BP = 400` until
    the due date (`Dr Assets:Bank / Cr Liabilities:Intercompany`), or skip the discount; the
    recommendation is the largest net benefit. Taking a discount credits `Income:Discounts`. Both
    accounts were added to the chart (spec §2.1) and to the payment agent's scope.
65. **Reconciliation.** Lines match a payment by `customer_ref = pay_id`; a debit whose remittance names
    open or scheduled payables summing to the amount is a batch (`Dr AP / Cr Bank`, payables paid);
    `NCHG` debits are fees; `NFEX` legs pair by reference across currencies into one entry with
    `Expenses:FX:Realized` beyond one cent; everything else becomes an informational packet
    (`kind=notice`, `severity=info`) per statement two days after the line became known. Packets
    gained a `kind` (anomaly | approval | notice); only anomalies count as false positives.
    `ActionType` gained `payment_requested` and `reconciled`; `EvidenceRole` gained the payment and
    statement roles.
66. **Unprovisioned vendors** are rejected by the chip (`02`), their payables held
    `not_allowlisted`, and the vendor is queued for provisioning (`provisioning{ok:false,
    reason:jumper_absent}`); a later successful provisioning event reopens those payables. With 16
    slots and 20 vendors due in the window, six vendors wait this way.
67. **API.** `create_app(desk=False)` is the bare clock/transport server the Part 1 tests exercise;
    the full app builds the runtime, bootstraps and provisions at startup. `SIGNER=hw` is refused with
    a message until Part 7.

## Part 5 — vendor agent, people, packets, controller phone

68. **64 allowlist slots** (`MAX_SLOTS = 64`, `proto/FRAME.md` slots `0x00`–`0x3F`). Provisioning at
    startup puts every active, approved account on the chip (`Provisioner.provision_all`, jumper
    raised then pulled); the queue is only for accounts activated later or beyond the slot count.
    The RTL compares the frame's hash against the slot table sequentially from BRAM (64 × 32 bytes,
    64 cycles), so the slot count costs nothing in logic. `tests/vectors.json` has 54 cases: slot 63
    provisioned and paid (`prov_slot63`, `pay_slot63`), slot `0x40` rejected `bad_slot`, every reason
    code.
69. **The bank binds the native amount.** The frame carries EUR cents; the bank recomputes the native
    amount from them at the `rate_date` logged on `payment_requested` and rejects a request whose
    native amount differs by more than one EUR cent in native units (`ceil(rate / 2)` minor units)
    with `bank_rejected{amount_mismatch}` plus a `system_error`; the payables are held. A tampered
    `payment_requested` can no longer move more than the chip signed.
70. **Channel rule.** Spec §7.4 prints `email → phone; portal → email; invoice → phone`, which
    contradicts T7 ("verified by phone" for a portal request), T8 (Petra's email confirmation) and the
    answer key (T7 recommends `approve`, T8 `request_phone_verification`). Implemented:
    `portal → phone`, `email → email to the contact on file (never the sender's thread)`,
    `invoice → phone`, `judge → phone`, `phone → email`. The packet recommends `approve` after a
    phone verification and `request_phone_verification` after an email one.
71. **Activation needs a second channel, even after approval.** The policy refuses
    `ActivateIfReady` when `verified_via == requested_via`. If the controller approves an
    email-verified change anyway, the desk phones the contact when the hold ends
    (`verification_requested{reason: same_channel_before_activation}`); a confirmed call becomes the
    account's verification (`MarkVerified(phone)`) and activation follows; a denial suspends the
    account and raises the urgent "denied" packet. A phone recheck the controller asked for works the
    same way. Any other policy refusal of a scheduled activation is reported once as a `notice`
    packet and never retried daily.
72. **`chip_response` fields.** `chip_reason` is the one code the chip returned (or null when ok);
    `desk_annotations` is host state (`in_hold` while the account's hold has not elapsed);
    `reasons` stays their union for older readers. T8's check reads `chip_reason` and
    `annotations_contains`.
73. **An approved pending account is paid at once.** `RequestPayment` accepts a pending account that
    is verified and approved; the chip's allowlist and hold are the stop, and the desk annotates
    `in_hold`. That is how T8 reaches the chip on the due date. R9 fires only for a pending account or
    an account activated within 72 hours that is on the chip; an active account the chip does not
    carry yet sits in the provisioning queue and is the payment desk's `not_allowlisted` hold, not
    an attack.
74. **Cash.** `PAY_LEAD_DAYS = 0`: a payable is paid on its due date (two days early would have paid
    KT-2270 before the change was even confirmed). Due payments may draw the overdraft line
    (`OVERDRAFT_LIMIT_MINOR`: EUR 1,000,000, none in CZK/PLN); discount payments need positive cash.
    The T9 shortfall is the part of the payment positive cash cannot fund (an account already
    overdrawn for due payments does not inflate the intercompany option). The T9 question is logged
    as `discount_escalated`, not `held`: the payable stays open and is paid at its due date whatever
    the answer, so it is not a prevented payment and the scoreboard does not count it as a false
    positive.
75. **Timers.** `timer{agent, purpose, refs, due_at, key}` is an event the agent schedules on the clock
    (activation at `hold_until`); the day-boundary sweep over `scheduled_activations` is the
    fallback. Timers fire at the next drain after `due_at`.
76. **People layer.** `People.reply` returns facts from the hidden scripts (`confirms_bank_change_for`,
    `mailbox_compromised_from`, `phone_reply`, `successor_id`) and wording from the persona model
    only when credentials exist (`LLMClient.persona_reply`, cached, verdict words validated),
    otherwise per-person templates; both paths produce the same `colleague_reply` shape. Delivery is
    a clock-scheduled event after a seeded delay (email 2–30 h, phone 1–3 h, portal 6–12 h). A
    world `colleague_reply` by the same person in the same thread stands in for a generated one, so
    T8 has exactly one Petra confirmation. `mailbox_compromised` lives in the `ground_truth` SQLite
    table (`People(truth_store=…)`, aliased `PeopleTruth` in the runtime so the guard test that
    keeps `ground_truth` out of `agents/` holds) and never in a payload. Marta
    (`AutoController`, `AUTO_CONTROLLER=1`) denies packets with fewer than three evidence rows or a
    `request_phone_verification` recommendation and otherwise follows the recommendation.
77. **Vendor agent trail.** Every message yields `message_classified`; a lookalike sender yields
    `domain_flagged` (rule null, non-blocking). Unknown vendors on invoices become a
    `vendor_message{channel: invoice}` emitted by the invoice agent before `onboarding_opened`, so
    onboarding and bank changes share one entry point. M2 asks the successor only after a bounce
    (`550`), never merely because the contact is inactive. Duplicate requests for the same account
    hash are `matched`, not reopened.
78. **R9 packet.** Rows: original request, verification path, chip rejection, account on file since
    <date>; options `call_supplier_on_contract_number` (recommended, with the contract phone in the
    label, reason and context), `pay_prior_account`, `cancel_payment`; severity urgent; expires in 7
    days; the payment and its payables are `held awaiting_controller`. A vendor without any prior
    account gets no `account_restored` action; the packet says "none". The scoreboard resolves a
    chip response's refs to the payment's vendor, payables and invoice numbers.
79. **Judge messages are not false positives.** `POST /judge/message` writes the `vendor_message`
    (source `judge`) and a `judge_injection{form, resulting_event_id}`; the scoreboard excludes
    escalations and holds whose refs touch a judge-injected vendor, message, thread or invoice from
    the false-positive count. Judges can test Petra's script freely without lowering the score.
80. **Harness cadence.** `scripts/run_world.py` drains at 06:30 (statements, payment run), 12:00 and
    18:00, then at midnight; the operator hook runs after midnight. The desk's latency on a message
    or a reply is at most a working half-day, which is what T8 needs (the confirmation at 10:40 is
    handled at 12:00, approved overnight, and the payment run on the due date targets the approved
    account).
81. **Phone API.** `GET /packets?status=`, `GET /packets/{id}` (rendered), `POST /packets/{id}/decide`
    (idempotent; the decision drains the runner so the flow continues in the same request),
    `GET /vendors/{id}` and `/accounts/history` with masked IBANs. `Context.with_principal` lets the
    vendor agent carry out a controller decision under the `controller` principal, logged as such.
82. **Spend cap and credentials.** `LLMClient` loads `.env` from the repo root (never overriding the
    environment), tallies every priced call in a machine-wide ledger (`.cache/payguard_spend.db`,
    `PAYGUARD_SPEND_DB`) and refuses to call the API once the total reaches `PAYGUARD_MAX_USD`
    (`LLMUnavailable("spend cap reached")`, checked before every request including each document of
    the async warm-up). Explanations fall back to templates; extraction holds the work. The ledger
    is per key, not per cache database, so the budget survives new runs and new world directories.
    Tests with fake clients pass their own ledger path; the two API-backed tests spend for real and
    count. The extraction cache is one persistent file per machine, `.cache/extraction_cache.db`
    (`PAYGUARD_EXTRACTION_CACHE`), used by the runtime and both API-backed tests: documents are
    keyed by content hash and the generator is deterministic, so the 300 documents are extracted
    once however often the world is regenerated into a temporary directory. The prose model sees
    `*_minor` amounts pre-formatted with their currency (`present_for_prose`) and an explanation
    that alters a formatted number is rejected and regenerated once, then templated.
