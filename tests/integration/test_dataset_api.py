"""The source dataset is visible without advancing agents or exposing scoring truth."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from agents.state import AgentState
from api.dataset import install_dataset
from core.events import EventStore, make_event


def _write_world(root: Path) -> None:
    (root / "invoices").mkdir(parents=True)
    (root / "emails").mkdir()
    (root / "statements").mkdir()
    (root / "statements" / "day.mt940").write_text("statement")
    (root / "suppliers.json").write_text(
        json.dumps(
            [
                {
                    "id": "V-1",
                    "legal_name": "Example Vendor",
                    "currency": "EUR",
                    "country": "DE",
                    "iban": "DE12345678901234567890",
                    "category": "parts",
                }
            ]
        )
    )
    (root / "pos.json").write_text(
        json.dumps(
            [
                {
                    "id": "PO-1",
                    "supplier_id": "V-1",
                    "currency": "EUR",
                    "lines": [{"line_no": 1, "qty": 3, "unit_price_minor": 250}],
                }
            ]
        )
    )
    (root / "receipts.json").write_text(
        json.dumps(
            [
                {
                    "id": "GR-1",
                    "po_id": "PO-1",
                    "line_no": 1,
                    "qty": 3,
                }
            ]
        )
    )
    # Deliberately invalid: any attempt to load scoring or extraction truth fails.
    (root / "answer_key.json").write_text("DO NOT READ ANSWER KEY")
    (root / "invoices_truth.json").write_text("DO NOT READ EXTRACTION TRUTH")
    events = []
    for index, (extension, format_) in enumerate(
        [
            ("pdf", "pdf"),
            ("json", "structured"),
            ("eml", "eml"),
            ("png", "png"),
        ],
        1,
    ):
        invoice_id = f"INV-{index}"
        directory = "emails" if extension == "eml" else "invoices"
        path = f"{directory}/{invoice_id}.{extension}"
        (root / path).write_bytes(f"source {invoice_id}".encode())
        events.append(
            {
                "type": "invoice_received",
                "known_at": f"2026-10-0{index}T12:00:00Z",
                "payload": {
                    "invoice_id": invoice_id,
                    "supplier_hint": "V-1",
                    "format": format_,
                    "document_path": path,
                },
            }
        )
    (root / "world.jsonl").write_text("\n".join(json.dumps(event) for event in events))


@pytest.fixture
def dataset(tmp_path):
    world = tmp_path / "world"
    _write_world(world)
    store = EventStore(tmp_path / "runtime.db")
    invoice_state = AgentState(tmp_path / "runtime.db")
    runtime = SimpleNamespace(store=store, invoice_state=invoice_state)
    app = FastAPI()
    install_dataset(app, runtime, world)
    with TestClient(app) as client:
        yield client, runtime, world
    invoice_state.close()
    store.close()


def test_dataset_is_visible_before_any_invoice_arrives_and_is_read_only(dataset):
    client, runtime, _ = dataset
    before = runtime.store.latest_seq()
    response = client.get("/dataset/summary")
    assert response.status_code == 200
    summary = response.json()
    assert summary["counts"] == {
        "invoices": 4,
        "purchase_orders": 1,
        "receipts": 1,
        "vendors": 1,
        "statements": 1,
        "events": 4,
    }
    assert summary["formats"] == {"pdf": 1, "structured": 1, "eml": 1, "png": 1}
    assert summary["first_invoice_at"] == "2026-10-01T12:00:00Z"
    assert summary["received_invoices"] == 0
    assert summary["queued_invoices"] == 4
    invoices = client.get("/dataset/invoices?limit=1000").json()["items"]
    assert len(invoices) == 4
    assert {row["status"] for row in invoices} == {"queued"}
    assert {row["supplier_name"] for row in invoices} == {"Example Vendor"}
    for row in invoices:
        source = client.get(row["document_url"])
        assert source.status_code == 200
        assert source.content == f"source {row['id']}".encode()
    assert runtime.store.latest_seq() == before
    assert runtime.invoice_state.invoices() == []


def test_business_records_search_pagination_and_no_full_bank_account(dataset):
    client, _, _ = dataset
    rows = client.get("/dataset/purchase-orders").json()["items"]
    assert rows[0]["total_minor"] == 750
    assert rows[0]["supplier_name"] == "Example Vendor"
    receipt = client.get("/dataset/receipts?q=PO-1").json()["items"][0]
    assert receipt["supplier_id"] == "V-1"
    assert receipt["qty"] == 3
    vendor = client.get("/dataset/vendors").json()["items"][0]
    assert vendor["account_masked"] == "•••• 7890"
    assert "iban" not in vendor
    rows = client.get("/dataset/invoices?limit=1&offset=1&q=example").json()
    assert rows["total"] == 4
    assert rows["count"] == 1
    assert rows["items"][0]["id"] == "INV-2"
    assert client.get("/dataset/invoices?limit=1001").status_code == 422
    assert client.get("/dataset/invoices?offset=-1").status_code == 422


def test_queued_and_recorded_statuses_stay_distinct(dataset):
    client, runtime, _ = dataset
    now = datetime(2026, 10, 1, 12, tzinfo=UTC)
    runtime.store.append(
        make_event(
            "invoice_received",
            {
                "invoice_id": "INV-1",
                "format": "pdf",
                "document_path": "invoices/INV-1.pdf",
                "received_via": "portal",
                "sender": "vendor@example.test",
                "supplier_hint": "V-1",
                "attachments": [],
                "size_bytes": 12,
                "sha256": "0" * 64,
            },
            event_time=now,
            source="test",
        )
    )
    summary = client.get("/dataset/summary").json()
    assert summary["received_invoices"] == 1
    assert summary["queued_invoices"] == 3
    assert summary["next_invoice_at"] == "2026-10-02T12:00:00Z"
    assert client.get("/dataset/invoices").json()["items"][0]["status"] == "received"
    runtime.invoice_state.set_invoice(
        "INV-1", "held", rule_id="RECEIPT_MISSING", reason="Receipt not received", now=now
    )
    row = client.get("/dataset/invoices").json()["items"][0]
    assert row["status"] == "held"
    assert row["rule_id"] == "RECEIPT_MISSING"
    # Ad hoc demo fixtures are not mislabeled as members of this generated dataset.
    runtime.invoice_state.set_invoice("DEMO-NORMAL", "posted", rule_id=None, reason=None, now=now)
    assert client.get("/dataset/summary").json()["received_invoices"] == 1


def test_document_server_rejects_truth_paths_absolute_paths_and_symlinks(tmp_path):
    world = tmp_path / "world"
    _write_world(world)
    secret = tmp_path / "secret.json"
    secret.write_text("secret")
    (world / "invoices" / "symlink.json").symlink_to(secret)
    paths = [
        "answer_key.json",
        "invoices/../answer_key.json",
        str(secret),
        "invoices/symlink.json",
        "invoices/no-such.pdf",
    ]
    (world / "world.jsonl").write_text(
        "\n".join(
            json.dumps(
                {
                    "type": "invoice_received",
                    "known_at": "2026-10-01T12:00:00Z",
                    "payload": {"invoice_id": f"UNSAFE-{i}", "document_path": path},
                }
            )
            for i, path in enumerate(paths)
        )
    )
    app = FastAPI()
    install_dataset(app, None, world)
    with TestClient(app) as client:
        for i in range(len(paths)):
            assert client.get(f"/dataset/documents/UNSAFE-{i}").status_code == 404
        assert client.get("/dataset/documents/answer_key.json").status_code == 404
        assert client.get("/dataset/documents/missing").status_code == 404
