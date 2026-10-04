"""The 24 Ground Physical Record 3 export contracts."""
import copy
import hashlib
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
from jsonschema import Draft7Validator

SERVER = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SERVER))
sys.path.insert(0, str(SERVER / "tests"))

import catalog
import deps
import product_capability_availability as availability
import solar_artifacts as artifacts
import solar_ground_analysis as analysis
import solar_ground_shade as shade
import solar_local_read as read
import solar_physical_analysis as hydration
import solar_physical_head as ph
import solar_physical_state as ps
import solar_tools
import store
from leaf_cloud_client import canonical_bytes
from solar_design_graph import GraphValidationError
from solar_sizing_client import digest
import test_solar_artifacts as ordinary
from test_solar_artifacts import client  # noqa: F401
from test_solar_local_read import TENANT, JOB, backend, _publish_head, forbidden  # noqa: F401
from test_solar_tool_physical_shade import field, document, controlled
from test_w1_design_graph import graph  # noqa: F401
from test_w1_local_graph_jobs import isolated_jobs, no_network  # noqa: F401

TOOL = "solar-physical-export"
PINS = {
    "terrain-csv": (138, "8a9192dbca9a223850a641c1de881ed9468d28eb8dbba457fa0dea60305331a2"),
    "shade-azal-matrix": (3474, "628c5a6ceb401df2eb6a65477184a634ae8e091777d33380fbd2a5106371aa09"),
    "shade-sam": (6764, "74d7f6d1730b90ece32851c5a5737beda3a56f227d46a60395256f47529d924f"),
    "shade-per-panel": (116884, "c2fd555e0a998bdeafba1c11551b43f2325dc9992376f002faff8dc139758f34"),
}
SUMMARY_KEYS = {"schema", "maturity", "scope", "head", "format", "units", "grid", "settings",
                "sample_count", "profile", "mean_shade", "datum_shift_m"}


def builtin():
    return read._load_builtin(TOOL)


def terrain_state():
    return {"grid": {"rows": 2, "cols": 3, "x_min": 0.0, "x_max": 200.0,
                     "y_min": 0.0, "y_max": 100.0, "elevations": [1, 2, 3, 4, 5, 6]}}


def request(head, fmt="terrain-csv"):
    return {"drawing_id": "solar", "expected_head": head["state"]["artifact_id"], "format": fmt}


def run_read(backend, head, fmt="terrain-csv"):
    return read.run_local_graph_read(backend, TENANT, TOOL, request(head, fmt),
                                     drawing_id="solar", source_version=1, job_id=JOB)


def proof(backend, result):
    output = result["output"]
    return read.graph_read_provenance(result, request(output["head"], output["summary"]["format"]),
                                      TENANT, JOB, TOOL, 1, backend=backend)


def keys(backend):
    return set(backend.drawing_object_keys(TENANT, "solar"))


def snapshot(backend):
    return {key: backend.get(key) for key in keys(backend)}


def refused(code, fn, *args, **kwargs):
    with pytest.raises(GraphValidationError) as error:
        fn(*args, **kwargs)
    assert error.value.code == code
    assert str(error.value) == f"{code}: <root>"


def direct(doc=None, fmt="shade-sam", head=None):
    head = {"state": {"artifact_id": "a" * 64}} if head is None else head
    return builtin().run({}, {"expected_head": head["state"]["artifact_id"], "format": fmt},
                         {"head": head, "document": document() if doc is None else doc})


