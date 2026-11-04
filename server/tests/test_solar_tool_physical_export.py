"""The 24 Ground Physical Record 3 export contracts."""
import copy
import decimal
import hashlib
import json
import re
import sys
import uuid
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
import solar_ground_buildout as bo
import solar_ground_layout as ground_layout
import solar_ground_shade as shade
import solar_local_read as read
import solar_physical_analysis as hydration
import solar_physical_head as ph
import solar_physical_state as ps
import solar_project_artifacts
import solar_tools
import store
import write_loop
from solar_ground_conversion import tracker_entities
from leaf_cloud_client import canonical_bytes
from solar_design_graph import GraphValidationError
from solar_sizing_client import digest
import test_solar_artifacts as ordinary
from test_solar_artifacts import client  # noqa: F401
from test_solar_local_read import TENANT, JOB, backend, _publish_head, forbidden  # noqa: F401
from test_solar_tool_physical_shade import field, document, controlled
from test_solar_physical_state import PROJECT
from test_w1_design_graph import graph  # noqa: F401
from test_w1_local_graph_adapter import held
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
        (50, "solar-pvcase-solve"), (60, TOOL), (60, "solar-pvcase-export"), (70, "solar-takeoff")]
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


# ---- solar-takeoff (E1) ----
TAKEOFF = "solar-takeoff"


def takeoff_builtin():
    return read._load_builtin(TAKEOFF)


def takeoff_binding(version=1):
    return {"drawing_id": "solar", "drawing_version": version,
            "graph_sha256": "b" * 64, "project_id": PROJECT}


def takeoff_pad(x=0, cut=0.1, fill=0.3):
    return {"vertices": [[x, 0], [x + 1, 0], [x + 1, 1], [x, 1]],
            "cut_fill": {"cut_m3": cut, "fill_m3": fill}}


def takeoff_state():
    return {"tracker_rows": [{"axis_start": [0, 0], "axis_end": [0, 20], "slots": 10}],
            "settings": {"TrackerModulePmaxW": 400}, "grade_pads": [takeoff_pad()]}


def takeoff_wrapper(state=None, unit="m"):
    return {"head": {"index": 0, "state": {
        "schema": artifacts.REF_SCHEMA, "artifact_id": "a" * 64,
        "content_sha256": "c" * 64, "source_version": 1, "media_type": "application/json",
        "filename": "physical.json", "byte_length": 1, "download": "/physical"}},
        "document": document(takeoff_state() if state is None else state, unit=unit)}


def takeoff_direct(graph=None, params=None, physical=None, source=None):
    return takeoff_builtin().run({} if graph is None else graph, {} if params is None else params,
                                  physical, takeoff_binding() if source is None else source)


def takeoff_read(backend, version=1, params=None, tenant=TENANT):
    return read.run_local_graph_read(backend, tenant, TAKEOFF,
                                     {"drawing_id": "solar", **(params or {})},
                                     drawing_id="solar", source_version=version, job_id=JOB)


def takeoff_lines(value):
    summary = value.summary if type(value) is artifacts.ArtifactOutput else value["output"]["summary"]
    return {line["line_id"]: line for line in summary["lines"]}


def takeoff_physical_lines(value):
    return [line for line in takeoff_lines(value).values() if line["source"]["kind"] == "physical"]


def takeoff_proof(backend, result, params=None):
    return read.graph_read_provenance(result, {"drawing_id": "solar", **(params or {})},
                                      TENANT, JOB, TAKEOFF, result["source_version"], backend=backend)


