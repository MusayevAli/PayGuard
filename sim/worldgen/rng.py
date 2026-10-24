"""Deterministic randomness: every stage derives its own stream from the master seed."""

from __future__ import annotations

import hashlib
import random
from collections.abc import Sequence
from datetime import date, datetime, time, timedelta


def derive_seed(seed: int, name: str) -> int:
    digest = hashlib.sha256(f"{seed}:{name}".encode()).digest()
    return int.from_bytes(digest[:8], "big")


def stream(seed: int, name: str) -> random.Random:
    return random.Random(derive_seed(seed, name))


def weighted_choice[T](rng: random.Random, items: Sequence[T], weights: Sequence[float]) -> T:
    return rng.choices(list(items), weights=list(weights), k=1)[0]


def business_time(
    rng: random.Random, day: date, start_hour: int = 7, end_hour: int = 17
) -> datetime:
    """A random instant on ``day`` inside working hours, never exactly at midnight."""
    from datetime import UTC

    minutes = rng.randrange(start_hour * 60, end_hour * 60)
    return datetime.combine(day, time(minutes // 60, minutes % 60, rng.randrange(60)), tzinfo=UTC)


def shift_to_weekday(day: date, rng: random.Random | None = None) -> date:
    """Move weekend dates to the next Monday (documents are produced on working days)."""
    while day.weekday() >= 5:
        day += timedelta(days=1)
    return day
