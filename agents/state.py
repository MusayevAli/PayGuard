"""Invoice-agent state in the shared SQLite file: invoice status/facts, colleague waits, PO-line
consumption (partials, short-pay balances), GRNI accruals and receipt reassignments."""

from __future__ import annotations

import json
import sqlite3
import threading
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict

InvoiceStatus = Literal[
    "received", "extracted", "matched", "posted", "held", "rejected", "escalated"
]

SCHEMA = """
CREATE TABLE IF NOT EXISTS invoice_status (invoice_id TEXT PRIMARY KEY, status TEXT, rule_id TEXT, reason TEXT, vendor_id TEXT,
  invoice_number TEXT, entry_id TEXT, updated_at TEXT, facts TEXT);
CREATE TABLE IF NOT EXISTS invoice_waits (id INTEGER PRIMARY KEY AUTOINCREMENT, kind TEXT, invoice_id TEXT, po_id TEXT, line_no INTEGER,
  asked_event_id TEXT, deadline TEXT, created_at TEXT, resolved_at TEXT NULL, reply TEXT NULL);
CREATE TABLE IF NOT EXISTS po_line_consumption (po_id TEXT, line_no INTEGER, invoice_id TEXT, qty_posted INTEGER, qty_billed INTEGER,
  PRIMARY KEY (po_id, line_no, invoice_id));
CREATE TABLE IF NOT EXISTS accruals (po_id TEXT, line_no INTEGER, month TEXT, entry_id TEXT, qty INTEGER, reversed_by TEXT NULL,
  PRIMARY KEY (po_id, line_no, month));
CREATE TABLE IF NOT EXISTS receipt_reassignments (gr_id TEXT PRIMARY KEY, po_id TEXT, from_line INTEGER, to_line INTEGER, uom TEXT NULL,
  qty INTEGER NULL, invoice_id TEXT, at TEXT);
CREATE INDEX IF NOT EXISTS ix_waits_open ON invoice_waits(resolved_at, deadline);
"""