def pinned(backend, head, fmt):
    result = run_read(backend, head, fmt)
    output = result["output"]
    assert set(output) == {"head", "summary", "artifact"}
    summary, ref = output["summary"], output["artifact"]
    assert set(summary) == SUMMARY_KEYS
    assert summary["head"] == output["head"] == head
    assert summary["schema"] == "leaf.solar-physical-export.v1"
    assert summary["maturity"] == "preview" and summary["format"] == fmt
    assert summary["units"] == {"drawing_units": "m", "meters_per_unit": 1.0}
    assert set(ref) == {"schema", "artifact_id", "media_type", "filename", "byte_length",
                        "content_sha256", "source_version", "download"}
    assert ref["schema"] == artifacts.REF_SCHEMA and ref["media_type"] == "text/csv"
    assert ref["filename"] == ("terrain.csv" if fmt == "terrain-csv" else fmt + ".csv")
    assert ref["source_version"] == result["source_version"] == 1
    assert ref["download"] == "/api/drawings/solar/artifacts/" + ref["artifact_id"]
    assert len(ref["artifact_id"]) == 64 and set(ref["artifact_id"]) <= set("0123456789abcdef")
    _, content = artifacts.read_artifact(backend, TENANT, "solar", ref["artifact_id"])
    assert (len(content), hashlib.sha256(content).hexdigest()) == PINS[fmt]
    assert (ref["byte_length"], ref["content_sha256"]) == PINS[fmt]
    assert result["drawing_changed"] is False
    if fmt != "terrain-csv":
        assert summary["scope"] == "cpu-terrain-native-frame-centres"
        assert summary["grid"] == {"rows": 21, "cols": 21, "cells": 441}
        assert (summary["sample_count"], summary["profile"], summary["mean_shade"],
                summary["datum_shift_m"]) == (15, "full", 0.0, 1.5)
        assert summary["settings"] == {"mode": "defaults", "target_clearance_m": 1.5,
                                       "profile_selection": "automatic"}
        assert not content.startswith(b"\xef\xbb\xbf")
        assert b"\n" not in content.replace(b"\r\n", b"")
    return result, content


def test_pex_01_declaration_and_discovery(backend, graph, monkeypatch):
    declared = json.loads((SERVER / "solar_tools/solar_physical_export.json").read_text())
    expected = json.loads((SERVER / "solar_tools/solar_terrain_read.json").read_text())
    expected.update(name=TOOL, builtin="builtins/solar_physical_export.py",
                    invalid_request_code="INVALID_PHYSICAL_EXPORT_REQUEST", order=60,
                    scenario="w4-physical-export", ledger=["terrain-csv-export", "shade-sim"])
    expected["record"].update(name=TOOL, engine_op="solar_physical_export",
        entry="builtins/solar_physical_export.py", description=(
            "Download one terrain or CPU terrain shade CSV from the requested physical head. "
            "Shade samples native frame centres using default clearance and automatic profiles. "
            "Excludes weather weighting and individual-module shading."), params={
            "type": "object", "properties": {"drawing_id": {"type": "string", "maxLength": 128},
            "expected_head": {"type": "string", "minLength": 64, "maxLength": 64,
                              "pattern": "^[0-9a-f]{64}$"},
            "format": {"type": "string", "enum": list(PINS)}},
            "required": ["expected_head", "format"], "additionalProperties": False})
    assert declared == expected
    assert builtin().READS_PHYSICAL_HEAD is True
    assert builtin().MAX_HEAD_BYTES == read._load_builtin("solar-physical-shade").MAX_HEAD_BYTES == 8192
    assert TOOL in read.local_graph_read_tools()
    entries = solar_tools.entries()
    assert len(entries) == len(list((SERVER / "solar_tools").glob("*.json")))
    assert set(read.local_graph_read_tools()) == {e["name"] for e in entries if e["adapter"] == "local-graph-read"}
    assert [(e["order"], e["name"]) for e in entries if e["wave"] == 4] == [
        (30, "solar-solaredge-accept"), (31, "solar-solaredge-tracking-read"),
        (40, "solar-pvcase-convert"), (40, "solar-terrain-read"), (50, "solar-physical-shade"),
        (50, "solar-pvcase-solve"), (60, TOOL), (60, "solar-pvcase-export")]
    assert availability.w1_local_commit_inputs(graph)[TOOL] == {"input_ready": True, "input_reason": None}
    monkeypatch.setattr(deps, "tenant_repo_dir", lambda tenant: None)
    monkeypatch.setattr(deps, "load_tenant_repo_tools", lambda tenant: [])
    monkeypatch.setattr(deps, "_AUTHORED", [])
    found = [(f, c) for f in catalog.build_catalog(deps.all_tools(TENANT))
             for c in f["capabilities"] if c["name"] == TOOL]
    assert len(found) == 1 and found[0][0]["family_id"] == "terrain"


