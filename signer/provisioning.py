"""Host-side provisioning (spec §10.7): which accounts go on the chip, in which slot, only while the
jumper is present. Every attempt is a ``provisioning`` event; the slot table lives in SQLite so the
host remembers what the chip holds."""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import threading
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from typing import Final

import structlog

from core.events import EventStore, make_event
from core.policy import Policy, SetOnChipAction
from core.rules import (
    ACCOUNT_CAP_EUR_MINOR,
    BUTTON_THRESHOLD_MINOR,
    DAILY_CAP_EUR_MINOR,
    NONCE_TIMEOUT_DAYS,
)
from core.vendors import VendorBankAccount, VendorMaster
from signer.frames import (
    MAX_SLOTS,
    SLOT_BUTTON_THRESHOLD,
    SLOT_DAILY_CAP,
    SLOT_KEY,
    SLOT_NONCE_TIMEOUT,
    ProvisionFrame,
    day_index,
    encode,
)
from signer.interface import Signer
from sim.models import Event

log = structlog.get_logger("provisioning")

DEV_CHIP_KEY: Final[bytes] = hashlib.sha256(b"payguard dev chip key 2026").digest()
"""Development key shared by the stub and the mock bank; ``PAYGUARD_CHIP_KEY`` (hex) overrides it."""

DEMO_VENDORS: Final[tuple[str, ...]] = (
    "V-0001",  # Kovak Trim (T1, T8's legitimate account)
    "V-0009",
    "V-0034",
    "V-0017",  # Metallbau Richter (T9)
    "V-0008",
    "V-0038",  # Alpenmech (T10)
    "V-0018",
    "V-0006",
    "V-0028",
    "V-0036",
    "V-0040",  # the US supplier: USD settles from the EUR account
    "V-0004",  # Lasertech Bohemia (T4 credit note netting)
    "V-0013",
    "V-0035",
)
"""The 14 accounts provisioned before the demo (spec §10.7): the vendors with the largest
EUR-equivalent payables falling due inside the simulated window, leaving two free slots (T7)."""

SCHEMA = """
CREATE TABLE IF NOT EXISTS provisioning_slots (
  slot INTEGER PRIMARY KEY, account_id TEXT NOT NULL UNIQUE, vendor_id TEXT NOT NULL, account_hash TEXT NOT NULL, provisioned_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS provisioning_queue (vendor_id TEXT PRIMARY KEY, requested_at TEXT NOT NULL);
"""


def chip_key() -> bytes:
    text = os.environ.get("PAYGUARD_CHIP_KEY")
    if text:
        key = bytes.fromhex(text)
        if len(key) != 32:
            raise ValueError("PAYGUARD_CHIP_KEY must be 32 bytes in hex")
        return key
    return DEV_CHIP_KEY


def eligible_account(vendors: VendorMaster, vendor_id: str) -> VendorBankAccount | None:
    """§10.7: active, approved, verified over a channel other than the request channel."""
    for acct in vendors.accounts(vendor_id=vendor_id, status="active"):
        if (
            acct.approved_by is not None
            and acct.verified_via is not None
            and acct.verified_via != acct.requested_via
        ):
            return acct
    return None