def test_takeoff_declaration(graph, monkeypatch):
    declared = json.loads((SERVER / "solar_tools/solar_takeoff.json").read_text())
    expected = json.loads((SERVER / "solar_tools/solar_physical_export.json").read_text())
    expected.update(name=TAKEOFF, builtin="builtins/solar_takeoff.py", family="schedules",
                    invalid_request_code="INVALID_TAKEOFF_REQUEST", order=70,
                    scenario="ge-takeoff", ledger=["bill-of-materials", "pad-grading"])
    expected["record"].update(name=TAKEOFF, family_id="schedules", engine_op="solar_takeoff",
        entry="builtins/solar_takeoff.py", description=(
            "Incomplete preview takeoff with no prices. Pile spacing is in 0.1 m steps."), params={
            "type": "object", "properties": {"drawing_id": {"type": "string", "maxLength": 128},
            "pile_spacing_m": {"type": "number", "minimum": 0.5, "maximum": 100}},
            "required": [], "additionalProperties": False})
    assert declared == expected
    assert b"\r" not in (SERVER / "solar_tools/solar_takeoff.json").read_bytes()
    ledger = json.loads((SERVER.parent / "docs/parity/solar-ledger.json").read_text())
    assert set(declared["ledger"]) <= {row["capability"] for row in ledger["rows"] if row["class"] == "T"}
    module = takeoff_builtin()
    assert module.READS_PHYSICAL_HEAD is True and module.READS_SOURCE_BINDING is True
    assert TAKEOFF in read.local_graph_read_tools()
    assert availability.w1_local_commit_inputs(graph)[TAKEOFF] == {"input_ready": True, "input_reason": None}
    monkeypatch.setattr(deps, "tenant_repo_dir", lambda tenant: None)
    monkeypatch.setattr(deps, "load_tenant_repo_tools", lambda tenant: [])
    monkeypatch.setattr(deps, "_AUTHORED", [])
    found = [(f, c) for f in catalog.build_catalog(deps.all_tools(TENANT))
             for c in f["capabilities"] if c["name"] == TAKEOFF]
    assert len(found) == 1 and found[0][0]["family_id"] == "schedules"


@pytest.mark.parametrize("params,echo", [({}, "5.0"), ({"pile_spacing_m": 0.5}, "0.5"),
    ({"pile_spacing_m": 2.5}, "2.5"), ({"pile_spacing_m": 100}, "100.0")])
def test_takeoff_params_accepted(backend, params, echo):
    result = takeoff_read(backend, params=params)
    assert result["output"]["summary"]["settings"] == {"pile_spacing_m": echo}
    assert takeoff_proof(backend, result, params)


@pytest.mark.parametrize("params", [{"pile_spacing_m": v} for v in
    (True, 0, 0.4, 0.55, -1, 100.1, "NaN", float("nan"), float("inf"))] + [{"extra": 1}, [], None])
def test_takeoff_params_refused(params, backend, monkeypatch):
    monkeypatch.setattr(artifacts.ArtifactSink, "prepare", forbidden)
    before = snapshot(backend)
    refused("INVALID_TAKEOFF_REQUEST", takeoff_builtin().run, {}, params, None, takeoff_binding())
    assert snapshot(backend) == before


def test_takeoff_no_physical_state(backend):
    result = takeoff_read(backend)
    summary = result["output"]["summary"]
    assert summary["head"] is None and summary["complete"] is False
    assert summary["schema"] == "leaf.solar-takeoff.v1" and summary["maturity"] == "preview"
    assert summary["counts"]["not_computed"] == 14
    assert sum(summary["counts"].values()) == len(summary["lines"])
    for line in summary["lines"]:
        assert set(line) == {"line_id", "category", "description", "unit", "quantity",
                             "status", "reason", "priceable", "source"}
        assert line["unit"] in {"ea", "m", "ft", "m3", "kWp"}
        assert len(line["description"]) <= 200
        if line["source"]["kind"] == "physical":
            assert line["status"] == "unavailable" and line["reason"] == "PHYSICAL_STATE_ABSENT"
            assert line["quantity"] is None and not line["priceable"]
            assert "physical_state" not in line["source"]
        elif line["source"]["kind"] == "graph":
            assert line["status"] == "computed" and line["priceable"] and line["reason"] is None
        else:
            assert line["status"] == "not-computed" and not line["priceable"]
            assert line["quantity"] is None and line["reason"].startswith("phase-b:")
    assert len(takeoff_physical_lines(result)) == 9
    phase = [line["line_id"] for line in summary["lines"] if line["status"] == "not-computed"]
    assert phase == ["phase_b." + key for key in (
        "dc_homeruns", "trench", "harness", "roads", "fence", "string_wire", "feeder_conductor_spec",
        "ac_collection", "transformers_mv", "conduit", "grounding", "terminations", "drainage", "labor")]


