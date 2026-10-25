"""First-run preparation must not start hardware, spend money or alter payment state."""

from __future__ import annotations

import json
import os
import socket
import subprocess
from pathlib import Path

import pytest

from scripts import prepare

WORLD_FILES = (
    "world.jsonl",
    "suppliers.json",
    "people.json",
    "fx_rates.json",
    "answer_key.json",
    "invoices_truth.json",
)


@pytest.fixture(autouse=True)
def clean_environment(monkeypatch):
    for name in (
        "SIGNER",
        "PAYGUARD_SERIAL_DEVICE",
        "PAYGUARD_CONTROLLER_TOKEN",
        "PAYGUARD_HW_INITIALIZE",
        "PAYGUARD_HW_PROVISION",
        "PAYGUARD_AUTOSTART",
        "PAYGUARD_DB",
        "PAYGUARD_WORLD_DIR",
        "PAYGUARD_ADMIN_KEY_FILE",
        "PAYGUARD_CONTROLLER_KEY_FILE",
        "PAYGUARD_RESPONSE_KEY_FILE",
        "ANTHROPIC_API_KEY",
        "ANTHROPIC_AUTH_TOKEN",
        "PAYGUARD_ANTHROPIC_KEY_FILE",
    ):
        monkeypatch.setenv(name, "")
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(
        socket,
        "create_connection",
        lambda *a, **kw: pytest.fail("No device/API connection during local checks"),
    )


def world_files(world):
    world.mkdir(parents=True, exist_ok=True)
    for name in WORLD_FILES:
        (world / name).write_text("{}")


def test_cold_world_is_prepared_before_check_and_existing_database_is_preserved(
    tmp_path, monkeypatch, capsys
):
    world, private, db = tmp_path / "world", tmp_path / "private", tmp_path / "existing.db"
    db.write_bytes(b"existing payment state")
    seen = []

    def generate(path, *, fx_cache, generate):
        seen.append((path, generate))
        world_files(path)

    monkeypatch.setattr(prepare, "ensure_world", generate)
    assert (
        prepare.main(
            [
                "--mode",
                "stub",
                "--world",
                str(world),
                "--private-dir",
                str(private),
                "--db",
                str(db),
            ]
        )
        == 0
    )
    assert seen == [(world, True)]
    assert db.read_bytes() == b"existing payment state"
    assert private.joinpath("controller-token").exists()
    assert "not a hardware acceptance result" in capsys.readouterr().out


def test_hardware_check_never_opens_device_or_creates_database(tmp_path, monkeypatch, capsys):
    world, private, db = tmp_path / "world", tmp_path / "private", tmp_path / "new.db"
    world_files(world)
    bundle = tmp_path / "keys.json"
    bundle.write_text(
        json.dumps({"admin": "11" * 32, "controller": "22" * 32, "response": "33" * 32})
    )
    bundle.chmod(0o600)
    real_open = os.open

    def guarded_open(path, *args, **kwargs):
        assert str(path) != "/dev/null", "Preparation must only stat the UART path, never open it"
        return real_open(path, *args, **kwargs)

    monkeypatch.setattr(os, "open", guarded_open)
    assert (
        prepare.main(
            [
                "--mode",
                "hw",
                "--device",
                "/dev/null",
                "--keys",
                str(bundle),
                "--world",
                str(world),
                "--private-dir",
                str(private),
                "--db",
                str(db),
                "--port",
                "8000",
            ]
        )
        == 0
    )
    assert not db.exists()
    assert os.environ["SIGNER"] == "hw"
    assert os.environ["PAYGUARD_HW_INITIALIZE"] == "0"
    assert os.environ["PAYGUARD_HW_PROVISION"] == "0"
    output = capsys.readouterr().out
    assert "11" * 32 not in output and "22" * 32 not in output and "33" * 32 not in output
    assert (private / "controller-token").read_text().strip() not in output


@pytest.mark.parametrize("flag", ["--initialize", "--provision", "--open"])
def test_startup_mutation_flags_are_rejected_before_preparation(flag, monkeypatch):
    monkeypatch.setattr(
        prepare,
        "ensure_world",
        lambda *a, **kw: pytest.fail("No preparation after an invalid mode flag"),
    )
    assert prepare.main([flag]) == 2


def test_incomplete_world_is_not_overwritten(tmp_path):
    world = tmp_path / "world"
    world.mkdir()
    sentinel = world / "suppliers.json"
    sentinel.write_text("keep the existing supplier data")
    assert (
        prepare.main(
            ["--mode", "stub", "--world", str(world), "--private-dir", str(tmp_path / "private")]
        )
        == 2
    )
    assert sentinel.read_text() == "keep the existing supplier data"
    assert not (tmp_path / "private").exists()


def test_missing_hardware_does_not_fall_back_to_stub(tmp_path):
    world = tmp_path / "world"
    world_files(world)
    assert (
        prepare.main(
            [
                "--mode",
                "hw",
                "--device",
                str(tmp_path / "missing-uart"),
                "--world",
                str(world),
                "--private-dir",
                str(tmp_path / "private"),
                "--db",
                str(tmp_path / "payguard-hw.db"),
            ]
        )
        == 2
    )
    assert os.environ["SIGNER"] == "hw"
    assert not (tmp_path / "payguard-hw.db").exists()


def test_shell_installs_locked_dependencies_and_preserves_arguments(tmp_path):
    executable = tmp_path / "uv"
    log = tmp_path / "arguments.jsonl"
    executable.write_text(
        '#!/usr/bin/env python3\nimport json,os,sys\nwith open(os.environ["PREP_TEST_LOG"],"a") as f: f.write(json.dumps(sys.argv[1:])+"\\n")\n'
    )
    executable.chmod(0o755)
    script = Path(__file__).resolve().parents[2] / "scripts" / "prepare_mac.sh"
    result = subprocess.run(
        ["bash", str(script), "--mode", "stub", "--world", "path with spaces"],
        env={**os.environ, "PATH": f"{tmp_path}:{os.environ['PATH']}", "PREP_TEST_LOG": str(log)},
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    assert [json.loads(line) for line in log.read_text().splitlines()] == [
        ["sync", "--frozen"],
        [
            "run",
            "--frozen",
            "python",
            "-m",
            "scripts.prepare",
            "--mode",
            "stub",
            "--world",
            "path with spaces",
        ],
    ]
