"""Package a completed KV260 build without deploying it or changing any keys.

Produces separate owner-only operator and board archives. The board archive has
only the response role key; the operator archive contains all existing roles.
Inputs are trusted local build outputs. Checks here do not prove physical-board
behavior or authenticate the provenance of an arbitrary supplied bitstream.
"""

# ruff: noqa: T201 -- CLI prints paths and archive digests, never private material.
from __future__ import annotations

import argparse
import hashlib
import importlib.util
import io
import json
import os
import re
import stat
import sys
import tarfile
import xml.etree.ElementTree as ET
import zipfile
from decimal import Decimal, InvalidOperation
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ROLES = ("admin", "controller", "response")
BUILD_FILES = (
    "siliconsign.bit.bin",
    "siliconsign.dts",
    "siliconsign.dtbo",
    "capabilities.json",
    "timing_summary.rpt",
    "pulse_width.rpt",
    "utilization.rpt",
    "drc.rpt",
    "synthesis_hierarchy.rpt",
)
# This image passed timing but its active-low auxiliary reset is tied low.
# Overlay changes cannot repair the implemented logic; never ship it again.
QUARANTINED_IMAGE_SHA256 = frozenset(
    {"7a25f51df561d92904ad812b4d3d01c5a4893416cf77886f5c895273575e3501"}
)


class PackageError(RuntimeError):
    pass


def read_private(path: Path) -> str:
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(fd, "r", encoding="ascii") as handle:
        info = os.fstat(handle.fileno())
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_uid != os.geteuid()
            or info.st_mode & 0o077
            or info.st_size > 8192
        ):
            raise PackageError(
                "Key bundle and generated key header must be private regular files owned by this operator."
            )
        return handle.read(8193)


def regular(path: Path) -> Path:
    if path.is_symlink() or not path.is_file() or path.stat().st_size == 0:
        raise PackageError(f"Required nonempty regular file is missing: {path}")
    return path


