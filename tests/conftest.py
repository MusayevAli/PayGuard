"""Shared fixtures: generate the world once per session (two runs for determinism)."""

from __future__ import annotations

import json
import subprocess
import sys
import time
from pathlib import Path

import pytest

from sim.models import Event

REPO_ROOT = Path(__file__).resolve().parents[1]
SEED = 42


@pytest.fixture(autouse=True)
def _no_credentials_unless_api(
    request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Tests never see the developer's key or .env, so nothing offline can spend money or change
    behaviour because a key happens to be present. The two API-backed tests carry ``@pytest.mark.api``
    and keep the real environment."""
    if request.node.get_closest_marker("api") is not None:
        return
    monkeypatch.setenv("PAYGUARD_ENV_FILE", str(REPO_ROOT / "tests" / "no-such.env"))
    for name in (
        "ANTHROPIC_API_KEY",
        "ANTHROPIC_AUTH_TOKEN",
        "PAYGUARD_MAX_USD",
        "PAYGUARD_MODEL",
        "PAYGUARD_MODEL_TEXT",
        "PAYGUARD_SPEND_DB",
    ):
        monkeypatch.delenv(name, raising=False)


def run_generator(out_dir: Path, seed: int = SEED) -> float:
    """Run the generator in a fresh interpreter; return the wall-clock seconds it took."""
    started = time.perf_counter()
    proc = subprocess.run(
        [sys.executable, "-m", "sim.generate_world", "--seed", str(seed), "--out", str(out_dir)],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    elapsed = time.perf_counter() - started
    assert proc.returncode == 0, f"generator failed\nSTDOUT:\n{proc.stdout}\nSTDERR:\n{proc.stderr}"
    return elapsed


@pytest.fixture(scope="session")
def world_dir(tmp_path_factory: pytest.TempPathFactory) -> Path:
    out = tmp_path_factory.mktemp("world") / "run1"
    elapsed = run_generator(out)
    (out / "GENERATION_SECONDS").write_text(f"{elapsed:.3f}")
    return out


@pytest.fixture(scope="session")
def world_dir_second_run(tmp_path_factory: pytest.TempPathFactory) -> Path:
    out = tmp_path_factory.mktemp("world") / "run2"
    run_generator(out)
    return out


def load_world_events(world_dir: Path) -> list[Event]:
    events: list[Event] = []
    with (world_dir / "world.jsonl").open(encoding="utf-8") as fh:
        for line in fh:
            if line.strip():
                events.append(Event.model_validate(json.loads(line)))
    return events


@pytest.fixture(scope="session")
def world_events(world_dir: Path) -> list[Event]:
    return load_world_events(world_dir)


def load_json(path: Path):
    with path.open(encoding="utf-8") as fh:
        return json.load(fh)


def synthetic_fx():
    """A small ECB-style table: EUR base, units per EUR, three dates (the third carried forward)."""
    from sim.fx import DayRates, FxTable, SourceWindow

    return FxTable(
        base="EUR",
        source="synthetic test table",
        source_window=SourceWindow(start="2026-10-01", end="2026-10-08"),
        rates={
            "2026-10-01": DayRates(
                rates={"CZK": "24.3450", "PLN": "4.2680", "USD": "1.0840", "JPY": "162.30"},
                carried_forward=False,
                source_date="2026-10-01",
            ),
            "2026-10-02": DayRates(
                rates={"CZK": "24.5000", "PLN": "4.2500", "USD": "1.0900", "JPY": "162.00"},
                carried_forward=False,
                source_date="2026-10-02",
            ),
            "2026-10-03": DayRates(
                rates={"CZK": "4", "PLN": "2", "USD": "1", "JPY": "100"},
                carried_forward=True,
                source_date="2026-10-02",
            ),
            "2026-10-05": DayRates(
                rates={"CZK": "24.3450", "PLN": "4.2680", "USD": "1.0840", "JPY": "162.30"},
                carried_forward=False,
                source_date="2026-10-05",
            ),
            "2026-10-08": DayRates(
                rates={"CZK": "24.345", "PLN": "4.26", "USD": "1.08", "JPY": "162"},
                carried_forward=False,
                source_date="2026-10-08",
            ),
            "2026-10-09": DayRates(
                rates={"CZK": "24.345", "PLN": "4.26", "JPY": "162"},
                carried_forward=False,
                source_date="2026-10-09",
            ),
        },
    )
