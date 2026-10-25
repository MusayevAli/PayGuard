"""Start the existing desk with explicit hardware authority and persistent state.

This launcher never creates a VM, resets the FPGA, deletes a database, disables
TLS, creates signing keys, or falls back from hardware to a software signer.
"""
# ruff: noqa: T201 -- this is an operator-facing CLI; private values are never printed.

from __future__ import annotations

import argparse
import json
import os
import re
import secrets
import stat
import sys
import threading
import webbrowser
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ROLES = ("admin", "controller", "response")


class SetupError(RuntimeError):
    pass


def _private_text(path: Path, *, limit: int = 4096) -> str:
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
        with os.fdopen(fd, "r", encoding="ascii") as source:
            info = os.fstat(source.fileno())
            if (
                not stat.S_ISREG(info.st_mode)
                or info.st_uid != os.geteuid()
                or info.st_mode & 0o077
                or info.st_size > limit
            ):
                raise SetupError(
                    f"{path} must be a private regular file owned by this user (chmod 600)."
                )
            return source.read(limit + 1).strip()
    except OSError as exc:
        raise SetupError(f"Cannot read private file {path}: {exc.strerror}") from exc


def _create_private(path: Path, value: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if path.parent.is_symlink():
        raise SetupError(f"Private directory must not be a symlink: {path.parent}")
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    except FileExistsError:
        raise SetupError(f"Refusing to overwrite existing private file {path}") from None
    with os.fdopen(fd, "w", encoding="ascii") as destination:
        destination.write(value + "\n")
        destination.flush()
        os.fsync(destination.fileno())


def controller_token(path: Path) -> str:
    """Create once, reuse on every subsequent launch; never print the token."""
    if not path.exists() and not path.is_symlink():
        try:
            _create_private(path, secrets.token_urlsafe(32))
        except SetupError:
            if not path.exists():
                raise
    token = _private_text(path)
    if len(token) < 32 or any(ch.isspace() for ch in token):
        raise SetupError("Controller token must contain at least 32 non-whitespace characters.")
    return token


def configure_api_key(private_dir: Path, explicit_file: Path | None = None) -> None:
    """Load a private API key before creating clients; never call or log the API.

    An explicit file (flag or PAYGUARD_ANTHROPIC_KEY_FILE) overrides credential
    environment variables. Existing credentials override the automatic file.
    """
    if explicit_file is None and (
        os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN")
    ):
        return
    path = (
        explicit_file.expanduser() if explicit_file is not None else private_dir / "anthropic.key"
    )
    if explicit_file is None and not path.exists() and not path.is_symlink():
        return  # Structured invoice flows can still run without model credentials.
    try:
        value = _private_text(path)
    except (SetupError, UnicodeError):
        raise SetupError(
            "Anthropic API-key file must exist and be a private regular file owned by this user (chmod 600)."
        ) from None
    if not re.fullmatch(r"[A-Za-z0-9_-]+", value):
        raise SetupError(
            "Anthropic API-key file must contain one nonempty key using letters, digits, hyphens or underscores."
        )
    os.environ["ANTHROPIC_API_KEY"] = value
    # An explicitly selected API key must not compete with a stale bearer token.
    os.environ.pop("ANTHROPIC_AUTH_TOKEN", None)


def split_role_bundle(bundle: Path, directory: Path) -> dict[str, Path]:
    """Split explicitly supplied existing image keys, without generating or rotating them."""
    try:
        data = json.loads(_private_text(bundle))
        if not isinstance(data, dict) or set(data) != set(ROLES):
            raise ValueError
        keys = {role: bytes.fromhex(data[role]) for role in ROLES}
        if (
            any(len(key) != 32 or not any(key) for key in keys.values())
            or len(set(keys.values())) != 3
        ):
            raise ValueError
    except (ValueError, TypeError):
        raise SetupError(
            "The private bundle must contain exactly admin/controller/response with distinct, nonzero 32-byte hex keys."
        ) from None
    paths = {role: directory / f"{role}.key" for role in ROLES}
    # Check all existing files before creating any missing file.
    for role, path in paths.items():
        if path.exists() or path.is_symlink():
            try:
                if bytes.fromhex(_private_text(path)) != keys[role]:
                    raise ValueError
            except ValueError:
                raise SetupError(
                    f"Existing {role} role key differs; no files were overwritten."
                ) from None
    for role, path in paths.items():
        if not path.exists():
            _create_private(path, keys[role].hex())
    return paths


def ensure_world(world: Path, *, fx_cache: Path, generate: bool) -> None:
    required = (
        "world.jsonl",
        "suppliers.json",
        "people.json",
        "fx_rates.json",
        "answer_key.json",
        "invoices_truth.json",
    )
    if all((world / name).is_file() for name in required):
        return
    if world.exists() and any(world.iterdir()):
        raise SetupError(
            f"{world} is incomplete. Refusing to overwrite it; select a new --world directory or restore the missing files."
        )
    if not generate:
        raise SetupError(
            "World is missing. Run without --check to generate it, or supply --world with an existing complete world."
        )
    from sim.fx import FxFetchError, build_fx_table, fetch_ecb_history
    from sim.generate_world import generate as generate_world

    print(
        "Preparing the seeded invoice world with verified, cached official ECB rates...", flush=True
    )
    try:
        # Validate official rates before the generator creates any output tree.
        build_fx_table(fetch_ecb_history(cache_path=fx_cache))
        generate_world(42, world, fx_cache=fx_cache)
    except FxFetchError as exc:
        raise SetupError(
            f"World generation stopped: {exc}\n"
            "TLS verification remains enabled. Use your trusted CA configuration or place the official ECB eurofxref-hist.zip at --fx-cache. No synthetic exchange rates were substituted."
        ) from exc


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description=__doc__)
    result.add_argument("--mode", choices=("hw", "stub"), default=os.environ.get("SIGNER", "hw"))
    result.add_argument(
        "--device",
        default=os.environ.get("PAYGUARD_SERIAL_DEVICE"),
        help="Existing USB UART, e.g. /dev/cu.usbserial-...",
    )
    result.add_argument(
        "--keys",
        type=Path,
        help="Existing private three-role bundle matching the programmed bitstream; split once into private/roles",
    )
    for role in ROLES:
        result.add_argument(
            f"--{role}-key", type=Path, default=os.environ.get(f"PAYGUARD_{role.upper()}_KEY_FILE")
        )
    result.add_argument("--private-dir", type=Path, default=ROOT / "private")
    result.add_argument(
        "--api-key-file",
        type=Path,
        default=os.environ.get("PAYGUARD_ANTHROPIC_KEY_FILE"),
        help="Private Anthropic API-key file; overrides credential environment variables. Otherwise existing credentials or private/anthropic.key are used.",
    )
    result.add_argument(
        "--db",
        type=Path,
        default=os.environ.get("PAYGUARD_DB"),
        help="Persistent database; default runtime/payguard-MODE.db",
    )
    result.add_argument(
        "--world", type=Path, default=os.environ.get("PAYGUARD_WORLD_DIR", ROOT / "sim" / "world")
    )
    result.add_argument("--fx-cache", type=Path, default=ROOT / ".cache" / "eurofxref-hist.zip")
    result.add_argument("--port", type=int, default=8000)
    result.add_argument(
        "--initialize",
        action="store_true",
        help="Explicitly initialize a fresh, verified FPGA session; never a recovery/reset shortcut",
    )
    result.add_argument(
        "--provision",
        action="store_true",
        help="Explicitly enroll reviewed bootstrap vendor accounts through the trusted setup path",
    )
    result.add_argument(
        "--check",
        action="store_true",
        help="Check local configuration; do not contact hardware, create a DB, generate a world, or start a server",
    )
    result.add_argument(
        "--open", action="store_true", help="Open the local dashboard in the default browser"
    )
    return result


