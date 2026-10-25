"""Actual serial FPGA backend with durable host state and explicit operator authority.

The operator service, its key files, the ledger and the simulated bank are trusted.
This backend does not claim isolation from host root or provide a physical button.
The FPGA enforces EUR daily budgets and a real 60-second bank-change hold in the
explicit kv260-eur-demo-60s profile. Business simulation time never drives its clock.
"""

from __future__ import annotations

import copy
import hashlib
import hmac
import json
import os
import secrets
import sqlite3
import threading
import time
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal, NoReturn, Protocol, TypedDict, cast

# isort: off
# authority adds the vendored FPGA client to sys.path before these imports.
from signer.authority import (
    PROFILE,
    PROOF_SCHEMA,
    binding_bytes,
    canonical_binding,
    evidence_digest,
    invoice_digest,
    load_role_key,
    validate_legacy_binding,
)
from siliconsign.protocol import (
    ADMIN_OPS,
    CONTROLLER_OPS,
    RESPONSE_MAGIC,
    Frame,
    Op,
    Status,
    authenticate,
    command,
)
from siliconsign.transport import SerialTransport, TransportError

# isort: on
from signer.frames import (
    DAY_EPOCH,
    MAX_SLOTS,
    REASON_CODES,
    DayTickFrame,
    FrameError,
    NonceQueryFrame,
    PayFrame,
    ProvisionFrame,
    StatusFrame,
    decode,
    encode_ok,
    encode_reject,
    pack_day_tick,
    pack_nonce,
    pack_status,
)
from signer.interface import ChipStatus, SignResult, SlotInfo


class HardwareLocked(RuntimeError):
    """The hardware outcome/state is uncertain. Operator reconciliation is required."""


class HardwareUnavailable(HardwareLocked):
    """A read-only status exchange failed; no payment authority was granted."""


SequenceRole = Literal["admin_seq", "controller_seq", "pay_seq"]
HARDWARE_CLOCK_DIVIDER_HZ = 50_000_000


class HardwareState(TypedDict):
    profile: str
    session: str | None
    admin_seq: int
    controller_seq: int
    pay_seq: int
    legacy_seq: int
    hardware_time: int
    spent: int
    budget_day: int
    initialized: bool
    locked: bool
    lock_reason: str | None
    daily_cap: int
    threshold: int
    cooldown: int
    auth_counter: int
    simulation_day: int


class HardwareTransport(Protocol):
    def info(self) -> dict[str, Any]: ...
    def exchange(self, frame: bytes, mac: bytes) -> tuple[bytes, bytes]: ...
    def close(self) -> None: ...


def _decode_state(data: str) -> HardwareState:
    """Validate the durable JSON boundary before assigning precise internal types."""
    try:
        obj = json.loads(data)
    except (TypeError, ValueError) as exc:
        raise HardwareLocked("unreadable persisted hardware-state JSON") from exc
    integer_fields = {
        "admin_seq",
        "controller_seq",
        "pay_seq",
        "legacy_seq",
        "hardware_time",
        "spent",
        "budget_day",
        "daily_cap",
        "threshold",
        "cooldown",
        "auth_counter",
        "simulation_day",
    }
    keys = integer_fields | {"profile", "session", "initialized", "locked", "lock_reason"}
    if not isinstance(obj, dict) or set(obj) != keys:
        raise HardwareLocked("invalid persisted hardware-state schema; reconciliation required")
    if any(type(obj[k]) is not int or not 0 <= obj[k] < 2**64 for k in integer_fields):
        raise HardwareLocked("invalid persisted hardware-state integer")
    if (
        not isinstance(obj["profile"], str)
        or type(obj["initialized"]) is not bool
        or type(obj["locked"]) is not bool
        or (obj["lock_reason"] is not None and not isinstance(obj["lock_reason"], str))
    ):
        raise HardwareLocked("invalid persisted hardware-state metadata")
    session = obj["session"]
    if session is not None:
        try:
            valid = (
                isinstance(session, str)
                and len(bytes.fromhex(session)) == 16
                and any(bytes.fromhex(session))
            )
        except ValueError:
            valid = False
        if not valid:
            raise HardwareLocked("invalid persisted hardware session")
    if (
        (obj["initialized"] and session is None)
        or not 0 < obj["threshold"] <= obj["daily_cap"] < 2**63
        or obj["cooldown"] != 60
        or obj["simulation_day"] > 65535
        or any(
            obj[k] > 0xFFFFFFFF
            for k in ("admin_seq", "controller_seq", "pay_seq", "legacy_seq", "auth_counter")
        )
    ):
        raise HardwareLocked("invalid persisted hardware-state bounds")
    return cast(HardwareState, obj)


