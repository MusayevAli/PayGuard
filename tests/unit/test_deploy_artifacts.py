"""Versioned FPGA artifacts cannot overwrite prior firmware or bypass hashes."""
from __future__ import annotations

import hashlib
import importlib.util
import json
import struct
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location("deploy_artifacts", ROOT / "hardware/scripts/deploy_kv260.py")
deployment = importlib.util.module_from_spec(spec)
spec.loader.exec_module(deployment)


def overlay(name="siliconsign-r5.bit.bin", *, duplicate=False, fragment="fragment@0"):
    """Small valid FDT structure; Linux topology checks are tested separately."""
    def begin(value):
        data = value.encode() + b"\0"
        return struct.pack(">I", 1) + data + b"\0" * (-len(data) % 4)

    def prop():
        data = name.encode() + b"\0"
        return struct.pack(">III", 3, len(data), 0) + data + b"\0" * (-len(data) % 4)

    body = begin("") + begin(fragment) + begin("__overlay__") + prop()
    if duplicate:
        body += prop()
    body += struct.pack(">4I", 2, 2, 2, 9)
    strings = b"firmware-name\0"
    start, names = 56, 56 + len(body)
    header = struct.pack(">10I", 0xD00DFEED, names + len(strings), start,
                         names, 40, 17, 16, 0, len(strings), len(body))
    return header + bytes(16) + body + strings


def make_build(tmp_path, *, name="siliconsign-r5.bit.bin", manifest=True):
    build = tmp_path / "build"
    build.mkdir()
    image = build / "siliconsign.bit.bin"
    image.write_bytes(bytes(range(256)) * 4)
    payload = overlay(name)
    (build / "siliconsign.dtbo").write_bytes(payload)
    record = {
        "schema": "siliconsign.deployment.v1",
        "firmware_name": name,
        "image_sha256": deployment.sha256(image),
        "overlay_sha256": hashlib.sha256(payload).hexdigest(),
    }
    if manifest:
        (build / "deployment.json").write_text(json.dumps(record))
    return build, record


def test_real_historical_overlay_remains_readable():
    actual = (ROOT / "hardware/recovery/r4/siliconsign.dtbo").read_bytes()
    assert deployment.firmware_name_from_overlay(actual) == "siliconsign.bit.bin"


def test_manifest_binds_versioned_firmware_and_keeps_input_filename(tmp_path):
    build, record = make_build(tmp_path)
    image, payload, name, digest = deployment.prepare_image(build)
    assert image.name == "siliconsign.bit.bin"
    assert name == "siliconsign-r5.bit.bin"
    assert digest == record["image_sha256"]
    assert hashlib.sha256(payload).hexdigest() == record["overlay_sha256"]


def test_legacy_package_needs_no_new_manifest(tmp_path):
    build, _ = make_build(tmp_path, name="siliconsign.bit.bin", manifest=False)
    assert deployment.prepare_image(build)[2] == "siliconsign.bit.bin"


def test_versioned_firmware_requires_manifest(tmp_path):
    build, _ = make_build(tmp_path, manifest=False)
    with pytest.raises(deployment.DeployError, match=r"requires deployment\.json"):
        deployment.prepare_image(build)


@pytest.mark.parametrize("field,value", [
    ("image_sha256", "0" * 64), ("overlay_sha256", "0" * 64),
    ("firmware_name", "siliconsign-r6.bit.bin"),
])
def test_mismatched_manifest_refused(tmp_path, field, value):
    build, record = make_build(tmp_path)
    record[field] = value
    (build / "deployment.json").write_text(json.dumps(record))
    with pytest.raises(deployment.DeployError, match="does not match"):
        deployment.prepare_image(build)


@pytest.mark.parametrize("name", ["../siliconsign-r5.bit.bin", "/tmp/image.bin",
                                  "other.bit.bin", "siliconsign-r0.bit.bin", "siliconsign-r5.bit.bin\0bad"])
def test_firmware_name_cannot_escape_namespace(name):
    with pytest.raises(deployment.DeployError):
        deployment.firmware_name_from_overlay(overlay(name))


@pytest.mark.parametrize("payload", [overlay(duplicate=True), overlay(fragment="fragment@1"),
                                     overlay()[:-1], b"\xd0\x0d\xfe\xed" + bytes(36)])
def test_ambiguous_or_malformed_overlay_is_rejected(payload):
    with pytest.raises(deployment.DeployError):
        deployment.firmware_name_from_overlay(payload)


