"""Payment agent state (SQLite, same database as the ledger): payments in flight, reconciliation of
statement lines, daily runs. Nothing here is a source of truth for money; the ledger and the
event store are."""

from __future__ import annotations

import json
import sqlite3
import threading
from dataclasses import dataclass
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any, Literal

PaymentStatus = Literal[
    "requested",
    "executed",
    "settled",
    "held",
    "awaiting_decision",
    "awaiting_button",
    "rejected",
    "cancelled",
]
LineStatus = Literal["matched", "batch", "fee", "fx", "fx_pending", "unmatched", "escalated"]

SCHEMA = """
CREATE TABLE IF NOT EXISTS payments (
  pay_id TEXT PRIMARY KEY, vendor_id TEXT NOT NULL, currency TEXT NOT NULL, amount_minor INTEGER NOT NULL,
  eur_equiv_minor INTEGER NOT NULL, payable_ids TEXT NOT NULL, account_id TEXT NOT NULL, account_hash TEXT NOT NULL,
  status TEXT NOT NULL, held_reason TEXT NULL, packet_id TEXT NULL, seq INTEGER NULL, frame_hex TEXT NULL, tag_hex TEXT NULL,
  bank_ref TEXT NULL, requested_event_id TEXT NULL, chip_event_id TEXT NULL, executed_event_id TEXT NULL,
  settled_event_id TEXT NULL, entry_id TEXT NULL, discount_minor INTEGER NOT NULL DEFAULT 0, netted_minor INTEGER NOT NULL DEFAULT 0,
  created_at TEXT NOT NULL, updated_at TEXT NOT NULL, extra TEXT NOT NULL DEFAULT '{}'
);
CREATE TABLE IF NOT EXISTS recon_lines (
  line_key TEXT PRIMARY KEY, event_id TEXT NOT NULL, statement_id TEXT NOT NULL, currency TEXT NOT NULL,
  value_date TEXT NOT NULL, status TEXT NOT NULL, entry_id TEXT NULL, pay_id TEXT NULL, note TEXT NULL, updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS payment_runs (day TEXT PRIMARY KEY, decision TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS payment_packets (packet_id TEXT PRIMARY KEY, kind TEXT NOT NULL, pay_id TEXT NULL, payable_id TEXT NULL, data TEXT NOT NULL, decided INTEGER NOT NULL DEFAULT 0);
"""


def _iso(dt: datetime) -> str:
    return dt.astimezone(UTC).isoformat().replace("+00:00", "Z")


@dataclass(frozen=True)
class PaymentRow:
    pay_id: str
    vendor_id: str
    currency: str
    amount_minor: int
    eur_equiv_minor: int
    payable_ids: tuple[str, ...]
    account_id: str
    account_hash: str
    status: str
    held_reason: str | None
    packet_id: str | None
    seq: int | None
    frame_hex: str | None
    tag_hex: str | None
    bank_ref: str | None
    requested_event_id: str | None
    chip_event_id: str | None
    executed_event_id: str | None
    settled_event_id: str | None
    entry_id: str | None
    discount_minor: int
    netted_minor: int
    extra: dict[str, Any]


@dataclass(frozen=True)
class LineRow:
    line_key: str
    event_id: str
    statement_id: str
    currency: str
    value_date: date
    status: str
    entry_id: str | None
    pay_id: str | None
    note: str | None


@dataclass(frozen=True)
class PacketRow:
    packet_id: str
    kind: str
    pay_id: str | None
    payable_id: str | None
    data: dict[str, Any]
    decided: bool


