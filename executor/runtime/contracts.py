"""Small schema reader for the contract subset used by this prototype.

The source schemas in ``executor/contracts`` remain authoritative.  Keeping the
reader here dependency-free makes the prototype runnable in a bare CPython
installation while still consuming those schemas rather than copying them.
"""
from __future__ import annotations

import json
import re
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any

SCHEMA_DIR = Path(__file__).resolve().parents[1] / "contracts" / "schemas"


class ContractError(ValueError):
    """A request did not satisfy an on-disk v1 contract schema."""


def _is_type(value: Any, expected: str) -> bool:
    return {
        "object": isinstance(value, dict),
        "array": isinstance(value, list),
        "string": isinstance(value, str),
        "integer": isinstance(value, int) and not isinstance(value, bool),
        "number": isinstance(value, (int, float)) and not isinstance(value, bool),
        "boolean": isinstance(value, bool),
        "null": value is None,
    }.get(expected, True)


def _check(value: Any, schema: dict[str, Any], root: dict[str, Any], path: str) -> None:
    if "$ref" in schema:
        ref = schema["$ref"]
        if ref.startswith("#/"):
            target: Any = root
            for key in ref[2:].split("/"):
                target = target[key]
            _check(value, target, root, path)
            return
        referenced = json.loads((SCHEMA_DIR / ref).read_text(encoding="utf-8"))
        _check(value, referenced, referenced, path)
        return
    expected = schema.get("type")
    if isinstance(expected, list):
        if not any(_is_type(value, item) for item in expected):
            raise ContractError(f"{path} has the wrong type")
    elif isinstance(expected, str) and not _is_type(value, expected):
        raise ContractError(f"{path} has the wrong type")
    if "const" in schema and value != schema["const"]:
        raise ContractError(f"{path} must equal {schema['const']!r}")
    if "enum" in schema and value not in schema["enum"]:
        raise ContractError(f"{path} is not an allowed value")
    if isinstance(value, str):
        # JSON Schema ``pattern`` succeeds on a matching substring. Patterns
        # that need whole-string matching carry their own ^ and $ anchors.
        if "pattern" in schema and not re.search(schema["pattern"], value):
            raise ContractError(f"{path} has an invalid format")
        if schema.get("format") == "uuid":
            try:
                uuid.UUID(value)
            except ValueError as exc:
                raise ContractError(f"{path} must be a UUID") from exc
        if schema.get("format") == "date-time":
            try:
                datetime.fromisoformat(value.replace("Z", "+00:00"))
            except ValueError as exc:
                raise ContractError(f"{path} must be an RFC3339 time") from exc
        if len(value) < schema.get("minLength", 0) or len(value) > schema.get("maxLength", float("inf")):
            raise ContractError(f"{path} has an invalid length")
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        if value < schema.get("minimum", value) or value > schema.get("maximum", value):
            raise ContractError(f"{path} is outside its allowed range")
    if isinstance(value, dict):
        for key in schema.get("required", []):
            if key not in value:
                raise ContractError(f"{path}.{key} is required")
        properties = schema.get("properties", {})
        if schema.get("additionalProperties") is False:
            extra = set(value) - set(properties)
            if extra:
                raise ContractError(f"{path} has unexpected field {sorted(extra)[0]!r}")
        if len(value) > schema.get("maxProperties", float("inf")):
            raise ContractError(f"{path} has too many properties")
        for key, item in value.items():
            if key in properties:
                _check(item, properties[key], root, f"{path}.{key}")
            elif isinstance(schema.get("additionalProperties"), dict):
                _check(item, schema["additionalProperties"], root, f"{path}.{key}")


def validate_contract(schema_name: str, value: dict[str, Any]) -> None:
    """Validate against a checked-in schema using the v1 fields we consume."""
    try:
        schema = json.loads((SCHEMA_DIR / schema_name).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"cannot load contract schema {schema_name}") from exc
    _check(value, schema, schema, "$")
