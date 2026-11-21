"""Offline author and execution checks on public or synthetic drawing geometry."""
import re
import sys
from pathlib import Path

import pytest

SERVER = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SERVER))

import dxf_intake
import intake_dxf
import tool_loader
import tools_fallback


def with_current_layer(raw, layer):
    # The standard DXF current-layer variable belongs in the HEADER section.
    raw = raw.replace(b"\r\n", b"\n")
    header = b"0\nSECTION\n2\nHEADER\n"
    assert header in raw and b"$CLAYER" not in raw
    return raw.replace(header, header + f"9\n$CLAYER\n8\n{layer}\n".encode(), 1)


def invoke(source, raw, params=None):
    intake = dxf_intake.parse_dxf_bytes(raw)
    return tool_loader._run_source_in_sandbox(source, "authored.py", intake, params or {})


def synthetic_roof():
    # One 2x1 module on North, two on South, and one on a literal layer called active.
    polys = []
    for layer, x, handle in (("North", 0, "A1"), ("South", 3, "B1"),
                             ("South", 6, "B2"), ("active", 9, "C1")):
        polys.append(dict(layer=layer, closed=True, handle=handle,
                          pts=[[x, 0, 0], [x+2, 0, 0], [x+2, 1, 0], [x, 1, 0]]))
    return intake_dxf.intake_to_dxf(dict(layers=["North", "South", "active"], polylines=polys))


def test_demo_panel_count_on_active_layer():
    raw = (SERVER.parent / "web/public/sample.dxf").read_bytes()
    _, source, _ = tools_fallback.author_tool("Make a tool that counts panels on the active layer.")
    status, payload = invoke(source, with_current_layer(raw, "Panels"))
    assert status == "ok", payload
    assert payload[0]["total"] == 2345


@pytest.mark.parametrize("ask,metric,expected", [
    ("Count entities on the current layer.", "total", (1, 2)),
    ("Measure total area on the active layer.", "total_area", (2, 4)),
    ("Highlight closed shapes near the edge on the current layer.", "matched", (1, 2)),
])
def test_one_authored_tool_follows_drawing_layer(ask, metric, expected):
    raw = synthetic_roof()
    _, source, _ = tools_fallback.author_tool(ask)
    for layer, value in zip(("North", "South"), expected):
        params = {"margin": 1} if metric == "matched" else {}
        status, payload = invoke(source, with_current_layer(raw, layer), params)
        assert status == "ok", payload
        assert (payload[0]["layer"], payload[0][metric]) == (layer, value)


def test_quoted_layer_active_is_literal():
    _, source, _ = tools_fallback.author_tool('Count entities on the "active" layer.')
    status, payload = invoke(source, with_current_layer(synthetic_roof(), "South"))
    assert status == "ok", payload
    assert (payload[0]["layer"], payload[0]["total"]) == ("active", 1)


def test_missing_current_layer_requires_context():
    _, source, _ = tools_fallback.author_tool("Count entities on the current layer.")
    status, payload = invoke(source, synthetic_roof())
    assert status == "tool_error", payload
    assert re.search(r"(active|current) layer", str(payload), re.IGNORECASE)


@pytest.mark.parametrize("ask", [
    "Make a tool that counts panels on the active layer.",
    "How many PV modules are on the current layer?",
    "Tally the solar panels on the active layer.",
])
def test_mixed_active_layer_counts_named_module_placements_only(ask):
    block_names = ("PV Module", "Roof Vent")
    placements = [("Equipment", "PV Module"), ("Equipment", "Roof Vent"),
                  ("South", "PV Module")]
    drawing = dict(layers=["Equipment", "South"], polylines=[], blocks={
        name: dict(base=[0, 0, 0], children=[dict(kind="POINT", layer="0", pt=[0, 0, 0])])
        for name in block_names
    }, inserts=[dict(name=name, layer=layer, x=i*3, y=0, z=0, handle=f"{16+i:X}")
                for i, (layer, name) in enumerate(placements)])
    raw = with_current_layer(intake_dxf.intake_to_dxf(drawing), "Equipment")
    _, source, _ = tools_fallback.author_tool(ask)
    status, payload = invoke(source, raw)
    assert status == "ok", payload
    assert (payload[0]["layer"], payload[0]["total"]) == ("Equipment", 1)
