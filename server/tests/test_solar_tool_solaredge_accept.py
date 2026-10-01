"""Acceptance for sf-w4-solaredge-accept: a stored SolarEdge report's tracking committed to the graph.

import-solaredge-pdf piece 3 of 3 (C14 and C15 of the W3 to W5 consult). The accept tool is a
local-graph-commit whose report arrives through the solaredge_report trusted input. It writes only
graph["extra"]["solaredge_import"]: import provenance and tracking associations, never a string,
inverter, device, route, schedule or Solve state. Every frozen value below was measured by running
this contract with python -B from server/.
"""
import copy
import hashlib
import json
from pathlib import Path

import pytest
from jsonschema import Draft7Validator

import write_loop
import store
import catalog
import deps
import entitlements
import jobs
import product_capability_availability as availability
import solar_artifacts
import solar_local_graph as local
import solar_solaredge_report as report
import solar_solaredge_tracking as tracking
import solar_tools
from solar_design_graph import GraphValidationError, entities
from solar_graph_context import resolve_graph_context
from solar_sizing_client import digest, sizing_basis
from solar_solve_results import upstream_basis
from test_solar_registry import package, load_package  # noqa: F401
from test_solar_solaredge_import import MEMBERSHIP_DIGEST, row_panels, two_block_grid
from test_solar_solaredge_report import (
    A, GRIDS, PDF, SRC1, SRC14, SMALL_ARTIFACT, SMALL_CONTENT, SMALL_GRAPH_SHA,
    C14_ARTIFACT, C14_CONTENT, C14_GRAPH_SHA, app_id, design_graph, intake_graph, small_graph,
    cached_parse, csv_source, sources)  # noqa: F401
from test_solar_import_sources import pdf
from test_w1_local_graph_adapter import held
from test_w1_local_graph_jobs import isolated_jobs, no_network  # noqa: F401
from test_w1_local_graph_rail import _api
from test_w1_solve_commit import seed

SERVER = Path(__file__).resolve().parents[1]
TOOL = "solar-solaredge-accept"
TENANT = "fixture-tenant"
PDF1_SHA = "82285a8f6ebe74ec978fd7dacb2bf52d7d5c20a9cc8945766072c12e1656111f"
INTAKE_PDF_SHA = "2e8076086b8e494295e5523b3bb94325924b3196d069e62d2f517275d678a1c1"
SMALL_ACCEPT_GRAPH = "623b624a667c7e48306580678287a182feda198172d45c2105ab4e8d36c7473c"
SMALL_REQUEST = "73fef73544be9ad309fffc84adb1f1be506bcc4edf62f6840fcf333480547324"
SMALL_RECORD_SHA = "5d2fc8a40c1607e9a53f498e0750ef11745fc48ed94b0f178aeebcac23a19f1d"
C14_ACCEPT_GRAPH = "379754e72a23004ba3b8213c7447bdbb99ebaf782827b28a9b744d22b0f064e0"
C14_RECORD_SHA = "92e48fe0688f9fb817deeb73dbde54a1e9bbb25391843148cd29508882640260"
C14_STRINGS_SHA = "27e84b28bbb4f90dace947bbd74f3a4a3ca4a373c83bad69f11311b409dc22ec"
C14_FRAMES_SHA = "67365c62ff7120fe6e8721324c87a86b6dad7cc5d4e460eaa5e923b40f536ac8"
C14_REQUEST = "e979e2bf79cd9a4c1e2f74e1a91c9a07248df482d918a73be538a22ee4433d59"
BRIDGE_GRAPH = "b8257afb0795fa7a2ee6b8d218bcf361244a42a8077617649544358be0ffe1ed"
BRIDGE_RECORD_SHA = "4427fd409360821d3676b791a196c59fdb0e1f51ffc49a53435cfd3ac58cdb54"
UNASSIGNED_RECORD_SHA = "a9c5322a26bb00898f8657de6d3e1c98466481bde94162997b575420a00fa922"
REACCEPT_GRAPH = "91ba2aadaa9ff075082ce64b8241b7df6905bf9054b5c34f8fe0fecbe473112a"
REACCEPT_REPORT = "0507dd1b4a06f6b10ea5f51c1f2db3b2b8d9a45706041f782b266a33872b6db1"
REACCEPT_CONTENT = "35d7e4005d8ac2a08c7fce6c32c971304dea2ceb181db6c56225591e5fe736fc"
FRAME1, FRAME2 = app_id("frame", 1), app_id("frame", 2)
SMALL_RECORD = {
    "schema": "leaf.solar-solaredge-import.v1",
    "source": {"artifact_id": SRC1, "content_sha256": PDF1_SHA, "byte_length": 191},
    "report": {"artifact_id": SMALL_ARTIFACT, "content_sha256": SMALL_CONTENT, "byte_length": 1803,
               "source_version": 1, "graph_sha256": SMALL_GRAPH_SHA},
    "request": {"alignment_tolerance": 1.0, "selection_order": "recorded"},
    "frames": [{"frame_ref": FRAME1, "pdf_matrix": 0, "pdf_grid": 0, "pdf_sub_grid": -1},
               {"frame_ref": FRAME2, "pdf_matrix": 1, "pdf_grid": 1, "pdf_sub_grid": -1}],
    "strings": [
        {"index": 0, "source": "group", "frame_ref": FRAME1, "pdf_matrix": 0, "pdf_inverter_id": 1,
         "pdf_string_input": 1, "partial": False,
         "panel_refs": [app_id("panel", 3), app_id("panel", 2), app_id("panel", 1)]},
        {"index": 1, "source": "group", "frame_ref": FRAME2, "pdf_matrix": 1, "pdf_inverter_id": 2,
         "pdf_string_input": 1, "partial": False,
         "panel_refs": [app_id("panel", 4), app_id("panel", 5), app_id("panel", 6)]}],
    "unassigned_panel_refs": [],
}


# ---------------------------------------------------------------- helpers


def params(report_id=SMALL_ARTIFACT, rev=0):
    return {"expected_rev": rev, "report_artifact_id": report_id}