class PaymentState:
    def __init__(self, path: str | Path, *, timeout: float = 30.0) -> None:
        self._lock = threading.RLock()
        self._con = sqlite3.connect(
            str(path), timeout=timeout, check_same_thread=False, isolation_level=None
        )
        self._con.row_factory = sqlite3.Row
        self._con.execute("PRAGMA journal_mode=WAL")
        self._con.executescript(SCHEMA)

    def close(self) -> None:
        with self._lock:
            self._con.close()

    # ------------------------------------------------------------------ payments

    def next_pay_id(self) -> str:
        with self._lock:
            n = int(self._con.execute("SELECT COUNT(*) FROM payments").fetchone()[0]) + 1
        return f"PAY-{n:06d}"

    def add_payment(
        self,
        *,
        pay_id: str,
        vendor_id: str,
        currency: str,
        amount_minor: int,
        eur_equiv_minor: int,
        payable_ids: list[str],
        account_id: str,
        account_hash: str,
        status: str,
        now: datetime,
        discount_minor: int = 0,
        netted_minor: int = 0,
        extra: dict[str, Any] | None = None,
    ) -> PaymentRow:
        with self._lock:
            self._con.execute(
                "INSERT INTO payments(pay_id, vendor_id, currency, amount_minor, eur_equiv_minor, payable_ids, account_id, account_hash, status, discount_minor, netted_minor, created_at, updated_at, extra)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    pay_id,
                    vendor_id,
                    currency,
                    amount_minor,
                    eur_equiv_minor,
                    json.dumps(list(payable_ids)),
                    account_id,
                    account_hash,
                    status,
                    discount_minor,
                    netted_minor,
                    _iso(now),
                    _iso(now),
                    json.dumps(extra or {}),
                ),
            )
        row = self.payment(pay_id)
        assert row is not None
        return row

    def update_payment(self, pay_id: str, *, now: datetime, **fields: Any) -> PaymentRow:
        allowed = {
            "status",
            "held_reason",
            "packet_id",
            "seq",
            "frame_hex",
            "tag_hex",
            "bank_ref",
            "requested_event_id",
            "chip_event_id",
            "executed_event_id",
            "settled_event_id",
            "entry_id",
            "discount_minor",
            "netted_minor",
            "amount_minor",
            "eur_equiv_minor",
            "account_id",
            "account_hash",
            "payable_ids",
            "extra",
        }
        unknown = set(fields) - allowed
        if unknown:
            raise ValueError(f"unknown payment fields {sorted(unknown)}")
        sets, params = ["updated_at = ?"], [_iso(now)]
        for key, value in fields.items():
            sets.append(f"{key} = ?")
            params.append(json.dumps(value) if key in ("payable_ids", "extra") else value)
        params.append(pay_id)
        with self._lock:
            cur = self._con.execute(
                f"UPDATE payments SET {', '.join(sets)} WHERE pay_id = ?", params
            )
            if cur.rowcount != 1:
                raise KeyError(pay_id)
        row = self.payment(pay_id)
        assert row is not None
        return row

    def payment(self, pay_id: str) -> PaymentRow | None:
        with self._lock:
            row = self._con.execute("SELECT * FROM payments WHERE pay_id = ?", (pay_id,)).fetchone()
        return None if row is None else self._payment(row)

    def payments(self, status: str | None = None, vendor_id: str | None = None) -> list[PaymentRow]:
        sql, params = "SELECT * FROM payments WHERE 1=1", []
        if status is not None:
            sql += " AND status = ?"
            params.append(status)
        if vendor_id is not None:
            sql += " AND vendor_id = ?"
            params.append(vendor_id)
        with self._lock:
            rows = self._con.execute(sql + " ORDER BY pay_id", params).fetchall()
        return [self._payment(r) for r in rows]

    def payment_by_packet(self, packet_id: str) -> PaymentRow | None:
        with self._lock:
            row = self._con.execute(
                "SELECT * FROM payments WHERE packet_id = ?", (packet_id,)
            ).fetchone()
        return None if row is None else self._payment(row)

    def payment_for_payable(self, payable_id: str) -> PaymentRow | None:
        for p in self.payments():
            if payable_id in p.payable_ids and p.status not in ("rejected", "cancelled"):
                return p
        return None

    @staticmethod
    def _payment(row: sqlite3.Row) -> PaymentRow:
        return PaymentRow(
            pay_id=row["pay_id"],
            vendor_id=row["vendor_id"],
            currency=row["currency"],
            amount_minor=int(row["amount_minor"]),
            eur_equiv_minor=int(row["eur_equiv_minor"]),
            payable_ids=tuple(json.loads(row["payable_ids"])),
            account_id=row["account_id"],
            account_hash=row["account_hash"],
            status=row["status"],
            held_reason=row["held_reason"],
            packet_id=row["packet_id"],
            seq=row["seq"],
            frame_hex=row["frame_hex"],
            tag_hex=row["tag_hex"],
            bank_ref=row["bank_ref"],
            requested_event_id=row["requested_event_id"],
            chip_event_id=row["chip_event_id"],
            executed_event_id=row["executed_event_id"],
            settled_event_id=row["settled_event_id"],
            entry_id=row["entry_id"],
            discount_minor=int(row["discount_minor"]),
            netted_minor=int(row["netted_minor"]),
            extra=json.loads(row["extra"]),
        )

    # ------------------------------------------------------------------ statement lines

    def line(self, line_key: str) -> LineRow | None:
        with self._lock:
            row = self._con.execute(
                "SELECT * FROM recon_lines WHERE line_key = ?", (line_key,)
            ).fetchone()
        return None if row is None else self._line(row)

    def set_line(
        self,
        line_key: str,
        *,
        event_id: str,
        statement_id: str,
        currency: str,
        value_date: date,
        status: str,
        now: datetime,
        entry_id: str | None = None,
        pay_id: str | None = None,
        note: str | None = None,
    ) -> LineRow:
        with self._lock:
            self._con.execute(
                "INSERT INTO recon_lines(line_key, event_id, statement_id, currency, value_date, status, entry_id, pay_id, note, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?)"
                " ON CONFLICT(line_key) DO UPDATE SET status = excluded.status, entry_id = COALESCE(excluded.entry_id, recon_lines.entry_id),"
                " pay_id = COALESCE(excluded.pay_id, recon_lines.pay_id), note = COALESCE(excluded.note, recon_lines.note), updated_at = excluded.updated_at",
                (
                    line_key,
                    event_id,
                    statement_id,
                    currency,
                    value_date.isoformat(),
                    status,
                    entry_id,
                    pay_id,
                    note,
                    _iso(now),
                ),
            )
        row = self.line(line_key)
        assert row is not None
        return row

    def lines(self, status: str | None = None) -> list[LineRow]:
        sql, params = "SELECT * FROM recon_lines WHERE 1=1", []
        if status is not None:
            sql += " AND status = ?"
            params.append(status)
        with self._lock:
            rows = self._con.execute(sql + " ORDER BY value_date, line_key", params).fetchall()
        return [self._line(r) for r in rows]

    @staticmethod
    def _line(row: sqlite3.Row) -> LineRow:
        return LineRow(
            line_key=row["line_key"],
            event_id=row["event_id"],
            statement_id=row["statement_id"],
            currency=row["currency"],
            value_date=date.fromisoformat(row["value_date"]),
            status=row["status"],
            entry_id=row["entry_id"],
            pay_id=row["pay_id"],
            note=row["note"],
        )

    # ------------------------------------------------------------------ runs and packets

    def run_recorded(self, day: date) -> bool:
        with self._lock:
            return (
                self._con.execute(
                    "SELECT 1 FROM payment_runs WHERE day = ?", (day.isoformat(),)
                ).fetchone()
                is not None
            )

    def record_run(self, day: date, decision: dict[str, Any]) -> None:
        with self._lock:
            self._con.execute(
                "INSERT OR REPLACE INTO payment_runs(day, decision) VALUES (?, ?)",
                (day.isoformat(), json.dumps(decision, default=str)),
            )

    def add_packet(
        self,
        packet_id: str,
        *,
        kind: str,
        pay_id: str | None,
        payable_id: str | None,
        data: dict[str, Any],
    ) -> None:
        with self._lock:
            self._con.execute(
                "INSERT OR IGNORE INTO payment_packets(packet_id, kind, pay_id, payable_id, data, decided) VALUES (?,?,?,?,?,0)",
                (packet_id, kind, pay_id, payable_id, json.dumps(data, default=str)),
            )

    def packet(self, packet_id: str) -> PacketRow | None:
        with self._lock:
            row = self._con.execute(
                "SELECT * FROM payment_packets WHERE packet_id = ?", (packet_id,)
            ).fetchone()
        return (
            None
            if row is None
            else PacketRow(
                packet_id=row["packet_id"],
                kind=row["kind"],
                pay_id=row["pay_id"],
                payable_id=row["payable_id"],
                data=json.loads(row["data"]),
                decided=bool(row["decided"]),
            )
        )

    def packets(self, kind: str | None = None, decided: bool | None = None) -> list[PacketRow]:
        sql, params = "SELECT * FROM payment_packets WHERE 1=1", []
        if kind is not None:
            sql += " AND kind = ?"
            params.append(kind)
        if decided is not None:
            sql += " AND decided = ?"
            params.append(1 if decided else 0)
        with self._lock:
            rows = self._con.execute(sql + " ORDER BY packet_id", params).fetchall()
        return [
            PacketRow(
                packet_id=r["packet_id"],
                kind=r["kind"],
                pay_id=r["pay_id"],
                payable_id=r["payable_id"],
                data=json.loads(r["data"]),
                decided=bool(r["decided"]),
            )
            for r in rows
        ]

    def set_packet_decision(self, packet_id: str, decision: str) -> None:
        with self._lock:
            row = self._con.execute(
                "SELECT data FROM payment_packets WHERE packet_id = ?", (packet_id,)
            ).fetchone()
            if row is None:
                return
            data = json.loads(row["data"])
            data["decision"] = decision
            self._con.execute(
                "UPDATE payment_packets SET data = ?, decided = 1 WHERE packet_id = ?",
                (json.dumps(data, default=str), packet_id),
            )

    def mark_packet_decided(self, packet_id: str) -> None:
        with self._lock:
            self._con.execute(
                "UPDATE payment_packets SET decided = 1 WHERE packet_id = ?", (packet_id,)
            )