def test_pex_02_terrain_bytes(backend):
    result, content = pinned(backend, _publish_head(backend, terrain_state()), "terrain-csv")
    assert content == ("\ufeffX,Y,Z\r\n0.000,0.000,1.000\r\n100.000,0.000,2.000\r\n"
                       "200.000,0.000,3.000\r\n0.000,100.000,4.000\r\n"
                       "100.000,100.000,5.000\r\n200.000,100.000,6.000\r\n").encode("utf-8")
    summary = result["output"]["summary"]
    assert summary["grid"] == {"rows": 2, "cols": 3, "cells": 6}
    assert summary["scope"] == "terrain-nodes"
    assert all(summary[k] is None for k in ("settings", "sample_count", "profile", "mean_shade", "datum_shift_m"))


def test_pex_03_azal_bytes(backend):
    _, content = pinned(backend, _publish_head(backend, field()), "shade-azal-matrix")
    assert len(content.splitlines()) == 14


def test_pex_04_sam_bytes(backend):
    _, content = pinned(backend, _publish_head(backend, field()), "shade-sam")
    assert len(content.splitlines()) == 469


def test_pex_05_per_frame_bytes(backend):
    _, content = pinned(backend, _publish_head(backend, field()), "shade-per-panel")
    lines = content.decode().splitlines()
    assert len(lines) == 7021
    assert lines[0] == "PanelIndex,AzimuthDeg,AltitudeDeg,ShadeFraction"
    assert lines[-1] == "14,350,85,0.0000"


def test_pex_06_closed_request():
    valid = {"expected_head": "a" * 64, "format": "terrain-csv"}
    validator = Draft7Validator(solar_tools.trusted_record(TOOL)["params"])
    assert validator.is_valid(valid)
    cases = [None, [], "", 0, {}, {"expected_head": "a" * 64}, {"format": "terrain-csv"}]
    cases += [dict(valid, expected_head=v) for v in (None, 1, True, "a" * 63, "A" * 64, "g" * 64, "a" * 64 + "\n")]
    cases += [dict(valid, format=v) for v in (None, [], 1, "", "csv", "shade_csv_text", "annual-shade", "terrain", "all")]
    cases += [dict(valid, **{k: None}) for k in ("settings", "profile", "units", "geometry", "destination",
              "filename", "project_id", "expected_rev", "initialize", "formats")]
    for params in cases:
        assert not validator.is_valid(params)
        refused("INVALID_PHYSICAL_EXPORT_REQUEST", builtin().run, {}, params)
    refused("INVALID_PHYSICAL_EXPORT_REQUEST", builtin().run, {}, dict(valid, drawing_id="solar"))
    for wrapper in ([], {}, {"head": [], "document": document()},
                    {"head": {}, "document": document()},
                    {"head": {"state": {"artifact_id": "A" * 64}}, "document": document()},
                    {"head": {"state": {"artifact_id": "a" * 64}}, "document": []}):
        refused("PHYSICAL_SHADE_INPUT_INVALID", builtin().run, {}, valid, wrapper)
    refused("PHYSICAL_SHADE_OUTPUT_LIMIT_EXCEEDED", direct, None, "shade-sam",
            {"state": {"artifact_id": "a" * 64}, "large": "x" * 8193})


def test_pex_07_stale_head_before_compute(backend, monkeypatch):
    parent = _publish_head(backend, terrain_state())
    state = terrain_state()
    state["settings"] = {"ignored": True}
    _publish_head(backend, state, parent=parent["state"]["artifact_id"])
    monkeypatch.setattr(builtin(), "hydrate_physical_document", forbidden)
    monkeypatch.setattr(builtin(), "_terrain", forbidden)
    monkeypatch.setattr(analysis, "terrain_csv_file_bytes", forbidden)
    monkeypatch.setattr(shade, "shade_sim", forbidden)
    monkeypatch.setattr(artifacts.ArtifactSink, "prepare", forbidden)
    before = snapshot(backend)
    for fmt in PINS:
        refused("PHYSICAL_EXPORT_HEAD_MOVED", run_read, backend, parent, fmt)
    assert snapshot(backend) == before


def test_pex_08_head_required(backend):
    before = snapshot(backend)
    refused("PHYSICAL_SHADE_HEAD_REQUIRED", run_read, backend, {"state": {"artifact_id": "a" * 64}})
    assert snapshot(backend) == before


