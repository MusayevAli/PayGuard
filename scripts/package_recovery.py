"""Create a public, overlay-only KV260 recovery paste; never access hardware."""

from __future__ import annotations

import base64
import gzip
import hashlib
import json
import re
import textwrap
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CURRENT_REVISION = "r4"

INSTALLER = r"""
import base64, gzip, hashlib, json, os, stat, subprocess, sys, types
from pathlib import Path

TARGET = Path("/opt/payguard-recovery-r4")
ORIGINAL = Path("/opt/payguard-kv260")
EXPECTED_NAMES = {
    "deploy_kv260.py", "build/siliconsign.dts",
    "build/siliconsign.dtbo", "manifest.json",
}

def phase(message):
    print("[PayGuard r4] " + message, flush=True)

def require(condition, message):
    if not condition:
        raise RuntimeError(message)

def safe_read(path, limit=268435456):
    path = Path(path)
    require(path.is_absolute(), "An absolute path is required")
    fd = os.open("/", os.O_RDONLY | os.O_DIRECTORY)
    try:
        for part in path.parts[1:-1]:
            next_fd = os.open(part, os.O_RDONLY | os.O_DIRECTORY |
                              os.O_NOFOLLOW, dir_fd=fd)
            os.close(fd)
            fd = next_fd
            require(os.fstat(fd).st_uid == 0,
                    "Path directory must be root-owned")
        source = os.open(path.name, os.O_RDONLY | os.O_NOFOLLOW,
                         dir_fd=fd)
        with os.fdopen(source, "rb") as handle:
            info = os.fstat(handle.fileno())
            require(stat.S_ISREG(info.st_mode) and info.st_uid == 0,
                    "Input must be a root-owned regular file")
            require(not info.st_mode & 0o077,
                    "Input must have owner-only permissions")
            require(info.st_size <= limit, "Input exceeds size limit")
            return handle.read(limit + 1)
    finally:
        os.close(fd)

def verified(data, record):
    require(len(data) == record["bytes"], "File size mismatch")
    require(hashlib.sha256(data).hexdigest() == record["sha256"],
            "File SHA-256 mismatch")
    return data

def unpack(encoded, expected):
    packed = base64.b64decode(encoded, validate=False)
    require(hashlib.sha256(packed).hexdigest() == expected,
            "Installer payload SHA-256 mismatch")
    obj = json.loads(gzip.decompress(packed))
    require(set(obj) == EXPECTED_NAMES, "Unexpected payload members")
    files = {}
    for name, item in obj.items():
        encoding = item.get("encoding", "base64")
        require(encoding in ("utf-8", "base64"), "Unknown encoding")
        data = (item["data"].encode("utf-8") if encoding == "utf-8"
                else base64.b64decode(item["data"], validate=True))
        files[name] = verified(data, item)
    return files

def write_new(path, data):
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL |
                 os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "wb") as handle:
        handle.write(data)
        handle.flush()
        os.fsync(handle.fileno())

def run(encoded, expected):
    require(os.geteuid() == 0, "Run this installer through sudo")
    os.umask(0o077)
    phase("Verify the public recovery payload before parsing code")
    files = unpack(encoded, expected)
    helper = types.ModuleType("payguard_recovery_deploy")
    helper.__file__ = str(TARGET / "deploy_kv260.py")
    sys.modules[helper.__name__] = helper
    exec(compile(files["deploy_kv260.py"], helper.__file__, "exec"),
         helper.__dict__)
    phase("Read-only board, kernel and overlay preflight")
    info = helper.inspection()
    print(json.dumps(info, indent=2), flush=True)
    helper.require_board(info)
    helper.require_kernel_ready()
    overlays = helper.active_overlays()
    starter = [{"name": "k26-starter-kits_image_1",
                "status": "applied"}]
    require(overlays in ([], starter),
            "Unexpected overlay; stop. No overlay has been unloaded.")
    phase("Verify the existing private image against its old manifest")
    manifest = json.loads(safe_read(ORIGINAL / "manifest.json", 1048576))
    image_name = "build/siliconsign.bit.bin"
    image = verified(safe_read(ORIGINAL / image_name),
                     manifest["files"][image_name])
    files[image_name] = image
    if TARGET.exists() or TARGET.is_symlink():
        for name, data in files.items():
            require(safe_read(TARGET / name) == data,
                    "Existing recovery target differs; preserved")
        phase("Matching target exists: INSPECTION ONLY; no reload")
        return
    phase("Check installed clock and AFI platform drivers")
    modules = {"xilinx_fclk": "xilinx_fclk", "afi-fpga": "xilinx_afi"}
    for driver, present in helper.required_drivers().items():
        if not present:
            require(driver in modules, "Unknown required driver")
            subprocess.run(["modprobe", modules[driver]], check=True)
    helper.require_platform_drivers()
    helper.require_kernel_ready()
    require(helper.active_overlays() == overlays,
            "Overlay state changed during preflight; stopped")
    phase("Create a separate private recovery directory")
    # ORIGINAL inspection already checked /opt without following symlinks.
    TARGET.mkdir(mode=0o700)
    (TARGET / "build").mkdir(mode=0o700)
    for name, data in files.items():
        write_new(TARGET / name, data)
    if overlays == starter:
        phase("Unload only the confirmed starter-kit application")
        subprocess.run(["xmutil", "unloadapp"], check=True)
    require(helper.active_overlays() == [],
            "An overlay remains; stopped before recovery image load")
    helper.require_kernel_ready()
    phase("Load corrected overlay only; NO PL MMIO or payment bridge")
    result = helper.deploy(TARGET / "build", True, load_only=True)
    print(json.dumps(result, indent=2), flush=True)
    phase("Load-only completed: UNVERIFIED. Original installation kept.")

try:
    run(PAYLOAD, PAYLOAD_SHA256)
except Exception as error:
    print("[PayGuard r4] STOPPED: " + str(error), file=sys.stderr,
          flush=True)
    raise SystemExit(1)
"""


