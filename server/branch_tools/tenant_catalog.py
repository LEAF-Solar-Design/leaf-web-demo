"""Tenant-local immutable packages; signature creation belongs to the publisher."""
from __future__ import annotations

from dataclasses import dataclass
from collections import OrderedDict
from datetime import datetime, timezone
import hashlib
import json
import os
import re
from pathlib import Path
import shutil
import tempfile
from threading import Lock
from uuid import UUID

import tool_envelopes

PACKAGE_LIMIT = 8 * 1024 * 1024
ENVELOPE_LIMIT = 64 * 1024
REVOCATIONS_LIMIT = 1024 * 1024
VERSION_SCAN_LIMIT = 2000  # bounded per-tool scan; names only, no file reads
PLUGIN_CHECK_FILE = ".plugin-check.json"
PLUGIN_CHECK_INTERVAL_S = 300
PLUGIN_CHECK_LIMIT = 1024
_PLUGIN_CHECK_CACHE_MAX = 10000
_PLUGIN_CHECK_CACHE: OrderedDict[str, datetime] = OrderedDict()
_PLUGIN_CHECK_LOCK = Lock()
_READ_ERRORS = (OSError, ValueError, UnicodeError, RecursionError)


class TenantCatalogError(RuntimeError):
    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class TenantEntry:
    name: str
    version: str
    package_digest: str
    path: Path


def _safe_path(path: Path) -> None:
    for part in (path, *path.parents):
        if part.is_symlink() or getattr(part, "is_junction", lambda: False)():
            raise TenantCatalogError("unsafe_path")


def _tenant(root: str | Path, tenant_id: str) -> Path:
    # Validate before joining, including before probing any caller-controlled path.
    try:
        valid = isinstance(tenant_id, str) and str(UUID(tenant_id)) == tenant_id
    except ValueError:
        valid = False
    if not valid:
        raise TenantCatalogError("invalid_tenant")
    if not root:
        raise TenantCatalogError("catalog_unavailable")
    path = Path(root).absolute() / tenant_id
    _safe_path(path)
    return path


def _version(root: str | Path, tenant_id: str, name: str, version: str) -> Path:
    if not tool_envelopes.valid_identity(name, version):
        raise TenantCatalogError("invalid_identity")
    path = _tenant(root, tenant_id) / name / version
    _safe_path(path)
    return path


def _read(path: Path, limit: int) -> bytes:
    _safe_path(path)
    return tool_envelopes._read(path, limit)


def _json(raw: bytes):
    return json.loads(raw.decode("utf-8"), object_pairs_hook=tool_envelopes._audience_pairs,
                      parse_constant=tool_envelopes._audience_constant)


def _envelope(value: object, tenant_id: str, name: str, version: str) -> bool:
    if isinstance(value, dict) and value.get("schema") == "leaf.tool-envelope.v2":
        if value.get("audience_tenant") != tenant_id:
            return False
        value = dict(value)
        value.pop("audience_tenant")
        value["schema"] = "leaf.tool-envelope.v1"
    return tool_envelopes._envelope(value, name, version)


def _revocations(directory: Path) -> tuple[bytes | None, set[str]]:
    path = directory / "revocations.json"
    _safe_path(path)
    if not path.exists():
        return None, set()
    raw = _read(path, REVOCATIONS_LIMIT)
    value = _json(raw)
    if not tool_envelopes._revocations(value):
        raise TenantCatalogError("invalid_revocations")
    return raw, {entry["package_digest"] for entry in value["entries"]}


def _entry(root: str | Path, tenant_id: str, name: str, version: str,
           revoked: set[str]) -> tuple[dict, bytes, Path] | None:
    try:
        path = _version(root, tenant_id, name, version)
        raw = _read(path / "envelope.json", ENVELOPE_LIMIT)
        value = _json(raw)
        if not _envelope(value, tenant_id, name, version):
            return None
        digest = value["package"]["package_digest"]
        if digest in revoked or _read(path / "digest.txt", 65).decode("ascii").strip() != digest:
            return None
        package = path / "package.zip"
        zip_digest = _read(path / "package.sha256", 65).decode("ascii").strip()
        if hashlib.sha256(_read(package, PACKAGE_LIMIT)).hexdigest() != zip_digest:
            return None
        return value, raw, package
    except (*_READ_ERRORS, TenantCatalogError):
        return None


