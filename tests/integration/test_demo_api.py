"""Live demo must prove outcomes through the real desk, without extraction or paid APIs."""

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest
from fastapi import FastAPI, Header, HTTPException
from fastapi.testclient import TestClient

from agents.llm import template_explanation
from agents.runtime import build_runtime, start_runtime
from api.demo import ATTACK_ACCOUNT, create_demo_router
from core.events import EventStore
from sim.clock import SimClock
from sim.fx import write_fx_rates
from sim.people import roster
from sim.worldgen.suppliers import build_suppliers
from tests.conftest import synthetic_fx


class NoPaidLLM:
    def explain(self, decision):
        return template_explanation(decision)

    def extract_invoice(self, *args, **kwargs):
        raise AssertionError("The deterministic demo must not call invoice extraction")

    def draft_message(self, *args, **kwargs):
        return "Demo message: see the evidence and decision in the audit log."


@pytest.fixture
def desk(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("SIGNER", "stub")
    # Master data plus an explicitly synthetic test FX table are enough for these
    # EUR fixtures. Tests must not need the public ECB network or a paid LLM.
    world_dir = tmp_path / "world"
    world_dir.mkdir()
    (world_dir / "suppliers.json").write_text(
        json.dumps([s.model_dump(mode="json") for s in build_suppliers(42)])
    )
    (world_dir / "people.json").write_text(
        json.dumps([p.model_dump(mode="json") for p in roster()])
    )
    write_fx_rates(synthetic_fx(), world_dir / "fx_rates.json")
    db = tmp_path / "demo.db"
    store = EventStore(db)
    clock = SimClock(store, [], start=datetime(2026, 10, 1, tzinfo=UTC))
    rt = build_runtime(
        world_dir=world_dir,
        db_path=db,
        store=store,
        clock=clock,
        out_dir=tmp_path,
        world_events=[],
        llm=NoPaidLLM(),
        score=False,
    )
    start_runtime(rt)

    def authorize(x_payguard_controller: str | None = Header(default=None)):
        if x_payguard_controller != "test-controller":
            raise HTTPException(403, "controller required")

    app = FastAPI()
    app.include_router(create_demo_router(rt, require_controller=authorize))
    with TestClient(app) as client:
        yield rt, client
    rt.close()


AUTH = {"X-Payguard-Controller": "test-controller"}


def run(client, scenario):
    response = client.post(f"/demo/scenarios/{scenario}", headers=AUTH)
    assert response.status_code == 200, response.text
    return response.json()


def test_demo_needs_controller_and_is_read_only_until_started(desk):
    rt, client = desk
    before = rt.store.latest_seq()
    state = client.get("/demo").json()
    assert state["bank_mode"] == "simulated"
    assert all(s["status"] == "not_started" for s in state["scenarios"])
    assert client.post("/demo/scenarios/normal").status_code == 403
    assert rt.store.latest_seq() == before


def test_normal_runs_matching_signing_bank_and_balanced_entries_once(desk):
    rt, client = desk
    now = rt.clock.sim_now
    before = rt.bank.balance("EUR")
    result = run(client, "normal")
    assert result["status"] == "executed", result
    assert result["chip_response"]["ok"] is True
    assert result["fixture_mode"] == "structured_invoice"
    assert rt.bank.balance("EUR") == before - result["amount_minor"]
    assert len(result["journal_entries"]) == 2
    evidence_types = {e["type"] for e in result["evidence_events"]}
    assert {
        "po_created",
        "goods_received",
        "judge_injection",
        "chip_response",
        "payment_executed",
    } <= evidence_types
    for entry in result["journal_entries"]:
        assert sum(ln["debit_minor"] for ln in entry["lines"]) == sum(
            ln["credit_minor"] for ln in entry["lines"]
        )
    seq = rt.store.latest_seq()
    assert run(client, "normal") == result
    assert rt.store.latest_seq() == seq
    assert rt.clock.sim_now == now
    assert len(rt.ledger.payables()) == 4  # three preexisting opening payables remain


def test_attack_uses_real_signer_rejection_and_never_enrolls_account(desk):
    rt, client = desk
    cash = rt.bank.balance("EUR")
    slots = len(rt.provisioner.slots())
    original_accounts = {a.id: a.model_dump() for a in rt.vendors.accounts()}
    result = run(client, "attack")
    assert result["fixture_mode"] == "compromised_host"
    assert result["status"] == "held"
    assert result["chip_response"]["ok"] is False
    assert result["chip_response"]["chip_reason"] == "not_allowlisted"
    assert result["held_reason"] == "not_allowlisted"
    assert rt.bank.balance("EUR") == cash
    assert len(rt.provisioner.slots()) == slots
    account = rt.vendors.get_account(ATTACK_ACCOUNT)
    assert account is not None and not account.on_chip and account.approved_by is None
    assert account.status == "retired"
    assert {
        a.id: a.model_dump() for a in rt.vendors.accounts() if a.id != ATTACK_ACCOUNT
    } == original_accounts
    assert not [e for e in result["evidence_events"] if e["type"] == "payment_executed"]


def test_large_is_held_until_review_and_separate_signer_authorization(desk):
    rt, client = desk
    cash = rt.bank.balance("EUR")
    result = run(client, "large")
    assert result["amount_minor"] > 5_000_000
    assert result["status"] == "awaiting_decision", result
    assert result["packet_id"]
    assert rt.bank.balance("EUR") == cash
    rt.packets.decide_packet(
        result["packet_id"],
        "approve",
        "Reviewed demo evidence",
        "test-controller",
        via="phone",
        decided_at=rt.clock.sim_now,
    )
    rt.runner.drain()
    state = client.get("/demo").json()["scenarios"][2]
    assert state["status"] == "awaiting_button"
    assert rt.bank.balance("EUR") == cash
    # Test fixture only: the API itself must never issue a stub button press.
    rt.signer.press_button()
    rt.runner.run_task(rt.payment_agent, rt.payment_agent.retry_waiting)
    state = client.get("/demo").json()["scenarios"][2]
    assert state["status"] == "executed"
    assert state["chip_response"]["ok"] is True
    assert rt.bank.balance("EUR") == cash - result["amount_minor"]


def test_device_failure_never_falls_back_to_stub_or_inserts_fixture(desk, monkeypatch):
    rt, client = desk
    seq = rt.store.latest_seq()

    def disconnected():
        raise OSError("device disconnected")

    monkeypatch.setattr(rt.signer, "status", disconnected)
    response = client.post("/demo/scenarios/normal", headers=AUTH)
    assert response.status_code == 503
    assert rt.store.latest_seq() == seq
    assert not any(p.inv_id.startswith("DEMO-") for p in rt.ledger.payables())
