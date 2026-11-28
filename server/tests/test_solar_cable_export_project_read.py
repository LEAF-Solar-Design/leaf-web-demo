"""Project Cable Export returns saved circuit changes and the current workbook."""
from copy import deepcopy
from hashlib import sha256
from uuid import uuid4

import pytest

import solar_local_graph
import solar_xlsx
from test_sip_r3b_tools import embedded
from test_sip_r6_artifacts import download, lane  # noqa: F401
from test_sip_r6_read import run
from test_solar_cable_revision_lengths import (
    DETOUR, DIRECT, FIRST, LONG, SAME_LENGTH_EAST, SAME_LENGTH_WEST, graph, revision,  # noqa: F401
)


def previous(s, value):
    s.versions.clear()
    s.parent = deepcopy(s.parent)
    s.parent.version_id = uuid4()
    s.set_parent(embedded(value))
    s.head = s.parent.version_id
    return s.head


def save_after(s, parent_id, value):
    raw = solar_local_graph.canonical_bytes(embedded(value))
    saved = s.publish(s.org, s.project, s.drawing, request_id=uuid4(),
                      expected_parent_version_id=parent_id, actor_binding_id=s.actor,
                      expected_fence=1, intake_sha256=sha256(raw).hexdigest(), before_new=lambda: None)
    s.blobs[saved.intake_ref] = raw
    return saved.version_id


def export(s):
    return run(s, "solar-cable-export", {"circuit_source": "labels"}, current=True)["result"]


def lengths(s, output):
    ref = output["artifact"]
    _, raw = download(s, ref["artifact_id"], current=True)
    assert sha256(raw).hexdigest() == ref["content_sha256"]
    sheet = next(sheet for sheet in solar_xlsx.read_workbook(raw) if sheet.name == "Homeruns")
    return [(row.cells[1], row.cells[5]) for row in sheet.rows if row.cells[2] == "Start Homerun"]


@pytest.mark.parametrize(("before", "after", "before_ft", "after_ft", "delta"), [
    (DIRECT, DETOUR, "36.09", "55.77", "19.68"),
    (LONG, DIRECT, "49.21", "36.09", "-13.12"),
    (SAME_LENGTH_EAST, SAME_LENGTH_WEST, "42.65", "42.65", "0.00"),
], ids=["longer", "shorter", "equal-length-reroute"])
def test_cable_export_reports_saved_comparison(lane, graph, before, after, before_ft, after_ft, delta):
    parent_id = previous(lane, revision(graph, 0, before))
    assert lengths(lane, export(lane)["output"])[0] == ("1", before_ft)
    after_id = save_after(lane, parent_id, revision(graph, 1, after))
    result = export(lane)
    comparison = result["circuit_comparison"]
    assert comparison["status"] == "ready", comparison
    assert (comparison["before_version_id"], comparison["after_version_id"]) == (str(parent_id), str(after_id))
    row, = comparison["changed_circuits"]
    assert (row["string_id"], row["before"]["length_ft"], row["after"]["length_ft"],
            row["delta_ft"], row["geometry_changed"]) == (FIRST, before_ft, after_ft, delta, True)
    assert lengths(lane, result["output"]) == [("1", after_ft), ("2", "36.09"), ("3", "62.34")]
    assert sha256(solar_local_graph.canonical_bytes(result["output"])).hexdigest() == result["output_sha256"]


def test_cable_export_remains_available_without_comparable_parent(lane, graph):
    parent_id = previous(lane, revision(graph, 0, DIRECT))
    initial = export(lane)
    assert initial["circuit_comparison"] == {"status": "no_previous_version"}
    save_after(lane, parent_id, revision(graph, 1, DETOUR))
    del lane.versions[parent_id]
    result = export(lane)
    assert result["circuit_comparison"] == {"status": "unavailable"}
    assert lengths(lane, result["output"])[0] == ("1", "55.77")


@pytest.mark.parametrize("fault", ["missing-graph", "skipped-graph-revision", "stale-parent"])
def test_cable_export_keeps_workbook_when_comparator_refuses(lane, graph, fault):
    parent_graph = revision(graph, 0, DIRECT)
    if fault == "stale-parent":
        parent_graph["strings"][0]["validity"] = {"state": "stale", "reasons": ["upstream_corrected"]}
    parent_id = previous(lane, parent_graph)
    if fault == "missing-graph":
        lane.set_parent({})
    after_rev = 2 if fault == "skipped-graph-revision" else 1
    save_after(lane, parent_id, revision(graph, after_rev, DETOUR))

    result = export(lane)

    assert result["circuit_comparison"] == {"status": "unavailable"}
    assert lengths(lane, result["output"]) == [("1", "55.77"), ("2", "36.09"), ("3", "62.34")]