class Provisioner:
    principal = "provisioner"

    def __init__(
        self,
        path: str | Path,
        *,
        signer: Signer,
        vendors: VendorMaster,
        policy: Policy,
        store: EventStore,
        now: Callable[[], datetime],
        jumper_present: Callable[[], bool],
        key: bytes | None = None,
    ) -> None:
        self._signer = signer
        self._vendors = vendors
        self._policy = policy
        self._store = store
        self._now = now
        self._jumper = jumper_present
        self._key = key or chip_key()
        self._lock = threading.RLock()
        self._con = sqlite3.connect(str(path), check_same_thread=False, isolation_level=None)
        self._con.row_factory = sqlite3.Row
        self._con.execute("PRAGMA journal_mode=WAL")
        self._con.executescript(SCHEMA)

    def close(self) -> None:
        with self._lock:
            self._con.close()

    @property
    def key(self) -> bytes:
        return self._key

    # ------------------------------------------------------------------ chip-wide settings

    def load_key_and_caps(
        self,
        *,
        daily_cap: int = DAILY_CAP_EUR_MINOR,
        button_threshold: int = BUTTON_THRESHOLD_MINOR,
        nonce_timeout_days: int = NONCE_TIMEOUT_DAYS,
    ) -> list[Event]:
        """Key, daily cap, button threshold and nonce timeout; each is a ``provisioning`` event."""
        out = []
        for name, frame in (
            ("key", ProvisionFrame(slot=SLOT_KEY, key=self._key)),
            ("daily_cap", ProvisionFrame(slot=SLOT_DAILY_CAP, daily_cap=daily_cap)),
            (
                "button_threshold",
                ProvisionFrame(slot=SLOT_BUTTON_THRESHOLD, button_threshold=button_threshold),
            ),
            (
                "nonce_timeout_days",
                ProvisionFrame(slot=SLOT_NONCE_TIMEOUT, nonce_timeout_days=nonce_timeout_days),
            ),
        ):
            if not self._jumper():
                out.append(self._event("chip", name, None, "", False, "jumper_absent"))
                continue
            res = self._signer.sign(encode(frame))
            out.append(self._event("chip", name, None, "", res.ok, None if res.ok else res.reason))
        return out

    # ------------------------------------------------------------------ accounts

    def slot_for(self, account_id: str) -> int | None:
        with self._lock:
            row = self._con.execute(
                "SELECT slot FROM provisioning_slots WHERE account_id = ?", (account_id,)
            ).fetchone()
        return None if row is None else int(row["slot"])

    def slots(self) -> list[dict[str, str | int]]:
        with self._lock:
            rows = self._con.execute("SELECT * FROM provisioning_slots ORDER BY slot").fetchall()
        return [dict(r) for r in rows]

    def enqueue(self, vendor_id: str, *, account_cap: int = ACCOUNT_CAP_EUR_MINOR) -> Event:
        """Put the vendor's eligible account on the chip now (jumper present) or queue it. Always
        returns the ``provisioning`` event that records the outcome."""
        acct = eligible_account(self._vendors, vendor_id)
        if acct is None:
            return self._event(vendor_id, "-", None, "", False, "no_eligible_account")
        existing = self.slot_for(acct.id)
        if existing is not None and acct.on_chip:
            return self._event(
                vendor_id, acct.id, existing, acct.account_hash[:12], True, "already_provisioned"
            )
        if not self._jumper():
            with self._lock:
                self._con.execute(
                    "INSERT OR IGNORE INTO provisioning_queue(vendor_id, requested_at) VALUES (?, ?)",
                    (vendor_id, self._now().isoformat()),
                )
            return self._event(
                vendor_id, acct.id, None, acct.account_hash[:12], False, "jumper_absent"
            )
        slot = existing if existing is not None else self._free_slot()
        if slot is None:
            return self._event(
                vendor_id, acct.id, None, acct.account_hash[:12], False, "no_free_slot"
            )
        frame = ProvisionFrame(
            slot=slot,
            account_hash=bytes.fromhex(acct.account_hash),
            account_cap=account_cap,
            hold_until_day=day_index(acct.hold_until.date()),
        )
        res = self._signer.sign(encode(frame))
        if not res.ok:
            return self._event(vendor_id, acct.id, slot, acct.account_hash[:12], False, res.reason)
        self._policy.execute(SetOnChipAction(account_id=acct.id), self.principal)
        with self._lock:
            self._con.execute(
                "INSERT INTO provisioning_slots(slot, account_id, vendor_id, account_hash, provisioned_at) VALUES (?,?,?,?,?)"
                " ON CONFLICT(slot) DO UPDATE SET account_id = excluded.account_id, vendor_id = excluded.vendor_id, account_hash = excluded.account_hash, provisioned_at = excluded.provisioned_at",
                (slot, acct.id, vendor_id, acct.account_hash, self._now().isoformat()),
            )
            self._con.execute("DELETE FROM provisioning_queue WHERE vendor_id = ?", (vendor_id,))
        return self._event(vendor_id, acct.id, slot, acct.account_hash[:12], True, None)

    def process_queue(self) -> list[Event]:
        """Provision everything queued while the jumper was absent (call with the jumper present)."""
        with self._lock:
            rows = self._con.execute(
                "SELECT vendor_id FROM provisioning_queue ORDER BY requested_at"
            ).fetchall()
        return [self.enqueue(str(r["vendor_id"])) for r in rows]

    def queued(self) -> list[str]:
        with self._lock:
            rows = self._con.execute(
                "SELECT vendor_id FROM provisioning_queue ORDER BY requested_at"
            ).fetchall()
        return [str(r["vendor_id"]) for r in rows]

    def _free_slot(self) -> int | None:
        used = {int(r["slot"]) for r in self.slots()}
        return next((s for s in range(MAX_SLOTS) if s not in used), None)

    def _event(
        self,
        vendor_id: str,
        account_id: str,
        slot: int | None,
        prefix: str,
        ok: bool,
        reason: str | None,
    ) -> Event:
        payload = {
            "vendor_id": vendor_id,
            "account_id": account_id,
            "slot": slot,
            "account_hash_prefix": prefix,
            "ok": ok,
            "reason": reason,
        }
        ev = self._store.append(
            make_event(
                "provisioning",
                payload,
                event_time=self._now(),
                source="provisioner",
                actor=self.principal,
            )
        ).event
        log.info("provisioning", **payload, event_id=ev.id, principal=self.principal, rule_id=None)
        return ev


def describe_slots(provisioner: Provisioner) -> str:
    return json.dumps(provisioner.slots())