def test_takeoff_graph_grouping_and_decimal_totals():
    graph = {"strings": [{"module_count": n} for n in (10, 12, 12)],
             "inverters": [{"is_l2": False, "equipment_type": "string_inverter", "model": "A"}] * 2
                + [{"is_l2": True, "equipment_type": "central_inverter", "model": "B"},
                   {"is_l2": False, "model": "legacy"}],
             "routes": [{"route_kind": "feeder", "wire_gauge": g, "length_ft": n}
                        for g, n in (("4/0", 100.1), ("4/0", 200.2), ("", 50), ("4-0", 7))]}
    output = takeoff_direct(graph)
    lines = takeoff_lines(output)
    assert lines["strings.count"]["quantity"] == "3"
    assert lines["strings.modules"]["quantity"] == "34"
    assert lines["feeders.count"]["quantity"] == "4"
    inverters = {key: line for key, line in lines.items() if key.startswith("inverters.")}
    assert len(inverters) == 3
    for model, prefix, count in (("A", "l1.string_inverter", "2"),
                                  ("B", "l2.central_inverter", "1"), ("legacy", "l1.unclassified", "1")):
        key = "inverters." + prefix + ".m_" + hashlib.sha256(model.encode()).hexdigest()[:30]
        assert inverters[key]["quantity"] == count
    for gauge, quantity in (("4/0", "300.300"), ("4-0", "7.000")):
        key = "feeders.length.g_" + hashlib.sha256(gauge.encode()).hexdigest()[:30]
        assert lines[key]["quantity"] == quantity
    assert lines["feeders.length.unspecified"]["quantity"] == "50.000"
    ids = [line["line_id"] for line in output.summary["lines"]]
    assert len(ids) == len(set(ids))
    assert all(re.fullmatch(r"^[a-z][a-z0-9_]{0,31}(\.[a-z][a-z0-9_]{0,31}){0,3}$", key) for key in ids)
    assert ids[:2] == ["strings.count", "strings.modules"]
    assert ids[2:5] == sorted(inverters)


@pytest.mark.parametrize("unit,frames_only", [("m", False), ("ft", True)])
def test_takeoff_ground_bom_equivalence(unit, frames_only):
    state = takeoff_state()
    if frames_only:
        state["tracker_rows"] = []
        state["frames"] = [{"layer": "LEAF-TRACKERS", "vertices": [[0, 0], [2, 0], [2, 40], [0, 40]]}]
    wrapper = takeoff_wrapper(state, unit)
    original = copy.deepcopy(wrapper)
    quantities = []
    for spacing in (5.0, 2.5):
        value = takeoff_direct(params={"pile_spacing_m": spacing}, physical=wrapper)
        lines = takeoff_lines(value)
        module = ground_layout.get_active_module(ground_layout.load_settings(state["settings"]))
        spec = {"cross_axis_m": module.cross_axis_m, "along_axis_m": module.along_axis_m,
                "pmax_w": module.pmax_w}
        rows = bo.read_tracker_rows(tracker_entities({"frames": state.get("frames", []),
            "tracker_rows": state["tracker_rows"]}), ps.UNITS[unit])
        expected = bo.compute_bom(rows, spec, spacing)
        mapping = {("Module", "ea"): "modules", ("Torque Tube", "m"): "tube_length",
                   ("Torque Tube", "ea"): "tube_sections", ("Pile", "ea"): "piles",
                   ("Drive Unit", "ea"): "drives", ("DC Capacity", "kWp"): "dc_capacity"}
        for item in expected["lines"]:
            line = lines["ground." + mapping[item["category"], item["unit"]]]
            d = decimal.Decimal(repr(float(item["quantity"])))
            quantity = str(int(d)) if item["unit"] == "ea" else str(d.quantize(
                decimal.Decimal("0.001"), rounding=decimal.ROUND_HALF_EVEN))
            assert line["quantity"] == quantity and line["description"] == item["description"][:200]
        quantities.append({key: line["quantity"] for key, line in lines.items() if key.startswith("ground.")})
    assert quantities[0]["ground.piles"] != quantities[1]["ground.piles"]
    assert {k: v for k, v in quantities[0].items() if k != "ground.piles"} == {
        k: v for k, v in quantities[1].items() if k != "ground.piles"}
    assert wrapper == original


def test_takeoff_ground_absent_rows_and_unset_power(monkeypatch):
    module = takeoff_builtin()
    with monkeypatch.context() as patch:
        patch.setattr(bo, "compute_bom", forbidden)
        value = takeoff_direct(physical=takeoff_wrapper({}))
        assert all(line["reason"] == "TRACKER_ROWS_ABSENT" for key, line in takeoff_lines(value).items()
                   if key.startswith("ground."))
    state = takeoff_state()
    state["settings"] = {}
    lines = takeoff_lines(takeoff_direct(physical=takeoff_wrapper(state)))
    assert lines["ground.dc_capacity"]["reason"] == "MODULE_POWER_UNSET"
    assert lines["ground.modules"]["status"] == "computed"
    assert module.TOOL == TAKEOFF


