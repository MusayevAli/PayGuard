"""Fresh demo requests retain old evidence and never retry an uncertain payment."""

from concurrent.futures import ThreadPoolExecutor
from uuid import uuid4

from fastapi import HTTPException

import api.demo as demo
from tests.integration import test_demo_api as base_demo

AUTH = base_demo.AUTH
desk = base_demo.desk


def run_live(client, scenario="normal", run_id=None):
    run_id = run_id or str(uuid4())
    response = client.post(f"/demo/runs/{scenario}", headers=AUTH, json={"run_id": run_id})
    assert response.status_code == 200, response.text
    assert response.json()["run_id"] == run_id
    return response.json()


def test_live_runs_require_controller_and_valid_unique_identifier(desk):
    rt, client = desk
    before = rt.store.latest_seq()
    assert client.post("/demo/runs/normal", json={"run_id": str(uuid4())}).status_code == 403
    assert client.post("/demo/runs/normal", headers=AUTH, json={}).status_code == 422
    assert client.post("/demo/runs/normal", headers=AUTH, json={"run_id": "bad"}).status_code == 422
    assert client.get(f"/demo/runs/{uuid4()}").status_code == 404
    assert rt.store.latest_seq() == before


def test_two_live_requests_are_new_payments_with_exact_historical_results(desk):
    rt, client = desk
    before = rt.bank.balance("EUR")
    now = rt.clock.sim_now
    first = run_live(client)
    second = run_live(client)
    assert first["status"] == second["status"] == "executed"
    assert first["pay_id"] != second["pay_id"]
    assert first["invoice_id"] != second["invoice_id"]
    assert first["run_started_at"] and second["run_started_at"]
    assert rt.clock.sim_now == now
    assert rt.bank.balance("EUR") == before - first["amount_minor"] - second["amount_minor"]
    assert client.get(f"/demo/runs/{first['run_id']}").json() == first
    assert client.get(f"/demo/runs/{second['run_id']}").json() == second
    state = client.get("/demo").json()["scenarios"][0]
    assert state["started"] is False  # original once-only scenario remains untouched
    assert state["latest_run_id"] == second["run_id"]
    assert state["latest_run"] == second
    pos = [e.payload["po"]["id"] for e in rt.store.query(type="po_created")]
    receipts = [e.payload["receipt"]["id"] for e in rt.store.query(type="goods_received")]
    assert pos == ["PO-90003", "PO-90004"]
    assert receipts == ["GR-9003", "GR-9004"]


def test_retry_and_cross_scenario_reuse_never_send_another_request(desk, monkeypatch):
    rt, client = desk
    first = run_live(client)
    seq, cash = rt.store.latest_seq(), rt.bank.balance("EUR")

    def must_not_send():
        raise AssertionError("A repeat must not touch the signer")

    monkeypatch.setattr(rt.signer, "status", must_not_send)
    assert run_live(client, run_id=first["run_id"]) == first
    wrong = client.post("/demo/runs/attack", headers=AUTH, json={"run_id": first["run_id"]})
    assert wrong.status_code == 409
    assert rt.store.latest_seq() == seq and rt.bank.balance("EUR") == cash


def test_concurrent_same_run_is_one_payment(desk):
    rt, client = desk
    run_id = str(uuid4())
    before = rt.bank.balance("EUR")
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: run_live(client, run_id=run_id), range(2)))
    assert results[0] == results[1]
    assert rt.bank.balance("EUR") == before - results[0]["amount_minor"]
    assert len([p for p in rt.ledger.payables() if p.inv_id == results[0]["invoice_id"]]) == 1


def test_live_run_does_not_replace_legacy_result_or_reserved_documents(desk):
    _rt, client = desk
    fresh = run_live(client)
    legacy = client.post("/demo/scenarios/normal", headers=AUTH).json()
    assert legacy["status"] == "executed"
    assert legacy["run_id"] is None
    assert fresh["invoice_id"] != legacy["invoice_id"]
    assert client.post("/demo/scenarios/normal", headers=AUTH).json() == legacy
    state = client.get("/demo").json()["scenarios"][0]
    assert state["pay_id"] == legacy["pay_id"]
    assert state["latest_run"]["pay_id"] == fresh["pay_id"]


def test_disconnected_hardware_creates_no_run_or_invoice(desk, monkeypatch):
    rt, client = desk
    run_id = str(uuid4())
    seq = rt.store.latest_seq()

    def disconnected():
        raise OSError("disconnected")

    monkeypatch.setattr(rt.signer, "status", disconnected)
    response = client.post("/demo/runs/normal", headers=AUTH, json={"run_id": run_id})
    assert response.status_code == 503
    assert client.get(f"/demo/runs/{run_id}").status_code == 404
    assert rt.store.latest_seq() == seq


def test_partially_started_run_is_retained_and_never_reexecuted(desk, monkeypatch):
    rt, client = desk
    run_id = str(uuid4())

    def interrupted(*args, **kwargs):
        raise HTTPException(409, "Fixture interrupted; inspect retained evidence")

    monkeypatch.setattr(demo, "_invoice_fixture", interrupted)
    response = client.post("/demo/runs/normal", headers=AUTH, json={"run_id": run_id})
    assert response.status_code == 409
    recorded = client.get(f"/demo/runs/{run_id}").json()
    assert recorded["status"] == "incomplete"
    assert recorded["chip_response"] is None
    seq = rt.store.latest_seq()
    assert run_live(client, run_id=run_id) == recorded
    assert rt.store.latest_seq() == seq
