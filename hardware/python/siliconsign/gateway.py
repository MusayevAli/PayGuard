"""Trusted simulated-bank settlement and durable bridge journal.

This module is deliberately outside the untrusted agent process. It does not call a
bank or blockchain. SQLite protects crash consistency, not malicious disk rollback.
"""
from __future__ import annotations

from contextlib import closing, contextmanager
import hashlib
import hmac
import json
import os
from pathlib import Path
import sqlite3
import struct
import time
from typing import Iterator

COMMAND_MAGIC = 0x53494C43
RESPONSE_MAGIC = 0x53494C52
FRAME_SIZE = 256
MAX_SQL_INT = (1 << 63) - 1


class GatewayError(ValueError):
    """A payment or recovery action failed closed."""


class RecoveryRequired(GatewayError):
    """An unresolved command may already have reserved funds in hardware."""


def _words(frame: bytes) -> tuple[int, ...]:
    if not isinstance(frame, bytes) or len(frame) != FRAME_SIZE:
        raise GatewayError("frame must be exactly 256 bytes")
    return struct.unpack("<64I", frame)


def _amount(words: tuple[int, ...]) -> int:
    value = words[12] | (words[13] << 32)
    if not 0 < value <= MAX_SQL_INT:
        raise GatewayError("amount is outside supported signed 64-bit storage range")
    return value


def _command(frame: bytes) -> tuple[int, ...]:
    words = _words(frame)
    if words[0] != COMMAND_MAGIC or words[1] != 1 or not 1 <= words[2] <= 11:
        raise GatewayError("invalid command header")
    if words[55] or any(words[56:64]) or words[11] & ~1:
        raise GatewayError("noncanonical command fields")
    return words


def _binding(frame: bytes) -> bytes:
    words = _command(frame)
    # Invoice slots are restored without renumbering during recovery. Session and
    # sequence are intentionally excluded so an unpaid invoice survives a reboot.
    return struct.pack("<23I", *(words[8:15] + words[16:32]))


def _response(command: bytes, response: bytes, mac: bytes, key: bytes) -> tuple[int, ...]:
    _command(command)
    words = _words(response)
    if len(mac) != 32 or not hmac.compare_digest(hmac.digest(key, response, "sha256"), mac):
        raise GatewayError("response authentication failed")
    if words[0] != RESPONSE_MAGIC or words[1] != 1:
        raise GatewayError("invalid response header")
    if response[4:224] != command[4:224]:
        raise GatewayError("response does not match the submitted request")
    return words


def _key(key: bytes) -> bytes:
    if not isinstance(key, bytes) or len(key) != 32:
        raise ValueError("response key must contain exactly 32 bytes")
    return key


class _Database:
    def __init__(self, path: str | os.PathLike[str]):
        self.path = str(path)
        if self.path == ":memory:":
            raise ValueError("a persistent SQLite path is required")
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)

    def _connect(self) -> sqlite3.Connection:
        db = sqlite3.connect(self.path, timeout=30, isolation_level=None)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys=ON")
        db.execute("PRAGMA busy_timeout=30000")
        db.execute("PRAGMA synchronous=FULL")
        return db

    @contextmanager
    def _transaction(self) -> Iterator[sqlite3.Connection]:
        db = self._connect()
        try:
            db.execute("BEGIN IMMEDIATE")
            yield db
            db.execute("COMMIT")
        except BaseException:
            if db.in_transaction:
                db.execute("ROLLBACK")
            raise
        finally:
            db.close()


