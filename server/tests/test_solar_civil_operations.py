"""Civil requests using the native engine's real physical-state fixtures."""
import copy
import sys
from pathlib import Path

import pytest

SERVER = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SERVER))
sys.path.insert(0, str(SERVER / "tests"))

import solar_civil_operations as civil
import solar_frames_piles as fp
import solar_physical_head as ph
import solar_physical_state as ps
from test_solar_frames_piles import (
    backend, graph, terrain_chain, GEN, PRESET, TEMPLATE, SQUARE_M, SQUARE_FT, OVERHANG_M,
    MEASURED_LX_M_GENERATE, MEASURED_LX_M_PILES, MEASURED_LX_OVERHANG,
    landxml_head, seed_head, state_of, head_id, narrow, TINY, TERRAIN_SHA)
from test_solar_ground_buildout import chain
from test_solar_physical_state import DRAWING, PROJECT, TENANT


def body(operation=fp.GENERATE, base=None, **changes):
    out = {"operation": operation, "expected_head": base}
    if operation == fp.GENERATE:
        out.update(boundary=GEN["boundary"], preset=PRESET, drawing_units="m")
    elif operation == fp.PILING:
        out.update(preset=PRESET, pile_template=TEMPLATE)
    elif operation == fp.RANGE:
        out["preset"] = PRESET
    elif operation == civil.GRADE:
        out["boundary"] = SQUARE_M
    out.update(changes)
    return out


def call(backend, request):
    return civil.operate(backend, TENANT, DRAWING, request, project_id=PROJECT)


def snapshot(backend):
    return {key: backend.get(key) for key in backend.drawing_object_keys(TENANT, DRAWING)}


def refuse(backend, request, code, tenant=TENANT):
    before = snapshot(backend)
    with pytest.raises(ps.PhysicalStateError) as exc:
        civil.operate(backend, tenant, DRAWING, request, project_id=PROJECT)
    assert exc.value.code == code
    assert snapshot(backend) == before


def test_civ_01_first_frame_generation(backend):
    result = call(backend, body())
    assert result["summary"] == {"frames_added": 144, "frames_off_terrain": 0, "preset_name": "TinyTest"}
    assert state_of(backend)["next_handle"] == 145
    assert result["schema"] == fp.RESULT_SCHEMA and result["outcome"] == "published"


def test_civ_02_landxml_metre_frames(backend):
    result = call(backend, body(base=landxml_head(backend), boundary=SQUARE_M))
    assert result["summary"] == MEASURED_LX_M_GENERATE
    assert result["standing"]["frames"] == {"state": "current", "checked": 242, "stale": 0}


def test_civ_03_landxml_metre_piles(backend):
    call(backend, body(base=landxml_head(backend), boundary=SQUARE_M))
    result = call(backend, body(fp.PILING, head_id(backend)))
    assert result["summary"] == MEASURED_LX_M_PILES
    assert result["preview"]["piles"] == 1936 and result["terrain"]["sampled"] is True


def test_civ_04_feet_equivalent(backend):
    made = call(backend, body(base=landxml_head(backend, "ft"), boundary=SQUARE_FT, drawing_units="ft"))
    assert made["summary"] == MEASURED_LX_M_GENERATE
    result = call(backend, body(fp.PILING, head_id(backend)))
    assert result["summary"] == MEASURED_LX_M_PILES
    assert result["units"] == {"drawing_units": "ft", "meters_per_unit": 0.3048}
    for pile in state_of(backend)["piles"]:
        assert pile["pile"]["depth_m"] == 2.0
        assert pile["thickness"] == pytest.approx(2.0 / 0.3048, abs=1e-12)


def test_civ_05_collision_unchanged(backend):
    made = call(backend, body())
    before = snapshot(backend)
    result = call(backend, body(fp.COLLISION, head_id(backend)))
    assert result["summary"] == {"frames_checked": 144, "collisions": 0}
    assert result["outcome"] == "unchanged" and result["head"] == made["head"]
    assert snapshot(backend) == before


