"""Bounded schemas and manifest validation against trusted metadata."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

from jsonschema import Draft202012Validator
from referencing import Registry

from lab_canonical import canonical_json


class ManifestInvalid(ValueError):
    def __init__(self, code, pointer="", block_id=None):
        self.code, self.pointer, self.block_id = code, pointer, block_id
        super().__init__(f"{code}: {pointer} ({block_id})")

    def to_http(self):
        return 422, {"detail": self.code, "pointer": self.pointer, "block": self.block_id}


def _pointer(parts):
    return "".join("/" + str(part).replace("~", "~0").replace("/", "~1") for part in parts)


_PATH = Path(__file__).resolve().parents[1] / "contract/fixtures/lab/leaf-block-manifest.v1.schema.json"
_SCHEMA = json.loads(_PATH.read_text(encoding="utf-8"))
# STD-5's structure and ID grammar remain pinned. SEL-201 extends only the
# execution vocabulary; the registry, rather than an enum, admits block kinds.
_SCHEMA["properties"]["kind"] = {"type": "string", "minLength": 1}
_SCHEMA["properties"]["effect"]["enum"] += ["simulate", "compute"]
_SCHEMA["properties"]["residency"]["enum"] += ["host", "none"]
_VALIDATOR = Draft202012Validator(_SCHEMA, registry=Registry())


def validate_manifest(manifest):
    from lab_registry import lookup

    block_id = manifest.get("id") if isinstance(manifest, dict) else None
    version = manifest.get("version") if isinstance(manifest, dict) else None
    block = lookup(manifest.get("kind"), version.get("semver")) if isinstance(version, dict) else None
    if block is not None:
        for field in ("effect", "residency"):
            if field in manifest and manifest[field] != getattr(block, field):
                raise ManifestInvalid("lab_manifest_spoof", "/" + field, block_id)
    error = next(_VALIDATOR.iter_errors(manifest), None)
    if error is not None:
        raise ManifestInvalid("lab_manifest_invalid", _pointer(error.absolute_path), block_id)
    if block is None:
        raise ManifestInvalid("lab_manifest_unknown_block", "/kind", block_id)
    return block


def validate_returns_schema(schema):
    def fail(pointer):
        raise ManifestInvalid("lab_returns_schema_bounds", pointer)

    if not isinstance(schema, dict):
        fail("")

    def walk(value, path=(), depth=0):
        if depth > 8:
            fail(_pointer(path))
        if isinstance(value, dict):
            for key, child in value.items():
                if not isinstance(key, str):
                    fail(_pointer(path))
                if key == "$ref" and (not isinstance(child, str) or not child.startswith("#/")):
                    fail(_pointer((*path, key)))
                walk(child, (*path, key), depth + 1)
        elif isinstance(value, list):
            for index, child in enumerate(value):
                walk(child, (*path, index), depth + 1)

    try:
        walk(schema)
        data = canonical_json(schema).encode("utf-8")
        if len(data) > 16384:
            fail("")
        return hashlib.sha256(data).hexdigest()
    except (ValueError, TypeError, RecursionError, OverflowError) as error:
        if isinstance(error, ManifestInvalid):
            raise
        raise ManifestInvalid("lab_returns_schema_bounds", "") from error