class Gateway(_Database):
    """Persistent, exactly-once simulated settlement and double-entry books.

    ``recognize`` is a trusted-controller operation, not an agent HTTP endpoint.
    Caller must independently authorize the liability before invoking it.
    Before REBIND_INVOICE, its recovery coordinator must inspect the authenticated
    hardware journal and set authorization_issued=True if any successful PAY exists;
    issued authorizations are consumed even when the bank has not settled them.
    """
    def __init__(self, path: str | os.PathLike[str], response_key: bytes):
        super().__init__(path)
        self.response_key = _key(response_key)
        with closing(self._connect()) as db:
            db.execute("PRAGMA journal_mode=WAL")
            db.executescript("""
                CREATE TABLE IF NOT EXISTS sessions(
                    session BLOB PRIMARY KEY CHECK(length(session)=16),
                    active INTEGER NOT NULL CHECK(active IN(0,1)),
                    created_ns INTEGER NOT NULL);
                CREATE UNIQUE INDEX IF NOT EXISTS one_active_session
                    ON sessions(active) WHERE active=1;
                CREATE TABLE IF NOT EXISTS obligations(
                    invoice BLOB PRIMARY KEY CHECK(length(invoice)=32),
                    binding BLOB NOT NULL,
                    amount INTEGER NOT NULL CHECK(amount>0),
                    currency INTEGER NOT NULL,
                    evidence BLOB NOT NULL CHECK(length(evidence)=32),
                    command BLOB NOT NULL CHECK(length(command)=256),
                    recognized_ns INTEGER NOT NULL);
                CREATE TABLE IF NOT EXISTS receipts(
                    receipt_id TEXT PRIMARY KEY,
                    session BLOB NOT NULL REFERENCES sessions(session),
                    counter INTEGER NOT NULL CHECK(counter>0),
                    invoice BLOB NOT NULL UNIQUE REFERENCES obligations(invoice),
                    command BLOB NOT NULL,
                    response BLOB NOT NULL,
                    mac BLOB NOT NULL,
                    amount INTEGER NOT NULL,
                    currency INTEGER NOT NULL,
                    settled_ns INTEGER NOT NULL,
                    UNIQUE(session,counter));
                CREATE TABLE IF NOT EXISTS entries(
                    id INTEGER PRIMARY KEY,
                    transaction_id TEXT NOT NULL,
                    invoice BLOB NOT NULL REFERENCES obligations(invoice),
                    account TEXT NOT NULL,
                    debit INTEGER NOT NULL DEFAULT 0,
                    credit INTEGER NOT NULL DEFAULT 0,
                    currency INTEGER NOT NULL,
                    CHECK((debit>0 AND credit=0) OR (credit>0 AND debit=0)),
                    UNIQUE(transaction_id,account));
                CREATE TABLE IF NOT EXISTS audit(
                    id INTEGER PRIMARY KEY,
                    kind TEXT NOT NULL,
                    data TEXT NOT NULL,
                    created_ns INTEGER NOT NULL);
            """)

    def activate_session(self, session: bytes) -> None:
        if not isinstance(session, bytes) or len(session) != 16 or not any(session):
            raise GatewayError("session must be a nonzero 128-bit value")
        with self._transaction() as db:
            prior = db.execute("SELECT active FROM sessions WHERE session=?", (session,)).fetchone()
            if prior is not None:
                if prior["active"]:
                    return
                raise GatewayError("a revoked session cannot be reactivated")
            db.execute("UPDATE sessions SET active=0 WHERE active=1")
            db.execute("INSERT INTO sessions VALUES(?,1,?)", (session, time.time_ns()))
            self._audit(db, "session_activated", {"session": session.hex()})

    def revoke_active_session(self) -> None:
        """Crash/recovery startup must revoke before serving new requests."""
        with self._transaction() as db:
            db.execute("UPDATE sessions SET active=0 WHERE active=1")
            self._audit(db, "sessions_revoked", {})

    @property
    def active_session(self) -> bytes | None:
        with closing(self._connect()) as db:
            row = db.execute("SELECT session FROM sessions WHERE active=1").fetchone()
            return bytes(row[0]) if row else None

    def recognize(self, command: bytes, *, authorization_issued: bool = False) -> dict:
        words = _command(command)
        if words[2] not in (4, 11):
            raise GatewayError("recognition requires a trusted COMMIT_INVOICE or REBIND_INVOICE command")
        amount = _amount(words)
        invoice = command[96:128]
        evidence = command[184:216]
        if not any(invoice) or not any(evidence) or not any(command[64:96]):
            raise GatewayError("invoice, account, and evidence must be nonzero")
        if words[8] >= 16 or words[9] >= 64 or words[10] == 0:
            raise GatewayError("invalid obligation identifiers")
        binding = _binding(command)
        with self._transaction() as db:
            prior = db.execute("SELECT * FROM obligations WHERE invoice=?", (invoice,)).fetchone()
            if words[2] == 11:
                if prior is None:
                    raise GatewayError("rebind requires an existing independently recognized liability")
                if authorization_issued:
                    raise GatewayError("cannot rebind an invoice with an issued hardware authorization")
                if db.execute("SELECT 1 FROM receipts WHERE invoice=?", (invoice,)).fetchone():
                    raise GatewayError("cannot rebind a settled invoice")
                previous = _command(bytes(prior["command"]))
                immutable = list(range(8, 14)) + list(range(24, 32))
                if any(words[i] != previous[i] for i in immutable):
                    raise GatewayError("rebind cannot change vendor, invoice, amount, currency, or flags")
                if words[14] <= previous[14]:
                    raise GatewayError("rebind requires a strictly newer vendor version")
                db.execute("UPDATE obligations SET binding=?,evidence=?,command=? WHERE invoice=?",
                           (binding, evidence, command, invoice))
                self._audit(db, "liability_rebound", {"invoice": invoice.hex(),
                            "old_version": previous[14], "new_version": words[14]})
                return {"invoice": invoice.hex(), "recognized": True, "rebound": True,
                        "idempotent": False}
            if prior:
                if bytes(prior["binding"]) != binding:
                    raise GatewayError("invoice identity already binds different payment details")
                return {"invoice": invoice.hex(), "recognized": True, "idempotent": True}
            db.execute("INSERT INTO obligations VALUES(?,?,?,?,?,?,?)",
                       (invoice, binding, amount, words[10], evidence, command, time.time_ns()))
            tx = "recognize:" + invoice.hex()
            self._entry(db, tx, invoice, "expense", amount, 0, words[10])
            self._entry(db, tx, invoice, "accounts_payable", 0, amount, words[10])
            self._audit(db, "liability_recognized", {"invoice": invoice.hex(), "amount_cents": amount})
        return {"invoice": invoice.hex(), "recognized": True, "idempotent": False}

    def settle(self, command: bytes, response: bytes, mac: bytes) -> dict:
        try:
            return self._settle(command, response, mac)
        except GatewayError as exc:
            with self._transaction() as db:
                self._audit(db, "payment_rejected", {
                    "reason": str(exc),
                    "request_hash": hashlib.sha256(command).hexdigest() if isinstance(command, bytes) else None,
                    "response_hash": hashlib.sha256(response).hexdigest() if isinstance(response, bytes) else None,
                })
            raise

    def _settle(self, command: bytes, response: bytes, mac: bytes) -> dict:
        words = _response(command, response, mac, self.response_key)
        if words[2] != 6 or words[56] != 0 or words[63] != 1 or words[57] == 0:
            raise GatewayError("response is not an active successful payment authorization")
        amount = _amount(words)
        session, invoice = command[16:32], command[96:128]
        counter = words[57]
        with self._transaction() as db:
            active = db.execute("SELECT active FROM sessions WHERE session=?", (session,)).fetchone()
            if not active or not active["active"]:
                raise GatewayError("payment belongs to an inactive session")
            prior = db.execute("SELECT * FROM receipts WHERE session=? AND counter=?",
                               (session, counter)).fetchone()
            if prior:
                if bytes(prior["response"]) == response and bytes(prior["mac"]) == mac and bytes(prior["command"]) == command:
                    return self._receipt(prior, True)
                raise GatewayError("authorization counter reused with different content")
            obligation = db.execute("SELECT * FROM obligations WHERE invoice=?", (invoice,)).fetchone()
            if not obligation or bytes(obligation["binding"]) != _binding(command):
                raise GatewayError("payment does not match an independently recognized liability")
            if db.execute("SELECT 1 FROM receipts WHERE invoice=?", (invoice,)).fetchone():
                raise GatewayError("invoice was already settled, including prior sessions")
            receipt_id = hashlib.sha256(b"SiliconSign-receipt-v1\0" + response + mac).hexdigest()
            settled_ns = time.time_ns()
            db.execute("INSERT INTO receipts VALUES(?,?,?,?,?,?,?,?,?,?)",
                       (receipt_id, session, counter, invoice, command, response, mac, amount, words[10], settled_ns))
            self._entry(db, receipt_id, invoice, "accounts_payable", amount, 0, words[10])
            self._entry(db, receipt_id, invoice, "cash", 0, amount, words[10])
            self._audit(db, "payment_settled", {"receipt_id": receipt_id, "invoice": invoice.hex(), "amount_cents": amount})
            row = db.execute("SELECT * FROM receipts WHERE receipt_id=?", (receipt_id,)).fetchone()
            return self._receipt(row, False)

    @staticmethod
    def _entry(db, tx, invoice, account, debit, credit, currency):
        db.execute("INSERT INTO entries(transaction_id,invoice,account,debit,credit,currency) VALUES(?,?,?,?,?,?)",
                   (tx, invoice, account, debit, credit, currency))

    @staticmethod
    def _audit(db, kind, data):
        db.execute("INSERT INTO audit(kind,data,created_ns) VALUES(?,?,?)",
                   (kind, json.dumps(data, sort_keys=True, separators=(",", ":")), time.time_ns()))

    @staticmethod
    def _receipt(row, idempotent):
        return {"receipt_id": row["receipt_id"], "invoice": bytes(row["invoice"]).hex(),
                "session": bytes(row["session"]).hex(), "counter": row["counter"],
                "amount_cents": row["amount"], "currency": row["currency"],
                "settled_ns": row["settled_ns"], "idempotent": idempotent}

    def snapshot(self) -> dict:
        with closing(self._connect()) as db:
            obligations = [dict(row) for row in db.execute("SELECT * FROM obligations ORDER BY recognized_ns,invoice")]
            receipts = [self._receipt(row, False) for row in db.execute("SELECT * FROM receipts ORDER BY settled_ns")]
            balances = [dict(row) for row in db.execute(
                "SELECT account,currency,SUM(debit) AS debit,SUM(credit) AS credit,SUM(debit)-SUM(credit) AS net FROM entries GROUP BY account,currency")]
            totals = [dict(row) for row in db.execute(
                "SELECT currency,SUM(debit) AS debit,SUM(credit) AS credit FROM entries GROUP BY currency")]
            return {"active_session": self.active_session, "obligations": obligations,
                    "receipts": receipts, "balances": balances, "totals": totals}

    def entries(self) -> list[dict]:
        with closing(self._connect()) as db:
            return [dict(row) for row in db.execute("SELECT * FROM entries ORDER BY id")]


