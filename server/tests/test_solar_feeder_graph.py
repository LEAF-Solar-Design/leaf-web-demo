"""Regeneration (rather than adoption) of every design-graph L1-to-L2 feeder."""
from copy import deepcopy
from pathlib import Path
import re
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import solar_feeder_graph as fg
from solar_design_graph import validate_graph
from test_solar_combiner_graph import c5, fresh, place, topo_strip  # noqa: F401, fixture
from test_solar_tool_equipment_move import move
import test_solar_ground_route_kinds as rk

cab, rb, st = fg.cab, fg.rb, fg.st


def feeders(g):
    return [r for r in g["routes"] if r["route_kind"] == "feeder"]


def kernel(g, groups, *, strip=True):
    state, _ = rb.state_from_graph(g)
    if strip:
        state["rows"]["cable"] = [r for r in state["rows"]["cable"] if r.get("cable_kind") != "feeder"]
    cap = next(i["collector_capacity"] for i in g["inverters"] if i["is_l2"])
    return cab.route_l2_feeders(state, groups, {"UseL2Collectors": True, "L1CollectorsPerL2": cap})


def refused(g, groups, code):
    snapshot = deepcopy(g)
    with pytest.raises(fg.FeederGraphError) as excinfo:
        fg.route_feeders(g, groups)
    assert excinfo.value.code == str(excinfo.value) == code
    assert g == snapshot


def test_feeder_graph_regenerates_every_feeder(c5, monkeypatch):
    g, groups = c5[3], c5[2]
    expected, lines = kernel(g, groups)
    real = cab.route_l2_feeders
    captured = []

    def record(state, outlines, host):
        assert not any(r.get("cable_kind") == "feeder" for r in state["rows"]["cable"])
        after, lines = real(state, outlines, host)
        captured.append(deepcopy(after))
        return after, lines

    monkeypatch.setattr(cab, "route_l2_feeders", record)
    result, receipt = fg.route_feeders(g, groups)
    assert len(captured) == 1
    rows = {cab._feeder_ends(r)[0]: r for r in captured[0]["rows"]["cable"] if r.get("cable_kind") == "feeder"}
    assert rows == {cab._feeder_ends(r)[0]: r for r in expected["rows"]["cable"] if r.get("cable_kind") == "feeder"}
    l1 = {i["id"]: i for i in result["inverters"] if not i["is_l2"]}
    mpu = result["project"]["units"]["meters_per_unit"]
    assert len(feeders(result)) == len(l1) == receipt["feeders"]
    for route in feeders(result):
        row = rows[l1[route["from_ref"]]["number"]]
        points = [st.point_of(v, "vertex") for v in row["vertices"]]
        assert route["points"] == [[x * mpu, y * mpu] for x, y in points]
        assert route["length_ft"] == row["length"]["value"]
        assert route["to_ref"] == l1[route["from_ref"]]["l2_ref"]
    assert receipt["format"] == "leaf.solar-feeder-routing.v1"
    assert receipt["unchanged"] + len(receipt["redrawn"]) == receipt["feeders"]
    saved = next(line for line in lines if line.endswith("new combiner box assignment(s) saved."))
    assert receipt["assignments_created"] == int(saved.split()[0])
    assert len(receipt["lines"]) <= 64 and all(len(line) <= 512 for line in receipt["lines"])


def test_feeder_graph_bare_kernel_adopts_everything(c5):
    _, lines = kernel(c5[3], c5[2], strip=False)
    assert "0 nearest-lane comb feeder(s) drawn." in lines


def test_feeder_graph_redraws_a_moved_combiner(c5):
    baseline, _ = fg.route_feeders(c5[3], c5[2])
    l1 = next(i for i in baseline["inverters"] if not i["is_l2"])
    mpu = baseline["project"]["units"]["meters_per_unit"]
    point = [l1["position"][0] / mpu + 0.001, l1["position"][1] / mpu - 0.001]
    moved = move(baseline, l1["id"], point)["graph"]
    result, receipt = fg.route_feeders(moved, c5[2])
    old = {r["id"]: r for r in feeders(moved)}
    changed = next(r for r in feeders(result) if r["from_ref"] == l1["id"])
    assert changed["id"] in old and changed["points"] != old[changed["id"]]["points"]
    source = next(i for i in result["inverters"] if i["id"] == l1["id"])
    assert changed["points"][0] == source["position"]
    assert receipt["redrawn"] == [changed["id"]]
    assert receipt["unchanged"] == len(old) - 1
    assert all(r == old[r["id"]] for r in feeders(result) if r["id"] != changed["id"])


