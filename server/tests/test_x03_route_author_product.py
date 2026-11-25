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
    layers = sorted({e["layer"] for e in entities})
    drawing = dxf_intake.parse_dxf_bytes(intake_dxf.intake_to_dxf(dict(layers=layers, polylines=entities)))
    _, source, _ = tools_fallback.author_tool(ask)
    status, payload = tool_loader._run_source_in_sandbox(source, "authored.py", drawing, {})
    assert status == "ok", payload
    result, overlay = payload
    assert result.get("crosses_array") == crosses, result
    assert result.get("stays_in_aisle") == contained, result
    assert overlay["highlight_handles"] == crosses
