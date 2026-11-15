"""Imported tracking groups must not require or fabricate electrical sizing."""
import copy
import hashlib
import json
from uuid import UUID

import pytest

import solar_local_graph as local
import solar_project_graph as project
from solar_design_graph import GraphValidationError, validate_graph
from solar_sizing_client import require_sizing
from test_w1_design_graph import graph  # noqa: F401


@pytest.fixture
def unsized(graph):
    value = copy.deepcopy(graph)
    for key in ("frames", "strings", "inverters", "routes", "schedules", "electrical_zones"):
        value[key] = []
    for index, panel in enumerate(value["panels"]):
        panel.update(frame_ref=None, matrix_cell=None, assignment={"string_ref": None, "seq": None})
        panel["provenance"]["source_handle"] = f"A{index + 1}"
    value["settings"].update(global_string_sizing_confirmed=False, panels_in_sequence=0)
    value["settings"]["extra"] = {}
    return validate_graph(value)


def request(value, **changes):
    return {"expected_rev": value["rev"], "groups": [{
        "name": "Imported geometry", "panel_refs": [p["id"] for p in value["panels"]],
        "alignment_tolerance": 0.5, "module_width_along_row": 1,
        "module_height_across_row": 2}], **changes}


def test_explicit_geometry_groups_keep_sizing_unknown_and_input_unchanged(unsized):
    before = copy.deepcopy(unsized)
    result = local._load_builtin("solar-panel-groups").run(
        unsized, request(unsized, geometry_only=True))
    assert unsized == before
    assert result["rev"] == before["rev"] + 1
    assert len(result["frames"]) == 1
    frame = result["frames"][0]
    assert frame["module_power_watts"] == 0
    assert frame["installation_design"] == "Roof"
    assert frame["extra"] == {"grouping_basis": "geometry-only", "module_power_status": "unknown",
                              "sizing_evidence_mode": "not-requested"}
    assert result["settings"] == before["settings"]
    assert result["source_hash"] == before["source_hash"]
    for key in ("strings", "inverters", "routes", "schedules", "electrical_zones"):
        assert result[key] == []
    with pytest.raises(GraphValidationError, match="SIZING_CONFIRMATION_REQUIRED"):
        require_sizing(result)


@pytest.mark.parametrize("changes", [{}, {"geometry_only": False}])
def test_default_still_requires_sizing(unsized, changes):
    with pytest.raises(GraphValidationError, match="SIZING_CONFIRMATION_REQUIRED"):
        local._load_builtin("solar-panel-groups").run(unsized, request(unsized, **changes))


@pytest.mark.parametrize("value", [None, 0, 1, "true", [], {}])
def test_geometry_flag_requires_boolean(unsized, value):
    with pytest.raises(GraphValidationError, match="INVALID_GROUP_REQUEST"):
        local._load_builtin("solar-panel-groups").run(unsized, request(unsized, geometry_only=value))


def test_geometry_mode_refuses_existing_electrical_design(graph):
    before = copy.deepcopy(graph)
    with pytest.raises(GraphValidationError, match="GEOMETRY_GROUPS_REQUIRE_UNASSIGNED_DESIGN"):
        local._load_builtin("solar-panel-groups").run(graph, request(graph, geometry_only=True))
    assert graph == before


def test_geometry_mode_does_not_invent_ground_tracker_evidence(unsized):
    unsized["project"]["installation_design"] = "Ground"
    with pytest.raises(GraphValidationError, match="GEOMETRY_GROUPS_REQUIRE_ROOF_DESIGN"):
        local._load_builtin("solar-panel-groups").run(unsized, request(unsized, geometry_only=True))


def test_canonical_candidate_preserves_source_and_reopens_unsized_groups(unsized):
    intake = {"solar_design_graph": unsized, "solar_design_graph_sha256": local.digest(unsized),
              "source_marker": {"preserve": "synthetic original intake"}}
    raw = local.canonical_bytes(intake)
    context = project.ProjectGraphContext(*(UUID(int=n) for n in range(1, 5)), raw,
        hashlib.sha256(raw).hexdigest(), "2026-01-01T00:00:00Z")
    params = request(unsized, geometry_only=True)
    before, after, base, output = project._candidate(context, "solar-panel-groups", params,
                                                   False, UUID(int=5))
    reopened = json.loads(output)
    assert before == unsized and base is None
    assert reopened["source_marker"] == intake["source_marker"]
    assert reopened["solar_design_graph_sha256"] == local.digest(after)
    assert project._companion(reopened) == after
    assert after["frames"][0]["provenance"]["created_at"] == context.created_at
    assert after["frames"][0]["id"] == project._creation_id(context, "solar-panel-groups", params, "frame", 0)
    assert after["settings"] == unsized["settings"]
