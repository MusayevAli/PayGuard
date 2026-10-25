#!/usr/bin/env python3
# ruff: noqa: T201
"""Exercise and verify the local physical path without approving a human decision.

PASS means the trusted local application reports USB UART hardware mode and the
checker verified its returned MACs, immutable bindings and accounting evidence.
It is not remote hardware attestation or a test of a real payment network.
"""

from __future__ import annotations

import argparse
import hashlib
import hmac
import ipaddress
import json
import os
import re
import stat
import sys
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlsplit
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

# isort: off
from signer.authority import (  # noqa: E402
    PROOF_SCHEMA,
    canonical_binding,
    evidence_digest,
    invoice_digest,
    load_role_key,
    validate_legacy_binding,
    verify_authorization,
)
from siliconsign.protocol import (  # noqa: E402
    COMMAND_MAGIC,
    RESPONSE_MAGIC,
    VERSION,
    Frame,
    Op,
    Status,
    authenticate,
)
# isort: on

REPORT_SCHEMA = "payguard.physical-path-acceptance.v1"
PAID = {"executed", "settled"}


class AcceptanceFailure(RuntimeError):
    """A public, sanitized acceptance failure; never embed HTTP response bodies."""


class JsonClient(Protocol):
    base_url: str

    def get(self, path: str) -> dict[str, Any]: ...
    def post(self, path: str) -> dict[str, Any]: ...


def require(condition: object, message: str) -> None:
    if not condition:
        raise AcceptanceFailure(message)


def validate_base_url(value: str) -> str:
    parts = urlsplit(value)
    require(
        parts.scheme == "http" and parts.hostname is not None, "Use an explicit HTTP loopback URL"
    )
    try:
        loopback = ipaddress.ip_address(parts.hostname).is_loopback
    except ValueError:
        loopback = False
    require(loopback, "Use a numeric loopback address such as http://127.0.0.1:8000")
    require(
        not parts.username
        and not parts.password
        and not parts.query
        and not parts.fragment
        and parts.path in ("", "/"),
        "The API URL must not contain credentials, a path, query or fragment",
    )
    _ = parts.port  # Reject malformed/out-of-range ports before loading credentials.
    return value.rstrip("/")


def read_token(path: Path | None) -> str:
    if path is None:
        token = os.environ.get("PAYGUARD_CONTROLLER_TOKEN", "")
    else:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
        with os.fdopen(fd, "r", encoding="utf-8") as source:
            meta = os.fstat(source.fileno())
            require(
                stat.S_ISREG(meta.st_mode)
                and meta.st_uid == os.geteuid()
                and not meta.st_mode & 0o077
                and meta.st_size <= 4096,
                "Controller token file must be private, owner-controlled and at most 4096 bytes",
            )
            token = source.read(4097).strip()
    require(
        32 <= len(token) <= 4096 and "\r" not in token and "\n" not in token,
        "Provide the controller token through a private file or PAYGUARD_CONTROLLER_TOKEN",
    )
    return token


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise AcceptanceFailure(
            "API redirects are forbidden; controller credentials stay on loopback"
        )


class LocalApi:
    def __init__(
        self,
        base_url: str,
        token: str | None,
        timeout: float = 30.0,
        fixture_timeout: float = 180.0,
    ):
        self.base_url = validate_base_url(base_url)
        self._token = token
        self.timeout = timeout
        self.fixture_timeout = fixture_timeout
        self._opener = build_opener(ProxyHandler({}), NoRedirect)

    def _request(self, method: str, path: str) -> dict[str, Any]:
        require(path.startswith("/") and not path.startswith("//"), "Invalid API path")
        headers = {"Accept": "application/json"}
        if method == "POST":
            require(
                self._token is not None,
                "Starting missing fixtures requires the controller credential",
            )
            headers["X-Payguard-Controller"] = self._token
            headers["Content-Type"] = "application/json"
        request = Request(
            self.base_url + path,
            data=b"{}" if method == "POST" else None,
            headers=headers,
            method=method,
        )
        try:
            # A fixture can perform several sequential model explanations. Keep
            # reads short while allowing a bounded, single mutation attempt.
            deadline = self.fixture_timeout if method == "POST" else self.timeout
            with self._opener.open(request, timeout=deadline) as response:
                raw = response.read(8_000_001)
            require(len(raw) <= 8_000_000, "API evidence exceeded the acceptance size limit")
            result = json.loads(raw)
            require(isinstance(result, dict), "API did not return a JSON object")
            return result
        except HTTPError as exc:
            raise AcceptanceFailure(
                f"API request failed with HTTP {exc.code}; inspect the local application"
            ) from None
        except (URLError, TimeoutError, OSError):
            if method == "POST":
                raise AcceptanceFailure(
                    "Fixture start timed out or disconnected; outcome UNKNOWN. "
                    "Re-read GET /demo after the service finishes; no automatic resend was issued"
                ) from None
            raise AcceptanceFailure(
                "Local API is unavailable or timed out; no retry was issued"
            ) from None
        except (ValueError, UnicodeError):
            raise AcceptanceFailure("Local API returned malformed JSON evidence") from None

    def get(self, path: str) -> dict[str, Any]:
        return self._request("GET", path)

    def post(self, path: str) -> dict[str, Any]:
        return self._request("POST", path)