def validate_hardware_handoff(build: Path) -> None:
    """Check the actual exported XSA, without exposing its private key parameters.

    The handoff is a required private input, never a board archive member. These
    are consistency checks on trusted Vivado outputs, not bitstream attestation.
    """
    handoff = regular(build / "siliconsign.xsa")
    if handoff.stat().st_size > 512 * 1024 * 1024:
        raise PackageError("Hardware handoff exceeds the supported size.")
    try:
        with zipfile.ZipFile(handoff) as archive:
            candidates = [
                item
                for item in archive.infolist()
                if Path(item.filename).name == "siliconsign_bd.hwh"
            ]
            if len(candidates) != 1 or not 0 < candidates[0].file_size <= 16 * 1024 * 1024:
                raise PackageError("XSA must contain one bounded top-level hardware handoff.")
            xml = archive.read(candidates[0])
        if b"<!DOCTYPE" in xml.upper() or b"<!ENTITY" in xml.upper():
            raise PackageError("Hardware handoff declarations are unsupported.")
        root = ET.fromstring(xml)
    except (zipfile.BadZipFile, ET.ParseError, RuntimeError, ValueError, OSError):
        # Never include parser context: XML contains the actual private keys.
        raise PackageError("Cannot read the exported XSA hardware handoff.") from None
    for element in root.iter():
        element.tag = element.tag.rsplit("}", 1)[-1]

    def module(instance: str, kind: str) -> ET.Element:
        matches = [m for m in root.iter("MODULE") if m.get("INSTANCE") == instance]
        if len(matches) != 1 or matches[0].get("MODTYPE") != kind:
            raise PackageError(f"Hardware handoff has an unexpected {instance} module.")
        return matches[0]

    def parameter(item: ET.Element, name: str) -> str:
        matches = [p for p in item.findall("./PARAMETERS/PARAMETER") if p.get("NAME") == name]
        if len(matches) != 1 or matches[0].get("VALUE") is None:
            raise PackageError("Hardware handoff is missing a required structural parameter.")
        return matches[0].get("VALUE", "")

    def number(item: ET.Element, name: str, expected: int) -> None:
        try:
            value = parameter(item, name)
            actual = int(value, 16) if re.fullmatch(r"0[xX][0-9a-fA-F]+", value) else Decimal(value)
            valid = actual == expected
        except (InvalidOperation, ValueError):
            valid = False
        if not valid:
            raise PackageError(f"Hardware handoff violates structural parameter {name}.")

    def port(item: ET.Element, name: str, direction: str, signal: str) -> ET.Element:
        matches = [p for p in item.findall("./PORTS/PORT") if p.get("NAME") == name]
        if (
            len(matches) != 1
            or matches[0].get("DIR") != direction
            or matches[0].get("SIGNAME") != signal
        ):
            raise PackageError(f"Hardware handoff has incorrect wiring for {name}.")
        return matches[0]

    ps = module("ps", "zynq_ultra_ps_e")
    reset = module("reset", "proc_sys_reset")
    gate = module("payment_gate", "siliconsign_axi")
    interconnect = module("interconnect", "smartconnect")
    for name, expected in {
        "C_MAXIGP0_DATA_WIDTH": 32,
        "PSU__MAXIGP0__DATA_WIDTH": 32,
        "PSU__USE__M_AXI_GP0": 1,
        "PSU__USE__M_AXI_GP1": 0,
        "PSU__USE__M_AXI_GP2": 0,
        "PSU__FPGA_PL0_ENABLE": 1,
        "PSU__CRL_APB__PL0_REF_CTRL__FREQMHZ": 50,
    }.items():
        number(ps, name, expected)
    try:
        actual_mhz = Decimal(parameter(ps, "PSU__CRL_APB__PL0_REF_CTRL__ACT_FREQMHZ"))
        clock_valid = actual_mhz.is_finite() and Decimal("49.5") <= actual_mhz <= 50
    except InvalidOperation:
        clock_valid = False
    if not clock_valid:
        raise PackageError("Hardware handoff actual PL clock is outside the accepted 50 MHz range.")
    for name, expected in {"NV": 64, "NI": 256, "CLK_HZ": 50000000}.items():
        number(gate, name, expected)
    for name in ("NUM_SI", "NUM_MI"):
        number(interconnect, name, 1)
    for name in ("C_EXT_RESET_HIGH", "C_AUX_RESET_HIGH"):
        number(reset, name, 0)
    for name, value in (("one", 1), ("zero", 0)):
        constant = module(name, "xlconstant")
        number(constant, "CONST_WIDTH", 1)
        number(constant, "CONST_VAL", value)
        port(constant, "dout", "O", f"{name}_dout")
    for item, name, direction, signal in (
        (ps, "pl_clk0", "O", "ps_pl_clk0"),
        (ps, "maxihpm0_fpd_aclk", "I", "ps_pl_clk0"),
        (ps, "pl_resetn0", "O", "ps_pl_resetn0"),
        (reset, "slowest_sync_clk", "I", "ps_pl_clk0"),
        (reset, "ext_reset_in", "I", "ps_pl_resetn0"),
        (reset, "aux_reset_in", "I", "one_dout"),
        (reset, "dcm_locked", "I", "one_dout"),
        (reset, "mb_debug_sys_rst", "I", "zero_dout"),
        (reset, "peripheral_aresetn", "O", "reset_peripheral_aresetn"),
        (interconnect, "aclk", "I", "ps_pl_clk0"),
        (interconnect, "aresetn", "I", "reset_peripheral_aresetn"),
        (gate, "s_axi_aclk", "I", "ps_pl_clk0"),
        (gate, "s_axi_aresetn", "I", "reset_peripheral_aresetn"),
    ):
        port(item, name, direction, signal)
    ranges = [r for r in root.iter("MEMRANGE") if r.get("INSTANCE") == "payment_gate"]
    expected_range = {
        "MASTERBUSINTERFACE": "M_AXI_HPM0_FPD",
        "SLAVEBUSINTERFACE": "S_AXI",
        "MEMTYPE": "REGISTER",
    }
    if len(ranges) != 1 or any(ranges[0].get(k) != v for k, v in expected_range.items()):
        raise PackageError("Hardware handoff has an unexpected payment-gate address mapping.")
    try:
        valid_range = (
            int(ranges[0].get("BASEVALUE", ""), 16) == 0xA0000000
            and int(ranges[0].get("HIGHVALUE", ""), 16) == 0xA0000FFF
        )
    except ValueError:
        valid_range = False
    if not valid_range:
        raise PackageError("Hardware handoff does not map the required 4 KiB payment aperture.")


def validate_deployment(build: Path, source_root: Path) -> None:
    """Use the board helper's exact firmware-name/digest contract, read-only.

    Requiring the compiled DTBO before calling prepare_image prevents its
    optional compiler path. No board inspection, installation or MMIO occurs.
    """
    regular(build / "siliconsign.dtbo")
    spec = importlib.util.spec_from_file_location(
        "payguard_deployment_preflight", regular(source_root / "hardware/scripts/deploy_kv260.py")
    )
    if spec is None or spec.loader is None:
        raise PackageError("Cannot load the matching deployment validator.")
    deployment = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(deployment)
    try:
        deployment.prepare_image(build)
    except deployment.DeployError:
        raise PackageError(
            "Compiled overlay, image and deployment manifest do not match."
        ) from None


