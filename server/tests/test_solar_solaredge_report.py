"""Revision-bound SolarEdge report parity, immutable reuse and HTTP refusals."""

import copy
import hashlib
import json
import math
import subprocess
import sys
import threading
from pathlib import Path

import pytest
import write_loop
import store
import solar_artifacts
import solar_import_sources as sources
import solar_solaredge_report as report
import solar_panel_group_kernel as kernel
import solar_solaredge_import as si
from solar_graph_context import resolve_graph_context
from solar_sizing_client import digest
from test_solar_solaredge_import import LENGTHS, MEMBERSHIP_DIGEST, plain_grid, two_block_grid, row_panels
from test_solar_import_sources import pdf, graphless
from test_w1_solve_commit import seed
from test_w1_local_graph_adapter import held, run as commit

TENANT = "fixture-tenant"
PROJECT = "leaf:project:00000000-0000-4000-8000-000000000001"
REPO_ROOT = Path(__file__).resolve().parents[2]
PDF = (REPO_ROOT / "data" / "solaredge_1to1_demo.pdf").read_bytes()
INTAKE = json.loads((REPO_ROOT / "docs/parity/evidence/solaredge/se-pg-intake.json").read_text(encoding="utf-8"))
URL = "/api/drawings/solar/imports/solaredge-pdf/report"
A = row_panels("A", 3)
B = row_panels("B", 3, x0=500.0)
GRIDS = [plain_grid([1, 2, 3]), plain_grid([3, 2, 1], inv=2)]
SRC1 = "949533a2c62f973034fbb40ab738ca4f3267997236b20a09e437dc07665c61fd"
SRC14 = "4887eecc076fd8206933323059e7c05ad01c3045963935ddc504f3a7b8d89a8a"
SMALL_GRAPH_SHA = "72a71ff696ea5695388cf83e66120cb3a7442a655e73c0c15b38473b50e304ad"
C14_GRAPH_SHA = "ed057934bc0b4b1a723cadece08e86e31c3c5e6733e3b4d14c1c2e2a70037342"
SMALL_ARTIFACT = "9b1ac1ff97a87f33eedb38096a1a654ab0fc3ea10ddaefc52920a7b6d0953dd0"
SMALL_CONTENT = "f82e2d8ca1688a3a84f7227ed44a0d168c565fb038ee81882497442545b8d334"
C14_ARTIFACT = "ef9ebf0c14a8a747cc6a0851f3e6e3dd6f86fac2631347983065c3683c549a60"
C14_CONTENT = "7d76ddbfc97111f159b7f0dca803d9f77a118e96e2a2372d02d1c06be78ba00e"
C15_COUNTS = {"pdf_matrices": 2, "pdf_panels": 6, "matchable_grids": 2, "bridge_grids": 0,
              "frames": 2, "matched_frames": 2, "group_strings": 2, "bridge_strings": 0,
              "strings": 2, "assigned_panels": 6, "unassigned_panels": 0, "partial_strings": 0}
C14_COUNTS = {"pdf_matrices": 14, "pdf_panels": 3526, "matchable_grids": 25, "bridge_grids": 8,
              "frames": 25, "matched_frames": 25, "group_strings": 92, "bridge_strings": 24,
              "strings": 116, "assigned_panels": 3526, "unassigned_panels": 0, "partial_strings": 0}


def app_id(kind, number):
    return f"leaf:{kind}:00000000-0000-4000-8000-{number:012d}"


def entity(kind, number, handle=None, **fields):
    provenance = {"created_by": "fixture", "created_at": "2026-09-17T00:00:00Z",
                  "last_writer": "fixture", "source_rev": 0, "source_hash": "a" * 64}
    if handle is not None:
        provenance["source_handle"] = handle
    return {"id": app_id(kind, number), "kind": kind, "rev": 0, "provenance": provenance,
            "extra": {}, "validity": {"state": "valid", "reasons": []}, **fields}


def empty_cell():
    return {"code": "empty", "panel_ref": None, "seq": None, "inverter_id": None,
            "string_input_number": None, "x": 0.0, "y": 0.0, "angle": 0.0}


def design_graph(groups, panels, tolerance):
    """A valid Roof graph: one frame per group (in order), panels in the given order."""
    angle = si.lattice_row_angle([(p["x"], p["y"]) for p in panels])
    degrees = math.degrees(angle)
    number = {p["handle"].upper(): i + 1 for i, p in enumerate(panels)}
    centre = {p["handle"].upper(): [p["x"], p["y"]] for p in panels}
    records, frames = {}, []
    for gi, group in enumerate(groups):
        frame_id = app_id("frame", gi + 1)
        members = [{"handle": h.upper(), "centre": centre[h.upper()]} for h in group["panels"]]
        matrix = []
        for r, row in enumerate(kernel.group_matrix(members, angle, tolerance)):
            cells = []
            for c, handle in enumerate(row):
                if handle is None:
                    cells.append(empty_cell())
                    continue
                records[handle] = entity(
                    "panel", number[handle], handle, frame_ref=frame_id,
                    matrix_cell={"row": r, "col": c}, centre=centre[handle], angle=degrees,
                    assignment={"string_ref": None, "seq": None})
                cells.append({"code": "panel", "panel_ref": app_id("panel", number[handle]),
                              "seq": None, "inverter_id": None, "string_input_number": None,
                              "x": centre[handle][0], "y": centre[handle][1], "angle": degrees})
            matrix.append(cells)
        refs = [app_id("panel", number[h.upper()]) for h in group["panels"]]
        frames.append(entity(
            "frame", gi + 1, group["block"], name=f"Group {gi + 1}", insertion_point=[0, 0, 0],
            installation_design="Roof", panel_refs=refs, module_rows=len(matrix),
            module_columns=len(matrix[0]), module_slots=len(matrix) * len(matrix[0]),
            module_power_watts=400, module_width_along_row=1, module_height_across_row=2,
            electrical_zone_ref=None, matrix=matrix, sequences=[],
            panel_assignments=[{"panel_ref": ref, "string_ref": None, "seq": None,
                                "inverter_id": None, "string_input_number": None} for ref in refs]))
    units = {"drawing_units": "in", "meters_per_unit": 0.0254, "source": "drawing_marker",
             "compute_units": "m", "wcs_to_ucs": [1, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1],
             "elevation_datum": "local roof", "crs": None, "drawing_unit_is_feet": False, "warnings": []}
    cold = {"passes": True, "override_accepted": False, "suggested_string_length": 2,
            "per_module": 50.0, "string_voltage": 100.0, "max_dc_voltage": 600.0}
    return {
        "graph_schema_version": 1, "rev": 0, "parent_rev": None, "source_hash": "a" * 64,
        "catalog_versions": {},
        "project": entity("project", 1, name="SolarEdge fixture", zip_code="00000", latitude=None,
                          longitude=None, installation_design="Roof", units=units,
                          graph_schema_version=1, site_revision="fixture-site-1"),
        "settings": entity("settings", 1, panel_layer_contains="Panels", panel_group_layer="Panel Group",
                           string_layer="String", home_run_layer="Homeruns", panels_in_sequence=28,
                           num_mppt=3, strings_per_mppt=3, optimizer_ratio=1, use_l2_collectors=False,
                           panel_group_number=len(groups) + 1, string_number=1, inverter_number=1,
                           mppt_letter="A", global_string_sizing_confirmed=False, voc_cold=cold),
        "electrical_zones": [], "frames": frames,
        "panels": [records[p["handle"].upper()] for p in panels if p["handle"].upper() in records],
        "strings": [], "inverters": [], "routes": [], "schedules": [],
        "opaque_stores": {}, "orphaned_xdata": [], "extra": {},
    }