def builtin():
    return local._load_builtin(TOOL)


def parse_with(monkeypatch, matrices):
    monkeypatch.setattr(report, "parse_source", lambda content: copy.deepcopy(matrices))


def seeded(tmp_path, monkeypatch, graph=None, matrices=None, order="recorded", tolerance=1):
    """A seeded drawing at version 1 with pdf(1) imported and its report stored; returns
    (backend, source reference, report result)."""
    backend, _ = seed(tmp_path, monkeypatch, small_graph() if graph is None else graph)
    monkeypatch.setattr(write_loop, "backend_for_tenant", lambda *a, **k: backend)
    parse_with(monkeypatch, GRIDS if matrices is None else matrices)
    source = sources.import_solaredge_source(backend, TENANT, "solar", pdf(1))["source"]
    stored = report.build_solaredge_report(backend, TENANT, "solar", {
        "source_artifact_id": source["artifact_id"], "alignment_tolerance": tolerance,
        "selection_order": order})
    return backend, source, stored


def dispatch(backend, fence, request, version=1, job="accept-job"):
    return local.run_local_graph_commit(
        backend, TENANT, TOOL, dict(request, drawing_id="solar"), drawing_id="solar",
        source_version=version, holder="fixture-owner", fence=fence, job_id=job)


def head(backend):
    return resolve_graph_context(backend, TENANT, "solar", "head")


def manifest(backend):
    value = store.load_manifest(backend, TENANT, "solar")
    return value["head"], value["latest"]


def resolved_small(tmp_path, monkeypatch):
    backend, _, stored = seeded(tmp_path, monkeypatch)
    meta, content = solar_artifacts.read_artifact(backend, TENANT, "solar", stored["report"]["artifact_id"])
    return {"meta": meta, "report": json.loads(content)}


def membership(record, graph):
    handle = {p["id"]: p["provenance"]["source_handle"].upper() for p in graph["panels"]}
    rows = [[handle[ref] for ref in s["panel_refs"]] for s in record["strings"]]
    return hashlib.sha256(json.dumps(rows, separators=(",", ":")).encode()).hexdigest()


def forged(backend, content, project_id=None):
    """A report-shaped artifact bound to the head, written straight through the artifact store."""
    context = copy.deepcopy(head(backend))
    if project_id is not None:
        context["project_id"] = project_id
    sink = solar_artifacts.ArtifactSink(backend, TENANT, "solar", context, report.REPORT_TOOL,
                                        hashlib.sha256(content).hexdigest(), False)
    return sink.finish(sink.prepare(solar_artifacts.ArtifactOutput(
        {"forged": True}, report.REPORT_MEDIA_TYPE, report.REPORT_FILENAME, content)))["artifact_id"]


class Proxy:
    def __init__(self, inner):
        self.inner = inner

    def __getattr__(self, name):
        return getattr(self.inner, name)

    def get(self, key):
        if "/artifacts/" in key:
            raise OSError("down")
        return self.inner.get(key)


def only_extra_changed(before, after):
    strip = lambda g: {k: v for k, v in g.items() if k not in ("rev", "parent_rev", "extra")}
    extra = {k: v for k, v in after["extra"].items() if k != tracking.STORE_KEY}
    return (strip(before) == strip(after) and extra == before["extra"]
            and after["rev"] == before["rev"] + 1 and after["parent_rev"] == before["rev"])


# ---------------------------------------------------------------- contract


def test_solaredge_accept_constants():
    assert (tracking.STORE_KEY, tracking.STORE_SCHEMA) == ("solaredge_import", "leaf.solar-solaredge-import.v1")
    assert (tracking.MAX_FRAMES, tracking.MAX_PANELS, tracking.MAX_INDEX, tracking.MAX_LABEL,
            tracking.MAX_CANDIDATES) == (10_000, 200_000, 1_000_000, 2_147_483_647, 16)
    assert [tracking.INVALID, tracking.REPORT_REQUIRED, tracking.REPORT_NOT_FOUND,
            tracking.REPORT_UNAVAILABLE, tracking.REPORT_CORRUPT, tracking.REPORT_KIND_MISMATCH,
            tracking.REPORT_STALE, tracking.REPORT_INVALID, tracking.REPORT_AMBIGUOUS] == [
        "INVALID_SOLAREDGE_ACCEPT_REQUEST", "SOLAREDGE_REPORT_REQUIRED", "SOLAREDGE_REPORT_NOT_FOUND",
        "SOLAREDGE_REPORT_UNAVAILABLE", "SOLAREDGE_REPORT_CORRUPT", "SOLAREDGE_REPORT_KIND_MISMATCH",
        "SOLAREDGE_REPORT_STALE", "SOLAREDGE_REPORT_INVALID", "SOLAREDGE_REPORT_AMBIGUOUS"]
    assert builtin().TOOL == TOOL
    assert solar_tools.TRUSTED_INPUTS == ("source_intake", "proposal_candidate", "solaredge_report", "physical_state")
    assert set(local._TRUSTED_RESOLVERS) == set(solar_tools.TRUSTED_INPUTS)
    assert local._TRUSTED_RESOLVERS["solaredge_report"] is local._solaredge_report