def validated_inputs(
    build: Path, keys_path: Path, *, source_root: Path = ROOT
) -> tuple[dict[str, str], list[Path]]:
    try:
        obj = json.loads(read_private(keys_path))
        if not isinstance(obj, dict) or set(obj) != set(ROLES):
            raise ValueError
        keys = {role: bytes.fromhex(obj[role]) for role in ROLES}
        if (
            any(len(key) != 32 or not any(key) for key in keys.values())
            or len(set(keys.values())) != 3
        ):
            raise ValueError
    except (ValueError, TypeError):
        raise PackageError(
            "Existing key bundle must contain distinct nonzero admin/controller/response keys; no new keys will be generated."
        ) from None
    files = [regular(build / name) for name in BUILD_FILES]
    image_size = (build / "siliconsign.bit.bin").stat().st_size
    if not 1024 <= image_size <= 256 * 1024 * 1024 or image_size % 4:
        raise PackageError("Bootgen image size or word alignment is invalid.")
    with (build / "siliconsign.bit.bin").open("rb") as image:
        image_digest = hashlib.file_digest(image, "sha256").hexdigest()
    if image_digest in QUARANTINED_IMAGE_SHA256:
        raise PackageError(
            "The R2 image is quarantined for permanently asserted reset; rebuild required."
        )
    validate_hardware_handoff(build)
    try:
        capabilities = json.loads((build / "capabilities.json").read_text())
    except ValueError:
        raise PackageError("Invalid build capability manifest.") from None
    expected = {
        "vendor_slots": 64,
        "obligation_slots": 256,
        "register": "0x00c",
        "register_value": "0x01000040",
        "clock_register": "0x010",
        "clock_divider_hz": 50000000,
        "part": "xck26-sfvc784-2LV-c",
    }
    if not isinstance(capabilities, dict) or any(
        capabilities.get(k) != v for k, v in expected.items()
    ):
        raise PackageError("Build is not the current KV260 64-vendor/256-obligation, 50 MHz image.")
    timing = (build / "timing_summary.rpt").read_text()
    if "All user specified timing constraints are met." not in timing:
        raise PackageError(
            "Timing report does not confirm all constraints are met; refusing packaging."
        )
    header = read_private(build / ".generated" / "siliconsign_keys.vh")
    for role in ROLES:
        match = re.search(
            rf"`define\s+SILICONSIGN_{role.upper()}_KEY\s+256'h([0-9a-fA-F]{{64}})\b", header
        )
        if match is None or bytes.fromhex(match[1]) != keys[role]:
            raise PackageError(
                "Supplied bundle does not match the build's generated key header; refusing packaging."
            )
    overlay = build / "siliconsign.dtbo"
    if overlay.read_bytes()[:4] != b"\xd0\x0d\xfe\xed":
        raise PackageError("Compiled overlay has an invalid device-tree header.")
    validate_deployment(build, source_root)
    manifest = build / "deployment.json"
    if manifest.exists() or manifest.is_symlink():
        files.append(regular(manifest))
    return {role: key.hex() for role, key in keys.items()}, files


VERIFY_SCRIPT = '''"""Verify extracted private package contents; this does not deploy an FPGA."""
import hashlib, json
from pathlib import Path
root = Path(__file__).resolve().parent
manifest = json.loads((root / "manifest.json").read_text())
for relative, record in manifest["files"].items():
    path = root / relative
    if path.is_symlink() or root not in path.resolve().parents or not path.is_file():
        raise SystemExit("Unsafe or missing package path: " + relative)
    data = path.read_bytes()
    if len(data) != record["bytes"] or hashlib.sha256(data).hexdigest() != record["sha256"]:
        raise SystemExit("Package verification failed: " + relative)
print("Package file hashes verified. Physical deployment has not been performed.")
'''


