"""Exercise generated layer subtotals through the normal authored script runner."""
import pytest

import dxf_intake
import tool_loader
import tools_fallback


@pytest.mark.parametrize(("ask", "layers", "excluded"), [
    ("Show just the joist layers, with one subtotal per layer.",
     ("Struc_Steel_Joist", "MetalJoists"), "Struc_Steel_Beam"),
    ("Tally objects on every duct layer.",
     ("MECH-DUCT-1", "DUCT-West"), "ductile"),
    ("List only door layers with a subtotal per layer.",
     ("A-DOOR-N", "Doors West"), "Outdoor"),
])
def test_author_counts_each_matching_layer(ask, layers, excluded):
    tool, source, _ = tools_fallback.author_tool(ask)
    assert tool["capabilities"] == ["drawing.read"]
    assert tool["engine_op"] == "layer_family_subtotals"
    fields = [(0, "SECTION"), (2, "ENTITIES")]
    for layer in (layers[0], excluded, layers[1], layers[0]):
        fields.extend([(0, "LINE"), (8, layer), (10, 0), (20, 0), (11, 1), (21, 0)])
    fields.extend([(0, "ENDSEC"), (0, "EOF")])
    raw = "".join(f"{code}\n{value}\n" for code, value in fields).encode()
    intake = dxf_intake.parse_dxf_bytes(raw)
    status, payload = tool_loader._run_source_in_sandbox(source, "layer_subtotals.py", intake, {})
    assert status == "ok", payload
    result, overlay = payload
    assert overlay is None
    assert result == {"counts": {layers[0]: 2, layers[1]: 1}, "total": 3}
    assert list(result["counts"]) == sorted(layers)

    empty = {"layers": [excluded], "polylines": [{"layer": excluded}]}
    assert tool_loader._run_source_in_sandbox(source, "layer_subtotals.py", empty, {}) == (
        "ok", ({"counts": {}, "total": 0}, None)
    )
