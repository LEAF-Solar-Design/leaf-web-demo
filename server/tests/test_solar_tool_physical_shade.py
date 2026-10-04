"""Frozen SHD 01–22 contract for the read-only CPU terrain shade report."""
import copy
import json
import math
import sys
from pathlib import Path

import pytest
from jsonschema import Draft7Validator

SERVER = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SERVER))
sys.path.insert(0, str(SERVER / "tests"))

import catalog
import deps
import solar_ground_shade as shade
import solar_ground_terrain as terrain
import solar_local_read as read
import solar_physical_analysis as analysis
import solar_physical_head as ph
import solar_physical_state as ps
import solar_tools
import store
from leaf_cloud_client import canonical_bytes
from solar_design_graph import GraphValidationError
from solar_sizing_client import digest
from test_solar_local_read import TENANT, JOB, backend, _publish_head, forbidden  # noqa: F401
from test_w1_design_graph import graph  # noqa: F401
from test_w1_local_graph_jobs import isolated_jobs, no_network  # noqa: F401

TOOL = "solar-physical-shade"


def builtin():
    return read._load_builtin(TOOL)


def field():
    frames = []
    for j in range(3):
        for i in range(5):
            x, y = 10.0 + 6.0 * i, 20.0 + 8.0 * j
            frames.append({"type": "LWPOLYLINE", "layer": "LEAF-TRACKERS", "closed": True,
                           "elevation": 0.0,
                           "vertices": [(x, y), (x + 2, y), (x + 2, y + 4), (x, y + 4)]})
    return {"frames": frames, "grid": {"rows": 21, "cols": 21, "x_min": 0.0, "x_max": 100.0,
                                      "y_min": 0.0, "y_max": 100.0, "elevations": [0.0] * 441}}


def document(state=None, unit="m"):
    return ps.physical_document(field() if state is None else state, drawing_units=unit,
                                source_sha256="a" * 64, capability="frame-generate")


def handed(doc=None, head=None):
    return {"head": {"index": 0} if head is None else head,
            "document": document() if doc is None else doc}


def report(doc=None, head=None):
    return builtin().run({}, {}, handed(doc, head))


def run_read(backend, params=None):
    return read.run_local_graph_read(backend, TENANT, TOOL,
                                     {"drawing_id": "solar"} if params is None else params,
                                     drawing_id="solar", source_version=1, job_id=JOB)


def proof(result, backend):
    return read.graph_read_provenance(result, {"drawing_id": "solar"}, TENANT, JOB, TOOL, 1,
                                      backend=backend)


def refused(code, fn, *args, **kwargs):
    with pytest.raises(GraphValidationError) as exc:
        fn(*args, **kwargs)
    assert exc.value.code == code
    assert str(exc.value) == f"{code}: <root>"


def flat(output, count=15):
    assert set(output) == {"schema", "maturity", "scope", "head", "units", "settings",
                           "sample_count", "mean_shade", "datum_shift_m", "profile", "surface",
                           "frames", "frames_omitted"}
    assert output["schema"] == "leaf.solar-physical-shade.v1"
    assert output["maturity"] == "preview"
    assert output["scope"] == "cpu-terrain-native-frame-centres"
    assert output["sample_count"] == count and output["mean_shade"] == 0.0
    assert output["datum_shift_m"] == pytest.approx(1.5)
    assert output["surface"] == {"rows": 21, "cols": 21, "cells": 441}
    assert output["profile"] == {"name": "full", "angle_count": 468, "ray_step_m": 1.0,
                                 "max_ray_m": 400.0, "estimated_samples": count * 468 * 400}
    assert output["frames"] == [{"sample_index": i, "frame_index": i, "shade": 0.0}
                                for i in range(min(count, 200))]
    assert output["frames_omitted"] == max(0, count - 200)


def controlled(n):
    return {"succeeded": True, "panels": [{"entity": i} for i in range(n)],
            "result": {"weighted_per_panel": [0.0] * n}, "mean_shade": 0.0,
            "binding": {"shift_m": 1.5}, "profile": shade.select_profile(n),
            "surface": {"rows": 21, "cols": 21, "cells": 441}}