def test_pex_09_terrain_without_frames_and_feet(backend):
    state = terrain_state()
    head = _publish_head(backend, state)
    _, original = pinned(backend, head, "terrain-csv")
    state["tracker_rows"] = [{"row": 0}]
    for key in ("x_min", "x_max", "y_min", "y_max"):
        state["grid"][key] /= 0.3048
    doc = ps.physical_document(state, drawing_units="ft", source_sha256="a" * 64,
                               capability="frame-generate", parent=head["state"]["artifact_id"])
    feet = ph.publish_physical_state(backend, TENANT, "solar", doc)["head"]
    result = run_read(backend, feet)
    assert result["output"]["summary"]["units"] == {"drawing_units": "ft", "meters_per_unit": 0.3048}
    assert artifacts.read_artifact(backend, TENANT, "solar", result["output"]["artifact"]["artifact_id"])[1] == original


def test_pex_10_shared_hydration_refusals(backend, monkeypatch):
    assert builtin().hydrate_physical_document is hydration.hydrate_physical_document
    cases = []
    for frames, rows, code in (([], [{}], "MANUAL_ROWS_UNSUPPORTED"), ([], [], "NATIVE_FRAMES_REQUIRED"),
                              ([None], [], "FRAMES_INVALID"), ({}, [], "FRAMES_INVALID")):
        doc = document()
        doc["state"].update(frames=frames, tracker_rows=rows)
        cases.append((doc, "PHYSICAL_SHADE_" + code))
    for grid, code in ((None, "GRID_MISSING"), ({}, "GRID_INVALID"),
                       ({"rows": 301, "cols": 300}, "INPUT_LIMIT_EXCEEDED")):
        doc = document()
        doc["state"]["grid"] = grid
        cases.append((doc, "PHYSICAL_SHADE_" + code))
    doc = document()
    doc["units"]["drawing_units"] = "cm"
    cases.append((doc, "PHYSICAL_STATE_UNITS_UNSUPPORTED"))
    doc = document()
    doc["frame"]["transform"][0] = 2
    cases.append((doc, "PHYSICAL_SHADE_FRAME_UNSUPPORTED"))
    doc = document()
    doc["state"]["frames"] *= 1334
    cases.append((doc, "PHYSICAL_SHADE_INPUT_LIMIT_EXCEEDED"))
    before = snapshot(backend)
    monkeypatch.setattr(artifacts.ArtifactSink, "prepare", forbidden)
    for doc, code in cases:
        refused(code, direct, doc)
    # Terrain uses the shared grid rules without reading frames or manual rows.
    for grid, code in ((None, "GRID_MISSING"), ({}, "GRID_INVALID"),
                       ({"rows": 301, "cols": 300}, "INPUT_LIMIT_EXCEEDED"),
                       ({"frame": {}}, "FRAME_UNSUPPORTED")):
        doc = document(terrain_state())
        doc["state"]["grid"] = grid
        refused("PHYSICAL_SHADE_" + code, direct, doc, "terrain-csv")
    for key in ("state", "units", "frame", "source"):
        doc = document(terrain_state())
        doc[key] = []
        refused("PHYSICAL_SHADE_INPUT_INVALID", direct, doc, "terrain-csv")
    for unit, mpu in (("cm", 0.01), ("m", 1), ("ft", 1.0), ("m", True)):
        doc = document(terrain_state())
        doc["units"] = {"drawing_units": unit, "meters_per_unit": mpu}
        refused("PHYSICAL_STATE_UNITS_UNSUPPORTED", direct, doc, "terrain-csv")
    doc = document(terrain_state())
    doc["frame"]["transform"][0] = 2
    refused("PHYSICAL_SHADE_FRAME_UNSUPPORTED", direct, doc, "terrain-csv")
    assert snapshot(backend) == before


def test_pex_11_defaults_and_input_isolation(backend, graph):
    state = field()
    state.update(settings={"TorqueTubeHeightM": 5}, shade_heatmap=[{"old": True}],
                 shade_files={"shade-sam": "cached content is not authoritative"})
    head = _publish_head(backend, state)
    _, doc = ph.load_physical_head(backend, TENANT, "solar", project_id=graph["project"]["id"])
    params = {"expected_head": head["state"]["artifact_id"], "format": "shade-sam"}
    wrapper = {"head": head, "document": doc}
    saved = copy.deepcopy((graph, params, wrapper))
    value = builtin().run(graph, params, wrapper)
    assert (graph, params, wrapper) == saved
    assert hashlib.sha256(value.content).hexdigest() == PINS["shade-sam"][1]
    assert value.summary["datum_shift_m"] == 1.5
    before = snapshot(backend)
    pinned(backend, head, "shade-sam")
    assert all(backend.get(k) == v for k, v in before.items())
    assert all("/artifacts/" in k for k in keys(backend) - set(before))


