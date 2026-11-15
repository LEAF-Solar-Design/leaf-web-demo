"""Manual Roof stringing must not require weather-location or electrical sizing."""
import copy

import pytest

import product_capability_availability as availability
import solar_local_graph as local
from solar_project import project_validity
from solar_graph_seed import new_empty_graph
from solar_sizing_client import require_sizing
from solar_solve_request import build_stringer_request
from test_w1_design_graph import graph  # noqa: F401


@pytest.fixture(params=["named", "seed-defaults"])
def manual_roof(graph, request):
    value = copy.deepcopy(graph)
    for key in ("frames", "strings", "inverters", "routes", "schedules", "electrical_zones"):
        value[key] = []
    value["project"]["zip_code"] = ""
    value["project"]["validity"] = project_validity(value["project"])
    value["settings"]["panels_in_sequence"] = 14
    outlines = []
    for index, panel in enumerate(value["panels"]):
        x = index * 3.0
        panel.update(frame_ref=None, matrix_cell=None, centre=[x, 0.0], angle=0.0,
                     assignment={"string_ref": None, "seq": None})
        panel["provenance"]["source_handle"] = f"{160 + index:X}"
        outlines.append({"handle": panel["provenance"]["source_handle"],
                         "layer": "Panels", "closed": True,
                         "pts": [[x-1, -.5], [x+1, -.5], [x+1, .5], [x-1, .5]]})
    if request.param == "seed-defaults":
        units = {key: graph["project"]["units"][key] for key in (
            "drawing_units", "wcs_to_ucs", "elevation_datum", "crs")}
        value = new_empty_graph(tenant_id="fixture", drawing_id="manual-roof",
            source_hash=graph["source_hash"], units=units, created_at="2026-11-15T00:00:00Z")
        value = local._load_builtin("solar-settings").run(value,
            {"expected_rev": value["rev"], "changes": {
                "panel_layer_contains": "Panels", "panels_in_sequence": 14}})
        value = local._load_builtin("solar-panels-from-drawing").run(value,
            {"expected_rev": value["rev"]}, source_intake={"polylines": outlines})
    confirmed = local._load_builtin("solar-size-strings").run_bound(value,
        {"expected_rev": value["rev"], "mode": "manual-global", "confirm": True},
        tenant_id="fixture", job_id="manual-confirm")
    grouped = local._load_builtin("solar-panel-groups").run(confirmed,
        {"expected_rev": confirmed["rev"], "discover": {
            "BranchMaxOffset": 1.0, "AlignmentTolerance": .1, "InstallationDesign": "Roof"}},
        source_intake={"polylines": outlines})
    return grouped


def test_confirmed_manual_discovery_admits_solve_without_zip(manual_roof):
    before = copy.deepcopy(manual_roof)
    assert require_sizing(manual_roof)["mode"] == "manual-global"
    assert manual_roof["project"]["validity"] in (
        {"state": "unknown", "reasons": ["project_zip_required"]},
        {"state": "unknown", "reasons": ["seed_defaults"]})
    assert all(frame["module_power_watts"] == 0 for frame in manual_roof["frames"])
    for frame in manual_roof["frames"]:
        assert build_stringer_request(manual_roof, frame["id"], max_string_length=14,
                                      dwgname="Synthetic", full_frame=True)
    readiness = availability.w1_graph_readiness(manual_roof)
    for tool in ("solar-solve-proposal", "solar-commit-solve"):
        assert readiness[tool] == {"input_ready": True, "input_reason": None}
    # Manual geometry does not grant electrical equipment or weather-based sizing readiness.
    assert readiness["solar-size-strings"]["input_ready"] is False
    assert readiness["solar-assign-equipment"]["input_ready"] is False
    assert manual_roof == before


@pytest.mark.parametrize("fault", ["confirmation", "evidence", "length", "panel-moved",
                                    "frame-stale", "panel-stale", "settings-stale",
                                    "project-name", "project-zip", "project-stale"])
def test_manual_roof_exception_keeps_real_prerequisites(manual_roof, fault):
    value = copy.deepcopy(manual_roof)
    if fault == "confirmation":
        value["settings"]["global_string_sizing_confirmed"] = False
    elif fault == "evidence":
        value["settings"]["extra"].pop("string_sizing")
    elif fault == "length":
        value["settings"]["panels_in_sequence"] = 13
    elif fault == "panel-moved":
        value["panels"][0]["centre"][0] += .1
    elif fault in ("frame-stale", "panel-stale", "settings-stale"):
        item = {"frame-stale": value["frames"][0], "panel-stale": value["panels"][0],
                "settings-stale": value["settings"]}[fault]
        item["validity"] = {"state": "stale", "reasons": ["upstream_corrected"]}
    elif fault == "project-stale":
        value["project"]["validity"] = {"state": "stale", "reasons": ["upstream_corrected"]}
    else:
        value["project"]["name" if fault == "project-name" else "zip_code"] = (
            "" if fault == "project-name" else "invalid")
        value["project"]["validity"] = project_validity(value["project"])
    for tool in ("solar-solve-proposal", "solar-commit-solve"):
        assert availability.w1_graph_readiness(value)[tool]["input_ready"] is False
