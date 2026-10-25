"""Only synthetic build files are used; these tests do not claim an FPGA build."""

import hashlib
import json
import stat
import struct
import subprocess
import sys
import tarfile
import xml.etree.ElementTree as ET
import zipfile

import pytest

from scripts import package_operator
from scripts.package_operator import BUILD_FILES, PackageError, package

# Sanitized structural schema observed in the real exported Vivado HWH. Role
# keys are deliberately absent; these fixtures never claim to contain hardware.
HANDOFF = """<SYSTEM><MODULES>
<MODULE INSTANCE="ps" MODTYPE="zynq_ultra_ps_e"><PARAMETERS>
 <PARAMETER NAME="C_MAXIGP0_DATA_WIDTH" VALUE="32"/>
 <PARAMETER NAME="PSU__MAXIGP0__DATA_WIDTH" VALUE="32"/>
 <PARAMETER NAME="PSU__USE__M_AXI_GP0" VALUE="1"/>
 <PARAMETER NAME="PSU__USE__M_AXI_GP1" VALUE="0"/>
 <PARAMETER NAME="PSU__USE__M_AXI_GP2" VALUE="0"/>
 <PARAMETER NAME="PSU__FPGA_PL0_ENABLE" VALUE="1"/>
 <PARAMETER NAME="PSU__CRL_APB__PL0_REF_CTRL__FREQMHZ" VALUE="50"/>
 <PARAMETER NAME="PSU__CRL_APB__PL0_REF_CTRL__ACT_FREQMHZ" VALUE="49.999500"/>
</PARAMETERS><PORTS>
 <PORT NAME="pl_clk0" DIR="O" SIGNAME="ps_pl_clk0"/>
 <PORT NAME="maxihpm0_fpd_aclk" DIR="I" SIGNAME="ps_pl_clk0"/>
 <PORT NAME="pl_resetn0" DIR="O" SIGNAME="ps_pl_resetn0"/>
</PORTS></MODULE>
<MODULE INSTANCE="reset" MODTYPE="proc_sys_reset"><PARAMETERS>
 <PARAMETER NAME="C_AUX_RESET_HIGH" VALUE="0"/>
 <PARAMETER NAME="C_EXT_RESET_HIGH" VALUE="0"/>
</PARAMETERS><PORTS>
 <PORT NAME="slowest_sync_clk" DIR="I" SIGNAME="ps_pl_clk0"/>
 <PORT NAME="ext_reset_in" DIR="I" SIGNAME="ps_pl_resetn0"/>
 <PORT NAME="aux_reset_in" DIR="I" SIGNAME="one_dout"/>
 <PORT NAME="dcm_locked" DIR="I" SIGNAME="one_dout"/>
 <PORT NAME="mb_debug_sys_rst" DIR="I" SIGNAME="zero_dout"/>
 <PORT NAME="peripheral_aresetn" DIR="O" SIGNAME="reset_peripheral_aresetn"/>
</PORTS></MODULE>
<MODULE INSTANCE="payment_gate" MODTYPE="siliconsign_axi"><PARAMETERS>
 <PARAMETER NAME="NV" VALUE="64"/><PARAMETER NAME="NI" VALUE="256"/>
 <PARAMETER NAME="CLK_HZ" VALUE="50000000"/>
</PARAMETERS><PORTS>
 <PORT NAME="s_axi_aclk" DIR="I" SIGNAME="ps_pl_clk0"/>
 <PORT NAME="s_axi_aresetn" DIR="I" SIGNAME="reset_peripheral_aresetn"/>
</PORTS></MODULE>
<MODULE INSTANCE="interconnect" MODTYPE="smartconnect"><PARAMETERS>
 <PARAMETER NAME="NUM_SI" VALUE="1"/><PARAMETER NAME="NUM_MI" VALUE="1"/>
</PARAMETERS><PORTS>
 <PORT NAME="aclk" DIR="I" SIGNAME="ps_pl_clk0"/>
 <PORT NAME="aresetn" DIR="I" SIGNAME="reset_peripheral_aresetn"/>
</PORTS></MODULE>
<MODULE INSTANCE="one" MODTYPE="xlconstant"><PARAMETERS>
 <PARAMETER NAME="CONST_VAL" VALUE="0x1"/><PARAMETER NAME="CONST_WIDTH" VALUE="1"/>
</PARAMETERS><PORTS><PORT NAME="dout" DIR="O" SIGNAME="one_dout"/></PORTS></MODULE>
<MODULE INSTANCE="zero" MODTYPE="xlconstant"><PARAMETERS>
 <PARAMETER NAME="CONST_VAL" VALUE="0x0"/><PARAMETER NAME="CONST_WIDTH" VALUE="1"/>
</PARAMETERS><PORTS><PORT NAME="dout" DIR="O" SIGNAME="zero_dout"/></PORTS></MODULE>
</MODULES><MEMORYMAP>
 <MEMRANGE INSTANCE="payment_gate" MASTERBUSINTERFACE="M_AXI_HPM0_FPD"
 SLAVEBUSINTERFACE="S_AXI" BASEVALUE="0xA0000000" HIGHVALUE="0xA0000FFF" MEMTYPE="REGISTER"/>
</MEMORYMAP></SYSTEM>"""