def test_civ_06_duplicate_layout_collision(backend):
    call(backend, body())
    call(backend, body(base=head_id(backend)))
    result = call(backend, body(fp.COLLISION, head_id(backend)))
    assert result["summary"] == {"frames_checked": 288, "collisions": 144}
    assert state_of(backend)["next_handle"] == 289


def test_civ_07_range_markers(backend, terrain_chain):
    base = seed_head(backend, copy.deepcopy(terrain_chain["t4"]))
    result = call(backend, body(fp.RANGE, base, preset=narrow(MaxPileLengthM=1.5)))
    assert result["summary"] == {"total_piles": 9576, "out_of_range": 9576, "min_m": 1.0, "max_m": 1.5}
    assert result["preview"]["range_markers"] == 9576


def test_civ_08_off_terrain_refusal(backend):
    result = call(backend, body(base=landxml_head(backend), boundary=OVERHANG_M))
    assert result["summary"] == MEASURED_LX_OVERHANG
    refuse(backend, body(fp.PILING, head_id(backend)), "FRAMES_PILES_OFF_TERRAIN")


def test_civ_09_captured_grade(backend, chain):
    base = seed_head(backend, copy.deepcopy(chain["state"]))
    original = state_of(backend)
    result = call(backend, body(civil.GRADE, base, boundary=chain["intake"]["boundary"]))
    summary = result["summary"]
    assert summary["elevation_m"] == pytest.approx(0.26064200685635874, abs=1e-12)
    assert summary["label"] == "PAD 1\\P0.26 m"
    assert round(summary["total_cut_m3"], 1) == 8938.1
    assert round(summary["total_fill_m3"], 1) == 8972.7
    assert set(summary) == {"pads_added", "grade_pads", "mode", "elevation_m", "label",
                            "total_cut_m3", "total_fill_m3", "net_m3"}
    assert summary["pads_added"] == 1 and result["terrain"]["sampled"] is True
    assert result["schema"] == civil.RESULT_SCHEMA
    after = state_of(backend)
    assert len(after["grade_pads"]) == len(original.get("grade_pads", [])) + 1
    assert {k: v for k, v in after.items() if k not in ("grade_pads", "grading_settings")} == {
        k: v for k, v in original.items() if k not in ("grade_pads", "grading_settings")}


def test_civ_10_grade_transaction(backend):
    base = landxml_head(backend)
    request = body(civil.GRADE, base, boundary=SQUARE_M)
    first = call(backend, request)
    before = snapshot(backend)
    retry = call(backend, dict(request, mode="Auto", value_du=None))
    assert retry["outcome"] == "retry" and retry["summary"] is None and retry["created"] is False
    assert retry["head"] == first["head"] and snapshot(backend) == before
    refuse(backend, dict(request, mode="Manual", value_du=1), "FRAMES_PILES_STALE_BASE")


def test_civ_19_closed_envelopes(backend):
    for operation in civil.OPERATIONS:
        request = body(operation)
        for key in ("extra", "base", "expected_rev", "project_id", "limits"):
            refuse(backend, dict(request, **{key: None}), "TERRAIN_BODY_INVALID")
        for key in set(request) - {"expected_head"}:
            broken = dict(request)
            del broken[key]
            refuse(backend, broken, "TERRAIN_OPERATION_INVALID" if key == "operation" else "TERRAIN_BODY_INVALID")
        missing = dict(request)
        del missing["expected_head"]
        refuse(backend, missing, "TERRAIN_EXPECTED_HEAD_INVALID")