def test_takeoff_earthwork_decimal_and_negative_net():
    state = takeoff_state()
    state["grade_pads"] = [takeoff_pad(0, 0.1, 0.3), takeoff_pad(2, 0.2, 0)]
    lines = takeoff_lines(takeoff_direct(physical=takeoff_wrapper(state)))
    assert [lines["earthwork." + k]["quantity"] for k in ("cut", "fill", "net")] == ["0.300", "0.300", "0.000"]
    for key in ("cut", "fill", "net"):
        line = lines["earthwork." + key]
        assert "2 pads" in line["description"] and "2 pads" in line["source"]["basis"]
        assert "independently" in line["description"] and "overlapping pads refused" in line["description"]
        assert line["priceable"] is (key != "net")
    state["grade_pads"] = [takeoff_pad(0, 0, 12.5)]
    assert takeoff_lines(takeoff_direct(physical=takeoff_wrapper(state)))["earthwork.net"]["quantity"] == "-12.500"


@pytest.mark.parametrize("case,reason", [("empty", "GRADE_PADS_ABSENT"),
    ("no-terrain", "GRADE_PAD_WITHOUT_TERRAIN"), ("duplicate", "GRADE_PAD_OVERLAP"),
    ("overlap", "GRADE_PAD_OVERLAP"), ("edge", None), ("thousand", None)])
def test_takeoff_earthwork_pad_policy(case, reason):
    state = takeoff_state()
    pads = [takeoff_pad(0), takeoff_pad(2)]
    if case == "empty":
        pads = []
    elif case == "no-terrain":
        pads[0]["cut_fill"] = None
        pads[1] = copy.deepcopy(pads[0])  # Missing terrain wins over overlap.
    elif case == "duplicate":
        pads[1] = copy.deepcopy(pads[0])
    elif case == "overlap":
        pads[1] = takeoff_pad(0.5)
    elif case == "edge":
        pads[1] = takeoff_pad(1)
    elif case == "thousand":
        pads = [takeoff_pad(2 * n) for n in range(1000)]
    state["grade_pads"] = pads
    lines = takeoff_lines(takeoff_direct(physical=takeoff_wrapper(state)))
    for key in ("cut", "fill", "net"):
        line = lines["earthwork." + key]
        assert line["reason"] == reason
        assert line["status"] == ("computed" if reason is None else "unavailable")
    if case == "thousand":
        assert lines["earthwork.cut"]["quantity"] == "100.000"


def test_takeoff_source_binding_and_durability(backend, graph):
    head = _publish_head(backend, takeoff_state())
    result = takeoff_read(backend)
    assert takeoff_proof(backend, result)
    assert result["output"]["summary"]["source"] == {
        "drawing_id": "solar", "drawing_version": result["source_version"],
        "graph_sha256": result["graph_sha256"], "project_id": graph["project"]["id"]}
    assert result["output"]["head"] == head
    for line in takeoff_lines(result).values():
        assert line["source"]["drawing_version"] == result["source_version"]
    for line in takeoff_physical_lines(result):
        assert line["source"]["physical_state"] == {"head_index": head["index"],
            "artifact_id": head["state"]["artifact_id"], "content_sha256": head["state"]["content_sha256"],
            "state_source_version": head["state"]["source_version"]}
    second = takeoff_read(backend)
    ref = result["output"]["artifact"]
    assert second["output"]["artifact"]["artifact_id"] == ref["artifact_id"]
    _, content = artifacts.read_artifact(backend, TENANT, "solar", ref["artifact_id"])
    expected = json.dumps(result["output"]["summary"], sort_keys=True, separators=(",", ":"),
                          ensure_ascii=False, allow_nan=False).encode("utf-8")
    assert content == expected and hashlib.sha256(content).hexdigest() == ref["content_sha256"]
    assert ref["filename"] == "takeoff.json" and ref["media_type"] == "application/json"


