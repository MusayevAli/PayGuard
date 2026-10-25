"""Fresh requests through HardwareSigner using signed model frames, not a physical board."""

from uuid import uuid4

from tests.integration.test_hardware_demo import AUTH
from tests.integration.test_hardware_demo import hardware_desk as _hardware_desk

hardware_desk = _hardware_desk


def run_live(client, scenario, run_id=None):
    run_id = run_id or str(uuid4())
    response = client.post(
        f"/demo/runs/{scenario}", headers=AUTH, json={"run_id": run_id}
    )
    assert response.status_code == 200, response.text
    result = response.json()
    assert result["run_id"] == run_id
    return result


def test_fresh_normal_and_attack_runs_use_new_hardware_requests(hardware_desk):
    rt, client, transport = hardware_desk
    # Exercise indicator verification with signed model replies. This metadata
    # setting does not turn this software integration test into a physical test.
    transport.is_hardware = True
    enrolled = {slot: (v.account, v.version) for slot, v in transport.model.vendors.items()}
    counter = transport.model.auth_counter
    balance = rt.bank.balance("EUR")
    normal = [run_live(client, "normal") for _ in range(2)]
    assert all(r["status"] == "executed" for r in normal), normal
    assert len({r["invoice_id"] for r in normal}) == 2
    assert len({r["pay_id"] for r in normal}) == 2
    assert transport.model.auth_counter == counter + 2
    assert rt.bank.balance("EUR") == balance - sum(r["amount_minor"] for r in normal)
    requests = len(transport.requests)
    repeated = run_live(client, "normal", normal[0]["run_id"])
    assert repeated["pay_id"] == normal[0]["pay_id"]
    assert len(transport.requests) == requests

    balance = rt.bank.balance("EUR")
    attacks = [run_live(client, "attack") for _ in range(2)]
    assert all(r["held_reason"] == "not_allowlisted" for r in attacks), attacks
    assert len({r["pay_id"] for r in attacks}) == 2
    assert transport.model.auth_counter == counter + 2
    assert rt.bank.balance("EUR") == balance
    assert {slot: (v.account, v.version) for slot, v in transport.model.vendors.items()} == enrolled
    rows = client.get("/dashboard/payments").json()["payments"]
    indicators = {r["pay_id"]: r["chip_indicator"] for r in rows}
    assert all(indicators[r["pay_id"]]["state"] == "approved" for r in normal)
    assert all(indicators[r["pay_id"]]["state"] == "blocked" for r in attacks)
    chip_ids = {
        e["id"] for r in attacks for e in r["evidence_events"] if e["type"] == "chip_response"
    }
    assert len(chip_ids) == 2
    for result in normal + attacks:
        saved = client.get(f"/demo/runs/{result['run_id']}")
        assert saved.status_code == 200
        assert saved.json()["pay_id"] == result["pay_id"]


def test_live_large_requires_review_then_exact_hardware_approval(hardware_desk):
    rt, client, transport = hardware_desk
    transport.is_hardware = True  # Signed model response presentation test only.
    balance = rt.bank.balance("EUR")
    counter = transport.model.auth_counter
    large = run_live(client, "large")
    assert large["status"] == "awaiting_decision", large
    indicator = client.get("/dashboard/summary").json()["latest_authorization"]["indicator"]
    assert indicator["state"] == "blocked" and indicator["status"] == "APPROVAL"
    authorize = f"/controller/payments/{large['pay_id']}/authorize"
    assert client.post(authorize, headers=AUTH).status_code == 409
    assert rt.bank.balance("EUR") == balance
    decision = client.post(
        f"/packets/{large['packet_id']}/decide", headers=AUTH,
        json={"decision": "approve", "note": "Reviewed this live test", "decided_by": "test-human"},
    )
    assert decision.status_code == 200, decision.text
    assert transport.model.auth_counter == counter
    accepted = client.post(authorize, headers=AUTH)
    assert accepted.status_code == 200, accepted.text
    result = client.get(f"/demo/runs/{large['run_id']}").json()
    assert result["status"] == "executed", result
    indicator = client.get("/dashboard/summary").json()["latest_authorization"]["indicator"]
    assert indicator["state"] == "approved" and indicator["pay_id"] == large["pay_id"]
    assert transport.model.auth_counter == counter + 1
    assert rt.bank.balance("EUR") == balance - large["amount_minor"]
    assert rt.ledger.ap_control_reconciles().ok
    requests = len(transport.requests)
    assert run_live(client, "large", large["run_id"])["pay_id"] == large["pay_id"]
    assert len(transport.requests) == requests