def test_civ_20_types_and_field_bounds(backend):
    cases = [
        (body(boundary=[[True, 0], [1, 0], [1, 1]]), "FRAMES_PILES_BOUNDARY_INVALID"),
        (body(boundary=[[0, 0]] * 20_001), "FRAMES_PILES_BOUNDARY_INVALID"),
        (body(boundary=[[1e9 + 1, 0], [1, 0], [1, 1]]), "FRAMES_PILES_BOUNDARY_INVALID"),
        (body(preset=dict(PRESET, Name="x" * 257)), "FRAMES_PILES_PRESET_INVALID"),
        (body(preset=[]), "FRAMES_PILES_PRESET_INVALID"),
        (body(fp.PILING, pile_template=[]), "FRAMES_PILES_PILE_TEMPLATE_INVALID"),
        (body(fp.PILING, pile_template={"Name": "x" * 257}), "FRAMES_PILES_PILE_TEMPLATE_INVALID"),
        (body(drawing_units="in"), "FRAMES_PILES_DRAWING_UNITS_INVALID"),
    ]
    for request, code in cases:
        refuse(backend, request, code)
    for head in (True, 1, "A" * 64, "a" * 63, "bad"):
        refuse(backend, body(base=head), "TERRAIN_EXPECTED_HEAD_INVALID")
    for changes in ({"mode": None}, {"mode": "auto"}, {"value_du": 1},
                    {"mode": "Manual", "value_du": True}, {"mode": "Clearance", "value_du": -1},
                    {"mode": "Manual", "value_du": float("inf")},
                    {"mode": "Manual", "value_du": 1e9 + 1}):
        refuse(backend, body(civil.GRADE, **changes), "CIVIL_GRADE_INPUT_INVALID")


def test_civ_24_preconditions(backend, monkeypatch):
    for operation in (fp.COLLISION, fp.PILING, fp.RANGE, civil.GRADE):
        refuse(backend, body(operation), "FRAMES_PILES_STATE_REQUIRED")
    refuse(backend, body(), "FRAMES_PILES_DRAWING_NOT_FOUND", tenant="foreign")
    base = landxml_head(backend)
    refuse(backend, body(fp.COLLISION, base), "FRAMES_PILES_NO_FRAMES")
    refuse(backend, body(fp.PILING, base), "FRAMES_PILES_NO_FRAMES")
    refuse(backend, body(fp.RANGE, base), "FRAMES_PILES_NO_PILES")
    refuse(backend, body(base=base, drawing_units="ft"), "FRAMES_PILES_UNITS_MISMATCH")
    call(backend, body(base=base, boundary=SQUARE_M))
    state = state_of(backend)
    state["road_lines"] = [[[0, 0], [1, 1]]]
    child = ps.physical_document(state, drawing_units="m", source_sha256=TERRAIN_SHA,
                                 capability="road-add", parent=head_id(backend))
    ph.publish_physical_state(backend, TENANT, DRAWING, child)
    base = head_id(backend)
    refuse(backend, body(base=base), "FRAMES_PILES_SITE_CONSTRAINTS_UNSUPPORTED")
    refuse(backend, body(fp.PILING, base), "FRAMES_PILES_SITE_CONSTRAINTS_UNSUPPORTED")
    state = state_of(backend)
    state["road_lines"] = []
    state["frames"].append({"type": "INSERT", "layer": "PVCASE-TRACKERS", "name": "TRK"})
    child = ps.physical_document(state, drawing_units="m", source_sha256=TERRAIN_SHA,
                                 capability="frame-generate", parent=base)
    ph.publish_physical_state(backend, TENANT, DRAWING, child)
    base = head_id(backend)
    refuse(backend, body(fp.PILING, base), "FRAMES_PILES_PVCASE_UNSUPPORTED")
    import write_loop
    monkeypatch.setattr(write_loop, "drawing_mutations_refusal", lambda: "drained")
    refuse(backend, body(base=base), "FRAMES_PILES_WRITES_DRAINED")