def test_takeoff_version_follow_and_unbound_guard(backend, monkeypatch):
    monkeypatch.setenv("LEAF_SOLAR_PHYSICAL_VERSIONED", "1")
    store._physical_memo_clear()
    try:
        a = _publish_head(backend, takeoff_state())
        assert a["state"]["source_version"] == 1
        with held(backend) as fence:
            assert write_loop.commit_physical_version(backend, TENANT, "solar", project_id=PROJECT,
                parent_version=1, physical={"head_index": 0, "state_artifact_id": a["state"]["artifact_id"],
                "state_content_sha256": a["state"]["content_sha256"]}, holder="fixture-owner", fence=fence,
                apply_id="e1-a") == (2, True)
            state_b = takeoff_state()
            state_b["tracker_rows"][0]["slots"] = 20
            b = _publish_head(backend, state_b, parent=a["state"]["artifact_id"])
            assert b["state"]["source_version"] == 2
            assert write_loop.commit_physical_version(backend, TENANT, "solar", project_id=PROJECT,
                parent_version=2, physical={"head_index": 1, "state_artifact_id": b["state"]["artifact_id"],
                "state_content_sha256": b["state"]["content_sha256"]}, holder="fixture-owner", fence=fence,
                apply_id="e1-b") == (3, True)
            for version, expected in ((2, a), (3, b)):
                result = takeoff_read(backend, version)
                assert result["output"]["summary"]["head"] == expected
                assert takeoff_proof(backend, result)
                for line in takeoff_physical_lines(result):
                    assert line["source"]["physical_state"] == {"head_index": expected["index"],
                        "artifact_id": expected["state"]["artifact_id"],
                        "content_sha256": expected["state"]["content_sha256"],
                        "state_source_version": expected["state"]["source_version"]}
                    assert line["source"]["drawing_version"] == version
            assert store.undo(backend, TENANT, "solar", holder="fixture-owner", fence=fence) == 2
            assert takeoff_read(backend, 2)["output"]["summary"]["head"] == a
            older = takeoff_read(backend, 1)
            assert older["output"]["summary"]["head"] == b
            assert all(line["reason"] == "PHYSICAL_STATE_AHEAD_OF_VERSION" and line["quantity"] is None
                       for line in takeoff_physical_lines(older))
            assert all(line["status"] == "computed" for line in takeoff_lines(older).values()
                       if line["source"]["kind"] == "graph")
            assert takeoff_proof(backend, older)
    finally:
        store._physical_memo_clear()


def test_takeoff_tenant_isolation(backend, monkeypatch):
    _publish_head(backend, takeoff_state())
    before = snapshot(backend)
    other_keys = set(backend.drawing_object_keys("other-tenant", "solar"))
    monkeypatch.setattr(artifacts.ArtifactSink, "prepare", forbidden)
    refused("GRAPH_CONTEXT_UNAVAILABLE", takeoff_read, backend, tenant="other-tenant")
    assert snapshot(backend) == before
    assert set(backend.drawing_object_keys("other-tenant", "solar")) == other_keys


@pytest.mark.parametrize("defect", ["hex", "index", "mpu", "mixed", "uuid", "schema", "extra"])
def test_takeoff_malformed_physical_refuses(defect, backend, monkeypatch):
    wrapper = takeoff_wrapper()
    if defect == "hex":
        wrapper["head"]["state"]["artifact_id"] = "A" * 64
    elif defect == "index":
        wrapper["head"]["index"] = True
    elif defect == "mpu":
        wrapper["document"]["units"]["meters_per_unit"] = 2.0
    elif defect == "extra":
        wrapper["head"]["state"]["extra"] = 1
    else:
        wrapper = takeoff_project_wrapper()
        if defect == "mixed":
            wrapper["head"]["state"]["source_version"] = 1
        elif defect == "uuid":
            wrapper["head"]["state"]["source_version_id"] = wrapper["head"]["state"]["source_version_id"].upper()
        else:
            wrapper["head"]["state"]["schema"] = artifacts.REF_SCHEMA
    monkeypatch.setattr(artifacts.ArtifactSink, "prepare", forbidden)
    monkeypatch.setattr(read, "_current_physical_head", lambda *a, **k: wrapper)
    before = snapshot(backend)
    refused("TAKEOFF_PHYSICAL_INPUT_INVALID", takeoff_read, backend)
    assert snapshot(backend) == before


