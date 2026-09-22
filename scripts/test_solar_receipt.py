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
        "fixture_sha256": "a" * 64, "revision": "b" * 40,
        "versions": {"schema": "1", "producer": "test-only", "capability": "1",
                     "engine": "test-only", "catalog": "none", "solver": "none"},
        "parameters": {}, "entity_mapping": {"frame-1": "group:1F4", "panel-1": "1F4"},
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


def strings_case():
    source, context = graph(), metadata()
    source["strings"] = [
        {"id": "string-1", "ordered_panel_refs": ["panel-1", "panel-2", "panel-3"],
         "module_count": 3, "extra": {"polarity": {
             "source": "derived", "source_rev": 0,
             "negative_panel_ref": "panel-1", "positive_panel_ref": "panel-3"}}},
        {"id": "string-2", "ordered_panel_refs": ["panel-4", "panel-5"],
         "module_count": 2, "extra": {"polarity": {
             "source": "derived", "source_rev": 0,
             "negative_panel_ref": "panel-4", "positive_panel_ref": "panel-5"}}},
    ]
    source["panels"] = [
        {"id": f"panel-{i}", "centre": [i * 1000, 1000, 10], "angle": 0}
        for i in range(1, 7)
    ]
    source["extra"] = {"solve_coverage": {
        "unassigned_panel_refs": ["panel-6"], "duplicate_panel_refs": []}}
    context["entity_mapping"].update({f"panel-{i}": f"panel-neutral-{i}" for i in range(2, 7)})
    context["entity_mapping"].update({"string-1": "string-one", "string-2": "string-two"})
    context["before"] = {"strings": [], "unassigned_panels": [],
                         "duplicate_panels": [], "length_distribution": []}
    return source, context


def test_strings_evidence_validates_order_and_sorted_lengths():
    source, context = strings_case()
    originals = deepcopy((source, context))
    evidence = adapter.build_evidence(source, "strings", context)
    compare.validate_evidence(evidence, "strings")
    assert (source, context) == originals
    assert evidence["after"] == {
        "strings": [
            {"id": {"entity_id": "string-1"}, "ordered_membership": [
                {"entity_id": "panel-1"}, {"entity_id": "panel-2"}, {"entity_id": "panel-3"}],
             "polarity": "positive"},
            {"id": {"entity_id": "string-2"}, "ordered_membership": [
                {"entity_id": "panel-4"}, {"entity_id": "panel-5"}], "polarity": "positive"},
        ],
        "unassigned_panels": [{"entity_id": "panel-6"}],
        "duplicate_panels": [], "length_distribution": [2, 3],
    }


def test_strings_polarity_requires_matching_endpoints():
    source, context = strings_case()
    source["strings"][1]["ordered_panel_refs"].reverse()
    evidence = adapter.build_evidence(source, "strings", context)
    assert [s["polarity"] for s in evidence["after"]["strings"]] == ["positive", "negative"]
    source["strings"][0]["extra"]["polarity"]["negative_panel_ref"] = "panel-2"
    with pytest.raises(adapter.compare.InputError, match="polarity"):
        adapter.build_evidence(source, "strings", context)


@pytest.mark.parametrize("family", ["groups", "panels", "strings"])
def test_shared_identity_is_derived_and_graph_identity_is_provenance(family):
    source, context = strings_case()
    evidence = adapter.build_evidence(source, family, context)
    assert evidence["revision"] == context["revision"]
    assert evidence["provenance"]["studio_graph_rev"] == source["rev"]
    assert evidence["provenance"]["studio_graph_sha256"] == compare.semantic_hash(source)
    assert evidence["input_sha256"] == compare.semantic_hash({
        "fixture_sha256": context["fixture_sha256"], "parameters": context["parameters"]})
    source["rev"] += 1
    changed = adapter.build_evidence(source, family, context)
    assert changed["revision"] == evidence["revision"]
    assert changed["input_sha256"] == evidence["input_sha256"]
    assert changed["provenance"] != evidence["provenance"]
    context["parameters"]["changed"] = True
    assert adapter.build_evidence(source, family, context)["input_sha256"] != evidence["input_sha256"]


@pytest.mark.parametrize("family", ["groups", "panels", "strings"])
def test_caller_cannot_supply_input_hash(family):
    source, context = strings_case()
    context["input_sha256"] = compare.semantic_hash({
        "fixture_sha256": context["fixture_sha256"], "parameters": context["parameters"]})
    with pytest.raises(adapter.compare.InputError, match="input_sha256"):
        adapter.build_evidence(source, family, context)


def test_revision_is_required_lowercase_full_git_commit():
    for revision in (None, 1, "1", "A" * 40, "a" * 39, "a" * 41, "g" * 40):
        context = metadata()
        context["revision"] = revision
        with pytest.raises(adapter.compare.InputError, match="revision"):
            adapter.build_evidence(graph(), "panels", context)
    context = metadata()
    del context["revision"]
    with pytest.raises(adapter.compare.InputError, match="missing measured"):
        adapter.build_evidence(graph(), "groups", context)