def _preflight(chip: dict[str, Any], demo: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    require(
        chip.get("kind") == chip.get("signer_kind") == demo.get("signer_kind") == "hw",
        "Hardware signer is required; software stub mode cannot pass physical acceptance",
    )
    require(
        chip.get("bank_mode") == demo.get("bank_mode") == "simulated",
        "Acceptance must use the simulated bank",
    )
    hw = chip.get("hardware")
    require(isinstance(hw, dict), "Hardware backend metadata is missing")
    require(
        hw.get("bridge_mode") == "external-gateway", "The external-gateway UART bridge is required"
    )
    require(
        hw.get("transport") == "usb-uart",
        "A live USB UART backend is required; a model transport cannot pass",
    )
    caps = hw.get("capacity", {})
    require(isinstance(caps, dict), "FPGA capacity metadata is missing")
    require(
        type(caps.get("vendors")) is int
        and caps["vendors"] >= 64
        and type(caps.get("invoices")) is int
        and caps["invoices"] >= 256,
        "FPGA capacity must be at least 64 vendors and 256 immutable batches",
    )
    require(
        type(hw.get("clock_divider_hz")) is int and hw["clock_divider_hz"] == 50_000_000,
        "The verified 50 MHz compiled clock-divider image is required",
    )
    require(
        chip.get("key_loaded") is True and hw.get("locked") is False,
        "Hardware must be keyed and unlocked; uncertain outcomes require reconciliation",
    )
    require(
        chip.get("controller_required") is True and hw.get("physical_button") is False,
        "The authenticated software-controller profile must be explicit",
    )
    session = hw.get("session")
    require(
        isinstance(session, str)
        and re.fullmatch(r"[0-9a-f]{32}", session)
        and int(session, 16) != 0,
        "Hardware session is missing or invalid",
    )
    return session, hw


def _events(scenario: dict[str, Any], type_: str) -> list[dict[str, Any]]:
    return [
        e["payload"]
        for e in scenario["evidence_events"]
        if e.get("type") == type_ and e.get("payload", {}).get("pay_id") == scenario["pay_id"]
    ]


def _proof(
    scenario: dict[str, Any],
    session: str,
    key: bytes | None,
    expected_op: Op,
    expected_status: Status,
) -> tuple[dict[str, Any], dict[str, Any]]:
    name = scenario["id"]
    chip = scenario.get("chip_response")
    require(
        isinstance(chip, dict) and chip.get("signer") == "hw", f"{name}: no hardware chip response"
    )
    proof = chip.get("authorization")
    require(
        isinstance(proof, dict) and proof.get("schema") == PROOF_SCHEMA,
        f"{name}: full signed backend proof is absent",
    )
    raw = bytes.fromhex(proof["response_hex"])
    mac = bytes.fromhex(proof["response_mac_hex"])
    cmd = Frame.unpack(bytes.fromhex(proof["command_hex"]))
    response = Frame.unpack(raw)
    legacy = bytes.fromhex(proof["legacy_frame_hex"])
    require(
        len(mac) == 32 and any(mac) and raw.hex() == chip.get("raw_hex"),
        f"{name}: incomplete or inconsistent response bytes",
    )
    require(
        cmd.words[:3] == (COMMAND_MAGIC, VERSION, int(expected_op)) and not any(cmd.words[55:]),
        f"{name}: unexpected hardware command",
    )
    require(
        response.words[0] == RESPONSE_MAGIC
        and response.words[1:56] == cmd.words[1:56]
        and response.status == expected_status
        and response.words[63] == 1,
        f"{name}: hardware returned a different command/status or lost initialization",
    )
    require(cmd.field("session").hex() == session, f"{name}: proof belongs to a different session")
    require(
        chip.get("ok") is (expected_status == Status.OK),
        f"{name}: chip status contradicts its signed response",
    )
    requested = _events(scenario, "payment_requested")
    require(requested, f"{name}: independent payment-request audit record is absent")
    binding = canonical_binding(requested[-1])
    require(
        binding == canonical_binding(proof["binding"]),
        f"{name}: signed settlement differs from the audit record",
    )
    require(
        requested[-1]["frame_hex"] == legacy.hex(),
        f"{name}: legacy request does not match its audit record",
    )
    validate_legacy_binding(legacy, binding)
    require(
        cmd.field("account").hex() == binding["account_hash"]
        and cmd.field("amount") == binding["eur_equiv_minor"]
        and cmd.field("currency") == 978
        and cmd.field("invoice_hash") == invoice_digest(binding)
        and cmd.field("evidence") == evidence_digest(legacy, binding),
        f"{name}: native settlement/EUR binding digest mismatch",
    )
    if key is not None:
        require(
            hmac.compare_digest(mac, authenticate(key, raw)),
            f"{name}: independent response MAC verification failed",
        )
        if expected_status == Status.OK:
            verify_authorization(proof, legacy, key, binding, session)
    return binding, {
        "mac_verified_independently": key is not None,
        "operation": expected_op.name,
        "status": expected_status.name,
        "response_sha256": hashlib.sha256(raw).hexdigest(),
        "authorization_counter": response.words[57],
        "native_currency": binding["currency"],
        "native_minor": binding["amount_minor"],
        "eur_equiv_minor": binding["eur_equiv_minor"],
        "settlement_currency": binding["settlement_currency"],
        "settlement_minor": binding["settlement_minor"],
    }


def _ledger(scenario: dict[str, Any], binding: dict[str, Any], paid: bool) -> list[str]:
    entries = scenario.get("journal_entries", [])
    require(entries, f"{scenario['id']}: no recognition entries were exposed")
    for entry in entries:
        totals: dict[str, list[int]] = {}
        require(entry.get("lines"), "An accounting entry has no lines")
        for line in entry["lines"]:
            debit, credit = line["debit_minor"], line["credit_minor"]
            require(
                type(debit) is int
                and type(credit) is int
                and debit >= 0
                and credit >= 0
                and not (debit and credit),
                "An accounting line is invalid",
            )
            subtotal = totals.setdefault(line["currency"], [0, 0])
            subtotal[0] += debit
            subtotal[1] += credit
        require(
            all(d == c and d > 0 for d, c in totals.values()),
            "Ledger entry is not balanced in each displayed currency",
        )
    settlement = [e for e in entries if e.get("cause_id") == scenario["pay_id"]]
    if paid:
        require(
            len(settlement) == 1,
            f"{scenario['id']}: exactly one payment accounting entry is required",
        )
        cash = sum(
            line["credit_minor"]
            for line in settlement[0]["lines"]
            if line["account"] == "Assets:Bank:" + binding["settlement_currency"]
            and line["currency"] == binding["settlement_currency"]
        )
        require(
            cash == binding["settlement_minor"],
            f"{scenario['id']}: cash credit differs from signed native settlement",
        )
    else:
        require(
            not settlement,
            f"{scenario['id']}: refused/pending payment unexpectedly has settlement entries",
        )
    return [e["id"] for e in entries]


def run_acceptance(
    client: JsonClient, *, verify: bool, response_key: bytes | None
) -> dict[str, Any]:
    chip, demo = client.get("/chip/status"), client.get("/demo")
    session, hw = _preflight(chip, demo)
    scenarios = {s["id"]: s for s in demo.get("scenarios", [])}
    require(
        set(scenarios) == {"normal", "attack", "large"},
        "The three current demo fixtures are required",
    )
    started = []
    for name in ("normal", "attack", "large"):
        if not scenarios[name].get("started"):
            require(not verify, "A demo fixture has not run; --verify performs no writes")
            client.post("/demo/scenarios/" + name)
            started.append(name)
    demo = client.get("/demo")
    session_after, _ = _preflight(client.get("/chip/status"), demo)
    require(session_after == session, "Hardware session changed during acceptance")
    scenarios = {s["id"]: s for s in demo["scenarios"]}
    findings: dict[str, Any] = {}
    pending = None
    paid_counters = []
    for name in ("normal", "attack", "large"):
        scenario = scenarios[name]
        require(
            scenario.get("signer_kind") == "hw" and scenario.get("bank_mode") == "simulated",
            f"{name}: scenario mode differs from hardware preflight",
        )
        paid = scenario.get("status") in PAID
        if name == "normal":
            require(paid, "Normal legitimate payment did not execute")
        if name == "attack":
            require(
                scenario.get("status") == "held"
                and scenario.get("held_reason") == "not_allowlisted",
                "Forged-account scenario did not produce the expected held outcome",
            )
            op, status = Op.COMMIT_INVOICE, Status.ACCOUNT
        elif paid:
            op, status = Op.PAY, Status.OK
        else:
            require(
                name == "large"
                and scenario.get("status") in ("awaiting_decision", "awaiting_button"),
                "Large legitimate payment did not reach the human approval boundary",
            )
            op, status = Op.PAY, Status.APPROVAL
        binding, proof_result = _proof(scenario, session, response_key, op, status)
        entries = _ledger(scenario, binding, paid)
        executed = _events(scenario, "payment_executed")
        bank_ref = None
        if paid:
            require(
                len(executed) == 1,
                f"{name}: exactly one simulated-bank execution receipt is required",
            )
            bank_ref = executed[0].get("bank_ref")
            require(
                isinstance(bank_ref, str) and re.fullmatch(r"[A-Za-z0-9-]{1,80}", bank_ref),
                f"{name}: bank reference is missing",
            )
            require(
                executed[0]["amount_minor"] == binding["amount_minor"]
                and executed[0]["currency"] == binding["currency"],
                f"{name}: bank execution differs from signed native amount",
            )
            paid_counters.append(proof_result["authorization_counter"])
        else:
            require(not executed, f"{name}: bank execution exists for a refused/pending payment")
        if name == "large":
            packet_id = scenario.get("packet_id")
            require(
                isinstance(packet_id, str) and re.fullmatch(r"PK-[a-f0-9]+", packet_id),
                "Large payment has no evidence packet",
            )
            packet = client.get("/packets/" + quote(packet_id, safe=""))
            require(
                packet.get("id") == packet_id
                and len(packet.get("evidence_rows", [])) >= 2
                and packet.get("question")
                and packet.get("context")
                and packet.get("if_no_answer"),
                "Large payment lacks a full reviewable evidence packet",
            )
            require(
                scenario["pay_id"] in packet.get("refs", []),
                "Evidence packet is not bound to the large payment",
            )
            if paid:
                decision = packet.get("decision")
                require(
                    isinstance(decision, dict) and decision.get("decision") == "approve",
                    "Large payment lacks a recorded human approval decision",
                )
            else:
                pending = {
                    "pay_id": scenario["pay_id"],
                    "packet_id": packet_id,
                    "packet_url": client.base_url + "/packets/" + packet_id,
                    "instruction": "Review the complete evidence packet in the dashboard, decide whether to approve, then separately authorize this exact payment. This checker never makes either decision.",
                }
        findings[name] = {
            "observed_status": scenario["status"],
            "pay_id": scenario["pay_id"],
            "proof": proof_result,
            "bank_ref": bank_ref,
            "balanced_entry_ids": entries,
        }
    require(
        len(set(paid_counters)) == len(paid_counters),
        "Successful payments reused a hardware authorization counter",
    )
    incomplete = []
    if pending:
        incomplete.append(
            "Human evidence review and separate exact-payment authorization remain pending"
        )
    if response_key is None:
        incomplete.append(
            "Independent response MAC verification requires the private response-key file"
        )
    return {
        "schema": REPORT_SCHEMA,
        "status": "INCOMPLETE" if incomplete else "PASS",
        "checked_at": datetime.now(UTC).isoformat(),
        "api": client.base_url,
        "scope": "physical-path acceptance from a trusted local host; not hardware attestation",
        "verification_only": verify,
        "fixtures_started": started,
        "session": session,
        "hardware": {
            "bridge_mode": hw["bridge_mode"],
            "transport": hw["transport"],
            "capacity": hw["capacity"],
            "clock_divider_hz": hw["clock_divider_hz"],
        },
        "bank_mode": "simulated",
        "scenarios": findings,
        "pending_review": pending,
        "incomplete_reasons": incomplete,
        "limits": [
            "The local operator host, bridge and key files are trusted; these checks do not attest silicon or resist a malicious host administrator.",
            "Bank execution references and balanced entries are verified through the API; the bank's internal durable receipt table is not directly inspected.",
            "No real funds move. This is authenticated software approval, not a physical approval button.",
        ],
    }


def write_report(path: Path, report: dict[str, Any]) -> None:
    if path.exists():
        require(
            path.is_file() and not path.is_symlink(), "Report destination must be a regular file"
        )
        try:
            prior = json.loads(path.read_text())
        except (ValueError, OSError):
            raise AcceptanceFailure(
                "Refusing to overwrite a file that is not an acceptance report"
            ) from None
        require(
            isinstance(prior, dict) and prior.get("schema") == REPORT_SCHEMA,
            "Refusing to overwrite a file that is not an acceptance report",
        )
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        mode="w", encoding="utf-8", dir=path.parent, delete=False
    ) as output:
        temporary = Path(output.name)
        try:
            json.dump(report, output, sort_keys=True, indent=2)
            output.write("\n")
            output.flush()
            os.fsync(output.fileno())
        except BaseException:
            temporary.unlink(missing_ok=True)
            raise
    os.replace(temporary, path)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default="http://127.0.0.1:8000")
    parser.add_argument(
        "--verify",
        action="store_true",
        help="GET-only checks after a human reviews and authorizes the large payment",
    )
    parser.add_argument(
        "--token-file",
        type=Path,
        help="private controller-token file; alternatively PAYGUARD_CONTROLLER_TOKEN",
    )
    parser.add_argument(
        "--response-key-file",
        type=Path,
        default=os.environ.get("PAYGUARD_RESPONSE_KEY_FILE"),
        help="private response-only key; required for PASS, alternatively PAYGUARD_RESPONSE_KEY_FILE",
    )
    parser.add_argument(
        "--report", type=Path, required=True, help="sanitized JSON acceptance report destination"
    )
    args = parser.parse_args(argv)
    report: dict[str, Any]
    try:
        base_url = validate_base_url(args.base_url)
        for secret_path in (args.token_file, args.response_key_file):
            require(
                secret_path is None or args.report.resolve() != secret_path.resolve(),
                "Report path must differ from credential files",
            )
        token = None if args.verify else read_token(args.token_file)
        response_key = (
            load_role_key(args.response_key_file, "response") if args.response_key_file else None
        )
        report = run_acceptance(
            LocalApi(base_url, token), verify=args.verify, response_key=response_key
        )
    except AcceptanceFailure as exc:
        report = {
            "schema": REPORT_SCHEMA,
            "status": "FAIL",
            "checked_at": datetime.now(UTC).isoformat(),
            "error": str(exc),
        }
    except (KeyError, IndexError, AttributeError, TypeError, ValueError, OSError):
        report = {
            "schema": REPORT_SCHEMA,
            "status": "FAIL",
            "checked_at": datetime.now(UTC).isoformat(),
            "error": "Credentials, configuration or API evidence is malformed/unavailable; inspect the local service without retrying an uncertain payment",
        }
    try:
        write_report(args.report, report)
    except (AcceptanceFailure, OSError) as exc:
        print(
            "FAIL: could not safely save acceptance report"
            + (": " + str(exc) if isinstance(exc, AcceptanceFailure) else ""),
            file=sys.stderr,
        )
        return 1
    print(
        report["status"]
        + ": "
        + (
            report.get("error")
            or "checked the recorded normal, forged-account and large-payment outcomes"
        )
    )
    if report.get("pending_review"):
        print("PENDING FULL-EVIDENCE REVIEW: " + report["pending_review"]["packet_url"])
        print(report["pending_review"]["instruction"])
    for reason in report.get("incomplete_reasons", []):
        print(reason)
    print("Sanitized report: " + str(args.report))
    return {"PASS": 0, "FAIL": 1, "INCOMPLETE": 3}[report["status"]]


if __name__ == "__main__":
    raise SystemExit(main())
