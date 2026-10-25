# Part 5 plan — vendor agent, people, packets, controller phone

Scope: `agents/vendor_agent.py` (+ `agents/vendor_classify.py`, `agents/vendor_state.py`), the people
layer (`sim/people.py`: `People.reply`, scripted facts + persona wording, Marta as auto-controller,
ground-truth table), packet rendering, the controller-phone API, and the T7/T8 flows against the
stub. No dashboard, no hardware client.

## Step 0 (Part 4 review)
- 64 allowlist slots (`MAX_SLOTS = 64`); provisioning puts every active approved account on the chip,
  the queue is only for overflow; a slot-63 vector; RTL compares hashes sequentially from BRAM.
- The bank binds the native amount: it recomputes the native amount from the frame's EUR cents at the
  `rate_date` logged on `payment_requested`; a mismatch beyond one EUR cent (in native units) is
  `bank_rejected{amount_mismatch}` + `system_error`, and the payables are held.
- `chip_response` separates `chip_reason` (one code from the chip) from `desk_annotations` (host
  state); `reasons` stays the union. T8's check reads both fields.

## Channel rule (recorded in DECISIONS 70)
Spec §7.4 prints `email → phone; portal → email; invoice → phone`. That matrix contradicts §2.6
(T7 "verified by phone"), §2.8/§15 (Petra's email confirmation for T8) and the answer key (T7 packet
recommends `approve`, T8 `request_phone_verification`). The implemented matrix is the one the
headline cases need: `portal → phone`, `email → email to the contact on file (never the sender)`,
`invoice → phone`; after an email verification the packet recommends phone verification, and R9
recovery uses the phone (the "channel rule" of §15 step 3).

## T8 sequence (every event, who emits it)
```
world        vendor_message{email, accounts@kovak-trim.cz, IBAN CZ62…1954, cc Petra}   Oct 15 09:12
vendor_agent agent_action{domain_flagged}          rule null   kovak-trim.cz vs kovaktrim.cz (distance 1)
vendor_agent agent_action{bank_change_opened}      R8         pending account VBA-…, hold until Oct 18 09:12
vendor_agent agent_action{verification_requested}  R8         email to petra.vlcek (contact on file)
world        colleague_reply{petra.vlcek, email, "Confirmed, they told me last week."}  Oct 15 10:40
             (ground_truth: mailbox_compromised=true; the people layer schedules nothing because the
              world reply exists in the thread)
vendor_agent agent_action{verification_received}   R8         verified_via=email
vendor_agent escalation{kind=approval, "Approve bank change for Kovak Trim s.r.o.?",
             rows [Request, Verification, Mismatch, Pending payments], recommendation request_phone_verification}
controller   controller_decision{approve}  (approval variant; Marta denies in the auto variant)
controller   agent_action{account_approved}        R8         approved_by=controller
vendor_agent agent_action{activation_scheduled}    R8         timer at hold_until (Oct 18 09:12)
vendor_agent timer{activate VBA-…}
world        vendor_message{email, in-thread follow-up, same IBAN}  Oct 16 08:05 → agent_action{matched: duplicate request}
payment_agent payment_requested{PAY-…, KT-2270, EUR 2,100,000.00 → new hash}  Oct 17 06:30 (due date)
stub         chip_response{ok:false, chip_reason:not_allowlisted, desk_annotations:[in_hold]}
payment_agent agent_action{payment_held}           R10        payables held not_allowlisted
vendor_agent agent_action{account_suspended}       R9         the pending account
vendor_agent agent_action{account_restored}        R9         prior account stays active
vendor_agent agent_action{payment_requeued}        R9         payables held awaiting_controller
vendor_agent escalation{kind=anomaly, severity urgent, rows [Original request, Verification path,
             Chip rejection, Account on file since 2016-…], recommendation call_supplier_on_contract_number}
```

## Files
- `agents/vendor_classify.py`: `classify(payload, known_vendor)`, `extract_ibans`, `extract_vat_ids`, `lookalike(from_domain, known_domain)`.
- `agents/vendor_state.py`: verification waits, packet rows, onboarding cases, scheduled activations.
- `agents/vendor_agent.py`: §7.4 flows R8/R9/R5-onboarding/M2; `Context.with_principal` for controller-side actions.
- `sim/people.py`: `People` (reply/deliver/ask), templates, `AutoController` (Marta), `GroundTruth`.
- `agents/llm.py`: `persona_reply(person, context)` when a key exists.
- `core/packets.py`: `render(packet)`; `core/policy.py`: `CreateVendorAction`, `ActivateVendorAction`, approved-pending accounts payable.
- `api/main.py`: `/packets`, `/packets/{id}`, `/packets/{id}/decide`, `/vendors/{id}`, `/vendors/{id}/accounts/history`, `/judge/message`.
- `sim/models.py`: `timer` event, `portal` reply channel, `confirms_bank_change_for`/`successor_id` scripts.

## Tests
`tests/unit/test_vendor_classify.py`, `test_vendor_agent.py`, `test_people.py`, `test_packets_render.py`,
`tests/integration/test_t7_t8.py` (auto-controller variant and approval variant), plus Part 4 vectors/bank/chip tests.

## Risks
- The 72-hour hold is enforced by the chip (and mirrored as a desk annotation); after the controller's
  approval the desk already routes payments to the approved account (that is how T8 reaches the chip).
- Due payments may use the EUR overdraft line; discount payments may not (T9 unchanged).
- Timers fire at the next drain; the harness drains four times a day, so "exactly at hold_until" is
  event time of the timer, not of the drain.

## What changed while building (all recorded in DECISIONS 68–81)
- The world's Petra reply (10:40) predates the drain that opens the change (12:00), so the people
  layer treats any world reply in the thread as the answer; the runtime never generates a second one.
- Approving an email-verified change cannot activate it (policy: same channel); the desk phones the
  contact at hold end and activates on a confirmed call. Found by the approval-variant run, where the
  judge's 20 Oct message to Petra was approved and the scheduled activation failed daily.
- R9 ignores an active account the chip simply does not carry yet (provisioning queue); it fires for
  pending accounts and for just-activated accounts that are on the chip.
- The T9 question is `discount_escalated`, not `held`, and its shortfall is the payment minus
  positive cash; after KT-2270 draws the overdraft, the deny variant otherwise raised nine holds that
  scored as false positives.
- Judge-injected vendors, messages and threads are excluded from the false-positive count.
- The harness drains at 06:30, 12:00, 18:00 and midnight.
- KT-2270 is due 17 Oct in the world data (the spec's "Friday"); the run targets the approved account
  on the due date.
