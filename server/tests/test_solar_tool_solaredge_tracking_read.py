"""Acceptance for sf-w4-solaredge-tracking-read: read the accepted SolarEdge tracking record.

import-solaredge-pdf, the read after the accept (C14 and C15 of the W3 to W5 consult). The read is a
local-graph-read over graph["extra"]["solaredge_import"]: provenance, counts, a reference check
against the graph being read and one bounded page of one section. It never writes, and it never
turns PDF labels into strings, inverters, devices, routes, schedules or Solve state. Freshness is by
reference identity: record.report.graph_sha256 is the PRE-accept graph, so comparing it with the
digest of the graph being read would call every fresh accept stale. Every frozen value below was
measured by running this contract with python -B from server/.
"""
import copy
import json
from pathlib import Path

import pytest
from jsonschema import Draft7Validator

import broker_client
import catalog
import deps
import entitlements
import jobs
import product_capability_availability as availability
import solar_local_graph as local
import solar_local_read as read
import solar_solaredge_report as report
import solar_solaredge_tracking as tracking
import solar_tools
import store
import write_loop
from leaf_cloud_client import canonical_bytes
from solar_design_graph import GraphValidationError
from solar_sizing_client import digest
from test_solar_tool_solaredge_accept import (
    SMALL_ACCEPT_GRAPH, SMALL_RECORD, dispatch, head, manifest, params, seeded)
from test_solar_solaredge_report import (  # noqa: F401
    A, GRIDS, PDF, SRC1, SRC14, C14_ARTIFACT, C14_GRAPH_SHA, SMALL_ARTIFACT, SMALL_GRAPH_SHA,
    app_id, cached_parse, design_graph, intake_graph, small_graph, sources)
from test_solar_solaredge_import import row_panels, two_block_grid
from test_w1_local_graph_adapter import held
from test_w1_local_graph_jobs import isolated_jobs, no_network  # noqa: F401
from test_w1_solve_commit import seed

SERVER = Path(__file__).resolve().parents[1]
TOOL = "solar-solaredge-tracking-read"
TENANT = "fixture-tenant"
FRAME1, FRAME2 = app_id("frame", 1), app_id("frame", 2)
PANEL = [None] + [app_id("panel", n) for n in range(1, 7)]
SMALL_PROVENANCE = {key: SMALL_RECORD[key] for key in ("source", "report", "request")}
SMALL_COUNTS = {"frames": 2, "strings": 2, "group_strings": 2, "bridge_strings": 0,
                "assigned_panels": 6, "unassigned_panels": 0, "partial_strings": 0,
                "distinct_labels": 2}
CURRENT = {"current": True, "missing_frames": 0, "missing_panels": 0, "moved_panels": 0,
           "unrecorded_panels": 0}
ABSENT = {"schema": "leaf.solar-solaredge-tracking-read.v1", "accepted": False, "labels_only": True,
          "provenance": None, "counts": None, "references": None, "section": "strings",
          "page": {"offset": 0, "returned": 0, "total": 0, "next_offset": None}, "items": []}
# (output_bytes, output_sha256) of each measured read.
OUTPUTS = {
    "absent": (229, "1607c42ae8d9ffb360100be001458638773dc8e2a32ec5d3dd2db46af086d2bc"),
    "small": (1665, "758fca33b425c0640a9228f2826e34a05e8d78ec0bbbbcb225ce7e53ecee5bf1"),
    "frames": (1228, "af6f069a6cbc2bd6a162c2361aa11477e0610bc1038d5cd246f5e579dafcb11a"),
    "unassigned": (1013, "d6584c1507f8d2a3717df81b84ff62560abf5f3c2a14fc2a84294521c0c513a3"),
    "offset-1": (1337, "52cb14e401027b61999de45b2699d3fdd6ba71c2d95d0d0c9af306903b698e2f"),
    "offset-2": (1010, "e5414b907a10e3930df542b76e3fdd3d9eb1516925a33afbbfe41bd9924fa87a"),
    "c14": (197091, "4cba71ad2db5d8dfc7d4a2ee2c34f8ed6de0e82ef312ebfb25364e320dda33da"),
    "c14-frames": (3776, "3c882f68269cc4ba0423bd85a99cb34b623131d6f16b6564081d404b17be0c6a"),
    "bridge": (1799, "976f56588814379ecf7f401a675aabb524607ca3b8638c59f1391cf830b97a7e"),
    "partial": (1564, "c39231b6996c23f6fa572e28db7035ad24f3ad3fd2edba71c7c10676b7cd1770"),
    "partial-unassigned": (1112, "1d87aa52e74e775ab0c2beff82cfbe1370e0df2ce60c945b4c4f993384795797"),
    "removed": (1666, "1f8987c53dcc934a907f37a3f7c29c0c7f79e398317bd9cab90bebe4cadfe9da"),
}
# request_sha256 of each read request at its source version.
REQUESTS = {
    ("{}", 1): "91aafc1289cd3cbfce981c973f205faad797d6b15e24bfcec5e49efa821e323a",
    ("{}", 2): "7f90af827ce1065290bde43d4abdee7a62af726821019766db9715be584cfa8d",
    ("frames", 2): "3b39b70b19d196cf7fd0421ddb41c1a6ac092182eb1bd26fa0c3ab3713300ad6",
    ("unassigned", 2): "7d21989e0d7a63f2fdcaaf2670e0d040dfd9a40ea89edd3489dca4b5a68cd40b",
    ("offset-1", 2): "b2a5bad62426d1e452d8916a6c3d6df2f5903db1750d798affcaaab44054a04d",
    ("offset-2", 2): "31bb7df45e2960c0b63f976fed0be62995b61dfcb7b3a5c5dd145dfd8fc57b34",
    ("{}", 3): "02761921aec5f043f607f45fd4192103bcc56c526ebdeed3c10985eff2664ac7",
    ("{}", 4): "021db0ea656af59b7feded3f35dc91a36edfec8bf7a5713028e96fc352efe57a",
}
SETTINGS_GRAPH = "2134a9711d6e9956be3f655e9460a19dd95724c4df2ee7eae82a80382a472534"
REMOVED_GRAPH = "6da1550041cf98c0025127dfd382ba06dc0fe3faaf39637e7c62ca5313571482"
BRIDGE_ACCEPT_GRAPH = "5d59bd5199655b50c93b4e398d224e1cfcc64c0cb75d3b8062b063c70d2548d1"
PARTIAL_ACCEPT_GRAPH = "a195d5cd1025dbd264ee75bf80d55636d31501055c1e17fc24c4ba3e4a3f31ae"
C14_ACCEPT_GRAPH = "379754e72a23004ba3b8213c7447bdbb99ebaf782827b28a9b744d22b0f064e0"
C14_PROVENANCE = {
    "source": {"artifact_id": SRC14, "byte_length": 1019229,
               "content_sha256": "2e8076086b8e494295e5523b3bb94325924b3196d069e62d2f517275d678a1c1"},
    "report": {"artifact_id": C14_ARTIFACT, "byte_length": 227335,
               "content_sha256": "7d76ddbfc97111f159b7f0dca803d9f77a118e96e2a2372d02d1c06be78ba00e",
               "graph_sha256": C14_GRAPH_SHA, "source_version": 1},
    "request": {"alignment_tolerance": 12.0, "selection_order": "unknown"},
}


