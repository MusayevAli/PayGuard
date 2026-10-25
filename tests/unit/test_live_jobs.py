from __future__ import annotations

import sqlite3
import threading
import time
from uuid import uuid4

import pytest

from api.live_jobs import JobConflict, LiveJobs


def wait_finished(manager, run_id):
    until = time.monotonic() + 3
    while time.monotonic() < until:
        row = manager.get(run_id)
        if row["job_status"] not in ("queued", "running"):
            return row
        time.sleep(0.005)
    raise AssertionError("Worker did not finish")


def test_background_job_has_live_progress_and_cannot_be_submitted_twice(tmp_path):
    manager = LiveJobs(str(tmp_path / "jobs.db"))
    started, release = threading.Event(), threading.Event()
    calls = []
    run_id = str(uuid4())

    def work(progress):
        calls.append(run_id)
        progress({"agent": "invoice_agent", "stage": "invoice_received", "status": "running"})
        started.set()
        assert release.wait(3)
        return {"status": "held", "reason": "missing_po"}

    try:
        receipt = manager.submit(run_id, "invoice", {"text": "fresh input"}, "LIVE-1", work)
        assert receipt["job_status"] == "queued"
        assert started.wait(1)
        current = manager.get(run_id)
        assert current["job_status"] == "running"
        assert current["progress"][0]["observed_at"]
        manager.submit(run_id, "invoice", {"text": "fresh input"}, "LIVE-1", work)
        with pytest.raises(JobConflict):
            manager.submit(run_id, "invoice", {"text": "edited input"}, "LIVE-1", work)
        with pytest.raises(JobConflict):
            manager.submit(run_id, "normal", {"text": "fresh input"}, "LIVE-1", work)
        release.set()
        done = wait_finished(manager, run_id)
        assert done["job_status"] == "completed"
        assert done["result"]["status"] == "held"
        assert calls == [run_id]
        manager.submit(run_id, "invoice", {"text": "fresh input"}, "LIVE-1", work)
        assert calls == [run_id]
    finally:
        release.set()
        manager.close()


def test_worker_failure_is_durable_without_automatic_retry(tmp_path):
    db = str(tmp_path / "jobs.db")
    manager = LiveJobs(db)
    run_id = str(uuid4())
    calls = []

    def failed(_progress):
        calls.append(1)
        raise OSError("Device disconnected")

    manager.submit(run_id, "normal", {}, "LIVE-1", failed)
    assert wait_finished(manager, run_id)["error"] == "OSError: Device disconnected"
    manager.close()
    manager = LiveJobs(db)
    try:
        receipt = manager.submit(run_id, "normal", {}, "LIVE-1", failed)
        assert receipt["job_status"] == "failed"
        assert calls == [1]
    finally:
        manager.close()


def test_restart_marks_uncertain_job_interrupted_without_reexecution(tmp_path):
    db = str(tmp_path / "jobs.db")
    manager = LiveJobs(db)
    run_id = str(uuid4())
    manager.submit(run_id, "normal", {}, "LIVE-1", lambda _: {"status": "executed"})
    wait_finished(manager, run_id)
    manager.close()
    with sqlite3.connect(db) as con:
        con.execute("UPDATE demo_live_jobs SET job_status='running', result=NULL WHERE run_id=?", (run_id,))
    manager = LiveJobs(db)
    try:
        row = manager.get(run_id)
        assert row["job_status"] == "interrupted" and row["result"] is None
        assert "server restarted" in row["error"]
        calls = []
        manager.submit(run_id, "normal", {}, "LIVE-1", lambda _: calls.append(1))
        assert calls == []
    finally:
        manager.close()


@pytest.mark.parametrize("failure_source", ["progress", "evidence"])
def test_recorded_failure_cannot_be_hidden_by_posted_invoice_status(tmp_path, failure_source):
    manager = LiveJobs(str(tmp_path / "jobs.db"))
    run_id = str(uuid4())

    def work(progress):
        result = {"status": "posted"}
        if failure_source == "progress":
            progress({"agent": "payment_agent", "state": "failed", "error": "Device disconnected"})
            for _ in range(101):
                progress({"agent": "invoice_agent", "state": "finished"})
        else:
            result["evidence_events"] = [{
                "type": "system_error", "payload": {
                    "component": "payment_agent", "code": "OSError", "message": "Device disconnected",
                },
            }]
        return result

    try:
        manager.submit(run_id, "invoice", {}, "LIVE-1", work)
        done = wait_finished(manager, run_id)
        assert done["job_status"] == "failed"
        assert "Device disconnected" in done["error"]
        assert done["result"]["status"] == "posted"
    finally:
        manager.close()
