#!/usr/bin/env python3
"""Inspect or load the SiliconSign image on the physical KV260.

Read-only: python3 scripts/deploy_kv260.py --inspect
Deploy: sudo python3 scripts/deploy_kv260.py --build-dir /path/to/vivado-output

It never unloads an existing overlay, edits boot firmware, starts a payment
service, or writes to DDR. It explicitly selects full-image firmware loading
and loads a manifest-bound SiliconSign image through the kernel FPGA-region
interface. Versioned firmware names preserve earlier installed images.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import mmap
import os
from pathlib import Path
import platform
import re
import shutil
import struct
import subprocess
import sys
import tempfile
import time

DT = Path("/proc/device-tree")
MANAGER = Path("/sys/class/fpga_manager/fpga0")
CONFIGFS = Path("/sys/kernel/config")
OVERLAYS = CONFIGFS / "device-tree/overlays"
DEBUGFS = Path("/sys/kernel/debug")
PLATFORM_DEVICES = Path("/sys/bus/platform/devices")
PLATFORM_DRIVERS = Path("/sys/bus/platform/drivers")
FIRMWARE = Path("/lib/firmware/siliconsign.bit.bin")
KERNEL_TAINT = Path("/proc/sys/kernel/tainted")
KERNEL_OOPS_TAINT = 1 << 7
PS_PL_RESET_IDS = (116, 117, 118, 119)
IP_BASE = 0xA0000000
IP_ID = 0x53494C31
MIN_CLOCK_HZ, MAX_CLOCK_HZ = 49_500_000, 50_000_000
# This exact r2 image permanently asserts active-low auxiliary reset. Retain
# its files as evidence, but never load/probe it through the current helper.
QUARANTINED_IMAGE_SHA256 = frozenset({
    "7a25f51df561d92904ad812b4d3d01c5a4893416cf77886f5c895273575e3501",
})


class DeployError(RuntimeError):
    pass


def phase(message: str) -> None:
    print(f"[SiliconSign deploy] {message}", file=sys.stderr, flush=True)


def text(path: Path) -> str:
    return path.read_bytes().replace(b"\0", b" ").decode("utf-8", "replace").strip()


def optional_text(path: Path) -> str | None:
    try:
        return text(path)
    except (OSError, ValueError):
        return None


def read_unsigned(path: Path, base: int) -> int | None:
    """Read an ASCII kernel attribute; malformed or unavailable is unknown."""
    try:
        value = path.read_text(encoding="ascii").strip()
    except (OSError, UnicodeError):
        return None
    pattern = r"(?:0[xX])?[0-9a-fA-F]+" if base == 16 else r"[0-9]+"
    if not re.fullmatch(pattern, value):
        return None
    try:
        return int(value, base)
    except ValueError:
        return None


def require_kernel_ready() -> int:
    taint = read_unsigned(KERNEL_TAINT, 10)
    if taint is None:
        raise DeployError("Cannot verify kernel OOPS state; no FPGA programming attempted.")
    if taint & KERNEL_OOPS_TAINT:
        raise DeployError("This kernel recorded an OOPS/BUG. Preserve its diagnostics and recover with a clean boot before programming; no FPGA load attempted.")
    # Warnings and externally built drivers alone are not an OOPS.
    return taint


def select_full_image_mode() -> int:
    """Clear stale AMD DMA-buffer/partial flags before firmware-name loading.

    AMD fpga_mgr_load checks manager-wide CONFIG_DMA_BUF before firmware_name.
    A previous DMA-buffer loader can leave that flag and a released buffer.
    Full, unencrypted firmware loading requires flags=0, verified by readback.
    """
    flags = MANAGER / "flags"
    previous = read_unsigned(flags, 16)
    if previous is None:
        raise DeployError("FPGA manager flags are missing, unreadable or malformed; no FPGA load attempted.")
    try:
        with flags.open("w", encoding="ascii") as target:
            if target.write("0\n") != 2:
                raise DeployError("Could not write complete full-image manager flags.")
    except OSError as exc:
        raise DeployError("Cannot set FPGA manager flags to full-image mode; no FPGA load attempted.") from exc
    if read_unsigned(flags, 16) != 0:
        raise DeployError("FPGA manager full-image flags did not read back as zero; no FPGA load attempted.")
    return previous


def identifies_kv260(model: str, compatible: str) -> bool:
    identity = (model + " " + compatible).lower()
    if any(other in identity for other in ("kr260", "kd240")):
        return False
    return bool(re.search(r"(?<![a-z0-9])(?:kv260|k26)(?![a-z0-9])", identity))


def clock_from_summary(summary: str) -> int | None:
    """Support kernels with and without a protect-count column, never guess it."""
    rate_column = None
    for line in summary.splitlines():
        fields = line.split()
        if fields and fields[0] == "clock" and "rate" in fields:
            rate_column = fields.index("rate")
        elif fields and fields[0] == "pl0_ref" and rate_column is not None:
            try:
                return int(fields[rate_column], 10)
            except (ValueError, IndexError):
                return None
    return None


def read_clock() -> int | None:
    direct = optional_text(DEBUGFS / "clk/pl0_ref/clk_rate")
    if direct is not None:
        try:
            return int(direct, 0)
        except ValueError:
            pass
    summary = optional_text(DEBUGFS / "clk/clk_summary")
    return clock_from_summary(summary) if summary is not None else None


def clock_counts_from_summary(summary: str) -> dict[str, int] | None:
    header = None
    for line in summary.splitlines():
        fields = line.split()
        if fields and fields[0] == "clock" and "rate" in fields:
            header = fields
        elif fields and fields[0] == "pl0_ref" and header is not None:
            if header[1:3] != ["count", "count"]:
                return None
            try:
                enabled, prepared = int(fields[1]), int(fields[2])
                rate = int(fields[header.index("rate")])
            except (ValueError, IndexError):
                return None
            if min(enabled, prepared, rate) < 0:
                return None
            return {"enable_count": enabled, "prepare_count": prepared, "rate_hz": rate}
    return None


def read_cells(path: Path) -> list[int] | None:
    try:
        data = path.read_bytes()
    except OSError:
        return None
    if not data or len(data) % 4:
        return None
    return list(struct.unpack(f">{len(data) // 4}I", data))


def platform_binding(name: str) -> dict | None:
    amba = optional_text(DT / "__symbols__/amba")
    if not amba or not amba.startswith("/"):
        return None
    try:
        expected = (DT / amba.lstrip("/") / name).resolve(strict=True)
        for device in PLATFORM_DEVICES.iterdir():
            try:
                if (device / "of_node").resolve(strict=True) == expected:
                    return {"device": device.name,
                            "compatible": optional_text(device / "of_node/compatible"),
                            "driver": (device / "driver").resolve(strict=True).name}
            except OSError:
                continue
    except OSError:
        pass
    return None


def required_drivers() -> dict[str, bool]:
    return {name: (PLATFORM_DRIVERS / name).is_dir() for name in ("xilinx_fclk", "afi-fpga")}


def require_platform_drivers() -> None:
    missing = [name for name, present in required_drivers().items() if not present]
    if missing:
        raise DeployError("Required platform drivers are not registered: " + ", ".join(missing) +
                          ". Inspect kernel configuration/module availability before loading; no overlay applied.")


def fabric_clock_diagnostics() -> dict:
    """Read kernel metadata only; never map or access the PL register aperture."""
    summary = optional_text(DEBUGFS / "clk/clk_summary")
    counts = clock_counts_from_summary(summary) if summary is not None else None
    binding = platform_binding("siliconsign_clocking0")
    afi = {"binding": platform_binding("siliconsign_afi"), "config": None,
           "resets": None, "expected_reset_phandle": None}
    amba = optional_text(DT / "__symbols__/amba")
    if amba and amba.startswith("/"):
        node = DT / amba.lstrip("/") / "siliconsign_afi"
        afi["config"] = read_cells(node / "config-afi")
        afi["resets"] = read_cells(node / "resets")
    reset = optional_text(DT / "__symbols__/zynqmp_reset")
    if reset and reset.startswith("/"):
        node = DT / reset.lstrip("/")
        phandle = read_cells(node / "phandle") or read_cells(node / "linux,phandle")
        if phandle and len(phandle) == 1:
            afi["expected_reset_phandle"] = phandle[0]
    return {"pl0_ref": counts, "fabric_clock_binding": binding, "afi": afi}


def require_clock_enabled(diagnostics: dict) -> None:
    counts = diagnostics.get("pl0_ref")
    binding = diagnostics.get("fabric_clock_binding")
    if not counts or counts["enable_count"] <= 0 or counts["prepare_count"] <= 0:
        raise DeployError("PL0 clock is not confirmed prepared and enabled; refusing PL MMIO probe.")
    if not MIN_CLOCK_HZ <= counts["rate_hz"] <= MAX_CLOCK_HZ:
        raise DeployError("PL0 clock summary rate is unsafe or unknown; refusing PL MMIO probe.")
    if not binding or binding["driver"] != "xilinx_fclk" or binding["compatible"] != "xlnx,fclk":
        raise DeployError("SiliconSign fabric-clock node is not bound to xilinx_fclk; refusing PL MMIO probe.")


def require_afi_ready(diagnostics: dict) -> None:
    afi = diagnostics.get("afi") or {}
    binding = afi.get("binding") or {}
    phandle = afi.get("expected_reset_phandle")
    # Follow AMD's generated all-four reset array. Firmware maps these IDs to
    # EMIO92..95, while Vivado pl_resetn0 uses EMIO95; ID116 alone misses it.
    expected_resets = [cell for reset_id in PS_PL_RESET_IDS for cell in (phandle, reset_id)]
    if (binding.get("driver") != "afi-fpga" or binding.get("compatible") != "xlnx,afi-fpga"
            or afi.get("config") != [14, 0] or not phandle
            or afi.get("resets") != expected_resets):
        raise DeployError("SiliconSign AFI width32, all four PS/PL reset references (116..119), and driver binding are not confirmed; refusing PL MMIO probe.")


def active_overlays() -> list[dict[str, str]]:
    if not OVERLAYS.is_dir():
        return []
    return [
        {"name": item.name, "status": optional_text(item / "status") or "unknown"}
        for item in sorted(OVERLAYS.iterdir()) if item.is_dir()
    ]


def inspection() -> dict:
    model = optional_text(DT / "model") or ""
    compatible = optional_text(DT / "compatible") or ""
    symbols = {}
    for symbol in ("fpga_full", "amba", "zynqmp_clk", "zynqmp_reset"):
        value = optional_text(DT / "__symbols__" / symbol)
        symbols[symbol] = value
    return {
        "mode": "READ-ONLY INSPECTION; no hardware image loaded by this command",
        "architecture": platform.machine(),
        "board_model": model,
        "board_compatible": compatible,
        "identified_kv260_or_k26": identifies_kv260(model, compatible),
        "fpga_manager_present": MANAGER.is_dir(),
        "fpga_manager_state": optional_text(MANAGER / "state"),
        "fpga_manager_flags": read_unsigned(MANAGER / "flags", 16),
        "kernel_taint": read_unsigned(KERNEL_TAINT, 10),
        "required_device_tree_symbols": symbols,
        "configfs_overlay_interface": OVERLAYS.is_dir(),
        "existing_overlays": active_overlays(),
        "pl0_ref_hz": read_clock(),
        "fabric_clock_diagnostics": fabric_clock_diagnostics(),
        "required_platform_drivers": required_drivers(),
        "effective_uid": os.geteuid(),
    }


def require_board(info: dict) -> None:
    if info["architecture"].lower() not in ("aarch64", "arm64"):
        raise DeployError("Deploy only on the physical aarch64 KV260, not starline-builder or the laptop.")
    if not info["identified_kv260_or_k26"]:
        raise DeployError("Device-tree model/compatible does not identify the supported KV260/K26 board.")
    if not info["fpga_manager_present"]:
        raise DeployError("Linux FPGA manager fpga0 is unavailable.")
    for symbol, value in info["required_device_tree_symbols"].items():
        if not value or not value.startswith("/") or not (DT / value.lstrip("/")).is_dir():
            raise DeployError(f"Required device-tree symbol {symbol!r} is missing or unresolved; do not apply this overlay.")


def mounted_at(target: Path, filesystem: str) -> bool:
    for line in Path("/proc/mounts").read_text().splitlines():
        fields = line.split()
        if len(fields) >= 3 and fields[1] == str(target) and fields[2] == filesystem:
            return True
    return False


def ensure_mount(target: Path, filesystem: str) -> None:
    if not mounted_at(target, filesystem):
        if not target.is_dir():
            raise DeployError(f"Expected kernel mountpoint {target} is missing.")
        subprocess.run(["mount", "-t", filesystem, filesystem, str(target)], check=True)


def sha256(path: Path) -> str:
    with path.open("rb") as source:
        return hashlib.file_digest(source, "sha256").hexdigest() if hasattr(hashlib, "file_digest") else hashlib.sha256(source.read()).hexdigest()


def firmware_destination(name: str) -> Path:
    if (not isinstance(name, str) or len(name) > 64
            or not re.fullmatch(r"siliconsign(?:-r[1-9][0-9]*)?\.bit\.bin", name)):
        raise DeployError("Firmware name must be a SiliconSign basename, never a path.")
    return FIRMWARE.parent / name


def require_supported_image_digest(digest: str) -> None:
    if digest in QUARANTINED_IMAGE_SHA256:
        raise DeployError("The known defective r2 image is quarantined: auxiliary reset is permanently asserted. No load or PL MMIO probe is permitted.")


def installed_firmware_record() -> dict:
    """Inspect only the published FPGA-region symbol and its firmware file.

    File identity is useful context, not a readback attestation of PL contents.
    Older kernels need not expose firmware-name, so absent metadata is unknown.
    """
    region = optional_text(DT / "__symbols__/fpga_full")
    if not region or not region.startswith("/"):
        return {}
    name = optional_text(DT / region.lstrip("/") / "firmware-name")
    if not name:
        return {}
    destination = firmware_destination(name)
    if destination.is_symlink():
        raise DeployError("Applied firmware name resolves to a symlink; no PL MMIO probe attempted.")
    record = {"firmware_name": name}
    if destination.is_file():
        digest = sha256(destination)
        require_supported_image_digest(digest)
        record["installed_firmware_sha256"] = digest
    return record


def firmware_name_from_overlay(payload: bytes) -> str:
    """Read the sole firmware-name property from a bounded FDT v17 structure.

    This performs no kernel operation and requires no external DT utilities.
    Parsing properties, rather than searching raw bytes, rejects a second
    firmware-name in another fragment or a string hidden in another property.
    """
    try:
        if not 40 <= len(payload) <= 256 * 1024:
            raise ValueError
        magic, total, structure, strings, _, version, compatible, _, string_size, struct_size = struct.unpack_from(
            ">10I", payload
        )
        if (magic != 0xD00DFEED or total != len(payload) or version != 17 or compatible > 17
                or structure < 40 or strings < 40 or structure % 4
                or structure + struct_size > total or strings + string_size > total
                or not (structure + struct_size <= strings or strings + string_size <= structure)):
            raise ValueError
        names = payload[strings:strings + string_size]
        cursor, end, stack, found = structure, structure + struct_size, [], []
        while cursor + 4 <= end:
            token = struct.unpack_from(">I", payload, cursor)[0]
            cursor += 4
            if token == 1:  # FDT_BEGIN_NODE
                stop = payload.index(b"\0", cursor, end)
                stack.append(payload[cursor:stop].decode("ascii"))
                cursor = (stop + 4) & ~3
            elif token == 2:  # FDT_END_NODE
                if not stack:
                    raise ValueError
                stack.pop()
            elif token == 3:  # FDT_PROP
                if cursor + 8 > end or not stack:
                    raise ValueError
                length, name_offset = struct.unpack_from(">II", payload, cursor)
                cursor += 8
                if cursor + length > end or name_offset >= len(names):
                    raise ValueError
                stop = names.index(b"\0", name_offset)
                property_name = names[name_offset:stop].decode("ascii")
                value = payload[cursor:cursor + length]
                cursor = (cursor + length + 3) & ~3
                if property_name == "firmware-name":
                    if stack != ["", "fragment@0", "__overlay__"] or not value.endswith(b"\0"):
                        raise ValueError
                    found.append(value[:-1].decode("ascii"))
            elif token == 4:  # FDT_NOP
                continue
            elif token == 9:  # FDT_END
                if stack or len(found) != 1:
                    raise ValueError
                firmware_destination(found[0])
                return found[0]
            else:
                raise ValueError
        raise ValueError
    except (ValueError, IndexError, UnicodeError, struct.error) as exc:
        raise DeployError("Invalid overlay or ambiguous/misplaced firmware-name; no image loaded.") from exc


def load_deployment_manifest(build: Path) -> dict | None:
    path = build / "deployment.json"
    if not path.exists() and not path.is_symlink():
        return None
    if path.is_symlink() or not path.is_file() or path.stat().st_size > 8192:
        raise DeployError("Deployment manifest must be a small regular file.")
    try:
        manifest = json.loads(path.read_text())
        if (not isinstance(manifest, dict) or set(manifest) !=
                {"schema", "firmware_name", "image_sha256", "overlay_sha256"}
                or manifest["schema"] != "siliconsign.deployment.v1"
                or any(not isinstance(manifest[key], str) or not re.fullmatch(r"[0-9a-f]{64}", manifest[key])
                       for key in ("image_sha256", "overlay_sha256"))):
            raise ValueError
        firmware_destination(manifest["firmware_name"])
        return manifest
    except (ValueError, UnicodeError) as exc:
        raise DeployError("Invalid deployment manifest; no image loaded.") from exc


def prepare_image(build: Path) -> tuple[Path, bytes, str, str]:
    image = build / "siliconsign.bit.bin"
    source = build / "siliconsign.dts"
    overlay = build / "siliconsign.dtbo"
    if not image.is_file() or image.is_symlink():
        raise DeployError("Build directory must contain the generated regular file siliconsign.bit.bin.")
    size = image.stat().st_size
    if size < 1024 or size > 256 * 1024 * 1024 or size % 4:
        raise DeployError("FPGA image size is invalid; use the Bootgen output from the completed Vivado build.")
    if not overlay.is_file():
        compiler = shutil.which("dtc")
        if not compiler or not source.is_file():
            raise DeployError("siliconsign.dtbo is missing; both generated siliconsign.dts and the OS dtc command are required.")
        fd, temporary = tempfile.mkstemp(prefix=".siliconsign-overlay-", dir=build)
        os.close(fd)
        try:
            subprocess.run([compiler, "-@", "-I", "dts", "-O", "dtb", "-o", temporary, str(source)], check=True)
            os.replace(temporary, overlay)
        finally:
            Path(temporary).unlink(missing_ok=True)
    if overlay.is_symlink():
        raise DeployError("Refusing a symlinked overlay artifact.")
    payload = overlay.read_bytes()
    name = firmware_name_from_overlay(payload)
    digest = sha256(image)
    require_supported_image_digest(digest)
    manifest = load_deployment_manifest(build)
    if manifest is None:
        if name != "siliconsign.bit.bin":
            raise DeployError("Versioned firmware requires deployment.json; no image loaded.")
    elif (manifest["firmware_name"] != name or manifest["image_sha256"] != digest
          or manifest["overlay_sha256"] != hashlib.sha256(payload).hexdigest()):
        raise DeployError("Deployment manifest does not match the image, overlay or firmware name; no image loaded.")
    return image, payload, name, digest


def install_firmware(image: Path, name: str = "siliconsign.bit.bin", expected_digest: str | None = None) -> str:
    destination = firmware_destination(name)
    digest = sha256(image)
    require_supported_image_digest(digest)
    if expected_digest is not None and digest != expected_digest:
        raise DeployError("Image changed after manifest validation; no firmware installed.")
    if destination.is_symlink():
        raise DeployError("The SiliconSign firmware destination is a symlink; refusing to follow it.")
    if destination.exists():
        if sha256(destination) != digest:
            raise DeployError(f"A different {destination} already exists. Choose a new versioned package; no file was replaced.")
        # Existing firmware may contain compiled keys; restrict this owned file.
        destination.chmod(0o600)
        return digest
    fd, temporary = tempfile.mkstemp(prefix=".siliconsign-", dir=destination.parent)
    try:
        with os.fdopen(fd, "wb") as target, image.open("rb") as source:
            shutil.copyfileobj(source, target)
            target.flush()
            os.fsync(target.fileno())
        os.chmod(temporary, 0o600)
        if sha256(Path(temporary)) != digest:
            raise DeployError("Image changed while copying; no firmware installed.")
        # The deployment precondition requires an unused firmware destination.
        # link() fails rather than overwriting if another process races creation.
        os.link(temporary, destination)
        parent = os.open(destination.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(parent)
        finally:
            os.close(parent)
    finally:
        Path(temporary).unlink(missing_ok=True)
    return digest


def read_ip_child() -> int:
    """Read fixed IP fields in a child, containing SIGBUS but NOT AXI stalls.

    An unresponsive PL bus can stall a CPU and the kernel even for read-only
    loads. A child timeout cannot make such hardware accesses safe.
    """
    if platform.machine().lower() not in ("aarch64", "arm64") or os.geteuid() != 0:
        return 2
    fd = os.open("/dev/mem", os.O_RDONLY | os.O_SYNC)
    try:
        with mmap.mmap(fd, 4096, flags=mmap.MAP_SHARED, prot=mmap.PROT_READ, offset=IP_BASE) as region:
            status = struct.unpack_from("<I", region, 4)[0]
            ident = struct.unpack_from("<I", region, 8)[0]
            capacities = struct.unpack_from("<I", region, 12)[0]
            divider = struct.unpack_from("<I", region, 16)[0]
        print(json.dumps({"id": ident, "status": status, "capabilities": capacities, "clock_divider_hz": divider}))
    finally:
        os.close(fd)
    return 0


def verify_ip() -> dict:
    child = ("import importlib.util,sys; s=importlib.util.spec_from_file_location('pg_probe',sys.argv[1]); "
             "m=importlib.util.module_from_spec(s); s.loader.exec_module(m); sys.exit(m.read_ip_child())")
    probe = subprocess.run([sys.executable, "-c", child, str(Path(__file__).resolve())],
                           capture_output=True, text=True, timeout=5)
    if probe.returncode != 0:
        raise DeployError("Read-only IP probe failed (mapping, permissions or bus fault); payment bridge must remain stopped.")
    try:
        result = json.loads(probe.stdout)
    except (json.JSONDecodeError, TypeError) as exc:
        raise DeployError("IP probe did not return valid verification data.") from exc
    if result.get("id") != IP_ID:
        raise DeployError(f"Wrong FPGA IP ID: expected 0x{IP_ID:08x}.")
    if result.get("capabilities") != 0x01000040:
        raise DeployError("Wrong FPGA capacity: PayGuard requires 64 vendors and 256 obligations")
    if result.get("clock_divider_hz") != 50_000_000:
        raise DeployError("Wrong compiled timer divider: PayGuard requires the 50 MHz image")
    status = result.get("status", 0)
    if not status & 4:
        raise DeployError("FPGA reports invalid/missing role keys; payment bridge must remain stopped.")
    if status & 3:
        raise DeployError("Freshly loaded FPGA unexpectedly reports BUSY or unacknowledged DONE; do not provision.")
    return {"ip_id": f"0x{result['id']:08x}", "status": status, "keys_valid": True}


def deploy(build: Path, replace_unmanaged: bool, *, load_only: bool = True) -> dict:
    phase("preflight: checking board, kernel and existing overlays")
    info = inspection()
    require_board(info)
    if os.geteuid() != 0:
        raise DeployError("Actual deployment requires root; --inspect is read-only and does not require root.")
    kernel_taint = require_kernel_ready()
    require_platform_drivers()
    ensure_mount(CONFIGFS, "configfs")
    if not OVERLAYS.is_dir():
        raise DeployError("This kernel lacks the configfs device-tree overlay interface; no image has been loaded.")
    previous = active_overlays()
    if previous:
        names = ", ".join(item["name"] for item in previous)
        raise DeployError(f"Existing overlays found ({names}). Preserve/stop the active Starline design explicitly; this script never unloads it.")
    state = optional_text(MANAGER / "state")
    if state == "operating" and not replace_unmanaged:
        raise DeployError("An unmanaged FPGA image is already operating. This could be Starline. After deliberately arranging its replacement, use --replace-unmanaged-image; nothing has been overwritten.")
    ensure_mount(DEBUGFS, "debugfs")
    # Confirm a readable clock source exists before changing the running image.
    if read_clock() is None:
        raise DeployError("Cannot inspect PL0 clock from debugfs; deployment stopped before loading an image.")
    image, overlay_data, firmware_name, expected_digest = prepare_image(build)
    previous_flags = select_full_image_mode()
    phase(f"full-image manager flags verified at 0 (previous hex {previous_flags:x})")
    image_digest = install_firmware(image, firmware_name, expected_digest)
    destination = OVERLAYS / "siliconsign"
    destination.mkdir()
    applied = False
    try:
        phase("begin overlay apply: closing dtbo invokes kernel FPGA programming")
        with (destination / "dtbo").open("wb", buffering=0) as target:
            if target.write(overlay_data) != len(overlay_data):
                raise DeployError("Kernel did not accept the complete overlay.")
        phase("overlay apply returned; checking manager and overlay state")
        for _ in range(50):
            state = optional_text(MANAGER / "state")
            overlay_state = optional_text(destination / "status")
            if state == "operating" and overlay_state == "applied":
                applied = True
                break
            time.sleep(0.1)
        if not applied:
            raise DeployError("FPGA manager/overlay did not reach operating/applied; no bridge was started.")
        phase("manager operating and overlay applied; checking clock, AFI and all four PS/PL reset references")
        rate = read_clock()
        if rate is None or not MIN_CLOCK_HZ <= rate <= MAX_CLOCK_HZ:
            raise DeployError(f"Unsafe or unknown PL0 clock {rate!r}; required {MIN_CLOCK_HZ}..{MAX_CLOCK_HZ} Hz. No bridge was started.")
        diagnostics = fabric_clock_diagnostics()
        result = {"firmware_sha256": image_digest, "firmware_name": firmware_name,
                  "firmware_path": str(firmware_destination(firmware_name)), "overlay": str(destination),
                  "fpga_state": state, "pl0_ref_hz": rate,
                  "previous_fpga_manager_flags": previous_flags,
                  "fpga_manager_flags": 0, "kernel_taint": kernel_taint,
                  "fabric_clock_diagnostics": diagnostics,
                  "payment_bridge_started": False, "ip_verified": False}
        require_clock_enabled(diagnostics)
        require_afi_ready(diagnostics)
        if load_only:
            phase("load-only complete: UNVERIFIED; no PL MMIO probe or payment bridge")
            return {"mode": "LOADED BUT UNVERIFIED; no PL MMIO performed", **result}
        phase("begin PL MMIO probe: an unresponsive AXI bus can stall the kernel")
        ip = verify_ip()
        phase("PL MMIO probe completed and IP identity/capacity/keys verified")
        return {"mode": "PHYSICAL FPGA VERIFIED; payment bank remains simulated",
                **result, **ip, "ip_verified": True}
    except Exception as exc:
        # Remove only a failed, unapplied directory created by this invocation.
        # Never remove a live overlay or pretend the previous bitstream was restored.
        if not applied and optional_text(destination / "status") != "applied":
            try:
                destination.rmdir()
            except OSError:
                pass
        raise DeployError(f"{exc} Inspect the FPGA state before proceeding; a successfully applied SiliconSign overlay is left installed and no payment bridge has been started.") from exc


def verify_existing() -> dict:
    """Explicitly probe an already-loaded, reviewed overlay without reloading it."""
    phase("verify-existing: checking kernel, overlay, clock, AFI and all four reset references before any MMIO")
    require_board(inspection())
    if os.geteuid() != 0:
        raise DeployError("Existing-image verification requires root.")
    require_kernel_ready()
    require_platform_drivers()
    if active_overlays() != [{"name": "siliconsign", "status": "applied"}]:
        raise DeployError("Expected only the applied SiliconSign overlay; no MMIO probe attempted.")
    if optional_text(MANAGER / "state") != "operating" or read_unsigned(MANAGER / "flags", 16) != 0:
        raise DeployError("FPGA manager is not operating in verified full-image mode; no MMIO probe attempted.")
    firmware_record = installed_firmware_record()
    diagnostics = fabric_clock_diagnostics()
    require_clock_enabled(diagnostics)
    require_afi_ready(diagnostics)
    phase("begin explicit PL MMIO probe: AXI stalls cannot be contained by a child timeout")
    ip = verify_ip()
    phase("existing-image MMIO probe completed")
    return {"mode": "PHYSICAL FPGA VERIFIED; payment bank remains simulated", **ip,
            "ip_verified": True, "image_reloaded": False,
            **firmware_record,
            "fabric_clock_diagnostics": diagnostics, "payment_bridge_started": False}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--inspect", action="store_true", help="read-only board/overlay/clock inspection")
    parser.add_argument("--build-dir", type=Path, help="completed Vivado output directory")
    parser.add_argument("--replace-unmanaged-image", action="store_true",
                        help="explicitly permit replacement of an operating image with no configfs overlay; never unloads overlays")
    parser.add_argument("--load-only", action="store_true",
                        help="explicitly select the default: load without PL MMIO; result remains UNVERIFIED")
    parser.add_argument("--verify-existing", action="store_true",
                        help="explicit PL MMIO verification of an already-loaded image; never reloads it")
    args = parser.parse_args(argv)
    if args.verify_existing:
        if args.inspect or args.build_dir or args.replace_unmanaged_image or args.load_only:
            parser.error("--verify-existing cannot be combined with loading or inspection")
        print(json.dumps(verify_existing(), indent=2))
        return 0
    if args.inspect:
        if args.build_dir or args.replace_unmanaged_image or args.load_only:
            parser.error("--inspect cannot be combined with deployment options")
        print(json.dumps(inspection(), indent=2))
        return 0
    if args.build_dir is None:
        parser.error("choose --inspect or --build-dir")
    result = deploy(args.build_dir.resolve(), args.replace_unmanaged_image, load_only=True)
    print(json.dumps(result, indent=2))
    print("UNVERIFIED: loading returned without a PL register probe. Keep the payment bridge stopped until explicit --verify-existing succeeds.")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (DeployError, OSError, ValueError, subprocess.SubprocessError) as exc:
        print(f"SiliconSign deployment stopped: {exc}", file=sys.stderr)
        raise SystemExit(2)