# ---------------------------------------------------------------- helpers


def builtin():
    return read._load_builtin(TOOL)


def run_read(backend, request, version, job="read-job"):
    return read.run_local_graph_read(backend, TENANT, TOOL, dict(request, drawing_id="solar"),
                                     drawing_id="solar", source_version=version, job_id=job)


def accepted(tmp_path, monkeypatch, graph=None, matrices=None):
    """A seeded drawing whose stored report was accepted: head version 2."""
    backend, _, stored = seeded(tmp_path, monkeypatch, graph, matrices)
    with held(backend) as fence:
        dispatch(backend, fence, params(stored["report"]["artifact_id"]))
    return backend


def pinned(result, name, request, version, graph_sha256):
    assert (result["output_bytes"], result["output_sha256"]) == OUTPUTS[name]
    assert result["request_sha256"] == REQUESTS[(request, version)]
    assert (result["source_version"], result["graph_sha256"]) == (version, graph_sha256)
    assert result["drawing_changed"] is False
    assert len(canonical_bytes(result["output"])) == result["output_bytes"]


def accepted_graph():
    graph = small_graph()
    graph["extra"][tracking.STORE_KEY] = copy.deepcopy(SMALL_RECORD)
    return graph


def synthetic(sizes, unassigned=0):
    """A minimal graph holding one frame and a record of group strings with the given panel counts;
    the builtin reads only extra, frames (id, panel_refs) and panels (id)."""
    frame = app_id("frame", 1)
    number, strings = 0, []
    for index, size in enumerate(sizes):
        refs = [app_id("panel", number + k + 1) for k in range(size)]
        number += size
        strings.append({"index": index, "source": "group", "frame_ref": frame, "pdf_matrix": 0,
                        "pdf_inverter_id": 1, "pdf_string_input": index, "partial": False,
                        "panel_refs": refs})
    loose = [app_id("panel", number + k + 1) for k in range(unassigned)]
    number += unassigned
    record = copy.deepcopy(SMALL_RECORD)
    record.update(frames=[{"frame_ref": frame, "pdf_matrix": 0, "pdf_grid": 0, "pdf_sub_grid": -1}],
                  strings=strings, unassigned_panel_refs=loose)
    panels = [app_id("panel", k + 1) for k in range(number)]
    return {"extra": {tracking.STORE_KEY: record}, "frames": [{"id": frame, "panel_refs": panels}],
            "panels": [{"id": ref} for ref in panels]}


# ---------------------------------------------------------------- contract


def test_solaredge_tracking_read_constants():
    module = builtin()
    assert module.TOOL == TOOL
    assert (module.OUTPUT_SCHEMA, module.INVALID, module.RECORD_INVALID) == (
        "leaf.solar-solaredge-tracking-read.v1", "INVALID_SOLAREDGE_TRACKING_READ_REQUEST",
        "SOLAREDGE_IMPORT_RECORD_INVALID")
    assert module.SECTIONS == ("strings", "frames", "unassigned")
    assert (module.PAGE_ITEMS, module.PAGE_BYTES, module.MAX_REF, module.MAX_VERSION) == (
        1_000, 262_144, 100, 99_999_999)
    assert (tracking.STORE_KEY, tracking.STORE_SCHEMA) == ("solaredge_import", "leaf.solar-solaredge-import.v1")