class InvoiceRow(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    invoice_id: str
    status: InvoiceStatus
    rule_id: str | None
    reason: str | None
    vendor_id: str | None
    invoice_number: str | None
    entry_id: str | None
    updated_at: datetime


class Wait(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    id: int
    kind: str
    invoice_id: str
    po_id: str | None
    line_no: int | None
    asked_event_id: str
    deadline: datetime
    created_at: datetime
    resolved_at: datetime | None
    reply: dict[str, Any] | None


class Accrual(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    po_id: str
    line_no: int
    month: str
    entry_id: str
    qty: int
    reversed_by: str | None


class Reassignment(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    gr_id: str
    po_id: str
    from_line: int
    to_line: int
    uom: str | None
    qty: int | None
    invoice_id: str


def _iso(dt: datetime) -> str:
    return dt.astimezone(UTC).isoformat().replace("+00:00", "Z")


class AgentState:
    def __init__(self, path: str | Path, *, timeout: float = 30.0) -> None:
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

    # ---------------------------------------------------------------- invoices

    def set_invoice(
        self,
        invoice_id: str,
        status: InvoiceStatus,
        *,
        rule_id: str | None,
        reason: str | None,
        now: datetime,
        vendor_id: str | None = None,
        invoice_number: str | None = None,
        entry_id: str | None = None,
        facts: dict[str, Any] | None = None,
    ) -> InvoiceRow:
        with self._lock:
            row = self._con.execute(
                "SELECT * FROM invoice_status WHERE invoice_id = ?", (invoice_id,)
            ).fetchone()
            prev = dict(row) if row else {}
            self._con.execute(
                "INSERT INTO invoice_status(invoice_id, status, rule_id, reason, vendor_id, invoice_number, entry_id, updated_at, facts) VALUES (?,?,?,?,?,?,?,?,?)"
                " ON CONFLICT(invoice_id) DO UPDATE SET status=excluded.status, rule_id=excluded.rule_id, reason=excluded.reason, vendor_id=excluded.vendor_id,"
                " invoice_number=excluded.invoice_number, entry_id=excluded.entry_id, updated_at=excluded.updated_at, facts=excluded.facts",
                (
                    invoice_id,
                    status,
                    rule_id,
                    reason,
                    vendor_id if vendor_id is not None else prev.get("vendor_id"),
                    invoice_number if invoice_number is not None else prev.get("invoice_number"),
                    entry_id if entry_id is not None else prev.get("entry_id"),
                    _iso(now),
                    json.dumps(facts, ensure_ascii=False)
                    if facts is not None
                    else prev.get("facts"),
                ),
            )
        got = self.invoice(invoice_id)
        assert got is not None
        return got

    def invoice(self, invoice_id: str) -> InvoiceRow | None:
        with self._lock:
            row = self._con.execute(
                "SELECT * FROM invoice_status WHERE invoice_id = ?", (invoice_id,)
            ).fetchone()
        return None if row is None else self._row(row)

    def invoices(
        self, status: InvoiceStatus | None = None, vendor_id: str | None = None
    ) -> list[InvoiceRow]:
        sql, params = "SELECT * FROM invoice_status WHERE 1=1", []
        if status:
            sql += " AND status = ?"
            params.append(status)
        if vendor_id:
            sql += " AND vendor_id = ?"
            params.append(vendor_id)
        sql += " ORDER BY invoice_id"
        with self._lock:
            rows = self._con.execute(sql, params).fetchall()
        return [self._row(r) for r in rows]

    def facts(self, invoice_id: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._con.execute(
                "SELECT facts FROM invoice_status WHERE invoice_id = ?", (invoice_id,)
            ).fetchone()
        if row is None or row["facts"] is None:
            return None
        return json.loads(row["facts"])

    @staticmethod
    def _row(row: sqlite3.Row) -> InvoiceRow:
        return InvoiceRow(
            invoice_id=row["invoice_id"],
            status=row["status"],
            rule_id=row["rule_id"],
            reason=row["reason"],
            vendor_id=row["vendor_id"],
            invoice_number=row["invoice_number"],
            entry_id=row["entry_id"],
            updated_at=datetime.fromisoformat(row["updated_at"]),
        )

    # ---------------------------------------------------------------- waits

    def add_wait(
        self,
        *,
        kind: str,
        invoice_id: str,
        po_id: str | None,
        line_no: int | None,
        asked_event_id: str,
        deadline: datetime,
        now: datetime,
    ) -> Wait:
        with self._lock:
            cur = self._con.execute(
                "INSERT INTO invoice_waits(kind, invoice_id, po_id, line_no, asked_event_id, deadline, created_at) VALUES (?,?,?,?,?,?,?)",
                (kind, invoice_id, po_id, line_no, asked_event_id, _iso(deadline), _iso(now)),
            )
            wait_id = int(cur.lastrowid or 0)
        got = self.wait(wait_id)
        assert got is not None
        return got

    def wait(self, wait_id: int) -> Wait | None:
        with self._lock:
            row = self._con.execute(
                "SELECT * FROM invoice_waits WHERE id = ?", (wait_id,)
            ).fetchone()
        return None if row is None else self._wait(row)

    def open_waits(self, invoice_id: str | None = None) -> list[Wait]:
        sql, params = "SELECT * FROM invoice_waits WHERE resolved_at IS NULL", []
        if invoice_id:
            sql += " AND invoice_id = ?"
            params.append(invoice_id)
        with self._lock:
            rows = self._con.execute(sql + " ORDER BY id", params).fetchall()
        return [self._wait(r) for r in rows]

    def wait_by_asked_event(self, event_id: str) -> Wait | None:
        with self._lock:
            row = self._con.execute(
                "SELECT * FROM invoice_waits WHERE asked_event_id = ? ORDER BY id DESC LIMIT 1",
                (event_id,),
            ).fetchone()
        return None if row is None else self._wait(row)

    def waits_for_po(self, po_id: str) -> list[Wait]:
        with self._lock:
            rows = self._con.execute(
                "SELECT * FROM invoice_waits WHERE po_id = ? AND resolved_at IS NULL ORDER BY id",
                (po_id,),
            ).fetchall()
        return [self._wait(r) for r in rows]

    def record_reply(self, wait_id: int, reply: dict[str, Any]) -> None:
        """Attach a colleague's reply; the wait stays open until the invoice posts, is held, or expires."""
        with self._lock:
            self._con.execute(
                "UPDATE invoice_waits SET reply = ? WHERE id = ?",
                (json.dumps(reply, ensure_ascii=False), wait_id),
            )

    def resolve_wait(self, wait_id: int, *, now: datetime, reply: dict[str, Any] | None) -> None:
        with self._lock:
            self._con.execute(
                "UPDATE invoice_waits SET resolved_at = ?, reply = ? WHERE id = ? AND resolved_at IS NULL",
                (
                    _iso(now),
                    json.dumps(reply, ensure_ascii=False) if reply is not None else None,
                    wait_id,
                ),
            )

    def last_reply(self, invoice_id: str, kind: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._con.execute(
                "SELECT reply FROM invoice_waits WHERE invoice_id = ? AND kind = ? AND reply IS NOT NULL ORDER BY id DESC LIMIT 1",
                (invoice_id, kind),
            ).fetchone()
        return None if row is None or row["reply"] is None else json.loads(row["reply"])

    @staticmethod
    def _wait(row: sqlite3.Row) -> Wait:
        return Wait(
            id=row["id"],
            kind=row["kind"],
            invoice_id=row["invoice_id"],
            po_id=row["po_id"],
            line_no=row["line_no"],
            asked_event_id=row["asked_event_id"],
            deadline=datetime.fromisoformat(row["deadline"]),
            created_at=datetime.fromisoformat(row["created_at"]),
            resolved_at=datetime.fromisoformat(row["resolved_at"]) if row["resolved_at"] else None,
            reply=json.loads(row["reply"]) if row["reply"] else None,
        )

    # ---------------------------------------------------------------- PO line consumption

    def record_consumption(
        self, po_id: str, line_no: int, invoice_id: str, qty_posted: int, qty_billed: int
    ) -> None:
        with self._lock:
            self._con.execute(
                "INSERT OR REPLACE INTO po_line_consumption VALUES (?,?,?,?,?)",
                (po_id, line_no, invoice_id, qty_posted, qty_billed),
            )

    def qty_posted(self, po_id: str, line_no: int, exclude_invoice: str | None = None) -> int:
        sql, params = (
            "SELECT COALESCE(SUM(qty_posted), 0) FROM po_line_consumption WHERE po_id = ? AND line_no = ?",
            [po_id, line_no],
        )
        if exclude_invoice:
            sql += " AND invoice_id != ?"
            params.append(exclude_invoice)
        with self._lock:
            return int(self._con.execute(sql, params).fetchone()[0])

    def po_line_balance(self, po_id: str, line_no: int) -> int:
        with self._lock:
            row = self._con.execute(
                "SELECT COALESCE(SUM(qty_billed - qty_posted), 0) FROM po_line_consumption WHERE po_id = ? AND line_no = ?",
                (po_id, line_no),
            ).fetchone()
        return int(row[0])

    def invoices_for_po(self, po_id: str) -> list[str]:
        with self._lock:
            rows = self._con.execute(
                "SELECT DISTINCT invoice_id FROM po_line_consumption WHERE po_id = ? ORDER BY invoice_id",
                (po_id,),
            ).fetchall()
        return [r[0] for r in rows]

    # ---------------------------------------------------------------- accruals (R7)

    def add_accrual(self, po_id: str, line_no: int, month: str, entry_id: str, qty: int) -> None:
        with self._lock:
            self._con.execute(
                "INSERT OR IGNORE INTO accruals(po_id, line_no, month, entry_id, qty) VALUES (?,?,?,?,?)",
                (po_id, line_no, month, entry_id, qty),
            )

    def open_accruals(self, po_id: str, line_no: int) -> list[Accrual]:
        with self._lock:
            rows = self._con.execute(
                "SELECT * FROM accruals WHERE po_id = ? AND line_no = ? AND reversed_by IS NULL ORDER BY month",
                (po_id, line_no),
            ).fetchall()
        return [Accrual(**dict(r)) for r in rows]

    def accrual_for_month(self, po_id: str, line_no: int, month: str) -> Accrual | None:
        with self._lock:
            row = self._con.execute(
                "SELECT * FROM accruals WHERE po_id = ? AND line_no = ? AND month = ?",
                (po_id, line_no, month),
            ).fetchone()
        return None if row is None else Accrual(**dict(row))

    def mark_accrual_reversed(self, entry_id: str, reversed_by: str) -> None:
        with self._lock:
            self._con.execute(
                "UPDATE accruals SET reversed_by = ? WHERE entry_id = ? AND reversed_by IS NULL",
                (reversed_by, entry_id),
            )

    # ---------------------------------------------------------------- receipt reassignments (M5/M6)

    def reassign_receipt(
        self,
        gr_id: str,
        po_id: str,
        from_line: int,
        to_line: int,
        *,
        uom: str | None,
        qty: int | None,
        invoice_id: str,
        now: datetime,
    ) -> None:
        with self._lock:
            self._con.execute(
                "INSERT OR REPLACE INTO receipt_reassignments VALUES (?,?,?,?,?,?,?,?)",
                (gr_id, po_id, from_line, to_line, uom, qty, invoice_id, _iso(now)),
            )

    def reassignments(self, po_id: str) -> list[Reassignment]:
        with self._lock:
            rows = self._con.execute(
                "SELECT gr_id, po_id, from_line, to_line, uom, qty, invoice_id FROM receipt_reassignments WHERE po_id = ?",
                (po_id,),
            ).fetchall()
        return [Reassignment(**dict(r)) for r in rows]