def test_strings_sort_by_neutral_id_without_reordering_members():
    source, context = strings_case()
    context["entity_mapping"].update({"string-1": "string:Z", "string-2": "string:A"})
    originals = deepcopy((source, context))
    evidence = adapter.build_evidence(source, "strings", context)
    assert [s["id"]["entity_id"] for s in evidence["after"]["strings"]] == ["string-2", "string-1"]
    assert evidence["after"]["strings"][1]["ordered_membership"] == [
        {"entity_id": ref} for ref in source["strings"][0]["ordered_panel_refs"]]
    assert (source, context) == originals


def test_replay_uses_frozen_comparator_recorded_mode():
    source, context = strings_case()
    context["execution_mode"] = "replay"
    assert adapter.build_evidence(source, "strings", context)["execution_mode"] == "recorded"
    assert context["execution_mode"] == "replay"


def test_strings_missing_polarity_is_flagged():
    source, context = strings_case()
    del source["strings"][0]["extra"]["polarity"]
    evidence = adapter.build_evidence(source, "strings", context)
    assert evidence["after"]["strings"][0]["polarity"] == "none"
    assert "after/strings/0/polarity" in evidence["fallback_fields"]
    assert evidence["synthetic_flagged"] is True


def test_strings_require_committed_solve_coverage():
    source, context = strings_case()
    del source["extra"]["solve_coverage"]
    with pytest.raises(adapter.compare.InputError, match="solve_coverage"):
        adapter.build_evidence(source, "strings", context)


def test_strings_reject_invalid_module_counts():
    for count in (-1, 2.5, "3", True, None):
        source, context = strings_case()
        source["strings"][0]["module_count"] = count
        with pytest.raises(adapter.compare.InputError, match="module_count"):
            adapter.build_evidence(source, "strings", context)


def test_strings_comparison_detects_reversed_membership():
    source, context = strings_case()
    plugin = adapter.build_evidence(source, "strings", context)
    studio = deepcopy(plugin)
    document = {"schema": compare.SCHEMA, "capability": "solve", "family": "strings",
                "plugin": plugin, "studio": studio}
    verdict = compare.compare_document(document)
    assert verdict["verdict"] == "pass"
    assert verdict["diffs"] == []
    studio["after"]["strings"][0]["ordered_membership"].reverse()
    studio["output_sha256"] = compare.semantic_hash(studio["after"])
    verdict = compare.compare_document(document)
    assert verdict["verdict"] == "fail"
    assert any("after/strings/0/ordered_membership" in diff for diff in verdict["diffs"])


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
        assert group["name"] == "group:1F4"
        for field in ("boundaries", "elevation", "equipment_config", "sizing_provenance"):
            assert group[field] is None
            assert "groups/" + field + "/unrecorded" in evidence["fallback_fields"]
        assert evidence["provenance"]["group_names"] == {"group:1F4": "Group one"}
        assert evidence["synthetic_flagged"] is True
    else:
        assert evidence["after"]["panels"][0]["geometry"]["centre"]["value"] == [500, 1000, 10]


def two_groups_case():
    """Frame labels and listing order that disagree with the neutral order on purpose."""
    source, context = graph(), metadata()
    source["panels"] += [{"id": f"panel-{tag}", "centre": [i * 1000, 0, 10], "angle": 0}
                         for i, tag in enumerate(("a", "b", "c"), 2)]
    source["frames"].insert(0, {
        "id": "frame-2", "name": "Second", "insertion_point": [0, 0],
        "panel_refs": ["panel-b", "panel-a", "panel-c"], "installation_design": "Roof",
        "module_rows": 1, "module_columns": 3, "module_slots": 3,
        "module_power_watts": 400, "module_width_along_row": 1000,
        "module_height_across_row": 2000,
    })
    # "1F" sorts before "2" as a string and after it as a hex value.
    context["entity_mapping"].update({"frame-2": "group:2", "panel-a": "A", "panel-b": "1F", "panel-c": "2"})
    return source, context


def test_groups_follow_contract_v2_neutral_names_and_handle_order():
    source, context = two_groups_case()
    originals = deepcopy((source, context))
    evidence = adapter.build_evidence(source, "groups", context)
    compare.validate_evidence(evidence, "groups")
    assert (source, context) == originals
    assert [g["id"]["entity_id"] for g in evidence["after"]["groups"]] == ["frame-1", "frame-2"]
    assert [g["name"] for g in evidence["after"]["groups"]] == ["group:1F4", "group:2"]
    assert evidence["after"]["groups"][1]["membership"] == [
        {"entity_id": "panel-c"}, {"entity_id": "panel-a"}, {"entity_id": "panel-b"}]
    assert evidence["provenance"]["group_names"] == {"group:1F4": "Group one", "group:2": "Second"}
    assert evidence["fallback_fields"] == [
        "groups/boundaries/unrecorded", "groups/elevation/unrecorded",
        "groups/equipment_config/unrecorded", "groups/sizing_provenance/unrecorded"]
    assert "group_names" not in adapter.build_evidence(source, "panels", context)["provenance"]


def test_groups_refuse_a_member_neutral_id_that_is_not_a_handle():
    source, context = two_groups_case()
    context["entity_mapping"]["panel-a"] = "panel-one"
    with pytest.raises(adapter.compare.InputError, match="hex handle"):
        adapter.build_evidence(source, "groups", context)
    context["entity_mapping"]["panel-a"] = "a"
    with pytest.raises(adapter.compare.InputError, match="hex handle"):
        adapter.build_evidence(source, "groups", context)


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
