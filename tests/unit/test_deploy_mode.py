"""Full-image loader guards using temporary files, never physical FPGA access."""

from __future__ import annotations

import importlib.util
from pathlib import Path
from types import SimpleNamespace

import pytest

HELPER = Path(__file__).resolve().parents[2] / "hardware/scripts/deploy_kv260.py"
spec = importlib.util.spec_from_file_location("deploy_mode", HELPER)
deployment = importlib.util.module_from_spec(spec)
spec.loader.exec_module(deployment)


@pytest.fixture
def kernel(tmp_path, monkeypatch):
    manager = tmp_path / "manager"
    manager.mkdir()
    flags = manager / "flags"
    flags.write_text("20\n")
    (manager / "state").write_text("operating\n")
    taint = tmp_path / "tainted"
    taint.write_text("0\n")
    overlays = tmp_path / "overlays"
    overlays.mkdir()
    monkeypatch.setattr(deployment, "MANAGER", manager)
    monkeypatch.setattr(deployment, "KERNEL_TAINT", taint)
    monkeypatch.setattr(deployment, "OVERLAYS", overlays)
    return SimpleNamespace(
        root=tmp_path, manager=manager, flags=flags, taint=taint, overlays=overlays
    )


@pytest.fixture
def fake_load(kernel, monkeypatch):
    calls = []
    monkeypatch.setattr(deployment, "inspection", lambda: {})
    monkeypatch.setattr(deployment, "require_board", lambda info: None)
    monkeypatch.setattr(deployment, "require_platform_drivers", lambda: None)
    monkeypatch.setattr(deployment.os, "geteuid", lambda: 0)
    monkeypatch.setattr(deployment, "ensure_mount", lambda *args: calls.append("mount"))
    monkeypatch.setattr(deployment, "read_clock", lambda: 49_999_501)
    monkeypatch.setattr(
        deployment,
        "fabric_clock_diagnostics",
        lambda: {
            "pl0_ref": {"enable_count": 1, "prepare_count": 1, "rate_hz": 49_999_501},
            "fabric_clock_binding": {
                "device": "axi:siliconsign_clocking0",
                "compatible": "xlnx,fclk",
                "driver": "xilinx_fclk",
            },
            "afi": {
                "binding": {"driver": "afi-fpga", "compatible": "xlnx,afi-fpga"},
                "config": [14, 0],
                "resets": [7, 116, 7, 117, 7, 118, 7, 119],
                "expected_reset_phandle": 7,
            },
        },
    )

    def prepare(build):
        calls.append("prepare")
        return (kernel.root / "fixture.bin", b"synthetic overlay fixture",
                "siliconsign.bit.bin", "fixture-digest")

    def install(image, name, expected_digest):
        assert kernel.flags.read_text() == "0\n"
        assert name == "siliconsign.bit.bin" and expected_digest == "fixture-digest"
        calls.append("install")
        return "fixture-digest"

    original_text = deployment.optional_text

    def text(path):
        if path == kernel.overlays / "siliconsign/status" and (path.parent / "dtbo").exists():
            return "applied"
        return original_text(path)

    def verify():
        assert (kernel.overlays / "siliconsign/dtbo").read_bytes() == b"synthetic overlay fixture"
        calls.append("verify")
        return {"ip_id": "0x53494c31", "keys_valid": True}

    monkeypatch.setattr(deployment, "prepare_image", prepare)
    monkeypatch.setattr(deployment, "install_firmware", install)
    monkeypatch.setattr(deployment, "optional_text", text)
    monkeypatch.setattr(deployment, "verify_ip", verify)
    return calls


def test_stale_dma_mode_is_cleared_before_image_install_and_overlay(kernel, fake_load):
    result = deployment.deploy(kernel.root, replace_unmanaged=True, load_only=False)
    assert result["previous_fpga_manager_flags"] == 0x20
    assert result["fpga_manager_flags"] == 0
    assert result["kernel_taint"] == 0
    assert result["payment_bridge_started"] is False
    assert fake_load[-3:] == ["prepare", "install", "verify"]


