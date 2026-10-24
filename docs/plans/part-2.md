# Part 2 plan — money, ledger, policy, packets, bootstrap

Scope: `core/` only. No agents, LLM, signer or bank execution.

## Files
- `spec/PAYGUARD_SPEC.md` §2.1 — chart of accounts added verbatim; `core/accounts.py` — registry (`CHART`, `validate_account`, `UnknownAccount`, category→expense map, bank account by currency)
- `core/money.py` — add `convert(money, to_currency, rate_date, rates) -> Conversion` (ECB EUR base, Decimal, ROUND_HALF_EVEN) and `allocate(money, weights)` (largest remainder, exact sum)
- `core/ledger.py` — §5.2 tables verbatim (+ `seq`/`content_hash` on entries, append-only triggers, `periods`, extra `period_log` and `payables` per §5.4); `Ledger.post/reverse/balance/ap_open_items/ap_control_reconciles/close_period/reopen_period`, `PostRejected(reason_code, detail)`; Beancount export after every post (atomic)
- `core/vendors.py` — §5.3 tables (+ `requested_by` column, needed for §8's requested≠approved check) and data access
- `core/policy.py` — `Scope`, `PRINCIPALS` verbatim, glob scope match, `Action` kinds, `Policy.execute()` single side-effect path, `PolicyViolation`, vendor-account operations enforcing §5.3 invariants, `jumper_present()` config stub
- `core/packets.py` — `Packet` schema (§9 + severity/expires_at/auto_action), `packets` table, `create_packet`, `decide_packet` (idempotent), `expire_open_packets`
- `core/bootstrap.py` — opening bank balances (2026-09-30, vs `Equity:Opening`), legacy Katowice Metal Forming AP + open payables matching the 2 Oct batch, vendor master seeded from `suppliers.json`; idempotent
- `core/beancount_export.py` — writer used by the ledger; `make fava` serves `ledger/payguard.beancount` on :5000
- `sim/worldgen/answer_key.py` — `Assets:VAT:Input` → `Expenses:VAT:Input`; world regenerated

## Interfaces
- `Ledger(path, *, events: EventStore | None, fx: FxTable | None, now: Callable[[], datetime], beancount_path: Path | None)`
- `post(draft: JournalEntryDraft, principal: str, posted_at=None) -> JournalEntry`; checks in order: lines (one side, known account) → per-currency balance (single-currency entries) → EUR equivalence ±1 cent with residual to `Expenses:FX:Rounding` → period open → scope → evidence (≥1, ids must exist in the event store when one is attached) → content hash
- `reverse(entry_id, principal, cause_type, cause_id, effective_date) -> JournalEntry`
- `balance(account, as_of_effective_date, as_of_known_at=None) -> dict[currency, debit_minus_credit]`
- `Policy(ledger, vendors, events, jumper_present, now).execute(action, principal)`; refusals log `agent_action{post_rejected|policy_violation}`
- `create_packet(draft) -> Packet`, `decide_packet(packet_id, decision, note, decided_by) -> PacketDecision`

## Tests (written first)
unit: `test_money` (convert date/rounding/direction, allocate exact on 50 random splits), `test_accounts`, `test_ledger` (every reason code, FX residual, residual > 1 cent, reversal, duplicate, triggers, as_of_known_at, close/reopen), `test_policy` (every principal ± scope, requested≠approved, activation preconditions, on_chip), `test_packets` (idempotent decide, events).
integration: `test_beancount` (bootstrap + 20 posts → `bean-check` zero errors, Fava loads, realization balances == `ledger.balance`), `test_bootstrap` (idempotent, `ap_control_reconciles()`), Part 1 suite stays green after the key rename.

## Risks
- Beancount 3 / Fava wheels on this machine; `bean-check` must be runnable from the venv.
- §5.2 check (2) vs multi-currency FX entries: per-currency balance is enforced for single-currency entries; multi-currency entries balance in EUR equivalent (documented).
- `controller`'s `Scope(approve=True)` must be able to post opening balances: `approve` grants posting to every account (documented).
- mypy --strict now covers all of `core/`, which imports `sim.fx`.
