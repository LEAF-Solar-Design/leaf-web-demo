from __future__ import annotations

import copy
import json

import pytest

import tool_record_fields
from tool_record_fields import (
    ToolRecordFieldError, canonicalize_catalog_record_fields,
    parse_catalog_record_fields_json,
)


def snapshot():
    return {"schema": "leaf.customization-record-fields.v1", "tools": {
        "drape-onto-spheres": {"version": "1.0.0", "record_sha256": "c" * 64,
                               "graph_input": tool_record_fields.GRAPH_INPUT_SOLAR_W1}}}


def structural_failure(value):
    with pytest.raises(ToolRecordFieldError) as caught:
        canonicalize_catalog_record_fields(value)
    assert caught.value.field == "catalog_record_fields_json"


def test_r1_snapshot_shape():
    valid = snapshot()
    assert parse_catalog_record_fields_json(canonicalize_catalog_record_fields(valid)) == valid
    assert parse_catalog_record_fields_json(canonicalize_catalog_record_fields(
        {"schema": valid["schema"], "tools": {}}))["tools"] == {}
    for value in ([], None, {}, {"schema": valid["schema"]},
                  {**valid, "extra": 1}, {**valid, "tools": []},
                  {**valid, "tools": {"tool": None}}, {**valid, "schema": "other"}):
        structural_failure(value)
    entry = valid["tools"]["drape-onto-spheres"]
    for key in entry:
        changed = copy.deepcopy(valid)
        del changed["tools"]["drape-onto-spheres"][key]
        structural_failure(changed)
        changed = copy.deepcopy(valid)
        changed["tools"]["drape-onto-spheres"][key] = 1
        structural_failure(changed)
    structural_failure({**valid, "tools": {"tool": {**entry, "extra": 1}}})
    for text in ('{"schema":"leaf.customization-record-fields.v1","tools":{},"tools":{}}',
                 "[", "null", "[]"):
        with pytest.raises(ToolRecordFieldError) as caught:
            parse_catalog_record_fields_json(text)
        assert caught.value.field == "catalog_record_fields_json"
    duplicate = json.dumps(valid).replace(
        '"version": "1.0.0"', '"version": "0.9.0", "version": "1.0.0"'
    )
    with pytest.raises(ToolRecordFieldError, match="duplicate JSON object key") as caught:
        parse_catalog_record_fields_json(duplicate)
    assert caught.value.field == "catalog_record_fields_json"


def test_r1_snapshot_bounds():
    valid = snapshot()
    entry = valid["tools"]["drape-onto-spheres"]
    many = {**valid, "tools": {str(index): entry for index in range(1024)}}
    assert len(parse_catalog_record_fields_json(canonicalize_catalog_record_fields(many))["tools"]) == 1024
    many["tools"]["overflow"] = entry
    structural_failure(many)
    assert canonicalize_catalog_record_fields({**valid, "tools": {"n" * 64: {**entry, "version": "v" * 128}}})
    for name in ("", " " * 64, "n" * 65, 1):
        structural_failure({**valid, "tools": {name: entry}})
    for version in ("", " " * 128, "v" * 129, None):
        structural_failure({**valid, "tools": {"tool": {**entry, "version": version}}})
    for digest in ("a" * 63, "a" * 65, "A" * 64, "g" * 64, None):
        structural_failure({**valid, "tools": {"tool": {**entry, "record_sha256": digest}}})
    for graph in ("", "g" * 65, None):
        structural_failure({**valid, "tools": {"tool": {**entry, "graph_input": graph}}})


def test_r1_snapshot_huge_integer():
    text = json.dumps(snapshot()).replace('"version": "1.0.0"', '"version": ' + "9" * 5000)
    with pytest.raises(ToolRecordFieldError) as caught:
        parse_catalog_record_fields_json(text)
    assert caught.value.field == "catalog_record_fields_json"


def test_r1_snapshot_value_authority(monkeypatch):
    valid = snapshot()
    assert canonicalize_catalog_record_fields(valid)
    valid["tools"]["drape-onto-spheres"]["graph_input"] = "other"
    with pytest.raises(ToolRecordFieldError) as caught:
        canonicalize_catalog_record_fields(valid)
    assert caught.value.field == "graph_input"
    seen = []

    def validator(value):
        seen.append(value)
        return value

    monkeypatch.setattr(tool_record_fields, "validate_graph_input", validator)
    assert canonicalize_catalog_record_fields(valid)
    assert seen == ["other"]
    assert parse_catalog_record_fields_json(json.dumps(valid)) == valid
    assert seen == ["other", "other"]


def test_r1_snapshot_canonicalization():
    valid = snapshot()
    entry = valid["tools"]["drape-onto-spheres"]
    entry["version"] = " 1.0.0 café "
    reordered = {"tools": {"drape-onto-spheres": dict(reversed(list(entry.items())))},
                 "schema": valid["schema"]}
    expected = json.dumps(valid, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False)
    assert canonicalize_catalog_record_fields(valid) == expected
    assert canonicalize_catalog_record_fields(parse_catalog_record_fields_json(json.dumps(reordered, indent=2))) == expected
    assert "\\u00e9" in expected
    for literal in ("NaN", "Infinity", "-Infinity", "1e999"):
        text = '{"schema":"leaf.customization-record-fields.v1","tools":{"tool":{"version":' + literal + ',"record_sha256":"' + "c" * 64 + '","graph_input":"solar-w1-graph"}}}'
        with pytest.raises(ToolRecordFieldError) as caught:
            parse_catalog_record_fields_json(text)
        assert caught.value.field == "catalog_record_fields_json"
