"""Explicit partial saves on the real producer/version rail with synthetic material."""
from copy import deepcopy
from dataclasses import replace
from hashlib import sha256

import pytest

import deps
import solar_local_graph as local
import solar_project_context as project
import solar_project_graph as graphs
import solar_project_read as reads
import solar_project_solaredge as service
import solar_tools
from solar_design_graph import GraphValidationError
import test_sip_r3b_tools as tools
from test_w1_design_graph import graph, app_id  # noqa: F401


def grid(length):
    return {"Dwgname": "synthetic", "Sequences": [length],
        "Rows": [{"Panels": [{"Code": 1, "Id": f"{i:04x}", "Seq": i + 1,
                                "InverterId": 1, "StringInputNumber": 1} for i in range(length)]}],
        "SubGrids": [{"Id": 0, "Cells": []}], "BridgeConnections": []}


@pytest.fixture
def mixed(graph, monkeypatch, request):
    value = deepcopy(graph)
    for key in ("frames", "panels", "strings", "inverters", "electrical_zones", "routes", "schedules"):
        value[key] = []
    value["settings"].update(global_string_sizing_confirmed=False, panels_in_sequence=0, extra={})
    whole = getattr(request, "param", None) == "whole"
    positions = [(1, 0), (0, 0), (1, 1), (0, 1)] if whole else [(x, 0) for x in (0, 10, 100, 110, 120)]
    for index, (x, y) in enumerate(positions, 1):
        panel = deepcopy(graph["panels"][0])
        panel.update(id=app_id("panel", index), frame_ref=None, matrix_cell=None, centre=[x, y],
                     assignment={"string_ref": None, "seq": None})
        panel["provenance"]["source_handle"] = f"A{index}"
        value["panels"].append(panel)
    memberships = (value["panels"],) if whole else (value["panels"][:2], value["panels"][2:])
    groups = [{"name": f"Roof {i}", "panel_refs": [p["id"] for p in members],
               "alignment_tolerance": 0.1, "module_width_along_row": 1,
               "module_height_across_row": 2}
              for i, members in enumerate(memberships, 1)]
    value = local._load_builtin("solar-panel-groups").run(value,
        {"expected_rev": value["rev"], "groups": groups, "geometry_only": True})
    s = tools.memory.__wrapped__(monkeypatch)
    s.set_parent(tools.embedded(value))
    s.ctx = graphs.resolve_project_graph_context(s.context())
    s.tenant = deps.TenantContext(str(s.org), org_id=str(s.org), tier="hosted_pro", subject="drafter")
    monkeypatch.setenv("LEAF_SOLAR_PROJECT_RUN_ENABLED", "1")
    monkeypatch.setattr(service.stored, "stored_job_entitlement_verdict", lambda *args: (None, None))
    monkeypatch.setattr(service.write_loop, "drawing_mutations_refusal", lambda: None)
    monkeypatch.setattr(deps, "find_tool", lambda name, *args: solar_tools.trusted_record(name))
    def access(tenant, proj, *, write):
        if str(tenant) != str(s.org) or proj != s.project:
            project.refuse("CONTEXT_NOT_FOUND")
        return s.org, s.actor
    monkeypatch.setattr(project, "_access", access)
    s.matrices = [grid(2)]
    monkeypatch.setattr(service.reports, "parse_source", lambda data: deepcopy(s.matrices))
    pdf = (b"%PDF-1.4\n1 0 obj<</Type/Catalog/Pages 2 0 R>>endobj\n"
           b"2 0 obj<</Type/Pages/Kids[3 0 R]/Count 1>>endobj\n"
           b"3 0 obj<</Type/Page/Parent 2 0 R/MediaBox[0 0 72 72]>>endobj\n"
           b"trailer<</Root 1 0 R>>\n%%EOF\n")
    source = service.import_source(s.tenant, s.project, s.drawing, s.ctx.parent_version_id, pdf)
    s.source_id = source["source"]["artifact_id"]
    return s


def report(s):
    return service.build_report(s.tenant, s.project, s.drawing, s.ctx.parent_version_id,
        {"source_artifact_id": s.source_id, "alignment_tolerance": 0.1, "selection_order": "unknown"})


def params(s, result, **extra):
    return {"expected_rev": s.ctx.graph["rev"], "report_artifact_id": result["report"]["artifact_id"], **extra}