def test_solaredge_accept_declaration():
    path = SERVER / "solar_tools" / "solar_solaredge_accept.json"
    declared = json.loads(path.read_text(encoding="utf-8"))
    description = declared["record"].pop("description")
    assert type(description) is str and description.strip() and len(description) <= 400
    assert declared == {
        "schema": "leaf.solar-tool.v1", "name": TOOL, "builtin": "builtins/solar_solaredge_accept.py",
        "family": "imports", "adapter": "local-graph-commit", "entitlement": "run_write",
        "requires_persisted_graph": True, "seedable": False,
        "invalid_request_code": "INVALID_SOLAREDGE_ACCEPT_REQUEST",
        "readiness": {"kind": "facets", "facets": ["frames", "panels"]},
        "engine": "server-builtin", "interaction": {"mode": "form"}, "record_store": "registry",
        "record": {
            "name": TOOL, "version": "1.0.0", "kind": "script", "family_id": "imports",
            "engine_op": "solar_solaredge_accept", "entry": "builtins/solar_solaredge_accept.py",
            "params": {"type": "object", "properties": {
                "drawing_id": {"type": "string", "maxLength": 128},
                "expected_rev": {"type": "integer", "minimum": 0, "maximum": 2147483647},
                "report_artifact_id": {"type": "string", "minLength": 64, "maxLength": 64}},
                "required": ["expected_rev", "report_artifact_id"], "additionalProperties": False},
            "returns": {"type": "object"}, "capabilities": ["drawing.write"],
            "allow_local_fallback": False},
        "ledger": ["import-solaredge-pdf"], "trusted_inputs": ["solaredge_report"],
        "maturity": "preview", "wave": 4, "order": 30, "scenario": "w4-solaredge"}
    assert solar_tools.get(TOOL)["record"] == solar_tools.trusted_record(TOOL)
    assert solar_tools.trusted_record(TOOL)["description"] == description
    legacy = json.loads((SERVER / "write_tools.json").read_text(encoding="utf-8"))["tools"]
    assert TOOL not in {tool["name"] for tool in legacy}


@pytest.mark.parametrize("value,valid", [
    ({"expected_rev": 0, "report_artifact_id": "a" * 64}, True),
    ({"expected_rev": 2147483647, "report_artifact_id": "0" * 64}, True),
    ({"drawing_id": "solar", "expected_rev": 3, "report_artifact_id": "f" * 64}, True),
    ({"report_artifact_id": "a" * 64}, False),
    ({"expected_rev": 0}, False),
    ({"expected_rev": -1, "report_artifact_id": "a" * 64}, False),
    ({"expected_rev": 2147483648, "report_artifact_id": "a" * 64}, False),
    ({"expected_rev": "0", "report_artifact_id": "a" * 64}, False),
    ({"expected_rev": 0, "report_artifact_id": "a" * 63}, False),
    ({"expected_rev": 0, "report_artifact_id": "a" * 65}, False),
    ({"expected_rev": 0, "report_artifact_id": 7}, False),
    ({"expected_rev": 0, "report_artifact_id": "a" * 64, "x": 1}, False),
    ({"expected_rev": 0, "report_artifact_id": "a" * 64, "drawing_id": "d" * 129}, False),
])
def test_solaredge_accept_params_schema(value, valid):
    assert Draft7Validator(solar_tools.trusted_record(TOOL)["params"]).is_valid(value) is valid


@pytest.mark.parametrize("adapter,seedable", [
    ("local-graph-read", False), ("cloud-proposal", False), ("local-graph-commit", True)])
def test_solaredge_accept_registry_rule(package, adapter, seedable):
    declaration = solar_tools.get(TOOL)
    declaration.update(adapter=adapter, seedable=seedable)
    (package[1] / "solar_solaredge_accept.json").write_text(json.dumps(declaration), encoding="utf-8")
    with pytest.raises(solar_tools.SolarRegistryError,
                       match="^solaredge_report requires a non-seed local graph commit$"):
        load_package(package)


def test_solaredge_accept_registry_and_catalog(monkeypatch):
    monkeypatch.setattr(deps, "tenant_repo_dir", lambda tenant: None)
    monkeypatch.setattr(deps, "load_tenant_repo_tools", lambda tenant: [])
    monkeypatch.setattr(deps, "_AUTHORED", [])
    assert TOOL in solar_tools.local_graph_tools() and TOOL not in solar_tools.local_graph_read_tools()
    assert TOOL in local.local_graph_tools()
    assert TOOL not in availability.W1_CAPABILITIES and len(availability.W1_CAPABILITIES) == 9
    assert entitlements.tool_required_capability(solar_tools.trusted_record(TOOL)) == "run_write"
    found = [(family, row) for family in catalog.build_catalog(deps.all_tools(TENANT))
             for row in family["capabilities"] if row["name"] == TOOL]
    assert len(found) == 1
    family, row = found[0]
    assert family["family_id"] == "imports"
    assert row["solar"] == {
        "schema": "leaf.solar-tool-view.v1", "name": TOOL, "family": "imports", "wave": 4, "order": 30,
        "maturity": "preview", "engine": "server-builtin", "adapter": "local-graph-commit",
        "entitlement": "run_write", "interaction": {"mode": "form"}, "ledger": ["import-solaredge-pdf"]}
    assert row["params_schema"] == solar_tools.trusted_record(TOOL)["params"]


def test_solaredge_accept_readiness():
    graph = small_graph()
    assert availability.w1_local_commit_inputs(graph)[TOOL] == {"input_ready": True, "input_reason": None}
    empty = copy.deepcopy(graph)
    empty["frames"], empty["panels"] = [], []
    assert availability.w1_local_commit_inputs(empty)[TOOL] == {
        "input_ready": False, "input_reason": "frames_required"}
    feet = copy.deepcopy(graph)
    feet["project"]["units"]["meters_per_unit"] = 0.3048
    assert availability.w1_local_commit_inputs(feet)[TOOL] == {
        "input_ready": False, "input_reason": "unresolved_units"}


# ---------------------------------------------------------------- C15 and the store


