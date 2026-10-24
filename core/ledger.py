"""Append-only double-entry ledger (spec §5.2, §5.4): journal entries with evidence, payables
subledger, period close, and a Beancount export after every post."""

from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
from collections.abc import Callable
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any, Literal

import structlog
from pydantic import AwareDatetime, BaseModel, ConfigDict, Field

from core.accounts import AP_ACCOUNT, AP_RETENTION_ACCOUNT, FX_ROUNDING_ACCOUNT, is_account
from core.events import EventStore, canonical_json
from core.money import FxRateUnavailable, Money, convert, minor_digits
from core.policy import PRINCIPALS, PolicyViolation, account_in_scope
from sim.fx import FxTable
from sim.models import Event

log = structlog.get_logger("ledger")

REASON_CODES: tuple[str, ...] = (
    "unbalanced_line",
    "unbalanced_currency",
    "unbalanced_eur",
    "period_closed",
    "out_of_scope",
    "no_evidence",
    "duplicate",
    "unknown_account",
)
EvidenceRole = Literal["invoice", "receipt", "po", "message", "decision"]
PayableStatus = Literal["open", "scheduled", "paid", "held", "rejected", "netted"]
OPEN_AP_STATUSES: tuple[str, ...] = ("open", "scheduled", "held")

SCHEMA = """
CREATE TABLE IF NOT EXISTS journal_entries (id TEXT PRIMARY KEY, seq INTEGER UNIQUE, posted_at TEXT, effective_date TEXT,
  description TEXT, posted_by TEXT, cause_type TEXT, cause_id TEXT, reverses TEXT NULL, content_hash TEXT);
CREATE TABLE IF NOT EXISTS journal_lines (entry_id TEXT, line_no INTEGER, account TEXT, debit_minor INTEGER, credit_minor INTEGER,
  currency TEXT, fx_rate TEXT NULL, rate_date TEXT NULL, eur_equiv_minor INTEGER);
CREATE TABLE IF NOT EXISTS entry_evidence (entry_id TEXT, event_id TEXT, role TEXT);
CREATE TABLE IF NOT EXISTS periods (month TEXT PRIMARY KEY, status TEXT, closed_at TEXT, closed_by TEXT);
CREATE TABLE IF NOT EXISTS payables (id TEXT PRIMARY KEY, vendor_id TEXT, inv_id TEXT, entry_id TEXT, amount_minor INTEGER, currency TEXT,
  due_date TEXT, discount_deadline TEXT NULL, discount_bp INTEGER, retention_minor INTEGER, status TEXT, held_reason TEXT NULL);
CREATE TABLE IF NOT EXISTS period_log (month TEXT, action TEXT, at TEXT, by TEXT, event_id TEXT NULL);
CREATE UNIQUE INDEX IF NOT EXISTS ux_entries_hash ON journal_entries(content_hash);
CREATE INDEX IF NOT EXISTS ix_lines_account ON journal_lines(account, entry_id);
CREATE INDEX IF NOT EXISTS ix_lines_entry ON journal_lines(entry_id, line_no);
CREATE INDEX IF NOT EXISTS ix_evidence_entry ON entry_evidence(entry_id);
CREATE UNIQUE INDEX IF NOT EXISTS ux_payables_natural ON payables(vendor_id, inv_id);
CREATE TRIGGER IF NOT EXISTS trg_je_no_update BEFORE UPDATE ON journal_entries
BEGIN SELECT RAISE(ABORT, 'journal_entries is append-only: UPDATE forbidden'); END;
CREATE TRIGGER IF NOT EXISTS trg_je_no_delete BEFORE DELETE ON journal_entries
BEGIN SELECT RAISE(ABORT, 'journal_entries is append-only: DELETE forbidden'); END;
CREATE TRIGGER IF NOT EXISTS trg_jl_no_update BEFORE UPDATE ON journal_lines
BEGIN SELECT RAISE(ABORT, 'journal_lines is append-only: UPDATE forbidden'); END;
CREATE TRIGGER IF NOT EXISTS trg_jl_no_delete BEFORE DELETE ON journal_lines
BEGIN SELECT RAISE(ABORT, 'journal_lines is append-only: DELETE forbidden'); END;
CREATE TRIGGER IF NOT EXISTS trg_ev_no_update BEFORE UPDATE ON entry_evidence
BEGIN SELECT RAISE(ABORT, 'entry_evidence is append-only: UPDATE forbidden'); END;
CREATE TRIGGER IF NOT EXISTS trg_ev_no_delete BEFORE DELETE ON entry_evidence
BEGIN SELECT RAISE(ABORT, 'entry_evidence is append-only: DELETE forbidden'); END;
"""


