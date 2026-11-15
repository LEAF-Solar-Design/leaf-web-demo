"""Three-decimal imported centroids retain their recorded module orientation."""
import copy
import math

import pytest

import solar_solaredge_import as importer
import solar_solaredge_report as reports


ANGLE = math.radians(17.2)


def imported_graph():
    panels = []
    for row in range(3):
        for col in range(3):
            x, y = col * 50, row * 90
            panels.append({"id": str(len(panels)), "angle": math.degrees(ANGLE),
                "centre": [round(x * math.cos(ANGLE) - y * math.sin(ANGLE), 3),
                           round(x * math.sin(ANGLE) + y * math.cos(ANGLE), 3)],
                "provenance": {"source_handle": f"A{len(panels):X}",
                               "created_by": "solar-panels-from-drawing"}})
    return {"project": {"units": {"drawing_units": "in", "meters_per_unit": 0.0254,
                                  "compute_units": "m", "drawing_unit_is_feet": False}},
            "panels": panels, "frames": [{"id": "roof", "panel_refs": [p["id"] for p in panels],
                "extra": {"grouping_basis": "geometry-only"},
                "provenance": {"created_by": "solar-panel-groups"}}]}


def report(monkeypatch, graph):
    def match(matrices, groups, panels, **kwargs):
        return {"matches": [], "strings": [], "bridge_strings": [], "unassigned": [],
                "matchable_count": 0, "bridge_grid_count": 0}
    monkeypatch.setattr(importer, "run_import", match)
    return reports.report_from_matrices(graph, [], alignment_tolerance=0.01,
                                       selection_order="unknown")


def test_rounded_import_uses_recorded_rotation_without_graph_mutation(monkeypatch):
    graph = imported_graph()
    before = copy.deepcopy(graph)
    with pytest.raises(importer.SolarEdgeImportError, match="lattice-angle-ambiguous"):
        importer.lattice_row_angle([p["centre"] for p in graph["panels"]])
    result = report(monkeypatch, graph)
    assert math.isclose(result["row_angle"], ANGLE, abs_tol=1e-12)
    assert graph == before


@pytest.mark.parametrize("damage", ["skew", "producer", "frame-producer", "not-geometry",
                                    "mixed-angles", "missing-angle", "nonfinite-angle"])
def test_rounding_correction_refuses_unsupported_or_inconsistent_evidence(monkeypatch, damage):
    graph = imported_graph()
    if damage == "skew":
        graph["panels"][0]["centre"][1] += 0.1
    elif damage == "producer":
        graph["panels"][0]["provenance"]["created_by"] = "fixture"
    elif damage == "frame-producer":
        graph["frames"][0]["provenance"]["created_by"] = "fixture"
    elif damage == "not-geometry":
        graph["frames"][0]["extra"] = {}
    elif damage == "mixed-angles":
        graph["panels"][0]["angle"] += 1
    elif damage == "missing-angle":
        del graph["panels"][0]["angle"]
    else:
        graph["panels"][0]["angle"] = float("nan")
    with pytest.raises(reports.SolarEdgeReportError, match="REPORT_ROW_ANGLE_UNRESOLVED"):
        report(monkeypatch, graph)


@pytest.mark.parametrize("angle", [17.2, 44.99999, 107.2, -72.8])
def test_recorded_angle_checks_rounding_across_axis_wrap(angle):
    radians = math.radians(angle)
    centres = [[round(x * math.cos(radians), 3), round(x * math.sin(radians), 3)]
               for x in (0, 50, 100)]
    result = importer.lattice_row_angle(centres, recorded_angle=radians,
                                       coordinate_tolerance=0.0005)
    expected = (radians + math.pi / 4) % (math.pi / 2) - math.pi / 4
    assert math.isclose(result, expected, abs_tol=1e-12)


@pytest.mark.parametrize("angle,tolerance", [(True, 0.0005), (float("nan"), 0.0005),
                                             (0, 0), (0, 0.001)])
def test_recorded_rotation_requires_finite_bounded_rounding(angle, tolerance):
    with pytest.raises(importer.SolarEdgeImportError, match="lattice-angle-unavailable"):
        importer.lattice_row_angle([[0, 0], [50, 0]], recorded_angle=angle,
                                   coordinate_tolerance=tolerance)


def test_rounding_does_not_infer_an_axis_from_subquantum_spacing():
    with pytest.raises(importer.SolarEdgeImportError, match="lattice-angle-ambiguous"):
        importer.lattice_row_angle([[0, 0], [0.0001, 0]], recorded_angle=0,
                                   coordinate_tolerance=0.0005)
