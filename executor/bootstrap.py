"""Materialize approved ECS secret values into a task-local ephemeral volume.

Only this short-lived init container receives raw secret environment values.
The control plane, reaper, executor supervisor, and restricted children receive
file paths in a shared task volume instead.
"""
from __future__ import annotations

import argparse
import base64
import binascii
import errno
import json
import os
from pathlib import Path
import stat
import uuid
from typing import Mapping, MutableMapping


class BootstrapError(RuntimeError):
    pass


CONTROL_FILES = {
    "LEAF_INSTANT_CONTROL_DATABASE_URL": ("control-database-url.txt", "text"),
    "LEAF_INSTANT_CONTROL_REDIS_URL": ("control-redis-url.txt", "text"),
    "LEAF_INSTANT_CONTROL_API_SECRET": ("control-api-secret.txt", "text"),
    "LEAF_INSTANT_HOST_LIFECYCLE_SECRET": ("host-lifecycle-secret.txt", "text"),
    "LEAF_INSTANT_RUNTIME_CONTROL_SECRET": ("runtime-control-secret.txt", "text"),
    "LEAF_INSTANT_LEASE_SIGNING_SEED_B64": ("lease-signing-seed.bin", "seed"),
    "LEAF_INSTANT_RUNTIME_CLIENT_CA_PEM": ("runtime-client-ca.pem", "pem"),
    "LEAF_INSTANT_RUNTIME_CLIENT_CERT_PEM": ("runtime-client-cert.pem", "pem"),
    "LEAF_INSTANT_RUNTIME_CLIENT_KEY_PEM": ("runtime-client-key.pem", "pem"),
    "LEAF_INSTANT_CONTROL_TLS_CERT_PEM": ("control-server-cert.pem", "pem"),
    "LEAF_INSTANT_CONTROL_TLS_KEY_PEM": ("control-server-key.pem", "pem"),
    "LEAF_INSTANT_CONTROL_TLS_CLIENT_CA_PEM": ("control-client-ca.pem", "pem"),
}

EXECUTOR_FILES = {
    "LEAF_INSTANT_HOST_LIFECYCLE_SECRET": ("host-lifecycle-secret.txt", "text"),
    "LEAF_INSTANT_RUNTIME_CONTROL_SECRET": ("runtime-control-secret.txt", "text"),
    "LEAF_INSTANT_RUNTIME_TLS_CERT_PEM": ("runtime-server-cert.pem", "pem"),
    "LEAF_INSTANT_RUNTIME_TLS_KEY_PEM": ("runtime-server-key.pem", "pem"),
    "LEAF_INSTANT_RUNTIME_TLS_CLIENT_CA_PEM": ("runtime-client-ca.pem", "pem"),
    "LEAF_INSTANT_CONTROL_CLIENT_CA_PEM": ("control-client-ca.pem", "pem"),
    "LEAF_INSTANT_CONTROL_CLIENT_CERT_PEM": ("control-client-cert.pem", "pem"),
    "LEAF_INSTANT_CONTROL_CLIENT_KEY_PEM": ("control-client-key.pem", "pem"),
    "LEAF_INSTANT_CONTROL_JWKS_JSON": ("control-jwks.json", "json"),
}

PROFILES = {"control": CONTROL_FILES, "executor": EXECUTOR_FILES}


def materialize(
    profile: str,
    *,
    environ: MutableMapping[str, str] | None = None,
    output_directory: str | Path | None = None,
) -> dict[str, Path]:
    """Write one complete allowlisted profile with mode 0600 and atomic replace."""
    if profile not in PROFILES:
        raise BootstrapError("unknown instant execution secret profile")
    source = os.environ if environ is None else environ
    root = Path(output_directory or source.get("LEAF_INSTANT_SECRET_DIR", "/run/leaf/secrets"))
    if not root.is_absolute():
        raise BootstrapError("instant execution secret directory must be absolute")
    if root.exists() and root.is_symlink():
        raise BootstrapError("instant execution secret directory must not be a symlink")
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    _harden_directory(root)
    if not stat.S_ISDIR(root.stat().st_mode):
        raise BootstrapError("instant execution secret path is not a directory")

    prepared: dict[str, tuple[Path, bytes]] = {}
    for variable, (filename, kind) in PROFILES[profile].items():
        raw = source.get(variable)
        if not isinstance(raw, str) or not raw:
            raise BootstrapError(f"{variable} is required for the {profile} secret profile")
        prepared[variable] = (root / filename, _decode(variable, raw, kind))

    written: dict[str, Path] = {}
    try:
        for variable, (target, payload) in prepared.items():
            temporary = root / f".{target.name}.{uuid.uuid4().hex}.tmp"
            descriptor = os.open(
                temporary,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
                0o600,
            )
            try:
                with os.fdopen(descriptor, "wb") as stream:
                    stream.write(payload)
                    stream.flush()
                    os.fsync(stream.fileno())
                _harden_file(temporary)
                os.replace(temporary, target)
                written[variable] = target
            finally:
                if temporary.exists():
                    temporary.unlink()
    except OSError as exc:
        detail = exc.strerror or exc.__class__.__name__
        raise BootstrapError(f"instant execution secret materialization failed: {detail}") from exc
    finally:
        for variable in PROFILES[profile]:
            source.pop(variable, None)
    return written


def _harden_directory(root: Path) -> None:
    try:
        os.chmod(root, 0o700)
    except PermissionError as exc:
        if exc.errno != errno.EPERM:
            raise


def _harden_file(target: Path) -> None:
    try:
        os.chmod(target, 0o600)
    except PermissionError as exc:
        if exc.errno != errno.EPERM:
            raise


def _decode(variable: str, raw: str, kind: str) -> bytes:
    if len(raw.encode("utf-8")) > 256 * 1024:
        raise BootstrapError(f"{variable} exceeds the secret size limit")
    if kind == "seed":
        try:
            value = base64.b64decode(raw, validate=True)
        except (binascii.Error, ValueError) as exc:
            raise BootstrapError(f"{variable} is not canonical base64") from exc
        if len(value) != 32 or base64.b64encode(value).decode("ascii") != raw:
            raise BootstrapError(f"{variable} must encode exactly 32 bytes")
        return value
    if kind == "pem":
        if "\x00" in raw or "-----BEGIN " not in raw or "-----END " not in raw:
            raise BootstrapError(f"{variable} is not PEM material")
        return raw.encode("utf-8")
    if kind == "json":
        try:
            value = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise BootstrapError(f"{variable} is not valid JSON") from exc
        if not isinstance(value, dict):
            raise BootstrapError(f"{variable} must be a JSON object")
        return json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8")
    if kind == "text":
        if "\x00" in raw or raw != raw.strip():
            raise BootstrapError(f"{variable} is not canonical text")
        return raw.encode("utf-8")
    raise BootstrapError("unknown secret encoding")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("profile", choices=sorted(PROFILES))
    args = parser.parse_args()
    try:
        materialize(args.profile)
    except BootstrapError as exc:
        raise SystemExit(str(exc)) from exc


if __name__ == "__main__":
    main()