def test_shd_01_declaration_and_discovery(backend, monkeypatch):
    """SHD 01 declaration and discovery."""
    declared = json.loads((SERVER / "solar_tools" / "solar_physical_shade.json").read_text())
    template = json.loads((SERVER / "solar_tools" / "solar_terrain_read.json").read_text())
    template.update(name=TOOL, builtin="builtins/solar_physical_shade.py",
                    invalid_request_code="INVALID_PHYSICAL_SHADE_REQUEST", ledger=["shade-sim"],
                    order=50, scenario="w4-physical-shade")
    template["record"].update(name=TOOL, engine_op="solar_physical_shade",
                                entry="builtins/solar_physical_shade.py", description=(
        "Report CPU terrain shade at native frame centres in the current physical head. "
        "Uses default clearance and an automatic sample profile. Reads only. "
        "Excludes weather weighting and individual-module shading."))
    assert declared == template
    assert builtin().READS_PHYSICAL_HEAD is True
    assert TOOL in solar_tools.local_graph_read_tools()
    assert len([e for e in solar_tools.entries() if e["name"] == TOOL]) == 1
    monkeypatch.setattr(deps, "tenant_repo_dir", lambda tenant: None)
    monkeypatch.setattr(deps, "load_tenant_repo_tools", lambda tenant: [])
    monkeypatch.setattr(deps, "_AUTHORED", [])
    found = [(f, c) for f in catalog.build_catalog(deps.all_tools(TENANT))
             for c in f["capabilities"] if c["name"] == TOOL]
    assert len(found) == 1 and found[0][0]["family_id"] == "terrain"
    validator = Draft7Validator(declared["record"]["params"])
    assert validator.is_valid({}) and validator.is_valid({"drawing_id": "solar"})
    _publish_head(backend, field())
    assert run_read(backend, {})["output"]["sample_count"] == 15
    assert run_read(backend)["output"]["sample_count"] == 15


def test_shd_02_flat_native_frames(backend):
    """SHD 02 flat native frames."""
    head = _publish_head(backend, field())
    output = run_read(backend)["output"]
    flat(output)
    assert output["head"] == head and output["datum_shift_m"] == 1.5
    assert output["units"] == {"drawing_units": "m", "meters_per_unit": 1.0}


def test_shd_03_feet_hydration():
    """SHD 03 feet hydration."""
    state = field()
    for frame in state["frames"]:
        frame["vertices"] = [(x / 0.3048, y / 0.3048) for x, y in frame["vertices"]]
    for key in ("x_min", "x_max", "y_min", "y_max"):
        state["grid"][key] /= 0.3048
    output = report(document(state, "ft"))
    flat(output)
    assert output["units"] == {"drawing_units": "ft", "meters_per_unit": 0.3048}


def test_shd_04_default_settings():
    """SHD 04 default settings."""
    state = field()
    state["settings"] = {"TorqueTubeHeightM": 2.0}
    doc = document(state)
    snapshot = copy.deepcopy(doc)
    output = report(doc)
    flat(output)
    assert output["settings"] == {"mode": "defaults", "target_clearance_m": 1.5,
                                   "profile_selection": "automatic"}
    assert doc == snapshot


def test_shd_05_recorded_head_replay(backend, monkeypatch):
    """SHD 05 recorded-head replay."""
    first_head = _publish_head(backend, field())
    first = run_read(backend)
    state = field()
    state["frames"] = state["frames"][:1]
    second_head = _publish_head(backend, state, parent=first_head["state"]["artifact_id"])
    second = run_read(backend)
    assert (first["output"]["sample_count"], second["output"]["sample_count"]) == (15, 1)
    assert first["output"]["head"] == first_head and second["output"]["head"] == second_head
    monkeypatch.setattr(ph, "load_physical_head", forbidden)
    for result in (first, second):
        assert proof(result, backend)["output_sha256"] == result["output_sha256"]


