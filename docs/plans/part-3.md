# Part 3 plan — invoice agent, LLM boundary, runner, scoreboard

Scope: `agents/` (runner, LLM boundary, invoice agent), `core/rules.py`, `core/tax.py`, `core/score.py`,
scripted responders in `sim/people.py`, Step 0 principal fixes. No vendor/payment agent, no signer.

## Step 0 (Part 2 review)
- `PRINCIPALS`: add `bootstrap` (post `*`, only while the store has no `day_boundary`), restrict
  `controller` to `post={"Equity:Opening"}` + approve (close/reopen). Spec §8 table, `core/policy.py`,
  `core/ledger.py` (bootstrap check in the scope step), `core/bootstrap.py` posts as `bootstrap`.
- Runner treats `PostRejected(duplicate)` as a successful no-op.

## Files
- `core/tax.py` — entity facts (DE, 19 %): treatment by supplier country, VAT lines (input/output self-assessment), R11 detection
- `core/rules.py` — constants (§6) + pure rule helpers: variance bp, duplicate test, UoM normalisation, price-list lookup with gap detection, rounding delta, freight threshold, retention split, discount terms
- `agents/base.py` — `Agent` protocol, `Context` (the only route to `policy.execute`, emits `agent_action` with explanation), `Messenger` port for colleague queries
- `agents/runner.py` — cursor-driven drain in seq order, try/except → `agent_action{error}` + `system_error`, LLM prefetch with `asyncio.Semaphore(4)`, clock hook (`after_advance`)
- `agents/parsed.py` — `ParsedInvoice`/`ValidatedInvoice`/`ExtractionFailure`; `agents/documents.py` — document loading (PDF→150 dpi images, PNG, EML body+attachments, JSON)
- `agents/llm.py` — only module importing `anthropic`: `extract_invoice`, `explain`, `draft_message`; strict tool schema, cached system prompt, SDK retries (2, backoff), per-call log + cost table, extraction cache by sha256
- `agents/validate.py` — locale parsing, Σlines=net, net+VAT=gross, dates, currency, qty>0; retry-with-errors then R0
- `agents/structured_mapper.py` — de/cs/pl/en/ja synonym dictionary; fallback to LLM on unmapped required fields
- `agents/resolve.py` — name normalisation (§6), VAT-ID precedence, `merged_into`, five PO reference formats + quote numbers, SKU/description fallback
- `agents/state.py` — invoice status, facts, waits, PO-line consumption, accruals
- `agents/invoice_agent.py` — §7.3 with R0–R7, R11–R15, M3–M6 handling, bank-detail forwarding
- `core/score.py` — all eight check kinds, per-case status, `score_update` event
- `sim/people.py` — `ScriptedPerson.reply(query, sim_time)` (Jonas, Tomáš); `sim/clock.py` — `schedule()` + `after_advance`
- World: entity-based VAT (DE domestic, EU reverse charge, JP/US import) in `materialize.py`; A8/A12/A13 key checks updated; regenerate

## Rule → proof
| Rule | Test | Synthetic input → expected |
|---|---|---|
| R0 | `test_rules::test_r0_unreconciled_totals_hold` | Σlines ≠ net twice → `held`, escalation "unreadable invoice", no entry |
| R1 | `test_r1_short_pay` | billed 1000, received 800 → AP = 800×price, `short_pay_notice`, balance 200 recorded |
| R2a | `test_r2a_within_tolerance_posts_invoice_price` | +144 bp vs the agreed PO price → posted at invoice price |
| R2b | `test_r2b_variance_escalates` | +216 bp → escalation, `held`, no entry |
| R3 | `test_r3_no_receipt_asks_then_posts` / `_escalates_after_48h` | no receipt → `asked_colleague` + wait; receipt → posted; 48 h → escalation |
| R4 | `test_r4_duplicate_rejected` (+ `_within_50bp` for A15) | same vendor/total/±3 d/other number → `duplicate_rejected` citing original |
| R5 | `test_r5_unknown_vendor_held` | unresolved name+VAT → `held`, `onboarding_opened`, status `held_unknown_vendor` |
| R6 | `test_r6_credit_note_reverses_proportionally` | CN 10 % → Dr AP / Cr Expense allocated by `Money.allocate`, payable reduced |
| R7 | `test_r7_month_end_grni_accrual_and_reversal` | receipt, no invoice at month end → one GRNI accrual per PO; later invoice → reversal + post, cross-referenced |
| R11 | `test_r11_reverse_charge_billed_with_vat` | CZ supplier charges 21 % → post net + self-assessed VAT, escalation (VAT > €500) |
| R12 | `test_r12_rounding_difference` | printed VAT off by 3 minor → `Expenses:FX:Rounding`, no escalation |
| R13 | `test_r13_freight_without_po` | €1,450 → posted; €3,200 → buyer asked, posted after reply |
| R14 | `test_r14_invoice_dated_before_po` | invoice date < PO date → escalation, no entry |
| R15 | `test_r15_retention_split` | retention clause on the document → AP 90 % + AP:Retention 10 %, two payables |
| tax | `test_tax` | DE domestic / EU reverse charge / import lines |
| M3–M6 | `test_rules::test_m3_gap`, `test_m5_wrong_line`, `test_m6_uom_receipt` | gap → escalation naming both lists; wrong line → asked + reassigned; pc/box50 receipt → asked, no short-pay |

Other tests: `test_resolve`, `test_validate`, `test_structured_mapper`, `test_score`, `test_runner`,
`test_no_truth_in_agents`, `test_extraction_accuracy` (60 docs, ≥ 0.97), `test_invoice_agent_world`
(through 2026-11-05: T1–T6, T10; ambient ≥ 43/45; evidence; reconciliation daily; zero violations; bean-check)
and its offline twin `test_invoice_agent_world_offline` (same assertions, truth-backed extractor from
`tests/truth_llm.py`, no API).

## Risks
- **No API credentials on this machine** (`ANTHROPIC_API_KEY` unset, no `ant` profile). Everything builds
  and the structured/offline paths are tested; the 60-doc accuracy run and the full world run need a key.
- Current models reject `temperature`; the boundary passes it only to models that accept it and otherwise
  relies on strict schemas + effort (documented).
- Entity-based VAT changes the world (CZ/PL plant invoices become reverse charge); hash changes, Part 1 tests
  stay green because they do not pin the hash.
- Scope: `Assets:Prepayments` is outside every agent's scope; A13 nets in Part 4 (key updated).

## Changes made while implementing (see DECISIONS 40–52)
- Reference price for R2 is the agreed PO line price; the price list is used for the M3 gap only.
- R7 is one auto-reversing accrual per purchase order (answer key T10), reversed by the first invoice.
- Retention (R15) is detected from the document's notes/terms, not from the SKU or supplier category.
- Waits stay open after a "nothing received" reply until the invoice posts, is held or expires.
- `invoice_agent` may post `Liabilities:VAT:Output` (self-assessment); spec §8 updated.
- World: T8 order priced from the list (quantities solve EUR 2,100,000.00 exactly); the gap supplier's
  ordinary invoices are known from 21 Oct; `vendor_message.channel` gained `invoice`.
- Test kit: `tests/agentkit.py` (synthetic world + fake LLM), `tests/truth_llm.py` (offline extractor).