@pytest.mark.parametrize("value", [None, "", "not-hex", "-1", "0\x00", "20 0"])
def test_missing_or_malformed_flags_cannot_install_firmware_or_overlay(kernel, fake_load, value):
    if value is None:
        kernel.flags.unlink()
    else:
        kernel.flags.write_text(value)
    with pytest.raises(deployment.DeployError, match="flags are missing, unreadable or malformed"):
        deployment.deploy(kernel.root, replace_unmanaged=True, load_only=False)
    assert "install" not in fake_load
    assert list(kernel.overlays.iterdir()) == []


def test_readonly_flags_fail_before_image_or_overlay(kernel, fake_load, monkeypatch):
    original_open = Path.open

    def open_file(path, mode="r", *args, **kwargs):
        if path == kernel.flags and mode == "w":
            raise PermissionError("synthetic read-only sysfs")
        return original_open(path, mode, *args, **kwargs)

    monkeypatch.setattr(Path, "open", open_file)
    with pytest.raises(deployment.DeployError, match="Cannot set FPGA manager flags"):
        deployment.deploy(kernel.root, replace_unmanaged=True, load_only=False)
    assert kernel.flags.read_text() == "20\n"
    assert "install" not in fake_load
    assert list(kernel.overlays.iterdir()) == []


@pytest.mark.parametrize("readback", [0x20, None])
def test_flags_must_read_back_zero_before_image_or_overlay(
    kernel, fake_load, monkeypatch, readback
):
    original_read = deployment.read_unsigned
    count = 0

    def read(path, base):
        nonlocal count
        if path == kernel.flags:
            count += 1
            if count > 1:
                return readback
        return original_read(path, base)

    monkeypatch.setattr(deployment, "read_unsigned", read)
    with pytest.raises(deployment.DeployError, match="did not read back as zero"):
        deployment.deploy(kernel.root, replace_unmanaged=True, load_only=False)
    assert "install" not in fake_load
    assert list(kernel.overlays.iterdir()) == []


@pytest.mark.parametrize("value", ["128", "640", "bad", "-1", "", None])
def test_oops_or_unknown_taint_stops_before_any_deployment_mutation(kernel, fake_load, value):
    if value is None:
        kernel.taint.unlink()
    else:
        kernel.taint.write_text(value)
    with pytest.raises(deployment.DeployError, match="OOPS"):
        deployment.deploy(kernel.root, replace_unmanaged=True, load_only=False)
    assert kernel.flags.read_text() == "20\n"
    assert fake_load == []


def test_warning_and_external_module_taints_alone_do_not_block(kernel, fake_load):
    kernel.taint.write_text(str(512 | 4096))
    result = deployment.deploy(kernel.root, replace_unmanaged=True, load_only=False)
    assert result["kernel_taint"] == 512 | 4096
    assert fake_load[-1] == "verify"


def test_existing_overlay_is_never_unloaded_or_manager_flags_changed(kernel, fake_load):
    previous = kernel.overlays / "previous-application"
    previous.mkdir()
    (previous / "status").write_text("applied")
    with pytest.raises(deployment.DeployError, match="Existing overlays"):
        deployment.deploy(kernel.root, replace_unmanaged=True, load_only=False)
    assert previous.is_dir()
    assert kernel.flags.read_text() == "20\n"
    assert "prepare" not in fake_load


def test_readonly_inspection_exposes_flags_and_taint_without_changing_them(kernel):
    info = deployment.inspection()
    assert info["fpga_manager_flags"] == 32  # sysfs reports hex 20
    assert info["kernel_taint"] == 0
    assert kernel.flags.read_text() == "20\n"


