"""Synthetic S03 manual geometry compatibility with the split admission path."""
from copy import deepcopy
from types import SimpleNamespace

import pytest

import leaf_cloud_client as cloud
import solar_project_solve as project_solve
import solar_sizing_client as sizing
import solar_solve_request as requests
from product_capability_availability import w1_graph_readiness
from solar_design_graph import validate_graph
from test_w1_design_graph import entity, graph  # noqa: F401


def manual_large_graph(base):
    value = deepcopy(base)
    frame = value["frames"][0]
    panels, matrix = [], []
    for row in range(22):
        cells = []
        for col in range(12):
            panel = entity("panel", len(panels) + 1, frame_ref=frame["id"],
                matrix_cell={"row": row, "col": col}, centre=[2.0 * col, -3.0 * row],
                angle=0.0, assignment={"string_ref": None, "seq": None})
            panels.append(panel)
            cells.append({"code": "panel", "panel_ref": panel["id"], "seq": None,
                "inverter_id": None, "string_input_number": None,
                "x": panel["centre"][0], "y": panel["centre"][1], "angle": 0.0})
        matrix.append(cells)
    frame.update(panel_refs=[p["id"] for p in panels], matrix=matrix,
        module_rows=22, module_columns=12, module_slots=264, module_power_watts=0,
        extra={"grouping_basis": "geometry-only", "module_power_status": "unknown",
               "sizing_evidence_mode": "manual-global"}, sequences=[],
        panel_assignments=[{"panel_ref": p["id"], "string_ref": None, "seq": None,
            "inverter_id": None, "string_input_number": None} for p in panels])
    value.update(panels=panels, strings=[], inverters=[], routes=[], schedules=[])
    value["electrical_zones"][0]["panel_refs"] = frame["panel_refs"][:]
    settings = value["settings"]
    settings.update(panels_in_sequence=14, voc_cold=sizing.manual_voltage(),
                    global_string_sizing_confirmed=True)
    settings["extra"]["string_sizing"] = sizing.manual_sizing_evidence(value)
    return validate_graph(value)


def test_manual_unknown_power_split_admission(graph, monkeypatch):
    def no_io(*args, **kwargs):
        pytest.fail("split planning must stay offline")
    monkeypatch.setattr(cloud, "resolve_grant", no_io)
    monkeypatch.setattr(cloud.requests.sessions.Session, "request", no_io)
    value = manual_large_graph(graph)
    before = deepcopy(value)
    frame = value["frames"][0]
    assert w1_graph_readiness(value)["solar-solve-proposal"]["input_ready"] is True
    assert requests.needs_split_solve(value, frame["id"], max_string_length=14)
    request = requests.build_stringer_request(value, frame["id"], max_string_length=14,
        dwgname="synthetic-manual-split", full_frame=True)
    plan = project_solve.plan_request(SimpleNamespace(graph=value,
        organization_id="synthetic-tenant"), {"grant_ref": "synthetic-no-access", "request": request},
        {"frame_ref": frame["id"], "expected_rev": value["rev"], "phase": "initial"})
    binding = plan["split_binding"]
    assert len(binding["pieces"]) > 1
    refs = [cell["Id"] for piece in binding["pieces"]
        for row in piece["request"]["grid"]["Rows"] for cell in row["Panels"] if cell["Code"] == 1]
    assert len(refs) == len(set(refs)) == 264
    assert set(refs) == set(frame["panel_refs"])
    assert plan["max_string_length"] == 14
    for piece in binding["pieces"]:
        parsed = cloud.PieceStringerRequest.model_validate(piece["request"])
        lengths, counts = parsed.grid.Sequences
        assert max(lengths) <= 14
        occupied = sum(cell.Code == 1 for row in parsed.grid.Rows for cell in row.Panels)
        assert sum(length * count for length, count in zip(lengths, counts)) == occupied
    assert value == before
    assert value["frames"][0]["module_power_watts"] == 0
    assert value["settings"]["extra"]["string_sizing"]["records"] == {}
