"""Feeder kernel inches across drawing units, with explicit bridge round trips."""
from copy import deepcopy
import json
from pathlib import Path
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import solar_electrical_route_bridge as rb
import solar_electrical_state_bridge as legacy
import solar_feeder_graph as fg
from solar_sizing_client import digest
from test_solar_tool_combiners import i4, place

UNITS = {"in": (0.0254, 1.0), "mm": (0.001, 25.4), "ft": (0.3048, 1 / 12),
         "m": (1.0, 0.0254)}
INPUT_DIGESTS = {
    "in": "2bf76c5170333bfae0c78f48c958947b67c14223662b0db51806795257abaa3f",
    "mm": "1fbe4a19024b42c76c291dedd757aaabb329a4240a0190b4055b39541fe78bdd",
    "ft": "2a7971891714aa25dab62f940e308e31a460532b9d3eb1a420a8451a8f38e1ef",
    "m": "8ab36c6d023e24fcc9c5badc9fbe230c80f578f3ed194f7b558b85f3d5b6b972",
}
INVALID_UNIT_MESSAGE = "metres_per_unit must be a positive finite number"


@pytest.fixture(scope="module")
def site():
    g0, intake, groups0 = i4()
    g0 = place(g0, intake, groups0)
    return g0, groups0


def variant(site, name):
    g, groups = deepcopy(site)
    mpu, scale = UNITS[name]
    g["project"]["units"]["meters_per_unit"] = mpu
    g["project"]["units"]["drawing_units"] = name
    if scale != 1.0:
        for group in groups:
            group["outlines"] = [[[p[0] * scale, p[1] * scale, *p[2:]] for p in ring]
                                 for ring in group["outlines"]]
    return g, groups


def feeders(graph):
    return {r["from_ref"]: r for r in graph["routes"] if r["route_kind"] == "feeder"}


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)


def recorder(monkeypatch):
    captured = []

    def record(state, groups, host):
        captured.append(deepcopy(groups))
        raise fg.cab.InverterCablingError("fixture refusal")

    monkeypatch.setattr(fg.cab, "route_l2_feeders", record)
    return captured


def test_feeder_units_inch_unchanged(site):
    g0, groups0 = site
    result, receipt = fg.route_feeders(g0, groups0)
    assert digest(result) == "c4b257fe56bad86b8c44dcabd5786269ee1973cb7d7de7cd27b17b7cee15b6e0"
    assert receipt["feeders"] == receipt["unchanged"] == receipt["assignments_created"] == 14
    assert receipt["redrawn"] == []
    for name, expected in INPUT_DIGESTS.items():
        g, _ = variant(site, name)
        assert digest(g) == expected


@pytest.mark.parametrize("name", ["mm", "ft", "m"])
def test_feeder_units_other_units_match_inch(site, name):
    inch, _ = fg.route_feeders(*site)
    result, receipt = fg.route_feeders(*variant(site, name))
    reference, actual = feeders(inch), feeders(result)
    assert actual.keys() == reference.keys()
    assert len(actual) == receipt["feeders"] == 14
    lengths = sorted(r["length_ft"] for r in actual.values())
    assert round(sum(lengths), 6) == 2647.891417
    assert [round(length, 6) for length in lengths[:5]] == [
        36.424378, 66.267332, 88.370225, 163.301148, 164.2944]
    for key, route in actual.items():
        baseline = reference[key]
        assert len(route["points"]) == len(baseline["points"])
        for point, expected in zip(route["points"], baseline["points"]):
            assert len(point) == len(expected)
            assert all(abs(a - b) <= 1e-9 for a, b in zip(point, expected))
        assert abs(route["length_ft"] - baseline["length_ft"]) <= 1e-9 * max(1, baseline["length_ft"])


def test_feeder_units_bridge_default_unchanged(site):
    g, _ = variant(site, "mm")
    for kwargs, mpu in [({}, 0.001), ({"metres_per_unit": 0.0254}, 0.0254)]:
        state, binding = rb.state_from_graph(g, **kwargs)
        inverters = {i["id"]: i for i in g["inverters"]}
        for row in state["rows"]["device"]:
            inverter = inverters[binding["devices"][row["_pair"]]["id"]]
            expected = [inverter["position"][0] / mpu, inverter["position"][1] / mpu]
            assert list(rb.st.point_of(row["position"], "device position")) == expected


@pytest.mark.parametrize("bridge", [legacy, rb], ids=["state", "route"])
def test_feeder_units_explicit_project_unit_is_identity(site, bridge):
    g, _ = variant(site, "mm")
    assert canonical(bridge.state_from_graph(g, metres_per_unit=0.001)) == canonical(bridge.state_from_graph(g))


@pytest.mark.parametrize("unit", [0, -1, float("nan"), float("inf"), True, "0.0254"])
def test_feeder_units_invalid_kernel_unit_refused(site, unit):
    g, _ = variant(site, "mm")
    for bridge in (legacy, rb):
        state, binding = bridge.state_from_graph(g)
        for function, args in [(bridge.state_from_graph, (g,)),
                               (bridge.graph_from_state, (g, state, binding))]:
            with pytest.raises(ValueError) as error:
                function(*args, metres_per_unit=unit)
            assert str(error.value) == INVALID_UNIT_MESSAGE
            # Invalid units must be refused before reading even a malformed graph or state.
            with pytest.raises(ValueError) as error:
                function(*(None for _ in args), metres_per_unit=unit)
            assert str(error.value) == INVALID_UNIT_MESSAGE


def test_feeder_units_outline_rescale_and_identity(site, monkeypatch):
    captured = recorder(monkeypatch)
    for name in ("in", "mm"):
        g, groups = variant(site, name)
        snapshot = deepcopy(groups)
        with pytest.raises(fg.FeederGraphError) as error:
            fg.route_feeders(g, groups)
        assert error.value.code == "FEEDER_KERNEL_REFUSED"
        assert groups == snapshot
        expected = deepcopy(groups)
        if name == "mm":
            for group in expected:
                group["outlines"] = [[[p[0] * 0.001 / 0.0254, p[1] * 0.001 / 0.0254, *p[2:]]
                                      for p in ring] for ring in group["outlines"]]
        assert captured[-1] == expected
    assert captured[0] == site[1]
    assert len(captured) == 2


def test_feeder_units_rescale_overflow_refused(site, monkeypatch):
    captured = recorder(monkeypatch)
    g, groups = variant(site, "m")
    groups[0]["outlines"][0][0][0] = 1e308
    snapshot = deepcopy(groups)
    with pytest.raises(fg.FeederGraphError) as error:
        fg.route_feeders(g, groups)
    assert error.value.code == "FEEDER_OUTLINES_INVALID"
    assert captured == []
    assert groups == snapshot
