"""Observable async jobs through the signer adapter, using signed model frames."""

from __future__ import annotations

import threading
import time
from uuid import uuid4

import pytest

from agents.parsed import ExtractionFailure
from api.live_jobs import close_manager
from tests.integration import test_hardware_demo as hardware
from tests.integration.test_judge_invoice_api import matching_input

hardware_desk = hardware.hardware_desk
AUTH = hardware.AUTH


@pytest.fixture
def live_desk(hardware_desk):
    rt, client, transport = hardware_desk
    yield rt, client, transport
    close_manager(rt)


def finish(client, run_id):
    until = time.monotonic() + 5
    while time.monotonic() < until:
        response = client.get(f"/demo/jobs/{run_id}")
        assert response.status_code == 200, response.text
        result = response.json()
        if result["job_status"] not in ("queued", "running"):
            return result
        time.sleep(0.01)
    raise AssertionError("Job did not finish")


def test_scenario_submission_returns_while_agent_runs_and_reads_remain_responsive(live_desk, monkeypatch):
    rt, client, transport = live_desk
    started, release = threading.Event(), threading.Event()
    explain = rt.llm.explain

    def delayed(decision):
        started.set()
        assert release.wait(3)
        return explain(decision)

    monkeypatch.setattr(rt.llm, "explain", delayed)
    run_id = str(uuid4())
    try:
        start = time.monotonic()
        response = client.post("/demo/jobs/normal", headers=AUTH, json={"run_id": run_id})
        assert response.status_code == 202, response.text
        assert time.monotonic() - start < 0.75
        assert started.wait(1)
        start = time.monotonic()
        pending = client.get(f"/demo/jobs/{run_id}").json()
        assert pending["job_status"] == "running"
        assert any(step["agent"] == "invoice_agent" for step in pending["progress"])
        assert client.get("/demo").status_code == 200
        assert time.monotonic() - start < 0.75
        assert pending["chip_event"] is None
        release.set()
        done = finish(client, run_id)
        assert done["job_status"] == "completed", done
        assert done["result"]["status"] == "executed", done
        assert done["chip_indicator"]["state"] == "software"
        requests = len(transport.requests)
        assert client.post("/demo/jobs/normal", headers=AUTH, json={"run_id": run_id}).status_code == 202
        assert len(transport.requests) == requests
        wrong = client.post("/demo/jobs/attack", headers=AUTH, json={"run_id": run_id})
        assert wrong.status_code == 409
    finally:
        release.set()


def test_structured_invoice_job_reaches_live_projection_with_its_actual_agent_explanations(live_desk):
    rt, client, transport = live_desk
    transport.is_hardware = True  # Verify signed-model evidence in hardware presentation branch.
    form = matching_input(rt)
    run_id = str(uuid4())
    assert client.post("/demo/jobs/invoice", json={"run_id": run_id, "invoice": form}).status_code == 403
    response = client.post("/demo/jobs/invoice", headers=AUTH, json={"run_id": run_id, "invoice": form})
    assert response.status_code == 202, response.text
    done = finish(client, run_id)
    assert done["job_status"] == "completed", done
    assert done["invoice_id"] == "LIVE-" + run_id.replace("-", "").upper()
    assert done["chip_indicator"]["state"] == "approved"
    assert done["chip_indicator"]["proof_verified"]
    assert done["pay_id"] == done["chip_event"]["payload"]["pay_id"]
    actions = [row for row in done["evidence_events"] if row["type"] == "agent_action"]
    assert actions and all(row["payload"]["explanation"] for row in actions)
    assert any(row["payload"]["agent"] == "invoice_agent" for row in actions)
    assert all(row["id"] == rt.store.get(row["id"]).id for row in done["evidence_events"])
    requests = len(transport.requests)
    changed = {**form, "invoice_number": "EDITED"}
    assert client.post("/demo/jobs/invoice", headers=AUTH, json={"run_id": run_id, "invoice": changed}).status_code == 409
    assert len(transport.requests) == requests


def test_attack_job_shows_verified_new_fpga_rejection(live_desk):
    _rt, client, transport = live_desk
    transport.is_hardware = True  # Signed policy model, not a physical FPGA.
    run_id = str(uuid4())
    result = client.post("/demo/jobs/attack", headers=AUTH, json={"run_id": run_id})
    assert result.status_code == 202
    done = finish(client, run_id)
    assert done["job_status"] == "completed", done
    assert done["result"]["held_reason"] == "not_allowlisted", done
    assert done["chip_indicator"]["state"] == "blocked"
    assert done["chip_indicator"]["status"] == "ACCOUNT"
    assert done["chip_indicator"]["proof_verified"]


def test_raw_incoming_text_calls_extractor_and_does_not_invent_fpga_output(live_desk, monkeypatch):
    rt, client, transport = live_desk
    calls = []

    def extract(document, **kwargs):
        calls.append((document.format, "\n".join(part.text for part in document.parts), kwargs))
        return ExtractionFailure(reason="Required invoice totals are missing")

    monkeypatch.setattr(rt.llm, "extract_invoice", extract)
    before = len(transport.requests)
    text = "Incoming message: please pay my invoice. I have not supplied any totals."
    run_id = str(uuid4())
    response = client.post(
        "/demo/jobs/invoice", headers=AUTH,
        json={"run_id": run_id, "invoice": {"document_text": text}},
    )
    assert response.status_code == 202, response.text
    done = finish(client, run_id)
    assert done["source_text"] == text
    assert len(calls) == 2 and all(c[0] == "eml" and text in c[1] for c in calls)
    assert done["result"]["invoice_status"] == "held", done
    assert done["result"]["reason"] == "unreadable_invoice"
    assert done["chip_event"] is None and done["chip_indicator"] is None
    assert len(transport.requests) == before


@pytest.mark.parametrize("kind", ["invoice", "normal"])
def test_payment_task_error_is_a_failed_job_with_own_evidence(live_desk, monkeypatch, kind):
    rt, client, _transport = live_desk
    calls = []

    def fail_payment(*args, **kwargs):
        calls.append(1)
        raise OSError("FPGA cable disconnected during authorization")

    monkeypatch.setattr(rt.payment_agent, "_pay", fail_payment)
    run_id = str(uuid4())
    payload = {"run_id": run_id}
    if kind == "invoice":
        payload["invoice"] = matching_input(rt)
    response = client.post(f"/demo/jobs/{kind}", headers=AUTH, json=payload)
    assert response.status_code == 202
    done = finish(client, run_id)
    assert done["job_status"] == "failed"
    assert "FPGA cable disconnected" in done["error"]
    assert done["result"]["status"] == "posted"
    assert done["chip_event"] is None and done["chip_indicator"] is None
    failures = [event for event in done["evidence_events"] if event["type"] == "system_error"]
    assert len(failures) == 1
    assert done["invoice_id"] in failures[0]["payload"]["refs"]
    assert any(step["status"] == "failed" and step["agent"] == "payment_agent" for step in done["progress"])
    repeated = client.post(f"/demo/jobs/{kind}", headers=AUTH, json=payload)
    assert repeated.status_code == 202 and repeated.json()["job_status"] == "failed"
    assert calls == [1]