def write_archive(
    path: Path, prefix: str, contents: dict[str, Path | bytes], purpose: str
) -> dict[str, object]:
    """No following source symlinks, overwriting destinations or copying build secrets wholesale."""
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    manifest: dict[str, object] = {
        "schema": "payguard.private-transfer.v1",
        "purpose": purpose,
        "physical_board_verified": False,
        "files": {},
    }
    try:
        with os.fdopen(fd, "wb") as raw, tarfile.open(fileobj=raw, mode="w:gz") as archive:
            directories = {prefix}
            for name in contents:
                parts = Path(name).parts
                if not parts or Path(name).is_absolute() or ".." in parts:
                    raise PackageError("Invalid package member path")
                directories.update(str(Path(prefix, *parts[:i])) for i in range(1, len(parts)))
            for name in sorted(directories):
                info = tarfile.TarInfo(name)
                info.type, info.mode, info.uid, info.gid = tarfile.DIRTYPE, 0o700, 0, 0
                archive.addfile(info)
            for name, item in sorted(contents.items()):
                if isinstance(item, Path):
                    regular(item)
                    data = item.read_bytes()
                else:
                    data = item
                manifest["files"][name] = {
                    "bytes": len(data),
                    "sha256": hashlib.sha256(data).hexdigest(),
                }
                info = tarfile.TarInfo(f"{prefix}/{name}")
                info.size, info.mode, info.uid, info.gid = len(data), 0o600, 0, 0
                archive.addfile(info, io.BytesIO(data))
            encoded = (json.dumps(manifest, indent=2, sort_keys=True) + "\n").encode()
            info = tarfile.TarInfo(f"{prefix}/manifest.json")
            info.size, info.mode, info.uid, info.gid = len(encoded), 0o600, 0, 0
            archive.addfile(info, io.BytesIO(encoded))
        with path.open("rb") as handle:
            digest = hashlib.file_digest(handle, "sha256").hexdigest()
        return {"path": str(path), "bytes": path.stat().st_size, "sha256": digest}
    except BaseException:
        # Remove only the incomplete file created exclusively by this invocation.
        path.unlink(missing_ok=True)
        raise


def package(
    build: Path, keys_path: Path, output: Path, *, source_root: Path = ROOT
) -> dict[str, object]:
    if output.exists() or output.is_symlink():
        raise PackageError(
            "Output directory already exists; choose a new private destination. Nothing was overwritten."
        )
    keys, artifacts = validated_inputs(build, keys_path, source_root=source_root)
    board_contents: dict[str, Path | bytes] = {f"build/{p.name}": p for p in artifacts}
    hardware = source_root / "hardware"
    for directory, pattern in (
        (hardware / "python" / "siliconsign", "*.py"),
        (hardware / "scripts", "*.py"),
        (hardware / "scripts", "*.sh"),
    ):
        for path in sorted(directory.glob(pattern)):
            board_contents[f"hardware/{path.relative_to(hardware).as_posix()}"] = regular(path)
    for needed in (
        "hardware/python/siliconsign/external_bridge.py",
        "hardware/python/siliconsign/__main__.py",
        "hardware/scripts/deploy_kv260.py",
        "hardware/scripts/run_uart_bridge.sh",
        "hardware/scripts/uart_console.py",
    ):
        if needed not in board_contents:
            raise PackageError(f"Current board runtime is incomplete: {needed}")
    guide = regular(hardware / "docs" / "DEPLOY_KV260.md")
    board_contents["DEPLOY_KV260.md"] = guide
    board_contents["verify_package.py"] = VERIFY_SCRIPT.encode()
    board_contents["private/response.json"] = (
        json.dumps({"response": keys["response"]}) + "\n"
    ).encode()
    output.mkdir(parents=True, mode=0o700)
    board_path = output / "payguard-kv260-board.tar.gz"
    board = write_archive(
        board_path,
        "payguard-kv260",
        board_contents,
        "Board deployment payload; response role only; no session initialized",
    )
    operator_contents: dict[str, Path | bytes] = {
        "board/payguard-kv260-board.tar.gz": board_path,
        "private/keys.json": (json.dumps(keys, indent=2) + "\n").encode(),
        "verify_package.py": VERIFY_SCRIPT.encode(),
        "SETUP.md": regular(source_root / "docs" / "SETUP.md"),
        "transfer/uart_upload.py": regular(source_root / "scripts" / "uart_upload.py"),
        "transfer/uart_receiver.py": regular(source_root / "scripts" / "uart_receiver.py"),
    }
    for role in ROLES:
        operator_contents[f"private/roles/{role}.key"] = (keys[role] + "\n").encode()
    operator = write_archive(
        output / "payguard-operator.tar.gz",
        "payguard-operator",
        operator_contents,
        "Trusted host operator payload; contains all existing role keys",
    )
    result = {
        "operator": operator,
        "board": board,
        "physical_board_verified": False,
        "note": "Archives are private and unencrypted. Transfer only the board archive to the Kria. No hardware or existing projects were changed.",
    }
    fd = os.open(output / "transfer-manifest.json", os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w") as handle:
        json.dump(result, handle, indent=2)
        handle.write("\n")
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--build-dir", required=True, type=Path)
    parser.add_argument(
        "--keys",
        required=True,
        type=Path,
        help="Existing private bundle matching this completed build",
    )
    parser.add_argument(
        "--output-dir",
        required=True,
        type=Path,
        help="New private output directory; never overwritten",
    )
    args = parser.parse_args(argv)
    try:
        result = package(
            args.build_dir.expanduser(), args.keys.expanduser(), args.output_dir.expanduser()
        )
    except (PackageError, OSError) as exc:
        print(f"Packaging stopped: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