def test_pex_12_kernel_failures(backend, monkeypatch):
    def broken(*a, **k):
        raise RuntimeError("private geometry and kernel message")
    before = snapshot(backend)
    real = analysis.terrain_csv_file_bytes(terrain_state()["grid"], 1.0)
    monkeypatch.setattr(analysis, "terrain_csv_file_bytes", broken)
    refused("PHYSICAL_EXPORT_KERNEL_FAILED", direct, document(terrain_state()), "terrain-csv")
    for value in (None, "CSV", b"", b"not csv", b"\xff",
                  b"\xef\xbb\xbfX,Y,Z\r\n",
                  b"\xef\xbb\xbfX,Y,Z\r\nnot,a,number\r\n",
                  b"\xef\xbb\xbfX,Y,Z\r\n0,0,nan\r\n",
                  b"\xef\xbb\xbfX,Y,Z\r\n" + b"0.000,0.000,nan\r\n" * 6,
                  real[:-2], real.rsplit(b"\r\n", 2)[0] + b"\r\n"):
        monkeypatch.setattr(analysis, "terrain_csv_file_bytes", lambda *a: value)
        refused("PHYSICAL_EXPORT_KERNEL_FAILED", direct, document(terrain_state()), "terrain-csv")
    monkeypatch.setattr(shade, "shade_sim", broken)
    refused("PHYSICAL_SHADE_KERNEL_FAILED", direct)
    for defect in ("failed", "nan", "shift", "weighted", "panels", "profile", "surface", "file", "missing"):
        sim = controlled(15)
        sim["files"] = {"shade-sam": "header\r\n"}
        if defect == "failed":
            sim["succeeded"] = False
        elif defect == "nan":
            sim["mean_shade"] = float("nan")
        elif defect == "shift":
            sim["binding"]["shift_m"] = float("inf")
        elif defect == "weighted":
            sim["result"]["weighted_per_panel"] = {}
        elif defect == "panels":
            sim["panels"][0]["entity"] = True
        elif defect == "profile":
            sim["profile"]["ray_step_m"] = float("nan")
        elif defect == "surface":
            sim["surface"]["cells"] = 1
        elif defect == "file":
            sim["files"]["shade-sam"] = b"header\r\n"
        else:
            del sim["files"]
        monkeypatch.setattr(shade, "shade_sim", lambda *a, **k: sim)
        refused("PHYSICAL_SHADE_KERNEL_FAILED", direct)
    assert snapshot(backend) == before


def test_pex_13_large_artifact(backend):
    state = field()
    state["frames"] = (state["frames"] * 80)[:1197]
    result = run_read(backend, _publish_head(backend, state), "shade-per-panel")
    ref = result["output"]["artifact"]
    content = artifacts.read_artifact(backend, TENANT, "solar", ref["artifact_id"])[1]
    assert len(content) == ref["byte_length"] == 4816417
    assert hashlib.sha256(content).hexdigest() == "44c300f51f9f544b3a3fd1973bdfee672cd7121717062dd1ece247044fcc7e57"
    summary = result["output"]["summary"]
    assert (summary["sample_count"], summary["profile"], summary["mean_shade"]) == (1197, "balanced", 0.0)
    assert set(result["output"]) == {"head", "summary", "artifact"} and set(summary) == SUMMARY_KEYS
    assert result["output_bytes"] == len(canonical_bytes(result["output"])) < read.MAX_OUTPUT_BYTES
    assert b"PanelIndex" not in canonical_bytes(result["output"])


