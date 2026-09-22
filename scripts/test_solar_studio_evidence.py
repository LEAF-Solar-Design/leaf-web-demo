"""Offline checks for the Studio evidence adapter's set-valued lists (contract rule G9).

Measured on the string-delete differential, 2026-09-22: the plugin and Studio agreed on
the same 14 unassigned panels and the comparator still returned fail, because the plugin
emits that set sorted by handle and the graph recorded it in its own uuid order. A set is
emitted in ascending neutral-id order on both sides; an ORDERED list (a string's
ordered_membership) is not a set and keeps the order the capability produced.

Every graph here is authored in this file. Nothing reads docs/parity, a capture, or the
network, so the counts are the same on every runner.
"""

from copy import deepcopy
import importlib.util
from pathlib import Path

import pytest


def load_module(name):
    spec = importlib.util.spec_from_file_location(name, Path(__file__).with_name(name + ".py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


adapter = load_module("solar_studio_evidence")
compare = adapter.compare
# The graph records these four freed panels in an order that is neither sorted nor
# reversed, so a list that arrives sorted can only have been sorted on purpose.
GRAPH_ORDER = ["panel-9", "panel-6", "panel-8", "panel-7"]
SORTED_ORDER = ["panel-6", "panel-7", "panel-8", "panel-9"]


def graph():
    return {
        "graph_schema_version": 1, "rev": 3,
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
        "panels": [{"id": f"panel-{i}", "centre": [i * 1000, 1000, 10], "angle": 0}
                   for i in range(1, 10)],
        # panel-3 leads string-1's membership, so the emitted order is the capability's
        # own and not a by-product of sorting.
        "strings": [
            {"id": "string-1", "ordered_panel_refs": ["panel-3", "panel-1", "panel-2"],
             "module_count": 3, "extra": {"polarity": {
                 "source": "derived", "source_rev": 0,
                 "negative_panel_ref": "panel-3", "positive_panel_ref": "panel-2"}}},
            {"id": "string-2", "ordered_panel_refs": ["panel-4", "panel-5"],
             "module_count": 2, "extra": {"polarity": {
                 "source": "derived", "source_rev": 0,
                 "negative_panel_ref": "panel-4", "positive_panel_ref": "panel-5"}}},
        ],
        "extra": {"solve_coverage": {
            "unassigned_panel_refs": list(GRAPH_ORDER), "duplicate_panel_refs": []}},
    }


def metadata():
    mapping = {"frame-1": "group:F1", "string-1": "string:A1", "string-2": "string:A4"}
    # A panel neutral id is its handle, so the neutral order is the handle order and the
    # graph's uuid order is unrelated to it.
    mapping.update({f"panel-{i}": f"A{i}" for i in range(1, 6)})
    mapping.update({f"panel-{i}": f"B{i - 5}" for i in range(6, 10)})
    return {
        "fixture_sha256": "a" * 64, "revision": "b" * 40,
        "versions": {"schema": "1", "producer": "test-only", "capability": "1",
                     "engine": "test-only", "catalog": "none", "solver": "none"},
        "parameters": {}, "entity_mapping": mapping,
        "before": {"strings": [], "unassigned_panels": [],
                   "duplicate_panels": [], "length_distribution": []},
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


def build(source, context):
    evidence = adapter.build_evidence(source, "strings", context)
    compare.validate_evidence(evidence, "strings")
    return evidence


def ids(records, context):
    return [context["entity_mapping"][record["entity_id"]] for record in records]


def test_unassigned_panels_are_emitted_in_neutral_id_order():
    source, context = graph(), metadata()
    originals = deepcopy((source, context))
    evidence = build(source, context)
    assert source["extra"]["solve_coverage"]["unassigned_panel_refs"] == GRAPH_ORDER
    assert evidence["after"]["unassigned_panels"] == [
        {"entity_id": identifier} for identifier in SORTED_ORDER]
    neutral = ids(evidence["after"]["unassigned_panels"], context)
    assert neutral == sorted(neutral) == ["B1", "B2", "B3", "B4"]
    # The adapter reads the graph and the metadata; it never rewrites either.
    assert (source, context) == originals


def test_duplicate_panels_are_emitted_in_neutral_id_order():
    source, context = graph(), metadata()
    coverage = source["extra"]["solve_coverage"]
    coverage["duplicate_panel_refs"] = ["panel-5", "panel-2", "panel-4"]
    evidence = build(source, context)
    assert evidence["after"]["duplicate_panels"] == [
        {"entity_id": identifier} for identifier in ("panel-2", "panel-4", "panel-5")]
    assert ids(evidence["after"]["duplicate_panels"], context) == ["A2", "A4", "A5"]
    # Sorting one set never disturbs the other.
    assert ids(evidence["after"]["unassigned_panels"], context) == ["B1", "B2", "B3", "B4"]


def test_an_ordered_membership_keeps_the_order_the_capability_produced():
    """ordered_membership is the capability's output, not a set, so G9 leaves it alone."""
    source, context = graph(), metadata()
    evidence = build(source, context)
    first = evidence["after"]["strings"][0]
    assert first["ordered_membership"] == [
        {"entity_id": "panel-3"}, {"entity_id": "panel-1"}, {"entity_id": "panel-2"}]
    assert ids(first["ordered_membership"], context) == ["A3", "A1", "A2"]
    assert first["polarity"] == "positive"


def test_records_still_sort_by_neutral_id():
    source, context = graph(), metadata()
    context["entity_mapping"].update({"string-1": "string:Z", "string-2": "string:A"})
    evidence = build(source, context)
    assert [record["id"]["entity_id"] for record in evidence["after"]["strings"]] == [
        "string-2", "string-1"]
    assert ids(evidence["after"]["unassigned_panels"], context) == ["B1", "B2", "B3", "B4"]
    # length_distribution is sorted by value and is not a set of entities.
    assert evidence["after"]["length_distribution"] == [2, 3]


def test_a_graph_with_nothing_unassigned_yields_empty_lists():
    source, context = graph(), metadata()
    source["extra"]["solve_coverage"] = {
        "unassigned_panel_refs": [], "duplicate_panel_refs": []}
    evidence = build(source, context)
    assert evidence["after"]["unassigned_panels"] == []
    assert evidence["after"]["duplicate_panels"] == []
    assert len(evidence["after"]["strings"]) == 2


def test_the_same_set_recorded_in_two_orders_compares_equal():
    """The measured failure: fourteen diffs over two equal sets in different orders."""
    left, context = graph(), metadata()
    right = graph()
    right["extra"]["solve_coverage"]["unassigned_panel_refs"] = list(SORTED_ORDER)
    plugin, studio = build(left, context), build(right, deepcopy(context))
    assert plugin["after"] == studio["after"]
    assert plugin["output_sha256"] == studio["output_sha256"]
    verdict = compare.compare_document({
        "schema": compare.SCHEMA, "capability": "string-delete", "family": "strings",
        "plugin": plugin, "studio": studio})
    assert verdict["verdict"] == "pass" and verdict["diffs"] == []


def test_a_reordered_membership_is_still_a_diff():
    """Sorting sets must not blunt the comparator: an ordered list still differs."""
    source, context = graph(), metadata()
    plugin = build(source, context)
    studio = deepcopy(plugin)
    studio["after"]["strings"][0]["ordered_membership"].reverse()
    studio["output_sha256"] = compare.semantic_hash(studio["after"])
    verdict = compare.compare_document({
        "schema": compare.SCHEMA, "capability": "string-delete", "family": "strings",
        "plugin": plugin, "studio": studio})
    assert verdict["verdict"] == "fail"
    assert any("after/strings/0/ordered_membership" in diff for diff in verdict["diffs"])


@pytest.mark.parametrize("field,refs,message", [
    ("unassigned", "panel-6", "panel reference arrays"),
    ("duplicate", {"panel-6": True}, "panel reference arrays"),
    ("unassigned", ["panel-6", "panel-404"], "explicit neutral mapping"),
    ("duplicate", ["panel-404"], "explicit neutral mapping"),
])
def test_a_malformed_set_is_refused_rather_than_sorted(field, refs, message):
    source, context = graph(), metadata()
    source["extra"]["solve_coverage"][field + "_panel_refs"] = refs
    with pytest.raises(compare.InputError, match=message):
        adapter.build_evidence(source, "strings", context)