def test_solaredge_tracking_read_declaration():
    path = SERVER / "solar_tools" / "solar_solaredge_tracking_read.json"
    declared = json.loads(path.read_text(encoding="utf-8"))
    description = declared["record"].pop("description")
    assert description == (
        "Show the accepted SolarEdge tracking: which PDF inverter and string input each panel was "
        "labelled with, the PDF and report it came from, and whether those panels still match the "
        "drawing. Labels only; no electrical design.")
    assert declared == {
        "schema": "leaf.solar-tool.v1", "name": TOOL, "builtin": "builtins/solar_solaredge_tracking_read.py",
        "family": "imports", "adapter": "local-graph-read", "entitlement": "run_read",
        "requires_persisted_graph": True, "seedable": False,
        "invalid_request_code": "INVALID_SOLAREDGE_TRACKING_READ_REQUEST",
        "readiness": {"kind": "facets", "facets": []},
        "engine": "server-builtin", "interaction": {"mode": "form"}, "record_store": "registry",
        "record": {
            "name": TOOL, "version": "1.0.0", "kind": "script", "family_id": "imports",
            "engine_op": "solar_solaredge_tracking_read", "entry": "builtins/solar_solaredge_tracking_read.py",
            "params": {"type": "object", "properties": {
                "drawing_id": {"type": "string", "maxLength": 128},
                "section": {"type": "string", "enum": ["strings", "frames", "unassigned"],
                            "default": "strings"},
                "offset": {"type": "integer", "minimum": 0, "maximum": 200000, "default": 0}},
                "required": [], "additionalProperties": False},
            "returns": {"type": "object"}, "capabilities": ["drawing.read"],
            "allow_local_fallback": False},
        "ledger": ["import-solaredge-pdf"], "trusted_inputs": [],
        "maturity": "preview", "wave": 4, "order": 31, "scenario": "w4-solaredge"}
    assert solar_tools.get(TOOL)["record"] == solar_tools.trusted_record(TOOL)
    assert solar_tools.trusted_record(TOOL)["description"] == description
    legacy = json.loads((SERVER / "write_tools.json").read_text(encoding="utf-8"))["tools"]
    assert TOOL not in {tool["name"] for tool in legacy}


@pytest.mark.parametrize("value,valid", [
    ({}, True),
    ({"drawing_id": "solar"}, True),
    ({"section": "strings"}, True),
    ({"section": "frames", "offset": 0}, True),
    ({"section": "unassigned", "offset": 200000}, True),
    ({"section": "panels"}, False),
    ({"section": "Strings"}, False),
    ({"section": 1}, False),
    ({"offset": -1}, False),
    ({"offset": 200001}, False),
    ({"offset": "0"}, False),
    ({"offset": 1.5}, False),
    ({"expected_rev": 0}, False),
    ({"drawing_id": "d" * 129}, False),
])
def test_solaredge_tracking_read_params_schema(value, valid):
    assert Draft7Validator(solar_tools.trusted_record(TOOL)["params"]).is_valid(value) is valid


def test_solaredge_tracking_read_registry_and_catalog(monkeypatch):
    monkeypatch.setattr(deps, "tenant_repo_dir", lambda tenant: None)
    monkeypatch.setattr(deps, "load_tenant_repo_tools", lambda tenant: [])
    monkeypatch.setattr(deps, "_AUTHORED", [])
    assert TOOL in solar_tools.local_graph_read_tools() and TOOL not in solar_tools.local_graph_tools()
    assert TOOL in read.local_graph_read_tools() and TOOL not in local.local_graph_tools()
    assert TOOL not in availability.W1_CAPABILITIES and len(availability.W1_CAPABILITIES) == 9
    assert availability.capability_adapter(TOOL) == "local-graph-read"
    assert entitlements.tool_required_capability(solar_tools.trusted_record(TOOL)) == "run_read"
    found = [(family, row) for family in catalog.build_catalog(deps.all_tools(TENANT))
             for row in family["capabilities"] if row["name"] == TOOL]
    assert len(found) == 1
    family, row = found[0]
    assert family["family_id"] == "imports"
    assert row["solar"] == {
        "schema": "leaf.solar-tool-view.v1", "name": TOOL, "family": "imports", "wave": 4, "order": 31,
        "maturity": "preview", "engine": "server-builtin", "adapter": "local-graph-read",
        "entitlement": "run_read", "interaction": {"mode": "form"}, "ledger": ["import-solaredge-pdf"]}
    assert row["params_schema"] == solar_tools.trusted_record(TOOL)["params"]


def test_solaredge_tracking_read_readiness():
    # Facets are empty: the only readiness code this tool can carry is the shared unresolved_units.
    ready = {"input_ready": True, "input_reason": None}
    assert availability.w1_local_commit_inputs(small_graph())[TOOL] == ready
    assert availability.w1_local_commit_inputs(accepted_graph())[TOOL] == ready
    empty = small_graph()
    empty["frames"], empty["panels"] = [], []
    assert availability.w1_local_commit_inputs(empty)[TOOL] == ready
    feet = small_graph()
    feet["project"]["units"]["meters_per_unit"] = 0.3048
    assert availability.w1_local_commit_inputs(feet)[TOOL] == {
        "input_ready": False, "input_reason": "unresolved_units"}


# ---------------------------------------------------------------- reads through the adapter


def test_solaredge_tracking_read_absent(tmp_path, monkeypatch):
    backend, _, _ = seeded(tmp_path, monkeypatch)
    result = run_read(backend, {}, 1)
    assert result["output"] == ABSENT
    pinned(result, "absent", "{}", 1, SMALL_GRAPH_SHA)
    for section in ("frames", "unassigned"):
        assert builtin().run(small_graph(), {"section": section}) == dict(ABSENT, section=section)
    with pytest.raises(GraphValidationError) as exc:
        builtin().run(small_graph(), {"offset": 1})
    assert exc.value.code == "INVALID_SOLAREDGE_TRACKING_READ_REQUEST"
    assert manifest(backend) == (1, 1)