def test_pex_14_physical_artifact_head_validation(backend, monkeypatch):
    head = _publish_head(backend, terrain_state())
    wrapper = {"head": head, "document": document(terrain_state())}
    sink = SimpleNamespace(prepare=forbidden, finish=forbidden)
    for summary in ([], {}, {"head": None}, {"head": []}, {"head": dict(head, index=99)}):
        value = artifacts.ArtifactOutput(summary, "text/csv", "terrain.csv", b"X,Y,Z\r\n")
        monkeypatch.setattr(read, "_load_builtin", lambda tool: SimpleNamespace(
            READS_PHYSICAL_HEAD=True, run=lambda *a, **k: value))
        refused("READ_OUTPUT_INVALID", read._read_output, TOOL, {}, {}, sink, physical_head=wrapper)
    value = artifacts.ArtifactOutput({"head": copy.deepcopy(head)}, "text/csv", "terrain.csv", b"X,Y,Z\r\n")
    calls = []
    sink = SimpleNamespace(prepare=lambda v: (calls.append("prepare") or SimpleNamespace(ref={"ok": True})),
                           finish=lambda p: calls.append("finish"))
    output, data = read._read_output(TOOL, {}, {}, sink, physical_head=wrapper)
    assert output == {"head": head, "summary": value.summary, "artifact": {"ok": True}}
    assert data == canonical_bytes(output) and calls == ["prepare", "finish"]


def test_pex_15_ordinary_artifact_shape(backend, monkeypatch):
    ordinary.builtin(monkeypatch)
    result = ordinary.run(backend)
    assert result["output"] == {"summary": {"rows": 2}, "artifact": ordinary.R1}
    assert result["output_bytes"] == 431 and result["output_sha256"] == ordinary.OUTPUT_SHA


def test_pex_16_repeat_identity(backend):
    head = _publish_head(backend, terrain_state())
    first = run_read(backend, head)
    before = snapshot(backend)
    assert run_read(backend, head) == first and snapshot(backend) == before


def test_pex_17_head_binds_request(backend):
    head = _publish_head(backend, terrain_state())
    first = run_read(backend, head)
    state = terrain_state()
    state["settings"] = {"ignored": 1}
    child = _publish_head(backend, state, parent=head["state"]["artifact_id"])
    second = run_read(backend, child)
    a, b = first["output"]["artifact"], second["output"]["artifact"]
    assert first["request_sha256"] != second["request_sha256"] and a["artifact_id"] != b["artifact_id"]
    assert a["content_sha256"] == b["content_sha256"] == PINS["terrain-csv"][1]
    assert first["graph_sha256"] == second["graph_sha256"]
    assert first["source_version"] == second["source_version"] == 1


def test_pex_18_format_binds_request(backend):
    head = _publish_head(backend, field())
    results = [pinned(backend, head, fmt)[0] for fmt in list(PINS)[1:]]
    assert len({r["request_sha256"] for r in results}) == 3
    assert len({r["output"]["artifact"]["artifact_id"] for r in results}) == 3


def test_pex_19_historical_proof(backend, monkeypatch):
    head = _publish_head(backend, terrain_state())
    first = run_read(backend, head)
    state = terrain_state()
    state["grid"]["elevations"][0] = 10
    child = _publish_head(backend, state, parent=head["state"]["artifact_id"])
    second = run_read(backend, child)
    before = snapshot(backend)
    monkeypatch.setattr(ph, "load_physical_head", forbidden)
    monkeypatch.setattr(ph, "physical_head", forbidden)
    monkeypatch.setattr(backend, "put", forbidden)
    monkeypatch.setattr(backend, "put_if_absent_or_verify", forbidden)
    for result in (first, second):
        assert proof(backend, result)["output_sha256"] == result["output_sha256"]
    assert snapshot(backend) == before


def test_pex_20_forged_output_proof(backend):
    original = run_read(backend, _publish_head(backend, terrain_state()))
    for defect in ("head", "summary-head", "summary-value"):
        result = copy.deepcopy(original)
        if defect == "head":
            result["output"]["head"]["index"] += 1
        elif defect == "summary-head":
            result["output"]["summary"]["head"]["index"] += 1
        else:
            result["output"]["summary"]["grid"]["cells"] += 1
        result.update(output_sha256=digest(result["output"]), output_bytes=len(canonical_bytes(result["output"])))
        with pytest.raises(ValueError, match="^graph read terminal proof rejected$"):
            proof(backend, result)


