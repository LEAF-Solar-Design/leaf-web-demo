"""Verify circuit comparisons against the saved-graph Cable Export product."""
from copy import deepcopy
from dataclasses import replace
from datetime import datetime, timezone
from hashlib import sha256
from uuid import UUID

import pytest

from solar_cable_revision_lengths import compare_saved_circuit_lengths
from solar_design_graph import GraphValidationError, validate_graph
from solar_project_context import ProjectContextError, VerifiedProjectContext, VersionBinding
from solar_sizing_client import digest
from solar_local_graph import canonical_bytes
from leaf_platform.models import DrawingVersion
from test_solar_tool_cable_export import builtin, reopened
from test_w1_design_graph import app_id, graph  # noqa: F401


ORG, PROJECT, DRAWING, BEFORE, AFTER = (UUID(int=n) for n in range(1, 6))
FIRST, SECOND, THIRD = (app_id("string", n) for n in (1, 2, 3))
FIRST_LEG, SECOND_LEG, THIRD_LEG = (app_id("route", n) for n in (1, 2, 3))
COMMON_TAIL = [[1, 1], [3, 1], [3, 3], [5, 3], [5, 0]]
DIRECT = [[1, 0]]
LONG = [[1, 0], [2, 0], [2, 1], [2, 2], [1, 2]]
SAME_LENGTH_EAST = [[1, 0], [2, 0], [2, 1]]
SAME_LENGTH_WEST = [[1, 0], [0, 0], [0, 1]]
DETOUR = [[1, 0], [3, 0], [3, -1], [2, -1], [2, 1]]


def revision(graph, rev, lead, *, labels=("+1/1a", "+2/1a")):
    value = deepcopy(graph)
    value.update(rev=rev, parent_rev=rev - 1 if rev else None, schedules=[])
    first, second = value["strings"]
    third_panel, first_panel, _ = value["panels"]
    third = dict(deepcopy(first), id=THIRD, ordered_panel_refs=[third_panel["id"]],
                 from_ref=third_panel["id"], module_count=1)
    first.update(ordered_panel_refs=[first_panel["id"]], from_ref=first_panel["id"], module_count=1)
    third_panel["assignment"].update(string_ref=THIRD, seq=0)
    first_panel["assignment"]["seq"] = 0
    frame = value["frames"][0]
    for panel, matrix, assignment, input_number in zip(
            value["panels"], frame["matrix"][0], frame["panel_assignments"], (2, 0, 1), strict=True):
        matrix.update(seq=0, string_input_number=input_number)
        assignment.update(**panel["assignment"], string_input_number=input_number)
    value["strings"].append(third)
    frame["sequences"] = [
        {"string_ref": string["id"], "ordered_panel_refs": string["ordered_panel_refs"][:]}
        for string in value["strings"]
    ]
    inverter = value["inverters"][0]
    inverter["total_dc_inputs"] = value["settings"]["strings_per_mppt"] = 3
    inverter["input_assignments"].append({"string_ref": THIRD, "mppt_letter": "A", "input_number": 2})
    for string, label in zip(value["strings"], (*labels, "+3/1a"), strict=True):
        string["circuit_tag"] = label
    leg = value["routes"][0]
    # Hold the stored length fixed. Measurements must come from saved geometry.
    leg.update(points=lead + COMMON_TAIL)
    value["routes"].append(dict(deepcopy(leg), id=SECOND_LEG, from_ref=SECOND,
                                points=[[0, 1]] + COMMON_TAIL))
    value["routes"].append(dict(deepcopy(leg), id=THIRD_LEG, from_ref=THIRD,
                                points=[[1, 4]] + COMMON_TAIL[:3] + [[3, 5], [6, 5], [6, 0], [5, 0]]))
    return validate_graph(value)


def saved(graph, version_id, seq, *, parent=None, drawing_id=DRAWING):
    intake = {} if graph is None else {
        "solar_design_graph": graph, "solar_design_graph_sha256": digest(graph),
    }
    raw = canonical_bytes(intake)
    ref = f"fixture-{version_id}"
    version = DrawingVersion(version_id, drawing_id, PROJECT, ORG, seq, intake_ref=ref,
                             created_at=datetime(2026, 11, 26, tzinfo=timezone.utc),
                             provenance={"parent_version_id": str(parent)} if parent else None)
    binding = VersionBinding(ORG, PROJECT, drawing_id, version_id, AFTER, version_id == AFTER, version)
    return VerifiedProjectContext(binding, raw, intake, sha256(raw).hexdigest(), ref)


def compare(before, after):
    return compare_saved_circuit_lengths(saved(before, BEFORE, 1), saved(after, AFTER, 2, parent=BEFORE))


def workbook_lengths(graph):
    artifact = builtin().run(graph, {"circuit_source": "labels"})
    return [(row[1], row[5]) for _, row in dict(reopened(artifact))["Homeruns"]
            if row[2] == "Start Homerun"]