def intake_graph():
    return design_graph(INTAKE["groups"], INTAKE["panels"], INTAKE["settings"]["AlignmentTolerance"])


def small_graph():
    return design_graph([{"block": "C1", "panels": [p["handle"] for p in A]},
                         {"block": "C2", "panels": [p["handle"] for p in B]}], A + B, 1.0)


C15_CORE = {
    "row_angle": 0.0, "counts": C15_COUNTS,
    "matches": [{"frame_ref": app_id("frame", 1), "pdf_grid": 0, "pdf_matrix": 0,
                 "pdf_sub_grid": -1, "candidates": [0, 1], "candidate_count": 2},
                {"frame_ref": app_id("frame", 2), "pdf_grid": 1, "pdf_matrix": 1,
                 "pdf_sub_grid": -1, "candidates": [1], "candidate_count": 1}],
    "strings": [{"index": 0, "source": "group", "frame_ref": app_id("frame", 1),
                 "pdf_matrix": 0, "pdf_inverter_id": 1, "pdf_string_input": 1, "partial": False,
                 "panel_handles": ["A02", "A01", "A00"],
                 "panel_refs": [app_id("panel", 3), app_id("panel", 2), app_id("panel", 1)]},
                {"index": 1, "source": "group", "frame_ref": app_id("frame", 2),
                 "pdf_matrix": 1, "pdf_inverter_id": 2, "pdf_string_input": 1, "partial": False,
                 "panel_handles": ["B00", "B01", "B02"],
                 "panel_refs": [app_id("panel", 4), app_id("panel", 5), app_id("panel", 6)]}],
    "unassigned_panel_refs": [],
}
SMALL_RESULT = {
    "schema": "leaf.solar-solaredge-report-result.v1", "kind": "solaredge-report", "drawing_id": "solar",
    "project_id": PROJECT, "source_version": 1, "graph_sha256": SMALL_GRAPH_SHA,
    "source_artifact_id": SRC1, "counts": C15_COUNTS,
    "report": {"schema": "leaf.solar-artifact-ref.v1", "artifact_id": SMALL_ARTIFACT,
               "media_type": "application/json", "filename": "solaredge-report.json", "byte_length": 1803,
               "content_sha256": SMALL_CONTENT, "source_version": 1,
               "download": "/api/drawings/solar/artifacts/" + SMALL_ARTIFACT},
}
C14_RESULT = {
    "schema": "leaf.solar-solaredge-report-result.v1", "kind": "solaredge-report", "drawing_id": "solar",
    "project_id": PROJECT, "source_version": 1, "graph_sha256": C14_GRAPH_SHA,
    "source_artifact_id": SRC14, "counts": C14_COUNTS,
    "report": {"schema": "leaf.solar-artifact-ref.v1", "artifact_id": C14_ARTIFACT,
               "media_type": "application/json", "filename": "solaredge-report.json", "byte_length": 227335,
               "content_sha256": C14_CONTENT, "source_version": 1,
               "download": "/api/drawings/solar/artifacts/" + C14_ARTIFACT},
}
_PARSED = {}
_REAL_PARSE = report.parse_source


@pytest.fixture
def cached_parse(monkeypatch):
    def parse(content):
        key = hashlib.sha256(content).hexdigest()
        if key not in _PARSED:
            _PARSED[key] = _REAL_PARSE(content)
        return copy.deepcopy(_PARSED[key])
    monkeypatch.setattr(report, "parse_source", parse)


@pytest.fixture
def stub(monkeypatch):
    calls = []
    def parse(content):
        calls.append(content)
        return copy.deepcopy(GRIDS)
    monkeypatch.setattr(report, "parse_source", parse)
    return calls


def seed_small(tmp_path, monkeypatch):
    backend, _ = seed(tmp_path, monkeypatch, small_graph())
    monkeypatch.setattr(write_loop, "backend_for_tenant", lambda *a, **k: backend)
    source = sources.import_solaredge_source(backend, TENANT, "solar", pdf(1))["source"]
    return backend, source


@pytest.fixture
def small(tmp_path, monkeypatch, stub):
    return seed_small(tmp_path, monkeypatch)