def test_civ_26_grade_isolation_and_refusal(backend, monkeypatch):
    call(backend, body())
    refuse(backend, body(civil.GRADE, head_id(backend)), "FRAMES_PILES_TERRAIN_REQUIRED")
    state = state_of(backend)
    # Add a real terrain grid without altering the generated frames.
    state["grid"] = {"rows": 2, "cols": 2, "x_min": -100.0, "x_max": 200.0,
                     "y_min": -100.0, "y_max": 200.0, "elevations": [0.0] * 4}
    state["grading_settings"] = {"retained": "setting"}
    state["grade_pads"] = [{"retained": "pad"}]
    child = ps.physical_document(state, drawing_units="m", source_sha256=TERRAIN_SHA,
                                 capability="terrain-import", parent=head_id(backend))
    ph.publish_physical_state(backend, TENANT, DRAWING, child)
    call(backend, body(fp.PILING, head_id(backend)))
    base = head_id(backend)
    request = body(civil.GRADE, base, mode="Manual", value_du=1)
    for error, code in ((civil.bo.BuildoutBoundsError, "CIVIL_GRADE_LIMIT_EXCEEDED"),
                        (civil.bo.BuildoutInputError, "CIVIL_GRADE_INPUT_INVALID"),
                        (RuntimeError, "CIVIL_GRADE_FAILED")):
        with monkeypatch.context() as patch:
            def fail(*args, **kwargs):
                raise error("payload must not escape")
            patch.setattr(civil.bo, "grade_multi", fail)
            refuse(backend, request, code)
    with monkeypatch.context() as patch:
        patch.setattr(civil.bo, "grade_multi", lambda *a, **k: {"succeeded": True, "pads": []})
        refuse(backend, request, "CIVIL_GRADE_NO_PAD")
    before = state_of(backend)
    expected_settings = civil.bo.grade_multi(
        before["grid"], [request["boundary"]], 1.0, mode=request["mode"],
        value_du=request["value_du"], runtime=civil.bo.RUNTIME_NET8)["settings"]
    call(backend, request)
    after = state_of(backend)
    assert after["frames"] == before["frames"]
    assert after.get("piles") == before.get("piles")
    assert after["grading_settings"] == {"retained": "setting", **expected_settings}
    assert after["grade_pads"][:-1] == before["grade_pads"]
    assert {k: v for k, v in after.items() if k not in ("grade_pads", "grading_settings")} == {
        k: v for k, v in before.items() if k not in ("grade_pads", "grading_settings")}
    after["grid"] = {"rows": 1, "cols": 1, "x_min": 0.0, "x_max": 1.0,
                     "y_min": 0.0, "y_max": 1.0, "elevations": [0.0]}
    invalid = ps.physical_document(after, drawing_units="m", source_sha256=TERRAIN_SHA,
                                   capability="terrain-import", parent=head_id(backend))
    ph.publish_physical_state(backend, TENANT, DRAWING, invalid)
    refuse(backend, body(civil.GRADE, head_id(backend)), "FRAMES_PILES_TERRAIN_INVALID")


def test_civ_28_grade_equivalent_numbers_retry(backend):
    base = landxml_head(backend)
    boundary = [[-0.0, -0.0], [10, -0.0], [10, 10], [-0.0, 10]]
    request = body(civil.GRADE, base, boundary=boundary, mode="Manual", value_du=-0.0)
    first = call(backend, request)
    before = snapshot(backend)
    for value in (0, 0.0):
        retry = call(backend, dict(request, boundary=[[0, 0.0], [10.0, 0], [10.0, 10.0], [0, 10.0]],
                                   value_du=value))
        assert retry["outcome"] == "retry" and retry["created"] is False
        assert retry["head"] == first["head"] and snapshot(backend) == before
    request = body(civil.GRADE, head_id(backend), boundary=boundary, mode="Manual", value_du=5)
    first = call(backend, request)
    before = snapshot(backend)
    retry = call(backend, dict(request, value_du=5.0))
    assert retry["outcome"] == "retry" and retry["created"] is False
    assert retry["head"] == first["head"] and snapshot(backend) == before
