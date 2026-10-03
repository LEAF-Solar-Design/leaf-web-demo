"""Manual row creation, unchanged refusal heads, conversion and immutable history."""
from copy import deepcopy
import hashlib
import inspect
import json
from pathlib import Path
import re
import sys

import pytest

SERVER = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SERVER))
sys.path.insert(0, str(SERVER / "tests"))

import write_loop
import solar_tracker_rows as rows_domain
import solar_physical_head as ph
import solar_physical_state as ps
import solar_ground_conversion as cv
import solar_ground_terrain_adapter as ta
import solar_frames_piles as fp
import solar_local_read as lr
import solar_local_graph as lg
from solar_design_graph import GraphValidationError
from test_w1_design_graph import graph
from test_w1_solve_commit import seed
from test_solar_ground_graph_codec import ground_base
from test_solar_ground_conversion import provenance

TENANT = "fixture-tenant"
DRAWING = "solar"
PROJECT = "leaf:project:00000000-0000-4000-8000-000000000001"
RAW = [
    {"axis_start": [0.0, 0.0], "axis_end": [0.0, 6.0], "cross_axis_width_du": 2.0, "slots": 3},
    {"axis_start": [4.0, 0.0], "axis_end": [4.0, 10.0], "cross_axis_width_du": 1.0, "slots": 2},
]
NATIVE = {
    "grid": {"elevations": [0.0, 0.0, 0.0, 0.0], "rows": 2, "cols": 2,
             "x_min": 0.0, "x_max": 1.0, "y_min": 0.0, "y_max": 1.0},
    "frames": [{"type": "LWPOLYLINE", "layer": "LEAF-TRACKERS",
                "vertices": [[0., 0.], [1., 0.], [1., 1.], [0., 1.]], "elevation": 0.0}],
    "piles": [{"type": "CIRCLE", "layer": "LEAF-PILING", "center": [0.5, 0.5, -1.5],
               "pile": {"embedment_m": 1.5}}],
}


@pytest.fixture
def factory(graph, tmp_path, monkeypatch):
    count = 0

    def make(*, state=None, frame=None, units="m", extra=None, transform=None, ground=True):
        nonlocal count
        g = ground_base(graph)
        g["project"]["units"].update(drawing_units=units,
                                      meters_per_unit={"m": 1.0, "ft": .3048, "in": .0254}[units])
        if not ground:
            g["project"]["installation_design"] = "Roof"
        if extra is not None:
            g["extra"].update(extra)
        if transform is not None:
            g["project"]["units"]["wcs_to_ucs"] = transform
        folder = tmp_path / str(count)
        count += 1
        folder.mkdir()
        backend, _ = seed(folder, monkeypatch, g)
        monkeypatch.setattr(write_loop, "backend_for_tenant", lambda *a, **k: backend)
        if state is not None:
            document = ps.physical_document(state, drawing_units=units, frame=frame,
                                            source_sha256="c" * 64, capability="fixture")
            ph.publish_physical_state(backend, TENANT, DRAWING, document, project_id=PROJECT)
        return backend, g

    return make


def head(backend):
    return ph.physical_head(backend, TENANT, DRAWING, project_id=PROJECT)


def publish(backend, **changes):
    args = {"rows": deepcopy(RAW), "module_power_watts": 450, "expected_head": None,
            "project_id": PROJECT}
    args.update(changes)
    return rows_domain.publish_manual_create(backend, TENANT, DRAWING, **args)


def validate(**changes):
    args = {"rows": deepcopy(RAW), "module_power_watts": 450, "expected_head": None,
            "drawing_units": "m"}
    args.update(changes)
    return rows_domain.validate_manual_create(**args)


def refused(code, fn, *args, **kwargs):
    with pytest.raises(rows_domain.TrackerRowsError) as error:
        fn(*args, **kwargs)
    assert error.value.code == code == str(error.value)
    assert error.value.code in rows_domain.CODES


def guard(factory, code, *, initial=None, **changes):
    backend, _ = factory(**(initial or {}))
    before = head(backend)
    if before is not None and "expected_head" not in changes:
        changes["expected_head"] = before["state"]["artifact_id"]
    refused(code, publish, backend, **changes)
    assert head(backend) == before


def converted(backend, g):
    return cv.convert_current_ground_state(backend, TENANT, DRAWING, g, project_id=PROJECT,
                                           provenance=provenance(), rev=0)


def child(backend, **changes):
    view, document = ph.load_physical_head(backend, TENANT, DRAWING, project_id=PROJECT)
    state = deepcopy(document["state"])
    state.update(changes)
    document = ps.physical_document(state, drawing_units=document["units"]["drawing_units"],
                                    frame=document["frame"], source_sha256="f" * 64,
                                    capability="fixture-child", parent=view["state"]["artifact_id"])
    return ph.publish_physical_state(backend, TENANT, DRAWING, document, project_id=PROJECT)