@pytest.fixture
def client(monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from envelopes import install_error_handlers
    from routers import drawings
    monkeypatch.delenv("LEAF_AUTH_LIVE", raising=False)
    monkeypatch.delenv("APS_LIVE", raising=False)
    app = FastAPI()
    install_error_handlers(app)
    app.include_router(drawings.router)
    return TestClient(app, raise_server_exceptions=False)


def written(backend):
    return {key for key in backend.drawing_object_keys(TENANT, "solar")
            if "/artifacts/" in key or "/imports/" in key}


def body(source, **over):
    return {"source_artifact_id": source["artifact_id"], "alignment_tolerance": 1,
            "selection_order": "recorded", **over}


def post(client, payload, media="application/json", url=URL):
    headers = {"X-Tenant-Id": TENANT}
    if media is not None:
        headers["Content-Type"] = media
    return client.post(url, content=payload if isinstance(payload, bytes) else json.dumps(payload).encode(),
                       headers=headers)


class Proxy:
    def __init__(self, inner, fail):
        self.inner, self.fail = inner, fail

    def __getattr__(self, name):
        return getattr(self.inner, name)

    def put_if_absent_or_verify(self, *args, **kwargs):
        if self.fail == "put":
            raise OSError("down")
        return self.inner.put_if_absent_or_verify(*args, **kwargs)

    def get(self, key):
        if self.fail == "get" and "/artifacts/" in key:
            raise OSError("down")
        return self.inner.get(key)


def save(backend, source, **over):
    return report.build_solaredge_report(backend, TENANT, "solar", body(source, **over))


def core(graph=None, matrices=None, order="recorded"):
    return report.report_from_matrices(small_graph() if graph is None else graph,
                                       GRIDS if matrices is None else matrices,
                                       alignment_tolerance=1.0, selection_order=order)


def refusal(code, func, *args, **kwargs):
    with pytest.raises(report.SolarEdgeReportError) as exc:
        func(*args, **kwargs)
    assert exc.value.code == code


def csv_source(backend):
    sink = solar_artifacts.ArtifactSink(backend, TENANT, "solar",
        resolve_graph_context(backend, TENANT, "solar", "head"), "solar-select-by-zone",
        "df0bc999488ca7eaffc5192d55b11f175876fd4124f0fd64bbfe50ac91402b03", False)
    return sink.finish(sink.prepare(solar_artifacts.ArtifactOutput(
        {"rows": 2}, "text/csv", "strings.csv", b"panel,string\r\nP1,S1\r\nP2,S1\r\n")))


def exhausted(monkeypatch):
    slots = threading.BoundedSemaphore(1)
    slots.acquire()
    monkeypatch.setattr(report, "_PARSE_SLOTS", slots)


def new_keys(artifact, content):
    prefix = "tenants/fixture-tenant/drawings/solar/artifacts/"
    return {prefix + artifact + ".json", prefix + "blobs/" + content + ".bin"}


def assert_head(backend, sha, version=1):
    context = resolve_graph_context(backend, TENANT, "solar", "head")
    assert context["resolved_version"] == version
    assert context["graph_sha256"] == sha
    assert context["graph"]["strings"] == []
    assert context["graph"]["inverters"] == []


def test_solaredge_report_constants():
    values = {"REPORT_TOOL": "solar-solaredge-report", "REPORT_SCHEMA": "leaf.solar-solaredge-report.v1",
              "REQUEST_SCHEMA": "leaf.solar-solaredge-report-request.v1",
              "RESULT_SCHEMA": "leaf.solar-solaredge-report-result.v1", "REPORT_KIND": "solaredge-report",
              "REPORT_MEDIA_TYPE": "application/json", "REPORT_FILENAME": "solaredge-report.json",
              "PARSE_BASE_NAME": "solaredge", "SELECTION_ORDERS": ("unknown", "recorded"),
              "MIN_ALIGNMENT_TOLERANCE": 1e-6, "MAX_ALIGNMENT_TOLERANCE": 1e6,
              "MAX_REPORT_FRAMES": 10000, "MAX_REPORT_PANELS": 200000, "MAX_CONCURRENT_PARSES": 2,
              "MAX_REPORT_CANDIDATES": 16, "MAX_REPORT_REQUEST_BYTES": 4096,
              "_REQUEST_KEYS": frozenset(("source_artifact_id", "alignment_tolerance", "selection_order", "project_id"))}
    for name, value in values.items():
        assert getattr(report, name) == value
    assert isinstance(report._PARSE_SLOTS, threading.BoundedSemaphore)


@pytest.mark.parametrize("over", [{"alignment_tolerance": 1e-6}, {"alignment_tolerance": 12},
    {"alignment_tolerance": 1e6, "selection_order": "recorded", "project_id": "p"}])
def test_solaredge_report_request_accepted(over):
    request = {"source_artifact_id": "a" * 64, **over}
    assert report.validate_request(request) == {
        "source_artifact_id": "a" * 64, "alignment_tolerance": float(over["alignment_tolerance"]),
        "selection_order": over.get("selection_order", "unknown"), "project_id": over.get("project_id")}


@pytest.mark.parametrize("payload", [
    *[{"source_artifact_id": "a" * 64, "alignment_tolerance": t}
      for t in (1e-7, 1000001.0, True, "12", float("nan"), float("inf"))],
    *[{"source_artifact_id": s, "alignment_tolerance": 12} for s in ("A" * 64, "a" * 63, 7)],
    *[{"source_artifact_id": "a" * 64, "alignment_tolerance": 12, "selection_order": o} for o in ("first", None)],
    {"source_artifact_id": "a" * 64, "alignment_tolerance": 12, "x": 1},
    {"source_artifact_id": "a" * 64}, {"alignment_tolerance": 12},
    *[{"source_artifact_id": "a" * 64, "alignment_tolerance": 12, "project_id": p} for p in ("p" * 101, "")],
    ["a"],
])
def test_solaredge_report_request_refused(payload):
    refusal("REPORT_REQUEST_INVALID", report.validate_request, payload)


@pytest.mark.parametrize("content,code", [(pdf(1), None), (b"%PDF-1.4\n\x00\x01garbage", "REPORT_PDF_UNSUPPORTED")])
def test_solaredge_report_parse(content, code):
    if code:
        refusal(code, report.parse_source, content)
    else:
        assert report.parse_source(content) == []


def test_solaredge_report_c14_core(cached_parse):
    graph = intake_graph()
    assert digest(graph) == C14_GRAPH_SHA
    assert sum(len(row) for frame in graph["frames"] for row in frame["matrix"]) == 3967
    matrices = report.parse_source(PDF)
    assert len(matrices) == 14
    value = report.report_from_matrices(graph, matrices, alignment_tolerance=12.0, selection_order="unknown")
    assert value["counts"] == C14_COUNTS
    assert value["row_angle"] == 0.03235485534444149
    assert digest(value) == "27038f6f09d395b16973b673baceb3f0d09854077cd71e0f21507b7bfc9b592e"
    assert digest(value["matches"]) == "04a885dda8b69661c4cbf1c489d6dec97c234b667b7147afad4bbbe0d0e5d272"
    assert digest(value["strings"]) == "ed69e9a6627a2f5a25d140ebe1a0757f920f3c30627aa7e44b2f346f9ca5d36f"
    assert value["matches"][0] == {"frame_ref": app_id("frame", 1), "pdf_grid": 24,
        "pdf_matrix": 13, "pdf_sub_grid": -1, "candidates": [24], "candidate_count": 1}
    strings = value["strings"]
    assert [s["source"] for s in strings] == ["group"] * 92 + ["bridge"] * 24
    assert strings[0] == {"index": 0, "source": "group", "frame_ref": app_id("frame", 1),
        "pdf_matrix": 13, "pdf_inverter_id": 2, "pdf_string_input": 7, "partial": False,
        "panel_handles": strings[0]["panel_handles"], "panel_refs": strings[0]["panel_refs"]}
    assert strings[0]["panel_handles"][:2] == ["959C", "959A"]
    assert strings[0]["panel_refs"][:2] == [app_id("panel", 3473), app_id("panel", 3471)]
    assert {k: strings[115][k] for k in ("frame_ref", "pdf_matrix", "pdf_inverter_id", "pdf_string_input")} == {
        "frame_ref": None, "pdf_matrix": 9, "pdf_inverter_id": 1, "pdf_string_input": 5}
    assert sorted(len(s["panel_refs"]) for s in strings) == LENGTHS
    assert hashlib.sha256(json.dumps([s["panel_handles"] for s in strings], separators=(",", ":")).encode()).hexdigest() == MEMBERSHIP_DIGEST
    assert len({(s["pdf_inverter_id"], s["pdf_string_input"]) for s in strings}) == 116
    assert all(m["candidate_count"] == 1 for m in value["matches"])
    assert {p for s in strings for p in s["panel_refs"]} == {p["id"] for p in graph["panels"]}
    assert value["unassigned_panel_refs"] == []


def test_solaredge_report_c14_recorded_core(cached_parse):
    graph, matrices = intake_graph(), report.parse_source(PDF)
    assert report.report_from_matrices(graph, matrices, alignment_tolerance=12.0, selection_order="recorded") == report.report_from_matrices(
        graph, matrices, alignment_tolerance=12.0, selection_order="unknown")


def test_solaredge_report_c15():
    refusal("REPORT_AMBIGUOUS_MATCH", core, order="unknown")
    assert core() == C15_CORE
    assert digest(core()) == "46e518a72aef6e3b25a29e4fb575d7cda090c8f103bdab367c1bbed5c1e54cdb"


def test_solaredge_report_unassigned_order():
    matrices = copy.deepcopy(GRIDS)
    # Keep the occupied PDF cells so the frame matches, but leave two panels
    # without a string sequence. Their refs must follow numeric handle order,
    # rather than the rightmost-first drawing matrix order.
    matrices[0]["Rows"][0]["Panels"][0]["Seq"] = 0
    matrices[0]["Rows"][0]["Panels"][2]["Seq"] = 0
    value = core(small_graph(), matrices)
    assert value["unassigned_panel_refs"] == [app_id("panel", 1), app_id("panel", 3)]
    assert value["counts"]["unassigned_panels"] == 2
    assert value["counts"]["assigned_panels"] == 4
    assert value["strings"][0]["panel_refs"] == [app_id("panel", 2)]
    assert value["strings"][1] == C15_CORE["strings"][1]


def test_solaredge_report_bridge():
    gb = row_panels("B", 3, x0=1000.0)
    graph = design_graph([{"block": "GA", "panels": [p["handle"] for p in A]},
                          {"block": "GB", "panels": [p["handle"] for p in gb]}], A + gb, 1.0)
    value = core(graph, [two_block_grid()])
    assert digest(value) == "91aa8fb9f5a910ec829414811022327175625a2eaacf06c69cfca08fe6159537"
    assert value["counts"] == {**C15_COUNTS, "pdf_matrices": 1, "bridge_grids": 1,
                               "bridge_strings": 1, "strings": 3}
    assert [m["pdf_sub_grid"] for m in value["matches"]] == [0, 1]
    assert [m["pdf_matrix"] for m in value["matches"]] == [0, 0]
    assert [s["panel_handles"] for s in value["strings"]] == [["A00", "A01"], ["B02"], ["A02", "B00", "B01"]]
    assert [(s["pdf_inverter_id"], s["pdf_string_input"]) for s in value["strings"]] == [(1, 1), (3, 2), (2, 4)]
    assert value["strings"][2] == {"index": 2, "source": "bridge", "frame_ref": None,
        "pdf_matrix": 0, "pdf_inverter_id": 2, "pdf_string_input": 4, "partial": False,
        "panel_handles": ["A02", "B00", "B01"],
        "panel_refs": [app_id("panel", 3), app_id("panel", 4), app_id("panel", 5)]}


@pytest.mark.parametrize("case,code", [
    ("four", "REPORT_NO_MATCH"), ("no-matrices", "REPORT_NO_MATCH"), ("bridge", "REPORT_BRIDGE_UNRESOLVED"),
    ("handle-ZZ", "REPORT_PANEL_HANDLE_INVALID"), ("handle-long", "REPORT_PANEL_HANDLE_INVALID"),
    ("handle-number", "REPORT_PANEL_HANDLE_INVALID"), ("handle-missing", "REPORT_PANEL_HANDLE_INVALID"),
    ("duplicate", "REPORT_PANEL_HANDLE_DUPLICATE"), ("no-frames", "REPORT_FRAMES_REQUIRED"),
    ("empty-frame", "REPORT_FRAME_EMPTY"), ("units", "REPORT_UNITS_UNRESOLVED"),
    ("skew", "REPORT_ROW_ANGLE_UNRESOLVED"), ("coincident", "REPORT_ROW_ANGLE_UNRESOLVED"),
    ("empty-rows", "REPORT_PDF_UNSUPPORTED"), ("bad-matrix", "REPORT_PDF_UNSUPPORTED"),
])
def test_solaredge_report_core_refusals(case, code):
    graph, matrices, order = copy.deepcopy(small_graph()), copy.deepcopy(GRIDS), "recorded"
    if case in ("four", "bridge"):
        panels = row_panels("A", 4) if case == "four" else A
        graph = design_graph([{"block": "C1", "panels": [p["handle"] for p in panels]}], panels, 1.0)
        matrices = [plain_grid([1, 2, 3])] if case == "four" else [two_block_grid()]
        order = "unknown" if case == "four" else "recorded"
    elif case == "no-matrices":
        matrices = []
    elif case.startswith("handle-"):
        provenance = graph["panels"][0]["provenance"]
        if case == "handle-missing":
            del provenance["source_handle"]
        else:
            provenance["source_handle"] = {"handle-ZZ": "ZZ", "handle-long": "1" * 33, "handle-number": 7}[case]
    elif case == "duplicate":
        graph["panels"][3]["provenance"]["source_handle"] = "a00"
    elif case == "no-frames":
        graph["frames"] = []
    elif case == "empty-frame":
        graph["frames"][1]["panel_refs"] = []
    elif case == "units":
        graph["project"]["units"]["meters_per_unit"] = 0.3048
    elif case == "skew":
        graph["panels"][5]["centre"] = [520.0, 7.0]
    elif case == "coincident":
        graph["panels"][4]["centre"] = graph["panels"][3]["centre"][:]
    elif case == "empty-rows":
        matrices = [{"Rows": []}]
    elif case == "bad-matrix":
        matrices = [7]
    refusal(code, core, graph, matrices, order)


@pytest.mark.parametrize("name,value,code", [("MAX_REPORT_FRAMES", 1, "REPORT_LIMIT_EXCEEDED"),
    ("MAX_REPORT_PANELS", 5, "REPORT_LIMIT_EXCEEDED"), ("MAX_REPORT_PANELS", 6, None)])
def test_solaredge_report_limits(monkeypatch, name, value, code):
    monkeypatch.setattr(report, name, value)
    if code:
        refusal(code, core)
    else:
        assert core() == C15_CORE


def test_solaredge_report_pure():
    graph, matrices = small_graph(), copy.deepcopy(GRIDS)
    before = copy.deepcopy((graph, matrices))
    assert core(graph, matrices) == C15_CORE
    assert (graph, matrices) == before
    graph["panels"][0]["provenance"]["source_handle"] = "1" * 32
    assert core(graph)["counts"] == C15_COUNTS


def test_solaredge_report_lazy_kernels():
    result = subprocess.run([sys.executable, "-B", "-c",
        "import write_loop, solar_solaredge_report, sys; print(sorted(m for m in sys.modules if m.split('.')[0] in ('pdfminer', 'solar_solaredge_pdf', 'solar_solaredge_parse')))"],
        cwd=REPO_ROOT / "server", capture_output=True, text=True, timeout=120)
    assert result.returncode == 0, result.stderr
    assert result.stdout.splitlines()[-1] == "[]"


def test_solaredge_report_pinned(small, stub):
    backend, source = small
    before = written(backend)
    value = save(backend, source)
    assert value == SMALL_RESULT
    assert digest(value) == "71afc006edd3f2dcbb8ed46156d3b2a1ad4ab009427844faeb7c06c60092494f"
    assert written(backend) - before == new_keys(SMALL_ARTIFACT, SMALL_CONTENT)
    meta, content = solar_artifacts.read_artifact(backend, TENANT, "solar", SMALL_ARTIFACT)
    assert len(meta) == 13
    assert digest(meta) == "f1fad1faa498e6086fb8ebabaa86c765abcb0c735fe1d5dc681f99fde4f91479"
    assert meta["tool"] == "solar-solaredge-report"
    assert meta["request_sha256"] == "f7954388a17d7c25c363a58ad22b4c698400aa0e9fde0e880ff1c589026de5a8"
    stored = json.loads(content)
    assert set(stored) == {"schema", "drawing_id", "project_id", "source_version", "graph_sha256",
                           "source", "request", *C15_CORE}
    assert stored["schema"] == "leaf.solar-solaredge-report.v1"
    assert stored["source"] == {"artifact_id": SRC1, "byte_length": 191,
        "content_sha256": "82285a8f6ebe74ec978fd7dacb2bf52d7d5c20a9cc8945766072c12e1656111f"}
    assert stored["request"] == {"alignment_tolerance": 1.0, "selection_order": "recorded"}
    assert {k: stored[k] for k in C15_CORE} == C15_CORE
    assert_head(backend, SMALL_GRAPH_SHA)
    assert len(stub) == 1


def test_solaredge_report_duplicate(small, stub, monkeypatch):
    backend, source = small
    first = save(backend, source)
    before = written(backend)
    assert save(backend, source) == first
    exhausted(monkeypatch)
    assert save(backend, source) == first
    assert written(backend) == before
    assert len(stub) == 1


def test_solaredge_report_ambiguous(small):
    backend, source = small
    before = written(backend)
    refusal("REPORT_AMBIGUOUS_MATCH", save, backend, source, selection_order="unknown")
    assert written(backend) == before


@pytest.mark.parametrize("over_http", [False, True])
def test_solaredge_report_handle_alias(tmp_path, monkeypatch, stub, client, over_http):
    graph = small_graph()
    graph["panels"][0]["provenance"]["source_handle"] = "A"
    graph["panels"][1]["provenance"]["source_handle"] = "0A"
    backend, _ = seed(tmp_path, monkeypatch, graph)
    monkeypatch.setattr(write_loop, "backend_for_tenant", lambda *a, **k: backend)
    source = sources.import_solaredge_source(backend, TENANT, "solar", pdf(1))["source"]
    assert resolve_graph_context(backend, TENANT, "solar", "head")["graph"] == graph
    before = set(backend.drawing_object_keys(TENANT, "solar"))
    def no_source_read(*args, **kwargs):
        pytest.fail("aliased handles must refuse before loading the source")
    monkeypatch.setattr(sources, "load_import_source", no_source_read)
    if over_http:
        response = post(client, body(source))
        assert response.status_code == 409
        assert response.json()["error"]["reason_code"] == "REPORT_HANDLE_ALIAS"
        assert response.json()["error"]["retryable"] is False
    else:
        refusal("REPORT_HANDLE_ALIAS", save, backend, source)
    assert set(backend.drawing_object_keys(TENANT, "solar")) == before
    assert stub == []


@pytest.mark.parametrize("handles", [("0A00",), ("D", "0D")])
def test_solaredge_report_unframed_handle_alias(tmp_path, monkeypatch, stub, client, handles):
    graph = small_graph()
    for number, handle in enumerate(handles, start=7):
        graph["panels"].append(entity(
            "panel", number, handle, frame_ref=None, matrix_cell=None,
            centre=[1000.0 + number, 1000.0], angle=0.0,
            assignment={"string_ref": None, "seq": None}))
    backend, _ = seed(tmp_path, monkeypatch, graph)
    monkeypatch.setattr(write_loop, "backend_for_tenant", lambda *a, **k: backend)
    source = sources.import_solaredge_source(backend, TENANT, "solar", pdf(1))["source"]
    assert resolve_graph_context(backend, TENANT, "solar", "head")["graph"] == graph
    before = written(backend)
    value = save(backend, source)
    assert value["counts"] == C15_COUNTS
    assert written(backend) - before == new_keys(
        value["report"]["artifact_id"], value["report"]["content_sha256"])
    _, content = solar_artifacts.read_artifact(
        backend, TENANT, "solar", value["report"]["artifact_id"])
    stored = json.loads(content)
    assert {key: stored[key] for key in C15_CORE} == C15_CORE
    response = post(client, body(source))
    assert response.status_code == 200
    assert response.json() == {**value, "error": None, "degraded_mode": False}
    assert resolve_graph_context(backend, TENANT, "solar", "head")["graph"] == graph
    assert len(stub) == 1


def test_solaredge_report_new_revision(small):
    backend, source = small
    save(backend, source)
    before = written(backend)
    with held(backend) as fence:
        commit(backend, fence=fence)
    value = save(backend, source)
    artifact = "a842dfb7b9b20f69c97af4e50a3644c57525fd65d51d49a6feabb254170fc73c"
    content = "727ca11fe446d5cd790f7c54d30fca986bb9ef44eb44567aa32dc533d9b2eb83"
    assert value["source_version"] == 2
    assert value["report"] == {**SMALL_RESULT["report"], "artifact_id": artifact,
        "content_sha256": content, "source_version": 2,
        "download": "/api/drawings/solar/artifacts/" + artifact}
    assert written(backend) - before == new_keys(artifact, content)
    assert_head(backend, "85adc6b3c33ed7e7bb29e97d804029793949b3a3983eb60a0d32d67135548c17", 2)
    assert solar_artifacts.read_artifact(backend, TENANT, "solar", SMALL_ARTIFACT)[0]["source_version"] == 1


def test_solaredge_report_real_parse(tmp_path, monkeypatch):
    backend, source = seed_small(tmp_path, monkeypatch)
    before = written(backend)
    refusal("REPORT_NO_MATCH", save, backend, source)
    assert written(backend) == before and len(before) == 3


@pytest.mark.parametrize("case,code", [
    ("drained", "REPORT_WRITES_DRAINED"), ("project", "REPORT_PROJECT_MISMATCH"),
    ("drawing", "REPORT_DRAWING_NOT_FOUND"), ("source", "IMPORT_SOURCE_NOT_FOUND"),
    ("csv", "IMPORT_SOURCE_KIND_MISMATCH"), ("corrupt", "IMPORT_SOURCE_CORRUPT"),
    ("busy", "REPORT_BUSY"), ("tolerance", "REPORT_REQUEST_INVALID"),
    ("graphless", "REPORT_GRAPH_REQUIRED"),
])
def test_solaredge_report_store_refusals(small, tmp_path, monkeypatch, case, code):
    backend, source = small
    request, drawing = body(source), "solar"
    if case == "drained":
        monkeypatch.setenv("LEAF_DRAWING_MUTATIONS_ENABLED", "0")
    elif case == "project":
        request["project_id"] = "leaf:project:other"
    elif case == "drawing":
        drawing = "nosuch"
    elif case == "source":
        request["source_artifact_id"] = "0" * 64
    elif case == "csv":
        request["source_artifact_id"] = csv_source(backend)["artifact_id"]
    elif case == "corrupt":
        backend.put(solar_artifacts._key(TENANT, "solar", source["content_sha256"], blob=True), pdf(2))
    elif case == "busy":
        exhausted(monkeypatch)
    elif case == "tolerance":
        request["alignment_tolerance"] = 0
    elif case == "graphless":
        backend = graphless(tmp_path, monkeypatch)
    before = written(backend)
    refusal(code, report.build_solaredge_report, backend, TENANT, drawing, request)
    assert written(backend) == before


def test_solaredge_report_matching_project(small):
    assert save(*small, project_id=PROJECT) == SMALL_RESULT


@pytest.mark.parametrize("operation,code", [("put", "REPORT_STORE_UNAVAILABLE"),
                                           ("get", "IMPORT_STORE_UNAVAILABLE")])
def test_solaredge_report_store_unavailable(small, operation, code):
    backend, source = small
    before = written(backend)
    refusal(code, save, Proxy(backend, operation), source)
    assert written(backend) == before


def test_solaredge_report_corrupt_stored(small):
    backend, source = small
    save(backend, source)
    backend.put(solar_artifacts._key(TENANT, "solar", SMALL_CONTENT, blob=True), b'{"x":1}')
    refusal("REPORT_ARTIFACT_CORRUPT", save, backend, source)


@pytest.mark.parametrize("case", ["noncanonical", "metadata"])
def test_solaredge_report_reuse_comparison(small, stub, case):
    backend, source = small
    save(backend, source)
    meta, content = solar_artifacts.read_artifact(backend, TENANT, "solar", SMALL_ARTIFACT)
    if case == "noncanonical":
        content = content + b"\n"
        meta["content_sha256"] = hashlib.sha256(content).hexdigest()
        meta["byte_length"] = len(content)
        backend.put(solar_artifacts._key(TENANT, "solar", meta["content_sha256"], blob=True), content)
    else:
        meta["filename"] = "other-report.json"
    backend.put(solar_artifacts._key(TENANT, "solar", SMALL_ARTIFACT),
                report.canonical_bytes(meta))
    # The artifact reader accepts the self-consistent bytes and metadata.
    # Only the report reuse comparison can reject this stored report.
    assert solar_artifacts.read_artifact(backend, TENANT, "solar", SMALL_ARTIFACT) == (meta, content)
    before = set(backend.drawing_object_keys(TENANT, "solar"))
    refusal("REPORT_ARTIFACT_CORRUPT", save, backend, source)
    assert set(backend.drawing_object_keys(TENANT, "solar")) == before
    assert len(stub) == 1


def test_solaredge_report_other_tenant(small):
    from solar_design_graph import GraphValidationError
    backend, source = small
    save(backend, source)
    with pytest.raises(GraphValidationError) as exc:
        solar_artifacts.read_artifact(backend, "other-tenant", "solar", SMALL_ARTIFACT)
    assert exc.value.code == "ARTIFACT_NOT_FOUND"
    refusal("REPORT_DRAWING_NOT_FOUND", report.build_solaredge_report,
            backend, "other-tenant", "solar", body(source))


def test_solaredge_report_foreign_source_after_graph(small, tmp_path, stub):
    backend, source = small
    other = "other-tenant"
    store.ingest_drawing(backend, other, str(tmp_path / "seed.json"), drawing_id="solar")
    context = resolve_graph_context(backend, other, "solar", "head")
    assert context["graph_sha256"] == SMALL_GRAPH_SHA
    assert context["resolved_version"] == 1
    before = {tenant: set(backend.drawing_object_keys(tenant, "solar"))
              for tenant in (TENANT, other)}
    with pytest.raises(sources.ImportSourceError) as exc:
        sources.load_import_source(backend, other, "solar", source["artifact_id"],
                                   project_id=context["project_id"])
    assert exc.value.code == "IMPORT_SOURCE_NOT_FOUND"
    refusal("IMPORT_SOURCE_NOT_FOUND", report.build_solaredge_report,
            backend, other, "solar", body(source))
    assert {tenant: set(backend.drawing_object_keys(tenant, "solar"))
            for tenant in (TENANT, other)} == before
    assert stub == []


def seed_c14(tmp_path, monkeypatch):
    backend, _ = seed(tmp_path, monkeypatch, intake_graph())
    monkeypatch.setattr(write_loop, "backend_for_tenant", lambda *a, **k: backend)
    source = sources.import_solaredge_source(backend, TENANT, "solar", PDF)["source"]
    return backend, source


def test_solaredge_report_c14_pinned(tmp_path, monkeypatch, cached_parse):
    backend, source = seed_c14(tmp_path, monkeypatch)
    before = written(backend)
    value = report.build_solaredge_report(backend, TENANT, "solar",
        {"source_artifact_id": source["artifact_id"], "alignment_tolerance": 12})
    assert value == C14_RESULT
    assert digest(value) == "459ec2ed0e7df64557fa4a4159f33054d76a8354721e79df25c0db15fc0232db"
    assert written(backend) - before == new_keys(C14_ARTIFACT, C14_CONTENT)
    meta, content = solar_artifacts.read_artifact(backend, TENANT, "solar", C14_ARTIFACT)
    assert meta["request_sha256"] == "34907c6555c364aba67b3e0c6f476d37e58ec8559e53a1f2976766adce539051"
    assert digest(meta) == "e6c2922b3bb4bc2ed9747f8a308b9ef4b1d1529569bccc1340c97b2609e2f3b1"
    assert hashlib.sha256(content).hexdigest() == C14_CONTENT and len(content) == 227335
    stored = json.loads(content)
    assert stored["counts"] == C14_COUNTS
    assert stored["row_angle"] == 0.03235485534444149
    assert stored["unassigned_panel_refs"] == []
    assert_head(backend, C14_GRAPH_SHA)
    assert {k for k in written(backend) if "/imports/" in k} == {k for k in before if "/imports/" in k}


def test_solaredge_report_c14_recorded_store(tmp_path, monkeypatch, cached_parse):
    backend, source = seed_c14(tmp_path, monkeypatch)
    unknown = save(backend, source, alignment_tolerance=12, selection_order="unknown")
    recorded = save(backend, source, alignment_tolerance=12)
    assert recorded["report"]["artifact_id"] == "3c6411af035e5d0513bf62921b8e567544ac7a7aac5acaa377533c3a63884c92"
    assert recorded["report"]["content_sha256"] == "54f811d189e601f67fd69e47f2c425afe4e65fa7a52dd8c2d60f277fb6bd4f98"
    assert recorded["counts"] == unknown["counts"] == C14_COUNTS
    values = [json.loads(solar_artifacts.read_artifact(backend, TENANT, "solar", v["report"]["artifact_id"])[1])
              for v in (unknown, recorded)]
    assert values[0]["strings"] == values[1]["strings"]


def test_solaredge_report_route_success_download(small, client):
    backend, source = small
    value = post(client, body(source))
    assert value.status_code == 200
    assert value.json() == {**SMALL_RESULT, "error": None, "degraded_mode": False}
    before = written(backend)
    assert post(client, body(source)).json() == value.json()
    assert written(backend) == before
    downloaded = client.get(value.json()["report"]["download"], headers={"X-Tenant-Id": TENANT})
    assert downloaded.status_code == 200
    assert hashlib.sha256(downloaded.content).hexdigest() == SMALL_CONTENT
    assert downloaded.headers["content-type"] == "application/json"
    assert downloaded.headers["content-disposition"] == 'attachment; filename="solaredge-report.json"'
    assert downloaded.headers["x-content-type-options"] == "nosniff"


def test_solaredge_report_route_c14(tmp_path, monkeypatch, cached_parse, client):
    backend, source = seed_c14(tmp_path, monkeypatch)
    response = post(client, {"source_artifact_id": source["artifact_id"], "alignment_tolerance": 12})
    assert response.status_code == 200
    assert response.json() == {**C14_RESULT, "error": None, "degraded_mode": False}
    downloaded = client.get(response.json()["report"]["download"], headers={"X-Tenant-Id": TENANT})
    assert downloaded.status_code == 200
    assert hashlib.sha256(downloaded.content).hexdigest() == C14_CONTENT
    assert downloaded.headers["etag"] == '"' + C14_CONTENT + '"'


@pytest.mark.parametrize("case,status,code", [
    ("media", 415, "REPORT_MEDIA_TYPE_REFUSED"), ("no-media", 415, "REPORT_MEDIA_TYPE_REFUSED"),
    ("length", 413, "REPORT_REQUEST_TOO_LARGE"), ("chunks", 413, "REPORT_REQUEST_TOO_LARGE"),
    ("json", 400, "REPORT_REQUEST_INVALID"), ("utf8", 400, "REPORT_REQUEST_INVALID"),
    ("shape", 400, "REPORT_REQUEST_INVALID"), ("source-id", 400, "REPORT_REQUEST_INVALID"),
    ("source", 404, "IMPORT_SOURCE_NOT_FOUND"), ("project", 409, "REPORT_PROJECT_MISMATCH"),
    ("ambiguous", 409, "REPORT_AMBIGUOUS_MATCH"), ("drawing-id", 400, "REPORT_DRAWING_ID_INVALID"),
    ("drained", 503, "REPORT_WRITES_DRAINED"), ("busy", 503, "REPORT_BUSY"),
    ("backend", 503, "REPORT_STORE_UNAVAILABLE"), ("drawing", 404, "REPORT_DRAWING_NOT_FOUND"),
    ("graphless", 409, "REPORT_GRAPH_REQUIRED"), ("csv", 409, "IMPORT_SOURCE_KIND_MISMATCH"),
    ("real", 422, "REPORT_NO_MATCH"),
])
def test_solaredge_report_route_refusals(tmp_path, monkeypatch, client, case, status, code):
    from routers import drawings
    backend, source = seed_small(tmp_path, monkeypatch)
    if case != "real":
        monkeypatch.setattr(report, "parse_source", lambda content: copy.deepcopy(GRIDS))
    payload, media, url = body(source), "application/json", URL
    if case == "media":
        media = "text/plain"
    elif case == "no-media":
        media = None
    elif case in ("json", "utf8", "shape"):
        payload = {"json": b"{", "utf8": b"\xff", "shape": [1]}[case]
    elif case in ("source-id", "source", "csv"):
        payload["source_artifact_id"] = (csv_source(backend)["artifact_id"] if case == "csv"
                                          else ("E" if case == "source-id" else "0") * 64)
    elif case == "project":
        payload["project_id"] = "leaf:project:other"
    elif case == "ambiguous":
        payload["selection_order"] = "unknown"
    elif case in ("drawing-id", "drawing"):
        url = URL.replace("/solar/", "/Bad!/" if case == "drawing-id" else "/nosuch/")
    elif case == "drained":
        monkeypatch.setenv("LEAF_DRAWING_MUTATIONS_ENABLED", "0")
    elif case == "busy":
        exhausted(monkeypatch)
    elif case == "backend":
        def unavailable(*args):
            raise RuntimeError("down")
        monkeypatch.setattr(drawings, "_backend", unavailable)
    elif case == "graphless":
        backend = graphless(tmp_path, monkeypatch)
    before = written(backend)
    if case in ("length", "chunks"):
        headers = {"X-Tenant-Id": TENANT, "Content-Type": "application/json"}
        if case == "length":
            headers["Content-Length"] = "5000"
        response = client.post(url, content=(b"{}" if case == "length" else
            iter([b"{" + b" " * 3000, b" " * 2000 + b"}"])), headers=headers)
    else:
        response = post(client, payload, media, url)
    assert response.status_code == status, response.text
    assert response.json()["error"]["reason_code"] == code
    assert response.json()["error"]["retryable"] is (status == 503)
    assert written(backend) == before


@pytest.mark.parametrize("unavailable", [False, True])
def test_solaredge_report_route_entitlement(small, client, monkeypatch, unavailable):
    import entitlements
    backend, source = small
    def policy(*args):
        if unavailable:
            raise entitlements.EntitlementsError("x")
        return {"run_read": False}
    monkeypatch.setattr(entitlements, "entitlements_for", policy)
    before = written(backend)
    response = post(client, body(source))
    assert response.status_code == (503 if unavailable else 403)
    if not unavailable:
        assert response.json()["entitlement_required"] is True
        assert response.json()["required"] == "run_read"
    assert written(backend) == before


def test_solaredge_report_route_unknown_reason(small, client, monkeypatch):
    backend, source = small
    def unknown(*args):
        raise report.SolarEdgeReportError("REPORT_SOMETHING_NEW")
    monkeypatch.setattr(report, "build_solaredge_report", unknown)
    before = written(backend)
    response = post(client, body(source))
    assert response.status_code == 500
    assert response.json()["error"]["reason_code"] == "REPORT_ARTIFACT_INVALID"
    assert response.json()["error"]["retryable"] is False
    assert written(backend) == before