class LedgerError(Exception):
    def __init__(self, reason: str, detail: str = "") -> None:
        super().__init__(
            f"{type(self).__name__}({reason}): {detail}"
            if detail
            else f"{type(self).__name__}({reason})"
        )
        self.reason = reason
        self.detail = detail


class PostRejected(LedgerError):
    """A posting failed one of the §5.2 checks. ``reason_code`` is one of ``REASON_CODES``."""

    def __init__(self, reason_code: str, detail: str, existing_entry_id: str | None = None) -> None:
        super().__init__(reason_code, detail)
        self.reason_code = reason_code
        self.existing_entry_id = existing_entry_id


class PeriodError(LedgerError):
    """Period close/reopen refused."""


class Evidence(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    event_id: str
    role: EvidenceRole


class JournalLineDraft(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    account: str
    debit_minor: int = 0
    credit_minor: int = 0
    currency: str
    rate_date: date | None = None


class JournalEntryDraft(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    effective_date: date
    description: str
    cause_type: str
    cause_id: str
    lines: list[JournalLineDraft] = Field(min_length=1)
    evidence: list[Evidence]
    reverses: str | None = None


class JournalLine(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    entry_id: str
    line_no: int
    account: str
    debit_minor: int
    credit_minor: int
    currency: str
    fx_rate: str | None
    rate_date: date | None
    eur_equiv_minor: int


class JournalEntry(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    id: str
    seq: int
    posted_at: AwareDatetime
    effective_date: date
    description: str
    posted_by: str
    cause_type: str
    cause_id: str
    reverses: str | None
    content_hash: str
    lines: list[JournalLine]
    evidence: list[Evidence]


class Payable(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    id: str
    vendor_id: str
    inv_id: str
    entry_id: str
    amount_minor: int
    currency: str
    due_date: date
    discount_deadline: date | None
    discount_bp: int
    retention_minor: int
    status: PayableStatus
    held_reason: str | None


class ReconciliationRow(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    payables_minor: int
    ap_balance_minor: int
    retention_payables_minor: int
    retention_balance_minor: int

    @property
    def ok(self) -> bool:
        return (
            self.payables_minor == self.ap_balance_minor
            and self.retention_payables_minor == self.retention_balance_minor
        )


class Reconciliation(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    ok: bool
    per_currency: dict[str, ReconciliationRow]


class PeriodLogRow(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    month: str
    action: str
    at: AwareDatetime
    by: str
    event_id: str | None


def _iso(dt: datetime) -> str:
    return dt.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _month_of(d: date) -> str:
    return f"{d.year:04d}-{d.month:02d}"


def _validate_month(month: str) -> str:
    try:
        datetime.strptime(month, "%Y-%m")
    except ValueError as exc:
        raise PeriodError("invalid_month", f"{month!r} is not YYYY-MM") from exc
    return month


def entry_content_hash(draft: JournalEntryDraft) -> str:
    payload = {
        "effective_date": draft.effective_date.isoformat(),
        "description": draft.description,
        "cause_type": draft.cause_type,
        "cause_id": draft.cause_id,
        "reverses": draft.reverses,
        "lines": [
            [
                ln.account,
                ln.debit_minor,
                ln.credit_minor,
                ln.currency,
                ln.rate_date.isoformat() if ln.rate_date else None,
            ]
            for ln in draft.lines
        ],
        "evidence": [[e.event_id, e.role] for e in draft.evidence],
    }
    return hashlib.sha256(canonical_json(payload).encode("utf-8")).hexdigest()


class Ledger:
    def __init__(
        self,
        path: str | Path,
        *,
        events: EventStore | None = None,
        fx: FxTable | None = None,
        now: Callable[[], datetime] | None = None,
        beancount_path: Path | None = None,
        timeout: float = 30.0,
    ) -> None:
        self._events = events
        self._fx = fx
        self._now: Callable[[], datetime] = now or (lambda: datetime.now(UTC))
        self._beancount_path = beancount_path
        self._lock = threading.RLock()
        self._con = sqlite3.connect(
            str(path), timeout=timeout, check_same_thread=False, isolation_level=None
        )
        self._con.row_factory = sqlite3.Row
        self._con.execute("PRAGMA journal_mode=WAL")
        self._con.execute("PRAGMA synchronous=NORMAL")
        self._con.executescript(SCHEMA)

    def close(self) -> None:
        with self._lock:
            self._con.close()

    # ------------------------------------------------------------------ posting

    def post(
        self, draft: JournalEntryDraft, principal: str, posted_at: datetime | None = None
    ) -> JournalEntry:
        """Validate (§5.2 checks 1–7 in order) and append an entry. Raises ``PostRejected``."""
        lines = self._check_lines(draft)  # (1) + unknown_account
        self._check_currency_balance(lines)  # (2)
        computed = self._check_eur_balance(draft, lines)  # (3), may add the rounding line
        self._check_period(draft.effective_date)  # (4)
        self._check_scope(principal, [c["account"] for c in computed])  # (5)
        self._check_evidence(draft.evidence)  # (6)
        digest = entry_content_hash(draft)
        when = posted_at or self._now()
        with self._lock:
            existing = self._con.execute(
                "SELECT id FROM journal_entries WHERE content_hash = ?", (digest,)
            ).fetchone()
            if existing is not None:  # (7)
                raise PostRejected(
                    "duplicate",
                    f"identical entry already posted as {existing['id']}",
                    existing_entry_id=str(existing["id"]),
                )
            self._con.execute("BEGIN IMMEDIATE")
            try:
                seq = int(
                    self._con.execute(
                        "SELECT COALESCE(MAX(seq), 0) + 1 FROM journal_entries"
                    ).fetchone()[0]
                )
                entry_id = f"JE-{seq:06d}"
                self._con.execute(
                    "INSERT INTO journal_entries VALUES (?,?,?,?,?,?,?,?,?,?)",
                    (
                        entry_id,
                        seq,
                        _iso(when),
                        draft.effective_date.isoformat(),
                        draft.description,
                        principal,
                        draft.cause_type,
                        draft.cause_id,
                        draft.reverses,
                        digest,
                    ),
                )
                for n, c in enumerate(computed, start=1):
                    self._con.execute(
                        "INSERT INTO journal_lines VALUES (?,?,?,?,?,?,?,?,?)",
                        (
                            entry_id,
                            n,
                            c["account"],
                            c["debit"],
                            c["credit"],
                            c["currency"],
                            c["fx_rate"],
                            c["rate_date"].isoformat() if c["rate_date"] else None,
                            c["eur_equiv"],
                        ),
                    )
                for e in draft.evidence:
                    self._con.execute(
                        "INSERT INTO entry_evidence VALUES (?,?,?)", (entry_id, e.event_id, e.role)
                    )
                self._con.execute("COMMIT")
            except BaseException:
                if self._con.in_transaction:
                    self._con.execute("ROLLBACK")
                raise
        entry = self.get_entry(entry_id)
        assert entry is not None
        log.info(
            "posted",
            entry_id=entry.id,
            principal=principal,
            cause_type=draft.cause_type,
            cause_id=draft.cause_id,
            lines=len(entry.lines),
            event_id=draft.evidence[0].event_id,
        )
        self.export_beancount()
        return entry

    def reverse(
        self,
        entry_id: str,
        principal: str,
        cause_type: str,
        cause_id: str,
        effective_date: date,
        posted_at: datetime | None = None,
    ) -> JournalEntry:
        """Post a mirrored entry with ``reverses`` set; the original is never modified."""
        original = self.get_entry(entry_id)
        if original is None:
            raise LedgerError("unknown_entry", f"no journal entry {entry_id}")
        mirrored = [
            JournalLineDraft(
                account=ln.account,
                debit_minor=ln.credit_minor,
                credit_minor=ln.debit_minor,
                currency=ln.currency,
                rate_date=ln.rate_date,
            )
            for ln in original.lines
        ]
        draft = JournalEntryDraft(
            effective_date=effective_date,
            description=f"Reversal of {entry_id}: {original.description}",
            cause_type=cause_type,
            cause_id=cause_id,
            lines=mirrored,
            evidence=list(original.evidence),
            reverses=entry_id,
        )
        return self.post(draft, principal, posted_at=posted_at)

    # ------------------------------------------------------------------ checks

    @staticmethod
    def _check_lines(draft: JournalEntryDraft) -> list[JournalLineDraft]:
        for n, ln in enumerate(draft.lines, start=1):
            if not is_account(ln.account):
                raise PostRejected(
                    "unknown_account",
                    f"line {n}: {ln.account!r} is not in the chart of accounts (spec §2.1)",
                )
            if ln.debit_minor < 0 or ln.credit_minor < 0:
                raise PostRejected("unbalanced_line", f"line {n}: amounts must be non-negative")
            if (ln.debit_minor > 0) == (ln.credit_minor > 0):
                raise PostRejected(
                    "unbalanced_line",
                    f"line {n}: exactly one of debit/credit must be > 0 (got debit={ln.debit_minor}, credit={ln.credit_minor})",
                )
            try:
                minor_digits(ln.currency)
            except ValueError as exc:
                raise PostRejected("unbalanced_line", f"line {n}: {exc}") from exc
        return list(draft.lines)

    @staticmethod
    def _check_currency_balance(lines: list[JournalLineDraft]) -> None:
        currencies = {ln.currency for ln in lines}
        if len(currencies) != 1:
            return  # multi-currency entries balance in EUR equivalent (check 3)
        ccy = next(iter(currencies))
        debit = sum(ln.debit_minor for ln in lines)
        credit = sum(ln.credit_minor for ln in lines)
        if debit != credit:
            raise PostRejected("unbalanced_currency", f"{ccy}: debits {debit} != credits {credit}")

    def _check_eur_balance(
        self, draft: JournalEntryDraft, lines: list[JournalLineDraft]
    ) -> list[dict[str, Any]]:
        computed: list[dict[str, Any]] = []
        for n, ln in enumerate(lines, start=1):
            amount = ln.debit_minor or ln.credit_minor
            rate_date = ln.rate_date or draft.effective_date
            if ln.currency == "EUR":
                eur, fx_rate = amount, None
            else:
                try:
                    conv = convert(Money(amount, ln.currency), "EUR", rate_date, self._fx)
                except FxRateUnavailable as exc:
                    raise PostRejected(
                        "unbalanced_eur", f"line {n}: cannot compute EUR equivalent: {exc}"
                    ) from exc
                eur, fx_rate = conv.money.minor, str(conv.from_rate)
            computed.append(
                {
                    "account": ln.account,
                    "debit": ln.debit_minor,
                    "credit": ln.credit_minor,
                    "currency": ln.currency,
                    "fx_rate": fx_rate,
                    "rate_date": rate_date if ln.currency != "EUR" else None,
                    "eur_equiv": eur,
                }
            )
        residual = sum(c["eur_equiv"] for c in computed if c["debit"]) - sum(
            c["eur_equiv"] for c in computed if c["credit"]
        )
        if residual == 0:
            return computed
        if abs(residual) > 1:
            raise PostRejected(
                "unbalanced_eur",
                f"EUR equivalents differ by {residual} cents (debits minus credits); tolerance is 1 cent",
            )
        computed.append(
            {
                "account": FX_ROUNDING_ACCOUNT,
                "debit": -residual if residual < 0 else 0,
                "credit": residual if residual > 0 else 0,
                "currency": "EUR",
                "fx_rate": None,
                "rate_date": None,
                "eur_equiv": abs(residual),
            }
        )
        return computed

    def _check_period(self, effective_date: date) -> None:
        if self.period_status(_month_of(effective_date)) == "closed":
            raise PostRejected(
                "period_closed",
                f"period {_month_of(effective_date)} is closed; effective_date {effective_date} cannot be posted",
            )

    @staticmethod
    def _check_scope(principal: str, accounts: list[str]) -> None:
        scope = PRINCIPALS.get(principal)
        for account in accounts:
            if not account_in_scope(principal, account):
                allowed = (
                    ", ".join(sorted(scope.post))
                    if scope and scope.post
                    else ("everything" if scope and scope.approve else "nothing")
                )
                raise PostRejected(
                    "out_of_scope",
                    f"principal {principal!r} may not post to {account} (scope: {allowed})",
                )

    def _check_evidence(self, evidence: list[Evidence]) -> None:
        if not evidence:
            raise PostRejected("no_evidence", "an entry needs at least one evidence event")
        if self._events is not None:
            for e in evidence:
                if not self._events.exists(e.event_id):
                    raise PostRejected(
                        "no_evidence",
                        f"evidence event {e.event_id} does not exist in the event store",
                    )

    # ------------------------------------------------------------------ reads

    def get_entry(self, entry_id: str) -> JournalEntry | None:
        with self._lock:
            row = self._con.execute(
                "SELECT * FROM journal_entries WHERE id = ?", (entry_id,)
            ).fetchone()
            if row is None:
                return None
            lines = self._con.execute(
                "SELECT * FROM journal_lines WHERE entry_id = ? ORDER BY line_no", (entry_id,)
            ).fetchall()
            evidence = self._con.execute(
                "SELECT event_id, role FROM entry_evidence WHERE entry_id = ? ORDER BY rowid",
                (entry_id,),
            ).fetchall()
        return JournalEntry(
            id=row["id"],
            seq=row["seq"],
            posted_at=datetime.fromisoformat(row["posted_at"]),
            effective_date=date.fromisoformat(row["effective_date"]),
            description=row["description"],
            posted_by=row["posted_by"],
            cause_type=row["cause_type"],
            cause_id=row["cause_id"],
            reverses=row["reverses"],
            content_hash=row["content_hash"],
            lines=[
                JournalLine(
                    entry_id=ln["entry_id"],
                    line_no=ln["line_no"],
                    account=ln["account"],
                    debit_minor=ln["debit_minor"],
                    credit_minor=ln["credit_minor"],
                    currency=ln["currency"],
                    fx_rate=ln["fx_rate"],
                    rate_date=date.fromisoformat(ln["rate_date"]) if ln["rate_date"] else None,
                    eur_equiv_minor=ln["eur_equiv_minor"],
                )
                for ln in lines
            ],
            evidence=[Evidence(event_id=e["event_id"], role=e["role"]) for e in evidence],
        )

    def entries(self, since_seq: int = 0, limit: int = 100000) -> list[JournalEntry]:
        with self._lock:
            ids = [
                r["id"]
                for r in self._con.execute(
                    "SELECT id FROM journal_entries WHERE seq > ? ORDER BY seq LIMIT ?",
                    (since_seq, limit),
                ).fetchall()
            ]
        out: list[JournalEntry] = []
        for entry_id in ids:
            entry = self.get_entry(entry_id)
            if entry is not None:
                out.append(entry)
        return out

    def balance(
        self, account: str, as_of_effective_date: date, as_of_known_at: datetime | None = None
    ) -> dict[str, int]:
        """Debit-minus-credit per currency over entries effective on or before the date; with
        ``as_of_known_at`` only entries posted at or before that instant count (what we believed then)."""
        sql = (
            "SELECT l.currency AS currency, COALESCE(SUM(l.debit_minor), 0) - COALESCE(SUM(l.credit_minor), 0) AS bal"
            " FROM journal_lines l JOIN journal_entries e ON e.id = l.entry_id WHERE l.account = ? AND e.effective_date <= ?"
        )
        params: list[Any] = [account, as_of_effective_date.isoformat()]
        if as_of_known_at is not None:
            sql += " AND e.posted_at <= ?"
            params.append(_iso(as_of_known_at))
        sql += " GROUP BY l.currency ORDER BY l.currency"
        with self._lock:
            rows = self._con.execute(sql, params).fetchall()
        return {r["currency"]: int(r["bal"]) for r in rows}

    def balances(
        self, as_of_effective_date: date, as_of_known_at: datetime | None = None
    ) -> dict[str, dict[str, int]]:
        sql = (
            "SELECT l.account AS account, l.currency AS currency, COALESCE(SUM(l.debit_minor), 0) - COALESCE(SUM(l.credit_minor), 0) AS bal"
            " FROM journal_lines l JOIN journal_entries e ON e.id = l.entry_id WHERE e.effective_date <= ?"
        )
        params: list[Any] = [as_of_effective_date.isoformat()]
        if as_of_known_at is not None:
            sql += " AND e.posted_at <= ?"
            params.append(_iso(as_of_known_at))
        sql += " GROUP BY l.account, l.currency ORDER BY l.account, l.currency"
        with self._lock:
            rows = self._con.execute(sql, params).fetchall()
        out: dict[str, dict[str, int]] = {}
        for r in rows:
            out.setdefault(r["account"], {})[r["currency"]] = int(r["bal"])
        return out

    # ------------------------------------------------------------------ payables (§5.4)

    def create_payable(
        self,
        *,
        vendor_id: str,
        inv_id: str,
        entry_id: str,
        amount_minor: int,
        currency: str,
        due_date: date,
        discount_deadline: date | None = None,
        discount_bp: int = 0,
        retention_minor: int = 0,
        status: PayableStatus = "open",
        held_reason: str | None = None,
    ) -> Payable:
        """Create the payable for (vendor, invoice); returns the existing one if already present."""
        with self._lock:
            row = self._con.execute(
                "SELECT id FROM payables WHERE vendor_id = ? AND inv_id = ?", (vendor_id, inv_id)
            ).fetchone()
            if row is not None:
                existing = self.get_payable(str(row["id"]))
                assert existing is not None
                return existing
            n = int(self._con.execute("SELECT COUNT(*) FROM payables").fetchone()[0]) + 1
            payable_id = f"AP-{n:06d}"
            self._con.execute(
                "INSERT INTO payables VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    payable_id,
                    vendor_id,
                    inv_id,
                    entry_id,
                    amount_minor,
                    currency,
                    due_date.isoformat(),
                    discount_deadline.isoformat() if discount_deadline else None,
                    discount_bp,
                    retention_minor,
                    status,
                    held_reason,
                ),
            )
        payable = self.get_payable(payable_id)
        assert payable is not None
        return payable

    def get_payable(self, payable_id: str) -> Payable | None:
        with self._lock:
            row = self._con.execute("SELECT * FROM payables WHERE id = ?", (payable_id,)).fetchone()
        return None if row is None else self._payable(row)

    def payables(
        self, status: PayableStatus | None = None, vendor_id: str | None = None
    ) -> list[Payable]:
        sql = "SELECT * FROM payables WHERE 1=1"
        params: list[Any] = []
        if status is not None:
            sql += " AND status = ?"
            params.append(status)
        if vendor_id is not None:
            sql += " AND vendor_id = ?"
            params.append(vendor_id)
        sql += " ORDER BY due_date, id"
        with self._lock:
            rows = self._con.execute(sql, params).fetchall()
        return [self._payable(r) for r in rows]

    def set_payable_status(
        self, payable_id: str, status: PayableStatus, held_reason: str | None = None
    ) -> Payable:
        with self._lock:
            cur = self._con.execute(
                "UPDATE payables SET status = ?, held_reason = ? WHERE id = ?",
                (status, held_reason, payable_id),
            )
            if cur.rowcount != 1:
                raise LedgerError("unknown_payable", f"no payable {payable_id}")
        payable = self.get_payable(payable_id)
        assert payable is not None
        return payable

    def ap_open_items(self, vendor_id: str | None = None) -> list[Payable]:
        return [p for p in self.payables(vendor_id=vendor_id) if p.status in OPEN_AP_STATUSES]

    def ap_control_reconciles(
        self, as_of_effective_date: date | None = None, as_of_known_at: datetime | None = None
    ) -> Reconciliation:
        """Σ open payables must equal the AP control balance per currency (retention likewise)."""
        as_of = as_of_effective_date or date(9999, 12, 31)
        ap = self.balance(AP_ACCOUNT, as_of, as_of_known_at)
        retention = self.balance(AP_RETENTION_ACCOUNT, as_of, as_of_known_at)
        open_items = self.ap_open_items()
        retained = [p for p in self.payables() if p.status != "rejected"]
        currencies = (
            set(ap)
            | set(retention)
            | {p.currency for p in open_items}
            | {p.currency for p in retained if p.retention_minor}
        )
        rows: dict[str, ReconciliationRow] = {}
        for ccy in sorted(currencies):
            rows[ccy] = ReconciliationRow(
                payables_minor=sum(p.amount_minor for p in open_items if p.currency == ccy),
                ap_balance_minor=-ap.get(ccy, 0),
                retention_payables_minor=sum(
                    p.retention_minor for p in retained if p.currency == ccy
                ),
                retention_balance_minor=-retention.get(ccy, 0),
            )
        return Reconciliation(ok=all(r.ok for r in rows.values()), per_currency=rows)

    @staticmethod
    def _payable(row: sqlite3.Row) -> Payable:
        return Payable(
            id=row["id"],
            vendor_id=row["vendor_id"],
            inv_id=row["inv_id"],
            entry_id=row["entry_id"],
            amount_minor=row["amount_minor"],
            currency=row["currency"],
            due_date=date.fromisoformat(row["due_date"]),
            discount_deadline=date.fromisoformat(row["discount_deadline"])
            if row["discount_deadline"]
            else None,
            discount_bp=row["discount_bp"],
            retention_minor=row["retention_minor"],
            status=row["status"],
            held_reason=row["held_reason"],
        )

    # ------------------------------------------------------------------ periods

    def period_status(self, month: str) -> str:
        with self._lock:
            row = self._con.execute(
                "SELECT status FROM periods WHERE month = ?", (month,)
            ).fetchone()
        return "open" if row is None else str(row["status"])

    def close_period(self, month: str, principal: str = "controller") -> None:
        """Close a month: controller only, and only when the AP subledger equals the GL control."""
        _validate_month(month)
        scope = PRINCIPALS.get(principal)
        if scope is None or not scope.approve:
            raise PolicyViolation(
                "approve_required", f"principal {principal!r} may not close periods"
            )
        if self.period_status(month) == "closed":
            raise PeriodError("already_closed", f"period {month} is already closed")
        rec = self.ap_control_reconciles()
        if not rec.ok:
            mismatch = {
                ccy: row.model_dump() for ccy, row in rec.per_currency.items() if not row.ok
            }
            raise PeriodError(
                "ap_control_mismatch",
                f"AP subledger does not equal Liabilities:AP: {json.dumps(mismatch, sort_keys=True)}",
            )
        when = self._now()
        with self._lock:
            self._con.execute(
                "INSERT INTO periods(month, status, closed_at, closed_by) VALUES (?,?,?,?)"
                " ON CONFLICT(month) DO UPDATE SET status='closed', closed_at=excluded.closed_at, closed_by=excluded.closed_by",
                (month, "closed", _iso(when), principal),
            )
            self._con.execute(
                "INSERT INTO period_log VALUES (?,?,?,?,?)",
                (month, "closed", _iso(when), principal, None),
            )
        log.info("period_closed", month=month, principal=principal, event_id=None, rule_id="R7")

    def reopen_period(self, month: str, decision: Event) -> None:
        """Reopen a closed month on the strength of a ``controller_decision`` event (never via the API)."""
        _validate_month(month)
        payload = decision.payload
        if decision.type != "controller_decision" or payload.get("decision") != "reopen_period":
            raise PeriodError(
                "invalid_decision",
                f"event {decision.id} is not a controller_decision to reopen a period",
            )
        note = str(payload.get("note") or "") + " " + str(payload.get("packet_id") or "")
        if month not in note:
            raise PeriodError(
                "invalid_decision", f"decision {decision.id} does not name period {month}"
            )
        if self._events is not None and not self._events.exists(decision.id):
            raise PeriodError(
                "invalid_decision", f"decision event {decision.id} is not in the event store"
            )
        if self.period_status(month) != "closed":
            raise PeriodError("not_closed", f"period {month} is not closed")
        when = self._now()
        decided_by = str(payload.get("decided_by") or "controller")
        with self._lock:
            self._con.execute(
                "UPDATE periods SET status='open', closed_at=NULL, closed_by=NULL WHERE month = ?",
                (month,),
            )
            self._con.execute(
                "INSERT INTO period_log VALUES (?,?,?,?,?)",
                (month, "reopened", _iso(when), decided_by, decision.id),
            )
        log.info(
            "period_reopened", month=month, principal=decided_by, event_id=decision.id, rule_id=None
        )

    def period_log(self, month: str) -> list[PeriodLogRow]:
        with self._lock:
            rows = self._con.execute(
                "SELECT * FROM period_log WHERE month = ? ORDER BY rowid", (month,)
            ).fetchall()
        return [
            PeriodLogRow(
                month=r["month"],
                action=r["action"],
                at=datetime.fromisoformat(r["at"]),
                by=r["by"],
                event_id=r["event_id"],
            )
            for r in rows
        ]

    # ------------------------------------------------------------------ export

    def export_beancount(self, path: Path | None = None) -> str:
        from core.beancount_export import render_beancount, write_atomic

        text = render_beancount(self.entries())
        target = path or self._beancount_path
        if target is not None:
            write_atomic(target, text)
        return text