def test_m01_two_rows_publish_and_convert_to_two_trackers_five_slots(factory):
    backend, g = factory()
    result = publish(backend)
    assert result["outcome"] == "published" and result["created"] is True
    assert result["head"]["index"] == 0
    output = converted(backend, g)
    assert output["counts"] == {"trackers": 2, "slots": 5}
    frames = output["frames"]
    assert [f["insertion_point"] for f in frames] == [[0., 3.], [4., 5.]]
    assert [[f["module_width_along_row"], f["module_height_across_row"]] for f in frames] == [[2., 2.], [5., 1.]]
    assert [f["module_power_watts"] for f in frames] == [450., 450.]
    assert [f["tracker"]["row_index"] for f in frames] == [0, 1]
    assert output["source"]["state_artifact_id"] == result["head"]["state"]["artifact_id"]


def test_m02_feet_rows_convert_to_metre_centres(factory):
    backend, g = factory(units="ft")
    publish(backend)
    output = converted(backend, g)
    assert output["counts"] == {"trackers": 2, "slots": 5}
    assert [f["insertion_point"] for f in output["frames"]] == [[0., .9144000000000001], [1.2192, 1.524]]
    assert [[f["module_width_along_row"], f["module_height_across_row"]] for f in output["frames"]] == [[.6096, .6096], [1.524, .3048]]
    assert [f["module_power_watts"] for f in output["frames"]] == [450., 450.]


def test_m03_empty_rows_and_zero_slots_refused_before_publish(factory):
    backend, g = factory()
    for raw, code in [([], "TRACKER_ROWS_ROWS_INVALID"),
                      ([dict(RAW[0], slots=0)], "TRACKER_ROWS_SLOTS_INVALID")]:
        refused(code, validate, rows=raw)
        refused(code, publish, backend, rows=raw)
        assert head(backend) is None
        document = ps.physical_document({"tracker_rows": raw}, drawing_units="m",
                                        source_sha256="c" * 64, capability="fixture")
        view = {"index": 0, "state": {"artifact_id": "a" * 64, "content_sha256": "b" * 64}}
        with pytest.raises(cv.GroundConversionError) as error:
            cv.convert_physical_state(view, document, g, provenance=provenance(), rev=0)
        assert error.value.code == "GROUND_CONVERSION_TRACKER_ROWS_REQUIRED"


def test_m04_zero_axis_and_zero_width_refused_before_publish(factory):
    backend, g = factory()
    for row, code in [(dict(RAW[0], axis_end=[0., 0.]), "TRACKER_ROWS_AXIS_INVALID"),
                      (dict(RAW[0], cross_axis_width_du=0.), "TRACKER_ROWS_WIDTH_INVALID")]:
        refused(code, validate, rows=[row])
        refused(code, publish, backend, rows=[row])
        assert head(backend) is None
        document = ps.physical_document({"tracker_rows": [row]}, drawing_units="m",
                                        source_sha256="c" * 64, capability="fixture")
        view = {"index": 0, "state": {"artifact_id": "a" * 64, "content_sha256": "b" * 64}}
        with pytest.raises(cv.GroundConversionError) as error:
            cv.convert_physical_state(view, document, g, provenance=provenance(), rev=0)
        assert error.value.code == "GROUND_CONVERSION_INPUT_INVALID"


def test_m05_ten_thousand_slots_convert_and_one_more_is_refused(factory):
    backend, g = factory()
    publish(backend, rows=[dict(RAW[0], slots=10000)])
    output = converted(backend, g)
    assert output["counts"] == {"trackers": 1, "slots": 10000}
    frame = output["frames"][0]
    assert frame["insertion_point"] == [0., 3.]
    assert [frame["module_width_along_row"], frame["module_height_across_row"]] == [.0006, 2.]
    assert frame["module_power_watts"] == 450.
    before = head(backend)
    refused("TRACKER_ROWS_SLOTS_INVALID", publish, backend, rows=[dict(RAW[0], slots=10001)])
    assert head(backend) == before


def test_m06_publish_retry_and_child_publication(factory, monkeypatch):
    backend, _ = factory()
    first = publish(backend)
    with monkeypatch.context() as patch:
        patch.setattr(ph, "publish_physical_state",
                      lambda *a, **k: pytest.fail("exact retry attempted publication"))
        retry = publish(backend)
    later = child(backend, next_handle=2)
    assert [(x["created"], x["head"]["index"]) for x in (first, retry, later)] == [(True, 0), (False, 0), (True, 1)]
    assert later["head"]["parent"] == first["head"]["state"]["artifact_id"]


