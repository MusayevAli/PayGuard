"""Blocking agent/UART work must leave the event loop free without parallel chip writes."""

from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor

import pytest

from tests.integration.test_hardware_demo import AUTH, hardware_desk  # noqa: F401


@pytest.mark.parametrize("action", ["judge_message", "packet_decision", "authorization"])
def test_state_responds_while_mutation_holds_runner_work(hardware_desk, monkeypatch, action):  # noqa: F811
    rt, client, _ = hardware_desk
    if action != "judge_message":
        large = client.post("/demo/scenarios/large", headers=AUTH).json()
        assert large["status"] == "awaiting_decision"
        packet_path = f"/packets/{large['packet_id']}/decide"
        decision = {
            "decision": "approve",
            "note": "Test controller reviewed evidence",
            "decided_by": "human",
        }
    if action == "judge_message":
        path = "/judge/message"
        body = {
            "supplier_id": rt.suppliers[0].id,
            "channel": "email",
            "from_address": "judge@example.invalid",
            "body": "Please check this routine vendor message",
        }
    elif action == "packet_decision":
        path, body = packet_path, decision
    else:
        assert client.post(packet_path, headers=AUTH, json=decision).status_code == 200
        path, body = f"/controller/payments/{large['pay_id']}/authorize", {}
    entered, release = threading.Event(), threading.Event()
    method_name = "run_task" if action == "authorization" else "drain"
    original = getattr(rt.runner, method_name)

    def slow_work(*args, **kwargs):
        # Represents a synchronous model call made while the existing runner
        # serialization lock is held. The production lock is not replaced.
        with rt.runner._lock:
            entered.set()
            assert release.wait(8), "test failed to release blocked agent work"
            return original(*args, **kwargs)

    monkeypatch.setattr(rt.runner, method_name, slow_work)
    with ThreadPoolExecutor(max_workers=2) as pool:
        mutation = pool.submit(client.post, path, headers=AUTH, json=body)
        try:
            assert entered.wait(3), "mutation never reached the synchronous agent work"
            read = pool.submit(client.get, "/state")
            result = read.result(timeout=2)
            assert result.status_code == 200 and "sim_now" in result.json()
            assert not mutation.done(), (
                "the state response must arrive while mutation is still blocked"
            )
        finally:
            release.set()
        assert mutation.result(timeout=5).status_code == 200


def test_concurrent_uart_status_reads_keep_one_journal_exchange_in_flight(
    hardware_desk,  # noqa: F811
    monkeypatch,
):
    rt, client, transport = hardware_desk
    entered, release = threading.Event(), threading.Event()
    guard = threading.Lock()
    active = 0
    maximum = 0
    count = 0
    original = transport.exchange

    def slow_exchange(frame, mac):
        nonlocal active, maximum, count
        with guard:
            active += 1
            count += 1
            maximum = max(maximum, active)
            first = count == 1
        try:
            if first:
                entered.set()
                assert release.wait(8), "test failed to release blocked UART"
            return original(frame, mac)
        finally:
            with guard:
                active -= 1

    monkeypatch.setattr(transport, "exchange", slow_exchange)
    journal_before = rt.signer._con.execute(
        "SELECT COUNT(*) FROM pg_hw_journal WHERE state='complete'"
    ).fetchone()[0]
    with ThreadPoolExecutor(max_workers=3) as pool:
        first = pool.submit(client.get, "/chip/status")
        try:
            assert entered.wait(3)
            second = pool.submit(client.get, "/chip/status")
            state = pool.submit(client.get, "/state").result(timeout=2)
            assert state.status_code == 200 and not first.done()
            assert maximum == 1
        finally:
            release.set()
        assert first.result(timeout=5).status_code == second.result(timeout=5).status_code == 200
    assert count == 2 and maximum == 1
    assert (
        rt.signer._con.execute(
            "SELECT COUNT(*) FROM pg_hw_journal WHERE state='pending'"
        ).fetchone()[0]
        == 0
    )
    assert (
        rt.signer._con.execute(
            "SELECT COUNT(*) FROM pg_hw_journal WHERE state='complete'"
        ).fetchone()[0]
        == journal_before + 2
    )