def test_solaredge_tracking_read_small(tmp_path, monkeypatch):
    backend = accepted(tmp_path, monkeypatch)
    result = run_read(backend, {}, 2)
    assert result["output"] == {
        "schema": "leaf.solar-solaredge-tracking-read.v1", "accepted": True, "labels_only": True,
        "provenance": SMALL_PROVENANCE, "counts": SMALL_COUNTS, "references": CURRENT,
        "section": "strings", "page": {"offset": 0, "returned": 2, "total": 2, "next_offset": None},
        "items": SMALL_RECORD["strings"]}
    pinned(result, "small", "{}", 2, SMALL_ACCEPT_GRAPH)
    # The trap: the record names the PRE-accept graph, so it never equals the graph that holds it;
    # the read still reports the fresh accept as current.
    assert result["output"]["provenance"]["report"]["graph_sha256"] == SMALL_GRAPH_SHA != result["graph_sha256"]
    assert result["output"]["references"]["current"] is True
    # Labels only: nothing electrical exists or appears.
    graph = head(backend)["graph"]
    assert graph["strings"] == graph["inverters"] == graph["routes"] == graph["schedules"] == []
    assert set(result["output"]) == {"schema", "accepted", "labels_only", "provenance", "counts",
                                     "references", "section", "page", "items"}
    assert manifest(backend) == (2, 2)


@pytest.mark.parametrize("name,request_key,body,section,page,items", [
    ("frames", "frames", {"section": "frames"}, "frames",
     {"offset": 0, "returned": 2, "total": 2, "next_offset": None}, SMALL_RECORD["frames"]),
    ("unassigned", "unassigned", {"section": "unassigned"}, "unassigned",
     {"offset": 0, "returned": 0, "total": 0, "next_offset": None}, []),
    ("offset-1", "offset-1", {"offset": 1}, "strings",
     {"offset": 1, "returned": 1, "total": 2, "next_offset": None}, SMALL_RECORD["strings"][1:]),
    ("offset-2", "offset-2", {"offset": 2}, "strings",
     {"offset": 2, "returned": 0, "total": 2, "next_offset": None}, []),
])
def test_solaredge_tracking_read_sections(tmp_path, monkeypatch, name, request_key, body, section,
                                          page, items):
    backend = accepted(tmp_path, monkeypatch)
    result = run_read(backend, body, 2)
    output = result["output"]
    assert (output["section"], output["page"], output["items"]) == (section, page, items)
    assert (output["provenance"], output["counts"], output["references"]) == (
        SMALL_PROVENANCE, SMALL_COUNTS, CURRENT)
    pinned(result, name, request_key, 2, SMALL_ACCEPT_GRAPH)


def test_solaredge_tracking_read_c14(tmp_path, monkeypatch, cached_parse):
    backend, _ = seed(tmp_path, monkeypatch, intake_graph())
    monkeypatch.setattr(write_loop, "backend_for_tenant", lambda *a, **k: backend)
    sources.import_solaredge_source(backend, TENANT, "solar", PDF)
    report.build_solaredge_report(backend, TENANT, "solar", {
        "source_artifact_id": SRC14, "alignment_tolerance": 12})
    with held(backend) as fence:
        dispatch(backend, fence, params(C14_ARTIFACT))
    record = head(backend)["graph"]["extra"][tracking.STORE_KEY]
    result = run_read(backend, {}, 2)
    output = result["output"]
    assert output["counts"] == {"frames": 25, "strings": 116, "group_strings": 92, "bridge_strings": 24,
                                "assigned_panels": 3526, "unassigned_panels": 0, "partial_strings": 0,
                                "distinct_labels": 116}
    assert len({(row["pdf_inverter_id"], row["pdf_string_input"]) for row in output["items"]}) == 116
    assert output["references"] == CURRENT
    assert output["page"] == {"offset": 0, "returned": 116, "total": 116, "next_offset": None}
    assert output["items"] == record["strings"]
    assert output["provenance"] == C14_PROVENANCE
    assert output["provenance"]["report"]["graph_sha256"] == C14_GRAPH_SHA != C14_ACCEPT_GRAPH
    pinned(result, "c14", "{}", 2, C14_ACCEPT_GRAPH)
    frames = run_read(backend, {"section": "frames"}, 2)
    assert frames["output"]["items"] == record["frames"] and len(record["frames"]) == 25
    pinned(frames, "c14-frames", "frames", 2, C14_ACCEPT_GRAPH)
    assert run_read(backend, {"section": "unassigned"}, 2)["output"]["items"] == []


def test_solaredge_tracking_read_bridge(tmp_path, monkeypatch):
    gb = row_panels("B", 3, x0=1000.0)
    graph = design_graph([{"block": "GA", "panels": [p["handle"] for p in A]},
                          {"block": "GB", "panels": [p["handle"] for p in gb]}], A + gb, 1.0)
    backend = accepted(tmp_path, monkeypatch, graph, [two_block_grid()])
    result = run_read(backend, {}, 2)
    output = result["output"]
    assert output["counts"] == dict(SMALL_COUNTS, strings=3, bridge_strings=1, distinct_labels=3)
    assert output["references"] == CURRENT
    assert [(row["source"], row["frame_ref"]) for row in output["items"]] == [
        ("group", FRAME1), ("group", FRAME2), ("bridge", None)]
    pinned(result, "bridge", "{}", 2, BRIDGE_ACCEPT_GRAPH)


