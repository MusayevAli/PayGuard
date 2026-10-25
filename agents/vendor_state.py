"""Vendor agent state: verification waits, the packets it raised, onboarding cases and scheduled
activations. Vendor and account truth lives in the vendor master; this is bookkeeping."""

from __future__ import annotations

import json
import sqlite3
import threading
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

SCHEMA = """
CREATE TABLE IF NOT EXISTS vendor_waits (
  id INTEGER PRIMARY KEY AUTOINCREMENT, kind TEXT NOT NULL, vendor_id TEXT NOT NULL, account_id TEXT NULL,
  person_id TEXT NOT NULL, channel TEXT NOT NULL, thread_id TEXT NULL, request_message_id TEXT NULL,
  asked_event_id TEXT NOT NULL, created_at TEXT NOT NULL, resolved_at TEXT NULL, reply_event_id TEXT NULL,
  verdict TEXT NULL, data TEXT NOT NULL DEFAULT '{}'
);
CREATE TABLE IF NOT EXISTS vendor_packets (
  packet_id TEXT PRIMARY KEY, kind TEXT NOT NULL, vendor_id TEXT NOT NULL, account_id TEXT NULL, pay_id TEXT NULL,
  data TEXT NOT NULL DEFAULT '{}', decided INTEGER NOT NULL DEFAULT 0, decision TEXT NULL
);
CREATE TABLE IF NOT EXISTS vendor_onboarding (
  vendor_id TEXT PRIMARY KEY, legal_name TEXT NOT NULL, vat_id TEXT NULL, iban TEXT NULL, invoice_id TEXT NULL,
  message_event_id TEXT NOT NULL, status TEXT NOT NULL, data TEXT NOT NULL DEFAULT '{}'
);
CREATE TABLE IF NOT EXISTS scheduled_activations (
  account_id TEXT PRIMARY KEY, vendor_id TEXT NOT NULL, due_at TEXT NOT NULL, timer_key TEXT NOT NULL, done INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS handled_requests (request_key TEXT PRIMARY KEY, account_id TEXT NULL, event_id TEXT NOT NULL);
"""


def _iso(dt: datetime) -> str:
    return dt.astimezone(UTC).isoformat().replace("+00:00", "Z")


@dataclass(frozen=True)
class VendorWait:
    id: int
    kind: str
    vendor_id: str
    account_id: str | None
    person_id: str
    channel: str
    thread_id: str | None
    request_message_id: str | None
    asked_event_id: str
    created_at: datetime
    resolved_at: datetime | None
    reply_event_id: str | None
    verdict: str | None
    data: dict[str, Any]


@dataclass(frozen=True)
class VendorPacket:
    packet_id: str
    kind: str
    vendor_id: str
    account_id: str | None
    pay_id: str | None
    data: dict[str, Any]
    decided: bool
    decision: str | None


@dataclass(frozen=True)
class Onboarding:
    vendor_id: str
    legal_name: str
    vat_id: str | None
    iban: str | None
    invoice_id: str | None
    message_event_id: str
    status: str
    data: dict[str, Any]


@dataclass(frozen=True)
class ScheduledActivation:
    account_id: str
    vendor_id: str
    due_at: datetime
    timer_key: str
    done: bool


