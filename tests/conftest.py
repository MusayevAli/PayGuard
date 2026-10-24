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
