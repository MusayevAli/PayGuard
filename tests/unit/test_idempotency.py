"""Idempotent requests serialize only matching route/key operations."""

from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import pytest

from api.main import IdempotencyCache


def request(path: str, key: str | None):
    return SimpleNamespace(
        headers={"Idempotency-Key": key} if key is not None else {},
        url=SimpleNamespace(path=path),
    )


@pytest.mark.parametrize(
    "other_path,other_key",
    [
        ("/clock/pause", "pause-request"),
        ("/clock/step", "different-step"),
        ("/clock/pause", "busy-request"),
    ],
    ids=["pause-during-step", "same-route-new-key", "same-key-different-route"],
)
def test_unrelated_operation_finishes_while_long_compute_is_blocked(other_path, other_key):
    cache = IdempotencyCache()
    entered, release = threading.Event(), threading.Event()

    def slow_compute():
        entered.set()
        assert release.wait(5), "test did not release the blocked operation"
        return 200, {"stepped": True}

    with ThreadPoolExecutor(max_workers=2) as pool:
        slow = pool.submit(cache.run, request("/clock/step", "busy-request"), slow_compute)
        try:
            assert entered.wait(2)
            unrelated = pool.submit(
                cache.run, request(other_path, other_key), lambda: (200, {"independent": True})
            )
            assert unrelated.result(timeout=2) == (200, {"independent": True})
            assert not slow.done(), "unrelated work must finish before the long operation"
        finally:
            release.set()
        assert slow.result(timeout=2) == (200, {"stepped": True})


def test_overlapping_duplicate_computes_once_and_replays_result():
    cache = IdempotencyCache()
    entered, duplicate_entered, release = (threading.Event() for _ in range(3))
    calls = []
    req = request("/clock/step", "one-step")
    expected = (200, {"day": 1})

    def compute():
        calls.append(threading.get_ident())
        entered.set()
        assert release.wait(5), "test did not release the first computation"
        return expected

    def duplicate():
        duplicate_entered.set()
        return cache.run(req, compute)

    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(cache.run, req, compute)
        try:
            assert entered.wait(2)
            second = pool.submit(duplicate)
            assert duplicate_entered.wait(2)
            with pytest.raises(TimeoutError):
                second.result(timeout=0.05)
        finally:
            release.set()
        assert first.result(timeout=2) == second.result(timeout=2) == expected
    assert len(calls) == 1

    def must_not_run():
        pytest.fail("a completed duplicate must replay its original result")

    assert cache.run(req, must_not_run) == expected


def test_failure_is_not_cached_and_the_same_key_can_retry():
    cache = IdempotencyCache()
    req = request("/clock/step", "retryable-step")
    attempts = []

    def compute():
        attempts.append(len(attempts) + 1)
        if len(attempts) == 1:
            raise RuntimeError("injected pre-completion failure")
        return 200, {"attempt": len(attempts)}

    with pytest.raises(RuntimeError, match="injected pre-completion failure"):
        cache.run(req, compute)
    assert cache.run(req, compute) == (200, {"attempt": 2})
    assert cache.run(req, compute) == (200, {"attempt": 2})
    assert attempts == [1, 2]


def test_waiting_duplicate_can_retry_after_first_compute_raises():
    cache = IdempotencyCache()
    entered, duplicate_entered, release = (threading.Event() for _ in range(3))
    req = request("/clock/step", "failed-first-attempt")
    attempts = []

    def failing():
        attempts.append("failed")
        entered.set()
        assert release.wait(5), "test did not release the failing computation"
        raise RuntimeError("first attempt failed")

    def retry():
        attempts.append("retried")
        return 200, {"recovered": True}

    def duplicate():
        duplicate_entered.set()
        return cache.run(req, retry)

    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(cache.run, req, failing)
        try:
            assert entered.wait(2)
            second = pool.submit(duplicate)
            assert duplicate_entered.wait(2)
            with pytest.raises(TimeoutError):
                second.result(timeout=0.05)
        finally:
            release.set()
        with pytest.raises(RuntimeError, match="first attempt failed"):
            first.result(timeout=2)
        assert second.result(timeout=2) == (200, {"recovered": True})
    assert attempts == ["failed", "retried"]
    assert cache.run(req, lambda: pytest.fail("successful retry was not cached")) == (
        200,
        {"recovered": True},
    )


def test_requests_without_an_idempotency_key_are_not_cached():
    cache = IdempotencyCache()
    req = request("/clock/pause", None)
    calls = []

    def compute():
        calls.append(len(calls) + 1)
        return 200, {"call": len(calls)}

    assert cache.run(req, compute) == (200, {"call": 1})
    assert cache.run(req, compute) == (200, {"call": 2})
