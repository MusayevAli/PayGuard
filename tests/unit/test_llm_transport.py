"""Real SDK/HTTP transport regressions, using only a local fake Messages API."""

from __future__ import annotations

import asyncio
import hashlib
import json
import threading
from collections.abc import Iterator
from contextlib import suppress
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import anthropic
import httpx2
import pytest

from agents.documents import ContentPart, Document
from agents.llm import LLMClient
from agents.parsed import LocaleHints, ParsedInvoice


class _UnexpectedSyncClient:
    """A cache miss must not be repaired by an extra synchronous API call."""

    calls = 0

    @property
    def messages(self) -> _UnexpectedSyncClient:
        return self

    def create(self, **_: Any) -> None:
        self.calls += 1
        pytest.fail("prefetched invoice was missing from cache; sync fallback attempted")


@pytest.fixture
def local_messages_api() -> Iterator[tuple[str, list[str]]]:
    requests: list[str] = []
    lock = threading.Lock()

    class MessagesHandler(BaseHTTPRequestHandler):
        # Keep connections alive: HTTP/1.0 would hide the loop-affinity defect by
        # forcing the SDK to open a fresh connection for every request.
        protocol_version = "HTTP/1.1"

        def do_POST(self) -> None:
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            assert self.path == "/v1/messages"
            invoice_number = body["messages"][0]["content"][0]["text"]
            with lock:
                requests.append(invoice_number)
            invoice = ParsedInvoice(
                supplier_name="Local Transport Test Supplier",
                invoice_number=invoice_number,
                invoice_date="2026-02-01",
                currency="EUR",
                lines=[],
                net="10.00",
                vat_amount="0.00",
                gross="10.00",
                confidence={
                    "supplier": 1,
                    "invoice_number": 1,
                    "dates": 1,
                    "totals": 1,
                    "lines": 1,
                },
            )
            response = {
                "id": f"msg_{invoice_number}",
                "type": "message",
                "role": "assistant",
                "model": body["model"],
                "content": [
                    {
                        "type": "tool_use",
                        "id": f"tool_{invoice_number}",
                        "name": "record_invoice",
                        "input": invoice.model_dump(exclude={"source"}),
                    }
                ],
                "stop_reason": "tool_use",
                "stop_sequence": None,
                "usage": {"input_tokens": 20, "output_tokens": 30},
            }
            encoded = json.dumps(response).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(encoded)))
            self.send_header("Request-Id", f"req_{invoice_number}")
            self.end_headers()
            self.wfile.write(encoded)
            self.wfile.flush()

        def log_message(self, *_: Any) -> None:
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), MessagesHandler)
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}", requests
    finally:
        server.shutdown()
        server.server_close()
        worker.join(timeout=5)


def _document(invoice_number: str) -> Document:
    return Document(
        invoice_id=invoice_number,
        format="structured",
        path=Path(f"{invoice_number}.json"),
        sha256=hashlib.sha256(invoice_number.encode()).hexdigest(),
        hints=LocaleHints(),
        parts=[ContentPart(kind="text", text=invoice_number)],
    )


@pytest.mark.parametrize("retries", [0, 2])
def test_extract_many_reuses_llm_client_across_event_loops(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    local_messages_api: tuple[str, list[str]],
    retries: int,
) -> None:
    """Sequential batches must cache both documents without a synchronous rescue.

    No SDK retries exposes a dead-loop connection immediately; the default retry
    count checks that normal configured behavior also remains supported.
    """
    base_url, requests = local_messages_api
    real_async_anthropic = anthropic.AsyncAnthropic
    created: list[anthropic.AsyncAnthropic] = []

    def local_client(**kwargs: Any) -> anthropic.AsyncAnthropic:
        kwargs["max_retries"] = retries
        client = real_async_anthropic(
            api_key="local-test-key-not-a-credential",
            base_url=base_url,
            http_client=httpx2.AsyncClient(trust_env=False),
            **kwargs,
        )
        created.append(client)
        return client

    monkeypatch.setattr(anthropic, "AsyncAnthropic", local_client)
    monkeypatch.setattr(LLMClient, "credentials_available", staticmethod(lambda: True))
    sync_client = _UnexpectedSyncClient()
    llm = LLMClient(tmp_path / "llm.sqlite", client=sync_client, timeout=3)
    first, second = _document("LOCAL-001"), _document("LOCAL-002")
    try:
        # Each call starts and destroys its own asyncio.run() loop in a worker.
        llm.extract_many([first])
        assert created and all(client.is_closed() for client in created)
        llm.extract_many([second])
        assert all(client.is_closed() for client in created)
        assert llm._cached(first.sha256) is not None
        assert llm._cached(second.sha256) is not None
        assert llm.extract_invoice(first).invoice_number == "LOCAL-001"
        assert llm.extract_invoice(second).invoice_number == "LOCAL-002"
        assert requests == ["LOCAL-001", "LOCAL-002"]
        assert sync_client.calls == 0
        assert llm.usage_report().calls == 2
        # A third prefetch is entirely cached and must not create an SDK client.
        before = len(created)
        llm.extract_many([first, second])
        assert len(created) == before
        assert requests == ["LOCAL-001", "LOCAL-002"]
    finally:
        llm.close()
        # Cleanup remains safe even if this regression reintroduces a client
        # whose transport survives its event loop; assertions above catch it.
        for client in created:
            if not client.is_closed():
                with suppress(RuntimeError):
                    asyncio.run(client.close())