def write_handoff(build, xml=HANDOFF):
    with zipfile.ZipFile(build / "siliconsign.xsa", "w") as archive:
        archive.writestr("siliconsign_bd.hwh", xml)
        # An XSA also has a SmartConnect sub-handoff; it is not the top-level design.
        archive.writestr("siliconsign_bd_interconnect_0.hwh", "<SYSTEM/>")


def compiled_overlay(name="siliconsign.bit.bin"):
    """Minimal FDT with the real firmware-name property location."""

    def node(value):
        data = value.encode() + b"\0"
        return struct.pack(">I", 1) + data + b"\0" * (-len(data) % 4)

    value = name.encode() + b"\0"
    prop = struct.pack(">III", 3, len(value), 0) + value + b"\0" * (-len(value) % 4)
    body = node("") + node("fragment@0") + node("__overlay__") + prop
    body += struct.pack(">4I", 2, 2, 2, 9)
    strings = b"firmware-name\0"
    string_offset = 56 + len(body)
    header = struct.pack(
        ">10I",
        0xD00DFEED,
        string_offset + len(strings),
        56,
        string_offset,
        40,
        17,
        16,
        0,
        len(strings),
        len(body),
    )
    return header + bytes(16) + body + strings


def versioned_deployment(build):
    payload = compiled_overlay("siliconsign-r5.bit.bin")
    (build / "siliconsign.dtbo").write_bytes(payload)
    manifest = {
        "schema": "siliconsign.deployment.v1",
        "firmware_name": "siliconsign-r5.bit.bin",
        "image_sha256": hashlib.sha256((build / "siliconsign.bit.bin").read_bytes()).hexdigest(),
        "overlay_sha256": hashlib.sha256(payload).hexdigest(),
    }
    (build / "deployment.json").write_text(json.dumps(manifest))
    return manifest


@pytest.fixture
def inputs(tmp_path):
    build = tmp_path / "synthetic-build"
    build.mkdir()
    for name in BUILD_FILES:
        (build / name).write_text("Synthetic packaging test fixture; not a Vivado artifact.\n")
    (build / "siliconsign.bit.bin").write_bytes(b"\x00" * 1024)
    (build / "siliconsign.dtbo").write_bytes(compiled_overlay())
    write_handoff(build)
    (build / "capabilities.json").write_text(
        json.dumps(
            {
                "vendor_slots": 64,
                "obligation_slots": 256,
                "register": "0x00c",
                "register_value": "0x01000040",
                "clock_register": "0x010",
                "clock_divider_hz": 50000000,
                "part": "xck26-sfvc784-2LV-c",
            }
        )
    )
    (build / "timing_summary.rpt").write_text(
        "TEST FIXTURE ONLY\nAll user specified timing constraints are met.\n"
    )
    values = {"admin": "11" * 32, "controller": "22" * 32, "response": "33" * 32}
    keys = tmp_path / "synthetic-keys.json"
    keys.write_text(json.dumps(values))
    keys.chmod(0o600)
    (build / ".generated").mkdir()
    header = build / ".generated" / "siliconsign_keys.vh"
    header.write_text(
        "\n".join(
            f"`define SILICONSIGN_{role.upper()}_KEY 256'h{value}" for role, value in values.items()
        )
    )
    header.chmod(0o600)
    return build, keys