def test_load_only_never_probes_ip_and_never_reports_verified(
    kernel, fake_load, monkeypatch, capsys
):
    result = deployment.deploy(kernel.root, replace_unmanaged=True, load_only=True)
    assert "verify" not in fake_load
    assert result["ip_verified"] is False
    assert result["payment_bridge_started"] is False
    assert result["mode"] == "LOADED BUT UNVERIFIED; no PL MMIO performed"
    output = capsys.readouterr()
    assert "begin overlay apply" in output.err
    assert "overlay apply returned" in output.err
    assert "load-only complete: UNVERIFIED" in output.err
    assert "begin PL MMIO probe" not in output.err


@pytest.mark.parametrize(
    "fault", ["disabled", "unprepared", "no_counts", "no_driver", "wrong_driver", "wrong_rate"]
)
def test_unknown_or_disabled_fabric_clock_prevents_mmio(kernel, fake_load, monkeypatch, fault):
    diagnostics = deployment.fabric_clock_diagnostics()
    if fault == "disabled":
        diagnostics["pl0_ref"]["enable_count"] = 0
    elif fault == "unprepared":
        diagnostics["pl0_ref"]["prepare_count"] = 0
    elif fault == "no_counts":
        diagnostics["pl0_ref"] = None
    elif fault == "no_driver":
        diagnostics["fabric_clock_binding"] = None
    elif fault == "wrong_driver":
        diagnostics["fabric_clock_binding"]["driver"] = "unrelated_driver"
    else:
        diagnostics["pl0_ref"]["rate_hz"] = 100_000_000
    monkeypatch.setattr(deployment, "fabric_clock_diagnostics", lambda: diagnostics)
    with pytest.raises(deployment.DeployError, match="refusing PL MMIO probe"):
        deployment.deploy(kernel.root, replace_unmanaged=True, load_only=False)
    assert "verify" not in fake_load
    assert (kernel.overlays / "siliconsign/dtbo").exists()


@pytest.mark.parametrize("protect", [False, True])
def test_clock_summary_counts_support_both_kernel_formats(protect):
    header = (
        "clock count count count rate accuracy phase"
        if protect
        else "clock count count rate accuracy phase"
    )
    values = "pl0_ref 2 3 0 49999501 0 0" if protect else "pl0_ref 2 3 49999501 0 0"
    assert deployment.clock_counts_from_summary(header + "\n" + values) == {
        "enable_count": 2,
        "prepare_count": 3,
        "rate_hz": 49_999_501,
    }


@pytest.mark.parametrize(
    "summary",
    [
        "",
        "pl0_ref 1 1 50000000",
        "clock count count rate\npl0_ref 1",
        "clock count count rate\npl0_ref -1 1 50000000",
    ],
)
def test_unparseable_clock_summary_is_unknown(summary):
    assert deployment.clock_counts_from_summary(summary) is None


def test_load_only_cannot_be_combined_with_hidden_mmio_probe(monkeypatch):
    def forbidden():
        pytest.fail("MMIO probe must not run in load-only mode")

    monkeypatch.setattr(deployment, "read_ip_child", forbidden)
    with pytest.raises(SystemExit) as error:
        deployment.main(["--load-only", "--_read-ip"])
    assert error.value.code == 2


def test_verify_existing_probes_once_without_reloading_or_rewriting_flags(kernel, fake_load):
    deployment.deploy(kernel.root, replace_unmanaged=True)
    before = list(fake_load)
    result = deployment.verify_existing()
    assert result["ip_verified"] is True
    assert result["image_reloaded"] is False
    assert fake_load == [*before, "verify"]


def test_known_quarantined_firmware_stops_existing_probe(kernel, fake_load, monkeypatch):
    deployment.deploy(kernel.root, replace_unmanaged=True)

    def quarantined():
        deployment.require_supported_image_digest(next(iter(deployment.QUARANTINED_IMAGE_SHA256)))

    monkeypatch.setattr(deployment, "installed_firmware_record", quarantined)
    with pytest.raises(deployment.DeployError, match="quarantined"):
        deployment.verify_existing()
    assert "verify" not in fake_load


