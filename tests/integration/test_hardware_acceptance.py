"""Acceptance CLI contract against real API routes with explicit simulated hardware.

Setting the fixture's transport flag exercises the reported USB-path contract only;
these tests do not claim physical-board acceptance.
"""

from __future__ import annotations

import json

import pytest

from scripts.check_hardware_demo import (
    REPORT_SCHEMA,
    AcceptanceFailure,
    LocalApi,
    main,
    read_token,
    run_acceptance,
    validate_base_url,
    write_report,
)
from tests.integration.test_hardware_demo import AUTH, TOKEN, hardware_desk  # noqa: F401
from tests.unit.test_hardware_signer import KEYS


class ApiAdapter:
    base_url = "http://127.0.0.1:8000"

    def __init__(self, client):
        self.client = client
        self.calls = []
        self.alter = lambda path, obj: obj

    def get(self, path):
        self.calls.append(("GET", path))
        response = self.client.get(path)
        assert response.status_code == 200, response.text
        return self.alter(path, response.json())

    def post(self, path):
        self.calls.append(("POST", path))
        response = self.client.post(path, headers=AUTH)
        if response.status_code != 200:
            raise AcceptanceFailure("Mock API request failed; no retry")
        return response.json()


@pytest.fixture
def acceptance_desk(hardware_desk):  # noqa: F811
    rt, client, transport = hardware_desk
    # Explicitly simulate the live-path marker to test checker behavior against
    # actual HTTP routes; cryptography still comes from the reference model.
    transport.is_hardware = True
    return rt, client, transport, ApiAdapter(client)


def human_approves(client, report):
    pending = report["pending_review"]
    response = client.post(
        f"/packets/{pending['packet_id']}/decide",
        headers=AUTH,
        json={
            "decision": "approve",
            "note": "Test human reviewed evidence independently of checker",
            "decided_by": "test-human",
        },
    )
    assert response.status_code == 200, response.text
    response = client.post(f"/controller/payments/{pending['pay_id']}/authorize", headers=AUTH)
    assert response.status_code == 200, response.text


def test_initial_run_stops_for_full_evidence_and_never_approves(acceptance_desk):
    rt, _, _, api = acceptance_desk
    report = run_acceptance(api, verify=False, response_key=KEYS[2])
    assert report["status"] == "INCOMPLETE"
    assert report["fixtures_started"] == ["normal", "attack", "large"]
    assert report["pending_review"]["packet_url"].startswith(api.base_url + "/packets/PK-")
    assert report["scenarios"]["normal"]["proof"]["mac_verified_independently"]
    assert report["scenarios"]["attack"]["proof"]["status"] == "ACCOUNT"
    assert report["scenarios"]["large"]["proof"]["status"] == "APPROVAL"
    assert [path for method, path in api.calls if method == "POST"] == [
        "/demo/scenarios/normal",
        "/demo/scenarios/attack",
        "/demo/scenarios/large",
    ]
    assert rt.bank._con.execute("SELECT COUNT(*) FROM bank_hardware_receipts").fetchone()[0] == 1


def test_verify_after_human_approval_is_get_only_and_reuse_is_idempotent(acceptance_desk):
    rt, client, _, api = acceptance_desk
    pending = run_acceptance(api, verify=False, response_key=KEYS[2])
    human_approves(client, pending)
    api.calls.clear()
    report = run_acceptance(api, verify=True, response_key=KEYS[2])
    assert report["status"] == "PASS" and report["pending_review"] is None
    assert all(method == "GET" for method, _ in api.calls)
    assert report["scenarios"]["large"]["bank_ref"]
    assert (
        report["scenarios"]["large"]["proof"]["native_minor"]
        == report["scenarios"]["large"]["proof"]["eur_equiv_minor"]
    )
    api.calls.clear()
    again = run_acceptance(api, verify=False, response_key=KEYS[2])
    assert again["status"] == "PASS" and not again["fixtures_started"]
    assert all(method == "GET" for method, _ in api.calls)
    assert rt.bank._con.execute("SELECT COUNT(*) FROM bank_hardware_receipts").fetchone()[0] == 2


def test_explicit_model_transport_cannot_claim_physical_acceptance(hardware_desk):  # noqa: F811
    (
        _,
        client,
        _,
    ) = hardware_desk
    api = ApiAdapter(client)
    with pytest.raises(AcceptanceFailure, match="model transport"):
        run_acceptance(api, verify=False, response_key=KEYS[2])
    assert not [x for x in api.calls if x[0] == "POST"]


@pytest.mark.parametrize(
    "mutation,expected",
    [
        (lambda chip: chip.update(kind="stub", signer_kind="stub"), "Hardware signer"),
        (lambda chip: chip["hardware"].update(bridge_mode="model"), "external-gateway"),
        (
            lambda chip: chip["hardware"].update(capacity={"vendors": 16, "invoices": 64}),
            "capacity",
        ),
        (lambda chip: chip["hardware"].update(clock_divider_hz=100_000_000), "50 MHz"),
        (lambda chip: chip.update(bank_mode="production"), "simulated bank"),
    ],
)
def test_preflight_fails_before_mutating_fixtures(acceptance_desk, mutation, expected):
    _, _, _, api = acceptance_desk

    def alter(path, obj):
        if path == "/chip/status":
            mutation(obj)
        return obj

    api.alter = alter
    with pytest.raises(AcceptanceFailure, match=expected):
        run_acceptance(api, verify=False, response_key=KEYS[2])
    assert not [x for x in api.calls if x[0] == "POST"]