def test_solaredge_tracking_read_partial_and_unassigned(tmp_path, monkeypatch):
    matrices = copy.deepcopy(GRIDS)
    matrices[0]["Rows"][0]["Panels"][0]["Seq"] = 0
    matrices[0]["Rows"][0]["Panels"][2]["Seq"] = 0
    backend = accepted(tmp_path, monkeypatch, matrices=matrices)
    result = run_read(backend, {}, 2)
    assert result["output"]["counts"] == dict(SMALL_COUNTS, assigned_panels=4, unassigned_panels=2,
                                              partial_strings=1)
    assert result["output"]["references"] == CURRENT
    assert result["output"]["items"][0]["partial"] is True
    pinned(result, "partial", "{}", 2, PARTIAL_ACCEPT_GRAPH)
    loose = run_read(backend, {"section": "unassigned"}, 2)
    assert loose["output"]["items"] == [PANEL[1], PANEL[3]]
    assert loose["output"]["page"] == {"offset": 0, "returned": 2, "total": 2, "next_offset": None}
    pinned(loose, "partial-unassigned", "unassigned", 2, PARTIAL_ACCEPT_GRAPH)


def test_solaredge_tracking_read_later_edits(tmp_path, monkeypatch):
    backend = accepted(tmp_path, monkeypatch)
    fresh = run_read(backend, {}, 2)
    with held(backend) as fence:
        local.run_local_graph_commit(
            backend, TENANT, "solar-settings", {"expected_rev": 1, "changes": {"panels_in_sequence": 3},
                                                "drawing_id": "solar"},
            drawing_id="solar", source_version=2, holder="fixture-owner", fence=fence, job_id="settings-job")
    after_settings = run_read(backend, {}, 3)
    # An unrelated edit moves the digest but not one recorded reference: same output, still current.
    assert after_settings["graph_sha256"] == SETTINGS_GRAPH != fresh["graph_sha256"]
    assert after_settings["output"] == fresh["output"]
    pinned(after_settings, "small", "{}", 3, SETTINGS_GRAPH)
    with held(backend) as fence:
        local.run_local_graph_commit(
            backend, TENANT, "solar-panel-remove", {
                "operation": "remove-panels", "expected_rev": 2, "frame_ref": FRAME1,
                "panel_refs": [PANEL[1]], "drawing_id": "solar"},
            drawing_id="solar", source_version=3, holder="fixture-owner", fence=fence, job_id="remove-job")
    removed = run_read(backend, {}, 4)
    assert removed["output"]["references"] == dict(CURRENT, current=False, moved_panels=1)
    assert removed["output"]["items"] == SMALL_RECORD["strings"]
    assert removed["output"]["provenance"] == SMALL_PROVENANCE
    pinned(removed, "removed", "{}", 4, REMOVED_GRAPH)
    assert manifest(backend) == (4, 4)


def _drift(case):
    graph = accepted_graph()
    if case == "panel-deleted":
        graph["panels"] = [p for p in graph["panels"] if p["id"] != PANEL[2]]
        graph["frames"][0]["panel_refs"] = [r for r in graph["frames"][0]["panel_refs"] if r != PANEL[2]]
    elif case == "frame-deleted":
        graph["frames"] = graph["frames"][1:]
    elif case == "panel-moved":
        graph["frames"][0]["panel_refs"].remove(PANEL[3])
        graph["frames"][1]["panel_refs"].append(PANEL[3])
    elif case == "panel-added":
        graph["panels"].append(dict(graph["panels"][0], id=app_id("panel", 7)))
        graph["frames"][1]["panel_refs"].append(app_id("panel", 7))
    return graph


@pytest.mark.parametrize("case,references", [
    ("unchanged", CURRENT),
    ("panel-deleted", dict(CURRENT, current=False, missing_panels=1)),
    ("frame-deleted", dict(CURRENT, current=False, missing_frames=1, moved_panels=3)),
    ("panel-moved", dict(CURRENT, current=False, moved_panels=1)),
    ("panel-added", dict(CURRENT, current=False, unrecorded_panels=1)),
])
def test_solaredge_tracking_read_reference_check(case, references):
    output = builtin().run(_drift(case), {})
    assert output["references"] == references
    assert output["items"] == SMALL_RECORD["strings"] and output["counts"] == SMALL_COUNTS


@pytest.mark.parametrize("sizes,unassigned,body,page", [
    ([1] * 2500, 0, {}, {"offset": 0, "returned": 1000, "total": 2500, "next_offset": 1000}),
    ([1] * 2500, 0, {"offset": 1000}, {"offset": 1000, "returned": 1000, "total": 2500, "next_offset": 2000}),
    ([1] * 2500, 0, {"offset": 2000}, {"offset": 2000, "returned": 500, "total": 2500, "next_offset": None}),
    ([1] * 2500, 0, {"offset": 2500}, {"offset": 2500, "returned": 0, "total": 2500, "next_offset": None}),
    ([100] * 60, 0, {}, {"offset": 0, "returned": 50, "total": 60, "next_offset": 50}),
    ([100] * 60, 0, {"offset": 50}, {"offset": 50, "returned": 10, "total": 60, "next_offset": None}),
    ([6000, 1], 0, {}, {"offset": 0, "returned": 1, "total": 2, "next_offset": 1}),
    ([1], 6000, {"section": "unassigned"},
     {"offset": 0, "returned": 1000, "total": 6000, "next_offset": 1000}),
])
def test_solaredge_tracking_read_paging(sizes, unassigned, body, page):
    graph = synthetic(sizes, unassigned)
    output = builtin().run(graph, body)
    assert output["page"] == page
    record = graph["extra"][tracking.STORE_KEY]
    section = {"strings": "strings", "frames": "frames", "unassigned": "unassigned_panel_refs"}[
        body.get("section", "strings")]
    assert output["items"] == record[section][page["offset"]:page["offset"] + page["returned"]]
    assert output["references"] == CURRENT
    if page["returned"] > 1:
        assert sum(len(canonical_bytes(item)) + 1 for item in output["items"]) <= 262_144
    assert len(canonical_bytes(output)) <= read.MAX_OUTPUT_BYTES


