"""Durable request identities and observable background work for the local demo.

Only explicit submissions start work. Restarting never retries an uncertain
payment; interrupted jobs retain their inputs and evidence for inspection.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from typing import Any


def _now() -> str:
    return datetime.now(UTC).isoformat()


class JobConflict(ValueError):
    pass


class LiveJobs:
    def __init__(self, db_path: str) -> None:
        self._lock = threading.RLock()
        self._con = sqlite3.connect(db_path, timeout=10, check_same_thread=False, isolation_level=None)
        self._con.row_factory = sqlite3.Row
        self._con.execute("PRAGMA journal_mode=WAL")
        self._con.execute(
            "CREATE TABLE IF NOT EXISTS demo_live_jobs ("
            "run_id TEXT PRIMARY KEY, kind TEXT NOT NULL, payload_hash TEXT NOT NULL, "
            "payload TEXT NOT NULL, invoice_id TEXT NOT NULL, job_status TEXT NOT NULL, "
            "submitted_at TEXT NOT NULL, updated_at TEXT NOT NULL, error TEXT, "
            "result TEXT, progress TEXT NOT NULL DEFAULT '[]')"
        )
        self._con.execute(
            "UPDATE demo_live_jobs SET job_status='interrupted', updated_at=?, error=? "
            "WHERE job_status IN ('queued','running')",
            (_now(), "The server restarted before this request finished. Inspect its evidence before starting another request."),
        )
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="payguard-live")
        self._closed = False

    def close(self) -> None:
        # Finish an in-flight hardware exchange before the runtime closes its
        # serial port or database. Work not yet started is not automatically run.
        self._executor.shutdown(wait=True, cancel_futures=True)
        with self._lock:
            self._con.execute(
                "UPDATE demo_live_jobs SET job_status='interrupted', updated_at=?, error=? "
                "WHERE job_status='queued'",
                (_now(), "The server stopped before this request started."),
            )
            self._closed = True
            self._con.close()

    def submit(
        self,
        run_id: str,
        kind: str,
        payload: dict[str, Any],
        invoice_id: str,
        work: Callable[[Callable[[dict[str, Any]], None]], dict[str, Any]],
    ) -> dict[str, Any]:
        encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
        digest = hashlib.sha256(encoded.encode()).hexdigest()
        with self._lock:
            if self._closed:
                raise RuntimeError("The demo server is stopping")
            existing = self._get(run_id)
            if existing:
                if existing["kind"] != kind or existing["payload_hash"] != digest:
                    raise JobConflict("This request ID already belongs to different input. Start a new request for edited input.")
                return existing
            stamp = _now()
            self._con.execute(
                "INSERT INTO demo_live_jobs(run_id,kind,payload_hash,payload,invoice_id,job_status,submitted_at,updated_at) "
                "VALUES (?,?,?,?,?,'queued',?,?)",
                (run_id, kind, digest, encoded, invoice_id, stamp, stamp),
            )
            receipt = self._get(run_id)
            assert receipt is not None
            self._executor.submit(self._run, run_id, work)
            return receipt

    def _run(self, run_id: str, work: Callable) -> None:
        self._update(run_id, job_status="running")
        try:
            result = work(lambda item: self.progress(run_id, item))
        except Exception as exc:
            # Persist the failure; neither a worker crash nor a lost HTTP reply
            # may silently retry the operation or claim successful processing.
            detail = getattr(exc, "detail", None) or str(exc)
            self._update(run_id, job_status="failed", error=f"{type(exc).__name__}: {detail}"[:1500])
        else:
            row = self.get(run_id) or {}
            error = result.get("processing_error") or row.get("error")
            if not error:
                for event in reversed(result.get("evidence_events", [])):
                    if event.get("type") == "system_error":
                        payload = event.get("payload", {})
                        error = ": ".join(str(payload[key]) for key in ("component", "code", "message") if payload.get(key))
                        error = error or "Processing failed; inspect the recorded system error."
                        break
            incomplete = result.get("status") in ("incomplete", "received")
            if incomplete and not error:
                error = "Processing did not finish: " + str(result.get("reason") or result.get("held_reason") or "inspect the recorded events")
            self._update(
                run_id,
                job_status="failed" if error else "completed",
                error=str(error)[:1500] if error else None,
                result=json.dumps(result, default=str),
            )

    def progress(self, run_id: str, item: dict[str, Any]) -> None:
        with self._lock:
            row = self._get(run_id)
            if row is None:
                return
            step = dict(item)
            step.setdefault("observed_at", _now())
            steps = [*row["progress"], step][-100:]
            values = {"progress": json.dumps(steps, default=str)}
            if step.get("state") == "failed" or step.get("status") == "failed":
                # Keep a failure even when later work produces more progress or
                # the bounded presentation history rolls past this step.
                values["error"] = str(step.get("error") or step.get("message") or "Agent processing failed")[:1500]
            self._update(run_id, **values)

    def _update(self, run_id: str, **values: Any) -> None:
        allowed = {"job_status", "error", "result", "progress"}
        if not values or not set(values).issubset(allowed):
            raise ValueError("Invalid job update")
        with self._lock:
            sql = ", ".join(f"{column}=?" for column in values)
            self._con.execute(
                f"UPDATE demo_live_jobs SET {sql}, updated_at=? WHERE run_id=?",
                (*values.values(), _now(), run_id),
            )

    def _get(self, run_id: str) -> dict[str, Any] | None:
        row = self._con.execute("SELECT * FROM demo_live_jobs WHERE run_id=?", (run_id,)).fetchone()
        if row is None:
            return None
        result = dict(row)
        for name in ("payload", "progress", "result"):
            if result[name] is not None:
                result[name] = json.loads(result[name])
        return result

    def get(self, run_id: str) -> dict[str, Any] | None:
        with self._lock:
            return self._get(run_id)


_manager_lock = threading.Lock()


def get_manager(runtime: Any) -> LiveJobs:
    with _manager_lock:
        manager = getattr(runtime, "_live_demo_jobs", None)
        if manager is None:
            manager = LiveJobs(runtime.store.path)
            runtime._live_demo_jobs = manager
        return manager


def close_manager(runtime: Any) -> None:
    manager = getattr(runtime, "_live_demo_jobs", None)
    if manager is not None:
        manager.close()
        runtime._live_demo_jobs = None