def test_shd_06_forged_replay(backend):
    """SHD 06 forged replay."""
    _publish_head(backend, field())
    original = run_read(backend)
    for case in ("head", "mean"):
        forged = copy.deepcopy(original)
        if case == "head":
            forged["output"]["head"]["index"] += 1
        else:
            forged["output"]["mean_shade"] = 0.5
        forged.update(output_sha256=digest(forged["output"]),
                      output_bytes=len(canonical_bytes(forged["output"])))
        with pytest.raises(ValueError, match="^graph read terminal proof rejected$"):
            proof(forged, backend)


def test_shd_07_manual_rows_only():
    """SHD 07 manual rows only."""
    state = field()
    state.update(frames=[], tracker_rows=[{"row": 0}])
    refused("PHYSICAL_SHADE_MANUAL_ROWS_UNSUPPORTED", report, document(state))


def test_shd_08_no_frames():
    """SHD 08 no frames."""
    for absent in (True, False):
        state = field()
        if absent:
            del state["frames"]
        else:
            state.update(frames=[], tracker_rows=[])
        refused("PHYSICAL_SHADE_NATIVE_FRAMES_REQUIRED", report, document(state))


def test_shd_09_native_filtering():
    """SHD 09 native filtering."""
    state = field()
    native = state["frames"][0]
    excluded = [dict(native, closed=False), dict(native, layer="OTHER"), {"type": "INSERT"}]
    state["frames"] = excluded + [native]
    output = report(document(state))
    assert output["sample_count"] == 1
    assert output["frames"] == [{"sample_index": 0, "frame_index": 3, "shade": 0.0}]
    state["frames"] = excluded
    refused("PHYSICAL_SHADE_NATIVE_FRAMES_REQUIRED", report, document(state))


def test_shd_10_missing_grid():
    """SHD 10 missing grid."""
    for absent in (True, False):
        doc = document()
        if absent:
            del doc["state"]["grid"]
        else:
            doc["state"]["grid"] = None
        refused("PHYSICAL_SHADE_GRID_MISSING", report, doc)


def test_shd_11_no_head(backend):
    """SHD 11 no head."""
    refused("PHYSICAL_SHADE_HEAD_REQUIRED", run_read, backend)
    refused("PHYSICAL_SHADE_HEAD_REQUIRED", builtin().run, {}, {})


def test_shd_12_unsupported_units():
    """SHD 12 unsupported units."""
    for unit, mpu in (("cm", 0.01), ("m", 0.0), ("ft", 1.0), ("m", True)):
        doc = document()
        doc["units"] = {"drawing_units": unit, "meters_per_unit": mpu}
        refused("PHYSICAL_STATE_UNITS_UNSUPPORTED", analysis.hydrate_physical_document, doc)


def test_shd_13_malformed_input():
    """SHD 13 malformed input."""
    for wrapper in ([], {}, {"head": [], "document": document()},
                    {"head": {}, "document": []}):
        refused("PHYSICAL_SHADE_INPUT_INVALID", builtin().run, {}, {}, wrapper)
    for key in ("state", "units", "frame"):
        doc = document()
        doc[key] = []
        refused("PHYSICAL_SHADE_INPUT_INVALID", report, doc)
    for defect in (None, [None, (1, 2), (3, 4)], [(True, 0), (1, 2), (3, 4)]):
        doc = document()
        doc["state"]["frames"][0]["vertices"] = defect
        refused("PHYSICAL_SHADE_FRAMES_INVALID", report, doc)
    for frames in (None, {}, [None]):
        doc = document()
        doc["state"]["frames"] = frames
        refused("PHYSICAL_SHADE_FRAMES_INVALID", report, doc)
    doc = document()
    doc["state"]["tracker_rows"] = {}
    refused("PHYSICAL_SHADE_INPUT_INVALID", report, doc)