@pytest.mark.parametrize(
    ("before_path", "after_path", "before_ft", "after_ft", "delta"),
    [
        (DIRECT, DETOUR, "36.09", "55.77", "19.68"),
        (LONG, DIRECT, "49.21", "36.09", "-13.12"),
        (SAME_LENGTH_EAST, SAME_LENGTH_WEST, "42.65", "42.65", "0.00"),
    ],
    ids=["lengthened", "shortened", "equal-length-reroute"],
)
def test_shared_aisle_changes(graph, before_path, after_path, before_ft, after_ft, delta):
    before, after = revision(graph, 0, before_path), revision(graph, 1, after_path)
    assert workbook_lengths(before) == [("1", before_ft), ("2", "36.09"), ("3", "62.34")]
    assert workbook_lengths(after) == [("1", after_ft), ("2", "36.09"), ("3", "62.34")]
    assert compare(before, after) == {
        "before_version_id": str(BEFORE),
        "after_version_id": str(AFTER),
        "changed_circuits": [{
            "string_id": FIRST,
            "before": {"label": "+1/1a", "length_ft": before_ft, "route_ids": {"start": FIRST_LEG}},
            "after": {"label": "+1/1a", "length_ft": after_ft, "route_ids": {"start": FIRST_LEG}},
            "geometry_changed": True,
            "delta_ft": delta,
        }],
    }


@pytest.mark.parametrize(
    ("labels", "lead", "expected_ids", "feet", "workbook"),
    [
        (("+4/1a", "+2/1a"), DIRECT, [FIRST], "36.09",
         [("2", "36.09"), ("3", "62.34"), ("4", "36.09")]),
        (("+2/1a", "+1/1a"), DETOUR, [FIRST, SECOND], "55.77",
         [("1", "36.09"), ("2", "55.77"), ("3", "62.34")]),
        (("+1/1a", "+1/1a"), DETOUR, [FIRST, SECOND], "55.77",
         [("1", "91.86"), ("1", "91.86"), ("3", "62.34")]),
    ],
    ids=["rename", "swap-with-reorder", "duplicate-labels"],
)
def test_circuit_identity_uses_saved_ids(graph, labels, lead, expected_ids, feet, workbook):
    before, after = revision(graph, 0, DIRECT), revision(graph, 1, lead, labels=labels)
    after["strings"].reverse()
    after["routes"].reverse()
    assert workbook_lengths(after) == workbook
    rows = compare(before, after)["changed_circuits"]
    assert [row["string_id"] for row in rows] == expected_ids
    assert rows[0]["after"] == {"label": labels[0], "length_ft": feet, "route_ids": {"start": FIRST_LEG}}
    assert rows[0]["geometry_changed"] == (lead != DIRECT)
    if len(rows) == 2:
        assert rows[1]["after"] == {"label": labels[1], "length_ft": "36.09", "route_ids": {"start": SECOND_LEG}}
        assert rows[1]["geometry_changed"] is False


def test_route_replacement_keeps_circuit_identity(graph):
    before, after = revision(graph, 0, DIRECT), revision(graph, 1, DIRECT)
    new_route_id = app_id("route", 7)
    after["routes"][0]["id"] = new_route_id
    row, = compare(before, after)["changed_circuits"]
    assert row["string_id"] == FIRST
    assert row["before"]["route_ids"] == {"start": FIRST_LEG}
    assert row["after"]["route_ids"] == {"start": new_route_id}
    assert row["delta_ft"] == "0.00"
    assert row["geometry_changed"] is False


def test_metadata_only_saved_version_has_no_circuit_changes(graph):
    snapshot = revision(graph, 0, DIRECT)
    assert compare(snapshot, snapshot)["changed_circuits"] == []


@pytest.mark.parametrize(
    ("fault", "code"),
    [
        ("other-drawing", "CABLE_COMPARISON_SCOPE_MISMATCH"),
        ("wrong-parent", "CABLE_COMPARISON_PAIR_NOT_ADJACENT"),
        ("skipped-version", "CABLE_COMPARISON_PAIR_NOT_ADJACENT"),
        ("skipped-graph-revision", "CABLE_COMPARISON_PAIR_NOT_ADJACENT"),
        ("missing-graph", "CABLE_COMPARISON_GRAPH_REQUIRED"),
        ("stale", "SOLAR_OUTPUT_NOT_CURRENT"),
    ],
)
def test_refuses_unproven_or_stale_comparison(graph, fault, code):
    old = saved(revision(graph, 0, DIRECT), BEFORE, 1)
    new_graph = revision(graph, 2 if fault == "skipped-graph-revision" else 1, DETOUR)
    if fault == "stale":
        new_graph["strings"][0]["validity"] = {"state": "stale", "reasons": ["upstream_corrected"]}
    new = saved(None if fault == "missing-graph" else new_graph, AFTER,
                3 if fault == "skipped-version" else 2,
                parent=AFTER if fault == "wrong-parent" else BEFORE,
                drawing_id=UUID(int=6) if fault == "other-drawing" else DRAWING)
    with pytest.raises(GraphValidationError, match=code):
        compare_saved_circuit_lengths(old, new)


def test_changed_intake_bytes_refuse_before_measuring(graph):
    old = saved(revision(graph, 0, DIRECT), BEFORE, 1)
    new = saved(revision(graph, 1, DETOUR), AFTER, 2, parent=BEFORE)
    with pytest.raises(ProjectContextError, match="INTAKE_DIGEST_MISMATCH"):
        compare_saved_circuit_lengths(old, replace(new, intake_bytes=b"{}"))
