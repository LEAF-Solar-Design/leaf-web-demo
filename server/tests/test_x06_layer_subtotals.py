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
    for layer, handle in zip((layers[0], excluded, layers[1], layers[0]), ("A4", "A2", "a3", "A1")):
        fields.extend([(0, "LINE"), (5, handle), (8, layer), (10, 0), (20, 0), (11, 1), (21, 0)])
    fields.extend([(0, "ENDSEC"), (0, "EOF")])
    raw = "".join(f"{code}\n{value}\n" for code, value in fields).encode()
    intake = dxf_intake.parse_dxf_bytes(raw)
    status, payload = tool_loader._run_source_in_sandbox(source, "layer_subtotals.py", intake, {})
    assert status == "ok", payload
    result, overlay = payload
    assert overlay is None
    assert result == {
        "counts": {layers[0]: 2, layers[1]: 1}, "total": 3,
        "member_handles_by_layer": {layers[0]: ["A1", "A4"], layers[1]: ["A3"]},
    }
    assert list(result["counts"]) == sorted(layers)

    empty = {"layers": [excluded], "polylines": [{"layer": excluded}]}
    assert tool_loader._run_source_in_sandbox(source, "layer_subtotals.py", empty, {}) == (
        "ok", ({"counts": {}, "total": 0, "member_handles_by_layer": {}}, None)
    )

@pytest.mark.parametrize(("ask", "first", "second"), [
    ("Give me separate joist and beam subtotals, and flag any layer whose name matches both.",
     "joist", "beam"),
    ("Compare duct and pipe layer subtotals and show overlapping layers.", "duct", "pipe"),
    ("List separate door and window subtotals, and list layers that match both.",
     "door", "window"),
])
def test_compare_layer_families(ask, first, second):
    tool, source, _ = tools_fallback.author_tool(ask)
    assert tool["engine_op"] == "layer_family_subtotals"
    overlap = f"A_{first}_{second}"
    layers = [f"A_{first}", overlap, overlap, f"B_{second}", "other"]
    intake = {"polylines": [{"layer": layer, "handle": f"A{i}"} for i, layer in enumerate(layers)]}
    status, payload = tool_loader._run_source_in_sandbox(source, "layer_subtotals.py", intake, {})
    assert status == "ok", payload
    result, _ = payload
    assert result == {
        "table": {
            "columns": ["Layer", first, second, "Overlap"],
            "rows": [[layers[0], 1, "", ""], [overlap, 2, 2, "Yes"], [layers[3], "", 1, ""]],
        },
        "overlapping_layers": [overlap],
        "total": 4,
        "member_handles_by_layer": {layers[0]: ["A0"], overlap: ["A1", "A2"], layers[3]: ["A3"]},
    }


def test_subtotal_keeps_counts_when_contributor_identity_is_ambiguous():
    _, source, _ = tools_fallback.author_tool("Tally objects on every duct layer.")
    intake = {
        "layers": ["Duct-Empty"],
        "polylines": [
            {"layer": "Duct-Missing", "handle": "L1"},
            {"layer": "Duct-Missing", "handle": "10"},
            {"layer": "Duct-Duplicate", "handle": "a1"},
        ],
        "inserts": [{"layer": "Mechanical", "handle": "A1"},
                    {"layer": "Duct-Mixed", "handle": "B1"}],
        "faces3d": [{"layer": "Duct-Mixed", "handle": "B2"}],
    }
    status, payload = tool_loader._run_source_in_sandbox(source, "layer_subtotals.py", intake, {})
    assert status == "ok", payload
    assert payload == ({
        "counts": {"Duct-Duplicate": 1, "Duct-Empty": 0, "Duct-Missing": 2, "Duct-Mixed": 2},
        "total": 5,
        "member_handles_by_layer": {"Duct-Duplicate": None, "Duct-Empty": [],
                                    "Duct-Missing": None, "Duct-Mixed": ["B1", "B2"]},
    }, None)


def test_three_family_oxford_comma_comparison():
    ask = "Compare joist, beam, and duct layer subtotals and show overlapping layers."
    tool, source, _ = tools_fallback.author_tool(ask)
    assert tool["engine_op"] == "layer_family_subtotals"
    intake = {"polylines": [{"layer": "A_JOIST_BEAM"}, {"layer": "B_DUCT"}]}
    status, payload = tool_loader._run_source_in_sandbox(source, "layer_subtotals.py", intake, {})
    assert status == "ok", payload
    result, _ = payload
    assert result["table"]["columns"] == ["Layer", "joist", "beam", "duct", "Overlap"]
    assert result["table"]["rows"] == [["A_JOIST_BEAM", 1, 1, "", "Yes"],
                                      ["B_DUCT", "", "", 1, ""]]
    assert result["overlapping_layers"] == ["A_JOIST_BEAM"]
    assert result["total"] == 2
