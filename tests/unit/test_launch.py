"""Operator startup preserves private material and requires explicit hardware setup."""

import json
import os
import stat

import pytest

from scripts.launch import (
    SetupError,
    configure,
    configure_api_key,
    controller_token,
    main,
    parser,
    split_role_bundle,
)


@pytest.fixture(autouse=True)
def isolated_environment(monkeypatch):
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
        # Record an undo even when initially absent: configure writes os.environ
        # directly, while delenv(..., raising=False) alone records no absent key.
        monkeypatch.setenv(name, "")
        monkeypatch.delenv(name, raising=False)


def test_controller_token_is_private_and_persistent(tmp_path):
    path = tmp_path / "private" / "controller-token"
    first = controller_token(path)
    assert len(first) >= 32
    assert controller_token(path) == first
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    path.chmod(0o644)
    with pytest.raises(SetupError, match="private regular file"):
        controller_token(path)


def test_role_split_reuses_matching_keys_and_never_overwrites_conflicts(tmp_path):
    bundle = tmp_path / "keys.json"
    values = {"admin": "11" * 32, "controller": "22" * 32, "response": "33" * 32}
    bundle.write_text(json.dumps(values))
    bundle.chmod(0o600)
    directory = tmp_path / "roles"
    paths = split_role_bundle(bundle, directory)
    assert {role: path.read_text().strip() for role, path in paths.items()} == values
    assert split_role_bundle(bundle, directory) == paths
    paths["controller"].write_text("44" * 32)
    before = {p.name: p.read_bytes() for p in directory.iterdir()}
    with pytest.raises(SetupError, match="differs"):
        split_role_bundle(bundle, directory)
    assert {p.name: p.read_bytes() for p in directory.iterdir()} == before


def test_hardware_never_falls_back_if_device_is_missing(tmp_path, monkeypatch):
    monkeypatch.delenv("SIGNER", raising=False)
    monkeypatch.delenv("PAYGUARD_SERIAL_DEVICE", raising=False)
    args = parser().parse_args(["--private-dir", str(tmp_path / "private"), "--check"])
    assert args.mode == "hw"
    with pytest.raises(SetupError, match="No software fallback"):
        configure(args)
    assert os.environ["SIGNER"] == "hw"
    assert not (tmp_path / "private").exists()


def test_check_preserves_database_and_ignores_old_initialize_flags(tmp_path, monkeypatch):
    monkeypatch.delenv("PAYGUARD_CONTROLLER_TOKEN", raising=False)
    monkeypatch.setenv("PAYGUARD_HW_INITIALIZE", "1")
    monkeypatch.setenv("PAYGUARD_HW_PROVISION", "1")
    world = tmp_path / "world"
    world.mkdir()
    for name in (
        "world.jsonl",
        "suppliers.json",
        "people.json",
        "fx_rates.json",
        "answer_key.json",
        "invoices_truth.json",
    ):
        (world / name).write_text("{}")
    db = tmp_path / "existing.db"
    db.write_bytes(b"existing persistent data")
    args = parser().parse_args(
        [
            "--mode",
            "stub",
            "--check",
            "--world",
            str(world),
            "--private-dir",
            str(tmp_path / "private"),
            "--db",
            str(db),
        ]
    )
    actual_db, token_path = configure(args)
    assert actual_db == db and db.read_bytes() == b"existing persistent data"
    assert token_path and token_path.is_file()
    assert os.environ["PAYGUARD_HW_INITIALIZE"] == "0"
    assert os.environ["PAYGUARD_HW_PROVISION"] == "0"
    assert os.environ["PAYGUARD_AUTOSTART"] == "0"


def test_incomplete_world_is_never_silently_overwritten(tmp_path, monkeypatch):
    monkeypatch.setenv("PAYGUARD_CONTROLLER_TOKEN", "test-controller-token-that-is-long-enough")
    world = tmp_path / "world"
    world.mkdir()
    existing = world / "suppliers.json"
    existing.write_text("keep this existing file")
    args = parser().parse_args(
        ["--mode", "stub", "--world", str(world), "--private-dir", str(tmp_path / "private")]
    )
    with pytest.raises(SetupError, match="Refusing to overwrite"):
        configure(args)
    assert existing.read_text() == "keep this existing file"


FAKE_API_KEY = "unit-test-api-key-never-real"


def private_api_key(path, value=FAKE_API_KEY):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(value + "\n")
    path.chmod(0o600)
    return path


