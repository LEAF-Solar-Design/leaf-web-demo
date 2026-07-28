"""Dependency-free JSON Schema subset checker for the instant execution fixtures."""
from __future__ import annotations

import json
import re
import sys
import uuid
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent
SCHEMAS = ROOT / "schemas"
FORBIDDEN_KEY = re.compile(r"(?:^|[_-])(aws|autodesk|redis|postgres(?:ql)?|broker|claude|credential|secret|password|api[_-]?key|access[_-]?key|token)(?:$|[_-])", re.I)


def load(path: Path):
    with path.open(encoding="utf-8") as handle:
        return json.load(handle)


def type_matches(value, kind):
    if kind == "object": return isinstance(value, dict)
    if kind == "array": return isinstance(value, list)
    if kind == "string": return isinstance(value, str)
    if kind == "integer": return isinstance(value, int) and not isinstance(value, bool)
    if kind == "number": return isinstance(value, (int, float)) and not isinstance(value, bool)
    if kind == "boolean": return isinstance(value, bool)
    if kind == "null": return value is None
    raise ValueError(f"unsupported schema type {kind}")


def validate(value, schema, path="$", seen=None):
    errors = []
    if "$ref" in schema:
        target = schema["$ref"]
        if "/" in target or target.startswith("#"):
            raise ValueError(f"unsupported ref {target}")
        return validate(value, load(SCHEMAS / target), path, seen)
    if "const" in schema and value != schema["const"]: errors.append(f"{path}: expected const {schema['const']!r}")
    if "enum" in schema and value not in schema["enum"]: errors.append(f"{path}: value is not in enum")
    kinds = schema.get("type")
    if kinds:
        kinds = kinds if isinstance(kinds, list) else [kinds]
        if not any(type_matches(value, kind) for kind in kinds):
            return errors + [f"{path}: expected type {kinds}"]
    if isinstance(value, str):
        if "pattern" in schema and not re.search(schema["pattern"], value): errors.append(f"{path}: pattern mismatch")
        if len(value) < schema.get("minLength", 0) or len(value) > schema.get("maxLength", len(value)): errors.append(f"{path}: string length out of bounds")
        if schema.get("format") == "uuid":
            try: uuid.UUID(value)
            except ValueError: errors.append(f"{path}: invalid uuid")
        if schema.get("format") == "date-time":
            try: datetime.fromisoformat(value.replace("Z", "+00:00"))
            except ValueError: errors.append(f"{path}: invalid date-time")
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        if value < schema.get("minimum", value): errors.append(f"{path}: below minimum")
        if value > schema.get("maximum", value): errors.append(f"{path}: above maximum")
    if isinstance(value, dict):
        props = schema.get("properties", {})
        for name in schema.get("required", []):
            if name not in value: errors.append(f"{path}: missing {name}")
        if len(value) > schema.get("maxProperties", len(value)): errors.append(f"{path}: too many properties")
        for name, child in value.items():
            if name in props: errors.extend(validate(child, props[name], f"{path}.{name}"))
            elif schema.get("additionalProperties") is False: errors.append(f"{path}: unexpected {name}")
            elif isinstance(schema.get("additionalProperties"), dict): errors.extend(validate(child, schema["additionalProperties"], f"{path}.{name}"))
    for branch in schema.get("allOf", []):
        errors.extend(validate(value, branch, path))
    if "if" in schema and not validate(value, schema["if"], path): errors.extend(validate(value, schema.get("then", {}), path))
    if "not" in schema and not validate(value, schema["not"], path): errors.append(f"{path}: forbidden shape")
    return errors


def forbidden_fields(value, path="$"):
    errors = []
    if isinstance(value, dict):
        for key, child in value.items():
            child_path = f"{path}.{key}"
            if FORBIDDEN_KEY.search(key): errors.append(f"{child_path}: credential-like field is forbidden")
            errors.extend(forbidden_fields(child, child_path))
    elif isinstance(value, list):
        for index, child in enumerate(value): errors.extend(forbidden_fields(child, f"{path}[{index}]"))
    return errors


def main():
    manifest = load(ROOT / "fixtures" / "manifest.json")
    schema_names = {path.name for path in SCHEMAS.glob("*.schema.json")}
    checked = 0
    for fixture in manifest["fixtures"]:
        bundle = load(ROOT / "fixtures" / fixture["bundle"])
        if set(bundle) != schema_names:
            raise AssertionError(f"{fixture['bundle']}: schema coverage differs from schema directory")
        for name in sorted(schema_names):
            errors = validate(bundle[name], load(SCHEMAS / name))
            if name == "invocation.v1.schema.json": errors.extend(forbidden_fields(bundle[name]))
            actual = not errors
            if actual != fixture["valid"]:
                raise AssertionError(f"{fixture['bundle']} {name}: expected valid={fixture['valid']}, errors={errors}")
            checked += 1
    print(f"contract fixtures passed: {checked} checks ({len(schema_names)} schemas, valid and invalid bundles)")


if __name__ == "__main__":
    main()