@pytest.mark.parametrize("request_value", [
    [], None, "strings", {"x": 1}, {"expected_rev": 0}, {"section": "panels"}, {"section": 1},
    {"section": None}, {"offset": -1}, {"offset": 200001}, {"offset": True}, {"offset": 1.0},
    {"offset": "0"}, {"offset": 3},
])
def test_solaredge_tracking_read_request_refusals(request_value):
    with pytest.raises(GraphValidationError) as exc:
        builtin().run(accepted_graph(), request_value)
    assert exc.value.code == "INVALID_SOLAREDGE_TRACKING_READ_REQUEST"


def _mutate(record, case):
    strings, frames = record["strings"], record["frames"]
    if case == "record-none":
        return None
    if case == "record-list":
        return [record]
    if case == "schema":
        record["schema"] = "leaf.solar-solaredge-import.v2"
    elif case == "extra-key":
        record["x"] = 1
    elif case == "missing-key":
        del record["request"]
    elif case == "source-upper-hex":
        record["source"]["artifact_id"] = record["source"]["artifact_id"].upper()
    elif case == "source-bytes-zero":
        record["source"]["byte_length"] = 0
    elif case == "report-version-zero":
        record["report"]["source_version"] = 0
    elif case == "report-extra-key":
        record["report"]["x"] = 1
    elif case == "tolerance-int":
        record["request"]["alignment_tolerance"] = 1
    elif case == "tolerance-small":
        record["request"]["alignment_tolerance"] = 1e-7
    elif case == "order-unknown-word":
        record["request"]["selection_order"] = "sorted"
    elif case == "frames-empty":
        record["frames"] = []
    elif case == "frame-duplicate":
        frames[1]["frame_ref"] = frames[0]["frame_ref"]
    elif case == "frame-sub-grid":
        frames[0]["pdf_sub_grid"] = -2
    elif case == "frame-ref-long":
        original_ref = frames[0]["frame_ref"]
        frames[0]["frame_ref"] = "f" * 101
        for row in strings:
            if row["frame_ref"] == original_ref:
                row["frame_ref"] = frames[0]["frame_ref"]
    elif case == "string-index":
        strings[1]["index"] = 0
    elif case == "string-index-bool":
        strings[0]["index"] = False
    elif case == "group-unknown-frame":
        strings[0]["frame_ref"] = app_id("frame", 9)
    elif case == "bridge-with-frame":
        strings[0]["source"] = "bridge"
    elif case == "group-without-frame":
        strings[0]["frame_ref"] = None
    elif case == "inverter-low":
        strings[0]["pdf_inverter_id"] = -2
    elif case == "input-high":
        strings[0]["pdf_string_input"] = 2_147_483_648
    elif case == "partial-int":
        strings[0]["partial"] = 0
    elif case == "refs-empty":
        strings[0]["panel_refs"] = []
    elif case == "ref-empty-text":
        strings[0]["panel_refs"][0] = ""
    elif case == "ref-duplicate":
        strings[1]["panel_refs"][0] = strings[0]["panel_refs"][0]
    elif case == "unassigned-duplicate":
        record["unassigned_panel_refs"] = [strings[0]["panel_refs"][0]]
    elif case == "strings-dict":
        record["strings"] = {}
    elif case == "string-extra-key":
        strings[0]["panel_handles"] = []
    return record


@pytest.mark.parametrize("case", [
    "record-none", "record-list", "schema", "extra-key", "missing-key", "source-upper-hex",
    "source-bytes-zero", "report-version-zero", "report-extra-key", "tolerance-int", "tolerance-small",
    "order-unknown-word", "frames-empty", "frame-duplicate", "frame-sub-grid", "frame-ref-long",
    "string-index", "string-index-bool", "group-unknown-frame", "bridge-with-frame",
    "group-without-frame", "inverter-low", "input-high", "partial-int", "refs-empty", "ref-empty-text",
    "ref-duplicate", "unassigned-duplicate", "strings-dict", "string-extra-key",
])
def test_solaredge_tracking_read_record_refusals(case):
    graph = accepted_graph()
    graph["extra"][tracking.STORE_KEY] = _mutate(graph["extra"][tracking.STORE_KEY], case)
    with pytest.raises(GraphValidationError) as exc:
        builtin().run(graph, {})
    assert exc.value.code == "SOLAREDGE_IMPORT_RECORD_INVALID"


def test_solaredge_tracking_read_is_pure():
    graph = accepted_graph()
    before = copy.deepcopy(graph)
    first = builtin().run(graph, {})
    second = builtin().run(graph, {})
    assert first == second and graph == before
    assert digest(first) == OUTPUTS["small"][1]
    first["items"][0]["panel_refs"].clear()
    first["provenance"]["source"]["byte_length"] = 7
    assert graph == before
    assert builtin().run(graph, {}) == second