def test_feeder_graph_is_deterministic(c5):
    g, groups = c5[3], c5[2]
    snapshot = deepcopy((g, groups))
    first = fg.route_feeders(g, groups)
    second = fg.route_feeders(g, groups)
    assert fg._canonical(first) == fg._canonical(second)
    assert (g, groups) == snapshot
    missing = deepcopy(g)
    missing["routes"] = [r for r in missing["routes"] if r["route_kind"] != "feeder"]
    before = deepcopy(missing)
    assert fg._canonical(fg.route_feeders(missing, groups)) == fg._canonical(fg.route_feeders(missing, groups))
    assert missing == before


def test_feeder_graph_mints_schema_valid_ids(c5):
    g = deepcopy(c5[3])
    g["routes"] = [r for r in g["routes"] if r["route_kind"] != "feeder"]
    first, _ = fg.route_feeders(g, c5[2])
    second, _ = fg.route_feeders(g, c5[2])
    assert validate_graph(first) == first
    ids = [r["id"] for r in feeders(first)]
    pattern = (r"leaf:[a-z][a-z0-9-]*:[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-"
               r"[89ab][0-9a-f]{3}-[0-9a-f]{12}")
    assert ids and all(re.fullmatch(pattern, value) for value in ids)
    assert len(ids) == len(set(ids))
    assert ids == [r["id"] for r in feeders(second)]


def test_feeder_graph_no_land_lane_refused(c5):
    groups = [{"handle": "no-lane", "outlines": [[[0, 0], [0, 1], [0, 2]]]}]
    assert cab.derive_vertical_lanes(cab.validate_outlines(groups)) == []
    refused(c5[3], groups, "FEEDER_NOT_PORTED")


def test_feeder_graph_requires_l2_mode(c5):
    g = topo_strip(c5[0])
    g["settings"]["use_l2_collectors"] = False
    refused(validate_graph(g), c5[2], "FEEDER_L2_MODE_REQUIRED")


def test_feeder_graph_requires_combiners_and_collectors(c5):
    refused(c5[0], c5[2], "FEEDER_COMBINERS_REQUIRED")
    g = deepcopy(c5[3])
    g["inverters"] = [i for i in g["inverters"] if not i["is_l2"]]
    for inverter in g["inverters"]:
        inverter["l2_ref"] = None
    g["routes"] = [r for r in g["routes"] if r["route_kind"] != "feeder"]
    refused(validate_graph(g), c5[2], "FEEDER_COLLECTORS_REQUIRED")


def test_feeder_graph_capacity_must_be_one_value(c5):
    g = deepcopy(c5[3])
    next(i for i in g["inverters"] if i["is_l2"])["collector_capacity"] += 1
    refused(validate_graph(g), c5[2], "FEEDER_CAPACITY_AMBIGUOUS")


def test_feeder_graph_outlines_invalid(c5):
    class ListSubclass(list):
        pass

    huge = [{"handle": "huge", "outlines": [[[10**400, 0], [1, 1], [2, 0]]]}]
    for groups in (huge, {}, ListSubclass(c5[2])):
        refused(c5[3], groups, "FEEDER_OUTLINES_INVALID")


def test_feeder_graph_trench_rider_releases_its_pathway(c5):
    g, _ = fg.route_feeders(c5[3], c5[2])
    route = feeders(g)[0]
    trench = rk.trench(900, points=route["points"])
    g["routes"].append(trench)
    route["pathway_ref"] = trench["id"]
    route["points"][0][0] += 1.0
    g = validate_graph(g)
    snapshot = deepcopy(g)
    result, receipt = fg.route_feeders(g, c5[2])
    redrawn = next(r for r in result["routes"] if r["id"] == route["id"])
    assert redrawn["points"] != route["points"] and redrawn["pathway_ref"] is None
    assert redrawn["id"] in receipt["redrawn"]
    assert fg._canonical(next(r for r in result["routes"] if r["id"] == trench["id"])) == fg._canonical(trench)
    assert g == snapshot


