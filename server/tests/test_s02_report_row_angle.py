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


def whole_matrix_case(*, bridge=False):
    """New inert two-by-two PDF, parsed into two one-row source subgrids."""
    cells = [{"Code": 1, "Seq": i + 1, "Id": "", "InverterId": 1,
              "StringInputNumber": 1} for i in range(4)]
    matrix = {"Dwgname": "synthetic-whole", "Sequences": [4],
        "Rows": [{"Panels": cells[:2]}, {"Panels": cells[2:]}],
        "SubGrids": [{"Id": 0, "Cells": [[1, 1], [1, 0]]},
                     {"Id": 1, "Cells": [[0, 1], [0, 0]]}],
        "BridgeConnections": [{"FromRow": 1, "FromCol": 0, "ToRow": 0, "ToCol": 0}]
                             if bridge else []}
    # PDF rows use the existing rotated-cell coordinate convention.
    panels = [{"handle": f"A{i + 1}", "x": x, "y": y}
              for i, (x, y) in enumerate([(1, 0), (0, 0), (1, 1), (0, 1)])]
    return [matrix], [{"block": "whole", "panels": [p["handle"] for p in panels]}], panels


def test_whole_matrix_legacy_review_keeps_the_unresolved_witness():
    matrices, groups, panels = whole_matrix_case()
    result = importer.run_import(matrices, groups, panels, alignment_tolerance=0.01, review=True)
    assert result["matches"][0]["status"] == "unmatched"
    assert result["matches"][0]["candidates"] == []
    assert result["strings"] == result["bridge_strings"] == []
    with pytest.raises(importer.SolarEdgeImportError, match="no-structural-match"):
        importer.run_import(matrices, groups, panels, alignment_tolerance=0.01)


@pytest.mark.parametrize("bridge", [False, True])
def test_optin_whole_matrix_preserves_labels_once_and_never_mutates_source(bridge):
    matrices, groups, panels = whole_matrix_case(bridge=bridge)
    before = copy.deepcopy((matrices, groups, panels))
    result = importer.run_import(matrices, groups, panels, alignment_tolerance=0.01,
                                 review=True, allow_whole_matrices=True)
    assert result["matches"][0]["grid_index"] == 0
    assert result["matches"][0]["original_index"] == 0
    assert result["matches"][0]["sub_grid_id"] == -1
    assert result["strings"][0]["panels"] == ["A1", "A2", "A3", "A4"]
    assert len(result["strings"]) == 1
    assert result["bridge_strings"] == result["unassigned"] == []
    assert (matrices, groups, panels) == before


def test_whole_matrix_never_competes_with_an_atomic_drawing_group():
    matrices, groups, panels = whole_matrix_case()
    panels += [{"handle": "B1", "x": 1, "y": 10}, {"handle": "B2", "x": 0, "y": 10}]
    groups += [{"block": "atomic", "panels": ["B1", "B2"]}]
    result = importer.run_import(matrices, groups, panels, alignment_tolerance=0.01,
                                 review=True, allow_whole_matrices=True)
    assert result["matches"][0]["status"] == "unmatched"
    assert result["matches"][1]["status"] == "ambiguous"
    assert result["strings"] == result["bridge_strings"] == []


def test_duplicate_whole_pdf_shapes_are_not_chosen_by_order():
    matrices, groups, panels = whole_matrix_case()
    matrices.append(copy.deepcopy(matrices[0]))
    result = importer.run_import(matrices, groups, panels, alignment_tolerance=0.01,
                                 review=True, allow_whole_matrices=True)
    assert result["matches"][0]["status"] == "ambiguous"
    assert result["matches"][0]["grid_index"] is None
    assert len(result["matches"][0]["candidates"]) == 2
    assert result["strings"] == result["bridge_strings"] == []


def test_competing_whole_drawing_groups_do_not_get_an_automatic_replacement():
    matrices, groups, panels = whole_matrix_case()
    duplicate = [{"handle": f"B{i + 1}", "x": p["x"] + 10, "y": p["y"]}
                 for i, p in enumerate(panels)]
    panels += duplicate
    groups += [{"block": "other", "panels": [p["handle"] for p in duplicate]}]
    result = importer.run_import(matrices, groups, panels, alignment_tolerance=0.01,
                                 review=True, allow_whole_matrices=True)
    assert all(m["status"] == "unmatched" for m in result["matches"])
    assert result["strings"] == result["bridge_strings"] == []


@pytest.mark.parametrize("value", [None, 0, 1, "yes", [], {}])
def test_whole_matrix_policy_requires_an_explicit_boolean(value):
    matrices, groups, panels = whole_matrix_case()
    with pytest.raises(importer.SolarEdgeImportError, match="invalid-matching-policy"):
        importer.run_import(matrices, groups, panels, alignment_tolerance=0.01,
                             review=True, allow_whole_matrices=value)


def test_whole_matrix_policy_is_never_enabled_on_the_strict_legacy_path():
    matrices, groups, panels = whole_matrix_case()
    with pytest.raises(importer.SolarEdgeImportError, match="invalid-matching-policy"):
        importer.run_import(matrices, groups, panels, alignment_tolerance=0.01,
                             allow_whole_matrices=True)


def test_report_policy_can_match_a_complete_synthetic_matrix_without_changing_legacy_default():
    graph = imported_graph()
    before = copy.deepcopy(graph)
    rows = [{"Panels": [{"Code": 1, "Id": "", "Seq": r * 3 + c + 1,
                         "InverterId": 1, "StringInputNumber": 1} for c in range(3)]}
            for r in range(3)]
    matrix = {"Dwgname": "synthetic-policy", "Rows": rows, "Sequences": [9],
        "SubGrids": [{"Id": 0, "Cells": [[2, c] for c in range(3)]},
                     {"Id": 1, "Cells": [[r, c] for r in range(2) for c in range(3)]}],
        "BridgeConnections": []}
    atomic = reports.report_from_matrices(graph, [matrix], alignment_tolerance=0.01,
                                         selection_order="unknown")
    modern = reports.report_from_matrices(graph, [matrix], alignment_tolerance=0.01,
        selection_order="unknown", matching_policy="whole-matrix-v1")
    assert atomic["counts"]["matched_frames"] == 0
    assert modern["counts"]["matched_frames"] == 1
    assert modern["counts"]["assigned_panels"] == 9
    assert modern["unassigned_panel_refs"] == []
    assert graph == before