def test_versioned_install_preserves_prior_image_and_journal(tmp_path, monkeypatch):
    build, record = make_build(tmp_path)
    firmware = tmp_path / "firmware"
    firmware.mkdir()
    previous = firmware / "siliconsign.bit.bin"
    previous.write_bytes(b"previous private image")
    journal = tmp_path / "journal.sqlite"
    journal.write_bytes(b"existing journal")
    monkeypatch.setattr(deployment, "FIRMWARE", previous)
    digest = deployment.install_firmware(build / "siliconsign.bit.bin", record["firmware_name"], record["image_sha256"])
    target = firmware / record["firmware_name"]
    assert digest == record["image_sha256"] == deployment.sha256(target)
    assert previous.read_bytes() == b"previous private image"
    assert journal.read_bytes() == b"existing journal"
    assert target.stat().st_mode & 0o777 == 0o600
    assert {p.name for p in firmware.iterdir()} == {previous.name, target.name}


@pytest.mark.parametrize("symlink", [False, True])
def test_existing_conflicting_version_is_never_replaced(tmp_path, monkeypatch, symlink):
    build, record = make_build(tmp_path)
    firmware = tmp_path / "firmware"
    firmware.mkdir()
    monkeypatch.setattr(deployment, "FIRMWARE", firmware / "siliconsign.bit.bin")
    target = firmware / record["firmware_name"]
    other = tmp_path / "other"
    other.write_bytes(b"preserve me")
    if symlink:
        target.symlink_to(other)
    else:
        target.write_bytes(b"preserve me")
    with pytest.raises(deployment.DeployError):
        deployment.install_firmware(build / "siliconsign.bit.bin", record["firmware_name"], record["image_sha256"])
    assert target.read_bytes() == b"preserve me"
    assert other.read_bytes() == b"preserve me"


def test_image_changed_after_validation_is_not_installed(tmp_path, monkeypatch):
    build, record = make_build(tmp_path)
    firmware = tmp_path / "firmware"
    firmware.mkdir()
    monkeypatch.setattr(deployment, "FIRMWARE", firmware / "siliconsign.bit.bin")
    image = build / "siliconsign.bit.bin"
    image.write_bytes(b"changed")
    with pytest.raises(deployment.DeployError, match="changed after"):
        deployment.install_firmware(image, record["firmware_name"], record["image_sha256"])
    assert list(firmware.iterdir()) == []


def test_image_changed_during_copy_is_not_installed(tmp_path, monkeypatch):
    build, record = make_build(tmp_path)
    firmware = tmp_path / "firmware"
    firmware.mkdir()
    monkeypatch.setattr(deployment, "FIRMWARE", firmware / "siliconsign.bit.bin")
    monkeypatch.setattr(deployment.shutil, "copyfileobj", lambda source, target: target.write(b"changed"))
    with pytest.raises(deployment.DeployError, match="changed while copying"):
        deployment.install_firmware(build / "siliconsign.bit.bin", record["firmware_name"], record["image_sha256"])
    assert list(firmware.iterdir()) == []


def test_quarantined_r2_rejected_even_for_legacy_package(tmp_path, monkeypatch):
    build, _ = make_build(tmp_path, name="siliconsign.bit.bin", manifest=False)
    known = next(iter(deployment.QUARANTINED_IMAGE_SHA256))
    monkeypatch.setattr(deployment, "sha256", lambda path: known)
    with pytest.raises(deployment.DeployError, match="quarantined"):
        deployment.prepare_image(build)


def test_known_applied_r2_file_is_rejected_without_mmio(tmp_path, monkeypatch):
    tree = tmp_path / "dt"
    (tree / "__symbols__").mkdir(parents=True)
    (tree / "fpga-region").mkdir()
    (tree / "__symbols__/fpga_full").write_bytes(b"/fpga-region\0")
    (tree / "fpga-region/firmware-name").write_bytes(b"siliconsign.bit.bin\0")
    image = tmp_path / "siliconsign.bit.bin"
    image.write_bytes(b"fixture")
    monkeypatch.setattr(deployment, "DT", tree)
    monkeypatch.setattr(deployment, "FIRMWARE", image)
    monkeypatch.setattr(deployment, "sha256", lambda path: next(iter(deployment.QUARANTINED_IMAGE_SHA256)))
    with pytest.raises(deployment.DeployError, match="quarantined"):
        deployment.installed_firmware_record()


def test_absent_region_metadata_does_not_guess_a_path(tmp_path, monkeypatch):
    monkeypatch.setattr(deployment, "DT", tmp_path)
    assert deployment.installed_firmware_record() == {}
