"""Recovery installer tests use temporary files and mocked board functions only."""

import base64
import gzip
import hashlib
import json
import stat
import subprocess
import sys
from pathlib import Path

import pytest

from scripts.package_recovery import INSTALLER, build_artifact


def payload(files):
    records = {
        name: {
            "bytes": len(data),
            "sha256": hashlib.sha256(data).hexdigest(),
            "data": base64.b64encode(data).decode(),
        }
        for name, data in files.items()
    }
    packed = gzip.compress(json.dumps(records).encode())
    return base64.b64encode(packed).decode(), hashlib.sha256(packed).hexdigest()


@pytest.fixture
def installer(tmp_path, monkeypatch):
    namespace = {"__name__": "test_recovery_installer"}
    exec(INSTALLER.split("\ntry:\n    run(PAYLOAD", 1)[0], namespace)
    namespace["TARGET"] = tmp_path / "recovery"
    namespace["ORIGINAL"] = tmp_path / "original"
    original = namespace["ORIGINAL"]
    (original / "build").mkdir(parents=True, mode=0o700)
    image = b"existing private image; never uploaded" * 128
    for name, data in {
        "build/siliconsign.bit.bin": image,
        "manifest.json": json.dumps(
            {
                "files": {
                    "build/siliconsign.bit.bin": {
                        "bytes": len(image),
                        "sha256": hashlib.sha256(image).hexdigest(),
                    }
                }
            }
        ).encode(),
    }.items():
        path = original / name
        path.write_bytes(data)
        path.chmod(0o600)
    operations = []

    def command(args, **kwargs):
        operations.append(args)
        assert args == ["xmutil", "unloadapp"]
        sys.modules["payguard_recovery_deploy"].overlays = []

    monkeypatch.setattr(subprocess, "run", command)
    return namespace, operations, image


def fake_files(overlays):
    helper = f"""overlays = {overlays!r}
calls = []
def inspection(): return {{"mode": "mock board, no hardware"}}
def require_board(info): calls.append("board")
def require_kernel_ready(): calls.append("kernel")
def active_overlays(): return overlays
def required_drivers(): return {{"xilinx_fclk": True, "afi-fpga": True}}
def require_platform_drivers(): calls.append("drivers")
def deploy(build, replace_unmanaged, *, load_only):
    assert replace_unmanaged is True and load_only is True
    calls.append("load_only")
    return {{"mode": "MOCK UNVERIFIED", "ip_verified": False}}
"""
    return {
        "deploy_kv260.py": helper.encode(),
        "build/siliconsign.dts": b"dts",
        "build/siliconsign.dtbo": b"dtbo",
        "manifest.json": b"{}",
    }


def test_public_artifact_is_bounded_and_exactly_current():
    artifact = build_artifact()
    assert max(map(len, artifact.splitlines())) <= 76
    assert artifact.startswith("sudo python3 - <<'PAYGUARD_RECOVERY_R4'")
    assert 'TARGET = Path("/opt/payguard-recovery-r4")' in artifact
    namespace = {}
    declarations = artifact.split("\n", 1)[1].split("import base64", 1)[0]
    exec(declarations, namespace)
    packed = base64.b64decode(namespace["PAYLOAD"])
    assert hashlib.sha256(packed).hexdigest() == namespace["PAYLOAD_SHA256"]
    records = json.loads(gzip.decompress(packed))
    assert set(records) == {
        "deploy_kv260.py",
        "build/siliconsign.dts",
        "build/siliconsign.dtbo",
        "manifest.json",
    }
    assert "siliconsign.bit.bin" not in records and "keys.json" not in records
    source_root = Path(__file__).resolve().parents[2]
    for name, record in records.items():
        expected_path = (
            source_root / "hardware/scripts/deploy_kv260.py"
            if name == "deploy_kv260.py"
            else source_root / "hardware/recovery/r4" / Path(name).name
        )
        data = (
            record["data"].encode("utf-8")
            if record["encoding"] == "utf-8"
            else base64.b64decode(record["data"])
        )
        assert data == expected_path.read_bytes()
        assert len(data) == record["bytes"]
        assert hashlib.sha256(data).hexdigest() == record["sha256"]


def test_r3_historical_paste_is_preserved_byte_for_byte():
    previous = Path(__file__).resolve().parents[2] / "hardware/recovery/r3/PayGuard-KV260-r3.txt"
    assert hashlib.sha256(previous.read_bytes()).hexdigest() == (
        "7df660bdf43a2e2510c7e58f343c6579959ccbfd623f8dffdfe5158178eab51f"
    )


def test_tampered_payload_stops_before_directory_creation(installer):
    namespace, operations, _ = installer
    encoded, _digest = payload(fake_files([]))
    with pytest.raises(RuntimeError, match="payload SHA-256"):
        namespace["run"](encoded, "0" * 64)
    assert not namespace["TARGET"].exists() and not operations


def test_existing_siliconsign_refused_before_mutations(installer):
    namespace, operations, _ = installer
    encoded, digest = payload(fake_files([{"name": "siliconsign", "status": "applied"}]))
    with pytest.raises(RuntimeError, match="Unexpected overlay"):
        namespace["run"](encoded, digest)
    assert not namespace["TARGET"].exists() and not operations


def test_private_install_reuses_image_and_loads_only_once(installer):
    namespace, operations, image = installer
    starter = [{"name": "k26-starter-kits_image_1", "status": "applied"}]
    encoded, digest = payload(fake_files(starter))
    namespace["run"](encoded, digest)
    target = namespace["TARGET"]
    assert (target / "build/siliconsign.bit.bin").read_bytes() == image
    assert operations == [["xmutil", "unloadapp"]]
    assert sys.modules["payguard_recovery_deploy"].calls[-1] == "load_only"
    assert stat.S_IMODE(target.stat().st_mode) == 0o700
    assert all(
        stat.S_IMODE(path.stat().st_mode) == 0o600 for path in target.rglob("*") if path.is_file()
    )
    namespace["run"](encoded, digest)
    assert operations == [["xmutil", "unloadapp"]]
    assert "load_only" not in sys.modules["payguard_recovery_deploy"].calls
    (target / "build/siliconsign.dts").write_bytes(b"changed")
    with pytest.raises(RuntimeError, match="differs"):
        namespace["run"](encoded, digest)
