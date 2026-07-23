"""Deterministic evidence-bundle manifest and offline verifier."""
from __future__ import annotations

import hashlib
import json
import uuid
from datetime import date, datetime
from decimal import Decimal
from typing import Any, Dict, Mapping

BUNDLE_VERSION = "leaf.evidence.v1"
_LEAF_DOMAIN = b"leaf-evidence-leaf-v1\0"
_NODE_DOMAIN = b"leaf-evidence-node-v1\0"


def canonical_bytes(value: Any) -> bytes:
    def encode(item: Any) -> Any:
        if isinstance(item, uuid.UUID):
            return str(item)
        if isinstance(item, (datetime, date)):
            return item.isoformat()
        if isinstance(item, Decimal):
            return format(item, "f")
        raise TypeError(f"unsupported evidence value {type(item).__name__}")
    return json.dumps(value, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False, allow_nan=False, default=encode).encode("utf-8")


def _digest(value: bytes) -> bytes:
    return hashlib.sha256(value).digest()


def _path_ok(path: Any) -> bool:
    """The frozen path law, shared by build() and verify(): a non-empty
    string, forward-slash separated, relative on EVERY platform. Backslashes
    are rejected outright (Windows joins would normalize `..\\escape` and
    `\\absolute` outside the bundle root — sol-critic PR #48 R2), as are
    drive-letter prefixes and `..` segments."""
    if not isinstance(path, str) or not path:
        return False
    if "\\" in path or path.startswith("/"):
        return False
    if len(path) >= 2 and path[1] == ":" and path[0].isalpha():
        return False
    return ".." not in path.split("/")


def _leaf(path: str, content_sha256: str) -> bytes:
    return _digest(_LEAF_DOMAIN + path.encode("utf-8") + b"\0" + bytes.fromhex(content_sha256))


def _root(entries: list[Dict[str, Any]], metadata: Mapping[str, Any]) -> str:
    header = {"bundleVersion": BUNDLE_VERSION, "algorithm": "sha256-merkle-v1",
              "metadata": dict(metadata)}
    header_sha = hashlib.sha256(canonical_bytes(header)).hexdigest()
    level = [_leaf("@manifest-metadata", header_sha)] + [
        _leaf(item["path"], item["sha256"]) for item in entries]
    if not level:
        raise ValueError("evidence bundle requires at least one entry")
    while len(level) > 1:
        if len(level) % 2:
            level.append(level[-1])
        level = [_digest(_NODE_DOMAIN + level[index] + level[index + 1])
                 for index in range(0, len(level), 2)]
    return level[0].hex()


def build(blobs: Mapping[str, bytes], *, metadata: Mapping[str, Any]) -> Dict[str, Any]:
    if not isinstance(metadata, Mapping) or not metadata:
        raise ValueError("bundle metadata must be a non-empty object")
    entries = []
    for path in sorted(blobs):
        value = blobs[path]
        if not _path_ok(path):
            raise ValueError("bundle paths must be relative and traversal-free")
        if not isinstance(value, bytes):
            raise ValueError("bundle entries must be bytes")
        entries.append({"path": path, "size": len(value),
                        "sha256": hashlib.sha256(value).hexdigest()})
    if not entries:
        raise ValueError("evidence bundle requires at least one entry")
    manifest = {"bundleVersion": BUNDLE_VERSION, "algorithm": "sha256-merkle-v1",
                "metadata": dict(metadata), "entries": entries}
    manifest["rootSha256"] = _root(entries, manifest["metadata"])
    return manifest


def verify(manifest: Mapping[str, Any], blobs: Mapping[str, bytes]) -> Dict[str, Any]:
    """Offline verification enforcing the FULL frozen contract
    (contract/EVIDENCE.md). verify() is the reference verifier for untrusted
    manifests, so every build-time law is re-checked here — a manifest
    build() could not have produced must not verify (sol-critic PR #48 R1:
    forged manifests with an extra unhashed top-level key, empty metadata,
    or a traversal path previously returned valid)."""
    errors = []
    if manifest.get("bundleVersion") != BUNDLE_VERSION or manifest.get("algorithm") != "sha256-merkle-v1":
        errors.append("unsupported_bundle_contract")
    # Exact top-level key set: an unknown key is UNHASHED surface (the root
    # covers header+entries only) — a smuggling channel, never tolerated.
    if set(manifest.keys()) != {"bundleVersion", "algorithm", "metadata", "entries", "rootSha256"}:
        errors.append("unexpected_manifest_keys")
    expected = manifest.get("entries")
    if not isinstance(expected, list) or not expected:
        errors.append("missing_entries")
        expected = []
    # Only STRING paths enter ordering/set logic — an unhashable path type
    # (list/dict) must yield the documented error result, never a TypeError
    # escaping the verifier (sol-critic PR #48 R2).
    expected_paths = [item.get("path") for item in expected if isinstance(item, Mapping)]
    string_paths = [p for p in expected_paths if isinstance(p, str)]
    if (len(expected_paths) != len(string_paths)
            or len(string_paths) != len(set(string_paths))
            or string_paths != sorted(string_paths)):
        errors.append("entries_not_unique_and_sorted")
    for path in string_paths:
        # The build-time path law (shared _path_ok), re-enforced offline.
        if not _path_ok(path):
            errors.append(f"invalid_path:{path}")
    if set(string_paths) != set(blobs):
        errors.append("entry_set_mismatch")
    rebuilt = []
    for item in expected:
        if not isinstance(item, Mapping) or not isinstance(item.get("path"), str):
            errors.append("invalid_entry")
            continue
        # Exact per-entry shape: the three keys build() emits, with STRICT
        # types — bool passes `== 1` and 1.0 passes `== 1` under Python
        # equality, and an extra entry field is unhashed smuggling surface.
        if (set(item.keys()) != {"path", "size", "sha256"}
                or not isinstance(item.get("size"), int)
                or isinstance(item.get("size"), bool)
                or not isinstance(item.get("sha256"), str)):
            errors.append("invalid_entry")
            continue
        path = item["path"]
        value = blobs.get(path)
        if value is None:
            errors.append(f"missing:{path}")
            continue
        digest = hashlib.sha256(value).hexdigest()
        if item["size"] != len(value) or item["sha256"] != digest:
            errors.append(f"digest_mismatch:{path}")
        rebuilt.append({"path": path, "size": len(value), "sha256": digest})
    if rebuilt:
        metadata = manifest.get("metadata")
        if not isinstance(metadata, Mapping) or not metadata:
            # The build-time law: metadata is a NON-EMPTY object. An empty
            # mapping is as invalid offline as it is at build time.
            errors.append("invalid_metadata")
            metadata = {"__invalid__": True}
        root = _root(rebuilt, metadata)
        if manifest.get("rootSha256") != root:
            errors.append("root_mismatch")
    return {"valid": not errors, "errors": errors,
            "rootSha256": manifest.get("rootSha256")}


def json_blob(value: Any) -> bytes:
    return canonical_bytes(value)
