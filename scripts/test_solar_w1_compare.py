"""Executable boundaries for the Solar W1 semantic evidence adapter."""

from copy import deepcopy
import importlib.util
import json
from pathlib import Path
import subprocess

import pytest

SPEC = importlib.util.spec_from_file_location("solar_w1_compare", Path(__file__).with_name("solar_w1_compare.py"))
compare = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(compare)


def entity(identifier="p1"):
    return {"entity_id": identifier}


def quantity(kind, value, unit):
    return {"kind": kind, "value": value, "unit": unit}


def evidence(after=None):
    after = after if after is not None else {"counts": {"panels": 1}, "identities": [entity()]}
    return {
        "fixture_sha256": "a" * 64, "input_sha256": "b" * 64,
        "output_sha256": compare.semantic_hash(after), "revision": "1",
        "versions": {"schema": "1", "producer": "1", "capability": "1", "engine": "1", "catalog": "none", "solver": "none"},
        "parameters": {}, "units": "mm",
        "frame": {"coordinate_system": "WCS", "transform": [1, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1], "elevation_datum": "local", "crs": "none"},
        "entity_mapping": {"p1": "panel-1", "p2": "panel-2", "s1": "string-1"},
        "before": {"revision": "0"}, "after": after,
        "changes": {"created": [], "modified": [], "deleted": []},
        "warnings": [], "rejected_inputs": [], "provenance": {"source": "committed-extraction"},
        "elapsed_ms": 12, "execution_mode": "recorded", "state": "committed",
        "survived_reopen": True, "synthetic_fields": [], "fallback_fields": [], "synthetic_flagged": False,
    }


def document(family="count", after=None):
    left = evidence(after)
    return {"schema": compare.SCHEMA, "capability": "test-capability", "family": family, "plugin": left, "studio": deepcopy(left)}


def rehash(doc):
    for side in ("plugin", "studio"):
        doc[side]["output_sha256"] = compare.semantic_hash(doc[side]["after"])
    return doc


def verdict(doc):
    return compare.compare_document(rehash(doc))["verdict"]


def fixed_string():
    return document("strings", {"strings": [{"id": entity("s1"), "ordered_membership": [entity(), entity("p2")], "polarity": "positive"}], "unassigned_panels": [], "duplicate_panels": [], "length_distribution": [2]})


def solve():
    doc = document("solve", {"added_panels": [], "removed_panels": [], "feasible": True, "objective": 10, "chosen_candidate": "first", "accepted": True, "reverted": False, "constraints": {"max_voltage": 1000}})
    doc["prerequisites"] = [document(), fixed_string()]
    for side in ("plugin", "studio"):
        doc[side]["parameters"]["objective_unit"] = "m"
    return doc


def test_count_transport_maps_ids_and_preserves_changes():
    doc = document()
    doc["studio"]["entity_mapping"]["dwg-1"] = doc["studio"]["entity_mapping"].pop("p1")
    doc["studio"]["after"]["identities"] = [entity("dwg-1")]
    doc["plugin"]["changes"]["modified"] = ["p1"]
    doc["studio"]["changes"]["modified"] = ["dwg-1"]
    assert verdict(doc) == "pass"
    doc["studio"]["after"]["counts"]["panels"] = 2
    with pytest.raises(compare.InputError, match="count transport"):
        verdict(doc)


def test_fixed_order_and_polarity_are_exact():
    doc = fixed_string()
    assert verdict(doc) == "pass"
    doc["studio"]["after"]["strings"][0]["ordered_membership"].reverse()
    assert verdict(doc) == "fail"
    doc = fixed_string()
    doc["studio"]["after"]["strings"][0]["polarity"] = "negative"
    assert verdict(doc) == "fail"


@pytest.mark.parametrize("kind,left,right,unit,expected", [
    ("length", 1000, 1001, "mm", "pass"),
    ("length", 1000, 1001.0001, "mm", "fail"),
    ("coordinate", [0, 0, 0], [1, 0, 0], "mm", "pass"),
    ("coordinate", [0, 0], [1.001, 0], "mm", "fail"),
    ("angle", 30, 30.01, "deg", "pass"),
    ("angle", 30, 30.0101, "deg", "fail"),
    ("float", 1, 1.000001, "ratio", "pass"),
    ("float", 1, 1.000002, "ratio", "fail"),
    ("float", 1000000, 1000000.009, "ratio", "pass"),
])
def test_frozen_tolerances(kind, left, right, unit, expected):
    doc = document("settings", {"settings": {"measurement": quantity(kind, left, unit)}})
    doc["studio"]["after"]["settings"]["measurement"] = quantity(kind, right, unit)
    assert verdict(doc) == expected


def test_length_units_normalize_without_rounding():
    doc = document("settings", {"settings": {"measurement": quantity("length", 1, "in")}})
    doc["studio"]["units"] = "m"
    doc["studio"]["after"]["settings"]["measurement"] = quantity("length", 0.0254, "m")
    assert verdict(doc) == "pass"


