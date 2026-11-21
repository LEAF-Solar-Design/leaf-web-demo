"""Read-only distribution catalog. Signing and entitlement checks live elsewhere."""
from __future__ import annotations

import json
import logging
import os
import re
from datetime import datetime
from itertools import islice
from pathlib import Path
from threading import Lock
from uuid import UUID

import customization_flags

SERVER_DIR = Path(__file__).resolve().parent

_LOG = logging.getLogger(__name__)
_CACHE: dict[Path, Catalog] = {}
_LOCK = Lock()
_NAME = re.compile(r"[a-z][a-z0-9]*(-[a-z0-9]+)*")
_VERSION = re.compile(r"(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)")
_ENVELOPE_KEYS = {"schema", "package", "tiers", "signed_at", "key_id", "alg", "signature"}
_REVOCATION_KEYS = {"schema", "issued_at", "key_id", "alg", "entries", "signature"}


def valid_identity(name: str, version: str) -> bool:
    return bool(3 <= len(name) <= 64 and _NAME.fullmatch(name) and _VERSION.fullmatch(version))


def _matches(pattern: str, value: object) -> bool:
    return isinstance(value, str) and re.fullmatch(pattern, value) is not None


def _utc(value: object) -> bool:
    if not _matches(r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}(\.[0-9]+)?Z", value):
        return False
    try:
        datetime.fromisoformat(value[:-1] + "+00:00")
        return True
    except ValueError:
        return False


def _signed(value: dict) -> bool:
    return (_matches(r"[0-9a-f]{16}", value.get("key_id"))
            and value.get("alg") == "ECDSA_P256_SHA256"
            and _matches(r"(?:[0-9a-f]{2}){8,80}", value.get("signature")))


def _envelope(value: object, name: str, version: str) -> bool:
    if not isinstance(value, dict) or set(value) != _ENVELOPE_KEYS:
        return False
    package = value["package"]
    tiers = value["tiers"]
    return (value["schema"] == "leaf.tool-envelope.v1"
            and isinstance(package, dict) and set(package) == {"name", "version", "package_digest"}
            and package["name"] == name and package["version"] == version
            and _matches(r"[0-9a-f]{64}", package["package_digest"])
            and isinstance(tiers, list) and bool(tiers)
            and all(_matches(r"[a-z][a-z0-9_]{1,40}", tier) for tier in tiers)
            and len(set(tiers)) == len(tiers)
            and _utc(value["signed_at"]) and _signed(value))


def _revocations(value: object) -> bool:
    if not (isinstance(value, dict) and set(value) == _REVOCATION_KEYS
            and value["schema"] == "leaf.tool-revocations.v1"
            and _utc(value["issued_at"]) and _signed(value)
            and isinstance(value["entries"], list)):
        return False
    for entry in value["entries"]:
        if not (isinstance(entry, dict)
                and set(entry) == {"package_digest", "name", "version", "reason", "revoked_at"}
                and isinstance(entry["name"], str) and isinstance(entry["version"], str)
                and valid_identity(entry["name"], entry["version"])
                and _matches(r"[0-9a-f]{64}", entry["package_digest"])
                and isinstance(entry["reason"], str) and _utc(entry["revoked_at"])):
            return False
    return True


def _read(path: Path, limit: int) -> bytes:
    if path.is_symlink() or not path.is_file() or path.stat().st_size > limit:
        raise ValueError("invalid catalog file")
    with path.open("rb") as stream:
        raw = stream.read(limit + 1)
    if len(raw) > limit:
        raise ValueError("catalog file exceeds limit")
    return raw


def _directories(path: Path, limit: int):
    if not path.is_dir():
        return
    with os.scandir(path) as entries:
        yield from islice((Path(entry.path) for entry in entries
                          if entry.is_dir(follow_symlinks=False)), limit)


def _audience_pairs(pairs: list[tuple[str, object]]) -> dict:
    value = {}
    for key, item in pairs:
        if key in value:
            raise ValueError("duplicate audience key")
        value[key] = item
    return value