def list_versions(root: str | Path, tenant_id: str, *, include_revoked: bool = False) -> list[dict]:
    try:
        directory = _tenant(root, tenant_id)
        _, revoked = _revocations(directory)
        result = []
        for name in tool_envelopes._directories(directory, 200):
            if not tool_envelopes.valid_identity(name.name, "0.0.0"):
                continue
            _safe_path(name)
            for version in tool_envelopes._directories(name, 50):
                entry = _entry(root, tenant_id, name.name, version.name,
                               set() if include_revoked else revoked)
                if entry is not None:
                    value = entry[0]
                    result.append({"name": name.name, "version": version.name,
                                   "package_digest": value["package"]["package_digest"],
                                   "signed_at": value["signed_at"]})
                    if include_revoked:
                        result[-1]["revoked"] = value["package"]["package_digest"] in revoked
        return sorted(result, key=lambda item: (item["name"], item["version"]))
    except (*_READ_ERRORS, TenantCatalogError):
        return []


def version_count(root, tenant_id, name, limit) -> int:
    if not tool_envelopes.valid_identity(name, "0.0.0"):
        raise TenantCatalogError("invalid_identity")
    path = _tenant(root, tenant_id) / name
    _safe_path(path)
    return sum(1 for _ in tool_envelopes._directories(path, limit))


def _semver(version: str) -> tuple[int, int, int]:
    major, minor, patch = version.split(".")
    return int(major), int(minor), int(patch)


def list_latest_versions(root: str | Path, tenant_id: str) -> list[dict]:
    """Newest non-revoked, verifiable version per tool; bounded scan, fails closed to []."""
    try:
        directory = _tenant(root, tenant_id)
        _, revoked = _revocations(directory)
        result = []
        for name in tool_envelopes._directories(directory, 200):
            if not tool_envelopes.valid_identity(name.name, "0.0.0"):
                continue
            _safe_path(name)
            versions = [version.name for version in tool_envelopes._directories(name, VERSION_SCAN_LIMIT)
                        if tool_envelopes.valid_identity(name.name, version.name)]
            for version in sorted(versions, key=_semver, reverse=True):
                entry = _entry(root, tenant_id, name.name, version, revoked)
                if entry is not None:
                    value = entry[0]
                    result.append({"name": name.name, "version": version,
                                   "package_digest": value["package"]["package_digest"],
                                   "signed_at": value["signed_at"]})
                    break
        return sorted(result, key=lambda item: item["name"])
    except (*_READ_ERRORS, TenantCatalogError):
        return []


def _visible_entry(root: str | Path, tenant_id: str, name: str, version: str):
    try:
        _version(root, tenant_id, name, version)
        _, revoked = _revocations(_tenant(root, tenant_id))
        return _entry(root, tenant_id, name, version, revoked)
    except (*_READ_ERRORS, TenantCatalogError):
        return None


def envelope_bytes(root: str | Path, tenant_id: str, name: str, version: str) -> bytes | None:
    entry = _visible_entry(root, tenant_id, name, version)
    return entry[1] if entry else None


def package_path(root: str | Path, tenant_id: str, name: str, version: str) -> Path | None:
    entry = _visible_entry(root, tenant_id, name, version)
    return entry[2] if entry else None


def revocations_bytes(root: str | Path, tenant_id: str) -> bytes | None:
    try:
        return _revocations(_tenant(root, tenant_id))[0]
    except (*_READ_ERRORS, TenantCatalogError):
        return None


def _write(path: Path, data: bytes) -> None:
    with path.open("xb") as stream:
        stream.write(data)
        stream.flush()
        os.fsync(stream.fileno())


def _sync_directory(path: Path) -> None:
    # Windows does not support opening directories through os.open.
    if os.name != "nt":
        descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)