def test_m07_stale_null_parent_is_refused_and_the_head_kept(factory):
    backend, _ = factory()
    publish(backend)
    child(backend, next_handle=2)
    before = head(backend)
    refused("TRACKER_ROWS_STALE_HEAD", publish, backend)
    assert head(backend) == before


def test_m08_historical_head_and_entry_stay_readable_after_a_child(factory):
    backend, _ = factory()
    first = publish(backend)
    _, document = ph.load_physical_head(backend, TENANT, DRAWING, project_id=PROJECT)
    source = lg.physical_state_source(first["head"])
    child(backend, next_handle=2)
    recorded = lr._recorded_physical_head(backend, TENANT, DRAWING, PROJECT, {"head": first["head"]})
    entry = lg._physical_state_entry(backend, TENANT, DRAWING, PROJECT, source)
    assert recorded == {"head": first["head"], "document": document}
    assert entry["document"] == document
    assert entry["view"]["state"]["artifact_id"] == first["head"]["state"]["artifact_id"]


def test_m09_native_frames_and_piles_keep_their_standing(factory):
    backend, _ = factory(state=NATIVE)
    before, document = ph.load_physical_head(backend, TENANT, DRAWING, project_id=PROJECT)
    standing = fp.terrain_standing(document["state"], 1.)
    result = publish(backend, expected_head=before["state"]["artifact_id"])
    _, after = ph.load_physical_head(backend, TENANT, DRAWING, project_id=PROJECT)
    for key in ("frames", "piles"):
        assert standing[key] == result["terrain_standing"][key] == {"state": "current", "checked": 1, "stale": 0}
    for key in NATIVE:
        assert ps.encode_state(after["state"])[key] == ps.encode_state(document["state"])[key]


def test_m10_raised_terrain_reports_stale_standing(factory):
    native = deepcopy(NATIVE)
    native["grid"]["elevations"] = [1., 1., 1., 1.]
    backend, _ = factory(state=native)
    result = publish(backend, expected_head=head(backend)["state"]["artifact_id"])
    for key in ("frames", "piles"):
        assert result["terrain_standing"][key] == {"state": "stale", "checked": 1, "stale": 1}


def test_g11_existing_empty_tracker_rows_refused(factory):
    guard(factory, "TRACKER_ROWS_ALREADY_EXISTS", initial={"state": {"tracker_rows": []}})


def test_g12_converted_graph_refused(factory):
    guard(factory, "TRACKER_ROWS_GRAPH_CONVERTED", initial={"extra": {"physical_state": None}})


def test_g13_dependent_shade_output_refused(factory):
    guard(factory, "TRACKER_ROWS_DEPENDENT_STATE", initial={"state": {"shade_heatmap": [{}]}})


def test_g14_unsupported_units_refused(factory):
    guard(factory, "TRACKER_ROWS_UNITS_UNSUPPORTED", initial={"units": "in"})


def test_g15_transformed_frame_refused(factory):
    frame = deepcopy(ps.DEFAULT_FRAME)
    frame["transform"][12] = 1
    guard(factory, "TRACKER_ROWS_FRAME_UNSUPPORTED", initial={"state": {"frames": []}, "frame": frame})
    guard(factory, "TRACKER_ROWS_FRAME_UNSUPPORTED", initial={"transform": frame["transform"]})


def test_g16_257_rows_refused(factory):
    guard(factory, "TRACKER_ROWS_LIMIT_EXCEEDED", rows=[RAW[0]] * 257)


def test_g17_request_bytes_over_limit_refused(factory):
    guard(factory, "TRACKER_ROWS_LIMIT_EXCEEDED", request_bytes=262145)


def test_g18_zero_slots_refused(factory):
    guard(factory, "TRACKER_ROWS_SLOTS_INVALID", rows=[dict(RAW[0], slots=0)])


def test_g19_slots_over_row_limit_refused(factory):
    guard(factory, "TRACKER_ROWS_SLOTS_INVALID", rows=[dict(RAW[0], slots=10001)])


def test_g20_total_slots_over_limit_refused(factory):
    guard(factory, "TRACKER_ROWS_LIMIT_EXCEEDED", rows=[dict(RAW[0], slots=10000)] * 10 + [dict(RAW[0], slots=1)])


def test_g21_zero_axis_refused(factory):
    guard(factory, "TRACKER_ROWS_AXIS_INVALID", rows=[dict(RAW[0], axis_end=[0., 0.])])


def test_g22_zero_width_refused(factory):
    guard(factory, "TRACKER_ROWS_WIDTH_INVALID", rows=[dict(RAW[0], cross_axis_width_du=0.)])