@pytest.mark.parametrize("unresolved", ["unmatched", "ambiguous"])
def test_explicit_partial_publishes_reopens_and_preserves_unresolved_work(mixed, unresolved):
    s = mixed
    if unresolved == "ambiguous":
        s.matrices += [grid(3), grid(3)]
    result = report(s)
    before = deepcopy(s.ctx.graph)
    prepared = tools.prepare(s, service.ACCEPT_TOOL, params(s, result, accept_matched_only=True), s.ctx)
    output = tools.publish(s, prepared)
    accepted = tools.output(s, output)
    record = accepted.graph["extra"][service.tracking.STORE_KEY]
    assert record["acceptance"] == {"mode": "matched-only", "status": "partial",
                                     "unresolved_frame_refs": [before["frames"][1]["id"]]}
    assert len(record["frames"]) == len(record["strings"]) == 1
    assert len(record["unassigned_panel_refs"]) == 3
    assert record["source"]["artifact_id"] == s.source_id
    assert record["report"]["artifact_id"] == result["report"]["artifact_id"]
    for key in ("settings", "panels", "frames", "strings", "inverters", "routes", "schedules"):
        assert accepted.graph[key] == before[key]
    assert tools.publish(s, prepared) == output
    readback = service.read_tracking(s.tenant, accepted, {})
    assert readback["accepted"] is True
    assert readback["acceptance"] == {"mode": "matched-only", "status": "partial", "unresolved_groups": 1}
    assert "need correction" in readback["one_liner"]
    assert readback["counts"]["assigned_panels"] == 2
    canonical_before = reads.run_project_read(s.tenant, s.project, s.drawing, accepted.parent_version_id,
        service.READ_TOOL, {}, catalog_digest=tools.manifest(service.READ_TOOL))["result"]["output"]
    tools.commit(s, "solar-settings", {"expected_rev": accepted.graph["rev"],
        "project_changes": {"name": "Later synthetic revision"}}, accepted)
    historical = reads.run_project_read(s.tenant, s.project, s.drawing, accepted.parent_version_id,
        service.READ_TOOL, {}, catalog_digest=tools.manifest(service.READ_TOOL))["result"]
    assert historical["is_head"] is False
    assert historical["output"] == canonical_before


@pytest.mark.parametrize("extra", [{}, {"accept_matched_only": False}])
def test_default_acceptance_stays_strict(mixed, extra):
    result = report(mixed)
    before = (mixed.head, len(mixed.versions), deepcopy(mixed.ctx.graph))
    with pytest.raises(GraphValidationError, match="SOLAREDGE_REPORT_INVALID"):
        service.accept(mixed.ctx, params(mixed, result, **extra))
    assert (mixed.head, len(mixed.versions), mixed.ctx.graph) == before


@pytest.mark.parametrize("value", [None, 0, 1, "true", {}, []])
def test_partial_choice_requires_a_boolean(mixed, value):
    result = report(mixed)
    with pytest.raises(GraphValidationError, match="INVALID_SOLAREDGE_ACCEPT_REQUEST"):
        service.accept(mixed.ctx, params(mixed, result, accept_matched_only=value))


def test_partial_choice_cannot_save_an_all_unresolved_report(mixed):
    mixed.matrices = []
    result = report(mixed)
    before = deepcopy(mixed.ctx.graph)
    with pytest.raises(GraphValidationError, match="SOLAREDGE_MATCHED_TRACKING_REQUIRED"):
        service.accept(mixed.ctx, params(mixed, result, accept_matched_only=True))
    assert mixed.ctx.graph == before


def test_new_report_policy_has_its_own_cache_without_rewriting_legacy_reports(mixed, monkeypatch):
    s = mixed
    with monkeypatch.context() as old:
        old.setattr(service, "_build_report_request", service._report_request)
        legacy = report(s)
    _, legacy_value = service._load_report(s.ctx, legacy["report"]["artifact_id"])
    assert "matching_policy" not in legacy_value["request"]
    saved = deepcopy(legacy_value)
    modern = report(s)
    _, modern_value = service._load_report(s.ctx, modern["report"]["artifact_id"])
    assert modern_value["request"]["matching_policy"] == "whole-matrix-v1"
    assert modern["report"]["artifact_id"] != legacy["report"]["artifact_id"]
    assert service._load_report(s.ctx, legacy["report"]["artifact_id"])[1] == saved
    assert report(s)["report"] == modern["report"]


def test_legacy_accepted_tracking_stays_readable_after_new_policy_production(mixed, monkeypatch):
    s = mixed
    with monkeypatch.context() as old:
        old.setattr(service, "_build_report_request", service._report_request)
        legacy = report(s)
    prepared = tools.prepare(s, service.ACCEPT_TOOL, params(s, legacy, accept_matched_only=True), s.ctx)
    accepted = tools.output(s, tools.publish(s, prepared))
    before = service.read_tracking(s.tenant, accepted, {})
    assert "matching_policy" not in before["provenance"]["request"]
    report(s)
    assert service.read_tracking(s.tenant, accepted, {}) == before


@pytest.mark.parametrize("value", [None, False, 1, "unknown", [], {}])
def test_invalid_matching_policy_refuses_before_parsing_or_publication(mixed, monkeypatch, value):
    def unexpected(_):
        raise AssertionError("invalid policy reached PDF parsing")
    monkeypatch.setattr(service.reports, "parse_source", unexpected)
    before = deepcopy(mixed.blobs)
    with pytest.raises(GraphValidationError, match="REPORT_REQUEST_INVALID"):
        service.build_report(mixed.tenant, mixed.project, mixed.drawing, mixed.ctx.parent_version_id,
            {"source_artifact_id": mixed.source_id, "alignment_tolerance": 0.1,
             "selection_order": "unknown", "matching_policy": value})
    assert mixed.blobs == before