def test_packages_private_archives_with_response_only_board_role(inputs, tmp_path):
    build, keys = inputs
    output = tmp_path / "private-package"
    result = package(build, keys, output)
    assert result["physical_board_verified"] is False
    assert stat.S_IMODE(output.stat().st_mode) == 0o700
    for archive_name in ("payguard-operator.tar.gz", "payguard-kv260-board.tar.gz"):
        assert stat.S_IMODE((output / archive_name).stat().st_mode) == 0o600
    board = output / "payguard-kv260-board.tar.gz"
    with tarfile.open(output / "payguard-operator.tar.gz") as operator:
        assert "payguard-operator/transfer/uart_upload.py" in operator.getnames()
        assert "payguard-operator/transfer/uart_receiver.py" in operator.getnames()
    with tarfile.open(board) as archive:
        names = archive.getnames()
        private = [n for n in names if "/private/" in n and not archive.getmember(n).isdir()]
        assert private == ["payguard-kv260/private/response.json"]
        role = json.load(archive.extractfile(private[0]))
        assert set(role) == {"response"}
        assert not any(".generated" in n for n in names)
        assert not any(n.endswith((".xsa", ".hwh")) for n in names)
        assert "payguard-kv260/hardware/python/siliconsign/external_bridge.py" in names
        assert "payguard-kv260/hardware/scripts/uart_console.py" in names
        assert all(member.mode in (0o600, 0o700) for member in archive)
        extracted = tmp_path / "extracted"
        archive.extractall(extracted, filter="data")
    check = subprocess.run(
        [sys.executable, str(extracted / "payguard-kv260" / "verify_package.py")],
        capture_output=True,
        text=True,
    )
    assert check.returncode == 0, check.stderr
    assert "not been performed" in check.stdout
    (extracted / "payguard-kv260" / "private" / "response.json").write_text("tampered")
    check = subprocess.run(
        [sys.executable, str(extracted / "payguard-kv260" / "verify_package.py")],
        capture_output=True,
        text=True,
    )
    assert check.returncode != 0 and "verification failed" in check.stderr


@pytest.mark.parametrize(
    "fault", ["old_capacity", "old_clock", "failed_timing", "wrong_keys", "no_overlay"]
)
def test_rejects_incompatible_or_incomplete_inputs_before_creating_output(inputs, tmp_path, fault):
    build, keys = inputs
    if fault == "old_capacity":
        (build / "capabilities.json").write_text('{"vendor_slots":16,"obligation_slots":64}')
    elif fault == "old_clock":
        data = json.loads((build / "capabilities.json").read_text())
        data["clock_divider_hz"] = 100000000
        (build / "capabilities.json").write_text(json.dumps(data))
    elif fault == "failed_timing":
        (build / "timing_summary.rpt").write_text("Timing constraints are not met.")
    elif fault == "wrong_keys":
        data = json.loads(keys.read_text())
        data["admin"] = "44" * 32
        keys.write_text(json.dumps(data))
    else:
        (build / "siliconsign.dtbo").unlink()
    output = tmp_path / "not-created"
    with pytest.raises(PackageError):
        package(build, keys, output)
    assert not output.exists()


def test_never_overwrites_an_existing_destination(inputs, tmp_path):
    build, keys = inputs
    output = tmp_path / "existing"
    output.mkdir()
    sentinel = output / "keep"
    sentinel.write_text("existing project")
    with pytest.raises(PackageError, match="already exists"):
        package(build, keys, output)
    assert sentinel.read_text() == "existing project"