def test_solaredge_accept_c15_pinned(tmp_path, monkeypatch):
    backend, source, stored = seeded(tmp_path, monkeypatch)
    assert source["artifact_id"] == SRC1
    assert stored["report"]["artifact_id"] == SMALL_ARTIFACT
    before = head(backend)
    assert before["graph_sha256"] == SMALL_GRAPH_SHA
    readiness = availability.w1_graph_readiness(before["graph"])
    with held(backend) as fence:
        receipt = dispatch(backend, fence, params())
    assert receipt["request_sha256"] == SMALL_REQUEST
    assert receipt["before_graph_sha256"] == SMALL_GRAPH_SHA
    assert receipt["graph_sha256"] == SMALL_ACCEPT_GRAPH
    assert receipt["new_version"] == {"drawing_id": "solar", "version": 2, "parent": 1}
    after = head(backend)
    assert (after["resolved_version"], after["graph_sha256"]) == (2, SMALL_ACCEPT_GRAPH)
    record = after["graph"]["extra"][tracking.STORE_KEY]
    assert record == SMALL_RECORD
    assert digest(record) == SMALL_RECORD_SHA
    # Explicit recorded order selects PDF grids [0, 1] for the two frames (C15).
    assert [row["pdf_grid"] for row in record["frames"]] == [0, 1]
    # Nothing synthesized: no string, inverter, route or schedule; no entity changed; no output stale.
    graph = after["graph"]
    assert graph["strings"] == graph["inverters"] == graph["routes"] == graph["schedules"] == []
    assert only_extra_changed(before["graph"], graph)
    assert entities(graph) == entities(before["graph"])
    assert upstream_basis(graph) == upstream_basis(before["graph"])
    assert sizing_basis(graph) == sizing_basis(before["graph"])
    assert "solve_coverage" not in graph["extra"]
    assert availability.w1_graph_readiness(graph) == readiness


def test_solaredge_accept_c15_ambiguous(tmp_path, monkeypatch):
    backend, source, _ = seeded(tmp_path, monkeypatch)
    with pytest.raises(report.SolarEdgeReportError) as exc:
        report.build_solaredge_report(backend, TENANT, "solar", {
            "source_artifact_id": source["artifact_id"], "alignment_tolerance": 1, "selection_order": "unknown"})
    assert exc.value.code == "REPORT_AMBIGUOUS_MATCH"
    meta, content = solar_artifacts.read_artifact(backend, TENANT, "solar", SMALL_ARTIFACT)
    value = json.loads(content)
    assert value["matches"][0]["candidates"] == [0, 1] and value["matches"][0]["candidate_count"] == 2
    value["request"]["selection_order"] = "unknown"
    with pytest.raises(GraphValidationError) as exc:
        builtin().run(small_graph(), params(), solaredge_report={"meta": meta, "report": value})
    assert exc.value.code == "SOLAREDGE_REPORT_AMBIGUOUS"
    assert manifest(backend) == (1, 1)


def test_solaredge_accept_c14(tmp_path, monkeypatch, cached_parse):
    backend, _ = seed(tmp_path, monkeypatch, intake_graph())
    monkeypatch.setattr(write_loop, "backend_for_tenant", lambda *a, **k: backend)
    source = sources.import_solaredge_source(backend, TENANT, "solar", PDF)["source"]
    assert source["artifact_id"] == SRC14
    stored = report.build_solaredge_report(backend, TENANT, "solar", {
        "source_artifact_id": SRC14, "alignment_tolerance": 12})
    assert stored["report"]["artifact_id"] == C14_ARTIFACT
    before = head(backend)
    with held(backend) as fence:
        receipt = dispatch(backend, fence, params(C14_ARTIFACT))
    assert receipt["request_sha256"] == C14_REQUEST
    assert receipt["before_graph_sha256"] == C14_GRAPH_SHA
    assert receipt["graph_sha256"] == C14_ACCEPT_GRAPH
    graph = head(backend)["graph"]
    record = graph["extra"][tracking.STORE_KEY]
    assert digest(record) == C14_RECORD_SHA
    assert digest(record["strings"]) == C14_STRINGS_SHA
    assert digest(record["frames"]) == C14_FRAMES_SHA
    assert len(record["frames"]) == 25
    assert len(record["strings"]) == 116
    assert [s["source"] for s in record["strings"]].count("group") == 92
    assert [s["source"] for s in record["strings"]].count("bridge") == 24
    assert sum(len(s["panel_refs"]) for s in record["strings"]) == 3526
    assert record["unassigned_panel_refs"] == []
    assert not any(s["partial"] for s in record["strings"])
    assert len({(s["pdf_inverter_id"], s["pdf_string_input"]) for s in record["strings"]}) == 116
    assert {ref for s in record["strings"] for ref in s["panel_refs"]} == {p["id"] for p in graph["panels"]}
    assert membership(record, graph) == MEMBERSHIP_DIGEST
    assert {key: record["strings"][0][key] for key in record["strings"][0] if key != "panel_refs"} == {
        "index": 0, "source": "group", "frame_ref": FRAME1, "pdf_matrix": 13, "pdf_inverter_id": 2,
        "pdf_string_input": 7, "partial": False}
    assert record["strings"][0]["panel_refs"][:2] == [app_id("panel", 3473), app_id("panel", 3471)]
    assert {key: record["strings"][115][key] for key in record["strings"][115] if key != "panel_refs"} == {
        "index": 115, "source": "bridge", "frame_ref": None, "pdf_matrix": 9, "pdf_inverter_id": 1,
        "pdf_string_input": 5, "partial": False}
    assert {key: record[key] for key in ("source", "report", "request")} == {
        "source": {"artifact_id": SRC14, "content_sha256": INTAKE_PDF_SHA, "byte_length": 1019229},
        "report": {"artifact_id": C14_ARTIFACT, "content_sha256": C14_CONTENT, "byte_length": 227335,
                   "source_version": 1, "graph_sha256": C14_GRAPH_SHA},
        "request": {"alignment_tolerance": 12.0, "selection_order": "unknown"}}
    assert graph["strings"] == graph["inverters"] == graph["routes"] == graph["schedules"] == []
    assert only_extra_changed(before["graph"], graph)



