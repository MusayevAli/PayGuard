"""Dashboard contract tests use real stores and signer simulation, never paid APIs."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from agents.llm import template_explanation
from api.main import create_app
from sim.fx import write_fx_rates
from sim.people import roster
from sim.worldgen.suppliers import build_suppliers
from tests.conftest import synthetic_fx


class NoPaidLLM:
    def explain(self, decision):
        return template_explanation(decision)

    def extract_invoice(self, *args, **kwargs):
        raise AssertionError("Dashboard tests must never call paid extraction")

    def draft_message(self, *args, **kwargs):
        return "Test message. Review the evidence."


def make_test_app(directory: Path):
    world = directory / "world"
    world.mkdir(parents=True, exist_ok=True)
    (world / "suppliers.json").write_text(
        json.dumps([s.model_dump(mode="json") for s in build_suppliers(42)])
    )
    (world / "people.json").write_text(json.dumps([p.model_dump(mode="json") for p in roster()]))
    (world / "world.jsonl").write_text("")
    (world / "answer_key.json").write_text(json.dumps({"seed": 42, "headline": [], "ambient": []}))
    (world / "invoices_truth.json").write_text("[]")
    write_fx_rates(synthetic_fx(), world / "fx_rates.json")
    return create_app(world_dir=world, db_path=directory / "desk.db", llm=NoPaidLLM())


@pytest.fixture
def dashboard(tmp_path, monkeypatch):
    monkeypatch.setenv("SIGNER", "stub")
    monkeypatch.setenv("PAYGUARD_CONTROLLER_TOKEN", "test-controller")
    app = make_test_app(tmp_path)
    with TestClient(app) as client:
        yield client


AUTH = {"X-Payguard-Controller": "test-controller"}


def test_dashboard_assets_and_read_only_routes(dashboard):
    before = dashboard.get("/state").json()["latest_seq"]
    for path in ("/", "/ui/assets/app.js", "/ui/assets/style.css", "/ui/assets/mark.svg"):
        response = dashboard.get(path)
        assert response.status_code == 200, response.text
    for path in ("summary", "payments", "vendors", "invoices", "ledger"):
        response = dashboard.get(f"/dashboard/{path}")
        assert response.status_code == 200, response.text
    assert dashboard.get("/state").json()["latest_seq"] == before
    assert dashboard.get("/dashboard/summary").json()["bank_is_simulated"] is True
    assert dashboard.get("/dashboard/vendors").json()["count"] > 0
    assert dashboard.get("/dashboard/events/does-not-exist").status_code == 404
    assert dashboard.get("/dashboard/ledger/does-not-exist").status_code == 404
    assert dashboard.get("/dashboard/ledger?limit=1001").status_code == 400
    assert dashboard.get("/ui/assets/../sim/world/answer_key.json").status_code == 404


def test_payment_evidence_projection_tracks_actual_authorizations(dashboard):
    result = dashboard.post("/demo/scenarios/normal", headers=AUTH).json()
    rows = dashboard.get("/dashboard/payments").json()["payments"]
    row = next(row for row in rows if row["pay_id"] == result["pay_id"])
    assert row["status"] == "settled"
    assert row["workflow_status"] == "executed"
    assert row["chip"]["ok"] is True
    assert len(row["events"]) >= 3
    entry = dashboard.get(f"/dashboard/ledger/{result['journal_entry_ids'][0]}").json()
    assert entry["evidence"]
    for event_id in row["events"]:
        assert dashboard.get(f"/dashboard/events/{event_id}").status_code == 200
    first = dashboard.get("/dashboard/summary").json()
    second = dashboard.get("/dashboard/summary").json()
    assert first == second  # Polling must not count the same event twice.
    assert first["authorizations"]["approved"] >= 1


def test_software_approval_is_separate_from_packet_review(dashboard):
    result = dashboard.post("/demo/scenarios/large", headers=AUTH).json()
    pay_id, packet_id = result["pay_id"], result["packet_id"]
    assert result["status"] == "awaiting_decision"
    assert (
        dashboard.post(
            f"/packets/{packet_id}/decide", json={"decision": "approve", "decided_by": "test"}
        ).status_code
        == 403
    )
    response = dashboard.post(
        f"/packets/{packet_id}/decide",
        json={"decision": "approve", "decided_by": "test"},
        headers=AUTH,
    )
    assert response.status_code == 200, response.text
    rows = dashboard.get("/dashboard/payments").json()["payments"]
    row = next(row for row in rows if row["pay_id"] == pay_id)
    assert row["packet_id"] == packet_id
    assert row["workflow_status"] == "awaiting_button"
    assert row["status"] == "blocked"
    assert dashboard.post("/stub/button", headers=AUTH).status_code == 200
    rows = dashboard.get("/dashboard/payments").json()["payments"]
    row = next(row for row in rows if row["pay_id"] == pay_id)
    assert row["status"] == "settled"
    assert row["workflow_status"] == "executed"
