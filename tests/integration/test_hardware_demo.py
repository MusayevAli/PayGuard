"""Full HTTP desk flow through HardwareSigner and an explicit PolicyModel transport.

This verifies integration and independent bank proof checks. It is intentionally
not a physical FPGA, UART, Vivado timing, or real-bank acceptance test.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from api.demo import ATTACK_ACCOUNT
from api.main import create_app
from core.events import make_event
from signer.authority import verify_authorization
from signer.hardware import HardwareSigner
from sim.fx import write_fx_rates
from sim.people import roster
from sim.worldgen.suppliers import build_suppliers
from tests.conftest import synthetic_fx
from tests.integration.test_demo_api import NoPaidLLM
from tests.unit.test_hardware_signer import KEYS, START, ExplicitModelTransport

# authority imported above establishes the maintained protocol package path.
from siliconsign.protocol import Frame, Op, Status  # isort: skip

TOKEN = "explicit-local-integration-controller-token-2026"
AUTH = {"X-Payguard-Controller": TOKEN}


@pytest.fixture
def hardware_desk(tmp_path: Path, monkeypatch):
    world = tmp_path / "world"
    world.mkdir()
    (world / "world.jsonl").write_text("")
    (world / "suppliers.json").write_text(
        json.dumps([s.model_dump(mode="json") for s in build_suppliers(42)])
    )
    (world / "people.json").write_text(json.dumps([p.model_dump(mode="json") for p in roster()]))
    (world / "answer_key.json").write_text(json.dumps({"seed": 42, "headline": [], "ambient": []}))
    (world / "invoices_truth.json").write_text("[]")
    write_fx_rates(synthetic_fx(), world / "fx_rates.json")
    transport = ExplicitModelTransport()
    monkeypatch.setenv("SIGNER", "hw")
    monkeypatch.setenv("PAYGUARD_CONTROLLER_TOKEN", TOKEN)
    monkeypatch.setenv("PAYGUARD_HW_PROVISION", "1")

    def explicit_test_adapter(cls, path):
        return cls(
            path,
            transport=transport,
            admin_key=KEYS[0],
            controller_key=KEYS[1],
            response_key=KEYS[2],
            initialize=True,
            now=lambda: START,
        )

    # Only this test replaces the physical transport constructor. Production
    # from_env still requires the configured device, keys, and explicit INIT.
    monkeypatch.setattr(HardwareSigner, "from_env", classmethod(explicit_test_adapter))
    app = create_app(world_dir=world, db_path=tmp_path / "desk.db", llm=NoPaidLLM())
    with TestClient(app) as client:
        yield app.state.runtime, client, transport


def run(client, scenario):
    result = client.post(f"/demo/scenarios/{scenario}", headers=AUTH)
    assert result.status_code == 200, result.text
    return result.json()


def scenario(client, name):
    return next(s for s in client.get("/demo").json()["scenarios"] if s["id"] == name)


def test_real_adapter_routes_normal_attack_and_exact_approval_to_independent_bank(hardware_desk):
    rt, client, transport = hardware_desk
    assert rt.signer.kind == "hw" and transport.is_hardware is False
    # Exercise the physical-transport presentation branch with signed model
    # frames. This is simulated identity metadata, not a physical FPGA test.
    transport.is_hardware = True
    assert len(transport.model.vendors) == sum(v.status == "active" for v in rt.vendors.vendors())
    before = rt.bank.balance("EUR")

    normal = run(client, "normal")
    assert normal["status"] == "executed", normal
    assert normal["chip_response"]["ok"] and normal["chip_response"]["tag_hex"] is None
    proof = normal["chip_response"]["authorization"]
    shown = client.get("/dashboard/summary").json()["latest_authorization"]
    assert shown["indicator"]["state"] == "approved"
    assert shown["indicator"]["op"] == "PAY"
    assert shown["payload"]["pay_id"] == normal["pay_id"]
    verified = verify_authorization(
        proof,
        bytes.fromhex(proof["legacy_frame_hex"]),
        KEYS[2],
        proof["binding"],
        rt.signer.session,
    )
    assert verified.op == Op.PAY and verified.status == Status.OK
    receipt = rt.bank._con.execute("SELECT * FROM bank_hardware_receipts").fetchone()
    assert receipt["pay_id"] == normal["pay_id"]
    assert rt.bank.balance("EUR") == before - normal["amount_minor"]
    assert len(normal["journal_entries"]) == 2
    for entry in normal["journal_entries"]:
        assert sum(x["debit_minor"] for x in entry["lines"]) == sum(
            x["credit_minor"] for x in entry["lines"]
        )

    enrolled = {slot: (v.account, v.version) for slot, v in transport.model.vendors.items()}
    paid_counter = transport.model.auth_counter
    requests_before = len(transport.requests)
    attack = run(client, "attack")
    assert attack["status"] == "held" and attack["held_reason"] == "not_allowlisted", attack
    rejection = Frame.unpack(bytes.fromhex(attack["chip_response"]["raw_hex"]))
    assert rejection.op == Op.COMMIT_INVOICE and rejection.status == Status.ACCOUNT
    shown = client.get("/dashboard/summary").json()["latest_authorization"]
    assert shown["indicator"]["state"] == "blocked"
    assert shown["indicator"]["op"] == "COMMIT_INVOICE"
    assert shown["indicator"]["status"] == "ACCOUNT"
    projected = client.get("/dashboard/payments").json()["payments"]
    assert (
        next(p for p in projected if p["pay_id"] == normal["pay_id"])["chip_indicator"]["state"]
        == "approved"
    )
    assert (
        next(p for p in projected if p["pay_id"] == attack["pay_id"])["chip_event_id"]
        == shown["id"]
    )
    assert transport.model.auth_counter == paid_counter
    assert rt.bank.balance("EUR") == before - normal["amount_minor"]
    assert {slot: (v.account, v.version) for slot, v in transport.model.vendors.items()} == enrolled
    assert not any(
        f.op in (Op.ADD_VENDOR, Op.CHANGE_BANK, Op.APPROVE_EXCEPTION, Op.PAY)
        for f, _ in transport.requests[requests_before:]
    )
    assert rt.vendors.get_account(ATTACK_ACCOUNT).status == "retired"
    assert attack["packet_id"] and not [
        e for e in attack["evidence_events"] if e["type"] == "payment_executed"
    ]

    large = run(client, "large")
    assert large["status"] == "awaiting_decision" and large["packet_id"], large
    assert Frame.unpack(bytes.fromhex(large["chip_response"]["raw_hex"])).status == Status.APPROVAL
    shown = client.get("/dashboard/summary").json()["latest_authorization"]
    assert shown["indicator"]["state"] == "blocked"
    assert shown["indicator"]["status"] == "APPROVAL"
    assert rt.bank.balance("EUR") == before - normal["amount_minor"]
    authorize = f"/controller/payments/{large['pay_id']}/authorize"
    assert client.post(authorize, headers=AUTH).status_code == 409
    packet_response = client.get(f"/packets/{large['packet_id']}")
    assert packet_response.status_code == 200
    packet = packet_response.json()
    assert "physical approval button" in packet["context"]
    assert len(packet["evidence_rows"]) >= 2
    approved = client.post(
        f"/packets/{large['packet_id']}/decide",
        headers=AUTH,
        json={
            "decision": "approve",
            "note": "Reviewed exact batch evidence",
            "decided_by": "test-human",
        },
    )
    assert approved.status_code == 200, approved.text
    assert scenario(client, "large")["status"] == "awaiting_button"
    assert transport.model.auth_counter == paid_counter
    assert client.post(authorize).status_code == 403
    authorized = client.post(authorize, headers=AUTH)
    assert authorized.status_code == 200, authorized.text
    assert authorized.json()["payments_resent"] == 1
    assert authorized.json()["approval_mode"] == "authenticated-software"
    executed = scenario(client, "large")
    assert executed["status"] == "executed", executed
    shown = client.get("/dashboard/summary").json()["latest_authorization"]
    assert shown["indicator"]["state"] == "approved"
    assert shown["indicator"]["op"] == "PAY"
    assert shown["indicator"]["pay_id"] == large["pay_id"]
    assert rt.bank.balance("EUR") == before - normal["amount_minor"] - large["amount_minor"]
    assert transport.model.auth_counter == paid_counter + 1
    assert rt.bank._con.execute("SELECT COUNT(*) FROM bank_hardware_receipts").fetchone()[0] == 2
    assert rt.ledger.ap_control_reconciles().ok
    assert not rt.signer.approval_ready(large["pay_id"])
    assert client.post(authorize, headers=AUTH).status_code == 409
    assert run(client, "large")["status"] == "executed"
    assert (
        rt.bank._con.execute("SELECT COUNT(*) FROM bank_settlements WHERE dc='D'").fetchone()[0]
        == 2
    )


def test_latest_fpga_result_survives_large_activity_window(hardware_desk):
    rt, client, transport = hardware_desk
    # The transport still executes PolicyModel; only its identity metadata is
    # emulated to exercise the production dashboard's proof-verification branch.
    transport.is_hardware = True
    normal = run(client, "normal")
    original = client.get("/dashboard/summary").json()["latest_authorization"]
    for index in range(510):
        rt.store.append(
            make_event(
                "controller_decision",
                {
                    "packet_id": f"HISTORY-{index}",
                    "decision": "reviewed",
                    "note": None,
                    "decided_by": "test",
                    "via": "api",
                },
                event_time=rt.clock.sim_now,
                source="controller",
            )
        )
    assert client.get("/dashboard/summary").json()["latest_authorization"] == original
    row = next(
        p
        for p in client.get("/dashboard/payments").json()["payments"]
        if p["pay_id"] == normal["pay_id"]
    )
    assert row["chip_indicator"]["state"] == "approved"
    assert row["chip_event_id"] == original["id"]
    assert row["chip_seq"] < rt.store.latest_seq() - 500


def test_hardware_http_mode_rejects_unauthenticated_mutations_and_stub_controls(hardware_desk):
    rt, client, transport = hardware_desk
    before = rt.store.latest_seq()
    assert client.post("/demo/scenarios/normal").status_code == 403
    assert (
        client.post(
            "/demo/scenarios/normal", headers={"X-Payguard-Controller": "incorrect"}
        ).status_code
        == 403
    )
    assert client.post("/stub/button", headers=AUTH).status_code == 404
    assert rt.store.latest_seq() == before
    assert transport.model.auth_counter == 0


def test_corrupted_real_adapter_receipt_is_independently_refused_by_bank(
    hardware_desk, monkeypatch
):
    from dataclasses import replace

    rt, client, transport = hardware_desk
    before = rt.bank.balance("EUR")
    original = rt.signer.sign

    def corrupted(frame):
        result = original(frame)
        if result.ok and result.authorization:
            auth = {**result.authorization, "response_mac_hex": "00" * 32}
            return replace(result, authorization=auth)
        return result

    monkeypatch.setattr(rt.signer, "sign", corrupted)
    result = run(client, "normal")
    assert result["status"] == "held" and result["held_reason"] == "bank:bad_hardware_authorization"
    # The model committed once, but the independent bank never trusted the
    # adapter's ok flag or debited funds based on the corrupt proof.
    assert transport.model.auth_counter == 1
    assert rt.bank.balance("EUR") == before
    assert rt.bank._con.execute("SELECT COUNT(*) FROM bank_hardware_receipts").fetchone()[0] == 0
    assert result["packet_id"]
    assert not [e for e in result["evidence_events"] if e["type"] == "payment_executed"]
