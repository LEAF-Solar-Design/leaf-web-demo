"""Compare lengths from the saved sources bound to real electrical schedules."""
from copy import deepcopy

import pytest

import solar_project_schedule_compare as comparison
from test_sip_r6_routes import http, save_schedule_graph, schedule_artifact  # noqa: F401
from test_solar_tool_electrical_schedules import c10_graphs
from test_w1_design_graph import graph  # noqa: F401


def _artifact(http, source):
    return schedule_artifact(http, save_schedule_graph(http, source))


def _compare(http, before, after, **scope):
    s = http.s
    return comparison.compare_saved_schedules(
        s.tenant, s.project, s.drawing, before_version_id=before["source_version_id"],
        after_version_id=after["source_version_id"], before_artifact_id=before["artifact_id"],
        after_artifact_id=after["artifact_id"], **scope,
    )


def _detour(route):
    x, y = route["points"][0]
    route["points"][1:1] = [[x, y + 10], [x, y]]


@pytest.mark.parametrize("kind", ["start homerun", "end homerun", "feeder"])
def test_length_comparison_uses_route_geometry(http, graph, kind):
    _, before = c10_graphs(graph)
    after = deepcopy(before)
    route = next(row for row in after["routes"] if row["route_kind"] == kind)
    _detour(route)
    assert route["length_ft"] == next(row["length_ft"] for row in before["routes"] if row["id"] == route["id"])
    result = _compare(http, _artifact(http, before), _artifact(http, after), cable_offset=0)

    assert result["schema"] == comparison.CABLE_SCHEMA
    assert result["total_changes"] == 1 and result["next_offset"] is None
    change, = result["changes"]
    assert change["route_id"] == route["id"] and change["change"] == "changed"
    assert change["after"]["route_kind"] == kind
    assert change["after"]["length_ft"] - change["before"]["length_ft"] == pytest.approx(
        20 / 0.3048, abs=0.11 if kind == "feeder" else 0.011)


def test_length_comparison_pages_added_and_removed_routes(http, graph, monkeypatch):
    before, after = (_artifact(http, source) for source in c10_graphs(graph))
    monkeypatch.setattr(comparison, "_CABLE_PAGE_SIZE", 1)
    first = _compare(http, before, after, cable_offset=0)
    second = _compare(http, before, after, cable_offset=first["next_offset"])
    reverse = _compare(http, after, before, cable_offset=0)
    same = _compare(http, after, after, cable_offset=0)

    assert first["total_changes"] == second["total_changes"] == 2
    assert first["next_offset"] == 1 and second["next_offset"] is None
    added, = first["changes"]
    removed, = reverse["changes"]
    assert added["route_id"] < second["changes"][0]["route_id"]
    assert added["change"] == "added" and added["before"] is None
    assert removed == {"route_id": added["route_id"], "change": "removed",
                       "before": added["after"], "after": None}
    assert same["changes"] == [] and same["total_changes"] == 0