@pytest.mark.parametrize(
    ("xpath", "attribute", "bad_value"),
    [
        # Exact implemented R2 fault: active-low auxiliary reset tied permanently low.
        (".//MODULE[@INSTANCE='reset']/PORTS/PORT[@NAME='aux_reset_in']", "SIGNAME", "zero_dout"),
        (".//PARAMETER[@NAME='C_AUX_RESET_HIGH']", "VALUE", "1"),
        (".//PARAMETER[@NAME='C_EXT_RESET_HIGH']", "VALUE", "1"),
        (".//MODULE[@INSTANCE='reset']/PORTS/PORT[@NAME='ext_reset_in']", "SIGNAME", "one_dout"),
        (
            ".//MODULE[@INSTANCE='reset']/PORTS/PORT[@NAME='mb_debug_sys_rst']",
            "SIGNAME",
            "one_dout",
        ),
        (".//MODULE[@INSTANCE='reset']/PORTS/PORT[@NAME='dcm_locked']", "SIGNAME", "zero_dout"),
        (".//MODULE[@INSTANCE='one']/PARAMETERS/PARAMETER[@NAME='CONST_VAL']", "VALUE", "0x0"),
        (".//MODULE[@INSTANCE='one']/PARAMETERS/PARAMETER[@NAME='CONST_WIDTH']", "VALUE", "32"),
        (".//MODULE[@INSTANCE='zero']/PARAMETERS/PARAMETER[@NAME='CONST_VAL']", "VALUE", "0x1"),
        (
            ".//MODULE[@INSTANCE='payment_gate']/PORTS/PORT[@NAME='s_axi_aresetn']",
            "SIGNAME",
            "one_dout",
        ),
        (
            ".//MODULE[@INSTANCE='payment_gate']/PORTS/PORT[@NAME='s_axi_aclk']",
            "SIGNAME",
            "ps_pl_clk1",
        ),
        (
            ".//MODULE[@INSTANCE='ps']/PORTS/PORT[@NAME='maxihpm0_fpd_aclk']",
            "SIGNAME",
            "ps_pl_clk1",
        ),
        (".//PARAMETER[@NAME='C_MAXIGP0_DATA_WIDTH']", "VALUE", "128"),
        (".//PARAMETER[@NAME='PSU__MAXIGP0__DATA_WIDTH']", "VALUE", "128"),
        (".//PARAMETER[@NAME='PSU__USE__M_AXI_GP1']", "VALUE", "1"),
        (".//PARAMETER[@NAME='PSU__CRL_APB__PL0_REF_CTRL__FREQMHZ']", "VALUE", "100"),
        (".//PARAMETER[@NAME='PSU__CRL_APB__PL0_REF_CTRL__ACT_FREQMHZ']", "VALUE", "100"),
        (".//PARAMETER[@NAME='PSU__CRL_APB__PL0_REF_CTRL__ACT_FREQMHZ']", "VALUE", "NaN"),
        (".//PARAMETER[@NAME='CLK_HZ']", "VALUE", "100000000"),
        (".//PARAMETER[@NAME='NV']", "VALUE", "16"),
        (".//PARAMETER[@NAME='NI']", "VALUE", "64"),
        (".//MEMRANGE", "BASEVALUE", "0xB0000000"),
        (".//MEMRANGE", "HIGHVALUE", "0xA000FFFF"),
        (".//MEMRANGE", "MASTERBUSINTERFACE", "M_AXI_HPM1_FPD"),
    ],
)
def test_actual_handoff_contract_rejects_fault_before_creating_archive(
    inputs, tmp_path, xpath, attribute, bad_value
):
    build, keys = inputs
    root = ET.fromstring(HANDOFF)
    root.find(xpath).set(attribute, bad_value)
    write_handoff(build, ET.tostring(root))
    output = tmp_path / "not-created"
    with pytest.raises(PackageError, match="Hardware handoff"):
        package(build, keys, output)
    assert not output.exists()


