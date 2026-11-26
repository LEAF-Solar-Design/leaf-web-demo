"""Compare title-block definitions and their identified inserts."""

import json

from .x12_evidence import compare, field, indexed, name


def _definition(row, path):
    geometry = field(row, "geometry", path, lambda v: isinstance(v, list), "geometry array")
    try:
        canonical = json.dumps(geometry, sort_keys=True, allow_nan=False)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{path}.geometry: invalid JSON geometry") from exc
    return {"geometry": canonical}


def _attribute(row, path):
    return field(row, "value", path, lambda v: isinstance(v, str), "text")


def _insert(row, path):
    attributes = field(row, "attributes", path, lambda v: isinstance(v, list), "attribute array")
    values = indexed(attributes, f"{path}.attributes", "tag", _attribute)
    return {"block": name(row, "block", path), "attributes": list(values.items())}


def _parse(snapshot, path):
    if not isinstance(snapshot, dict):
        raise ValueError(f"{path}: expected an object")
    definitions = indexed(snapshot.get("definitions"), f"{path}.definitions", "name", _definition)
    inserts = indexed(snapshot.get("inserts"), f"{path}.inserts", "id", _insert)
    for insert_id, insert in inserts.items():
        if insert["block"] not in definitions:
            raise ValueError(f"{path}.inserts[{insert_id!r}].block: missing definition")
    return definitions, inserts


def compare_title_blocks(before, after):
    old_defs, old_inserts = _parse(before, "before.title_blocks")
    new_defs, new_inserts = _parse(after, "after.title_blocks")
    issues = compare(old_defs, new_defs, "title_blocks.definitions", ("geometry",))
    issues += compare(old_inserts, new_inserts, "title_blocks.inserts", ("block", "attributes"))
    for label, old, new in (
        ("definitions", old_defs, new_defs), ("inserts", old_inserts, new_inserts),
    ):
        issues.extend(f"title_blocks.{label}[{key!r}]: unexpected" for key in new if key not in old)
    return issues