def test_solaredge_tracking_read_terminal_proof(tmp_path, monkeypatch):
    backend = accepted(tmp_path, monkeypatch)
    result = run_read(backend, {}, 2)
    proof = read.graph_read_provenance(result, {"drawing_id": "solar"}, TENANT, "read-job", TOOL, 2,
                                       backend=backend)
    assert proof == {"execution_mode": "local_graph_read", "adapter": "local-graph-read",
                     "request_sha256": REQUESTS[("{}", 2)], "graph_sha256": SMALL_ACCEPT_GRAPH,
                     "source_version": 2, "output_sha256": OUTPUTS["small"][1]}
    tampered = copy.deepcopy(result)
    tampered["output"]["references"]["current"] = False
    tampered["output_sha256"] = digest(tampered["output"])
    tampered["output_bytes"] = len(canonical_bytes(tampered["output"]))
    with pytest.raises(ValueError, match="^graph read terminal proof rejected$"):
        read.graph_read_provenance(tampered, {"drawing_id": "solar"}, TENANT, "read-job", TOOL, 2,
                                   backend=backend)


# ---------------------------------------------------------------- binding to this drawing's history


def _store(backend, tmp_path, graph, drawing):
    """Store graph as version 1 of a new drawing, or as the next version of "solar"; the version."""
    path = tmp_path / ("stored-%s.json" % drawing)
    path.write_text(json.dumps({"solar_design_graph": graph, "solar_design_graph_sha256": digest(graph)}))
    if drawing != "solar":
        store.ingest_drawing(backend, TENANT, str(path), drawing_id=drawing)
        return 1
    with held(backend) as fence:
        return store.put_drawing(backend, TENANT, "solar", str(path), manifest(backend)[0],
                                 holder="fixture-owner", fence=fence)


def _read_on(backend, drawing, version):
    return read.run_local_graph_read(backend, TENANT, TOOL, {"drawing_id": drawing},
                                     drawing_id=drawing, source_version=version, job_id="read-job")


@pytest.mark.parametrize("case", ["transplant", "source-version", "graph-sha256"])
def test_solaredge_tracking_read_refuses_a_record_not_from_this_drawing(tmp_path, monkeypatch, case):
    # Astra round one, T1: the record's report must name a version of THIS drawing with that digest.
    backend = accepted(tmp_path, monkeypatch)
    graph = copy.deepcopy(head(backend)["graph"])
    record = graph["extra"][tracking.STORE_KEY]
    if case == "source-version":
        record["report"]["source_version"] = 99_999_999
    elif case == "graph-sha256":
        record["report"]["graph_sha256"] = "0" * 64
    drawing = "other" if case == "transplant" else "solar"
    version = _store(backend, tmp_path, graph, drawing)
    assert version == (1 if case == "transplant" else 3)
    with pytest.raises(GraphValidationError) as exc:
        _read_on(backend, drawing, version)
    assert exc.value.code == "SOLAREDGE_IMPORT_RECORD_INVALID"
    assert "not a version of this drawing" in str(exc.value)
    # The receipt the unbound read would have produced: accepted and current, and the terminal
    # proof, which re-derives through the same lookup, now rejects it.
    with monkeypatch.context() as patch:
        patch.setattr(builtin(), "READS_VERSION_HISTORY", False)
        unbound = _read_on(backend, drawing, version)
    assert unbound["output"]["accepted"] is True and unbound["output"]["references"] == CURRENT
    with pytest.raises(ValueError, match="^graph read terminal proof rejected$"):
        read.graph_read_provenance(unbound, {"drawing_id": drawing}, TENANT, "read-job", TOOL, version,
                                   backend=backend)


def test_solaredge_tracking_read_binding_holds_for_its_own_history(tmp_path, monkeypatch):
    backend = accepted(tmp_path, monkeypatch)
    original = read.resolve_graph_context
    looked_up = []

    def resolve(backend_value, tenant_id, drawing_id, version="head", **kwargs):
        looked_up.append((tenant_id, drawing_id, version))
        return original(backend_value, tenant_id, drawing_id, version, **kwargs)

    monkeypatch.setattr(read, "resolve_graph_context", resolve)
    result = run_read(backend, {}, 2)
    pinned(result, "small", "{}", 2, SMALL_ACCEPT_GRAPH)
    # The read resolves version 2, then its one history lookup resolves version 1 of "solar".
    assert looked_up == [(TENANT, "solar", 2), (TENANT, "solar", 1)]
    assert original(backend, TENANT, "solar", 1)["graph_sha256"] == SMALL_GRAPH_SHA
    assert read.graph_read_provenance(result, {"drawing_id": "solar"}, TENANT, "read-job", TOOL, 2,
                                      backend=backend)["output_sha256"] == OUTPUTS["small"][1]
    assert looked_up[2:] == [(TENANT, "solar", 2), (TENANT, "solar", 1)]


# ---------------------------------------------------------------- the job rail


