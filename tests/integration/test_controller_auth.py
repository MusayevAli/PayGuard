"""Controller authorization fails before mutations; tests never call live LLMs/FPGA."""

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from api.main import create_app
from tests.integration.test_dashboard import make_test_app

TOKEN = "controller-test-secret-32-characters-minimum"
AUTH = {"X-Payguard-Controller": TOKEN}


@pytest.fixture
def controller(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("SIGNER", "stub")
    monkeypatch.setenv("PAYGUARD_CONTROLLER_TOKEN", TOKEN)
    app = make_test_app(tmp_path)
    with TestClient(app) as client:
        yield client, app.state.runtime


@pytest.mark.parametrize("headers", [{}, {"X-Payguard-Controller": "wrong-secret"}])
@pytest.mark.parametrize(
    "path",
    [
        "/clock/pause",
        "/clock/resume",
        "/clock/step",
        "/clock/speed",
        "/stub/button",
        "/packets/not-a-packet/decide",
        "/judge/message",
        "/judge/invoice",
        "/demo/scenarios/normal",
        "/demo/scenarios/attack",
        "/demo/scenarios/large",
        "/controller/payments/not-a-payment/authorize",
        "/controller/check",
    ],
)
def test_missing_or_wrong_controller_cannot_mutate(controller, headers, path):
    client, runtime = controller
    before = (runtime.store.latest_seq(), runtime.clock.state(), runtime.bank.balances())
    response = client.post(path, headers=headers, json={})
    assert response.status_code == 403, response.text
    assert response.json()["error"]["code"] == "controller_required"
    assert (runtime.store.latest_seq(), runtime.clock.state(), runtime.bank.balances()) == before
    assert TOKEN not in response.text


def test_authenticated_decision_records_true_channel_and_authentication_identity(controller):
    client, runtime = controller
    result = client.post("/demo/scenarios/large", headers=AUTH).json()
    packet_id = result["packet_id"]
    response = client.post(
        f"/packets/{packet_id}/decide",
        headers=AUTH,
        json={"decision": "approve", "decided_by": "spoofed-mayor", "note": "Reviewed"},
    )
    assert response.status_code == 200, response.text
    decision = response.json()["decision"]
    assert decision["via"] == "trusted_operator_ui"
    assert decision["decided_by"] == "authenticated-controller"
    event = runtime.store.get(decision["event_id"])
    assert event.actor == "authenticated-controller"
    assert event.payload["via"] == "trusted_operator_ui"
    assert runtime.payment_state.payment(result["pay_id"]).status == "awaiting_button"
    assert TOKEN not in client.get("/events?limit=1000").text


def test_hardware_authorization_requires_packet_review_first(controller, monkeypatch):
    client, runtime = controller
    result = client.post("/demo/scenarios/large", headers=AUTH).json()
    assert result["status"] == "awaiting_decision"
    # An API-boundary test double only; no physical-hardware claim is made.
    monkeypatch.setattr(runtime.signer, "kind", "hw")

    def unexpected_approval(pay_id):
        raise AssertionError("Signer approval must not be reached before packet review")

    monkeypatch.setattr(runtime.signer, "approve_pending", unexpected_approval, raising=False)
    before = runtime.store.latest_seq(), runtime.bank.balances()
    response = client.post(f"/controller/payments/{result['pay_id']}/authorize", headers=AUTH)
    assert response.status_code == 409, response.text
    assert response.json()["error"]["code"] == "review_required"
    assert (runtime.store.latest_seq(), runtime.bank.balances()) == before


def test_hardware_mode_refuses_startup_without_operator_secret(tmp_path, monkeypatch):
    monkeypatch.setenv("SIGNER", "hw")
    monkeypatch.delenv("PAYGUARD_CONTROLLER_TOKEN", raising=False)
    with pytest.raises(RuntimeError, match="CONTROLLER_TOKEN"):
        create_app(world_dir=tmp_path / "missing-world", db_path=tmp_path / "desk.db")


def test_unknown_signer_mode_never_falls_back_to_stub(tmp_path, monkeypatch):
    monkeypatch.setenv("SIGNER", "unknown")
    with pytest.raises(RuntimeError, match="never fall back"):
        create_app(world_dir=tmp_path / "missing-world", db_path=tmp_path / "desk.db")


def test_control_password_check_validates_access_without_approval(controller):
    client, runtime = controller
    before = runtime.store.latest_seq(), runtime.bank.balances()
    response = client.post("/controller/check", headers=AUTH)
    assert response.status_code == 200
    assert response.json()["unlocked"] is True
    assert response.json()["payment_authorized"] is False
    assert (runtime.store.latest_seq(), runtime.bank.balances()) == before
    assert TOKEN not in response.text


def test_failed_chip_status_is_actionable_json_and_never_fabricates_connection(controller, monkeypatch):
    client, runtime = controller
    before = runtime.store.latest_seq(), runtime.bank.balances()

    def disconnected():
        raise OSError("USB serial port is unavailable")

    with monkeypatch.context() as patch:
        patch.setattr(runtime.signer, "status", disconnected)
        response = client.get("/chip/status")
        assert response.status_code == 503
        assert response.json()["error"]["code"] == "hardware_status_unavailable"
        assert "USB" in response.json()["error"]["message"]
        assert "no software fallback" in response.json()["error"]["hint"]
    assert client.get("/chip/status").status_code == 200
    assert (runtime.store.latest_seq(), runtime.bank.balances()) == before