_SCHEMA = """
CREATE TABLE IF NOT EXISTS pg_hw_state (id INTEGER PRIMARY KEY CHECK(id=1), data TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS pg_hw_journal (
 id INTEGER PRIMARY KEY AUTOINCREMENT, command_hex TEXT NOT NULL, request_mac_hex TEXT NOT NULL,
 state TEXT NOT NULL, response_hex TEXT, response_mac_hex TEXT, error TEXT);
CREATE TABLE IF NOT EXISTS pg_hw_vendors (
 vendor_id TEXT PRIMARY KEY, slot INTEGER NOT NULL UNIQUE, account_hash TEXT NOT NULL UNIQUE,
 version INTEGER NOT NULL, cap INTEGER NOT NULL, hold_until_day INTEGER NOT NULL,
 hardware_hold_until INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS pg_hw_batches (
 pay_id TEXT PRIMARY KEY, slot INTEGER NOT NULL UNIQUE, binding_json TEXT NOT NULL,
 command_hex TEXT, committed INTEGER NOT NULL DEFAULT 0, paid INTEGER NOT NULL DEFAULT 0,
 approval_handle INTEGER, approval_until INTEGER NOT NULL DEFAULT 0, proof_json TEXT);
"""
_REASONS = {
    Status.SEQUENCE: "bad_seq",
    Status.VENDOR: "not_allowlisted",
    Status.ACCOUNT: "not_allowlisted",
    Status.DISABLED: "not_allowlisted",
    Status.COOLDOWN: "in_hold",
    Status.VENDOR_CAP: "over_account_cap",
    Status.DAILY_CAP: "over_daily_cap",
    Status.APPROVAL: "needs_button",
    Status.AUTH: "provisioning_locked",
    Status.PAID: "already_paid",
}