@pytest.mark.parametrize(
    "fault",
    ["missing", "invalid_zip", "invalid_xml", "duplicate", "missing_module", "symlink", "entity"],
)
def test_malformed_or_ambiguous_xsa_fails_closed_without_printing_private_xml(
    inputs, tmp_path, fault
):
    build, keys = inputs
    secret = "PRIVATE-HWH-KEY-MUST-NEVER-APPEAR-IN-DIAGNOSTICS"
    path = build / "siliconsign.xsa"
    if fault == "missing":
        path.unlink()
    elif fault == "invalid_zip":
        path.write_text(secret)
    elif fault == "invalid_xml":
        write_handoff(build, "<SYSTEM " + secret)
    elif fault == "duplicate":
        with zipfile.ZipFile(path, "a") as archive:
            archive.writestr("other/siliconsign_bd.hwh", HANDOFF)
    elif fault == "missing_module":
        write_handoff(build, HANDOFF.replace('INSTANCE="reset"', 'INSTANCE="wrong"'))
    elif fault == "symlink":
        target = build / "actual.xsa"
        path.rename(target)
        path.symlink_to(target)
    else:
        write_handoff(build, '<!DOCTYPE SYSTEM [<!ENTITY secret "' + secret + '">]>' + HANDOFF)
    with pytest.raises(PackageError) as error:
        package(build, keys, tmp_path / "not-created")
    assert secret not in str(error.value)
    assert not (tmp_path / "not-created").exists()


def test_quarantined_image_rejected_even_with_good_handoff(inputs, tmp_path, monkeypatch):
    build, keys = inputs
    assert (
        "7a25f51df561d92904ad812b4d3d01c5a4893416cf77886f5c895273575e3501"
        in package_operator.QUARANTINED_IMAGE_SHA256
    )
    fixture_digest = hashlib.sha256((build / "siliconsign.bit.bin").read_bytes()).hexdigest()
    monkeypatch.setattr(
        package_operator,
        "QUARANTINED_IMAGE_SHA256",
        package_operator.QUARANTINED_IMAGE_SHA256 | {fixture_digest},
    )
    with pytest.raises(PackageError, match="quarantined"):
        package(build, keys, tmp_path / "not-created")
    assert not (tmp_path / "not-created").exists()


def test_versioned_deployment_manifest_is_verified_and_included(inputs, tmp_path):
    build, keys = inputs
    expected = versioned_deployment(build)
    output = tmp_path / "versioned-package"
    package(build, keys, output)
    with tarfile.open(output / "payguard-kv260-board.tar.gz") as archive:
        assert json.load(archive.extractfile("payguard-kv260/build/deployment.json")) == expected
        assert "payguard-kv260/build/siliconsign.bit.bin" in archive.getnames()
        assert not any(name.endswith(".xsa") for name in archive.getnames())


@pytest.mark.parametrize("fault", ["missing", "image", "overlay", "name", "malformed", "symlink"])
def test_versioned_deployment_mismatch_rejected_before_packaging(inputs, tmp_path, fault):
    build, keys = inputs
    manifest = versioned_deployment(build)
    path = build / "deployment.json"
    if fault == "missing":
        path.unlink()
    elif fault == "image":
        (build / "siliconsign.bit.bin").write_bytes(b"X" * 1024)
    elif fault == "overlay":
        manifest["overlay_sha256"] = "0" * 64
        path.write_text(json.dumps(manifest))
    elif fault == "name":
        manifest["firmware_name"] = "siliconsign-r6.bit.bin"
        path.write_text(json.dumps(manifest))
    elif fault == "malformed":
        path.write_text('{"schema":"wrong"}')
    else:
        other = build / "actual-deployment.json"
        path.rename(other)
        path.symlink_to(other)
    with pytest.raises(PackageError, match="deployment manifest"):
        package(build, keys, tmp_path / "not-created")
    assert not (tmp_path / "not-created").exists()
