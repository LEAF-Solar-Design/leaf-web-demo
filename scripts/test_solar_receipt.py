"""Synthetic pipeline controls for the Branch2025 to Studio receipt slice.

All fixtures are authored here, independent of the captured plugin corpus.
The positive control models recorded observations; it does not certify a real
producer, a real reopen, or capability parity.
"""

from copy import deepcopy
import importlib.util
import json
from pathlib import Path

import pytest


def load_module(name):
    spec = importlib.util.spec_from_file_location(name, Path(__file__).with_name(name + ".py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


adapter = load_module("solar_studio_evidence")
writer = load_module("solar_write_receipt")
compare = writer.compare
status = writer.status
CAPABILITY = "test-groups"


def graph():
    return {
        "graph_schema_version": 1, "rev": 1,
        "project": {"units": {
            "drawing_units": "mm", "meters_per_unit": 0.001,
            "source": "explicit", "compute_units": "m",
            "wcs_to_ucs": [1, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1],
            "elevation_datum": "local", "crs": "test-local",
        }},
        "frames": [{
            "id": "frame-1", "name": "Group one", "insertion_point": [0, 0, 10],
            "panel_refs": ["panel-1"], "installation_design": "Roof",
            "module_rows": 1, "module_columns": 1, "module_slots": 1,
            "module_power_watts": 400, "module_width_along_row": 1000,
            "module_height_across_row": 2000,
        }],
        "panels": [{"id": "panel-1", "centre": [500, 1000, 10], "angle": 0}],
    }


def metadata():
    return {
        "fixture_sha256": "a" * 64, "input_sha256": "b" * 64,
        "versions": {"schema": "1", "producer": "test-only", "capability": "1",
                     "engine": "test-only", "catalog": "none", "solver": "none"},
        "parameters": {}, "entity_mapping": {"frame-1": "group-one", "panel-1": "panel-one"},
        "before": {"groups": []},
        "changes": {"created": ["frame-1", "panel-1"], "modified": [], "deleted": []},
        "warnings": [], "rejected_inputs": [],
        "provenance": {"source": "authored-test-control", "build": "test-build-not-head",
                       "receipt_sha256": "c" * 64, "fixture_id": "test-fixture",
                       "engine": "server-builtin"},
        "elapsed_ms": 1, "execution_mode": "recorded", "state": "committed",
        "survived_reopen": True, "synthetic_fields": [], "fallback_fields": [],
        "synthetic_flagged": False,
        "coordinate_system": "WCS-to-UCS", "geometry_units": "mm", "angle_units": "deg",
    }


def pair():
    evidence = adapter.build_evidence(graph(), "groups", metadata())
    return evidence, deepcopy(evidence)


def receipt_for(plugin, studio):
    return writer.build_receipt(plugin, studio, CAPABILITY, "1", "groups", produced_at="2026-09-17T00:00:00Z")


def save_json(path, value):
    path.write_text(json.dumps(value), encoding="utf-8")
    return path


@pytest.mark.parametrize("family", ["groups", "panels"])
def test_studio_evidence_validates(family):
    source, context = graph(), metadata()
    originals = deepcopy((source, context))
    evidence = adapter.build_evidence(source, family, context)
    compare.validate_evidence(evidence, family)
    assert set(evidence) == compare.EVIDENCE_KEYS
    assert len(evidence) == 22
    assert (source, context) == originals
    assert evidence["output_sha256"] == compare.semantic_hash(evidence["after"])
    if family == "groups":
        group = evidence["after"]["groups"][0]
        assert group["membership"] == [{"entity_id": "panel-1"}]
        assert group["boundaries"] is None
        assert group["sizing_provenance"] is None
        assert "after/groups/0/boundaries" in evidence["fallback_fields"]
        assert "after/groups/0/sizing_provenance" in evidence["fallback_fields"]
        assert evidence["synthetic_flagged"] is True
    else:
        assert evidence["after"]["panels"][0]["geometry"]["centre"]["value"] == [500, 1000, 10]


def test_identical_pair_passes_and_parser_accepts(tmp_path):
    plugin, studio = pair()
    receipt = receipt_for(plugin, studio)
    assert receipt["comparator"]["verdict"] == "pass"
    assert receipt["comparator"]["diffs"] == []
    path = writer.write_receipt(receipt, tmp_path)
    assert path.parent == tmp_path / CAPABILITY
    parsed = status.parse_receipt(path, CAPABILITY)
    assert parsed["comparator"] == receipt["comparator"]
    assert status.receipt_violations(parsed, {"1"}) == []
    assert receipt["plugin"] == {"build": "test-build-not-head", "state": "committed", "receipt_sha256": "c" * 64}
    assert receipt["comparison"]["plugin"] == plugin
    assert receipt["comparison"]["studio"] == studio


def differing_pair():
    plugin, studio = pair()
    studio["after"]["groups"][0]["name"] = "Different group"
    studio["output_sha256"] = compare.semantic_hash(studio["after"])
    return plugin, studio


def test_difference_is_a_valid_failing_receipt(tmp_path):
    receipt = receipt_for(*differing_pair())
    assert receipt["comparator"]["verdict"] == "fail"
    assert any("after/groups/0/name" in diff for diff in receipt["comparator"]["diffs"])
    path = writer.write_receipt(receipt, tmp_path)
    assert status.parse_receipt(path, CAPABILITY)["comparator"] == receipt["comparator"]


def test_forged_pass_is_rejected_by_real_parser(tmp_path):
    receipt = receipt_for(*differing_pair())
    receipt["comparator"]["verdict"] = "pass"
    receipt["comparator"]["diffs"] = []
    path = save_json(tmp_path / "forged.json", receipt)
    with pytest.raises(status.InputError, match="disagrees with executable comparison"):
        status.parse_receipt(path, CAPABILITY)
    with pytest.raises(status.InputError, match="disagrees with executable comparison"):
        writer.write_receipt(receipt, tmp_path / "receipts")
    assert list((tmp_path / "receipts").rglob("*.*")) == []


def test_no_reopen_is_well_formed_but_disqualified(tmp_path):
    plugin, studio = pair()
    plugin["survived_reopen"] = studio["survived_reopen"] = False
    receipt = receipt_for(plugin, studio)
    path = writer.write_receipt(receipt, tmp_path)
    parsed = status.parse_receipt(path, CAPABILITY)
    assert parsed["survived_reopen"] is False
    assert parsed["comparator"]["verdict"] == "fail"
    assert "RECEIPT_NO_REOPEN" in status.receipt_violations(parsed, {"1"})


@pytest.mark.parametrize("field,value,error", [
    ("fixture_sha256", "d" * 64, "same fixture"),
    ("survived_reopen", False, "same reopen"),
])
def test_receipt_refuses_unrepresentable_pair(field, value, error):
    plugin, studio = pair()
    studio[field] = value
    with pytest.raises(compare.InputError, match=error):
        receipt_for(plugin, studio)


def test_receipt_refuses_missing_provenance_and_wrong_version():
    plugin, studio = pair()
    del plugin["provenance"]["build"]
    with pytest.raises(compare.InputError, match="provenance requires build"):
        receipt_for(plugin, studio)
    plugin, studio = pair()
    studio["versions"]["capability"] = "2"
    with pytest.raises(compare.InputError, match="capability version"):
        receipt_for(plugin, studio)


def test_synthetic_execution_is_not_a_positive_control():
    plugin, studio = pair()
    plugin["execution_mode"] = studio["execution_mode"] = "synthetic"
    assert receipt_for(plugin, studio)["comparator"]["verdict"] == "fail"


def test_file_entry_points_preserve_fail_and_validate_before_emitting(tmp_path):
    source = save_json(tmp_path / "graph.json", graph())
    context = save_json(tmp_path / "metadata.json", metadata())
    output = tmp_path / "studio.json"
    assert adapter.main(["--graph", str(source), "--metadata", str(context), "--family", "groups", "--output", str(output)]) == 0
    plugin, studio = differing_pair()
    plugin_file = save_json(tmp_path / "plugin.json", plugin)
    save_json(output, studio)
    receipts = tmp_path / "receipts"
    assert writer.main([
        "--plugin-input", str(plugin_file), "--studio-input", str(output),
        "--capability", CAPABILITY, "--capability-version", "1", "--family", "groups",
        "--receipts-dir", str(receipts),
    ]) == 1
    paths = list(receipts.rglob("*.json"))
    assert len(paths) == 1
    assert status.parse_receipt(paths[0], CAPABILITY)["comparator"]["verdict"] == "fail"
    invalid = metadata()
    invalid["survived_reopen"] = "yes"
    save_json(context, invalid)
    rejected = tmp_path / "rejected.json"
    assert adapter.main(["--graph", str(source), "--metadata", str(context), "--family", "groups", "--output", str(rejected)]) == 2
    assert not rejected.exists()