@pytest.mark.parametrize("defect,code", [("inverters", "TAKEOFF_LIMIT_EXCEEDED"),
    ("gauges", "TAKEOFF_LIMIT_EXCEEDED"), ("quantity", "TAKEOFF_LIMIT_EXCEEDED"),
    ("pads", "TAKEOFF_LIMIT_EXCEEDED"), ("read-rows", "TAKEOFF_KERNEL_FAILED"),
    ("frame", "TAKEOFF_KERNEL_FAILED"), ("bom", "TAKEOFF_LIMIT_EXCEEDED"),
    ("duplicate-line", "TAKEOFF_INPUT_INVALID"), ("nan", "TAKEOFF_INPUT_INVALID")])
def test_takeoff_fail_closed(defect, code, backend, graph, monkeypatch):
    wrapper = takeoff_wrapper()
    if defect == "pads":
        wrapper["document"]["state"]["grade_pads"] = [takeoff_pad(2 * n) for n in range(1001)]
    elif defect == "frame":
        wrapper["document"]["state"]["frames"] = [{"layer": "LEAF-TRACKERS"}]
    elif defect in ("read-rows", "bom"):
        def broken(*args, **kwargs):
            raise (bo.BuildoutInputError("private input") if defect == "read-rows"
                   else bo.BuildoutBoundsError("private bounds"))
        monkeypatch.setattr(bo, "read_tracker_rows" if defect == "read-rows" else "compute_bom", broken)
    elif defect == "duplicate-line":
        original = takeoff_builtin()._line
        def duplicate(*args, **kwargs):
            value = original(*args, **kwargs)
            value["line_id"] = "strings.count"
            return value
        monkeypatch.setattr(takeoff_builtin(), "_line", duplicate)
    else:
        modified = copy.deepcopy(graph)
        if defect == "inverters":
            modified["inverters"] = [dict(graph["inverters"][0], model=str(n)) for n in range(65)]
        else:
            modified["routes"] = [dict(graph["routes"][0], route_kind="feeder", wire_gauge=str(n),
                length_ft=2e12 if defect == "quantity" else float("nan") if defect == "nan" else 1)
                for n in range(33 if defect == "gauges" else 1)]
        resolve = read.resolve_graph_context
        def changed(*args, **kwargs):
            context = resolve(*args, **kwargs)
            return dict(context, graph=modified)
        monkeypatch.setattr(read, "resolve_graph_context", changed)
    monkeypatch.setattr(read, "_current_physical_head", lambda *a, **k: wrapper)
    monkeypatch.setattr(artifacts.ArtifactSink, "prepare", forbidden)
    before = snapshot(backend)
    refused(code, takeoff_read, backend)
    assert snapshot(backend) == before


@pytest.mark.parametrize("source", [None, {}, {"drawing_id": "solar"},
    dict(takeoff_binding(), drawing_version=True), dict(takeoff_binding(), drawing_version=0),
    dict(takeoff_binding(), graph_sha256="A" * 64), dict(takeoff_binding(), project_id=4),
    dict(takeoff_binding(), extra=1)])
def test_takeoff_source_required(source):
    refused("TAKEOFF_SOURCE_REQUIRED", takeoff_builtin().run, {}, {}, None, source)


def test_takeoff_adapter_hook_opt_in(backend, monkeypatch):
    non_declaring = read._load_builtin("solar-select-by-zone")
    calls = []
    def ordinary_run(graph, params):
        calls.append((graph, params))
        return {"ok": True}
    monkeypatch.setattr(non_declaring, "run", ordinary_run)
    result = read.run_local_graph_read(backend, TENANT, "solar-select-by-zone",
        {"drawing_id": "solar", "zone_name": "Roof"}, drawing_id="solar", source_version=1, job_id=JOB)
    assert result["output"] == {"ok": True} and len(calls) == 1
    assert read._source_argument("solar-select-by-zone", "solar", {}) == {}
    assert not read._reads_source_binding(SimpleNamespace(READS_SOURCE_BINDING=1))
    assert read._reads_source_binding(SimpleNamespace(READS_SOURCE_BINDING=True))
    refused("READ_OUTPUT_INVALID", read._read_output, TAKEOFF, {}, {}, SimpleNamespace(), physical_head=None)


def takeoff_project_wrapper():
    wrapper = takeoff_wrapper()
    ref = {key: "unused" for key in solar_project_artifacts._REF_KEYS}
    ref.update(schema=solar_project_artifacts.REF_SCHEMA, download="/project-artifact",
               source_version_id=str(uuid.UUID("abcdef00-0000-4000-8000-000000000001")),
               artifact_id="a" * 64, content_sha256="c" * 64, byte_length=1)
    wrapper["head"]["state"] = ref
    return wrapper


