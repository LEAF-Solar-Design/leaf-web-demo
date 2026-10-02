"""The params validator's structural fallback (used only when jsonschema cannot import)."""
from __future__ import annotations

import builtins
import json
import sys
from pathlib import Path

import pytest

SERVER_DIR = Path(__file__).resolve().parent.parent
if str(SERVER_DIR) not in sys.path:
    sys.path.insert(0, str(SERVER_DIR))

import tool_validate  # noqa: E402

NULLABLE_NUMBER = {"type": "object", "properties": {"meters_per_unit": {"type": ["number", "null"]}}}


def schema_of(type_value):
    return {"type": "object", "properties": {"k": {"type": type_value}}}


def structural(type_value, value):
    return tool_validate._structural_params(schema_of(type_value), {"k": value})


@pytest.fixture
def no_jsonschema(monkeypatch):
    """Make `import jsonschema` raise ImportError, as on a host without the package."""
    real_import = builtins.__import__

    def fake_import(name, *args, **kwargs):
        if name == "jsonschema" or name.startswith("jsonschema."):
            raise ImportError("jsonschema is not installed")
        return real_import(name, *args, **kwargs)

    monkeypatch.delitem(sys.modules, "jsonschema", raising=False)
    monkeypatch.setattr(builtins, "__import__", fake_import)


@pytest.mark.parametrize("type_name,value,ok", [
    ("string", "x", True), ("string", 1, False),
    ("number", 1, True), ("number", 1.5, True), ("number", "1", False), ("number", True, False),
    ("integer", 1, True), ("integer", 1.5, False), ("integer", False, False),
    ("boolean", True, True), ("boolean", 1, False),
    ("object", {}, True), ("object", [], False),
    ("array", [], True), ("array", {}, False),
    ("null", None, True), ("null", 0, False),
    ("made-up", object(), True),
])
def test_tool_validate_structural_single_type_unchanged(type_name, value, ok):
    assert structural(type_name, value) == ([] if ok else [f"k: expected {type_name}"])


@pytest.mark.parametrize("value,ok", [
    (1, True), (1.5, True), (None, True), ("1", False), (True, False), ([1], False), ({}, False),
])
def test_tool_validate_structural_nullable_number(value, ok):
    errors = tool_validate._structural_params(NULLABLE_NUMBER, {"meters_per_unit": value})
    assert errors == ([] if ok else ["meters_per_unit: expected number or null"])


@pytest.mark.parametrize("names,value,ok", [
    (["string", "null"], "x", True), (["string", "null"], None, True), (["string", "null"], 1, False),
    (["integer", "boolean"], True, True), (["integer", "boolean"], 1, True),
    (["integer", "boolean"], 1.5, False),
    (["number"], True, False), (["number"], 2, True),
    (["null", "array", "object"], [], True), (["null", "array", "object"], "x", False),
])
def test_tool_validate_structural_type_list(names, value, ok):
    assert structural(names, value) == ([] if ok else [f"k: expected {' or '.join(names)}"])


@pytest.mark.parametrize("type_value", [
    [], ["made-up"], ["number", "made-up"], [["number"]], [1], [None], ["number", ["null"]],
])
def test_tool_validate_structural_unknown_type_entries_are_not_checked(type_value):
    for value in ("x", 1, None, True, [], {}):
        assert structural(type_value, value) == []


def test_tool_validate_structural_other_rules_unchanged():
    schema = {"type": "object", "required": ["a"], "additionalProperties": False,
              "properties": {"a": {"type": "string"}, "b": {"type": ["integer", "null"]}}}
    assert tool_validate._structural_params(schema, []) == ["<root>: expected object"]
    assert tool_validate._structural_params(schema, {}) == ["a: required property missing"]
    assert tool_validate._structural_params(schema, {"a": "x", "b": None}) == []
    assert tool_validate._structural_params(schema, {"a": "x", "b": 2, "c": 1}) == [
        "c: additional property not allowed"]
    assert tool_validate._structural_params(schema, {"a": 1, "b": "2"}) == [
        "a: expected string", "b: expected integer or null"]


def test_tool_validate_structural_fallback_is_reached_without_jsonschema(no_jsonschema):
    tool = {"params": dict(NULLABLE_NUMBER, additionalProperties=False)}
    for params in ({}, {"meters_per_unit": None}, {"meters_per_unit": 1.0}, {"meters_per_unit": 1}):
        assert tool_validate.validate_params(tool, params) == []
    assert tool_validate.validate_params(tool, {"meters_per_unit": "1"}) == [
        "meters_per_unit: expected number or null"]
    assert tool_validate.validate_params(tool, {"meters_per_unit": True}) == [
        "meters_per_unit: expected number or null"]
    assert tool_validate.validate_params(tool, {"other": 1}) == [
        "other: additional property not allowed"]


@pytest.mark.parametrize("value", [1, 1.5, None, "1", True, [1], {}])
def test_tool_validate_structural_agrees_with_jsonschema_on_type_lists(value):
    pytest.importorskip("jsonschema")
    tool = {"params": NULLABLE_NUMBER}
    params = {"meters_per_unit": value}
    assert bool(tool_validate.validate_params(tool, params)) == bool(
        tool_validate._structural_params(NULLABLE_NUMBER, params))


def test_tool_validate_structural_never_raises_on_any_catalog_record(no_jsonschema):
    # Every catalog record, with its defaults and with every declared property set to null: the
    # fallback answers with a list of sentences, whatever shape the property's "type" has.
    catalog = json.loads((SERVER_DIR / "catalog_tools.json").read_text(encoding="utf-8"))
    checked = 0
    for record in catalog["tools"]:
        schema = record.get("params")
        if not (isinstance(schema, dict) and schema):
            continue
        nulls = {key: None for key in (schema.get("properties") or {})}
        for params in (record.get("default_params") or {}, nulls):
            errors = tool_validate.validate_params(record, params)
            assert isinstance(errors, list) and all(isinstance(e, str) for e in errors), record["name"]
        checked += 1
    assert checked >= 1