def test_pex_21_corrupt_artifact_proof(backend, monkeypatch):
    result = run_read(backend, _publish_head(backend, terrain_state()))
    ref = result["output"]["artifact"]
    prefix = store.drawing_prefix(TENANT, "solar") + "/artifacts/"
    for key in (prefix + "blobs/" + ref["content_sha256"] + ".bin", prefix + ref["artifact_id"] + ".json"):
        original = backend.get(key)
        backend.put(key, b"tampered")
        before = snapshot(backend)
        with monkeypatch.context() as patch:
            patch.setattr(backend, "put", forbidden)
            patch.setattr(backend, "put_if_absent_or_verify", forbidden)
            with pytest.raises(ValueError, match="^graph read terminal proof rejected$"):
                proof(backend, result)
            assert snapshot(backend) == before
        backend.put(key, original)
    forged = copy.deepcopy(result)
    forged["output"]["artifact"]["filename"] = "other.csv"
    forged.update(output_sha256=digest(forged["output"]), output_bytes=len(canonical_bytes(forged["output"])))
    monkeypatch.setattr(backend, "put_if_absent_or_verify", forbidden)
    with pytest.raises(ValueError, match="^graph read terminal proof rejected$"):
        proof(backend, forged)


def test_pex_22_download_contract(backend, client):
    result, content = pinned(backend, _publish_head(backend, terrain_state()), "terrain-csv")
    ref = result["output"]["artifact"]
    response = client.get(ref["download"], headers={"X-Tenant-Id": TENANT})
    assert response.status_code == 200 and response.content == content
    assert response.headers["content-type"].startswith("text/csv")
    expected = {"content-disposition": 'attachment; filename="terrain.csv"',
                "etag": '"' + ref["content_sha256"] + '"', "x-leaf-artifact-id": ref["artifact_id"],
                "x-leaf-source-version": "1", "cache-control": "private, no-cache",
                "x-content-type-options": "nosniff"}
    for key, value in expected.items():
        assert response.headers[key] == value
    cached = client.get(ref["download"], headers={"X-Tenant-Id": TENANT, "If-None-Match": expected["etag"]})
    assert cached.status_code == 304 and cached.content == b""
    other = client.get(ref["download"], headers={"X-Tenant-Id": "other-tenant"})
    assert other.status_code == 404 and other.json()["error"]["reason_code"] == "ARTIFACT_NOT_FOUND"


def test_pex_23_artifact_refusals(backend, graph, monkeypatch):
    head = _publish_head(backend, terrain_state())
    before = snapshot(backend)
    assert artifacts.MAX_ARTIFACT_BYTES == 16777216
    with monkeypatch.context() as patch:
        patch.setattr(analysis, "terrain_csv_file_bytes",
                      lambda *a: b"\xef\xbb\xbfX,Y,Z\r\n" + b"0" * artifacts.MAX_ARTIFACT_BYTES
                      + b".000,0.000,0.000\r\n" + b"0.000,0.000,0.000\r\n" * 5)
        refused("ARTIFACT_TOO_LARGE", run_read, backend, head)
    with monkeypatch.context() as patch:
        patch.setenv("LEAF_DRAWING_MUTATIONS_ENABLED", "0")
        refused("ARTIFACT_WRITES_DRAINED", run_read, backend, head)
    def failed(*a, **k):
        raise OSError("private store path")
    with monkeypatch.context() as patch:
        patch.setattr(backend, "put_if_absent_or_verify", failed)
        refused("ARTIFACT_STORE_UNAVAILABLE", run_read, backend, head)
    assert snapshot(backend) == before
    assert store.load_manifest(backend, TENANT, "solar")["head"] == 1
    assert ph.load_physical_head(backend, TENANT, "solar", project_id=graph["project"]["id"])[0] == head


def test_pex_24_output_limit_before_persistence(backend, monkeypatch):
    head = _publish_head(backend, terrain_state())
    assert read.MAX_OUTPUT_BYTES == 1048576
    value = artifacts.ArtifactOutput({"head": head, "padding": "x" * read.MAX_OUTPUT_BYTES},
                                     "text/csv", "terrain.csv", b"X,Y,Z\r\n")
    monkeypatch.setattr(builtin(), "run", lambda *a, **k: value)
    monkeypatch.setattr(artifacts.ArtifactSink, "finish", forbidden)
    before = snapshot(backend)
    refused("READ_OUTPUT_LIMIT_EXCEEDED", run_read, backend, head)
    refused("READ_OUTPUT_INVALID", read._read_output, TOOL, {}, {},
            physical_head={"head": head, "document": document(terrain_state())})
    assert snapshot(backend) == before
