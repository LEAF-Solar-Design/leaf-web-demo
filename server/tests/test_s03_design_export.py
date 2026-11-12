"""Synthetic S03 full-design JSON export through the existing read artifact rail."""
from copy import deepcopy
from hashlib import sha256
import json

import pytest
from jsonschema import Draft7Validator

import solar_local_read as local
import solar_sizing_client as sizing
import solar_tools
from solar_design_graph import GraphValidationError
from test_w1_design_graph import graph  # noqa: F401

TOOL = "solar-string-data"


def manual_graph(graph):
    value = deepcopy(graph)
    value["frames"][0]["module_power_watts"] = 0
    value["frames"][0]["extra"] = {"grouping_basis": "geometry-only",
        "module_power_status": "unknown", "sizing_evidence_mode": "manual-global"}
    value["strings"][0]["extra"] = {"correction": {"note": "synthetic operator change"}}
    value["settings"].update(voc_cold=sizing.manual_voltage())
    value["settings"]["extra"]["string_sizing"] = sizing.manual_sizing_evidence(value)
    return value


def test_full_design_preserves_geometry_membership_and_unknown_power(graph):
    value = manual_graph(graph)
    before = deepcopy(value)
    result = local._load_builtin(TOOL).run(value, {"format": "design-json"})
    payload = json.loads(result.content)
    assert payload == {"schema": "leaf.solar-design-export.v1",
        "electrical_verification": "not-evaluated", "graph": value}
    assert result.filename == "SolarDesign.json"
    assert result.media_type == "application/json"
    assert result.summary["panels"] == 3
    assert result.summary["groups"] == 1
    assert result.summary["strings"] == 2
    assert value == before
    assert payload["graph"]["frames"][0]["module_power_watts"] == 0
    assert payload["graph"]["strings"][0]["ordered_panel_refs"] == value["strings"][0]["ordered_panel_refs"]
    assert payload["graph"]["strings"][0]["extra"]["correction"] == {"note": "synthetic operator change"}
    assert local._load_builtin(TOOL).run(value, {"format": "design-json"}).content == result.content


def test_legacy_string_data_unchanged(graph):
    module = local._load_builtin(TOOL)
    assert module.run(graph, {}) == module.run(graph, {"format": "string-data"})
    assert module.run(graph, {}).filename == "StringData.json"


@pytest.mark.parametrize("params", [{"format": True}, {"format": "other"},
    {"format": []}, {"format": "design-json", "string_refs": []}])
def test_design_export_refuses_ambiguous_or_invalid_format(graph, params):
    with pytest.raises(GraphValidationError, match="INVALID_STRING_DATA_REQUEST"):
        local._load_builtin(TOOL).run(manual_graph(graph), params)


def test_design_export_refuses_stale_design(graph):
    value = manual_graph(graph)
    value["panels"][0]["validity"] = {"state": "stale", "reasons": ["source_changed"]}
    with pytest.raises(GraphValidationError, match="SOLAR_OUTPUT_NOT_CURRENT"):
        local._load_builtin(TOOL).run(value, {"format": "design-json"})


def test_design_export_requires_current_manual_evidence(graph):
    value = manual_graph(graph)
    value["settings"]["extra"]["string_sizing"]["basis_sha256"] = "0" * 64
    with pytest.raises(GraphValidationError, match="SIZING_CONFIRMATION_REQUIRED"):
        local._load_builtin(TOOL).run(value, {"format": "design-json"})


def test_design_export_declaration_is_additive():
    params = solar_tools.trusted_record(TOOL)["params"]
    validator = Draft7Validator(params)
    assert validator.is_valid({})
    assert validator.is_valid({"format": "design-json"})
    assert not validator.is_valid({"format": "other"})


def test_full_design_uses_canonical_bound_artifact_download(graph, monkeypatch):
    import solar_project_artifacts as artifacts
    import test_sip_r6_read as reads

    # Start at a fresh imported source so the memory head names this graph,
    # rather than the last publication of an unrelated partial workflow.
    monkeypatch.setenv("LEAF_SOLAR_PROJECT_RUN_ENABLED", "1")
    lane = reads.tools_cases.memory.__wrapped__(monkeypatch)
    lane.set_parent(reads.tools_cases.embedded(manual_graph(graph)))
    lane.ctx = reads.graph_adapter.resolve_project_graph_context(lane.context())
    lane.tenant = reads.deps.TenantContext(str(lane.org), org_id=str(lane.org),
        tier="hosted_pro", subject="reader")
    monkeypatch.setattr(reads.deps, "find_tool", lambda name, tenant: solar_tools.trusted_record(name))
    monkeypatch.setattr(reads.read.stored, "stored_job_entitlement_verdict", lambda org, kind: (None, None))
    monkeypatch.setattr(artifacts.write_loop, "drawing_mutations_refusal", lambda: None)
    before = deepcopy(lane.versions)
    result = reads.run(lane, TOOL, {"format": "design-json"}, current=True)["result"]
    ref = result["output"]["artifact"]
    meta, content = artifacts.read_project_artifact(lane.tenant, lane.project, lane.drawing,
        ref["artifact_id"], current=True)
    assert json.loads(content)["graph"] == lane.ctx.graph
    assert ref["project_id"] == str(lane.project)
    assert ref["drawing_id"] == str(lane.drawing)
    assert ref["source_version_id"] == str(lane.head)
    assert meta["source_intake_sha256"] == lane.ctx.intake_sha256
    assert meta["graph_sha256"] == lane.ctx.graph_sha256
    assert meta["content_sha256"] == sha256(content).hexdigest()
    assert lane.versions == before
    assert result["drawing_changed"] is False


def test_design_export_refuses_incomplete_panel_coverage(graph):
    from solar_solve_results import sync_assignments

    value = manual_graph(graph)
    value["strings"].pop()
    value["inverters"][0]["input_assignments"].pop()
    value["schedules"] = []
    sync_assignments(value)
    with pytest.raises(GraphValidationError, match="SOLAR_OUTPUT_NOT_CURRENT"):
        local._load_builtin(TOOL).run(value, {"format": "design-json"})


def test_design_export_refuses_empty_design(graph):
    value = manual_graph(graph)
    for key in ("panels", "frames", "strings", "electrical_zones", "inverters", "routes", "schedules"):
        value[key] = []
    with pytest.raises(GraphValidationError, match="SOLAR_OUTPUT_NOT_CURRENT"):
        local._load_builtin(TOOL).run(value, {"format": "design-json"})