def test_versioned_load_reports_filename_without_mmio(kernel, fake_load, monkeypatch):
    monkeypatch.setattr(deployment, "prepare_image", lambda build: (
        kernel.root / "fixture.bin", b"synthetic overlay fixture", "siliconsign-r5.bit.bin", "r5-digest"
    ))

    def install(image, name, expected_digest):
        assert name == "siliconsign-r5.bit.bin" and expected_digest == "r5-digest"
        assert kernel.flags.read_text() == "0\n"
        return expected_digest

    monkeypatch.setattr(deployment, "install_firmware", install)
    result = deployment.deploy(kernel.root, replace_unmanaged=True)
    assert result["firmware_name"] == "siliconsign-r5.bit.bin"
    assert result["firmware_sha256"] == "r5-digest"
    assert result["ip_verified"] is False
    assert "verify" not in fake_load


@pytest.mark.parametrize(
    "fault",
    ["driver", "width", "reset", "controller", "absent", "old_reset116_only", "missing_reset119"],
)
def test_afi_reset_guards_stop_load_only_and_existing_probe(kernel, fake_load, monkeypatch, fault):
    diagnostics = deployment.fabric_clock_diagnostics()
    afi = diagnostics["afi"]
    if fault == "driver":
        afi["binding"]["driver"] = "unrelated"
    elif fault == "width":
        afi["config"] = [14, 256]
    elif fault == "reset":
        afi["resets"] = [7, 117]
    elif fault == "controller":
        afi["resets"] = [7, 116, 7, 117, 7, 118, 9, 119]
    elif fault == "old_reset116_only":
        afi["resets"] = [7, 116]
    elif fault == "missing_reset119":
        afi["resets"] = [7, 116, 7, 117, 7, 118]
    else:
        diagnostics["afi"] = None
    monkeypatch.setattr(deployment, "fabric_clock_diagnostics", lambda: diagnostics)
    with pytest.raises(deployment.DeployError, match="AFI"):
        deployment.deploy(kernel.root, replace_unmanaged=True)
    with pytest.raises(deployment.DeployError, match="AFI"):
        deployment.verify_existing()
    assert "verify" not in fake_load


def test_all_four_actual_controller_reset_references_allow_staged_verification(kernel, fake_load):
    result = deployment.deploy(kernel.root, replace_unmanaged=True)
    assert result["fabric_clock_diagnostics"]["afi"]["resets"] == [7, 116, 7, 117, 7, 118, 7, 119]
    assert result["ip_verified"] is False
    assert "verify" not in fake_load
    verified = deployment.verify_existing()
    assert verified["ip_verified"] is True
    assert fake_load.count("verify") == 1


def test_unregistered_platform_driver_is_not_assumed_available(kernel, monkeypatch):
    monkeypatch.setattr(
        deployment, "required_drivers", lambda: {"xilinx_fclk": True, "afi-fpga": False}
    )
    with pytest.raises(deployment.DeployError, match="afi-fpga"):
        deployment.require_platform_drivers()
    assert kernel.flags.read_text() == "20\n"


def test_default_cli_load_is_no_mmio_and_verify_existing_is_separate(kernel, monkeypatch, capsys):
    calls = []

    def load(build, replace, *, load_only):
        calls.append((build, replace, load_only))
        return {"ip_verified": False}

    monkeypatch.setattr(deployment, "deploy", load)
    assert deployment.main(["--build-dir", str(kernel.root)]) == 0
    assert calls == [(kernel.root, False, True)]
    assert "UNVERIFIED" in capsys.readouterr().out
    with pytest.raises(SystemExit) as error:
        deployment.main(["--verify-existing", "--build-dir", str(kernel.root)])
    assert error.value.code == 2