def plugin_check_at(root: str | Path, tenant_id: str) -> str | None:
    try:
        value = _json(_read(_tenant(root, tenant_id) / PLUGIN_CHECK_FILE, PLUGIN_CHECK_LIMIT))
        if not isinstance(value, dict):
            return None
        stamp = value["last_plugin_check_at"]
        if not isinstance(stamp, str) or re.fullmatch(
                r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z", stamp) is None:
            return None
        datetime.strptime(stamp, "%Y-%m-%dT%H:%M:%SZ")
        return stamp
    except (*_READ_ERRORS, TenantCatalogError, KeyError, TypeError):
        return None


def _cache_plugin_check(tenant_id: str, stamp: datetime) -> None:
    with _PLUGIN_CHECK_LOCK:
        _PLUGIN_CHECK_CACHE[tenant_id] = stamp
        _PLUGIN_CHECK_CACHE.move_to_end(tenant_id)
        while len(_PLUGIN_CHECK_CACHE) > _PLUGIN_CHECK_CACHE_MAX:
            _PLUGIN_CHECK_CACHE.popitem(last=False)


def record_plugin_check(root: str | Path, tenant_id: str, now: datetime) -> bool:
    temporary = None
    try:
        with _PLUGIN_CHECK_LOCK:
            cached = _PLUGIN_CHECK_CACHE.get(tenant_id)
        if cached is not None and 0 <= (now - cached).total_seconds() < PLUGIN_CHECK_INTERVAL_S:
            return False
        stored = plugin_check_at(root, tenant_id)
        if stored is not None:
            stamp = datetime.strptime(stored, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
            if 0 <= (now - stamp).total_seconds() < PLUGIN_CHECK_INTERVAL_S:
                _cache_plugin_check(tenant_id, stamp)
                return False
        directory = _tenant(root, tenant_id)
        directory.mkdir(parents=True, exist_ok=True)
        _safe_path(directory)
        destination = directory / PLUGIN_CHECK_FILE
        _safe_path(destination)
        temporary = Path(tempfile.mkdtemp(prefix=".plugin-check-", dir=directory))
        source = temporary / PLUGIN_CHECK_FILE
        stamp = now.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        _write(source, json.dumps({"last_plugin_check_at": stamp}).encode("utf-8"))
        _safe_path(destination)
        os.replace(source, destination)
        _sync_directory(directory)
        _cache_plugin_check(tenant_id, now)
        return True
    except (*_READ_ERRORS, TenantCatalogError, KeyError, TypeError):
        return False
    finally:
        if temporary is not None:
            try:
                shutil.rmtree(temporary)
            except _READ_ERRORS:
                pass


def write_version(root: str | Path, tenant_id: str, name: str, version: str,
                  package: bytes, envelope: bytes, package_digest: str) -> TenantEntry:
    destination = _version(root, tenant_id, name, version)
    if len(package) > PACKAGE_LIMIT or len(envelope) > ENVELOPE_LIMIT:
        raise TenantCatalogError("size_limit")
    if not isinstance(package_digest, str) or re.fullmatch(r"[0-9a-f]{64}", package_digest) is None:
        raise TenantCatalogError("invalid_digest")
    if destination.exists():
        raise TenantCatalogError("version_exists")
    directory = _tenant(root, tenant_id)
    destination.parent.mkdir(parents=True, exist_ok=True)
    _safe_path(destination)
    # Serialize publishers of this identity, including an empty existing version.
    lock = directory / f".publish-{name}-{version}.lock"
    _safe_path(lock)
    try:
        descriptor = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError as exc:
        raise TenantCatalogError("version_exists") from exc
    temporary = None
    try:
        os.close(descriptor)
        if destination.exists():
            raise TenantCatalogError("version_exists")
        temporary = Path(tempfile.mkdtemp(prefix=".publish-", dir=directory))
        digest = package_digest
        _write(temporary / "package.zip", package)
        _write(temporary / "envelope.json", envelope)
        _write(temporary / "digest.txt", (digest + "\n").encode("ascii"))
        _write(temporary / "package.sha256", (hashlib.sha256(package).hexdigest() + "\n").encode("ascii"))
        _sync_directory(temporary)
        _safe_path(destination)
        if destination.exists():
            raise TenantCatalogError("version_exists")
        temporary.rename(destination)
        _sync_directory(destination.parent)
        _sync_directory(directory)
        return TenantEntry(name, version, digest, destination)
    finally:
        if temporary is not None and temporary.exists():
            shutil.rmtree(temporary)
        lock.unlink()


def write_revocations(root: str | Path, tenant_id: str, data: bytes) -> None:
    directory = _tenant(root, tenant_id)
    if len(data) > REVOCATIONS_LIMIT:
        raise TenantCatalogError("size_limit")
    destination = directory / "revocations.json"
    _safe_path(destination)
    directory.mkdir(parents=True, exist_ok=True)
    _safe_path(directory)
    temporary = Path(tempfile.mkdtemp(prefix=".revocations-", dir=directory))
    try:
        source = temporary / "revocations.json"
        _write(source, data)
        _safe_path(destination)
        os.replace(source, destination)
        _sync_directory(directory)
    finally:
        shutil.rmtree(temporary)
