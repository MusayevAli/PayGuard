"""Read-only access to the generated source documents and business records.

This catalog is available before the simulation clock reaches an invoice. It
never imports the scoring answer key or extraction ground truth, and browsing
it does not deliver events to agents or authorize payments.
"""

from __future__ import annotations

import json
from collections import Counter
from pathlib import Path
from typing import Any
from urllib.parse import quote

from fastapi import APIRouter, FastAPI, HTTPException, Query
from fastapi.responses import FileResponse

_MEDIA_TYPES = {
    ".pdf": "application/pdf",
    ".png": "image/png",
    ".json": "application/json",
    ".eml": "message/rfc822",
}


def _records(path: Path) -> list[dict[str, Any]]:
    if not path.is_file():
        return []
    rows = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
        raise ValueError(f"Invalid dataset record collection: {path.name}")
    return rows


def _masked_account(vendor: dict[str, Any]) -> str | None:
    bank = vendor.get("bank_account") or {}
    account = vendor.get("iban") or bank.get("iban") or bank.get("account_number")
    return f"•••• {str(account)[-4:]}" if account else None


def install_dataset(app: FastAPI, runtime: Any, world_dir: Path) -> None:
    """Expose only named dataset records and registered invoice documents."""
    root = Path(world_dir).resolve()
    vendors = _records(root / "suppliers.json")
    vendor_names = {row["id"]: row.get("legal_name", row["id"]) for row in vendors}
    orders = _records(root / "pos.json")
    receipts = _records(root / "receipts.json")
    order_vendors = {row["id"]: row.get("supplier_id") for row in orders}
    invoice_rows: dict[str, dict[str, Any]] = {}
    document_paths: dict[str, str] = {}
    event_count = 0
    manifest = root / "world.jsonl"
    if manifest.is_file():
        with manifest.open(encoding="utf-8") as stream:
            for line in stream:
                if not line.strip():
                    continue
                event = json.loads(line)
                event_count += 1
                if event.get("type") != "invoice_received":
                    continue
                payload = event.get("payload", {})
                invoice_id = payload.get("invoice_id")
                if not isinstance(invoice_id, str):
                    continue
                vendor_id = payload.get("supplier_hint")
                document_path = payload.get("document_path")
                if isinstance(document_path, str):
                    document_paths[invoice_id] = document_path
                invoice_rows.setdefault(
                    invoice_id,
                    {
                        "id": invoice_id,
                        "supplier_id": vendor_id,
                        "supplier_name": vendor_names.get(
                            vendor_id, payload.get("sender") or "Unknown supplier"
                        ),
                        "format": payload.get("format"),
                        "received_via": payload.get("received_via"),
                        "sender": payload.get("sender"),
                        "known_at": event.get("known_at"),
                        "document_name": Path(document_path).name
                        if isinstance(document_path, str)
                        else None,
                        "document_url": f"/dataset/documents/{quote(invoice_id, safe='')}"
                        if isinstance(document_path, str)
                        else None,
                        "size_bytes": payload.get("size_bytes"),
                        "pages": payload.get("pages"),
                    },
                )
    invoices = sorted(invoice_rows.values(), key=lambda row: (row["known_at"] or "", row["id"]))
    orders = [
        {
            **row,
            "supplier_name": vendor_names.get(row.get("supplier_id"), "Unknown supplier"),
            "total_minor": sum(
                line.get("qty", 0) * line.get("unit_price_minor", 0)
                for line in row.get("lines", [])
            ),
        }
        for row in orders
    ]
    receipts = [
        {
            **row,
            "supplier_id": order_vendors.get(row.get("po_id")),
            "supplier_name": vendor_names.get(
                order_vendors.get(row.get("po_id")), "Unknown supplier"
            ),
        }
        for row in receipts
    ]
    vendors = [
        {
            **{
                key: row.get(key)
                for key in (
                    "id",
                    "legal_name",
                    "country",
                    "currency",
                    "terms",
                    "category",
                    "email",
                    "language",
                )
            },
            "account_masked": _masked_account(row),
        }
        for row in vendors
    ]
    counts = {
        "invoices": len(invoices),
        "purchase_orders": len(orders),
        "receipts": len(receipts),
        "vendors": len(vendors),
        "statements": sum(1 for path in (root / "statements").rglob("*.mt940") if path.is_file()),
        "events": event_count,
    }
    router = APIRouter(prefix="/dataset", tags=["dataset"])

    def statuses() -> dict[str, dict[str, Any]]:
        if runtime is None:
            return {}
        result: dict[str, dict[str, Any]] = {}
        cursor = 0
        while True:
            events = runtime.store.query(type="invoice_received", since_seq=cursor, limit=1000)
            for event in events:
                cursor = max(cursor, event.seq or 0)
                invoice_id = event.payload.get("invoice_id")
                if invoice_id in invoice_rows:
                    result[invoice_id] = {"status": "received"}
            if len(events) < 1000:
                break
        for row in runtime.invoice_state.invoices():
            if row.invoice_id in invoice_rows:
                result[row.invoice_id] = {
                    "status": row.status,
                    "invoice_number": row.invoice_number,
                    "rule_id": row.rule_id,
                    "reason": row.reason,
                    "entry_id": row.entry_id,
                }
        return result

    def page(rows: list[dict[str, Any]], q: str, offset: int, limit: int) -> dict[str, Any]:
        query = q.strip().casefold()
        matched = (
            [row for row in rows if query in json.dumps(row, ensure_ascii=False).casefold()]
            if query
            else rows
        )
        selected = matched[offset : offset + limit]
        return {
            "items": selected,
            "count": len(selected),
            "total": len(matched),
            "offset": offset,
            "limit": limit,
        }

    @router.get("/summary")
    def summary() -> dict[str, Any]:
        observed = statuses()
        queued = [row for row in invoices if row["id"] not in observed]
        return {
            "available": manifest.is_file(),
            "counts": counts,
            "formats": dict(Counter(row["format"] for row in invoices)),
            "first_invoice_at": invoices[0]["known_at"] if invoices else None,
            "next_invoice_at": queued[0]["known_at"] if queued else None,
            "received_invoices": len(observed),
            "queued_invoices": len(queued),
            "notice": "Generated demonstration data. Queued documents are available to browse now; agents process them when the simulation reaches their arrival time.",
        }

    @router.get("/invoices")
    def list_invoices(
        q: str = Query("", max_length=200),
        offset: int = Query(0, ge=0),
        limit: int = Query(300, ge=1, le=1000),
    ) -> dict[str, Any]:
        observed = statuses()
        return page(
            [{**row, **observed.get(row["id"], {"status": "queued"})} for row in invoices],
            q,
            offset,
            limit,
        )

    @router.get("/purchase-orders")
    def list_orders(
        q: str = Query("", max_length=200),
        offset: int = Query(0, ge=0),
        limit: int = Query(300, ge=1, le=1000),
    ) -> dict[str, Any]:
        return page(orders, q, offset, limit)

    @router.get("/receipts")
    def list_receipts(
        q: str = Query("", max_length=200),
        offset: int = Query(0, ge=0),
        limit: int = Query(300, ge=1, le=1000),
    ) -> dict[str, Any]:
        return page(receipts, q, offset, limit)

    @router.get("/vendors")
    def list_vendors(
        q: str = Query("", max_length=200),
        offset: int = Query(0, ge=0),
        limit: int = Query(300, ge=1, le=1000),
    ) -> dict[str, Any]:
        return page(vendors, q, offset, limit)

    @router.get("/documents/{invoice_id}")
    def document(invoice_id: str) -> FileResponse:
        relative = document_paths.get(invoice_id)
        if relative is None:
            raise HTTPException(404, "Invoice document not found.")
        source = Path(relative)
        target = (root / source).resolve()
        # A manifest entry cannot turn this endpoint into a local-file server.
        if (
            source.is_absolute()
            or ".." in source.parts
            or not source.parts
            or source.parts[0] not in {"invoices", "emails"}
            or not target.is_relative_to(root / source.parts[0])
            or target.suffix.lower() not in _MEDIA_TYPES
            or not target.is_file()
        ):
            raise HTTPException(404, "Invoice document not found.")
        return FileResponse(
            target,
            media_type=_MEDIA_TYPES[target.suffix.lower()],
            filename=target.name,
            content_disposition_type="inline"
            if target.suffix.lower() in {".pdf", ".png"}
            else "attachment",
            headers={"X-Content-Type-Options": "nosniff", "Cache-Control": "no-cache"},
        )

    app.include_router(router)