class Journal(_Database):
    """Write-ahead record of every command sent to hardware.

    A pending intent is ambiguous after a crash: the FPGA might already have reserved
    money. ``assert_recoverable`` deliberately blocks automatic recovery. An operator
    must reconcile it; never delete a pending PAY or pretend it did not execute.
    """
    def __init__(self, path: str | os.PathLike[str], response_key: bytes):
        super().__init__(path)
        self.response_key = _key(response_key)
        with closing(self._connect()) as db:
            db.execute("PRAGMA journal_mode=WAL")
            db.executescript("""
                CREATE TABLE IF NOT EXISTS command_journal(
                    id INTEGER PRIMARY KEY,
                    command BLOB NOT NULL,
                    command_mac BLOB NOT NULL,
                    response BLOB,
                    response_mac BLOB,
                    intent_ns INTEGER NOT NULL,
                    completed_ns INTEGER,
                    CHECK((response IS NULL AND response_mac IS NULL AND completed_ns IS NULL)
                       OR (response IS NOT NULL AND response_mac IS NOT NULL AND completed_ns IS NOT NULL)));
                CREATE TABLE IF NOT EXISTS recovery_events(
                    id INTEGER PRIMARY KEY, data TEXT NOT NULL, created_ns INTEGER NOT NULL);
            """)

    def begin(self, frame: bytes, mac: bytes) -> int:
        _command(frame)
        if not isinstance(mac, bytes) or len(mac) != 32:
            raise GatewayError("command MAC must be 32 bytes, zero for unsigned requests")
        with self._transaction() as db:
            cursor = db.execute("INSERT INTO command_journal(command,command_mac,intent_ns) VALUES(?,?,?)",
                                (frame, mac, time.time_ns()))
            return cursor.lastrowid

    append_before = begin

    def finish(self, intent_id: int, response: bytes, mac: bytes) -> None:
        with self._transaction() as db:
            row = db.execute("SELECT * FROM command_journal WHERE id=?", (intent_id,)).fetchone()
            if not row:
                raise GatewayError("unknown journal intent")
            _response(bytes(row["command"]), response, mac, self.response_key)
            if row["response"] is not None:
                if bytes(row["response"]) == response and bytes(row["response_mac"]) == mac:
                    return
                raise GatewayError("journal intent cannot be completed twice differently")
            db.execute("UPDATE command_journal SET response=?,response_mac=?,completed_ns=? WHERE id=?",
                       (response, mac, time.time_ns(), intent_id))

    def pending(self) -> list[dict]:
        with closing(self._connect()) as db:
            return [dict(row) for row in db.execute("SELECT * FROM command_journal WHERE response IS NULL ORDER BY id")]

    def assert_recoverable(self) -> None:
        pending = self.pending()
        if pending:
            raise RecoveryRequired(f"{len(pending)} unresolved hardware command(s); reconciliation required")

    def records(self) -> list[dict]:
        with closing(self._connect()) as db:
            rows = [dict(row) for row in db.execute("SELECT * FROM command_journal ORDER BY id")]
        for row in rows:
            if row["response"] is not None:
                _response(bytes(row["command"]), bytes(row["response"]), bytes(row["response_mac"]), self.response_key)
        return rows

    @staticmethod
    def _recovery_data(old_session: bytes | None, new_session: bytes,
                       summary: dict, phase: str) -> dict:
        if (not isinstance(new_session, bytes) or len(new_session) != 16
                or not any(new_session)):
            raise GatewayError("invalid recovery session")
        if old_session is not None and (not isinstance(old_session, bytes)
                                       or len(old_session) != 16):
            raise GatewayError("invalid prior recovery session")
        return {"phase": phase,
                "old_session": old_session.hex() if old_session else None,
                "new_session": new_session.hex(), "summary": summary}

    @staticmethod
    def _last_recovery(db: sqlite3.Connection) -> dict | None:
        row = db.execute("SELECT data FROM recovery_events ORDER BY id DESC LIMIT 1").fetchone()
        if row is None:
            return None
        try:
            data = json.loads(row["data"])
        except (ValueError, TypeError) as exc:
            raise RecoveryRequired("invalid recovery generation marker") from exc
        if not isinstance(data, dict) or data.get("phase", "complete") not in ("preparing", "complete"):
            raise RecoveryRequired("invalid recovery generation marker")
        return data

    def assert_recovery_complete(self) -> None:
        """Do not derive a new snapshot from a partially provisioned generation.

        Journals written before generation markers existed (no event, or an old
        checkpoint without ``phase``) remain readable as completed generations.
        """
        with closing(self._connect()) as db:
            latest = self._last_recovery(db)
        if latest is not None and latest.get("phase", "complete") == "preparing":
            raise RecoveryRequired("incomplete recovery generation; operator reconciliation required")

    def begin_recovery(self, old_session: bytes | None, new_session: bytes,
                       summary: dict) -> None:
        """Persist this marker before INIT or any reprovisioning command is sent."""
        data = self._recovery_data(old_session, new_session, summary, "preparing")
        with self._transaction() as db:
            if db.execute("SELECT 1 FROM command_journal WHERE response IS NULL LIMIT 1").fetchone():
                raise RecoveryRequired("unresolved hardware command; reconciliation required")
            latest = self._last_recovery(db)
            if latest is not None and latest.get("phase", "complete") == "preparing":
                raise RecoveryRequired("incomplete recovery generation; operator reconciliation required")
            db.execute("INSERT INTO recovery_events(data,created_ns) VALUES(?,?)",
                       (json.dumps(data, sort_keys=True), time.time_ns()))

    def checkpoint(self, old_session: bytes | None, new_session: bytes, summary: dict) -> None:
        """Complete only after every provisioning response has been persisted."""
        data = self._recovery_data(old_session, new_session, summary, "complete")
        with self._transaction() as db:
            if db.execute("SELECT 1 FROM command_journal WHERE response IS NULL LIMIT 1").fetchone():
                raise RecoveryRequired("unresolved hardware command; reconciliation required")
            latest = self._last_recovery(db)
            if latest is not None and latest.get("phase", "complete") == "preparing":
                if any(latest.get(field) != data[field] for field in ("old_session", "new_session")):
                    raise RecoveryRequired("completion does not match the pending recovery generation")
            db.execute("INSERT INTO recovery_events(data,created_ns) VALUES(?,?)",
                       (json.dumps(data, sort_keys=True), time.time_ns()))
