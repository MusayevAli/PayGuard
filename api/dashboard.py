"""Read-only desk projections and a dependency-free browser dashboard.

The dashboard never signs, provisions or decides. Those operations use the
existing controller-authenticated API. No answer-key or local filesystem is
served by these routes.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from pathlib import Path
from threading import RLock
from typing import Any

from fastapi import APIRouter, FastAPI, HTTPException, Query
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from api.authorization import authorization_indicator


def install_dashboard(app: FastAPI, runtime: Any, world_dir: Path | None = None) -> None:
    """Mount after constructing the desk runtime; ``world_dir`` is intentionally unused."""
    assets = Path(__file__).resolve().parent.parent / "ui"
    app.mount("/ui/assets", StaticFiles(directory=assets), name="dashboard-assets")

    @app.get("/", include_in_schema=False)
    def home() -> FileResponse:
        return FileResponse(assets / "index.html", headers={"Cache-Control": "no-cache"})

    router = APIRouter(prefix="/dashboard", tags=["dashboard"])
    lock = RLock()
    seen_seq = 0
    counts: Counter[str] = Counter()
    agent_stats: dict[str, dict[str, Any]] = {}
    latest_score: dict[str, Any] | None = None
    payments: dict[str, dict[str, Any]] = {}
    authorizations: Counter[str] = Counter()
    payment_requests: dict[str, dict[str, Any]] = {}
    latest_authorization: dict[str, Any] | None = None

    def require_runtime() -> None:
        if runtime is None:
            raise HTTPException(503, "The desk runtime is not enabled.")

    def refresh_projection() -> None:
        nonlocal seen_seq, latest_score, latest_authorization
        require_runtime()
        with lock:
            while True:
                batch = runtime.store.query(since_seq=seen_seq, limit=1000)
                for event in batch:
                    seen_seq = max(seen_seq, event.seq or 0)
                    counts[event.type] += 1
                    data = event.payload
                    if event.type == "agent_action":
                        agent = data.get("agent", event.actor or "unknown")
                        stats = agent_stats.setdefault(agent, {"actions": 0, "errors": 0})
                        stats["actions"] += 1
                        stats["errors"] += int(data.get("action_type") == "error")
                        stats["last"] = event.model_dump(mode="json")
                    elif event.type == "score_update":
                        latest_score = data
                    elif event.type in {
                        "payment_requested",
                        "chip_response",
                        "payment_executed",
                        "bank_rejected",
                    }:
                        pay_id = data.get("pay_id")
                        if pay_id:
                            row = payments.setdefault(
                                pay_id, {"pay_id": pay_id, "status": "requested", "events": []}
                            )
                            row["events"].append(event.id)
                            row["updated_at"] = event.event_time.isoformat()
                            row["seq"] = event.seq
                            if event.type == "payment_requested":
                                payment_requests[pay_id] = data
                                row.update(
                                    {
                                        k: data.get(k)
                                        for k in (
                                            "vendor_id",
                                            "amount_minor",
                                            "currency",
                                            "account_masked",
                                            "payable_ids",
                                        )
                                    }
                                )
                            elif event.type == "chip_response":
                                indicator = authorization_indicator(
                                    event, runtime.signer, payment_requests.get(pay_id)
                                )
                                latest_authorization = {
                                    **event.model_dump(mode="json"),
                                    "indicator": indicator,
                                }
                                authorizations["approved" if data.get("ok") else "blocked"] += 1
                                row["status"] = "authorized" if data.get("ok") else "blocked"
                                row["chip"] = data
                                row["chip_event_id"] = event.id
                                row["chip_event_time"] = event.event_time.isoformat()
                                row["chip_seq"] = event.seq
                                row["chip_indicator"] = indicator
                            elif event.type == "payment_executed":
                                row["status"] = "settled"
                                row["bank_ref"] = data.get("bank_ref")
                            else:
                                row["status"] = "bank_rejected"
                                row["bank_reason"] = data.get("reason")
                if len(batch) < 1000:
                    break

    def llm_status() -> dict[str, Any]:
        """Credential presence is not proof of an API response; expose no credential value."""
        client = runtime.llm
        name = type(client).__name__
        credentials = getattr(client, "credentials_available", None)
        usage = getattr(client, "usage_report", None)
        configured = bool(credentials()) if callable(credentials) else None
        report = usage() if callable(usage) else None
        responses = int(report.calls) if report is not None else 0
        return {
            "client": name,
            "model": getattr(client, "model", None),
            "credentials_present": configured,
            "responses_recorded": responses,
            "api_verified": responses > 0,
            "mode": "responses_recorded"
            if responses
            else "configured_unverified"
            if configured
            else "credentials_missing"
            if configured is False
            else "custom_client_unverified",
            "structured_challenges": "deterministic extraction and policy checks",
        }

    @router.get("/summary")
    def summary() -> dict[str, Any]:
        refresh_projection()
        payable_rows = runtime.ledger.payables()
        totals: dict[str, int] = defaultdict(int)
        for row in payable_rows:
            if row.status in {"open", "scheduled", "held"}:
                totals[row.currency] += row.amount_minor
        with lock:
            return {
                "latest_seq": seen_seq,
                "event_counts": dict(counts),
                "authorizations": dict(authorizations),
                "latest_authorization": latest_authorization,
                "agents": dict(agent_stats),
                "invoices": dict(Counter(row.status for row in runtime.invoice_state.invoices())),
                "payables": dict(Counter(row.status for row in payable_rows)),
                "open_amounts_minor": dict(totals),
                "open_packets": len(runtime.packets.packets(status="open")),
                "score": latest_score,
                "reconciliation": runtime.ledger.ap_control_reconciles().model_dump(mode="json"),
                "bank_is_simulated": True,
                "llm": llm_status(),
            }

    @router.get("/payments")
    def payment_history(limit: int = Query(200, ge=1, le=1000)) -> dict[str, Any]:
        refresh_projection()
        with lock:
            workflow = {p.pay_id: p for p in runtime.payment_state.payments()}
            rows = []
            for row in sorted(payments.values(), key=lambda p: p["seq"], reverse=True):
                current = workflow.get(row["pay_id"])
                rows.append(
                    {
                        **row,
                        "workflow_status": current.status if current else None,
                        "packet_id": current.packet_id if current else None,
                    }
                )
            return {"payments": rows[:limit], "count": len(rows)}

    @router.get("/vendors")
    def vendors() -> dict[str, Any]:
        require_runtime()
        rows = []
        for vendor in runtime.vendors.vendors():
            account = runtime.vendors.active_account(vendor.id)
            rows.append(
                {
                    **vendor.model_dump(mode="json"),
                    "on_chip": bool(account and account.on_chip),
                    "account_hash_prefix": account.account_hash[:12] if account else None,
                }
            )
        return {"vendors": rows, "count": len(rows)}

    @router.get("/invoices")
    def invoices() -> dict[str, Any]:
        require_runtime()
        rows = runtime.invoice_state.invoices()
        return {"invoices": [r.model_dump(mode="json") for r in rows], "count": len(rows)}

    @router.get("/ledger")
    def ledger(
        since_seq: int = Query(0, ge=0), limit: int = Query(200, ge=1, le=1000)
    ) -> dict[str, Any]:
        require_runtime()
        entries = runtime.ledger.entries(since_seq=since_seq, limit=limit)
        return {
            "entries": [e.model_dump(mode="json") for e in entries],
            "balances": runtime.ledger.balances(runtime.clock.sim_now.date()),
            "reconciliation": runtime.ledger.ap_control_reconciles().model_dump(mode="json"),
            "has_more": len(entries) == limit,
        }

    @router.get("/ledger/{entry_id}")
    def journal_entry(entry_id: str) -> dict[str, Any]:
        require_runtime()
        entry = runtime.ledger.get_entry(entry_id)
        if entry is None:
            raise HTTPException(404, "Journal entry not found")
        return entry.model_dump(mode="json")

    @router.get("/events/{event_id}")
    def event_detail(event_id: str) -> dict[str, Any]:
        require_runtime()
        event = runtime.store.get(event_id)
        if event is None:
            raise HTTPException(404, "Event not found")
        return event.model_dump(mode="json")

    app.include_router(router)