def _audience_constant(value: str):
    raise ValueError("invalid audience constant")


def _audience(path: Path) -> frozenset[str] | str:
    value = json.loads(_read(path, 4096).decode("utf-8"),
                       object_pairs_hook=_audience_pairs, parse_constant=_audience_constant)
    if not (isinstance(value, dict) and set(value) == {"schema", "tenants"}
            and value["schema"] == "leaf.tool-audience.v1"):
        raise ValueError("invalid audience")
    tenants = value["tenants"]
    if tenants == "internal":
        return "internal"
    if not (isinstance(tenants, list) and 1 <= len(tenants) <= 1000
            and all(isinstance(tenant, str) and str(UUID(tenant)) == tenant for tenant in tenants)
            and len(set(tenants)) == len(tenants)):
        raise ValueError("invalid audience tenants")
    return frozenset(tenants)


def _visible(audience: frozenset[str] | str, tenant_id: str) -> bool:
    return tenant_id in (customization_flags.internal_tenants()
                         if audience == "internal" else audience)


class Catalog:
    def __init__(self, root: Path):
        self._entries: dict[tuple[str, str], tuple[dict, bytes, Path, frozenset[str] | str]] = {}
        self._revocations: bytes | None = None
        for name_dir in _directories(root, 200):
            if not (3 <= len(name_dir.name) <= 64 and _NAME.fullmatch(name_dir.name)):
                _LOG.warning("Skipping published tool name directory %s", name_dir.name)
                continue
            for version_dir in _directories(name_dir, 50):
                try:
                    if not valid_identity(name_dir.name, version_dir.name):
                        raise ValueError("invalid identity")
                    raw = _read(version_dir / "envelope.json", 64 * 1024)
                    value = json.loads(raw)
                    if not _envelope(value, name_dir.name, version_dir.name):
                        raise ValueError("invalid envelope")
                    package = version_dir / "package.zip"
                    if package.is_symlink() or not package.is_file() or package.stat().st_size > 8 * 1024 * 1024:
                        raise ValueError("invalid package")
                    audience = _audience(version_dir / "audience.json")
                    self._entries[(name_dir.name, version_dir.name)] = (value, raw, package, audience)
                except (OSError, ValueError, UnicodeError, RecursionError):
                    _LOG.warning("Skipping published tool entry %s/%s", name_dir.name, version_dir.name)
        revocations = root / "revocations.json"
        if revocations.exists() or revocations.is_symlink():
            try:
                raw = _read(revocations, 1024 * 1024)
                if not _revocations(json.loads(raw)):
                    raise ValueError("invalid revocation list")
                self._revocations = raw
            except (OSError, ValueError, UnicodeError, RecursionError):
                _LOG.warning("Ignoring invalid published tool revocation list")

    def list(self, tenant_id: str) -> list[dict]:
        return [{"name": name, "version": version,
                 "package_digest": value["package"]["package_digest"],
                 "tiers": list(value["tiers"]), "signed_at": value["signed_at"]}
                for (name, version), (value, _, _, audience) in sorted(self._entries.items())
                if _visible(audience, tenant_id)]

    def envelope_bytes(self, name: str, version: str, tenant_id: str) -> bytes | None:
        entry = self._entries.get((name, version))
        return entry[1] if entry and _visible(entry[3], tenant_id) else None

    def package_path(self, name: str, version: str, tenant_id: str) -> Path | None:
        entry = self._entries.get((name, version))
        return entry[2] if entry and _visible(entry[3], tenant_id) else None

    def revocations_bytes(self) -> bytes | None:
        return self._revocations


def load_catalog(root: str | Path) -> Catalog:
    path = Path(root).resolve()
    with _LOCK:
        if path not in _CACHE:
            catalog = Catalog(path)
            if len(_CACHE) >= 4:
                del _CACHE[next(iter(_CACHE))]
            _CACHE[path] = catalog
        return _CACHE[path]


def published_catalog() -> Catalog:
    return load_catalog(os.environ.get("LEAF_PUBLISHED_TOOLS_DIR", str(SERVER_DIR / "published_tools")))