def test_api_key_automatically_loads_from_private_directory_without_echo(tmp_path, capsys):
    private_api_key(tmp_path / "anthropic.key")
    configure_api_key(tmp_path)
    assert os.environ["ANTHROPIC_API_KEY"] == FAKE_API_KEY
    output = capsys.readouterr()
    assert FAKE_API_KEY not in output.out + output.err


@pytest.mark.parametrize("name", ["ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN"])
def test_existing_credentials_override_automatic_file_without_reading_it(
    tmp_path, monkeypatch, name
):
    path = private_api_key(tmp_path / "anthropic.key")
    path.chmod(0o644)  # Would fail if the lower-precedence file were opened.
    monkeypatch.setenv(name, "existing-env-credential")
    configure_api_key(tmp_path)
    assert os.environ[name] == "existing-env-credential"
    assert os.environ.get("ANTHROPIC_API_KEY") != FAKE_API_KEY


def test_explicit_flag_overrides_path_environment_and_existing_credentials(tmp_path, monkeypatch):
    selected = private_api_key(tmp_path / "selected.key")
    other = private_api_key(tmp_path / "other.key", "other-fake-key")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "old-env-key")
    monkeypatch.setenv("ANTHROPIC_AUTH_TOKEN", "old-env-token")
    monkeypatch.setenv("PAYGUARD_ANTHROPIC_KEY_FILE", str(other))
    args = parser().parse_args(["--api-key-file", str(selected), "--private-dir", str(tmp_path)])
    configure_api_key(args.private_dir, args.api_key_file)
    assert os.environ["ANTHROPIC_API_KEY"] == FAKE_API_KEY
    assert "ANTHROPIC_AUTH_TOKEN" not in os.environ


def test_explicit_file_environment_and_missing_explicit_fail_closed(tmp_path, monkeypatch):
    path = private_api_key(tmp_path / "chosen.key")
    monkeypatch.setenv("PAYGUARD_ANTHROPIC_KEY_FILE", str(path))
    args = parser().parse_args(["--private-dir", str(tmp_path)])
    configure_api_key(args.private_dir, args.api_key_file)
    assert os.environ["ANTHROPIC_API_KEY"] == FAKE_API_KEY
    with pytest.raises(SetupError, match="must exist"):
        configure_api_key(tmp_path, tmp_path / "missing.key")


def test_api_key_permissions_and_symlink_are_rejected_without_key_echo(tmp_path):
    path = private_api_key(tmp_path / "anthropic.key")
    path.chmod(0o644)
    with pytest.raises(SetupError) as caught:
        configure_api_key(tmp_path)
    assert FAKE_API_KEY not in str(caught.value)
    path.chmod(0o600)
    link = tmp_path / "linked.key"
    link.symlink_to(path)
    with pytest.raises(SetupError, match="private regular file"):
        configure_api_key(tmp_path, link)


@pytest.mark.parametrize(
    "value",
    [
        "",
        "two values",
        "secret\nANTHROPIC_API_KEY=injected",
        "secret\x00value",
        "secret$(command)",
        "secret\x1bvalue",
    ],
)
def test_api_key_invalid_values_are_never_exported_or_echoed(tmp_path, value):
    path = private_api_key(tmp_path / "anthropic.key", value)
    with pytest.raises(SetupError) as caught:
        configure_api_key(tmp_path, path)
    assert "ANTHROPIC_API_KEY" not in os.environ
    assert not value or value not in str(caught.value)


def test_absent_optional_api_key_keeps_structured_mode_available(tmp_path):
    configure_api_key(tmp_path)
    assert "ANTHROPIC_API_KEY" not in os.environ


def test_check_loads_api_key_without_network_or_secret_output(tmp_path, monkeypatch, capsys):
    import socket

    private = tmp_path / "private"
    private_api_key(private / "anthropic.key")
    world = tmp_path / "world"
    world.mkdir()
    for name in (
        "world.jsonl",
        "suppliers.json",
        "people.json",
        "fx_rates.json",
        "answer_key.json",
        "invoices_truth.json",
    ):
        (world / name).write_text("{}")
    monkeypatch.setattr(
        socket, "create_connection", lambda *a, **k: pytest.fail("--check must not contact an API")
    )
    assert (
        main(["--mode", "stub", "--check", "--world", str(world), "--private-dir", str(private)])
        == 0
    )
    assert os.environ["ANTHROPIC_API_KEY"] == FAKE_API_KEY
    output = capsys.readouterr()
    assert FAKE_API_KEY not in output.out + output.err
