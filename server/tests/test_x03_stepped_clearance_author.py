"""Independent piecewise-plane clearance expectations for the offline author."""
import math

import pytest

from test_x03_spatial_route_clearance import (
    dxf_intake, intake_dxf, path, tool_loader, tools_fallback,
)


ASKS = (
    "Check clearance over a stepped panel surface and report where along each cable the minimum occurs.",
    "Where along the wire is its closest approach to the stepped array surface, and what is the vertical clearance?",
    "Find the smallest vertical gap between this conduit and the stepped module faces, with distance along the run.",
)


def raw_drawing(route):
    # Adjacent closed faces: z=x+2 on [0,3], then z=9 on [3,6].
    left = path("A1", [(0,0,2), (3,0,5), (3,2,5), (0,2,2)], True)
    right = path("A2", [(3,0,9), (6,0,9), (6,2,9), (3,2,9)], True)
    return intake_dxf.intake_to_dxf(dict(layers=["Panels", "Cable Routes"], polylines=[
        left, right, path("B1", route),
        path("B2", [(8,1,7), (9,1,7)]),
        path("B3", [(1,1,4), (3,1,6)]),
    ]))


def run(ask, raw):
    pkg, source, _ = tools_fallback.author_tool(ask)
    assert "clearance" not in pkg["params"]["required"], pkg
    drawing = dxf_intake.parse_dxf_bytes(raw)
    return tool_loader._run_source_in_sandbox(source, "authored.py", drawing, {})


@pytest.mark.parametrize("ask,route,separation,station,face", [
    # First contact with the upper step at x=3: |8-9|=1.
    (ASKS[0], [(-1,1,8), (7,1,8)], 1, 4, "A2"),
    # After a 4-unit lead-in, z=3+x/2 intersects z=2+x at x=2.
    (ASKS[1], [(-1,-3,2.5), (-1,1,2.5), (7,1,6.5)], 0, 4 + 3*math.hypot(1,0.5), "A1"),
    # Both faces include the seam. No surface exists between z=5 and z=9.
    (ASKS[2], [(3,1,7), (3,1,8)], 1, 1, "A2"),
])
def test_stepped_station(ask, route, separation, station, face):
    status, payload = run(ask, raw_drawing(route))
    assert status == "ok", payload
    row = payload[0]["minimum_by_route"]["B1"]
    assert row["separation"] == pytest.approx(separation)
    assert row["station"] == pytest.approx(station)
    assert row["panel_handle"] == face
    # B2 never passes over a face. B3 keeps a gap of one along the
    # entire lower slope, so its first vertex is the earliest minimum.
    assert payload[0]["minimum_by_route"]["B2"] is None
    assert payload[0]["minimum_by_route"]["B3"] == {
        "separation": 1, "station": 0, "panel_handle": "A1"}
    assert "clear_routes" not in payload[0]


def test_missing_step_elevation():
    raw = raw_drawing([(-1,1,8), (7,1,8)]).replace(b"\r\n", b"\n")
    assert b"38\n9.0\n" in raw
    status, payload = run(ASKS[0], raw.replace(b"38\n9.0\n", b""))
    assert status == "tool_error" and "elevation" in str(payload).lower(), payload