def test_old_browser_associations_keep_atomic_candidate_indexes(mixed):
    s = mixed
    result = service.build_report(s.tenant, s.project, s.drawing, s.ctx.parent_version_id,
        {"source_artifact_id": s.source_id, "alignment_tolerance": 0.1, "selection_order": "unknown",
         "associations": {s.ctx.graph["frames"][0]["id"]: 0}})
    _, value = service._load_report(s.ctx, result["report"]["artifact_id"])
    assert value["request"]["matching_policy"] == "atomic-v1"
    assert value["matches"][0]["pdf_grid"] == 0


def test_saved_tracking_cannot_relabel_the_report_matching_policy(mixed):
    s = mixed
    result = report(s)
    graph = service.accept(s.ctx, params(s, result, accept_matched_only=True))
    graph["extra"][service.tracking.STORE_KEY]["request"]["matching_policy"] = "atomic-v1"
    raw = local.canonical_bytes(tools.embedded(graph))
    context = replace(s.ctx, intake_bytes=raw, intake_sha256=sha256(raw).hexdigest())
    with pytest.raises(GraphValidationError, match="SOLAREDGE_IMPORT_RECORD_INVALID"):
        service.read_tracking(s.tenant, context, {})


@pytest.mark.parametrize("mixed", ["whole"], indirect=True)
def test_whole_matrix_publishes_and_reopens_complete_tracking_without_electrical_writes(mixed):
    from test_s02_report_row_angle import whole_matrix_case
    s = mixed
    s.matrices = whole_matrix_case(bridge=True)[0]
    result = report(s)
    assert result["counts"]["matched_frames"] == 1
    assert result["counts"]["assigned_panels"] == 4
    assert result["counts"]["unassigned_panels"] == 0
    assert result["counts"]["bridge_strings"] == 0
    before = deepcopy(s.ctx.graph)
    prepared = tools.prepare(s, service.ACCEPT_TOOL, params(s, result), s.ctx)
    accepted = tools.output(s, tools.publish(s, prepared))
    for key in ("settings", "panels", "frames", "strings", "inverters", "routes", "schedules"):
        assert accepted.graph[key] == before[key]
    readback = service.read_tracking(s.tenant, accepted, {})
    assert readback["accepted"] is True and readback["labels_only"] is True
    assert readback["provenance"]["request"]["matching_policy"] == "whole-matrix-v1"
    assert readback["counts"]["assigned_panels"] == 4
    assert readback["items"][0]["panel_refs"] == [p["id"] for p in before["panels"]]


def test_legacy_builtin_does_not_enable_project_partial_policy(mixed):
    result = report(mixed)
    with pytest.raises(GraphValidationError, match="INVALID_SOLAREDGE_ACCEPT_REQUEST"):
        local._load_builtin(service.ACCEPT_TOOL).run(mixed.ctx.graph,
            params(mixed, result, accept_matched_only=True))


def test_all_matched_groups_with_unassigned_panels_still_have_partial_status(mixed):
    mixed.matrices = [grid(2), grid(3)]
    mixed.matrices[0]["Rows"][0]["Panels"][1]["Seq"] = 0
    mixed.matrices[0]["Sequences"] = [1]
    result = report(mixed)
    assert result["counts"]["matched_frames"] == 2
    graph = service.accept(mixed.ctx, params(mixed, result, accept_matched_only=True))
    record = graph["extra"][service.tracking.STORE_KEY]
    assert record["acceptance"]["status"] == "partial"
    assert record["acceptance"]["unresolved_frame_refs"] == []
    assert len(record["unassigned_panel_refs"]) == 1


@pytest.mark.parametrize("damage", ["status", "unresolved", "mode", "tracking"])
def test_partial_status_and_unresolved_refs_are_verified_on_read(mixed, damage):
    result = report(mixed)
    graph = service.accept(mixed.ctx, params(mixed, result, accept_matched_only=True))
    record = graph["extra"][service.tracking.STORE_KEY]
    if damage == "status":
        record["acceptance"]["status"] = "complete"
    elif damage == "unresolved":
        record["acceptance"]["unresolved_frame_refs"] = []
    elif damage == "mode":
        record["acceptance"]["mode"] = "complete"
    else:
        record["strings"][0]["pdf_inverter_id"] += 1
    intake = tools.embedded(graph)
    raw = local.canonical_bytes(intake)
    context = replace(mixed.ctx, intake_bytes=raw, intake_sha256=sha256(raw).hexdigest())
    with pytest.raises(GraphValidationError, match="SOLAREDGE_IMPORT_RECORD_INVALID"):
        service.read_tracking(mixed.tenant, context, {})