def test_shd_14_invalid_terrain(monkeypatch):
    """SHD 14 invalid terrain."""
    for change in ({"elevations": [0.0]}, {"rows": 1, "cols": 1, "elevations": [0.0]}):
        doc = document()
        doc["state"]["grid"].update(change)
        refused("PHYSICAL_SHADE_GRID_INVALID", report, doc)
    doc = document()
    doc["frame"]["transform"][0] = 2
    refused("PHYSICAL_SHADE_FRAME_UNSUPPORTED", report, doc)
    doc = document()
    doc["state"]["grid"]["frame"] = {}
    refused("PHYSICAL_SHADE_FRAME_UNSUPPORTED", report, doc)
    monkeypatch.setattr(terrain, "terrain_interpolator", lambda *a: None)
    refused("PHYSICAL_SHADE_GRID_INVALID", report)


def test_shd_15_input_limits(monkeypatch):
    """SHD 15 input limits."""
    monkeypatch.setattr(shade, "shade_sim", forbidden)
    doc = document()
    doc["state"]["frames"] = [doc["state"]["frames"][0]] * 20_001
    refused("PHYSICAL_SHADE_INPUT_LIMIT_EXCEEDED", report, doc)
    doc = document()
    doc["state"]["grid"].update(rows=301, cols=300, elevations=[0.0] * 90_300)
    refused("PHYSICAL_SHADE_INPUT_LIMIT_EXCEEDED", report, doc)


def test_shd_16_closed_request():
    """SHD 16 closed request."""
    validator = Draft7Validator(solar_tools.trusted_record(TOOL)["params"])
    for key in ("settings", "profile", "head", "expected_head", "expected_rev", "units",
                "geometry", "export"):
        params = {key: {}}
        assert not validator.is_valid(params)
        refused("INVALID_PHYSICAL_SHADE_REQUEST", builtin().run, {}, params, handed())
    for params in (None, [], "", 0):
        refused("INVALID_PHYSICAL_SHADE_REQUEST", builtin().run, {}, params, handed())


def test_shd_17_kernel_failures(monkeypatch):
    """SHD 17 kernel failures."""
    def broken(*args, **kwargs):
        raise RuntimeError("private path and arbitrary kernel text")
    monkeypatch.setattr(shade, "shade_sim", broken)
    refused("PHYSICAL_SHADE_KERNEL_FAILED", report)
    for change in ("unsuccessful", "length", "nan", "index", "profile", "surface", "shift"):
        sim = controlled(15)
        if change == "unsuccessful":
            sim["succeeded"] = False
        elif change == "length":
            sim["result"]["weighted_per_panel"].pop()
        elif change == "nan":
            sim["mean_shade"] = float("nan")
        elif change == "index":
            sim["panels"][0]["entity"] = True
        elif change == "profile":
            sim["profile"]["ray_step_m"] = float("inf")
        elif change == "surface":
            sim["surface"]["cells"] = 1
        else:
            sim["binding"]["shift_m"] = float("nan")
        monkeypatch.setattr(shade, "shade_sim", lambda *a, **k: sim)
        refused("PHYSICAL_SHADE_KERNEL_FAILED", report)
    # A container of the wrong type is refused even when its keys pass the value checks.
    for weighted in ({0: "PRIVATE_KERNEL_PAYLOAD"}, {0: float("nan")}, (0.0,)):
        sim = controlled(1)
        sim["result"]["weighted_per_panel"] = weighted
        monkeypatch.setattr(shade, "shade_sim", lambda *a, **k: sim)
        refused("PHYSICAL_SHADE_KERNEL_FAILED", report)
    sim = controlled(1)
    sim["panels"] = ({"entity": 0},)
    monkeypatch.setattr(shade, "shade_sim", lambda *a, **k: sim)
    refused("PHYSICAL_SHADE_KERNEL_FAILED", report)