def test_takeoff_project_ref_head():
    wrapper = takeoff_project_wrapper()
    saved = copy.deepcopy(wrapper)
    result = takeoff_direct(physical=wrapper, source=takeoff_binding(1))
    assert takeoff_builtin().PROJECT_REF_SCHEMA == solar_project_artifacts.REF_SCHEMA
    assert result.summary["head"] == wrapper["head"]
    assert all(line["status"] == "computed" and line["source"]["physical_state"]["state_source_version"] is None
               for line in takeoff_physical_lines(result))
    assert wrapper == saved


@pytest.mark.parametrize("defect", ["frames", "rows", "settings", "pads", "pad", "vertices",
    "point", "coordinate", "cut-fill", "cut", "fill", "missing-cut-fill"])
def test_takeoff_malformed_ground_document(defect, backend, monkeypatch):
    wrapper = takeoff_wrapper()
    state = wrapper["document"]["state"]
    pad = state["grade_pads"][0]
    if defect in ("frames", "settings", "pads"):
        state[{"frames": "frames", "settings": "settings", "pads": "grade_pads"}[defect]] = None
    elif defect == "rows":
        state["tracker_rows"] = {}
    elif defect == "pad":
        state["grade_pads"] = [None]
    elif defect == "vertices":
        pad["vertices"] = [[0, 0], [1, 1]]
    elif defect == "point":
        pad["vertices"][0] = [0]
    elif defect == "coordinate":
        pad["vertices"][0][0] = True
    elif defect == "cut-fill":
        pad["cut_fill"] = []
    elif defect == "missing-cut-fill":
        del pad["cut_fill"]
    else:
        pad["cut_fill"][defect + "_m3"] = float("nan") if defect == "cut" else True
    before = snapshot(backend)
    monkeypatch.setattr(read, "_current_physical_head", lambda *a, **k: wrapper)
    monkeypatch.setattr(artifacts.ArtifactSink, "prepare", forbidden)
    refused("TAKEOFF_PHYSICAL_INPUT_INVALID", takeoff_read, backend)
    assert snapshot(backend) == before


def test_takeoff_round_half_even_after_summing():
    graph = {"routes": [{"route_kind": "feeder", "wire_gauge": "", "length_ft": n}
                        for n in (0.0005, 0.0005)]}
    state = takeoff_state()
    state["grade_pads"] = [takeoff_pad(0, 0.0005, 0.0015), takeoff_pad(2, 0.0005, 0)]
    lines = takeoff_lines(takeoff_direct(graph, physical=takeoff_wrapper(state)))
    assert lines["feeders.length.unspecified"]["quantity"] == "0.001"
    assert lines["earthwork.cut"]["quantity"] == "0.001"
    assert lines["earthwork.fill"]["quantity"] == "0.002"
    assert lines["earthwork.net"]["quantity"] == "-0.001"


def test_takeoff_source_argument_is_copied_and_proof_rederives_it(backend, monkeypatch):
    module = takeoff_builtin()
    original = module.run
    context = read.resolve_graph_context(backend, TENANT, "solar", 1)
    supplied = read._source_argument(TAKEOFF, "solar", context)["source_binding"]
    saved = copy.deepcopy(supplied)
    def mutate(graph, params, physical_head=None, source_binding=None):
        value = original(graph, params, physical_head, source_binding)
        source_binding["drawing_version"] = 100
        return value
    monkeypatch.setattr(module, "run", mutate)
    sink = artifacts.ArtifactSink(backend, TENANT, "solar", context, TAKEOFF,
                                  read.request_digest(TAKEOFF, "solar", 1, {}), False)
    read._read_output(TAKEOFF, context["graph"], {}, sink,
                       physical_head=None, source_binding=supplied)
    assert supplied == saved
    result = takeoff_read(backend)
    assert takeoff_proof(backend, result)
    forged = copy.deepcopy(result)
    forged["output"]["summary"]["source"]["drawing_version"] = 100
    forged.update(output_sha256=digest(forged["output"]), output_bytes=len(canonical_bytes(forged["output"])))
    with pytest.raises(ValueError, match="^graph read terminal proof rejected$"):
        takeoff_proof(backend, forged)
