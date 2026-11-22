"""Panel clearance through the offline author, DXF intake, and script executor."""
import sys
from pathlib import Path

import pytest

SERVER = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SERVER))

import dxf_intake
import intake_dxf
import tool_loader
import tools_fallback


ASK = "Check the cable routes that pass above or below a panel and tell me which ones actually violate its clearance."


def path(handle, points, panel=False):
    return dict(handle=handle, layer="Panels" if panel else "Cable Routes",
                closed=panel, pts=points)


def drawing(routes):
    panel = path("A1", [(0, 0, 10), (4, 0, 10), (4, 4, 10), (0, 4, 10)], True)
    return intake_dxf.intake_to_dxf(dict(layers=["Panels", "Cable Routes"],
                                         polylines=[panel, *routes]))


def execute(ask, raw, params):
    package, source, _ = tools_fallback.author_tool(ask)
    assert package["params"]["required"] == ["clearance"]
    assert package["params"]["properties"]["clearance"]["exclusiveMinimum"] == 0
    intake = dxf_intake.parse_dxf_bytes(raw)
    return tool_loader._run_source_in_sandbox(source, "authored.py", intake, params)


@pytest.mark.parametrize("ask,routes,violations,clear", [
    (ASK, [
        path("B1", [(-1, 2, 8.5), (5, 2, 8.5)]),
        path("B2", [(-1, 2, 12), (5, 2, 12)]),
        path("B3", [(-1, 2, 8), (5, 2, 8)]),
    ], ["B1"], ["B2", "B3"]),
    ("Which wire runs breach the vertical clearance of a module?", [
        path("C1", [(-2, 1, 0), (6, 1, 20)]),
        path("C2", [(-10, 1, 10), (2, 1, 22)]),
    ], ["C1"], ["C2"]),
    ("Flag conduit routes inside the panel clearance envelope above or below its footprint.", [
        path("D1", [(0, 0, 9), (0, 0, 12)]),
        path("D2", [(-2, 2, 13), (2, -2, 13)]),
        path("D3", [(-1, 0, 12), (1, -2, 10)]),
    ], ["D1"], ["D2", "D3"]),
])
def test_clearance(ask, routes, violations, clear):
    status, payload = execute(ask, drawing(routes), {"clearance": 2})
    assert status == "ok", payload
    result, overlay = payload
    assert result.get("violating_routes") == violations, result
    assert result.get("clear_routes") == clear, result
    assert overlay["highlight_handles"] == violations


def test_missing_elevation_is_unknown_even_if_parser_supplies_zero():
    raw = drawing([path("B1", [(1, 1, 11), (2, 2, 11)])]).replace(b"\r\n", b"\n")
    assert b"38\n11.0\n" in raw
    status, payload = execute(ASK, raw.replace(b"38\n11.0\n", b""), {"clearance": 2})
    assert status == "tool_error" and "elevation" in str(payload).lower(), payload


def test_clearance_requirement_is_explicit():
    status, payload = execute(ASK, drawing([path("B1", [(1, 1, 11), (2, 2, 11)])]), {})
    assert status == "tool_error" and "clearance" in str(payload).lower(), payload