class HardwareSigner:
    kind = "hw"

    def __init__(
        self,
        path: str | Path,
        *,
        transport: HardwareTransport,
        admin_key: bytes,
        controller_key: bytes,
        response_key: bytes,
        initialize: bool = False,
        now: Callable[[], float] = time.time,
        daily_cap: int = 300_000_000,
        threshold: int = 5_000_000,
    ) -> None:
        keys = (admin_key, controller_key, response_key)
        if any(len(k) != 32 or not any(k) for k in keys) or len(set(keys)) != 3:
            raise ValueError("three distinct, nonzero 32-byte role keys are required")
        if (
            type(daily_cap) is not int
            or type(threshold) is not int
            or not 0 < threshold <= daily_cap < 2**63
        ):
            raise ValueError("invalid hardware EUR caps")
        self._admin_key, self._controller_key, self.response_key = keys
        self.transport = transport
        self._lock = threading.RLock()
        self._fixture_onboarding: dict[str, str] = {}
        self._con = sqlite3.connect(str(path), check_same_thread=False, isolation_level=None)
        self._closed = False
        self._status_read_failed = False
        self._con.row_factory = sqlite3.Row
        self._con.execute("PRAGMA journal_mode=WAL")
        self._con.execute("PRAGMA synchronous=FULL")
        self._con.executescript(_SCHEMA)
        stored = self._con.execute("SELECT data FROM pg_hw_state WHERE id=1").fetchone()
        self._state: HardwareState = {
            "profile": PROFILE,
            "session": None,
            "admin_seq": 0,
            "controller_seq": 0,
            "pay_seq": 0,
            "legacy_seq": 0,
            "hardware_time": 0,
            "spent": 0,
            "budget_day": 0,
            "initialized": False,
            "locked": False,
            "lock_reason": None,
            "daily_cap": daily_cap,
            "threshold": threshold,
            "cooldown": 60,
            "auth_counter": 0,
            "simulation_day": 0,
        }
        try:
            if stored:
                self._state = _decode_state(stored[0])
            if self._state["profile"] != PROFILE:
                raise HardwareLocked("persisted hardware profile differs")
            pending = self._con.execute(
                "SELECT * FROM pg_hw_journal WHERE state='pending' ORDER BY id"
            ).fetchall()
            recover_status = self._recoverable_status_polls(pending)
            if pending and not recover_status:
                self._lock_down(
                    "unfinished hardware transaction; reconcile journals before restarting"
                )
            if recover_status:
                # Provisional, in-memory only. The durable lock is cleared only
                # by a later authenticated STATUS and an atomic journal update.
                self._state["locked"] = False
                self._state["lock_reason"] = None
            last = self._con.execute(
                "SELECT state FROM pg_hw_journal ORDER BY id DESC LIMIT 1"
            ).fetchone()
            self._status_read_failed = bool(recover_status or (last and last[0] == "read_failed"))
            self._ensure_unlocked()
            resync = getattr(self.transport, "resynchronize_read_only", None)
            info = (
                resync() if self._status_read_failed and callable(resync) else self.transport.info()
            )
            caps = info.get("capabilities", {})
            if (
                info.get("mode") != "external-gateway"
                or type(caps.get("vendors")) is not int
                or type(caps.get("invoices")) is not int
                or caps["vendors"] < 64
                or caps["invoices"] < 256
            ):
                raise HardwareLocked(
                    "PayGuard requires the external-gateway bridge and new FPGA image with at least 64 vendor / 256 batch slots; the earlier 16-vendor image is incompatible"
                )
            self._capacity = {"vendors": caps["vendors"], "invoices": caps["invoices"]}
            self._bridge_mode = str(info["mode"])
            if (
                type(info.get("clock_divider_hz")) is not int
                or info["clock_divider_hz"] != HARDWARE_CLOCK_DIVIDER_HZ
            ):
                raise HardwareLocked(
                    "PayGuard requires the 50 MHz FPGA image and matching compiled clock divider; "
                    "the earlier 100 MHz image or an unknown divider is incompatible"
                )
            if info.get("locked") is not False:
                raise HardwareLocked("board bridge is locked; reconcile its durable journal")
            if stored:
                if info.get("session") != self._state["session"] or not self._state["initialized"]:
                    self._lock_down(
                        "board session changed or initialization was incomplete; automatic reset/reprovision is forbidden"
                    )
                expected_sequences = {
                    "next_admin_sequence": self._state["admin_seq"] + 1,
                    "next_controller_sequence": self._state["controller_seq"] + 1,
                    "next_payment_sequence": self._state["pay_seq"] + 1,
                }
                for name, expected in expected_sequences.items():
                    if info.get(name) != expected:
                        self._lock_down(
                            "board and host sequence journals disagree; operator reconciliation required"
                        )
                recovering_read = self._status_read_failed
                self._status_read_failed = False  # HELLO barrier above consumed stale read replies.
                self._read_status(
                    reconcile_ids=[row["id"] for row in pending] if recover_status else None,
                    recovering_read=recovering_read,
                )
            else:
                if not initialize:
                    raise HardwareLocked(
                        "fresh hardware session requires explicit PAYGUARD_HW_INITIALIZE=1"
                    )
                if info.get("session") is not None:
                    raise HardwareLocked(
                        "board already has a session without matching host state; refusing to reset it"
                    )
                if any(
                    info.get("next_" + role + "_sequence") not in (None, 1)
                    for role in ("admin", "controller", "payment")
                ):
                    raise HardwareLocked("fresh board sequence journal is not empty")
                self._state["session"] = secrets.token_bytes(16).hex()
                self._save_state()
                stamp = int(now())
                frame = command(
                    Op.INIT,
                    seq=1,
                    session=self.session,
                    currency=978,
                    timestamp=stamp,
                    daily_cap=daily_cap,
                    threshold=threshold,
                    cooldown=60,
                    max_count=65535,
                )

                def initialized(resp: Frame) -> None:
                    self._state["initialized"] = True
                    self._state["budget_day"] = stamp // 86400

                response, _ = self._exchange(frame, on_ok=initialized)
                if response.status != Status.OK:
                    self._lock_down("FPGA INIT rejected: " + response.status.name)
        except BaseException:
            self._con.close()
            self._closed = True
            raise

    @classmethod
    def from_env(cls, path: str | Path) -> HardwareSigner:
        names = (
            "PAYGUARD_ADMIN_KEY_FILE",
            "PAYGUARD_CONTROLLER_KEY_FILE",
            "PAYGUARD_RESPONSE_KEY_FILE",
        )
        missing = [name for name in (*names, "PAYGUARD_SERIAL_DEVICE") if not os.environ.get(name)]
        if missing:
            raise ValueError("hardware mode requires " + ", ".join(missing))
        keys = [
            load_role_key(os.environ[name], role)
            for name, role in zip(names, ("admin", "controller", "response"), strict=True)
        ]
        transport = SerialTransport(os.environ["PAYGUARD_SERIAL_DEVICE"])
        try:
            return cls(
                path,
                transport=transport,
                admin_key=keys[0],
                controller_key=keys[1],
                response_key=keys[2],
                initialize=os.environ.get("PAYGUARD_HW_INITIALIZE") == "1",
            )
        except BaseException:
            transport.close()
            raise

    @property
    def session(self) -> bytes:
        return bytes.fromhex(self._state["session"]) if self._state["session"] else bytes(16)

    def _save_state(self) -> None:
        self._con.execute(
            "INSERT INTO pg_hw_state(id,data) VALUES(1,?) ON CONFLICT(id) DO UPDATE SET data=excluded.data",
            (json.dumps(self._state, sort_keys=True),),
        )

    def _ensure_unlocked(self) -> None:
        if self._closed:
            raise HardwareLocked("hardware signer is closed")
        if self._state["locked"]:
            raise HardwareLocked(str(self._state["lock_reason"]))

    def _recoverable_status_polls(self, pending: list[sqlite3.Row]) -> bool:
        """Recognize only old-version locks caused by losing a read-only UART poll.

        A lost payment/commit/approval, an authentication failure, malformed
        response, or an unexplained crash always requires operator reconciliation.
        """
        if not pending or not self._state["initialized"] or not self._state["locked"]:
            return False
        reason = self._state["lock_reason"] or ""
        prefix = "uncertain hardware transaction; do not retry or reset: "
        if not reason.startswith(prefix):
            return False
        failure = reason[len(prefix) :]
        allowed = (
            "serial response timeout; do not reissue payment",
            "serial write timeout; outcome unknown",
            "serial disconnected",
        )
        if failure not in allowed:
            return False
        expected = command(Op.STATUS, session=self.session, currency=978).pack().hex()
        return all(
            row["command_hex"] == expected and row["request_mac_hex"] == bytes(32).hex()
            for row in pending
        )

    def _check_read_barrier(self, info: dict[str, Any]) -> None:
        """HELLO is a framing barrier, never proof of authorization or FPGA health."""
        expected = {
            "mode": self._bridge_mode,
            "session": self.session.hex(),
            "locked": False,
            "capabilities": self._capacity,
            "clock_divider_hz": HARDWARE_CLOCK_DIVIDER_HZ,
            "next_admin_sequence": self._state["admin_seq"] + 1,
            "next_controller_sequence": self._state["controller_seq"] + 1,
            "next_payment_sequence": self._state["pay_seq"] + 1,
        }
        if any(info.get(name) != value for name, value in expected.items()):
            self._lock_down(
                "read-only status recovery found a different bridge session or sequence"
            )

    def _lock_down(self, reason: str) -> NoReturn:
        self._state["locked"] = True
        self._state["lock_reason"] = reason
        self._save_state()
        raise HardwareLocked(reason)

    def _sequence(self, op: Op) -> int:
        key: SequenceRole = (
            "admin_seq"
            if op in ADMIN_OPS
            else "controller_seq"
            if op in CONTROLLER_OPS
            else "pay_seq"
        )
        value = self._state[key] + 1
        if value > 0xFFFFFFFF:
            self._lock_down("hardware sequence exhausted; no wrap permitted")
        return value

    def _exchange(
        self,
        frame: Frame,
        *,
        on_ok: Callable[[Frame], None] | None = None,
        on_result: Callable[[Frame, bytes], None] | None = None,
    ) -> tuple[Frame, bytes]:
        """Persist intent before sending, then atomically persist result and local effects."""
        self._ensure_unlocked()
        if frame.op != Op.STATUS and self._status_read_failed:
            # Never send a state-changing command into an unsynchronized stream.
            self._read_status()
        key = (
            self._admin_key
            if frame.op in ADMIN_OPS
            else self._controller_key
            if frame.op in CONTROLLER_OPS
            else None
        )
        mac = authenticate(key, frame) if key else bytes(32)
        cur = self._con.execute(
            "INSERT INTO pg_hw_journal(command_hex,request_mac_hex,state) VALUES(?,?,'pending')",
            (frame.pack().hex(), mac.hex()),
        )
        row_id = cur.lastrowid
        old_state = copy.deepcopy(self._state)
        try:
            raw, tag = self.transport.exchange(frame.pack(), mac)
            response = Frame.unpack(raw)
            if len(tag) != 32 or not hmac.compare_digest(tag, authenticate(self.response_key, raw)):
                raise HardwareLocked("invalid response MAC")
            if response.words[0] != RESPONSE_MAGIC or response.words[1:56] != frame.words[1:56]:
                raise HardwareLocked("response does not echo the complete submitted command")
            _ = response.status
            hw_time = response.integer(58, 2)
            if (
                hw_time < self._state["hardware_time"]
                or response.words[57] < self._state["auth_counter"]
            ):
                raise HardwareLocked("hardware time/authorization counter moved backwards")
            if self._state["initialized"] and not response.words[63]:
                raise HardwareLocked("FPGA lost initialization; refusing automatic reprovision")
            self._con.execute("BEGIN IMMEDIATE")
            self._state["hardware_time"] = hw_time
            self._state["spent"] = response.integer(60, 2)
            self._state["auth_counter"] = response.words[57]
            if response.status == Status.OK:
                if frame.op != Op.STATUS:
                    role: SequenceRole = (
                        "admin_seq"
                        if frame.op in ADMIN_OPS
                        else "controller_seq"
                        if frame.op in CONTROLLER_OPS
                        else "pay_seq"
                    )
                    self._state[role] = frame.words[3]
                if on_ok:
                    on_ok(response)
                if on_result:
                    on_result(response, tag)
            self._con.execute(
                "UPDATE pg_hw_journal SET state='complete',response_hex=?,response_mac_hex=? WHERE id=?",
                (raw.hex(), tag.hex(), row_id),
            )
            self._save_state()
            self._con.execute("COMMIT")
            return response, tag
        except BaseException as exc:
            if self._con.in_transaction:
                self._con.execute("ROLLBACK")
                self._state = old_state
            if frame.op == Op.STATUS and isinstance(exc, (TransportError, OSError, TimeoutError)):
                # STATUS has no sequence, ledger or authority effect. Preserve
                # its failed read, and require a clean authenticated read before
                # any later mutation. Integrity failures still lock below.
                self._con.execute(
                    "UPDATE pg_hw_journal SET state='read_failed',error=? WHERE id=?",
                    (f"{type(exc).__name__}: {exc}", row_id),
                )
                self._status_read_failed = True
                raise HardwareUnavailable(
                    "FPGA status is unavailable: "
                    + str(exc)
                    + "; no payment was authorized by this status request"
                ) from exc
            self._lock_down("uncertain hardware transaction; do not retry or reset: " + str(exc))

    def _read_status(
        self, *, reconcile_ids: list[int] | None = None, recovering_read: bool = False
    ) -> Frame:
        recovering_read = recovering_read or self._status_read_failed
        if self._status_read_failed:
            resync = getattr(self.transport, "resynchronize_read_only", None)
            try:
                info = resync() if callable(resync) else self.transport.info()
            except (TransportError, OSError, TimeoutError) as exc:
                raise HardwareUnavailable(
                    "FPGA status is unavailable: " + str(exc) + "; UART read recovery is incomplete"
                ) from exc
            self._check_read_barrier(info)
        before_counter, before_spent = self._state["auth_counter"], self._state["spent"]

        def reconcile(response: Frame) -> None:
            if not recovering_read:
                return
            if response.words[57] != before_counter or response.integer(60, 2) != before_spent:
                raise HardwareLocked(
                    "read-only status recovery found changed spending or authorization count"
                )
            for row_id in reconcile_ids or []:
                self._con.execute(
                    "UPDATE pg_hw_journal SET state='read_reconciled',error=? WHERE id=? AND state='pending'",
                    (
                        "Lost read-only STATUS reconciled by a new authenticated STATUS; no command replayed",
                        row_id,
                    ),
                )

        response, _ = self._exchange(
            command(Op.STATUS, session=self.session, currency=978), on_ok=reconcile
        )
        if response.status != Status.OK:
            self._lock_down("FPGA STATUS rejected")
        self._status_read_failed = False
        return response

    def _ok(self, payload: bytes = b"") -> SignResult:
        return SignResult(ok=True, raw=encode_ok(payload), payload=payload)

    def _reject(
        self,
        reason: str,
        response: bytes | None = None,
        latency: int = 0,
        authorization: dict[str, object] | None = None,
    ) -> SignResult:
        code = REASON_CODES.get(reason)
        return SignResult(
            ok=False,
            raw=response
            if response is not None
            else encode_reject(reason if code else "malformed"),
            reason=reason,
            reason_code=code,
            latency_ms=latency,
            authorization=authorization,
        )

    @staticmethod
    def _proof(
        request: Frame, response: Frame, mac: bytes, legacy: bytes, binding: dict[str, Any]
    ) -> dict[str, object]:
        # A signed rejection is evidence, not authority: verify_authorization requires
        # successful PAY, independently of the envelope's existence.
        return {
            "schema": PROOF_SCHEMA,
            "command_hex": request.pack().hex(),
            "response_hex": response.pack().hex(),
            "response_mac_hex": mac.hex(),
            "legacy_frame_hex": legacy.hex(),
            "binding": binding,
        }

    def provision_vendor(
        self,
        vendor_id: str,
        account_hash: bytes,
        cap: int,
        hold_until_day: int = 0,
        evidence: bytes | None = None,
    ) -> SignResult:
        """Trusted operator entry point. Production legacy provisioning checks the vendor master first."""
        with self._lock:
            self._ensure_unlocked()
            if (
                not vendor_id
                or len(account_hash) != 32
                or not any(account_hash)
                or type(cap) is not int
                or not 0 < cap < 2**63
            ):
                return self._reject("malformed")
            current = self._con.execute(
                "SELECT * FROM pg_hw_vendors WHERE vendor_id=?", (vendor_id,)
            ).fetchone()
            same_account = self._con.execute(
                "SELECT vendor_id FROM pg_hw_vendors WHERE account_hash=?", (account_hash.hex(),)
            ).fetchone()
            if same_account and same_account[0] != vendor_id:
                return self._reject("account_already_assigned")
            if current and current["cap"] != cap:
                return self._reject("hardware_caps_immutable")
            if current and current["account_hash"] == account_hash.hex():
                return self._ok(bytes([current["slot"]]))
            if current:
                slot, version, op = current["slot"], current["version"] + 1, Op.CHANGE_BANK
            else:
                used = {r[0] for r in self._con.execute("SELECT slot FROM pg_hw_vendors")}
                free = next((i for i in range(MAX_SLOTS) if i not in used), None)
                if free is None:
                    return self._reject("hardware_vendor_capacity")
                slot, version, op = free, 1, Op.ADD_VENDOR
            evidence = (
                evidence
                or hashlib.sha256(
                    json.dumps(
                        {
                            "vendor_id": vendor_id,
                            "account_hash": account_hash.hex(),
                            "cap": cap,
                            "business_hold_day": hold_until_day,
                        },
                        sort_keys=True,
                    ).encode()
                ).digest()
            )
            frame = command(
                op,
                seq=self._sequence(op),
                session=self.session,
                currency=978,
                vendor=slot,
                vendor_version=version,
                account=account_hash,
                vendor_cap=cap if op == Op.ADD_VENDOR else 0,
                evidence=evidence,
            )

            def applied(response: Frame) -> None:
                hold = response.integer(58, 2) + (60 if op == Op.CHANGE_BANK else 0)
                self._con.execute(
                    "INSERT INTO pg_hw_vendors VALUES(?,?,?,?,?,?,?) ON CONFLICT(vendor_id) DO UPDATE SET account_hash=excluded.account_hash,version=excluded.version,hold_until_day=excluded.hold_until_day,hardware_hold_until=excluded.hardware_hold_until",
                    (vendor_id, slot, account_hash.hex(), version, cap, hold_until_day, hold),
                )

            response, _ = self._exchange(frame, on_ok=applied)
            return (
                self._ok(bytes([slot]))
                if response.status == Status.OK
                else self._reject(
                    _REASONS.get(response.status, response.status.name.lower()), response.pack()
                )
            )

    def begin_fixture_onboarding(self, accounts: Mapping[str, str]) -> None:
        """Trusted startup only: authorize exact synthetic fixture ID/account pairs.

        This explicitly declared demo exception is not live due diligence. The
        caller derives these hashes from the reviewed fixture supplier file, never
        from newly requested vendor-bank records, and clears the scope in finally.
        """
        with self._lock:
            self._ensure_unlocked()
            checked: dict[str, str] = {}
            for vendor, account in accounts.items():
                if not isinstance(vendor, str) or not vendor or not isinstance(account, str):
                    raise ValueError(
                        "fixture onboarding requires exact vendor IDs and account hashes"
                    )
                digest = bytes.fromhex(account)
                if len(digest) != 32 or not any(digest):
                    raise ValueError("fixture account hash must be nonzero SHA256")
                checked[vendor] = digest.hex()
            if len(checked) > 64:
                raise ValueError("fixture vendor list exceeds hardware capacity")
            self._fixture_onboarding = checked

    def end_fixture_onboarding(self) -> None:
        with self._lock:
            self._fixture_onboarding.clear()

    def _provision(self, frame: ProvisionFrame) -> SignResult:
        if frame.slot >= MAX_SLOTS or frame.account_hash is None or frame.account_cap is None:
            return self._reject("provisioning_locked")
        try:
            rows = self._con.execute(
                "SELECT * FROM vendor_bank_accounts WHERE account_hash=? AND status='active'",
                (frame.account_hash.hex(),),
            ).fetchall()
        except sqlite3.OperationalError:
            return self._reject("vendor_evidence_required")
        if len(rows) != 1:
            return self._reject("vendor_evidence_required")
        row = rows[0]
        if (
            not row["approved_by"]
            or not row["verified_via"]
            or "email" in row["verified_via"].casefold()
            or row["verified_via"] == row["requested_via"]
        ):
            return self._reject("vendor_evidence_required")
        existing = self._con.execute(
            "SELECT 1 FROM pg_hw_vendors WHERE vendor_id=?", (row["vendor_id"],)
        ).fetchone()
        record = dict(row)
        fixture = (
            self._fixture_onboarding.get(row["vendor_id"]) == frame.account_hash.hex()
            and row["requested_via"] == "onboarding"
            and row["verified_via"] == "contract"
            and row["approved_by"] == "controller"
            and "approval_event_id" in record
            and record["approval_event_id"] is None
        )
        if not existing and not fixture:
            # No live credit/sanctions/identity provider is integrated. A phone
            # confirmation is necessary for bank changes but cannot replace it.
            return self._reject("vendor_due_diligence_unavailable")
        evidence = hashlib.sha256(
            json.dumps(
                {
                    "onboarding": "explicit_synthetic_fixture"
                    if not existing
                    else "existing_vendor_bank_change",
                    "record": record,
                },
                sort_keys=True,
            ).encode()
        ).digest()
        result = self.provision_vendor(
            row["vendor_id"],
            frame.account_hash,
            frame.account_cap,
            frame.hold_until_day or 0,
            evidence,
        )
        # The existing provisioner owns its host slot table; actual vendor-slot mapping
        # remains in pg_hw_vendors and is never inferred from the host's requested slot.
        return self._ok(bytes([frame.slot])) if result.ok else result

    def prepare_payment(self, frame: bytes, binding: dict[str, Any]) -> None:
        """Record an immutable, trusted controller settlement proposal before signing."""
        with self._lock:
            self._ensure_unlocked()
            b = canonical_binding(binding)
            validate_legacy_binding(frame, b)
            encoded = binding_bytes(b).decode()
            existing = self._con.execute(
                "SELECT binding_json FROM pg_hw_batches WHERE pay_id=?", (b["pay_id"],)
            ).fetchone()
            if existing:
                if existing[0] != encoded:
                    raise ValueError(
                        "payment identity is already bound to different settlement details"
                    )
                return
            used = {r[0] for r in self._con.execute("SELECT slot FROM pg_hw_batches")}
            free = next((i for i in range(256) if i not in used), None)
            if free is None:
                raise HardwareLocked(
                    "all 256 immutable hardware batch slots are used; operator recovery required"
                )
            self._con.execute(
                "INSERT INTO pg_hw_batches(pay_id,slot,binding_json) VALUES(?,?,?)",
                (b["pay_id"], free, encoded),
            )

    def _pay(self, raw64: bytes, pay: PayFrame) -> SignResult:
        started = time.perf_counter()
        if pay.seq != self._state["legacy_seq"] + 1:
            return self._reject("bad_seq")
        batch = self._con.execute(
            "SELECT * FROM pg_hw_batches WHERE pay_id=?", (pay.pay_id,)
        ).fetchone()
        if not batch:
            return self._reject("settlement_binding_required")
        binding = json.loads(batch["binding_json"])
        try:
            validate_legacy_binding(raw64, binding)
        except ValueError:
            return self._reject("immutable_batch_mismatch")
        if batch["paid"]:
            return self._reject("already_paid")
        if pay.nonce and (
            batch["approval_handle"] != pay.nonce
            or batch["approval_until"] <= self._state["hardware_time"]
        ):
            return self._reject("bad_nonce")
        vendor = self._con.execute(
            "SELECT * FROM pg_hw_vendors WHERE vendor_id=?", (binding["vendor_id"],)
        ).fetchone()
        if not vendor:
            return self._reject("not_allowlisted")
        request = command(
            Op.COMMIT_INVOICE,
            seq=self._sequence(Op.COMMIT_INVOICE),
            session=self.session,
            currency=978,
            vendor=vendor["slot"],
            invoice=batch["slot"],
            amount=pay.amount_minor,
            vendor_version=vendor["version"],
            account=pay.account_hash,
            invoice_hash=invoice_digest(binding),
            evidence=evidence_digest(raw64, binding),
        )
        if not batch["committed"]:

            def committed(_response: Frame) -> None:
                self._con.execute(
                    "UPDATE pg_hw_batches SET committed=1,command_hex=? WHERE pay_id=?",
                    (request.pack().hex(), pay.pay_id),
                )

            result, result_mac = self._exchange(request, on_ok=committed)
            if result.status != Status.OK:
                return self._reject(
                    _REASONS.get(result.status, result.status.name.lower()),
                    result.pack(),
                    authorization=self._proof(request, result, result_mac, raw64, binding),
                )
        else:
            original = Frame.unpack(bytes.fromhex(batch["command_hex"]))
            if (
                original.field("account") != pay.account_hash
                or original.words[14] != vendor["version"]
            ):
                return self._reject("bank_binding_changed")
            request = original
        payment = request.replace(
            op=Op.PAY, seq=self._sequence(Op.PAY), ttl=0, evidence=evidence_digest(raw64, binding)
        )

        def paid(response: Frame, verified_mac: bytes) -> None:
            self._state["legacy_seq"] = pay.seq
            # Persist the actual verified FPGA MAC; never re-sign a legacy request.
            proof = self._proof(payment, response, verified_mac, raw64, binding)
            self._con.execute(
                "UPDATE pg_hw_batches SET paid=1,approval_handle=NULL,approval_until=0,proof_json=? WHERE pay_id=?",
                (json.dumps(proof, sort_keys=True), pay.pay_id),
            )

        response, mac = self._exchange(payment, on_result=paid)
        latency = int((time.perf_counter() - started) * 1000)
        if response.status != Status.OK:
            return self._reject(
                _REASONS.get(response.status, response.status.name.lower()),
                response.pack(),
                latency,
                authorization=self._proof(payment, response, mac, raw64, binding),
            )
        proof = json.loads(
            self._con.execute(
                "SELECT proof_json FROM pg_hw_batches WHERE pay_id=?", (pay.pay_id,)
            ).fetchone()[0]
        )
        if proof["response_mac_hex"] != mac.hex():
            self._lock_down("durable response authentication disagrees")
        return SignResult(
            ok=True,
            raw=response.pack(),
            payload=response.pack(),
            latency_ms=latency,
            authorization=proof,
        )

    def sign(self, frame: bytes) -> SignResult:
        with self._lock:
            self._ensure_unlocked()
            try:
                decoded = decode(frame)
            except FrameError as exc:
                return self._reject(exc.reason)
            if isinstance(decoded, PayFrame):
                return self._pay(frame, decoded)
            if isinstance(decoded, ProvisionFrame):
                return self._provision(decoded)
            if isinstance(decoded, DayTickFrame):
                self.tick_day(decoded.day)
                return self._ok(pack_day_tick(self._hardware_day(), self._state["spent"]))
            if isinstance(decoded, NonceQueryFrame):
                handle = self.nonce()
                return self._ok(pack_nonce(handle or 0, handle is not None))
            if isinstance(decoded, StatusFrame):
                s = self.status()
                return self._ok(
                    pack_status(
                        seq=s.seq,
                        day=s.day,
                        daily_cap=s.daily_cap_minor,
                        daily_spent=s.daily_spent_minor,
                        button_threshold=s.button_threshold_minor,
                        nonce_valid=s.nonce_valid,
                        slots_used=s.slots_used,
                        key_loaded=s.key_loaded,
                        nonce_timeout_days=s.nonce_timeout_days,
                    )
                )

    def approve_pending(self, pay_id: str) -> int:
        """Human-controller approval of one exact pending batch; not a physical button."""
        with self._lock:
            self._ensure_unlocked()
            self._read_status()
            row = self._con.execute(
                "SELECT * FROM pg_hw_batches WHERE pay_id=?", (pay_id,)
            ).fetchone()
            if not row or not row["committed"] or row["paid"]:
                raise ValueError("approval requires an exact committed, unpaid payment batch")
            original = Frame.unpack(bytes.fromhex(row["command_hex"]))
            approval = original.replace(
                op=Op.APPROVE_EXCEPTION, seq=self._sequence(Op.APPROVE_EXCEPTION), ttl=300
            )
            handle = secrets.randbelow(0xFFFFFFFF) + 1

            def applied(response: Frame) -> None:
                self._con.execute(
                    "UPDATE pg_hw_batches SET approval_handle=?,approval_until=? WHERE pay_id=?",
                    (handle, response.integer(58, 2) + 300, pay_id),
                )

            response, _ = self._exchange(approval, on_ok=applied)
            if response.status != Status.OK:
                raise ValueError("FPGA approval rejected: " + response.status.name)
            return handle

    def approval_ready(self, pay_id: str) -> bool:
        with self._lock:
            self._read_status()
            row = self._con.execute(
                "SELECT approval_until,paid FROM pg_hw_batches WHERE pay_id=?", (pay_id,)
            ).fetchone()
            return bool(row and not row[1] and row[0] > self._state["hardware_time"])

    def nonce(self) -> int | None:
        with self._lock:
            self._read_status()
            row = self._con.execute(
                "SELECT approval_handle FROM pg_hw_batches WHERE paid=0 AND approval_until>? ORDER BY slot DESC LIMIT 1",
                (self._state["hardware_time"],),
            ).fetchone()
            return row[0] if row else None

    def _hardware_day(self) -> int:
        epoch = int(datetime.combine(DAY_EPOCH, datetime.min.time(), tzinfo=UTC).timestamp())
        return min(65535, max(0, (self._state["hardware_time"] - epoch) // 86400))

    def tick_day(self, day: int) -> None:
        with self._lock:
            self._ensure_unlocked()
            if type(day) is not int or not 0 <= day <= 65535:
                raise ValueError("simulation day must be an unsigned 16-bit integer")
            self._read_status()
            self._state["simulation_day"] = day
            actual = self._state["hardware_time"]
            if actual // 86400 > self._state["budget_day"]:
                frame = command(
                    Op.ADVANCE_DAY,
                    seq=self._sequence(Op.ADVANCE_DAY),
                    session=self.session,
                    currency=978,
                    timestamp=actual,
                )

                def advanced(_response: Frame) -> None:
                    self._state["budget_day"] = actual // 86400

                response, _ = self._exchange(frame, on_ok=advanced)
                if response.status != Status.OK:
                    raise HardwareLocked("real-clock rollover rejected: " + response.status.name)
            self._save_state()

    def status(self) -> ChipStatus:
        with self._lock:
            self._read_status()
            rows = self._con.execute("SELECT * FROM pg_hw_vendors ORDER BY slot").fetchall()
            valid = bool(
                self._con.execute(
                    "SELECT 1 FROM pg_hw_batches WHERE paid=0 AND approval_until>? LIMIT 1",
                    (self._state["hardware_time"],),
                ).fetchone()
            )
            return ChipStatus(
                kind="hw",
                seq=self._state["legacy_seq"],
                day=self._hardware_day(),
                daily_cap_minor=self._state["daily_cap"],
                daily_spent_minor=self._state["spent"],
                button_threshold_minor=self._state["threshold"],
                nonce_valid=valid,
                slots_used=len(rows),
                key_loaded=True,
                nonce_timeout_days=0,
                slots=[
                    SlotInfo(r["slot"], r["account_hash"], r["cap"], r["hold_until_day"])
                    for r in rows
                ],
                hardware={
                    "profile": PROFILE,
                    "bridge_mode": self._bridge_mode,
                    "transport": "usb-uart"
                    if getattr(self.transport, "is_hardware", False) is True
                    else "test-model",
                    "session": self.session.hex(),
                    "clock": "fpga-real-time",
                    "clock_divider_hz": HARDWARE_CLOCK_DIVIDER_HZ,
                    "hardware_time": self._state["hardware_time"],
                    "simulation_day": self._state["simulation_day"],
                    "cooldown_seconds": 60,
                    "approval_ttl_seconds": 300,
                    "approval_kind": "trusted-controller-exact-batch",
                    "physical_button": False,
                    "live_vendor_vetting": "unavailable-new-vendor-enrollment-disabled",
                    "onboarding_exception": "explicit-reviewed-synthetic-fixture-id-and-account-only",
                    "cap_mode": "cumulative-vendor-and-global-daily-EUR",
                    "settlement_binding": "native-values-bound-by-SHA256-batch",
                    "capacity": self._capacity,
                    "locked": self._state["locked"],
                    "bank_holds": [
                        {"vendor_id": r["vendor_id"], "until": r["hardware_hold_until"]}
                        for r in rows
                    ],
                },
            )

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._con.close()
            self._closed = True
            self.transport.close()