def test_missing_signed_proof_or_corrupt_mac_fails(acceptance_desk):
    _, _, _, api = acceptance_desk
    run_acceptance(api, verify=False, response_key=KEYS[2])

    def corrupt(path, obj):
        if path == "/demo":
            obj["scenarios"][0]["chip_response"]["authorization"]["response_mac_hex"] = "11" * 32
        return obj

    api.alter = corrupt
    with pytest.raises(AcceptanceFailure, match="MAC verification failed"):
        run_acceptance(api, verify=True, response_key=KEYS[2])

    def absent(path, obj):
        if path == "/demo":
            obj["scenarios"][0]["chip_response"]["authorization"] = None
        return obj

    api.alter = absent
    with pytest.raises(AcceptanceFailure, match="proof is absent"):
        run_acceptance(api, verify=True, response_key=KEYS[2])


def test_missing_key_is_incomplete_even_after_human_authorizes(acceptance_desk):
    _, client, _, api = acceptance_desk
    pending = run_acceptance(api, verify=False, response_key=KEYS[2])
    human_approves(client, pending)
    report = run_acceptance(api, verify=True, response_key=None)
    assert report["status"] == "INCOMPLETE"
    assert report["scenarios"]["large"]["proof"]["mac_verified_independently"] is False


def test_read_only_verification_never_starts_missing_fixtures(acceptance_desk):
    _, _, _, api = acceptance_desk
    with pytest.raises(AcceptanceFailure, match="--verify performs no writes"):
        run_acceptance(api, verify=True, response_key=KEYS[2])
    assert not [x for x in api.calls if x[0] == "POST"]


def test_simulated_api_error_does_not_retry_or_approve(acceptance_desk):
    _, _, _, api = acceptance_desk

    def fail(path):
        api.calls.append(("POST", path))
        raise AcceptanceFailure("API request failed")

    api.post = fail
    with pytest.raises(AcceptanceFailure, match="API request failed"):
        run_acceptance(api, verify=False, response_key=KEYS[2])
    assert [x for x in api.calls if x[0] == "POST"] == [("POST", "/demo/scenarios/normal")]


def test_local_api_uses_separate_bounded_timeouts_without_resending():
    client = LocalApi("http://127.0.0.1:8000", TOKEN)
    calls = []

    class TimeoutOpener:
        def open(self, request, timeout):
            calls.append((request.method, request.full_url, timeout))
            raise TimeoutError("simulated slow model prose")

    client._opener = TimeoutOpener()
    with pytest.raises(AcceptanceFailure, match=r"outcome UNKNOWN.*GET /demo.*no automatic resend"):
        client.post("/demo/scenarios/normal")
    assert calls == [("POST", "http://127.0.0.1:8000/demo/scenarios/normal", 180.0)]
    with pytest.raises(AcceptanceFailure, match="no retry was issued"):
        client.get("/demo")
    assert calls[-1] == ("GET", "http://127.0.0.1:8000/demo", 30.0)
    assert len(calls) == 2


def test_report_contains_no_credentials_account_details_or_raw_proofs(acceptance_desk, tmp_path):
    _, _, _, api = acceptance_desk
    report = run_acceptance(api, verify=False, response_key=KEYS[2])
    path = tmp_path / "acceptance.json"
    write_report(path, report)
    raw = path.read_text()
    for secret in (
        TOKEN,
        KEYS[0].hex(),
        KEYS[1].hex(),
        KEYS[2].hex(),
        "response_mac_hex",
        "account_hash",
        "iban",
    ):
        assert secret not in raw
    assert json.loads(raw)["schema"] == REPORT_SCHEMA
    write_report(path, report)
    path.write_text("preserve this unrelated file")
    with pytest.raises(AcceptanceFailure, match="Refusing to overwrite"):
        write_report(path, report)


def test_loopback_only_and_private_token_file(tmp_path, monkeypatch):
    assert validate_base_url("http://127.0.0.1:8000/") == "http://127.0.0.1:8000"
    for bad in (
        "https://example.com",
        "http://localhost:8000",
        "http://127.0.0.1/a",
        "http://secret@127.0.0.1",
    ):
        with pytest.raises(AcceptanceFailure):
            validate_base_url(bad)
    token = tmp_path / "token"
    token.write_text(TOKEN)
    token.chmod(0o600)
    assert read_token(token) == TOKEN
    token.chmod(0o644)
    with pytest.raises(AcceptanceFailure, match="private"):
        read_token(token)
    monkeypatch.setenv("PAYGUARD_CONTROLLER_TOKEN", TOKEN)
    assert read_token(None) == TOKEN


def test_cli_failure_report_sanitizes_api_error_and_does_not_require_token_for_verify(
    tmp_path, monkeypatch, capsys
):
    monkeypatch.delenv("PAYGUARD_CONTROLLER_TOKEN", raising=False)
    monkeypatch.delenv("PAYGUARD_RESPONSE_KEY_FILE", raising=False)

    def fail(self, path):
        raise AcceptanceFailure("Local API is unavailable or timed out; no retry was issued")

    monkeypatch.setattr(LocalApi, "get", fail)
    path = tmp_path / "failed.json"
    assert main(["--verify", "--report", str(path)]) == 1
    assert json.loads(path.read_text())["status"] == "FAIL"
    assert TOKEN not in capsys.readouterr().out