@pytest.mark.parametrize("field", ["safety_thresholds", "voltage_limit", "rounding_decisions", "validity", "counts", "capacities", "loads"])
def test_safety_and_decisions_never_use_float_tolerance(field):
    doc = document("settings", {"settings": {field: quantity("float", 1, "V")}})
    doc["studio"]["after"]["settings"][field]["value"] = 1.0000001
    assert verdict(doc) == "fail"


@pytest.mark.parametrize("field,value", [("survived_reopen", False), ("state", "failed"), ("execution_mode", "synthetic"), ("fallback_fields", ["model"]), ("synthetic_fields", ["tilt"])])
def test_evidence_cannot_silently_satisfy_parity(field, value):
    doc = document()
    doc["plugin"][field] = value
    assert verdict(doc) == "fail"


def test_flagged_fields_are_explicit_and_not_ignored():
    doc = document()
    doc["plugin"]["fallback_fields"] = ["model"]
    doc["plugin"]["synthetic_flagged"] = True
    assert verdict(doc) == "pass"
    doc["studio"]["after"]["identities"] = [entity("p2")]
    assert verdict(doc) == "fail"


@pytest.mark.parametrize("mutation", ["missing", "hash", "nan", "mapping", "unknown-unit", "reference", "boolean-count"])
def test_malformed_evidence_fails_closed(mutation):
    doc = document()
    if mutation == "missing":
        del doc["plugin"]["revision"]
    elif mutation == "hash":
        doc["plugin"]["output_sha256"] = "0" * 64
    elif mutation == "nan":
        doc["plugin"]["elapsed_ms"] = float("nan")
    elif mutation == "mapping":
        doc["plugin"]["entity_mapping"]["p2"] = "panel-1"
    elif mutation == "unknown-unit":
        doc["plugin"]["units"] = "unknown"
    elif mutation == "reference":
        doc["plugin"]["after"]["identities"] = [entity("missing")]
        rehash(doc)
    else:
        doc["plugin"]["after"]["counts"]["panels"] = True
        rehash(doc)
    with pytest.raises(compare.InputError):
        compare.compare_document(doc)


def test_optimizer_follows_count_then_fixed_string():
    doc = solve()
    assert verdict(doc) == "pass"
    doc["prerequisites"].reverse()
    with pytest.raises(compare.InputError, match="order"):
        verdict(doc)


def test_alternative_solve_requires_frozen_bound_and_equal_constraints():
    doc = solve()
    doc["studio"]["after"]["chosen_candidate"] = "second"
    doc["studio"]["after"]["objective"] = 10.5
    assert verdict(doc) == "fail"
    doc["objective_bound"] = 0.5
    assert verdict(doc) == "pass"
    doc["studio"]["after"]["constraints"]["max_voltage"] = 1000.000001
    assert verdict(doc) == "fail"
    doc["studio"]["after"]["constraints"]["max_voltage"] = 1000
    doc["studio"]["after"]["feasible"] = False
    assert verdict(doc) == "fail"


@pytest.mark.parametrize("family", list(compare.FAMILIES))
def test_every_family_requires_its_semantic_fields(family):
    doc = document(family, {"unrelated": True})
    with pytest.raises(compare.InputError, match="family semantic"):
        compare.compare_document(doc)


def test_named_input_cli_and_deadline(tmp_path, monkeypatch, capsys):
    doc = document()
    plugin, studio = tmp_path / "plugin.json", tmp_path / "studio.json"
    plugin.write_text(json.dumps(doc["plugin"]), encoding="utf-8")
    studio.write_text(json.dumps(doc["studio"]), encoding="utf-8")
    args = ["--plugin-input", str(plugin), "--studio-input", str(studio), "--family", "count", "--capability", "count-by-layer"]
    assert compare.main(args) == 0
    assert json.loads(capsys.readouterr().out)["verdict"] == "pass"
    def timeout(*args, **kwargs):
        assert kwargs["timeout"] == compare.IO_TIMEOUT
        raise subprocess.TimeoutExpired("reader", compare.IO_TIMEOUT)
    monkeypatch.setattr(compare.subprocess, "run", timeout)
    assert compare.main(args) == 2
    assert capsys.readouterr().out == ""


def test_duplicate_json_key_and_structure_bounds(tmp_path):
    path = tmp_path / "duplicate.json"
    path.write_text('{"x":1,"x":2}', encoding="utf-8")
    with pytest.raises(compare.InputError):
        compare.load_evidence(path)
    value = {}
    for _ in range(compare.MAX_DEPTH + 1):
        value = {"nested": value}
    with pytest.raises(compare.InputError, match="structural"):
        compare.semantic_hash(value)


def test_schema_matches_executable_families_and_evidence_fields():
    path = Path(__file__).resolve().parents[1] / "contract" / "solar-w1-comparison.v1.schema.json"
    schema = json.loads(path.read_text(encoding="utf-8"))
    assert set(schema["properties"]["family"]["enum"]) == set(compare.FAMILIES)
    assert set(schema["$defs"]["evidence"]["required"]) == compare.EVIDENCE_KEYS
