"""Drawing-unit contract for the catalog string-target builtin."""
from __future__ import annotations

import copy
import hashlib
import importlib.util
import json
import math
import sys
from pathlib import Path

import pytest

SERVER_DIR = Path(__file__).resolve().parent.parent
REPO_DIR = SERVER_DIR.parent
if str(SERVER_DIR) not in sys.path:
    sys.path.insert(0, str(SERVER_DIR))

import tool_validate  # noqa: E402


def _load_string_panels():
    path = SERVER_DIR / "builtins" / "string_panels.py"
    spec = importlib.util.spec_from_file_location("string_panels_tool", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


string_panels = _load_string_panels()
INCH_DIGEST = "0ba07e51b7c4dbc61b201f3f59d8ea78fb6bb161500bb3839275258c72ac022b"
PARAM_ERROR = "meters_per_unit must be a finite number greater than 0 and at most 1000"
GRAPH_ERROR = (
    "the drawing's Solar design does not declare a usable meters_per_unit; "
    "pass meters_per_unit"
)
OVERFLOW_ERROR = "drawing coordinates are not finite at this meters_per_unit"
SCALES = [(0.0254, 1.0), (25.4, 0.001), (1 / 12, 0.3048),
          (2.54, 0.01), (0.0254, 1)]
DESCRIPTION = (
    "Metres per drawing unit: inches 0.0254, metres 1.0, millimetres 0.001, "
    "feet 0.3048. Leave empty to use the units this drawing's Solar design "
    "declares, or inches when it declares none. Lengths are returned in feet; "
    "coordinates stay in drawing units."
)


def scaled(intake, f):
    out = copy.deepcopy(intake)
    for pl in out["polylines"]:
        pl["pts"] = [[c * f for c in pt] for pt in pl["pts"]]
    return out


def with_graph(intake, graph):
    out = copy.deepcopy(intake)
    out["solar_design_graph"] = graph
    return out


def g(mpu):
    return {"project": {"units": {"drawing_units": "x", "meters_per_unit": mpu}}}


def digest(pair):
    return hashlib.sha256(
        json.dumps(pair, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def run(intake, params):
    # Enforce immutability of the intake and of the params on every successful and refused call.
    before = copy.deepcopy(intake)
    params_before = copy.deepcopy(params)
    try:
        return string_panels.run(intake, params)
    finally:
        assert intake == before
        assert params == params_before


def rect(x, w, h, f=1.0):
    pts = [[x - w / 2, -h / 2], [x + w / 2, -h / 2], [x + w / 2, h / 2], [x - w / 2, h / 2]]
    return {"layer": "Panels", "closed": True, "pts": [[c * f for c in pt] for pt in pts]}


def assert_equals_baseline(pair, baseline):
    result, base = pair[0], baseline[0]
    assert [s["length_ft"] for s in result["strings"]] == [
        s["length_ft"] for s in base["strings"]]
    assert [s["modules"] for s in result["strings"]] == [
        s["modules"] for s in base["strings"]]
    assert result["stats"] == base["stats"]


def assert_refused(intake, params, message):
    with pytest.raises(ValueError) as exc:
        run(intake, params)
    assert str(exc.value) == message


@pytest.fixture(scope="module")
def intake():
    return json.loads(
        (REPO_DIR / "data" / "rooftop_demo.intake.json").read_text(encoding="utf-8")
    )


@pytest.fixture(scope="module")
def baseline(intake):
    return run(intake, {})


def test_string_panels_units_inch_default_unchanged(intake, baseline):
    for drawing, params in [
        (intake, {}), (intake, {"meters_per_unit": 0.0254}),
        (intake, {"meters_per_unit": None}),
        (with_graph(intake, g(0.0254)), {}),
        (with_graph(intake, None), {}),
    ]:
        assert digest(run(drawing, params)) == INCH_DIGEST
    result = baseline[0]
    assert result["stats"]["bank_count"] == 11
    assert result["stats"]["string_count"] == 135
    assert result["stats"]["full_string_count"] == 124
    assert result["stats"]["total_wire_ft"] == 15475.5
    assert result["strings"][0]["length_ft"] == 115.7
    assert result["strings"][0]["modules"] == 18


@pytest.mark.parametrize("f,mpu", SCALES)
def test_string_panels_units_explicit_scale_matches_inch(intake, baseline, f, mpu):
    pair = run(scaled(intake, f), {"meters_per_unit": mpu})
    assert_equals_baseline(pair, baseline)
    for string, base in zip(pair[0]["strings"], baseline[0]["strings"]):
        assert len(string["pts"]) == len(base["pts"])
        for pt, pb in zip(string["pts"], base["pts"]):
            assert len(pt) == 2
            assert all(abs(c - cb * f) <= 0.00100001 * f
                       for c, cb in zip(pt, pb))


def test_string_panels_units_today_was_wrong(intake):
    metres = run(scaled(intake, 0.0254), {"meters_per_unit": 0.0254})[0]
    assert metres["stats"]["total_wire_ft"] == 391.2
    millimetres = run(scaled(intake, 25.4), {"meters_per_unit": 0.0254})[0]
    assert millimetres["stats"]["string_count"] == 2345
    assert millimetres["stats"]["total_wire_ft"] == 0.0


@pytest.mark.parametrize("f,mpu", SCALES[:4])
def test_string_panels_units_graph_scale_used_when_param_absent(
        intake, baseline, f, mpu):
    drawing = with_graph(scaled(intake, f), g(mpu))
    explicit = run(scaled(intake, f), {"meters_per_unit": mpu})
    for params in ({}, {"meters_per_unit": None}):
        pair = run(drawing, params)
        assert_equals_baseline(pair, baseline)
        assert digest(pair) == digest(explicit)


@pytest.mark.parametrize("f,mpu", SCALES)
def test_string_panels_units_param_beats_graph(intake, baseline, f, mpu):
    pair = run(with_graph(scaled(intake, f), g(123.0)), {"meters_per_unit": mpu})
    assert_equals_baseline(pair, baseline)


@pytest.mark.parametrize("value", [
    True, False, "1", 0, 0.0, -1, float("nan"), float("inf"), -float("inf"),
    1001, 1000.0000001, [1], {}, 10 ** 400,
])
def test_string_panels_units_bad_param_refused(intake, value):
    for drawing in (intake, with_graph(intake, g(0.0254))):
        assert_refused(drawing, {"meters_per_unit": value}, PARAM_ERROR)


@pytest.mark.parametrize("graph", [
    {}, {"project": None}, {"project": {}}, {"project": {"units": None}},
    {"project": {"units": {}}}, g(None), g(True), g(0), g(-1.0), g("1"),
    g(float("nan")), g(float("inf")), g(1000.0000001), [], "x", 5, False,
    {"project": []}, {"project": {"units": [1.0]}},
])
def test_string_panels_units_bad_graph_refused(intake, graph):
    drawing = with_graph(intake, graph)
    assert_refused(drawing, {}, GRAPH_ERROR)
    assert digest(run(drawing, {"meters_per_unit": 0.0254})) == INCH_DIGEST


def test_string_panels_units_bounds_accepted(intake):
    for value in (1000.0, 1000, 1.0):
        stats = run(intake, {"meters_per_unit": value})[0]["stats"]
        assert stats["string_count"] == 2345
        assert stats["bank_count"] == 2345
        assert stats["total_wire_ft"] == 0.0
    tiny = run(intake, {"meters_per_unit": 5e-324})
    assert tiny[0]["stats"]["string_count"] == 135
    assert tiny[0]["stats"]["bank_count"] == 11
    # Not the inch reading: every length collapses and every point returns to the origin.
    assert tiny[0]["stats"]["total_wire_ft"] == 0.0
    assert tiny[0]["strings"][0]["pts"][0] == [0.0, 0.0]
    assert digest(tiny) != INCH_DIGEST
    assert run(with_graph(intake, g(1000)), {})[0]["stats"]["string_count"] == 2345


def test_string_panels_units_overflow_refused(intake):
    drawing = copy.deepcopy(intake)
    first = next(pl for pl in drawing["polylines"]
                 if pl.get("layer") == "Panels" and pl.get("closed"))
    first["pts"][0][0] = 1e308
    assert_refused(drawing, {"meters_per_unit": 1000}, OVERFLOW_ERROR)
    assert run(drawing, {})[0]["stats"]["string_count"] == 136


def test_string_panels_units_result_overflow_refused():
    # The scaled coordinates are finite (about 0.0017 inch); the point overflows on the way back
    # to drawing units, so only the check on the returned points can refuse this drawing.
    x = 1.7e308
    drawing = {"polylines": [{"layer": "Panels", "closed": True,
                              "pts": [[x, 0.0], [x, 1.0], [x, 1.0], [x, 0.0]]}]}
    assert x * (2.54e-313 / 0.0254) < 0.01
    assert_refused(drawing, {"meters_per_unit": 2.54e-313}, OVERFLOW_ERROR)
    result = run(drawing, {"meters_per_unit": 2.54e-312})[0]
    assert result["stats"]["string_count"] == 1
    assert len(result["strings"][0]["pts"]) == 1
    assert all(math.isfinite(c) for c in result["strings"][0]["pts"][0])


def test_string_panels_units_cluster_threshold_tie_is_a_declared_limit():
    # DECLARED LIMIT, not a guarantee. Two panels exactly 200 inches apart sit on the inclusive
    # neighbour window. A feet drawing stores 200/12, the scale is 12.000000000000002, and the
    # product lands one float step outside the window, so the two units disagree at the tie.
    # One inch inside the window they agree.
    def summary(drawing, params):
        result = run(drawing, params)[0]
        return ([s["modules"] for s in result["strings"]],
                [s["length_ft"] for s in result["strings"]])

    feet = {"meters_per_unit": 0.3048}
    assert summary({"polylines": [rect(0, 2, 1), rect(200, 2, 1)]}, {}) == ([2], [16.7])
    assert summary({"polylines": [rect(0, 2, 1, 1 / 12), rect(200, 2, 1, 1 / 12)]}, feet) == (
        [1, 1], [0.0, 0.0])
    assert summary({"polylines": [rect(0, 2, 1), rect(199, 2, 1)]}, {}) == ([2], [16.6])
    assert summary({"polylines": [rect(0, 2, 1, 1 / 12), rect(199, 2, 1, 1 / 12)]}, feet) == (
        [2], [16.6])


def test_string_panels_units_intake_not_mutated(intake):
    drawing = scaled(intake, 0.0254)
    before = copy.deepcopy(drawing)
    run(drawing, {"meters_per_unit": 1.0})
    assert drawing == before
    assert_refused(drawing, {"meters_per_unit": True}, PARAM_ERROR)
    assert drawing == before
    drawing = with_graph(drawing, g(1.0))
    before = copy.deepcopy(drawing)
    run(drawing, {})
    assert drawing == before
    drawing = with_graph(drawing, {})
    before = copy.deepcopy(drawing)
    assert_refused(drawing, {}, GRAPH_ERROR)
    assert drawing == before


def test_string_panels_units_defaults_unchanged():
    assert string_panels.DEFAULTS == {
        "module_model": "Q.Peak DUO XL-G11.7 / 425", "voc": 48.5,
        "temp_coeff_pct_per_c": -0.27, "design_min_temp_c": -25.0,
        "max_system_voltage": 1000.0, "panel_layer": "Panels",
        "cluster_radius_factor": 3.0,
    }


def test_string_panels_units_catalog_schema():
    catalog = json.loads((SERVER_DIR / "catalog_tools.json").read_text(encoding="utf-8"))
    record = next(t for t in catalog["tools"] if t["name"] == "autofill-string-targets")
    assert record["params"]["properties"]["meters_per_unit"] == {
        "type": ["number", "null"], "exclusiveMinimum": 0,
        "maximum": 1000.0, "description": DESCRIPTION,
    }
    assert "meters_per_unit" not in record["default_params"]
    for params in ({}, {"meters_per_unit": None},
                   *({"meters_per_unit": v} for v in (1.0, 1, 1000))):
        assert tool_validate.validate_params(record, params) == []
    for value in (0, -1, "1", True, 1001):
        errors = tool_validate.validate_params(record, {"meters_per_unit": value})
        assert len(errors) == 1
        assert errors[0].startswith("meters_per_unit:")


def test_string_panels_units_no_panels_message_unchanged():
    for drawing in (None, {}):
        assert_refused(drawing, {}, "no closed polylines found on layer 'Panels'")