def test_shd_18_input_isolation(backend, graph, monkeypatch):
    """SHD 18 input isolation."""
    state = field()
    state.update(shade_heatmap=[{"old": True}], shade_files={"old": "bytes"})
    head = _publish_head(backend, state)
    stored_head, doc = ph.load_physical_head(backend, TENANT, "solar", project_id=graph["project"]["id"])
    wrapper = handed(doc, stored_head)
    snapshot = copy.deepcopy(wrapper)
    graph_snapshot = copy.deepcopy(graph)
    params = {}
    builtin().run(graph, params, wrapper)
    assert wrapper == snapshot and graph == graph_snapshot and params == {}
    object_keys = set(backend.drawing_object_keys(TENANT, "solar"))
    manifest = copy.deepcopy(store.load_manifest(backend, TENANT, "solar"))
    stored_bytes = ps.encode_document(doc)
    monkeypatch.setattr(backend, "put", forbidden)
    request = {"drawing_id": "solar"}
    result = run_read(backend, request)
    proof(result, backend)
    assert request == {"drawing_id": "solar"} and result["drawing_changed"] is False
    assert set(backend.drawing_object_keys(TENANT, "solar")) == object_keys
    assert store.load_manifest(backend, TENANT, "solar") == manifest
    after_head, after_doc = ph.load_physical_head(backend, TENANT, "solar",
                                               project_id=graph["project"]["id"])
    assert after_head == head and ps.encode_document(after_doc) == stored_bytes


def test_shd_19_deterministic_report(backend):
    """SHD 19 deterministic report."""
    _publish_head(backend, field())
    first, second = run_read(backend), run_read(backend)
    assert canonical_bytes(first["output"]) == canonical_bytes(second["output"])
    assert first["output_sha256"] == second["output_sha256"]
    assert proof(first, backend) == proof(second, backend)


def test_shd_20_bounded_frame_table():
    """SHD 20 bounded frame table."""
    state = field()
    state["frames"] = [state["frames"][0]] * 201
    output = report(document(state))
    flat(output, 201)
    assert not {"result", "files", "markers", "panels", "message", "cleared"} & set(output)


def test_shd_21_output_limits(monkeypatch):
    """SHD 21 output limits."""
    refused("PHYSICAL_SHADE_OUTPUT_LIMIT_EXCEEDED", report, None, {"large": "x" * 8_193})
    output = report(head={"index": 0, "unicode": "é" * 500})
    assert len(canonical_bytes(output)) <= 65_536 < read.MAX_OUTPUT_BYTES
    summary_size = len(canonical_bytes(dict(output, frames=[], frames_omitted=15)))
    monkeypatch.setattr(builtin(), "MAX_OUTPUT_BYTES", summary_size + 100)
    bounded = report(head={"index": 0, "unicode": "é" * 500})
    assert len(canonical_bytes(bounded)) <= summary_size + 100
    assert 0 < len(bounded["frames"]) < 15
    assert bounded["frames"] == output["frames"][:len(bounded["frames"])]
    assert bounded["frames_omitted"] == 15 - len(bounded["frames"])
    monkeypatch.setattr(builtin(), "MAX_OUTPUT_BYTES", summary_size - 1)
    refused("PHYSICAL_SHADE_OUTPUT_LIMIT_EXCEEDED", report, None,
            {"index": 0, "unicode": "é" * 500})


def test_shd_22_profile_thresholds(monkeypatch):
    """SHD 22 profile thresholds."""
    rows = [(300, "full", 468, 1.0, 56_160_000),
            (301, "balanced", 216, 3.0, 8_712_144),
            (1500, "balanced", 216, 3.0, 43_416_000),
            (1501, "large-site", 108, 5.0, 12_968_640),
            (5000, "large-site", 108, 5.0, 43_200_000),
            (5001, "very-large-site", 48, 10.0, 9_601_920)]
    for n, name, angles, step, estimated in rows:
        doc = document()
        doc["state"]["frames"] = [doc["state"]["frames"][0]] * n
        sim = controlled(n)
        monkeypatch.setattr(shade, "shade_sim", lambda *a, **k: sim)
        output = report(doc)
        assert output["sample_count"] == n
        assert output["profile"] == {"name": name, "angle_count": angles, "ray_step_m": step,
                                     "max_ray_m": 400.0, "estimated_samples": estimated}
        assert estimated == n * angles * math.ceil(400.0 / step)
