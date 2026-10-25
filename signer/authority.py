"""Shared hardware-proof encoding and verification; no legacy bank tags are minted.

This runs in PayGuard's trusted operator/bank process. It is not a privilege boundary
against that process or the host administrator. Keys must never be passed to LLM tools.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import stat
import sys
from pathlib import Path
from typing import Any

# The maintained FPGA client is vendored with its original package name.
_CLIENT = Path(__file__).resolve().parents[1] / "hardware" / "python"
if str(_CLIENT) not in sys.path:
    sys.path.insert(0, str(_CLIENT))

from siliconsign.protocol import (  # noqa: E402
    COMMAND_MAGIC,
    RESPONSE_MAGIC,
    VERSION,
    Frame,
    Op,
    Status,
    authenticate,
)

from signer.frames import CURRENCY_CODES, PayFrame, decode  # noqa: E402

PROOF_SCHEMA = "payguard.fpga.authorization.v1"
PROFILE = "kv260-eur-demo-60s"
_BATCH_DOMAIN = b"PayGuard FPGA immutable settlement batch v1\x00"
_EVIDENCE_DOMAIN = b"PayGuard FPGA request evidence v1\x00"


def load_role_key(path: str | Path, role: str | None = None) -> bytes:
    """Read private raw hex or one-role JSON, never the combined provisioning bundle."""
    roles = {"admin", "controller", "response"}
    if role is not None and role not in roles:
        raise ValueError("unknown key role")
    p = Path(path)
    fd = os.open(p, os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(fd, "r", encoding="ascii") as source:
        st = os.fstat(source.fileno())
        if (
            not stat.S_ISREG(st.st_mode)
            or st.st_uid != os.geteuid()
            or st.st_mode & 0o077
            or st.st_size > 4096
        ):
            raise ValueError("role key must be a private regular file owned by this operator")
        text = source.read(4097).strip()
    if text.startswith("{"):
        obj = json.loads(text)
        if not isinstance(obj, dict) or len(obj) != 1 or not set(obj) <= roles:
            raise ValueError("role key JSON must contain exactly one role")
        found = next(iter(obj))
        if role is not None and found != role:
            raise ValueError("key file belongs to a different role")
        text = obj[found]
    if not isinstance(text, str):
        raise ValueError("role key must be hexadecimal text")
    key = bytes.fromhex(text)
    if len(key) != 32 or not any(key):
        raise ValueError("role key must contain a nonzero 32-byte hexadecimal key")
    return key


def _positive(value: Any, name: str) -> int:
    if type(value) is not int or not 0 < value < 2**64:
        raise ValueError(f"{name} must be a positive unsigned 64-bit integer")
    return value


def canonical_binding(payload: dict[str, Any]) -> dict[str, Any]:
    """Select immutable economic fields; transport sequences/nonces are excluded.

    Coverage and credits make a partial/grouped payment an explicit settlement batch.
    Native settlement is bound by the batch digest; the FPGA cap unit is EUR cents.
    The trusted controller/bank remains responsible for FX and ledger validation.
    """
    if not isinstance(payload, dict):
        raise ValueError("payment binding must be an object")
    required = (
        "pay_id",
        "vendor_id",
        "account_hash",
        "amount_minor",
        "currency",
        "eur_equiv_minor",
        "settlement_minor",
        "settlement_currency",
        "payable_ids",
        "covered",
    )
    missing = [k for k in required if k not in payload]
    if missing:
        raise ValueError("payment binding is missing " + ", ".join(missing))
    pay_id = payload["pay_id"]
    if (
        not isinstance(pay_id, str)
        or not 1 <= len(pay_id) <= 12
        or pay_id != pay_id.strip()
        or not all(32 <= ord(c) <= 126 for c in pay_id)
    ):
        raise ValueError("invalid payment identity")
    vendor = payload["vendor_id"]
    if not isinstance(vendor, str) or not vendor:
        raise ValueError("vendor_id must be nonempty")
    account = bytes.fromhex(payload["account_hash"])
    if len(account) != 32 or not any(account):
        raise ValueError("account_hash must be nonzero SHA256")
    for name in ("currency", "settlement_currency"):
        if payload[name] not in CURRENCY_CODES:
            raise ValueError(f"unsupported {name}")
    ids = payload["payable_ids"]
    if (
        not isinstance(ids, list)
        or not ids
        or any(not isinstance(x, str) or not x for x in ids)
        or len(set(ids)) != len(ids)
    ):
        raise ValueError("payable_ids must be distinct nonempty strings")
    covered = payload["covered"]
    if (
        not isinstance(covered, dict)
        or not covered
        or any(not isinstance(k, str) or not k for k in covered)
    ):
        raise ValueError("covered must specify positive payable allocations")
    covered = {k: _positive(v, "covered amount") for k, v in sorted(covered.items())}
    credits = payload.get("credits", {})
    if not isinstance(credits, dict) or any(
        not isinstance(k, str) or not k or type(v) is not int or not -(2**63) < v < 0
        for k, v in credits.items()
    ):
        raise ValueError("credits must map payable identities to negative integer amounts")
    if set(credits) & set(covered) or set(credits) | set(covered) != set(ids):
        raise ValueError("payable_ids must equal the distinct coverage and credit identities")
    normalized_credits = dict(sorted(credits.items()))
    discount = payload.get("discount_minor", 0)
    if type(discount) is not int or not 0 <= discount < 2**64:
        raise ValueError("invalid discount amount")
    fx, rate_date = payload.get("fx_rate"), payload.get("rate_date")
    if any(x is not None and not isinstance(x, str) for x in (fx, rate_date)):
        raise ValueError("FX rate and rate_date must be strings or null")
    result = {
        "pay_id": pay_id,
        "vendor_id": vendor,
        "account_hash": account.hex(),
        "amount_minor": _positive(payload["amount_minor"], "amount_minor"),
        "currency": payload["currency"],
        "eur_equiv_minor": _positive(payload["eur_equiv_minor"], "eur_equiv_minor"),
        "settlement_minor": _positive(payload["settlement_minor"], "settlement_minor"),
        "settlement_currency": payload["settlement_currency"],
        "payable_ids": sorted(ids),
        "covered": covered,
        "credits": normalized_credits,
        "discount_minor": discount,
        "fx_rate": fx,
        "rate_date": rate_date,
    }
    if (
        sum(covered.values()) + sum(normalized_credits.values()) - discount
        != result["amount_minor"]
    ):
        raise ValueError("native amount does not equal coverage minus credits/discount")
    return result


def binding_bytes(binding: dict[str, Any]) -> bytes:
    return json.dumps(
        canonical_binding(binding), sort_keys=True, separators=(",", ":"), ensure_ascii=True
    ).encode("ascii")


def invoice_digest(binding: dict[str, Any]) -> bytes:
    return hashlib.sha256(_BATCH_DOMAIN + binding_bytes(binding)).digest()


def evidence_digest(raw64: bytes, binding: dict[str, Any]) -> bytes:
    if len(raw64) != 64:
        raise ValueError("legacy request must have 64 bytes")
    return hashlib.sha256(_EVIDENCE_DOMAIN + raw64 + binding_bytes(binding)).digest()


def validate_legacy_binding(raw64: bytes, binding: dict[str, Any]) -> PayFrame:
    pay = decode(raw64)
    b = canonical_binding(binding)
    if not isinstance(pay, PayFrame) or (
        pay.pay_id,
        pay.account_hash.hex(),
        pay.amount_minor,
        pay.currency,
    ) != (b["pay_id"], b["account_hash"], b["eur_equiv_minor"], b["currency"]):
        raise ValueError("legacy request differs from the approved settlement batch")
    return pay


def verify_authorization(
    authorization: dict[str, Any],
    raw64: bytes,
    response_key: bytes,
    expected_binding: dict[str, Any],
    expected_session: bytes | str | None = None,
) -> Frame:
    """Verify a full FPGA PAY proof. The bank must also consume it transactionally.

    This function does not debit accounts and does not replace the bank's independent
    ledger/FX checks, session allowlist, or durable authorization replay protection.
    """
    expected_keys = {
        "schema",
        "command_hex",
        "response_hex",
        "response_mac_hex",
        "legacy_frame_hex",
        "binding",
    }
    if (
        not isinstance(authorization, dict)
        or set(authorization) != expected_keys
        or authorization["schema"] != PROOF_SCHEMA
    ):
        raise ValueError("invalid FPGA authorization envelope")
    binding = canonical_binding(authorization["binding"])
    if binding != canonical_binding(expected_binding):
        raise ValueError("authorization differs from the independent payment record")
    if bytes.fromhex(authorization["legacy_frame_hex"]) != raw64:
        raise ValueError("authorization names a different legacy request")
    validate_legacy_binding(raw64, binding)
    cmd = Frame.unpack(bytes.fromhex(authorization["command_hex"]))
    response = Frame.unpack(bytes.fromhex(authorization["response_hex"]))
    mac = bytes.fromhex(authorization["response_mac_hex"])
    if (
        len(response_key) != 32
        or not any(response_key)
        or len(mac) != 32
        or not hmac.compare_digest(mac, authenticate(response_key, response))
    ):
        raise ValueError("invalid FPGA response authentication")
    if (
        cmd.words[:3] != (COMMAND_MAGIC, VERSION, int(Op.PAY))
        or any(cmd.words[55:])
        or cmd.words[11] & ~1
    ):
        raise ValueError("invalid FPGA PAY command")
    if (
        response.words[0] != RESPONSE_MAGIC
        or response.words[1:56] != cmd.words[1:56]
        or response.status != Status.OK
        or response.words[63] != 1
        or response.words[57] == 0
    ):
        raise ValueError("response is not an authenticated successful PAY for this command")
    if (
        not any(cmd.field("session"))
        or cmd.words[3] == 0
        or cmd.words[8] >= 64
        or cmd.words[9] >= 256
    ):
        raise ValueError("invalid session, sequence or hardware slot")
    if expected_session is not None:
        session = (
            bytes.fromhex(expected_session)
            if isinstance(expected_session, str)
            else expected_session
        )
        if cmd.field("session") != session:
            raise ValueError("hardware session differs from bank's active session")
    if (
        cmd.field("account") != bytes.fromhex(binding["account_hash"])
        or cmd.field("amount") != binding["eur_equiv_minor"]
        or cmd.words[10] != 978
        or cmd.field("invoice_hash") != invoice_digest(binding)
        or cmd.field("evidence") != evidence_digest(raw64, binding)
    ):
        raise ValueError("FPGA authorization does not bind the complete settlement")
    return response