def test_g23_negative_width_refused(factory):
    guard(factory, "TRACKER_ROWS_WIDTH_INVALID", rows=[dict(RAW[0], cross_axis_width_du=-2.)])


def test_g24_fractional_slots_refused(factory):
    guard(factory, "TRACKER_ROWS_SLOTS_INVALID", rows=[dict(RAW[0], slots=1.5)])


def test_g25_boolean_slots_refused(factory):
    guard(factory, "TRACKER_ROWS_SLOTS_INVALID", rows=[dict(RAW[0], slots=True)])


def test_g26_empty_rows_refused(factory):
    guard(factory, "TRACKER_ROWS_ROWS_INVALID", rows=[])


def test_g27_zero_power_refused(factory):
    guard(factory, "TRACKER_ROWS_POWER_INVALID", module_power_watts=0.)


def test_g28_request_bytes_at_limit_published(factory):
    backend, _ = factory()
    result = publish(backend, request_bytes=262144)
    assert (result["outcome"], result["created"], result["head"]["index"]) == ("published", True, 0)


def test_g29_one_slot_row_published(factory):
    backend, g = factory()
    result = publish(backend, rows=[dict(RAW[0], slots=1)])
    assert (result["outcome"], result["created"], result["head"]["index"]) == ("published", True, 0)
    assert result["summary"] == {"rows": 1, "slots": 1, "module_power_watts": 450.}
    assert converted(backend, g)["counts"] == {"trackers": 1, "slots": 1}


def test_g30_exact_retry_returns_the_head_unchanged(factory, monkeypatch):
    backend, _ = factory()
    first = publish(backend)
    saved = head(backend)
    with monkeypatch.context() as patch:
        patch.setattr(ph, "publish_physical_state",
                      lambda *a, **k: pytest.fail("exact retry attempted publication"))
        retry = publish(backend, module_power_watts=450., request_bytes=262144)
        # A negative zero is the same coordinate, so it is the same exact retry.
        signed = publish(backend, rows=[dict(RAW[0], axis_start=[-0.0, 0.0]), RAW[1]])
    assert (signed["outcome"], signed["created"], signed["head"]) == ("retry", False, saved)
    assert (first["outcome"], first["created"], first["head"]["index"]) == ("published", True, 0)
    assert (retry["outcome"], retry["created"], retry["head"]["index"]) == ("retry", False, 0)
    assert retry["head"] == saved == head(backend)


def test_g31_changed_power_on_the_old_base_is_stale(factory):
    backend, _ = factory()
    publish(backend)
    before = head(backend)
    refused("TRACKER_ROWS_STALE_HEAD", publish, backend, module_power_watts=451.)
    assert head(backend) == before


def test_g32_changed_power_on_the_current_base_already_exists(factory):
    backend, _ = factory()
    publish(backend)
    before = head(backend)
    refused("TRACKER_ROWS_ALREADY_EXISTS", publish, backend, module_power_watts=451.,
            expected_head=before["state"]["artifact_id"])
    assert head(backend) == before


def _row_from(points):
    return {"axis_start": [points[0], points[1]], "axis_end": [points[2], points[3]],
            "cross_axis_width_du": 2.0, "slots": 1}


def _planted_conversion_code(row, units, g):
    document = ps.physical_document({"tracker_rows": [row]}, drawing_units=units,
                                    source_sha256="c" * 64, capability="fixture")
    view = {"index": 0, "state": {"artifact_id": "a" * 64, "content_sha256": "b" * 64}}
    with pytest.raises(cv.GroundConversionError) as error:
        cv.convert_physical_state(view, document, g, provenance=provenance(), rev=0)
    return error.value.code


def _published_and_converted(factory, units, points):
    backend, g = factory(units=units)
    result = publish(backend, rows=[_row_from(points)])
    assert result["outcome"] == "published" and result["head"]["index"] == 0
    assert converted(backend, g)["counts"] == {"trackers": 1, "slots": 1}


def test_g33_axis_upper_limit_uses_the_conversion_length(factory):
    # math.dist reads this axis as 100000.0 m; conversion's sqrt(dx*dx + dy*dy) reads 100000.00000000001.
    backend, g = factory()
    row = _row_from([0.0, 0.0, 99964.38379571516, 2668.7023706216264])
    refused("TRACKER_ROWS_AXIS_INVALID", validate, rows=[row])
    refused("TRACKER_ROWS_AXIS_INVALID", publish, backend, rows=[row])
    assert head(backend) is None
    assert _planted_conversion_code(row, "m", g) == "GROUND_CONVERSION_INPUT_INVALID"
    # the other side of the same rounding: math.dist reads 100000.00000000001, conversion 100000.0
    _published_and_converted(factory, "m", [-140500.24147980136, 141428.1417138602,
                                            -227963.55517066835, 92949.60256288123])
    # and through the feet scale: 328083.9895013123 ft is within 100 km, math.dist's ...124 is not
    _published_and_converted(factory, "ft", [-26578.94754983927, -14377.471338763717,
                                             -317818.6367451932, -165435.5648143894])