def test_solaredge_accept_bridge(tmp_path, monkeypatch):
    gb = row_panels("B", 3, x0=1000.0)
    graph = design_graph([{"block": "GA", "panels": [p["handle"] for p in A]},
                          {"block": "GB", "panels": [p["handle"] for p in gb]}], A + gb, 1.0)
    assert digest(graph) == BRIDGE_GRAPH
    backend, _, stored = seeded(tmp_path, monkeypatch, graph, [two_block_grid()])
    with held(backend) as fence:
        dispatch(backend, fence, params(stored["report"]["artifact_id"]))
    record = head(backend)["graph"]["extra"][tracking.STORE_KEY]
    assert digest(record) == BRIDGE_RECORD_SHA
    assert record["frames"] == [
        {"frame_ref": FRAME1, "pdf_matrix": 0, "pdf_grid": 0, "pdf_sub_grid": 0},
        {"frame_ref": FRAME2, "pdf_matrix": 0, "pdf_grid": 1, "pdf_sub_grid": 1}]
    assert [(s["source"], s["frame_ref"], s["pdf_inverter_id"], s["pdf_string_input"], s["panel_refs"])
            for s in record["strings"]] == [
        ("group", FRAME1, 1, 1, [app_id("panel", 1), app_id("panel", 2)]),
        ("group", FRAME2, 3, 2, [app_id("panel", 6)]),
        ("bridge", None, 2, 4, [app_id("panel", 3), app_id("panel", 4), app_id("panel", 5)])]


def test_solaredge_accept_unassigned(tmp_path, monkeypatch):
    matrices = copy.deepcopy(GRIDS)
    matrices[0]["Rows"][0]["Panels"][0]["Seq"] = 0
    matrices[0]["Rows"][0]["Panels"][2]["Seq"] = 0
    backend, _, stored = seeded(tmp_path, monkeypatch, matrices=matrices)
    with held(backend) as fence:
        dispatch(backend, fence, params(stored["report"]["artifact_id"]))
    record = head(backend)["graph"]["extra"][tracking.STORE_KEY]
    assert digest(record) == UNASSIGNED_RECORD_SHA
    assert record["unassigned_panel_refs"] == [app_id("panel", 1), app_id("panel", 3)]
    assert record["strings"][0]["panel_refs"] == [app_id("panel", 2)]
    assert record["strings"][0]["partial"] is True
    assert record["strings"][1] == SMALL_RECORD["strings"][1]


def test_solaredge_accept_reaccept_replaces_and_stale_is_refused(tmp_path, monkeypatch):
    backend, source, _ = seeded(tmp_path, monkeypatch)
    with held(backend) as fence:
        dispatch(backend, fence, params())
        again = report.build_solaredge_report(backend, TENANT, "solar", {
            "source_artifact_id": source["artifact_id"], "alignment_tolerance": 1,
            "selection_order": "recorded"})
        assert again["report"]["artifact_id"] == REACCEPT_REPORT
        assert again["report"]["content_sha256"] == REACCEPT_CONTENT
        assert again["source_version"] == 2 and again["graph_sha256"] == SMALL_ACCEPT_GRAPH
        receipt = dispatch(backend, fence, params(REACCEPT_REPORT, rev=1), version=2, job="accept-job-2")
        with pytest.raises(GraphValidationError) as exc:
            dispatch(backend, fence, params(SMALL_ARTIFACT, rev=2), version=3, job="accept-job-3")
        assert exc.value.code == "SOLAREDGE_REPORT_STALE"
    assert receipt["graph_sha256"] == REACCEPT_GRAPH
    record = head(backend)["graph"]["extra"][tracking.STORE_KEY]
    assert record["report"] == {"artifact_id": REACCEPT_REPORT, "content_sha256": REACCEPT_CONTENT,
                                "byte_length": 1803, "source_version": 2, "graph_sha256": SMALL_ACCEPT_GRAPH}
    assert {key: value for key, value in record.items() if key != "report"} == {
        key: value for key, value in SMALL_RECORD.items() if key != "report"}
    assert manifest(backend) == (3, 3)


# ---------------------------------------------------------------- refusals through the adapter


