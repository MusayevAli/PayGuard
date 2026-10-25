"""Mock bank (spec §11). Balances per account start from the bootstrap opening balances and follow
the world's statements; ``execute(frame, tag)`` recomputes the HMAC with the shared key, settles
the native amount recorded on ``payment_requested`` and queues a ``:61:``/``:86:`` pair for the
next day's MT940. Incoming statements (the world's and the bank's own) are parsed into
``bank_statement_line`` events exactly once per line."""

from __future__ import annotations

import json
import sqlite3
import threading
from collections.abc import Callable
from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path
from typing import Any

import structlog
from pydantic import BaseModel, ConfigDict

from core.bootstrap import OPENING_BANK_MINOR
from core.events import EventStore, make_event
from core.money import FxRateUnavailable, Money, convert
from signer.frames import FrameError, PayFrame, decode, verify_tag
from sim.fx import FxTable
from sim.models import Event
from sim.mt940 import Statement, StatementLine, parse_mt940, truncate_remittance, write_mt940
from sim.worldgen.catalog import BANK_ACCOUNTS

log = structlog.get_logger("bank")

RUNTIME_SEQUENCE_BASE = 1000
STATEMENT_PRODUCED_AT = time(5, 30)
STATEMENT_KNOWN_AT = time(6, 0)
LINE_EVENT_TIME = time(12, 0)
SETTLEMENT_CURRENCIES = tuple(OPENING_BANK_MINOR)

SCHEMA = """
CREATE TABLE IF NOT EXISTS bank_state (
  currency TEXT PRIMARY KEY, world_balance_minor INTEGER NOT NULL, runtime_delta_minor INTEGER NOT NULL,
  last_statement_date TEXT NULL, last_statement_id TEXT NULL
);
CREATE TABLE IF NOT EXISTS bank_settlements (
  bank_ref TEXT PRIMARY KEY, pay_id TEXT NOT NULL, currency TEXT NOT NULL, dc TEXT NOT NULL,
  amount_minor INTEGER NOT NULL, value_date TEXT NOT NULL, customer_ref TEXT NOT NULL,
  remittance TEXT NOT NULL, published INTEGER NOT NULL DEFAULT 0, detail TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS bank_statements_seen (statement_id TEXT PRIMARY KEY, lines INTEGER NOT NULL, known_date TEXT NULL, currency TEXT NULL);
"""


class BankResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    ok: bool
    pay_id: str
    bank_ref: str | None
    reason: str | None
    detail: str
    amount_minor: int
    currency: str
    settlement_currency: str | None
    settlement_amount_minor: int | None
    fx_rate: str | None
    value_date: date | None
    event_id: str | None