def build_artifact(root: Path = ROOT, revision: str = CURRENT_REVISION) -> str:
    if not re.fullmatch(r"r[1-9][0-9]*", revision):
        raise ValueError("Invalid recovery revision")
    recovery = root / "hardware/recovery" / revision
    sources = {
        "deploy_kv260.py": root / "hardware/scripts/deploy_kv260.py",
        "build/siliconsign.dts": recovery / "siliconsign.dts",
        "build/siliconsign.dtbo": recovery / "siliconsign.dtbo",
        "manifest.json": recovery / "manifest.json",
    }
    manifest = json.loads(sources["manifest.json"].read_bytes())
    records = {}
    for name, path in sources.items():
        data = path.read_bytes()
        digest = hashlib.sha256(data).hexdigest()
        if name.startswith("build/"):
            recorded = manifest["files"][f"recovery/{revision}/" + path.name]
            if recorded != {"bytes": len(data), "sha256": digest}:
                raise ValueError(f"{revision} overlay differs from its manifest")
        encoding = "base64" if path.suffix == ".dtbo" else "utf-8"
        records[name] = {
            "bytes": len(data),
            "sha256": digest,
            "encoding": encoding,
            "data": base64.b64encode(data).decode("ascii")
            if encoding == "base64"
            else data.decode("utf-8"),
        }
    packed = gzip.compress(json.dumps(records, sort_keys=True).encode(), mtime=0)
    encoded = "\n".join(textwrap.wrap(base64.b64encode(packed).decode(), 76))
    digest = hashlib.sha256(packed).hexdigest()
    terminator = "PAYGUARD_RECOVERY_" + revision.upper()
    installer = INSTALLER.replace(CURRENT_REVISION, revision)
    script = (
        f"sudo python3 - <<'{terminator}'\n"
        f"PAYLOAD_SHA256 = (\n    '{digest}'\n)\n"
        f'PAYLOAD = """\n{encoded}\n"""\n' + installer.lstrip() + terminator + "\n"
    )
    if max(map(len, script.splitlines())) > 76:
        raise ValueError("Paste artifact contains a line longer than 76 characters")
    return script


if __name__ == "__main__":
    destination = (
        ROOT / "hardware/recovery" / CURRENT_REVISION / f"PayGuard-KV260-{CURRENT_REVISION}.txt"
    )
    destination.write_text(build_artifact())
    print(destination)  # noqa: T201 -- report the generated public artifact path.