def _store_refusal(case, backend, source):
    if case == "not-found":
        return backend, params("0" * 64)
    if case == "upper-hex":
        return backend, params("A" * 64)
    if case == "missing-id":
        return backend, {"expected_rev": 0}
    if case == "extra-key":
        return backend, dict(params(), x=1)
    if case == "stale-rev":
        return backend, params(rev=1)
    if case == "pdf-source":
        return backend, params(source["artifact_id"])
    if case == "csv":
        return backend, params(csv_source(backend)["artifact_id"])
    if case == "blob-overwritten":
        backend.put(f"tenants/{TENANT}/drawings/solar/artifacts/blobs/{SMALL_CONTENT}.bin", b'{"x":1}')
        return backend, params()
    if case == "store-down":
        return Proxy(backend), params()
    if case in ("frame-ref-list", "panel-ref-dict", "count-bool", "coherent-project-mismatch"):
        _, content = solar_artifacts.read_artifact(backend, TENANT, "solar", SMALL_ARTIFACT)
        rep = json.loads(content)
        project_id = None
        if case == "frame-ref-list":
            rep["strings"][0]["frame_ref"] = []
        elif case == "panel-ref-dict":
            rep["strings"][0]["panel_refs"][0] = {}
        elif case == "count-bool":
            rep["counts"]["strings"] = True
        else:
            project_id = rep["project_id"] = app_id("project", 99)
        content = json.dumps(rep, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
        return backend, params(forged(backend, content, project_id=project_id))
    content = {"not-canonical": b'{"a": 1}', "duplicate-key": b'{"a":1,"a":2}',
               "wrong-shape": b'{"a":1}'}[case]
    return backend, params(forged(backend, content))


@pytest.mark.parametrize("case,code", [
    ("not-found", "SOLAREDGE_REPORT_NOT_FOUND"),
    ("upper-hex", "INVALID_SOLAREDGE_ACCEPT_REQUEST"),
    ("missing-id", "INVALID_SOLAREDGE_ACCEPT_REQUEST"),
    ("extra-key", "INVALID_SOLAREDGE_ACCEPT_REQUEST"),
    ("stale-rev", "STALE_GRAPH_REVISION"),
    ("pdf-source", "SOLAREDGE_REPORT_KIND_MISMATCH"),
    ("csv", "SOLAREDGE_REPORT_KIND_MISMATCH"),
    ("blob-overwritten", "SOLAREDGE_REPORT_CORRUPT"),
    ("store-down", "SOLAREDGE_REPORT_UNAVAILABLE"),
    ("not-canonical", "SOLAREDGE_REPORT_CORRUPT"),
    ("duplicate-key", "SOLAREDGE_REPORT_CORRUPT"),
    ("wrong-shape", "SOLAREDGE_REPORT_INVALID"),
    ("frame-ref-list", "SOLAREDGE_REPORT_INVALID"),
    ("panel-ref-dict", "SOLAREDGE_REPORT_INVALID"),
    ("count-bool", "SOLAREDGE_REPORT_INVALID"),
    ("coherent-project-mismatch", "SOLAREDGE_REPORT_STALE"),
])
def test_solaredge_accept_adapter_refusals(tmp_path, monkeypatch, case, code):
    backend, source, _ = seeded(tmp_path, monkeypatch)
    before = copy.deepcopy(head(backend)["graph"])
    target, request = _store_refusal(case, backend, source)
    with held(backend) as fence:
        with pytest.raises(GraphValidationError) as exc:
            dispatch(target, fence, request)
    assert exc.value.code == code
    assert manifest(backend) == (1, 1)
    assert tracking.STORE_KEY not in head(backend)["graph"]["extra"]
    assert head(backend)["graph"] == before


def test_solaredge_accept_stale_head(tmp_path, monkeypatch):
    backend, _, _ = seeded(tmp_path, monkeypatch)
    graph = head(backend)["graph"]
    with held(backend) as fence:
        local.run_local_graph_commit(
            backend, TENANT, "solar-settings", {"expected_rev": 0, "changes": {"panels_in_sequence": 3},
                                                "drawing_id": "solar"},
            drawing_id="solar", source_version=1, holder="fixture-owner", fence=fence, job_id="settings-job")
        with pytest.raises(GraphValidationError) as exc:
            dispatch(backend, fence, params(rev=1), version=2)
    assert exc.value.code == "SOLAREDGE_REPORT_STALE"
    assert manifest(backend) == (2, 2)
    assert tracking.STORE_KEY not in head(backend)["graph"]["extra"]
    assert graph["rev"] == 0


# ---------------------------------------------------------------- the builtin, pure


def _mutate(case, graph, request, resolved):
    rep, meta = resolved["report"], resolved["meta"]
    s0, s1 = rep["strings"][0], rep["strings"][1]
    m0 = rep["matches"][0]
    actions = {
        "resolved-none": lambda: resolved.clear(),
        "resolved-extra": lambda: resolved.update(x=1),
        "meta-other-artifact": lambda: meta.update(artifact_id="0" * 64),
        "meta-other-tool": lambda: meta.update(tool="solar-select-by-zone"),
        "schema": lambda: rep.update(schema="leaf.other.v1"),
        "report-extra-key": lambda: rep.update(x=1),
        "report-missing-key": lambda: rep.pop("counts"),
        "graph-edited": lambda: graph["settings"].update(string_layer="Other"),
        "report-graph-sha": lambda: rep.update(graph_sha256="0" * 64),
        "meta-graph-sha": lambda: meta.update(graph_sha256="0" * 64),
        "report-version": lambda: rep.update(source_version=2),
        "drawing": lambda: rep.update(drawing_id="other"),
        "project": lambda: rep.update(project_id="leaf:project:other"),
        "source-length-zero": lambda: rep["source"].update(byte_length=0),
        "source-id": lambda: rep["source"].update(artifact_id="x"),
        "source-extra": lambda: rep["source"].update(x=1),
        "tolerance-int": lambda: rep["request"].update(alignment_tolerance=1),
        "tolerance-inf": lambda: rep["request"].update(alignment_tolerance=float("inf")),
        "tolerance-zero": lambda: rep["request"].update(alignment_tolerance=0.0),
        "order": lambda: rep["request"].update(selection_order="first"),
        "matches-empty": lambda: rep.update(matches=[]),
        "match-frame-unknown": lambda: m0.update(frame_ref=app_id("frame", 9)),
        "match-frame-duplicate": lambda: (rep["matches"][1].update(frame_ref=FRAME1),
                                          s1.update(source="bridge", frame_ref=None),
                                          rep["counts"].update(group_strings=1, bridge_strings=1)),
        "match-grid-negative": lambda: m0.update(pdf_grid=-1),
        "match-grid-bool": lambda: rep["matches"][1].update(pdf_grid=True, candidates=[True]),
        "match-sub-grid": lambda: m0.update(pdf_sub_grid=-2),
        "match-matrix-large": lambda: m0.update(pdf_matrix=1_000_001),
        "candidates-empty": lambda: m0.update(candidates=[]),
        "candidates-17": lambda: m0.update(candidates=list(range(17)), candidate_count=17),
        "grid-not-candidate": lambda: m0.update(candidates=[1], candidate_count=1),
        "candidate-count-low": lambda: m0.update(candidate_count=1),
        "ambiguous": lambda: rep["request"].update(selection_order="unknown"),
        "string-index": lambda: s1.update(index=0),
        "string-index-bool": lambda: s1.update(index=True),
        "string-source": lambda: s0.update(source="other"),
        "group-without-frame": lambda: s0.update(frame_ref=None),
        "bridge-with-frame": lambda: s0.update(source="bridge"),
        "group-frame-unmatched": lambda: (rep["matches"].pop(0),
                                          rep["counts"].update(matched_frames=1)),
        "inverter-low": lambda: s0.update(pdf_inverter_id=-2),
        "inverter-high": lambda: s0.update(pdf_inverter_id=2_147_483_648),
        "string-input-low": lambda: s0.update(pdf_string_input=-1),
        "partial-int": lambda: s0.update(partial=0),
        "refs-empty": lambda: s0.update(panel_refs=[], panel_handles=[]),
        "ref-unknown": lambda: s0["panel_refs"].__setitem__(0, app_id("panel", 99)),
        "ref-duplicate": lambda: (s1["panel_refs"].__setitem__(0, s0["panel_refs"][0]),
                                  s1["panel_handles"].__setitem__(0, s0["panel_handles"][0])),
        "ref-duplicate-own-frame": lambda: (s0["panel_refs"].append(s0["panel_refs"][0]),
                                            s0["panel_handles"].append(s0["panel_handles"][0])),
        "handle-mismatch": lambda: s0["panel_handles"].__setitem__(0, "A01"),
        "handles-short": lambda: s0["panel_handles"].pop(),
        "group-other-frame": lambda: (s0["panel_refs"].append(s1["panel_refs"].pop()),
                                      s0["panel_handles"].append(s1["panel_handles"].pop())),
        "unassigned-unknown": lambda: rep["unassigned_panel_refs"].append(app_id("panel", 99)),
        "unassigned-covered": lambda: rep["unassigned_panel_refs"].append(s0["panel_refs"][0]),
        "coverage": lambda: (s0["panel_refs"].pop(), s0["panel_handles"].pop()),
    }
    actions[case]()


BUILTIN_REFUSALS = [
    ("resolved-none", "SOLAREDGE_REPORT_INVALID"),
    ("resolved-extra", "SOLAREDGE_REPORT_INVALID"),
    ("meta-other-artifact", "SOLAREDGE_REPORT_INVALID"),
    ("meta-other-tool", "SOLAREDGE_REPORT_INVALID"),
    ("schema", "SOLAREDGE_REPORT_INVALID"),
    ("report-extra-key", "SOLAREDGE_REPORT_INVALID"),
    ("report-missing-key", "SOLAREDGE_REPORT_INVALID"),
    ("graph-edited", "SOLAREDGE_REPORT_STALE"),
    ("report-graph-sha", "SOLAREDGE_REPORT_STALE"),
    ("meta-graph-sha", "SOLAREDGE_REPORT_STALE"),
    ("report-version", "SOLAREDGE_REPORT_STALE"),
    ("drawing", "SOLAREDGE_REPORT_INVALID"),
    ("project", "SOLAREDGE_REPORT_INVALID"),
    ("source-length-zero", "SOLAREDGE_REPORT_INVALID"),
    ("source-id", "SOLAREDGE_REPORT_INVALID"),
    ("source-extra", "SOLAREDGE_REPORT_INVALID"),
    ("tolerance-int", "SOLAREDGE_REPORT_INVALID"),
    ("tolerance-inf", "SOLAREDGE_REPORT_INVALID"),
    ("tolerance-zero", "SOLAREDGE_REPORT_INVALID"),
    ("order", "SOLAREDGE_REPORT_INVALID"),
    ("matches-empty", "SOLAREDGE_REPORT_INVALID"),
    ("match-frame-unknown", "SOLAREDGE_REPORT_INVALID"),
    ("match-frame-duplicate", "SOLAREDGE_REPORT_INVALID"),
    ("match-grid-negative", "SOLAREDGE_REPORT_INVALID"),
    ("match-grid-bool", "SOLAREDGE_REPORT_INVALID"),
    ("match-sub-grid", "SOLAREDGE_REPORT_INVALID"),
    ("match-matrix-large", "SOLAREDGE_REPORT_INVALID"),
    ("candidates-empty", "SOLAREDGE_REPORT_INVALID"),
    ("candidates-17", "SOLAREDGE_REPORT_INVALID"),
    ("grid-not-candidate", "SOLAREDGE_REPORT_INVALID"),
    ("candidate-count-low", "SOLAREDGE_REPORT_INVALID"),
    ("ambiguous", "SOLAREDGE_REPORT_AMBIGUOUS"),
    ("string-index", "SOLAREDGE_REPORT_INVALID"),
    ("string-index-bool", "SOLAREDGE_REPORT_INVALID"),
    ("string-source", "SOLAREDGE_REPORT_INVALID"),
    ("group-without-frame", "SOLAREDGE_REPORT_INVALID"),
    ("bridge-with-frame", "SOLAREDGE_REPORT_INVALID"),
    ("group-frame-unmatched", "SOLAREDGE_REPORT_INVALID"),
    ("inverter-low", "SOLAREDGE_REPORT_INVALID"),
    ("inverter-high", "SOLAREDGE_REPORT_INVALID"),
    ("string-input-low", "SOLAREDGE_REPORT_INVALID"),
    ("partial-int", "SOLAREDGE_REPORT_INVALID"),
    ("refs-empty", "SOLAREDGE_REPORT_INVALID"),
    ("ref-unknown", "SOLAREDGE_REPORT_INVALID"),
    ("ref-duplicate", "SOLAREDGE_REPORT_INVALID"),
    ("ref-duplicate-own-frame", "SOLAREDGE_REPORT_INVALID"),
    ("handle-mismatch", "SOLAREDGE_REPORT_INVALID"),
    ("handles-short", "SOLAREDGE_REPORT_INVALID"),
    ("group-other-frame", "SOLAREDGE_REPORT_INVALID"),
    ("unassigned-unknown", "SOLAREDGE_REPORT_INVALID"),
    ("unassigned-covered", "SOLAREDGE_REPORT_INVALID"),
    ("coverage", "SOLAREDGE_REPORT_INVALID"),
]


@pytest.fixture
def small_resolved(tmp_path, monkeypatch):
    return resolved_small(tmp_path, monkeypatch)


@pytest.mark.parametrize("case,code", BUILTIN_REFUSALS, ids=[row[0] for row in BUILTIN_REFUSALS])
def test_solaredge_accept_builtin_refusals(small_resolved, case, code):
    graph, request, resolved = small_graph(), params(), copy.deepcopy(small_resolved)
    _mutate(case, graph, request, resolved)
    before = copy.deepcopy(graph)
    with pytest.raises(GraphValidationError) as exc:
        builtin().run(graph, request, solaredge_report=resolved)
    assert exc.value.code == code
    assert graph == before


@pytest.mark.parametrize("request_value,code", [
    ([], "INVALID_SOLAREDGE_ACCEPT_REQUEST"),
    ({}, "INVALID_SOLAREDGE_ACCEPT_REQUEST"),
    ({"expected_rev": 0}, "INVALID_SOLAREDGE_ACCEPT_REQUEST"),
    ({"report_artifact_id": SMALL_ARTIFACT}, "INVALID_SOLAREDGE_ACCEPT_REQUEST"),
    ({"expected_rev": 0, "report_artifact_id": SMALL_ARTIFACT.upper()}, "INVALID_SOLAREDGE_ACCEPT_REQUEST"),
    ({"expected_rev": 0, "report_artifact_id": SMALL_ARTIFACT, "x": 1}, "INVALID_SOLAREDGE_ACCEPT_REQUEST"),
    ({"expected_rev": 0, "report_artifact_id": SMALL_ARTIFACT, "drawing_id": "solar"},
     "INVALID_SOLAREDGE_ACCEPT_REQUEST"),
    ({"expected_rev": 1, "report_artifact_id": SMALL_ARTIFACT}, "STALE_GRAPH_REVISION"),
    ({"expected_rev": "0", "report_artifact_id": SMALL_ARTIFACT}, "STALE_GRAPH_REVISION"),
    ({"expected_rev": False, "report_artifact_id": SMALL_ARTIFACT}, "STALE_GRAPH_REVISION"),
])
def test_solaredge_accept_builtin_request_refusals(small_resolved, request_value, code):
    with pytest.raises(GraphValidationError) as exc:
        builtin().run(small_graph(), request_value, solaredge_report=copy.deepcopy(small_resolved))
    assert exc.value.code == code


def test_solaredge_accept_builtin_requires_the_report():
    with pytest.raises(GraphValidationError) as exc:
        builtin().run(small_graph(), params())
    assert exc.value.code == "SOLAREDGE_REPORT_REQUIRED"


def test_solaredge_accept_builtin_is_pure(small_resolved):
    graph, request, resolved = small_graph(), params(), copy.deepcopy(small_resolved)
    first = builtin().run(copy.deepcopy(graph), copy.deepcopy(request), solaredge_report=resolved)
    assert resolved == small_resolved
    second = builtin().run(copy.deepcopy(graph), copy.deepcopy(request), solaredge_report=resolved)
    assert first == second
    assert digest(first) == SMALL_ACCEPT_GRAPH
    assert first["extra"][tracking.STORE_KEY] == SMALL_RECORD
    assert (first["rev"], first["parent_rev"]) == (1, 0)
    first["extra"][tracking.STORE_KEY]["strings"][0]["panel_refs"].clear()
    assert resolved["report"]["strings"][0]["panel_refs"] == SMALL_RECORD["strings"][0]["panel_refs"]
    assert graph == small_graph()


# ---------------------------------------------------------------- terminal proof and the job rail


def test_solaredge_accept_terminal_proof(tmp_path, monkeypatch):
    backend, _, _ = seeded(tmp_path, monkeypatch)
    request = dict(params(), drawing_id="solar")
    with held(backend) as fence:
        receipt = dispatch(backend, fence, params())
    proof = local.graph_commit_provenance(receipt, request, TENANT, "accept-job", TOOL, 1, backend=backend)
    assert proof == {"execution_mode": "local_graph_commit", "adapter": "local-graph-commit",
                     "request_sha256": SMALL_REQUEST, "graph_sha256": SMALL_ACCEPT_GRAPH,
                     "intake_sha256": receipt["intake_sha256"], "source_version": 1, "new_version": 2}
    backend.put(f"tenants/{TENANT}/drawings/solar/artifacts/blobs/{SMALL_CONTENT}.bin", b'{"x":1}')
    with pytest.raises(ValueError, match="^graph commit terminal proof rejected$"):
        local.graph_commit_provenance(receipt, request, TENANT, "accept-job", TOOL, 1, backend=backend)


@pytest.fixture
def accept_api(isolated_jobs, no_network, tmp_path, monkeypatch):
    backend, _, stored = seeded(tmp_path, monkeypatch)
    for client in _api(backend, tmp_path, monkeypatch):
        client[2][TOOL] = solar_tools.trusted_record(TOOL)
        yield client, stored["report"]["artifact_id"]


def post(api, request):
    client = api[0]
    return client[0].post("/api/run?wait=1", json={
        "tool": TOOL, "dwg": "solar", "params": request,
        "catalog_digest": deps.catalog_tool_digest(client[2][TOOL])})


def jobs_rows():
    return [jobs.get_job(row["job_id"]) for row in jobs._query("SELECT job_id FROM jobs")]


def test_solaredge_accept_api_end_to_end(accept_api):
    assert accept_api[1] == SMALL_ARTIFACT
    response = post(accept_api, params())
    assert response.status_code == 200, response.text
    env = response.json()
    assert env["ok"] is True
    assert env["execution_provenance"]["execution_mode"] == "local_graph_commit"
    assert env["result"]["new_version"] == {"drawing_id": "solar", "version": 2, "parent": 1}
    assert env["result"]["graph_sha256"] == SMALL_ACCEPT_GRAPH
    assert env["result"]["request_sha256"] == SMALL_REQUEST
    backend = accept_api[0][1]
    assert head(backend)["graph"]["extra"][tracking.STORE_KEY] == SMALL_RECORD
    records = jobs_rows()
    assert len(records) == 1 and records[0]["status"] == "complete"


def test_solaredge_accept_api_builtin_refusal(accept_api):
    response = post(accept_api, params("0" * 64))
    assert response.status_code == 400, response.text
    env = response.json()
    assert env.get("ok") is not True
    assert env["error"]["message"] == env["reason_code"] == "SOLAREDGE_REPORT_NOT_FOUND"
    assert manifest(accept_api[0][1]) == (1, 1)
    records = jobs_rows()
    assert len(records) == 1 and records[0]["status"] == "failed"


def test_solaredge_accept_api_schema_refusal(accept_api):
    response = post(accept_api, params("a" * 63))
    env = response.json()
    assert env.get("ok") is not True, response.text
    reasons = [env.get("reason_code"), (env.get("error") or {}).get("reason_code")]
    reasons += [(row.get("error") or {}).get("reason_code") for row in jobs_rows()]
    assert "tool_params_invalid" in reasons, response.text
    assert all(row["status"] != "complete" for row in jobs_rows())
    assert manifest(accept_api[0][1]) == (1, 1)
