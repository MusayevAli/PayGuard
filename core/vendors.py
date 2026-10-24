"""Vendor master (spec §5.3): vendors and their bank accounts, with the account state the
policy layer enforces (pending → active → retired/suspended)."""

from __future__ import annotations

import hashlib
import re
import sqlite3
import threading
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict

from sim.models import Supplier

VendorStatus = Literal["active", "onboarding", "merged", "suspended"]
AccountStatus = Literal["pending", "active", "suspended", "retired"]

SCHEMA = """
CREATE TABLE IF NOT EXISTS vendors (
  id TEXT PRIMARY KEY, legal_name TEXT, vat_id TEXT, country TEXT, currency TEXT, terms TEXT,
  contact_person TEXT, status TEXT, merged_into TEXT NULL
);
CREATE TABLE IF NOT EXISTS vendor_bank_accounts (
  id TEXT PRIMARY KEY, vendor_id TEXT, iban_or_acct TEXT, account_hash TEXT, status TEXT,
  requested_at TEXT, requested_via TEXT, requested_by TEXT, request_event_id TEXT NULL,
  verified_via TEXT NULL, verified_event_id TEXT NULL, verified_at TEXT NULL, hold_until TEXT,
  approved_by TEXT NULL, approval_event_id TEXT NULL, approved_at TEXT NULL, on_chip INTEGER, retired_at TEXT NULL
);
CREATE INDEX IF NOT EXISTS ix_vba_vendor ON vendor_bank_accounts(vendor_id, status);
CREATE UNIQUE INDEX IF NOT EXISTS ux_vba_one_active ON vendor_bank_accounts(vendor_id) WHERE status = 'active';
"""