def test_g34_axis_lower_limit_uses_the_conversion_length(factory):
    # math.dist reads this axis as 1.0000000000000003e-09; conversion reads 1e-09, which it refuses.
    backend, g = factory()
    row = _row_from([0.0, 0.0, 9.99684504840722e-10, 2.5117539317397735e-11])
    refused("TRACKER_ROWS_AXIS_INVALID", validate, rows=[row])
    refused("TRACKER_ROWS_AXIS_INVALID", publish, backend, rows=[row])
    assert head(backend) is None
    assert _planted_conversion_code(row, "m", g) == "GROUND_CONVERSION_INPUT_INVALID"
    # the other side: math.dist reads 1e-09 (refused before), conversion reads 1.0000000000000003e-09
    _published_and_converted(factory, "m", [0.0, 0.0, 6.789514057955015e-10, -7.341832118540389e-10])


def test_contract_constants_signatures_and_vocabulary():
    assert (rows_domain.OPERATION, rows_domain.RESULT_SCHEMA) == ("manual-create", "leaf.solar-tracker-rows.v1")
    assert (rows_domain.MAX_ROWS, rows_domain.MAX_REQUEST_BYTES, rows_domain.MAX_ROW_SLOTS,
            rows_domain.MAX_TOTAL_SLOTS) == (256, 262144, 10000, 100000)
    assert issubclass(rows_domain.TrackerRowsError, ps.PhysicalStateError)
    expected = {
        "OPERATION_UNSUPPORTED", "REQUEST_INVALID", "ROWS_INVALID", "ROW_INVALID", "POWER_INVALID",
        "HEAD_INVALID", "UNITS_UNSUPPORTED", "UNITS_MISMATCH", "FRAME_UNSUPPORTED", "SLOTS_INVALID",
        "AXIS_INVALID", "WIDTH_INVALID", "LIMIT_EXCEEDED", "PROJECT_ID_INVALID", "PROJECT_MISMATCH",
        "DRAWING_NOT_FOUND", "GRAPH_REQUIRED", "GROUND_REQUIRED", "GRAPH_CONVERTED", "DEPENDENT_STATE",
        "ALREADY_EXISTS", "STALE_HEAD", "STATE_INVALID", "WRITES_DRAINED", "STORE_UNAVAILABLE",
        "STORE_UNSAFE", "LOG_FULL", "CHECKOUT_REQUIRED", "CHECKOUT_UNAVAILABLE", "CONTENT_TYPE_UNSUPPORTED",
    }
    assert rows_domain.CODES == frozenset("TRACKER_ROWS_" + code for code in expected)
    source = Path(rows_domain.__file__).read_text(encoding="utf-8")
    assert set(re.findall(r"\bTRACKER_ROWS_[A-Z_]+\b", source)) == rows_domain.CODES
    assert rows_domain.INDEPENDENT_STATE_KEYS == frozenset({
        "grid", "frames", "piles", "settings", "restriction_outlines", "road_lines", "setback_rings",
        "vegetation_outlines", "grading_settings", "export_settings", "pile_store_text", "next_handle",
        "mesh_faces", "terrain_face_colors", "collision_layer",
    })
    for fn, names in [(rows_domain.validate_manual_create,
                       ["rows", "module_power_watts", "expected_head", "drawing_units", "request_bytes"]),
                      (rows_domain.publish_manual_create,
                       ["backend", "tenant_id", "drawing_id", "rows", "module_power_watts", "expected_head",
                        "project_id", "request_bytes"])]:
        params = inspect.signature(fn).parameters
        assert list(params) == names
        for name in names[3:] if fn is rows_domain.publish_manual_create else names:
            assert params[name].kind == inspect.Parameter.KEYWORD_ONLY
        assert params["request_bytes"].default is None
    assert inspect.signature(rows_domain.publish_manual_create).parameters["project_id"].default is None