def _read_api(backend, tmp_path, monkeypatch):
    """The select-by-zone read harness (test_solar_tool_select_by_zone._api) for this tool."""
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from routers import jobs as route

    monkeypatch.setenv("BROKER_LEDGER", str(tmp_path / "ledger.jsonl"))
    monkeypatch.setenv("BROKER_TENANTS", str(tmp_path / "tenants.json"))
    monkeypatch.setenv("LEAF_BROKER_STORE", "legacy")
    monkeypatch.setenv("LEAF_RUNTIME_ENV", "staging")
    import broker

    monkeypatch.setattr(broker, "LEDGER_PATH", tmp_path / "ledger.jsonl")
    monkeypatch.setattr(broker, "_tenants", {TENANT: {"tier": "demo", "disabled": False}})
    monkeypatch.setattr(broker, "_cap_preflight", lambda *a: None)
    monkeypatch.setattr(broker, "_emit_aps_metric", lambda *a: None)
    monkeypatch.setattr(broker, "_get_da", lambda: pytest.fail("APS must not execute"))
    monkeypatch.setattr(broker, "run_tool_dynamic", lambda *a, **k: pytest.fail("dynamic dispatch"))
    monkeypatch.setattr(deps, "tenant_repo_dir", lambda tenant: None)
    monkeypatch.setattr(deps, "load_tenant_repo_tools", lambda tenant: [])
    monkeypatch.setattr(deps, "_AUTHORED", [])
    monkeypatch.setattr(deps, "AUTHORED_STORE", tmp_path / "absent-authored.json")
    record = deps.find_tool(TOOL, TENANT)
    assert record == solar_tools.trusted_record(TOOL)

    class InlineExecutor:
        def submit(self, fn, *args, **kwargs):
            fn(*args, **kwargs)

    monkeypatch.setattr(jobs, "_executors", {jobs.lane_for(record, False): InlineExecutor()})
    monkeypatch.setattr(route.deps, "backedge_run_identity", lambda tenant, *a: tenant)
    monkeypatch.setattr(route.deps, "auth_live", lambda: False)
    monkeypatch.setattr(jobs.platform_link, "resolve_submission_context", lambda *a: None)
    monkeypatch.setattr(write_loop, "backend_for_tenant", lambda *a, **k: backend)
    monkeypatch.setattr(route.catalog, "live_aps_runtime_authorized", lambda *a, **k: True)
    monkeypatch.delenv("LEAF_ENTITLEMENTS_FILE", raising=False)
    monkeypatch.delenv("LEAF_EXACT_WRITE_PINS_REQUIRED", raising=False)
    mode = {"tamper": False, "requests": []}

    def transport(url, *, json, headers, timeout):
        mode["requests"].append(copy.deepcopy(json))
        response = broker._broker_run(broker.BrokerRunRequest(**json))

        class Reply:
            status_code = response.status_code

            def json(self):
                reply = __import__("json").loads(response.body)
                if mode["tamper"] and reply.get("ok") is True:
                    output = dict(reply["result"]["output"], accepted=False)
                    reply["result"].update(output=output, output_sha256=digest(output),
                                           output_bytes=len(canonical_bytes(output)))
                return reply

        return Reply()

    monkeypatch.setattr(broker_client.requests, "post", transport)
    app = FastAPI()
    app.include_router(route.router)
    tenant = route.deps.TenantContext(TENANT, tier="demo", subject="fixture-subject", authority_resolved=True)
    app.dependency_overrides[route.deps.require_tenant] = lambda: tenant
    with TestClient(app) as client:
        yield client, backend, record, tenant, mode


@pytest.fixture
def read_api(isolated_jobs, no_network, tmp_path, monkeypatch):
    backend = accepted(tmp_path, monkeypatch)
    yield from _read_api(backend, tmp_path, monkeypatch)


def post(api, request):
    return api[0].post("/api/run?wait=1", json={
        "tool": TOOL, "dwg": "solar", "params": request, "catalog_digest": deps.catalog_tool_digest(api[2])})


def test_solaredge_tracking_read_api_end_to_end(read_api):
    response = post(read_api, {})
    assert response.status_code == 200, response.text
    env = response.json()
    assert env["ok"] is True
    result = env["result"]
    rec = jobs.get_job(result["job_id"])
    assert rec["status"] == "complete" and rec["dwg_version"] == 2
    assert rec["params"] == {"drawing_id": "solar"}
    assert result["output"]["items"] == SMALL_RECORD["strings"]
    assert result["output"]["references"] == CURRENT
    assert (result["output_bytes"], result["output_sha256"]) == OUTPUTS["small"]
    assert result["request_sha256"] == REQUESTS[("{}", 2)]
    provenance = env["execution_provenance"]
    assert provenance["execution_path"] == "local"
    receipt = read.graph_read_provenance(result, rec["params"], TENANT, rec["job_id"], TOOL, 2,
                                         backend=read_api[1])
    for key, value in receipt.items():
        assert provenance[key] == rec["provenance"][key] == value
    assert len(read_api[4]["requests"]) == 1
    assert manifest(read_api[1]) == (2, 2)


def test_solaredge_tracking_read_api_tampered_receipt(read_api):
    read_api[4]["tamper"] = True
    response = post(read_api, {})
    assert response.status_code == 500, response.text
    assert response.json()["error"]["message"] == "graph read terminal proof rejected"
    rows = jobs._query("SELECT job_id FROM jobs")
    assert len(rows) == 1 and jobs.get_job(rows[0]["job_id"])["status"] == "failed"
    assert manifest(read_api[1]) == (2, 2)


def test_solaredge_tracking_read_api_builtin_refusal(read_api):
    response = post(read_api, {"offset": 3})
    assert response.status_code == 400, response.text
    env = response.json()
    assert env.get("ok") is not True
    assert env["error"]["message"] == env["reason_code"] == "INVALID_SOLAREDGE_TRACKING_READ_REQUEST"
    rows = jobs._query("SELECT job_id FROM jobs")
    assert len(rows) == 1 and jobs.get_job(rows[0]["job_id"])["status"] == "failed"
    assert manifest(read_api[1]) == (2, 2)


def test_solaredge_tracking_read_api_schema_refusal(read_api):
    response = post(read_api, {"section": "panels"})
    env = response.json()
    assert env.get("ok") is not True, response.text
    rows = [jobs.get_job(row["job_id"]) for row in jobs._query("SELECT job_id FROM jobs")]
    reasons = [env.get("reason_code"), (env.get("error") or {}).get("reason_code")]
    reasons += [(row.get("error") or {}).get("reason_code") for row in rows]
    assert "tool_params_invalid" in reasons, response.text
    assert all(row["status"] != "complete" for row in rows)
    assert manifest(read_api[1]) == (2, 2)