class Vendor(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    legal_name: str
    vat_id: str
    country: str
    currency: str
    terms: str
    contact_person: str
    status: VendorStatus
    merged_into: str | None


class VendorBankAccount(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    vendor_id: str
    iban_or_acct: str
    account_hash: str
    status: AccountStatus
    requested_at: AwareDatetime
    requested_via: str
    requested_by: str
    request_event_id: str | None
    verified_via: str | None
    verified_event_id: str | None
    verified_at: AwareDatetime | None
    hold_until: AwareDatetime
    approved_by: str | None
    approval_event_id: str | None
    approved_at: AwareDatetime | None
    on_chip: bool
    retired_at: AwareDatetime | None


def account_hash(iban_or_acct: str) -> str:
    """sha256 of the normalised account identifier (no whitespace, upper case); spec §10.2."""
    normalised = re.sub(r"\s+", "", iban_or_acct).upper()
    return hashlib.sha256(normalised.encode("ascii", "ignore")).hexdigest()


def _iso(dt: datetime | None) -> str | None:
    return None if dt is None else dt.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _dt(text: str | None) -> datetime | None:
    return None if text is None else datetime.fromisoformat(text)


class VendorMaster:
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

    # -- vendors ---------------------------------------------------------------------------

    def upsert_vendor(self, vendor: Vendor) -> Vendor:
        with self._lock:
            self._con.execute(
                "INSERT INTO vendors(id, legal_name, vat_id, country, currency, terms, contact_person, status, merged_into)"
                " VALUES (?,?,?,?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET legal_name=excluded.legal_name,"
                " vat_id=excluded.vat_id, country=excluded.country, currency=excluded.currency, terms=excluded.terms,"
                " contact_person=excluded.contact_person, status=excluded.status, merged_into=excluded.merged_into",
                (
                    vendor.id,
                    vendor.legal_name,
                    vendor.vat_id,
                    vendor.country,
                    vendor.currency,
                    vendor.terms,
                    vendor.contact_person,
                    vendor.status,
                    vendor.merged_into,
                ),
            )
        return vendor

    def get_vendor(self, vendor_id: str) -> Vendor | None:
        with self._lock:
            row = self._con.execute("SELECT * FROM vendors WHERE id = ?", (vendor_id,)).fetchone()
        return None if row is None else Vendor(**dict(row))

    def vendors(self) -> list[Vendor]:
        with self._lock:
            rows = self._con.execute("SELECT * FROM vendors ORDER BY id").fetchall()
        return [Vendor(**dict(r)) for r in rows]

    # -- bank accounts ---------------------------------------------------------------------

    def next_account_id(self) -> str:
        with self._lock:
            row = self._con.execute("SELECT COUNT(*) FROM vendor_bank_accounts").fetchone()
        return f"VBA-{int(row[0]) + 1:06d}"

    def insert_account(self, acct: VendorBankAccount) -> VendorBankAccount:
        with self._lock:
            self._con.execute(
                "INSERT INTO vendor_bank_accounts VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                self._values(acct),
            )
        return acct

    def update_account(self, acct: VendorBankAccount) -> VendorBankAccount:
        with self._lock:
            cur = self._con.execute(
                "UPDATE vendor_bank_accounts SET vendor_id=?, iban_or_acct=?, account_hash=?, status=?, requested_at=?,"
                " requested_via=?, requested_by=?, request_event_id=?, verified_via=?, verified_event_id=?, verified_at=?,"
                " hold_until=?, approved_by=?, approval_event_id=?, approved_at=?, on_chip=?, retired_at=? WHERE id=?",
                (*self._values(acct)[1:], acct.id),
            )
            if cur.rowcount != 1:
                raise KeyError(f"unknown vendor bank account {acct.id}")
        return acct

    def get_account(self, account_id: str) -> VendorBankAccount | None:
        with self._lock:
            row = self._con.execute(
                "SELECT * FROM vendor_bank_accounts WHERE id = ?", (account_id,)
            ).fetchone()
        return None if row is None else self._account(row)

    def accounts(
        self, vendor_id: str | None = None, status: AccountStatus | None = None
    ) -> list[VendorBankAccount]:
        sql = "SELECT * FROM vendor_bank_accounts WHERE 1=1"
        params: list[Any] = []
        if vendor_id is not None:
            sql += " AND vendor_id = ?"
            params.append(vendor_id)
        if status is not None:
            sql += " AND status = ?"
            params.append(status)
        sql += " ORDER BY id"
        with self._lock:
            rows = self._con.execute(sql, params).fetchall()
        return [self._account(r) for r in rows]

    def active_account(self, vendor_id: str) -> VendorBankAccount | None:
        found = self.accounts(vendor_id=vendor_id, status="active")
        return found[0] if found else None

    def account_by_hash(self, vendor_id: str, digest: str) -> VendorBankAccount | None:
        with self._lock:
            row = self._con.execute(
                "SELECT * FROM vendor_bank_accounts WHERE vendor_id = ? AND account_hash = ? ORDER BY id DESC LIMIT 1",
                (vendor_id, digest),
            ).fetchone()
        return None if row is None else self._account(row)

    # -- seeding from the world ------------------------------------------------------------

    def seed_from_suppliers(
        self, suppliers: Sequence[Supplier], *, now: datetime, approved_by: str = "controller"
    ) -> int:
        """Load the supplier master as vendors with one active, approved account each. Idempotent.

        A supplier sharing its VAT ID with an earlier-onboarded supplier (fault M1) is stored
        with ``status=merged`` and ``merged_into`` pointing at the canonical record.
        """
        by_vat: dict[str, Supplier] = {}
        for s in sorted(suppliers, key=lambda x: (x.onboarded_at, x.id)):
            by_vat.setdefault(s.vat_id, s)
        created = 0
        for s in suppliers:
            canonical = by_vat[s.vat_id]
            merged_into = canonical.id if canonical.id != s.id else None
            existing = self.get_vendor(s.id)
            self.upsert_vendor(
                Vendor(
                    id=s.id,
                    legal_name=s.legal_name,
                    vat_id=s.vat_id,
                    country=s.country,
                    currency=s.currency,
                    terms=s.terms,
                    contact_person=s.purchasing_contact,
                    status="merged" if merged_into else "active",
                    merged_into=merged_into,
                )
            )
            if existing is None:
                created += 1
            identifier = s.iban or self._local_identifier(s)
            if (
                self.account_by_hash(s.id, account_hash(identifier)) is None
                and self.active_account(s.id) is None
            ):
                onboarded = datetime(
                    s.onboarded_at.year, s.onboarded_at.month, s.onboarded_at.day, tzinfo=UTC
                )
                self.insert_account(
                    VendorBankAccount(
                        id=self.next_account_id(),
                        vendor_id=s.id,
                        iban_or_acct=identifier,
                        account_hash=account_hash(identifier),
                        status="active",
                        requested_at=onboarded,
                        requested_via="onboarding",
                        requested_by="onboarding",
                        request_event_id=None,
                        verified_via="contract",
                        verified_event_id=None,
                        verified_at=onboarded,
                        hold_until=onboarded,
                        approved_by=approved_by,
                        approval_event_id=None,
                        approved_at=onboarded,
                        on_chip=False,
                        retired_at=None,
                    )
                )
        return created

    @staticmethod
    def _local_identifier(s: Supplier) -> str:
        b = s.bank_account
        if b.kind == "us_aba":
            return f"ABA:{b.routing_number}:{b.account_number}"
        return f"JP:{b.bank_code}:{b.branch_code}:{b.account_number}"

    # -- row mapping -----------------------------------------------------------------------

    @staticmethod
    def _values(a: VendorBankAccount) -> tuple[Any, ...]:
        return (
            a.id,
            a.vendor_id,
            a.iban_or_acct,
            a.account_hash,
            a.status,
            _iso(a.requested_at),
            a.requested_via,
            a.requested_by,
            a.request_event_id,
            a.verified_via,
            a.verified_event_id,
            _iso(a.verified_at),
            _iso(a.hold_until),
            a.approved_by,
            a.approval_event_id,
            _iso(a.approved_at),
            1 if a.on_chip else 0,
            _iso(a.retired_at),
        )

    @staticmethod
    def _account(row: sqlite3.Row) -> VendorBankAccount:
        d = dict(row)
        return VendorBankAccount(
            id=d["id"],
            vendor_id=d["vendor_id"],
            iban_or_acct=d["iban_or_acct"],
            account_hash=d["account_hash"],
            status=d["status"],
            requested_at=_dt(d["requested_at"]) or datetime.min.replace(tzinfo=UTC),
            requested_via=d["requested_via"],
            requested_by=d["requested_by"],
            request_event_id=d["request_event_id"],
            verified_via=d["verified_via"],
            verified_event_id=d["verified_event_id"],
            verified_at=_dt(d["verified_at"]),
            hold_until=_dt(d["hold_until"]) or datetime.min.replace(tzinfo=UTC),
            approved_by=d["approved_by"],
            approval_event_id=d["approval_event_id"],
            approved_at=_dt(d["approved_at"]),
            on_chip=bool(d["on_chip"]),
            retired_at=_dt(d["retired_at"]),
        )