def test_contract_validator_normalizes_without_mutating_and_binds_the_digest(monkeypatch):
    raw = [{"axis_start": [0, 0], "axis_end": [0, 6], "cross_axis_width_du": 2, "slots": 3}, RAW[1]]
    before = deepcopy(raw)
    result = validate(rows=raw)
    assert raw == before
    normalized = deepcopy(RAW)
    request = {"operation": "manual-create", "expected_head": None, "drawing_units": "m",
               "rows": normalized, "module_power_watts": 450.}
    canonical = json.dumps(request, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")
    assert result == {**request, "rows": [{**row, "row_index": i, "source_command": "manual"}
                                          for i, row in enumerate(normalized)],
                      "request_sha256": hashlib.sha256(canonical).hexdigest(),
                      "summary": {"rows": 2, "slots": 5, "module_power_watts": 450.}}
    assert validate()["request_sha256"] == result["request_sha256"]
    # A negative zero binds the same digest and is stored as a positive zero.
    signed = validate(rows=[dict(RAW[0], axis_start=[-0.0, -0.0]), RAW[1]])
    assert signed["request_sha256"] == result["request_sha256"]
    assert [repr(v) for v in signed["rows"][0]["axis_start"]] == ["0.0", "0.0"]
    for changes in ({"rows": list(reversed(RAW))}, {"rows": [dict(RAW[0], slots=4), RAW[1]]},
                    {"rows": [dict(RAW[0], axis_end=[0., 7.]), RAW[1]]},
                    {"module_power_watts": 451}, {"expected_head": "a" * 64}, {"drawing_units": "ft"}):
        assert validate(**changes)["request_sha256"] != result["request_sha256"]
    for value in (None, {}, (), "rows"):
        refused("TRACKER_ROWS_ROWS_INVALID", validate, rows=value)
    for point in ([True, 0], [float("nan"), 0], [float("inf"), 0], [1e9 + 1, 0],
                  [10 ** 1000, 0], [0], (0, 0), "00"):
        refused("TRACKER_ROWS_ROW_INVALID", validate, rows=[dict(RAW[0], axis_start=point)])
    for row in ({}, dict(RAW[0], row_index=0), [0, 1]):
        refused("TRACKER_ROWS_ROW_INVALID", validate, rows=[row])
    for power in (True, None, "450", -1, float("nan"), float("inf"), 1e15 + 1, 10 ** 1000):
        refused("TRACKER_ROWS_POWER_INVALID", validate, module_power_watts=power)
    for base in (True, 1, "A" * 64, "a" * 63, "g" * 64):
        refused("TRACKER_ROWS_HEAD_INVALID", validate, expected_head=base)
    for unit in (None, [], "M", "in"):
        refused("TRACKER_ROWS_UNITS_UNSUPPORTED", validate, drawing_units=unit)
    for size in (True, -1, 1.0, "10"):
        refused("TRACKER_ROWS_REQUEST_INVALID", validate, request_bytes=size)
    for width in (True, float("nan"), float("inf"), 1e-9, 1000.0001):
        refused("TRACKER_ROWS_WIDTH_INVALID", validate, rows=[dict(RAW[0], cross_axis_width_du=width)])
    for end in ([0, 1e-9], [0, 100000.0001]):
        refused("TRACKER_ROWS_AXIS_INVALID", validate, rows=[dict(RAW[0], axis_end=end)])
    assert validate(rows=[dict(RAW[0], axis_end=[0, 100000], cross_axis_width_du=1000)],
                    module_power_watts=1e15)["module_power_watts"] == 1e15
    assert validate(rows=[dict(RAW[0], slots=10000)] * 10)["summary"]["slots"] == 100000
    assert validate(rows=[dict(RAW[0], slots=1)] * 256)["summary"]["rows"] == 256
    # The canonical byte bound is enforced independently of the supplied wire count.
    monkeypatch.setattr(rows_domain, "MAX_REQUEST_BYTES", len(canonical))
    assert validate(request_bytes=0)["request_sha256"] == result["request_sha256"]
    monkeypatch.setattr(rows_domain, "MAX_REQUEST_BYTES", len(canonical) - 1)
    refused("TRACKER_ROWS_LIMIT_EXCEEDED", validate, request_bytes=0)


def test_contract_refusal_translation_matrix(factory, monkeypatch):
    backend, g = factory()
    matrix = {
        "PHYSICAL_HEAD_CONFLICT": "STALE_HEAD", "PHYSICAL_STATE_CONFLICT": "STALE_HEAD",
        "PHYSICAL_HEAD_LOG_FULL": "LOG_FULL", "PHYSICAL_HEAD_STORE_UNSAFE": "STORE_UNSAFE",
        "PHYSICAL_HEAD_CORRUPT": "STATE_INVALID", "PHYSICAL_STATE_KEY_UNKNOWN": "STATE_INVALID",
        "PHYSICAL_STATE_FRAME_INVALID": "FRAME_UNSUPPORTED", "TERRAIN_FRAME_UNSUPPORTED": "FRAME_UNSUPPORTED",
        "PHYSICAL_STATE_UNITS_UNSUPPORTED": "UNITS_UNSUPPORTED",
        "PHYSICAL_HEAD_PROJECT_MISMATCH": "PROJECT_MISMATCH", "PROJECT_MISMATCH": "PROJECT_MISMATCH",
        "PHYSICAL_STATE_PROJECT_ID_INVALID": "PROJECT_ID_INVALID",
        "PHYSICAL_STATE_DRAWING_NOT_FOUND": "DRAWING_NOT_FOUND",
        "GRAPH_CONTEXT_UNAVAILABLE": "DRAWING_NOT_FOUND", "GRAPH_NOT_EMBEDDED": "GRAPH_REQUIRED",
        "GRAPH_DIGEST_MISMATCH": "GRAPH_REQUIRED", "PHYSICAL_STATE_GRAPH_REQUIRED": "GRAPH_REQUIRED",
        "PHYSICAL_HEAD_WRITES_DRAINED": "WRITES_DRAINED", "PHYSICAL_STATE_WRITES_DRAINED": "WRITES_DRAINED",
        "PHYSICAL_HEAD_STORE_UNAVAILABLE": "STORE_UNAVAILABLE",
        "PHYSICAL_STATE_STORE_UNAVAILABLE": "STORE_UNAVAILABLE", "FRAMES_PILES_STATE_INVALID": "STATE_INVALID",
        "FRAMES_PILES_TERRAIN_INVALID": "STATE_INVALID",
    }
    for code, suffix in matrix.items():
        def fail(*args, _code=code, **kwargs):
            raise ps.PhysicalStateError(_code)
        with monkeypatch.context() as patch:
            patch.setattr(ph, "load_physical_head", fail)
            refused("TRACKER_ROWS_" + suffix, publish, backend)
        assert head(backend) is None
    for code, suffix in [("PROJECT_MISMATCH", "PROJECT_MISMATCH"),
                         ("GRAPH_NOT_EMBEDDED", "GRAPH_REQUIRED"),
                         ("GRAPH_CONTEXT_UNAVAILABLE", "DRAWING_NOT_FOUND")]:
        def fail_graph(*args, _code=code, **kwargs):
            raise GraphValidationError(_code)
        with monkeypatch.context() as patch:
            patch.setattr(rows_domain, "resolve_graph_context", fail_graph)
            refused("TRACKER_ROWS_" + suffix, publish, backend)
    for exc, suffix in [(OSError(), "STORE_UNAVAILABLE"), (RuntimeError(), "STORE_UNAVAILABLE"),
                        (ValueError(), "STATE_INVALID"), (TypeError(), "STATE_INVALID")]:
        def fail_untyped(*args, _exc=exc, **kwargs):
            raise _exc
        with monkeypatch.context() as patch:
            patch.setattr(ph, "load_physical_head", fail_untyped)
            refused("TRACKER_ROWS_" + suffix, publish, backend)
    for project in (True, "", "x" * 101):
        refused("TRACKER_ROWS_PROJECT_ID_INVALID", publish, backend, project_id=project)
    with monkeypatch.context() as patch:
        patch.setattr(write_loop, "drawing_mutations_refusal", lambda: "paused")
        refused("TRACKER_ROWS_WRITES_DRAINED", publish, backend)
    with monkeypatch.context() as patch:
        wrong = deepcopy(g)
        wrong["project"]["installation_design"] = "Roof"
        patch.setattr(rows_domain, "resolve_graph_context", lambda *a, **k: {"graph": wrong, "project_id": PROJECT})
        refused("TRACKER_ROWS_GROUND_REQUIRED", publish, backend)
    assert head(backend) is None
    other, _ = factory(state={}, units="ft")
    before = head(other)
    with monkeypatch.context() as patch:
        patch.setattr(rows_domain, "resolve_graph_context", lambda *a, **k: {"graph": g, "project_id": PROJECT})
        refused("TRACKER_ROWS_UNITS_MISMATCH", publish, other, expected_head=before["state"]["artifact_id"])
    assert head(other) == before


def test_contract_publisher_race_retry_after_child_and_preserved_state(factory, monkeypatch):
    empty = {key: [] if list in types else {} if dict in types and type(None) not in types else None
             for key, types in ps.STATE_KEYS.items()
             if key not in rows_domain.INDEPENDENT_STATE_KEYS and key != "tracker_rows"}
    state = {**deepcopy(NATIVE), **empty, "settings": {"kept": (1, 2), "TrackerModulePmaxW": 1},
             "next_handle": 7, "mesh_faces": 3, "pile_store_text": "kept", "collision_layer": []}
    frame = dict(deepcopy(ps.DEFAULT_FRAME), elevation_datum="EPSG:5703", crs="EPSG:26915")
    backend, _ = factory(state=state, frame=frame)
    base = head(backend)["state"]["artifact_id"]
    first = publish(backend, expected_head=base)
    _, document = ph.load_physical_head(backend, TENANT, DRAWING, project_id=PROJECT)
    expected = deepcopy(state)
    expected["settings"]["TrackerModulePmaxW"] = 450.
    expected["tracker_rows"] = validate(expected_head=base)["rows"]
    assert document["state"] == expected
    assert document["capability"] == "manual-create" and document["parent"] == base
    assert document["source"]["sha256"] == validate(expected_head=base)["request_sha256"]
    assert document["frame"] == frame
    assert set(first) == {"schema", "operation", "outcome", "created", "drawing_id", "project_id",
                          "expected_head", "head", "frame", "summary", "terrain_standing"}
    # Retry is recognized without any publication call and is normalized across numeric spelling.
    with monkeypatch.context() as patch:
        def no_publish(*args, **kwargs):
            pytest.fail("exact retry attempted publication")
        patch.setattr(ph, "publish_physical_state", no_publish)
        retry = publish(backend, expected_head=base, module_power_watts=450.)
        assert retry["created"] is False and retry["head"] == first["head"]
    # A request hash alone cannot establish a retry: all stored bindings must match.
    for field in ("rows", "power", "capability", "digest", "parent"):
        fake_document = deepcopy(document)
        fake_head = deepcopy(first["head"])
        if field == "rows":
            fake_document["state"]["tracker_rows"][0]["slots"] = 4
        elif field == "power":
            fake_document["state"]["settings"]["TrackerModulePmaxW"] = 451.
        elif field == "capability":
            fake_document["capability"] = "other-operation"
        elif field == "digest":
            fake_document["source"]["sha256"] = "a" * 64
        else:
            fake_head["parent"] = None
        with monkeypatch.context() as patch:
            patch.setattr(ph, "load_physical_head", lambda *a, **k: (fake_head, fake_document))
            refused("TRACKER_ROWS_STALE_HEAD", publish, backend, expected_head=base)
        assert head(backend) == first["head"]
    child(backend, next_handle=8)
    saved = head(backend)
    refused("TRACKER_ROWS_STALE_HEAD", publish, backend, expected_head=base)
    assert head(backend) == saved
    # Every dependent populated output refuses, while its accepted empty value is preserved above.
    for key, types in ps.STATE_KEYS.items():
        if key in rows_domain.INDEPENDENT_STATE_KEYS or key == "tracker_rows":
            continue
        value = [{}] if list in types else {"output": True} if dict in types else b"output" if bytes in types else "output"
        guard(factory, "TRACKER_ROWS_DEPENDENT_STATE", initial={"state": {key: value}})
    for state in ({"frames": [{"type": "LWPOLYLINE", "layer": "LEAF-TRACKERS", "vertices": []}]},
                  {"piles": [{"type": "CIRCLE", "layer": "LEAF-PILING", "center": [0, 0]}]},
                  {"grid": {"bad": True}}):
        guard(factory, "TRACKER_ROWS_STATE_INVALID", initial={"state": state})
    # A concurrent child wins entry 1; the losing manual writer cannot append entry 2.
    racing, _ = factory(state={"settings": {"original": True}})
    original = head(racing)
    real_publish = ph.publish_physical_state
    real_atomic = racing.put_if_absent_or_verify
    winner = []
    racing_started = False

    def raced_atomic(key, data):
        nonlocal racing_started
        if key == ph.entry_key(TENANT, DRAWING, 1) and not racing_started:
            racing_started = True
            competing = ps.physical_document({"next_handle": 9}, drawing_units="m",
                                             source_sha256="e" * 64, capability="competing",
                                             parent=original["state"]["artifact_id"])
            winner.append(real_publish(racing, TENANT, DRAWING, competing, project_id=PROJECT)["head"])
        return real_atomic(key, data)

    with monkeypatch.context() as patch:
        patch.setattr(racing, "put_if_absent_or_verify", raced_atomic)
        refused("TRACKER_ROWS_STALE_HEAD", publish, racing, expected_head=original["state"]["artifact_id"])
    assert head(racing) == winner[0] and winner[0]["index"] == 1
    with pytest.raises(KeyError):
        racing.get(ph.entry_key(TENANT, DRAWING, 2))
    # A store failure after the head commits is outcome-unknown; the exact retry recovers it.
    landed, _ = factory()

    def commit_then_fail(*args, **kwargs):
        real_publish(*args, **kwargs)
        raise OSError("response lost after commit")

    with monkeypatch.context() as patch:
        patch.setattr(ph, "publish_physical_state", commit_then_fail)
        refused("TRACKER_ROWS_STORE_UNAVAILABLE", publish, landed)
    committed = head(landed)
    assert committed is not None and committed["index"] == 0
    recovered = publish(landed)
    assert (recovered["outcome"], recovered["created"]) == ("retry", False)
    assert recovered["head"] == committed == head(landed)
