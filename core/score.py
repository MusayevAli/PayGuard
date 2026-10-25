"""Scoreboard (spec §12, §2.9): evaluates every answer-key check against the event store, the
ledger and the vendor master, tallies headline/ambient cases, and emits ``score_update``.
This module (and tests) may read ground truth; agents never do."""

from __future__ import annotations

import json
from collections.abc import Callable
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict

from core.events import EventStore, canonical_json, make_event
from core.ledger import JournalEntry, Ledger
from core.money import FxRateUnavailable, Money, convert
from core.vendors import VendorMaster, account_hash
from sim.fx import FxTable
from sim.models import AnswerKey, AnswerKeyEntry, Event, Invoice

PREVENTING_ACTIONS = ("duplicate_rejected", "held")


class CheckResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    kind: str
    ok: bool
    detail: str


class CaseStatus(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    id: str
    tier: str
    case: str
    status: str  # caught | missed | pending
    checks: list[CheckResult]


class Tally(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    caught: int = 0
    missed: int = 0
    pending: int = 0
    fp: int = 0


class ScoreReport(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    as_of: datetime
    headline: Tally
    ambient: Tally
    euros_at_risk_prevented_minor: int
    entries_without_evidence: int
    cases: list[CaseStatus]


def _resolve(payload: Any, path: str) -> Any:
    cur = payload
    for part in path.split("."):
        if isinstance(cur, dict) and part in cur:
            cur = cur[part]
        else:
            return None
    return cur


def match_where(payload: dict[str, Any], where: dict[str, Any]) -> bool:
    for key, expected in where.items():
        if key.endswith("_contains_all"):
            value = _resolve(payload, key[: -len("_contains_all")])
            if value is None:
                return False
            if not all(
                (x in value) if isinstance(value, list) else (str(x) in str(value))
                for x in expected
            ):
                return False
        elif key.endswith("_contains"):
            value = _resolve(payload, key[: -len("_contains")])
            if value is None:
                return False
            if isinstance(value, list):
                if expected not in value:
                    return False
            elif str(expected) not in str(value):
                return False
        elif _resolve(payload, key) != expected:
            return False
    return True


class Scoreboard:
    def __init__(
        self,
        key: AnswerKey,
        *,
        store: EventStore,
        ledger: Ledger,
        vendors: VendorMaster | None = None,
        truth: list[Invoice] | None = None,
        fx: FxTable | None = None,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        self._key = key
        self._store = store
        self._ledger = ledger
        self._vendors = vendors
        self._truth = {i.id: i for i in truth} if truth else {}
        self._truth_by_number = (
            {(i.supplier_id, i.invoice_number): i for i in truth} if truth else {}
        )
        self._fx = fx
        self._now: Callable[[], datetime] = now or (lambda: datetime.now(UTC))
        self._last_signature: str | None = None

    # ------------------------------------------------------------------ checks

    def _entries_matching(self, entry_match: dict[str, Any]) -> list[JournalEntry]:
        out = []
        for entry in self._ledger.entries():
            ev = entry_match.get("evidence_contains")
            if ev and not any(e.event_id == ev for e in entry.evidence):
                continue
            cause = entry_match.get("cause_id")
            if cause and entry.cause_id != cause:
                continue
            desc = entry_match.get("description_contains")
            if desc and desc not in entry.description:
                continue
            out.append(entry)
        return out

    def _check(self, check: Any) -> CheckResult:
        kind = check.kind
        if kind == "journal_amount":
            entries = self._entries_matching(check.entry_match.model_dump())
            for entry in entries:
                for ln in entry.lines:
                    if ln.account != check.account:
                        continue
                    if check.currency and ln.currency != check.currency:
                        continue
                    if check.debit_minor is not None and ln.debit_minor != check.debit_minor:
                        continue
                    if check.credit_minor is not None and ln.credit_minor != check.credit_minor:
                        continue
                    return CheckResult(kind=kind, ok=True, detail=f"{entry.id} {check.account}")
            found = [
                (e.id, [(ln.account, ln.debit_minor, ln.credit_minor) for ln in e.lines])
                for e in entries
            ]
            return CheckResult(
                kind=kind,
                ok=False,
                detail=f"no line {check.account} debit={check.debit_minor} credit={check.credit_minor} in {found[:3]}",
            )
        if kind == "journal_count":
            n = len(self._entries_matching(check.entry_match.model_dump()))
            return CheckResult(
                kind=kind, ok=n == check.count, detail=f"{n} entries, expected {check.count}"
            )
        if kind in ("event_exists", "no_event"):
            hits = [
                e
                for e in self._store.query(type=check.type, limit=100000)
                if match_where(e.payload, check.where)
            ]
            ok = bool(hits) if kind == "event_exists" else not hits
            return CheckResult(
                kind=kind, ok=ok, detail=f"{len(hits)} matching {check.type} for {check.where}"
            )
        if kind == "payable_status":
            for p in self._ledger.payables():
                if p.inv_id != check.inv_id or (
                    check.status is not None and p.status != check.status
                ):
                    continue
                if check.discount_bp is not None and p.discount_bp != check.discount_bp:
                    continue
                if (
                    check.retention_minor is not None
                    and p.retention_minor != check.retention_minor
                    and p.amount_minor != check.retention_minor
                ):
                    continue
                return CheckResult(kind=kind, ok=True, detail=p.id)
            return CheckResult(
                kind=kind, ok=False, detail=f"no payable {check.inv_id} with status {check.status}"
            )
        if kind == "vendor_account_status":
            if self._vendors is None:
                return CheckResult(kind=kind, ok=False, detail="no vendor master attached")
            acct = self._vendors.account_by_hash(check.vendor_id, account_hash(check.iban))
            ok = acct is not None and acct.status == check.status
            return CheckResult(
                kind=kind,
                ok=ok,
                detail=f"{acct.status if acct else 'absent'}, expected {check.status}",
            )
        if kind == "chip_response":
            for e in self._store.query(type="chip_response", limit=100000):
                body = e.payload
                if not match_where(
                    {**body, "refs": self._payment_refs(str(body.get("pay_id", "")))}, check.where
                ):
                    continue
                if body.get("ok") != check.ok:
                    continue
                if check.chip_reason is not None and body.get("chip_reason") != check.chip_reason:
                    continue
                if not all(
                    a in body.get("desk_annotations", []) for a in check.annotations_contains
                ):
                    continue
                if all(r in body.get("reasons", []) for r in check.reasons_contains):
                    return CheckResult(kind=kind, ok=True, detail=e.id)
            return CheckResult(
                kind=kind,
                ok=False,
                detail=f"no chip_response ok={check.ok} reasons {check.reasons_contains} for {check.where}",
            )
        if kind == "packet_exists":
            for e in self._store.query(type="escalation", limit=100000):
                if match_where(e.payload, check.where) and (
                    check.recommendation is None
                    or e.payload.get("recommendation") == check.recommendation
                ):
                    return CheckResult(kind=kind, ok=True, detail=e.payload.get("packet_id", e.id))
            return CheckResult(
                kind=kind,
                ok=False,
                detail=f"no escalation for {check.where} recommendation={check.recommendation}",
            )
        return CheckResult(kind=kind, ok=False, detail="unknown check kind")

    def _case(self, entry: AnswerKeyEntry) -> CaseStatus:
        injected = [self._store.get(i) for i in entry.injected_event_ids]
        inserted = [e for e in injected if e is not None]
        handled = all(
            self._store.handled_by_any(e.id)
            for e in inserted
            if e.type in ("invoice_received", "vendor_message", "colleague_reply")
        )
        checks = [self._check(c) for c in entry.checks]
        if len(inserted) < len(injected) or not handled:
            status = "caught" if all(c.ok for c in checks) and inserted and handled else "pending"
        else:
            status = "caught" if all(c.ok for c in checks) else "missed"
        return CaseStatus(
            id=entry.id, tier=entry.tier, case=entry.case, status=status, checks=checks
        )

    # ------------------------------------------------------------------ report

    def _payment_refs(self, pay_id: str) -> list[str]:
        """What a chip response is about: the payment, its vendor, payables, invoices and numbers."""
        refs = [pay_id]
        for req in self._store.query(type="payment_requested", limit=100000):
            if req.payload.get("pay_id") != pay_id:
                continue
            refs.append(str(req.payload.get("vendor_id")))
            payable_ids = {str(x) for x in req.payload.get("payable_ids", [])}
            for p in self._ledger.payables():
                if p.id in payable_ids:
                    refs.append(p.inv_id)
                    inv = self._truth.get(p.inv_id)
                    if inv is not None:
                        refs.append(inv.invoice_number)
        return list(dict.fromkeys(refs))

    def _judge_refs(self) -> set[str]:
        """Vendors, messages and threads the judge injected at the tablet: the desk's reaction to
        them is what was asked for, never a false alarm on the planted world."""
        refs: set[str] = set()
        for e in self._store.query(type="judge_injection", limit=100000):
            form = e.payload.get("form") or {}
            refs.update(str(form[k]) for k in ("supplier_id", "supplier_name") if form.get(k))
            rid = e.payload.get("resulting_event_id")
            res = self._store.get(str(rid)) if rid else None
            if res is not None:
                refs.add(res.id)
                refs.update(
                    str(res.payload[k])
                    for k in ("vendor_id", "message_id", "thread_id", "invoice_id")
                    if res.payload.get(k)
                )
        return refs

    def _false_positives(self) -> int:
        refs_by_case: list[set[str]] = [set(e.refs) for e in self._key.headline + self._key.ambient]
        judge = self._judge_refs()
        fp = 0
        for ev in self._store.query(type="escalation", limit=100000) + [
            e
            for e in self._store.query(type="agent_action", limit=100000)
            if e.payload.get("action_type") in PREVENTING_ACTIONS
        ]:
            if ev.type == "escalation" and ev.payload.get("kind", "anomaly") != "anomaly":
                continue  # approvals (button, bank change) and notices are controls, not alarms
            refs = set(ev.payload.get("refs", []))
            # an action about an invoice is judged by its invoice ids alone: sharing a vendor or a
            # purchase order with a planted case does not make a hold on another invoice legitimate
            invoice_refs = {r for r in refs if r.startswith("INV-")}
            if (invoice_refs & judge) or (not invoice_refs and refs & judge):
                continue  # the judge asked for this one
            probe = invoice_refs or refs
            if not any(probe & case_refs for case_refs in refs_by_case):
                fp += 1
        return fp

    def _at_risk(self, case: CaseStatus, entry: AnswerKeyEntry) -> int:
        if case.status != "caught" or not self._truth:
            return 0
        invoices = [self._truth[r] for r in entry.refs if r in self._truth]
        if not invoices:
            return 0
        inv = invoices[-1]
        amount = 0
        if entry.case in ("T1",):
            amount = max(0, inv.gross_minor - 1_112_000)
        elif entry.case in ("T2", "A9"):
            amount = sum(
                abs(ln.net_minor) // 50 for ln in inv.lines
            )  # the price variance share (2 %)
        elif entry.case in ("T3", "A15", "T6", "A14", "T8", "M3"):
            amount = abs(inv.gross_minor)
        else:
            return 0
        if inv.currency == "EUR":
            return amount
        try:
            return convert(
                Money(amount, inv.currency), "EUR", inv.invoice_date, self._fx
            ).money.minor
        except (FxRateUnavailable, ValueError):
            return 0

    def evaluate(self) -> ScoreReport:
        cases = [self._case(e) for e in self._key.headline + self._key.ambient]
        tallies = {"headline": Tally(), "ambient": Tally()}
        at_risk = 0
        for case, entry in zip(cases, self._key.headline + self._key.ambient, strict=True):
            t = tallies[case.tier]
            tallies[case.tier] = t.model_copy(update={case.status: getattr(t, case.status) + 1})
            at_risk += self._at_risk(case, entry)
        fp = self._false_positives()
        tallies["headline"] = tallies["headline"].model_copy(update={"fp": fp})
        without_evidence = sum(1 for e in self._ledger.entries() if not e.evidence)
        return ScoreReport(
            as_of=self._now(),
            headline=tallies["headline"],
            ambient=tallies["ambient"],
            euros_at_risk_prevented_minor=at_risk,
            entries_without_evidence=without_evidence,
            cases=cases,
        )

    def publish(self) -> Event | None:
        """Emit a ``score_update`` event when the tallies or case statuses changed."""
        report = self.evaluate()
        body = {
            "headline": report.headline.model_dump(),
            "ambient": report.ambient.model_dump(),
            "euros_at_risk_prevented_minor": report.euros_at_risk_prevented_minor,
            "entries_without_evidence": report.entries_without_evidence,
            "cases": [
                {
                    "id": c.id,
                    "tier": c.tier,
                    "status": c.status,
                    "failed": [x.kind for x in c.checks if not x.ok],
                }
                for c in report.cases
            ],
        }
        signature = canonical_json(body)
        if signature == self._last_signature:
            return None
        self._last_signature = signature
        payload = {**body, "as_of": report.as_of.isoformat()}
        return self._store.append(
            make_event(
                "score_update",
                payload,
                event_time=report.as_of,
                source="scoreboard",
                actor="scoreboard",
            )
        ).event


def month_end(d: date) -> date:
    nxt = date(d.year + (d.month == 12), (d.month % 12) + 1, 1)
    return date.fromordinal(nxt.toordinal() - 1)


def scoreboard_from_world(
    world_dir: Path,
    *,
    store: EventStore,
    ledger: Ledger,
    vendors: VendorMaster | None = None,
    fx: FxTable | None = None,
    now: Callable[[], datetime] | None = None,
) -> Scoreboard:
    """Build the scoreboard from a generated world. This is the one place outside tests that reads
    ``answer_key.json`` and ``invoices_truth.json`` (spec §12)."""
    key = AnswerKey.model_validate_json((world_dir / "answer_key.json").read_bytes())
    truth = [
        Invoice.model_validate(i)
        for i in json.loads((world_dir / "invoices_truth.json").read_text(encoding="utf-8"))
    ]
    return Scoreboard(key, store=store, ledger=ledger, vendors=vendors, truth=truth, fx=fx, now=now)