def test_feeder_graph_postcondition_refuses_a_missing_feeder(c5, monkeypatch):
    real = cab.route_l2_feeders

    def missing(*args, **kwargs):
        after, lines = real(*args, **kwargs)
        row = next(r for r in after["rows"]["cable"] if r.get("cable_kind") == "feeder")
        after["rows"]["cable"].remove(row)
        return after, lines

    monkeypatch.setattr(fg.cab, "route_l2_feeders", missing)
    refused(c5[3], c5[2], "FEEDER_POSTCONDITION_FAILED")


@pytest.mark.parametrize("case", ["capacity", "destination", "vertices"])
def test_feeder_graph_output_failure_stays_closed(c5, monkeypatch, case):
    g = deepcopy(c5[3])
    if case == "capacity":
        g["routes"] = [r for r in g["routes"] if r["route_kind"] != "feeder"]
        for inverter in g["inverters"]:
            if inverter["is_l2"]:
                inverter["l1_assignments"] = []
                inverter["collector_capacity"] = 1
            else:
                inverter["l2_ref"] = None
    else:
        real = cab.route_l2_feeders

        def invalid(*args, **kwargs):
            after, lines = real(*args, **kwargs)
            row = next(r for r in after["rows"]["cable"] if r.get("cable_kind") == "feeder")
            if case == "destination":
                row["to"] = next(i["number"] for i in g["inverters"]
                                 if i["is_l2"] and i["number"] != row["to"])
            else:
                row["vertices"] = row["vertices"][:1]
            return after, lines

        monkeypatch.setattr(fg.cab, "route_l2_feeders", invalid)
    refused(validate_graph(g), c5[2], "FEEDER_POSTCONDITION_FAILED")


@pytest.mark.parametrize(("created_at", "expected"), [
    ("2026-10-01T12:00:00+00:00", "2026-10-01T12:00:00Z"),
    ("2026-10-01T12:00:00.123+02:30", "2026-10-01T09:30:00.123000Z"),
])
def test_feeder_graph_default_timestamp_normalized(c5, created_at, expected):
    g = deepcopy(c5[3])
    g["project"]["provenance"]["created_at"] = created_at
    g["routes"] = [r for r in g["routes"] if r["route_kind"] != "feeder"]
    snapshot = deepcopy(g)
    result, receipt = fg.route_feeders(g, c5[2])
    assert len(feeders(result)) == receipt["feeders"] == 14
    assert all(r["provenance"]["created_at"] == expected for r in feeders(result))
    second, _ = fg.route_feeders(result, c5[2])
    assert second == result
    assert g == snapshot


def test_feeder_graph_length_is_the_kernel_row(c5):
    g = deepcopy(c5[3])
    prior_mpu = g["project"]["units"]["meters_per_unit"]
    g["project"]["units"].update(drawing_units="ft", meters_per_unit=0.3048, drawing_unit_is_feet=True)
    scale = 0.3048 / prior_mpu
    for inverter in g["inverters"]:
        inverter["position"] = [p * scale for p in inverter["position"]]
    for string in g["strings"]:
        string["route"] = [[p * scale for p in point] for point in string["route"]]
    for route in g["routes"]:
        route["points"] = [[p * scale for p in point] for point in route["points"]]
    expected, _ = kernel(validate_graph(g), c5[2])
    rows = {cab._feeder_ends(r)[0]: r for r in expected["rows"]["cable"] if r.get("cable_kind") == "feeder"}
    result, _ = fg.route_feeders(g, c5[2])
    mpu = result["project"]["units"]["meters_per_unit"]
    numbers = {i["id"]: i["number"] for i in result["inverters"]}
    for route in feeders(result):
        # Graph points are metres; kernel lengths treat drawing units as inches.
        points = [(p[0] / mpu, p[1] / mpu) for p in route["points"]]
        assert route["length_ft"] == rows[numbers[route["from_ref"]]]["length"]["value"]
        assert route["length_ft"] == pytest.approx(cab._path_length(points) / cab.INCHES_PER_FOOT, rel=1e-15)


def test_feeder_graph_codes_closed():
    assert fg.CODES == ("FEEDER_L2_MODE_REQUIRED", "FEEDER_COMBINERS_REQUIRED", "FEEDER_COLLECTORS_REQUIRED",
                        "FEEDER_CAPACITY_AMBIGUOUS", "FEEDER_OUTLINES_INVALID", "FEEDER_NOT_PORTED",
                        "FEEDER_KERNEL_REFUSED", "FEEDER_POSTCONDITION_FAILED")
    assert issubclass(fg.FeederGraphError, ValueError)
