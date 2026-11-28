"""Exercise the cable route tool through DXF intake, offline author and executor."""
import sys
from pathlib import Path

import pytest

SERVER = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SERVER))

import dxf_intake
import intake_dxf
import tool_loader
import tools_fallback


def entity(layer, handle, xy, closed=False):
    return dict(layer=layer, handle=handle, closed=closed, pts=[[x, y, 0] for x, y in xy])


def run_authored(ask, entities):
    layers = sorted({e["layer"] for e in entities})
    drawing = dxf_intake.parse_dxf_bytes(intake_dxf.intake_to_dxf(dict(layers=layers, polylines=entities)))
    _, source, _ = tools_fallback.author_tool(ask)
    status, payload = tool_loader._run_source_in_sandbox(source, "authored.py", drawing, {})
    assert status == "ok", payload
    return payload


def footprint(layer, handle, left, right):
    return entity(layer, handle, [(left, 0), (right, 0), (right, 2), (left, 2)], True)


@pytest.mark.parametrize("ask,entities,crosses,contained", [
    (
        "Build a rooftop check that flags cable routes crossing an array instead of following an aisle.",
        [
            entity("Array", "A1", [(0, 0), (4, 0), (4, 4), (0, 4)], True),
            entity("Aisle", "C1", [(-2, 5), (6, 5), (6, 7), (-2, 7)], True),
            entity("Cable", "B1", [(-1, 2), (5, 2)]),
            entity("Cable", "B2", [(-1, 6), (5, 6)]),
            entity("Notes", "D1", [(-1, 2), (5, 2)]),
            dict(entity("Cable", "D2", [(-1, 2), (5, 2)]), space="paper"),
            dict(entity("Aisle", "D3", [(-2, 1), (6, 1), (6, 3), (-2, 3)], True), space="paper"),
        ], ["B1"], ["B2"],
    ),
    (
        "Which wire paths cut through panel arrays and which stay inside the access aisles?",
        [
            entity("PV-Array", "A2", [(3, 1), (4, 1), (4, 4), (3, 4)], True),
            # Endpoints can be in opposite arms of the U without the segment staying inside.
            entity("Access Aisles", "C2", [(0, 0), (8, 0), (8, 8), (6, 8),
                                            (6, 2), (2, 2), (2, 8), (0, 8)], True),
            entity("Wire Paths", "B3", [(1, 6), (7, 6)]),
            entity("Wire Paths", "B4", [(1, 7), (1, 0), (7, 0), (7, 6)]),
            entity("Wire Paths", "B5", [(1, 1.5), (7, 1.5)]),
            entity("Wire Paths", "B9", [(3.5, 0), (3.5, 5)]),
        ], ["B9"], ["B4", "B5"],
    ),
    (
        "Highlight conduit runs that enter an array footprint; identify runs contained in an aisle.",
        [
            entity("Array Footprints", "A3", [(0, 0), (6, 0), (6, 1), (1, 1),
                                               (1, 6), (0, 6)], True),
            entity("Aisle", "C3", [(1, 1), (6, 1), (6, 6), (1, 6)], True),
            entity("Conduit", "B6", [(6, 5), (1, 5), (1, 1)]),
            entity("Conduit", "B7", [(-2, 2), (2, -2)]),  # touches only a corner
            entity("Conduit", "B8", [(0.5, 2), (0.5, 3)]),  # entirely inside the array
        ], ["B8"], ["B6"],
    ),
])
def test_authored_route_crossings(ask, entities, crosses, contained):
    result, overlay = run_authored(ask, entities)
    assert result.get("crosses_array") == crosses, result
    assert result.get("stays_in_aisle") == contained, result
    assert overlay["highlight_handles"] == crosses


@pytest.mark.parametrize("ask,layer,points,footprints,outside,crosses,contained", [
    (
        "Show which cable segments leave the allowed aisles and how many drawing units each spends outside. Flag array crossings separately.",
        "Cable", [(1, 1), (4, 1), (4, 3)],
        [footprint("Aisle", "C1", 0, 2), footprint("Aisle", "C2", 6, 8)],
        [(0, [2, 1], [4, 1], 2), (1, [4, 1], [4, 3], 2)], [], [],
    ),
    (
        "For each wire path, measure the stretches outside access aisles and flag panel array crossings.",
        "Wire Paths", [(1, 1), (11, 1)],
        [footprint("Access Aisles", "C1", 0, 2), footprint("Access Aisles", "C2", 4, 6),
         footprint("Access Aisles", "C3", 10, 12), footprint("PV Array", "A1", 7, 8)],
        [(0, [2, 1], [4, 1], 2), (0, [6, 1], [10, 1], 4)], ["B1"], [],
    ),
    (
        "Calculate how far each conduit run travels outside the marked aisle boundaries.",
        "Conduit", [(1, 0), (5, 0)],
        [footprint("Aisle", "C1", 0, 3), footprint("Aisle", "C2", 3, 6)],
        [], [], ["B1"],
    ),
], ids=["bend", "two-gaps-and-array", "continuous-boundary"])
def test_authored_route_departure_lengths(ask, layer, points, footprints, outside, crosses, contained):
    result, overlay = run_authored(ask, [*footprints, entity(layer, "B1", points)])
    assert result["outside_aisle_intervals"] == [
        {"handle": "B1", "segment_index": index, "from": start, "to": end, "length": length}
        for index, start, end, length in outside
    ]
    assert result["crosses_array"] == crosses
    assert result["stays_in_aisle"] == contained
    assert result["outside_aisle"] == (["B1"] if outside and not crosses else [])
    assert overlay["highlight_handles"] == crosses