class MockBank:
    def __init__(
        self,
        path: str | Path,
        *,
        store: EventStore,
        key: bytes,
        fx: FxTable,
        now: Callable[[], datetime],
        schedule: Callable[[Event], None],
        out_dir: Path,
        accounts: dict[str, str] | None = None,
        opening: dict[str, int] | None = None,
    ) -> None:
        self._store = store
        self._key = key
        self._fx = fx
        self._now = now
        self._schedule = schedule
        self._out_dir = Path(out_dir)
        self._ibans = accounts or {ccy: acct["iban"] for ccy, acct in BANK_ACCOUNTS.items()}
        self._lock = threading.RLock()
        self._con = sqlite3.connect(str(path), check_same_thread=False, isolation_level=None)
        self._con.row_factory = sqlite3.Row
        self._con.execute("PRAGMA journal_mode=WAL")
        self._con.executescript(SCHEMA)
        for ccy, minor in (opening or OPENING_BANK_MINOR).items():
            self._con.execute(
                "INSERT OR IGNORE INTO bank_state(currency, world_balance_minor, runtime_delta_minor) VALUES (?,?,0)",
                (ccy, minor),
            )
        self._requests: dict[str, dict[str, Any]] = {}
        self._requests_seq = 0
        self._line_keys: set[str] | None = None
        self._line_keys_seq = 0

    def close(self) -> None:
        with self._lock:
            self._con.close()

    # ------------------------------------------------------------------ balances

    @property
    def key(self) -> bytes:
        return self._key

    def iban(self, currency: str) -> str:
        return self._ibans[currency]

    def currency_of(self, iban: str) -> str | None:
        return next((c for c, i in self._ibans.items() if i == iban), None)

    def balance(self, currency: str) -> int:
        with self._lock:
            row = self._con.execute(
                "SELECT world_balance_minor, runtime_delta_minor FROM bank_state WHERE currency = ?",
                (currency,),
            ).fetchone()
        if row is None:
            raise KeyError(f"no bank account in {currency}")
        return int(row["world_balance_minor"]) + int(row["runtime_delta_minor"])

    def balances(self) -> dict[str, int]:
        return {ccy: self.balance(ccy) for ccy in SETTLEMENT_CURRENCIES}

    def world_statements_seen(self) -> int:
        """How many of the bank's (world) statements have been ingested; 0 means no delivery yet."""
        with self._lock:
            row = self._con.execute(
                "SELECT COUNT(*) FROM bank_statements_seen WHERE statement_id NOT LIKE '%P'"
            ).fetchone()
        return int(row[0])

    def runtime_delta(self, currency: str) -> int:
        with self._lock:
            row = self._con.execute(
                "SELECT runtime_delta_minor FROM bank_state WHERE currency = ?", (currency,)
            ).fetchone()
        return int(row["runtime_delta_minor"]) if row else 0

    def accounts(self) -> list[dict[str, Any]]:
        out = []
        for ccy in SETTLEMENT_CURRENCIES:
            with self._lock:
                row = self._con.execute(
                    "SELECT * FROM bank_state WHERE currency = ?", (ccy,)
                ).fetchone()
            out.append(
                {
                    "currency": ccy,
                    "iban": self._ibans[ccy],
                    "bank": BANK_ACCOUNTS[ccy]["bank"],
                    "balance_minor": self.balance(ccy),
                    "world_balance_minor": int(row["world_balance_minor"]),
                    "runtime_delta_minor": int(row["runtime_delta_minor"]),
                    "last_statement_date": row["last_statement_date"],
                }
            )
        return out

    # ------------------------------------------------------------------ execution

    def _request(self, pay_id: str) -> dict[str, Any] | None:
        """The latest payment_requested payload for ``pay_id`` (a re-sent payment supersedes)."""
        for e in self._store.query(
            type="payment_requested", since_seq=self._requests_seq, limit=1_000_000
        ):
            self._requests[str(e.payload["pay_id"])] = e.payload
            if e.seq is not None:
                self._requests_seq = max(self._requests_seq, e.seq)
        return self._requests.get(pay_id)

    def statements_ingested_on(self, known: date) -> set[str]:
        """Currencies whose (world) statement became known on ``known`` and has been ingested."""
        with self._lock:
            rows = self._con.execute(
                "SELECT currency FROM bank_statements_seen WHERE known_date = ? AND statement_id NOT LIKE '%P'",
                (known.isoformat(),),
            ).fetchall()
        return {str(r["currency"]) for r in rows}

    def execute(self, frame: bytes, tag: bytes) -> BankResult:
        """Settle a signed payment. The tag must verify against the shared key; the native amount
        comes from the ``payment_requested`` event for the frame's pay_id."""
        try:
            decoded = decode(frame)
        except FrameError as exc:
            return self._reject("?", "malformed_frame", str(exc), 0, "EUR", None)
        if not isinstance(decoded, PayFrame):
            return self._reject("?", "not_a_payment", "frame is not a pay frame", 0, "EUR", None)
        pay = decoded
        prefix = pay.account_hash.hex()[:12]
        if not verify_tag(self._key, frame, tag):
            return self._reject(
                pay.pay_id,
                "bad_tag",
                "HMAC does not verify with the shared key",
                pay.amount_minor,
                pay.currency,
                prefix,
            )
        req = self._request(pay.pay_id)
        if req is None:
            return self._reject(
                pay.pay_id,
                "unknown_payment",
                "no payment_requested event for this pay_id",
                pay.amount_minor,
                pay.currency,
                prefix,
            )
        if (
            req["account_hash"] != pay.account_hash.hex()
            or int(req["eur_equiv_minor"]) != pay.amount_minor
            or req["currency"] != pay.currency
        ):
            return self._reject(
                pay.pay_id,
                "frame_mismatch",
                "frame disagrees with the payment request (hash, amount or currency)",
                pay.amount_minor,
                pay.currency,
                prefix,
            )
        native_minor = int(req["amount_minor"])
        currency = str(req["currency"])
        value_date = self._now().date()
        if currency in self._ibans:
            settle_ccy, settle_minor, rate = currency, native_minor, None
        else:
            try:
                conv = convert(Money(native_minor, currency), "EUR", value_date, self._fx)
            except (FxRateUnavailable, ValueError) as exc:
                return self._reject(
                    pay.pay_id, "fx_unavailable", str(exc), native_minor, currency, prefix
                )
            settle_ccy, settle_minor, rate = "EUR", conv.money.minor, str(conv.rate)
        if self.balance(settle_ccy) < settle_minor:
            return self._reject(
                pay.pay_id,
                "insufficient_funds",
                f"{settle_ccy} balance {self.balance(settle_ccy)} < {settle_minor}",
                native_minor,
                currency,
                prefix,
            )
        with self._lock:
            n = (
                int(
                    self._con.execute(
                        "SELECT COUNT(*) FROM bank_settlements WHERE currency = ? AND value_date = ?",
                        (settle_ccy, value_date.isoformat()),
                    ).fetchone()[0]
                )
                + 1
            )
            bank_ref = f"{settle_ccy[0]}{value_date:%y%m%d}P{n:03d}"
            remittance = f"/REMI/{pay.pay_id} {req['vendor_id']} {req['account_masked']}"
            if rate is not None:
                remittance += f" {currency} {native_minor} AT {rate}"
            self._con.execute(
                "INSERT INTO bank_settlements(bank_ref, pay_id, currency, dc, amount_minor, value_date, customer_ref, remittance, published, detail) VALUES (?,?,?,?,?,?,?,?,0,?)",
                (
                    bank_ref,
                    pay.pay_id,
                    settle_ccy,
                    "D",
                    settle_minor,
                    value_date.isoformat(),
                    pay.pay_id,
                    truncate_remittance(remittance),
                    json.dumps(
                        {"native_currency": currency, "native_minor": native_minor, "fx_rate": rate}
                    ),
                ),
            )
            self._con.execute(
                "UPDATE bank_state SET runtime_delta_minor = runtime_delta_minor - ? WHERE currency = ?",
                (settle_minor, settle_ccy),
            )
        log.info(
            "payment_settled",
            pay_id=pay.pay_id,
            bank_ref=bank_ref,
            currency=settle_ccy,
            amount_minor=settle_minor,
            event_id=None,
            principal="bank",
            rule_id=None,
        )
        return BankResult(
            ok=True,
            pay_id=pay.pay_id,
            bank_ref=bank_ref,
            reason=None,
            detail="settled",
            amount_minor=native_minor,
            currency=currency,
            settlement_currency=settle_ccy,
            settlement_amount_minor=settle_minor,
            fx_rate=rate,
            value_date=value_date,
            event_id=None,
        )

    def credit(self, currency: str, amount_minor: int, *, reference: str, remittance: str) -> str:
        """An incoming transfer the desk arranged (an intercompany draw): credited now, reported on
        the next statement."""
        if amount_minor <= 0:
            raise ValueError("credit amount must be positive")
        value_date = self._now().date()
        with self._lock:
            n = (
                int(
                    self._con.execute(
                        "SELECT COUNT(*) FROM bank_settlements WHERE currency = ? AND value_date = ?",
                        (currency, value_date.isoformat()),
                    ).fetchone()[0]
                )
                + 1
            )
            bank_ref = f"{currency[0]}{value_date:%y%m%d}P{n:03d}"
            self._con.execute(
                "INSERT INTO bank_settlements(bank_ref, pay_id, currency, dc, amount_minor, value_date, customer_ref, remittance, published, detail) VALUES (?,?,?,?,?,?,?,?,0,?)",
                (
                    bank_ref,
                    reference,
                    currency,
                    "C",
                    amount_minor,
                    value_date.isoformat(),
                    reference[:16],
                    truncate_remittance(remittance),
                    json.dumps({}),
                ),
            )
            self._con.execute(
                "UPDATE bank_state SET runtime_delta_minor = runtime_delta_minor + ? WHERE currency = ?",
                (amount_minor, currency),
            )
        log.info(
            "credit_booked",
            reference=reference,
            currency=currency,
            amount_minor=amount_minor,
            event_id=None,
            principal="bank",
            rule_id=None,
        )
        return bank_ref

    def _reject(
        self,
        pay_id: str,
        reason: str,
        detail: str,
        amount_minor: int,
        currency: str,
        prefix: str | None,
    ) -> BankResult:
        ev = self._store.append(
            make_event(
                "bank_rejected",
                {
                    "pay_id": pay_id,
                    "reason": reason,
                    "detail": detail[:500],
                    "amount_minor": max(0, amount_minor),
                    "currency": currency
                    if currency in ("EUR", "CZK", "PLN", "USD", "JPY")
                    else "EUR",
                    "account_hash_prefix": prefix,
                },
                event_time=self._now(),
                source="bank",
                actor="bank",
            )
        ).event
        log.warning(
            "bank_rejected",
            pay_id=pay_id,
            reason=reason,
            event_id=ev.id,
            principal="bank",
            rule_id=None,
        )
        return BankResult(
            ok=False,
            pay_id=pay_id,
            bank_ref=None,
            reason=reason,
            detail=detail,
            amount_minor=max(0, amount_minor),
            currency=currency,
            settlement_currency=None,
            settlement_amount_minor=None,
            fx_rate=None,
            value_date=None,
            event_id=ev.id,
        )

    # ------------------------------------------------------------------ statements out

    def publish_statements(self, through: date) -> list[Event]:
        """Emit the bank's own MT940 for every (account, day) with unpublished runtime lines up to
        ``through``: produced at d+1 05:30, known at d+1 06:00, via the clock's schedule."""
        out: list[Event] = []
        with self._lock:
            rows = self._con.execute(
                "SELECT * FROM bank_settlements WHERE published = 0 AND value_date <= ? ORDER BY currency, value_date, bank_ref",
                (through.isoformat(),),
            ).fetchall()
        groups: dict[tuple[str, str], list[sqlite3.Row]] = {}
        for r in rows:
            groups.setdefault((str(r["currency"]), str(r["value_date"])), []).append(r)
        for (ccy, day_text), lines_rows in groups.items():
            day = date.fromisoformat(day_text)
            with self._lock:
                before = int(
                    self._con.execute(
                        "SELECT COALESCE(SUM(CASE WHEN dc = 'C' THEN amount_minor ELSE -amount_minor END), 0) FROM bank_settlements WHERE currency = ? AND published = 1 AND value_date < ?",
                        (ccy, day_text),
                    ).fetchone()[0]
                )
            lines = [
                StatementLine(
                    value_date=day,
                    entry_date=day,
                    dc="C" if str(r["dc"]) == "C" else "D",
                    amount_minor=int(r["amount_minor"]),
                    txn_type="NTRF",
                    customer_ref=str(r["customer_ref"]),
                    bank_ref=str(r["bank_ref"]),
                    remittance=str(r["remittance"]),
                )
                for r in lines_rows
            ]
            closing = before + sum(
                ln.amount_minor if ln.dc == "C" else -ln.amount_minor for ln in lines
            )
            stmt = Statement(
                statement_id=f"{ccy}{day:%y%m%d}P",
                account_iban=self._ibans[ccy],
                currency=ccy,
                statement_date=day,
                sequence_no=RUNTIME_SEQUENCE_BASE + (day - date(2026, 1, 1)).days,
                opening_balance_minor=before,
                lines=lines,
                closing_balance_minor=closing,
            )
            text = write_mt940(stmt)
            path = self._out_dir / "bank" / f"{stmt.statement_id}.txt"
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(text.encode("utf-8"))
            produced = datetime.combine(day + timedelta(days=1), STATEMENT_PRODUCED_AT, tzinfo=UTC)
            known = datetime.combine(day + timedelta(days=1), STATEMENT_KNOWN_AT, tzinfo=UTC)
            stmt_event = make_event(
                "bank_statement",
                {
                    "statement_id": stmt.statement_id,
                    "account_iban": stmt.account_iban,
                    "currency": ccy,
                    "statement_date": day.isoformat(),
                    "sequence_no": stmt.sequence_no,
                    "path": str(path.relative_to(self._out_dir)),
                    "mt940": text,
                },
                event_time=produced,
                known_at=known,
                source="bank",
                actor=BANK_ACCOUNTS[ccy]["bic"],
            )
            self._schedule(stmt_event)
            out.append(stmt_event)
            for n, ln in enumerate(lines, start=1):
                line_event = make_event(
                    "bank_statement_line",
                    self._line_payload(stmt, n, ln),
                    event_time=datetime.combine(ln.value_date, LINE_EVENT_TIME, tzinfo=UTC),
                    known_at=known,
                    source="bank",
                    actor=BANK_ACCOUNTS[ccy]["bic"],
                )
                self._schedule(line_event)
                out.append(line_event)
                self._remember_line(self._line_key(stmt.account_iban, ln))
            with self._lock:
                self._con.execute(
                    "UPDATE bank_settlements SET published = 1 WHERE currency = ? AND value_date = ?",
                    (ccy, day_text),
                )
                self._con.execute(
                    "INSERT OR IGNORE INTO bank_statements_seen(statement_id, lines) VALUES (?, ?)",
                    (stmt.statement_id, len(lines)),
                )
            log.info(
                "statement_published",
                statement_id=stmt.statement_id,
                lines=len(lines),
                event_id=stmt_event.id,
                principal="bank",
                rule_id=None,
            )
        return out

    # ------------------------------------------------------------------ statements in

    def ingest(self, statement_event: Event) -> list[Event]:
        """Parse an incoming ``bank_statement`` (world or own) and emit any line not yet seen as a
        ``bank_statement_line`` event. Idempotent on (account, value date, reference, amount, side)."""
        payload = statement_event.payload
        stmt = parse_mt940(str(payload["mt940"]))
        if stmt.statement_id != payload["statement_id"]:
            raise ValueError(
                f"statement id {payload['statement_id']} does not match its MT940 text ({stmt.statement_id})"
            )
        emitted: list[Event] = []
        own = stmt.statement_id.endswith("P")
        with self._lock:
            seen = self._con.execute(
                "SELECT 1 FROM bank_statements_seen WHERE statement_id = ?", (stmt.statement_id,)
            ).fetchone()
        if not own and seen is None:
            with self._lock:
                row = self._con.execute(
                    "SELECT * FROM bank_state WHERE currency = ?", (stmt.currency,)
                ).fetchone()
                if (
                    row is not None
                    and row["last_statement_date"] is not None
                    and int(row["world_balance_minor"]) != stmt.opening_balance_minor
                ):
                    self._store.append(
                        make_event(
                            "system_error",
                            {
                                "component": "bank",
                                "code": "statement_gap",
                                "message": f"{stmt.statement_id} opens at {stmt.opening_balance_minor} but the previous statement closed at {row['world_balance_minor']}",
                                "refs": [stmt.statement_id],
                                "recoverable": True,
                            },
                            event_time=self._now(),
                            source="bank",
                            actor="bank",
                        )
                    )
                self._con.execute(
                    "UPDATE bank_state SET world_balance_minor = ?, last_statement_date = ?, last_statement_id = ? WHERE currency = ?",
                    (
                        stmt.closing_balance_minor,
                        stmt.statement_date.isoformat(),
                        stmt.statement_id,
                        stmt.currency,
                    ),
                )
                self._con.execute(
                    "INSERT OR IGNORE INTO bank_statements_seen(statement_id, lines, known_date, currency) VALUES (?, ?, ?, ?)",
                    (
                        stmt.statement_id,
                        len(stmt.lines),
                        statement_event.known_at.date().isoformat(),
                        stmt.currency,
                    ),
                )
        for n, ln in enumerate(stmt.lines, start=1):
            key = self._line_key(stmt.account_iban, ln)
            if key in self._known_line_keys():
                continue
            ev = self._store.append(
                make_event(
                    "bank_statement_line",
                    self._line_payload(stmt, n, ln),
                    event_time=datetime.combine(ln.value_date, LINE_EVENT_TIME, tzinfo=UTC),
                    known_at=statement_event.known_at,
                    source="bank",
                    actor=statement_event.actor,
                )
            ).event
            self._remember_line(key)
            emitted.append(ev)
        return emitted

    @staticmethod
    def _line_payload(stmt: Statement, n: int, ln: StatementLine) -> dict[str, Any]:
        return {
            "statement_id": stmt.statement_id,
            "account_iban": stmt.account_iban,
            "currency": stmt.currency,
            "line_no": n,
            "value_date": ln.value_date.isoformat(),
            "entry_date": ln.entry_date.isoformat() if ln.entry_date else None,
            "dc": ln.dc,
            "amount_minor": ln.amount_minor,
            "txn_type": ln.txn_type,
            "customer_ref": ln.customer_ref,
            "bank_ref": ln.bank_ref,
            "remittance": ln.remittance,
        }

    @staticmethod
    def _line_key(iban: str, ln: StatementLine) -> str:
        return f"{iban}|{ln.value_date.isoformat()}|{ln.customer_ref}|{ln.amount_minor}|{ln.dc}"

    @staticmethod
    def line_key_of(payload: dict[str, Any]) -> str:
        return f"{payload['account_iban']}|{payload['value_date']}|{payload['customer_ref']}|{payload['amount_minor']}|{payload['dc']}"

    def _known_line_keys(self) -> set[str]:
        """Keys of every bank_statement_line event in the store, refreshed incrementally so lines
        the world inserted since the last call are seen too."""
        if self._line_keys is None:
            self._line_keys = set()
            self._line_keys_seq = 0
        for e in self._store.query(
            type="bank_statement_line", since_seq=self._line_keys_seq, limit=1_000_000
        ):
            self._line_keys.add(self.line_key_of(e.payload))
            if e.seq is not None:
                self._line_keys_seq = max(self._line_keys_seq, e.seq)
        return self._line_keys

    def _remember_line(self, key: str) -> None:
        self._known_line_keys().add(key)