def configure(args: argparse.Namespace) -> tuple[Path, Path | None]:
    if args.mode not in ("hw", "stub"):
        raise SetupError("SIGNER must be hw or stub.")
    if not 1 <= args.port <= 65535:
        raise SetupError("Port must be between 1 and 65535.")
    if args.mode == "stub" and (args.initialize or args.provision):
        raise SetupError(
            "--initialize/--provision are explicit hardware setup flags; omit them for stub mode."
        )
    os.environ["SIGNER"] = args.mode
    # Environment remnants cannot silently reinitialize or reprovision the device.
    os.environ["PAYGUARD_HW_INITIALIZE"] = "1" if args.initialize else "0"
    os.environ["PAYGUARD_HW_PROVISION"] = "1" if args.provision else "0"
    os.environ["PAYGUARD_AUTOSTART"] = "0"
    args.private_dir = args.private_dir.expanduser().resolve()
    configure_api_key(args.private_dir, args.api_key_file)
    if args.mode == "hw":
        if not args.device:
            raise SetupError(
                "Hardware mode needs --device /dev/cu.YOUR_KRIA_UART (or PAYGUARD_SERIAL_DEVICE). No software fallback was started."
            )
        device = Path(args.device).expanduser()
        if not device.exists() or not stat.S_ISCHR(device.stat().st_mode):
            raise SetupError(
                f"USB UART character device is unavailable: {device}. Finish the Kria bridge handoff and close the terminal emulator first."
            )
        os.environ["PAYGUARD_SERIAL_DEVICE"] = str(device)
        from signer.authority import load_role_key

        if args.keys:
            paths = split_role_bundle(args.keys.expanduser(), args.private_dir / "roles")
        else:
            paths = {
                role: (getattr(args, f"{role}_key") or args.private_dir / "roles" / f"{role}.key")
                for role in ROLES
            }
        loaded = []
        for role, path in paths.items():
            try:
                loaded.append(load_role_key(path, role))
            except (OSError, ValueError) as exc:
                raise SetupError(
                    f"Cannot use the private {role} key: {exc}. Supply keys matching the programmed FPGA; the launcher never generates signing keys."
                ) from exc
            os.environ[f"PAYGUARD_{role.upper()}_KEY_FILE"] = str(path.resolve())
        if len(set(loaded)) != 3:
            raise SetupError("Admin, controller and response keys must be distinct.")
    token_path = None
    if not os.environ.get("PAYGUARD_CONTROLLER_TOKEN"):
        token_path = args.private_dir / "controller-token"
        os.environ["PAYGUARD_CONTROLLER_TOKEN"] = controller_token(token_path)
    elif len(os.environ["PAYGUARD_CONTROLLER_TOKEN"]) < 32:
        raise SetupError("PAYGUARD_CONTROLLER_TOKEN must contain at least 32 characters.")
    db = (args.db or ROOT / "runtime" / f"payguard-{args.mode}.db").expanduser().resolve()
    world = args.world.expanduser().resolve()
    ensure_world(world, fx_cache=args.fx_cache.expanduser().resolve(), generate=not args.check)
    if not args.check:
        db.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.environ["PAYGUARD_DB"] = str(db)
    os.environ["PAYGUARD_WORLD_DIR"] = str(world)
    return db, token_path


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    try:
        db, token_path = configure(args)
    except (SetupError, OSError) as exc:
        print(f"PayGuard setup: {exc}", file=sys.stderr)
        return 2
    print(f"Signer: {args.mode}; bank: simulated; database: {db}", flush=True)
    if token_path is not None:
        print(
            f"Controller credential is stored privately at {token_path}. Paste it into the dashboard's controller field.",
            flush=True,
        )
        if sys.platform == "darwin":
            import shlex

            print(f"Copy it locally with: pbcopy < {shlex.quote(str(token_path))}", flush=True)
    if args.check:
        print("Local setup checks passed. No hardware connection or server was started.")
        return 0
    if args.mode == "hw":
        print(
            f"Explicit setup: initialize={args.initialize}, provision={args.provision}. Preserve the FPGA, bridge and host journals between launches.",
            flush=True,
        )
    else:
        print(
            "Software signer selected explicitly; this run is not a physical FPGA demonstration.",
            flush=True,
        )
    url = f"http://127.0.0.1:{args.port}"
    print(f"Dashboard: {url}", flush=True)
    if args.open:
        timer = threading.Timer(2.0, lambda: webbrowser.open(url))
        timer.daemon = True
        timer.start()
    import uvicorn

    uvicorn.run(
        "api.main:app", host="127.0.0.1", port=args.port, ws_ping_interval=15, ws_ping_timeout=30
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