class VendorState:
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

    # ------------------------------------------------------------------ waits

    def add_wait(
        self,
        *,
        kind: str,
        vendor_id: str,
        account_id: str | None,
        person_id: str,
        channel: str,
        thread_id: str | None,
        request_message_id: str | None,
        asked_event_id: str,
        now: datetime,
        data: dict[str, Any] | None = None,
    ) -> VendorWait:
        with self._lock:
            cur = self._con.execute(
                "INSERT INTO vendor_waits(kind, vendor_id, account_id, person_id, channel, thread_id, request_message_id, asked_event_id, created_at, data) VALUES (?,?,?,?,?,?,?,?,?,?)",
                (
                    kind,
                    vendor_id,
                    account_id,
                    person_id,
                    channel,
                    thread_id,
                    request_message_id,
                    asked_event_id,
                    _iso(now),
                    json.dumps(data or {}, default=str),
                ),
            )
            row = self._con.execute(
                "SELECT * FROM vendor_waits WHERE id = ?", (cur.lastrowid,)
            ).fetchone()
        return self._wait(row)

    def open_waits(self, vendor_id: str | None = None, kind: str | None = None) -> list[VendorWait]:
        sql, params = "SELECT * FROM vendor_waits WHERE resolved_at IS NULL", []
        if vendor_id is not None:
            sql += " AND vendor_id = ?"
            params.append(vendor_id)
        if kind is not None:
            sql += " AND kind = ?"
            params.append(kind)
        with self._lock:
            rows = self._con.execute(sql + " ORDER BY id", params).fetchall()
        return [self._wait(r) for r in rows]

    def waits_for_person(self, person_id: str) -> list[VendorWait]:
        with self._lock:
            rows = self._con.execute(
                "SELECT * FROM vendor_waits WHERE resolved_at IS NULL AND person_id = ? ORDER BY id",
                (person_id,),
            ).fetchall()
        return [self._wait(r) for r in rows]

    def resolve_wait(
        self, wait_id: int, *, now: datetime, reply_event_id: str, verdict: str
    ) -> None:
        with self._lock:
            self._con.execute(
                "UPDATE vendor_waits SET resolved_at = ?, reply_event_id = ?, verdict = ? WHERE id = ? AND resolved_at IS NULL",
                (_iso(now), reply_event_id, verdict, wait_id),
            )

    def waits_for_account(self, account_id: str) -> list[VendorWait]:
        with self._lock:
            rows = self._con.execute(
                "SELECT * FROM vendor_waits WHERE account_id = ? ORDER BY id", (account_id,)
            ).fetchall()
        return [self._wait(r) for r in rows]

    @staticmethod
    def _wait(row: sqlite3.Row) -> VendorWait:
        return VendorWait(
            id=int(row["id"]),
            kind=row["kind"],
            vendor_id=row["vendor_id"],
            account_id=row["account_id"],
            person_id=row["person_id"],
            channel=row["channel"],
            thread_id=row["thread_id"],
            request_message_id=row["request_message_id"],
            asked_event_id=row["asked_event_id"],
            created_at=datetime.fromisoformat(row["created_at"]),
            resolved_at=datetime.fromisoformat(row["resolved_at"]) if row["resolved_at"] else None,
            reply_event_id=row["reply_event_id"],
            verdict=row["verdict"],
            data=json.loads(row["data"]),
        )

    # ------------------------------------------------------------------ packets

    def add_packet(
        self,
        packet_id: str,
        *,
        kind: str,
        vendor_id: str,
        account_id: str | None,
        pay_id: str | None = None,
        data: dict[str, Any] | None = None,
    ) -> None:
        with self._lock:
            self._con.execute(
                "INSERT OR IGNORE INTO vendor_packets(packet_id, kind, vendor_id, account_id, pay_id, data) VALUES (?,?,?,?,?,?)",
                (
                    packet_id,
                    kind,
                    vendor_id,
                    account_id,
                    pay_id,
                    json.dumps(data or {}, default=str),
                ),
            )

    def packet(self, packet_id: str) -> VendorPacket | None:
        with self._lock:
            row = self._con.execute(
                "SELECT * FROM vendor_packets WHERE packet_id = ?", (packet_id,)
            ).fetchone()
        return None if row is None else self._packet(row)

    def packets(
        self, kind: str | None = None, vendor_id: str | None = None, account_id: str | None = None
    ) -> list[VendorPacket]:
        sql, params = "SELECT * FROM vendor_packets WHERE 1=1", []
        for col, val in (("kind", kind), ("vendor_id", vendor_id), ("account_id", account_id)):
            if val is not None:
                sql += f" AND {col} = ?"
                params.append(val)
        with self._lock:
            rows = self._con.execute(sql + " ORDER BY rowid", params).fetchall()
        return [self._packet(r) for r in rows]

    def decide_packet(self, packet_id: str, decision: str) -> None:
        with self._lock:
            self._con.execute(
                "UPDATE vendor_packets SET decided = 1, decision = ? WHERE packet_id = ?",
                (decision, packet_id),
            )

    @staticmethod
    def _packet(row: sqlite3.Row) -> VendorPacket:
        return VendorPacket(
            packet_id=row["packet_id"],
            kind=row["kind"],
            vendor_id=row["vendor_id"],
            account_id=row["account_id"],
            pay_id=row["pay_id"],
            data=json.loads(row["data"]),
            decided=bool(row["decided"]),
            decision=row["decision"],
        )

    # ------------------------------------------------------------------ onboarding

    def add_onboarding(
        self,
        *,
        vendor_id: str,
        legal_name: str,
        vat_id: str | None,
        iban: str | None,
        invoice_id: str | None,
        message_event_id: str,
        status: str,
        data: dict[str, Any] | None = None,
    ) -> Onboarding:
        with self._lock:
            self._con.execute(
                "INSERT OR REPLACE INTO vendor_onboarding(vendor_id, legal_name, vat_id, iban, invoice_id, message_event_id, status, data) VALUES (?,?,?,?,?,?,?,?)",
                (
                    vendor_id,
                    legal_name,
                    vat_id,
                    iban,
                    invoice_id,
                    message_event_id,
                    status,
                    json.dumps(data or {}, default=str),
                ),
            )
        row = self.onboarding(vendor_id)
        assert row is not None
        return row

    def onboarding(self, vendor_id: str) -> Onboarding | None:
        with self._lock:
            row = self._con.execute(
                "SELECT * FROM vendor_onboarding WHERE vendor_id = ?", (vendor_id,)
            ).fetchone()
        return (
            None
            if row is None
            else Onboarding(
                vendor_id=row["vendor_id"],
                legal_name=row["legal_name"],
                vat_id=row["vat_id"],
                iban=row["iban"],
                invoice_id=row["invoice_id"],
                message_event_id=row["message_event_id"],
                status=row["status"],
                data=json.loads(row["data"]),
            )
        )

    def onboarding_by_name(self, legal_name: str) -> Onboarding | None:
        with self._lock:
            row = self._con.execute(
                "SELECT * FROM vendor_onboarding WHERE lower(legal_name) = lower(?)", (legal_name,)
            ).fetchone()
        return None if row is None else self.onboarding(str(row["vendor_id"]))

    def set_onboarding_status(self, vendor_id: str, status: str) -> None:
        with self._lock:
            self._con.execute(
                "UPDATE vendor_onboarding SET status = ? WHERE vendor_id = ?", (status, vendor_id)
            )

    def onboarding_count(self) -> int:
        with self._lock:
            return int(self._con.execute("SELECT COUNT(*) FROM vendor_onboarding").fetchone()[0])

    # ------------------------------------------------------------------ activations and request keys

    def schedule_activation(
        self, *, account_id: str, vendor_id: str, due_at: datetime, timer_key: str
    ) -> None:
        with self._lock:
            self._con.execute(
                "INSERT OR REPLACE INTO scheduled_activations(account_id, vendor_id, due_at, timer_key, done) VALUES (?,?,?,?,0)",
                (account_id, vendor_id, _iso(due_at), timer_key),
            )

    def scheduled_activations(self, *, pending_only: bool = True) -> list[ScheduledActivation]:
        sql = "SELECT * FROM scheduled_activations" + (" WHERE done = 0" if pending_only else "")
        with self._lock:
            rows = self._con.execute(sql + " ORDER BY due_at").fetchall()
        return [
            ScheduledActivation(
                account_id=r["account_id"],
                vendor_id=r["vendor_id"],
                due_at=datetime.fromisoformat(r["due_at"]),
                timer_key=r["timer_key"],
                done=bool(r["done"]),
            )
            for r in rows
        ]

    def mark_activation_done(self, account_id: str) -> None:
        with self._lock:
            self._con.execute(
                "UPDATE scheduled_activations SET done = 1 WHERE account_id = ?", (account_id,)
            )

    def request_seen(self, request_key: str) -> str | None:
        with self._lock:
            row = self._con.execute(
                "SELECT event_id FROM handled_requests WHERE request_key = ?", (request_key,)
            ).fetchone()
        return None if row is None else str(row["event_id"])

    def remember_request(self, request_key: str, *, account_id: str | None, event_id: str) -> None:
        with self._lock:
            self._con.execute(
                "INSERT OR IGNORE INTO handled_requests(request_key, account_id, event_id) VALUES (?,?,?)",
                (request_key, account_id, event_id),
            )
