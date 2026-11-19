"""Beam takeoffs count physical INSERT paths even when their coordinates coincide."""
from copy import deepcopy

import pytest

from test_x03_oblique_spline_author import section
from test_x03_offline_drawing_author import dxf_intake, invoke, tool_loader, tools_fallback


ASKS = [
    ("Total beam lengths by layer, counting coincident members separately.", 1, 1),
    ("List each beam length in centimetres per layer, including overlapping block instances.", 2, .1),
    ("Sum beam lengths per layer in metres, counting each placed entity once.", 3, .001),
]


def drawing(columns):
    def line(handle):
        return [(0, "LINE"), (5, handle), (8, "0"), (10, 0), (20, 0), (11, 3), (21, 4)]

    def placement(name, handle, layer, count):
        return [(0, "INSERT"), (5, handle), (2, name), (8, layer), (10, 2), (20, 1),
                (70, count), (71, 1), (44, 0)]

    blocks = [(0, "BLOCK"), (2, "Bars"), *line("D1"), *line("D2"), (0, "ENDBLK"),
              (0, "BLOCK"), (2, "Frame"), *placement("Bars", "E1", "0", 1), (0, "ENDBLK")]
    inserts = placement("Frame", "A1", "Steel-Beams", columns) + placement("Frame", "A2", "Steel-Beams", 1)
    wire = (section("HEADER", [(9, "$INSUNITS"), (70, 4)]) + section("BLOCKS", blocks)
            + section("ENTITIES", inserts) + [(0, "EOF")])
    return "".join(f"{k}\n{v}\n" for k, v in wire).encode()


@pytest.mark.parametrize("ask,columns,factor", ASKS)
def test_physical_instance_totals_survive_repeated_traversal(ask, columns, factor):
    raw = drawing(columns)
    _, source, _ = tools_fallback.author_tool(ask)
    status, payload = invoke(source, raw)
    assert status == "ok", payload
    copies = ["A1"] if columns == 1 else [f"A1[0,{i}]" for i in range(columns)]
    independent = {f"{placement}/E1/{line}": 5 * factor
                   for placement in [*copies, "A2"] for line in ["D1", "D2"]}
    result = payload[0]
    assert result["lengths_by_layer"] == pytest.approx({"Steel-Beams": sum(independent.values())})
    if ask.startswith("List"):
        assert {k: v[result["units"]] for k, v in result["lengths_by_handle"].items()} == independent

    intake = dxf_intake.parse_dxf_bytes(raw)
    for rows in (intake["inserts"], intake["blocks"]["Frame"]["nested_inserts"],
                 intake["blocks"]["Bars"]["beam_paths"]):
        rows.append(deepcopy(rows[0]))
    status, payload = tool_loader._run_source_in_sandbox(source, "authored.py", intake, {})
    assert status == "ok", payload
    assert payload[0] == result


def test_conflicting_records_for_one_insert_require_clarification():
    _, source, _ = tools_fallback.author_tool(ASKS[0][0])
    intake = dxf_intake.parse_dxf_bytes(drawing(1))
    intake["inserts"].append(dict(intake["inserts"][0], columns=3))
    status, payload = tool_loader._run_source_in_sandbox(source, "authored.py", intake, {})
    assert status == "tool_error"
    assert "Conflicting beam evidence for handle A1" in str(payload)
