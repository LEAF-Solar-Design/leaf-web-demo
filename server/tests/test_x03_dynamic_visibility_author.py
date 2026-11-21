"""Explicit visibility labels on local DXF fixtures with native parameter member tables."""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import dxf_intake
import tool_loader
import tools_fallback


ASKS = [
    "Count the panels shown by the active visibility state of each dynamic rack block.",
    "How many PV modules are visible after applying the individual block state overrides?",
    "List the visible solar panel placements across nested racks using each instance's visibility setting.",
]


def fixture(default, override, copies=1, damage=""):
    def block(name, children):
        return [(0, "BLOCK"), (2, name), (10, 0), (20, 0), *children, (0, "ENDBLK")]

    def ref(h, name, cols=1, **kw):
        return [(0, "INSERT"), (5, h), (2, name), (8, "Panels"), (10, 0), (20, 0),
                (70, cols), (44, 3), *((int(k), v) for k, v in kw.items())]

    def attribute(h, owner, value):
        return [(0, "ATTRIB"), (5, h), (330, owner), (2, "Mode"), (1, value), (0, "SEQEND")]

    states = {"East": ["A1"], "West": ["A2"], "Both": ["A1", "A2"], "Off": []}
    params = [(0, "BLOCKVISIBILITYPARAMETER"), (5, "F1"), (100, "AcDbBlockVisibilityParameter"),
              (301, "Mode"), (302, "Rack variant"), (93, 2), (331, "A1"), (331, "A2"), (92, 4)]
    for name, members in states.items():
        params += [(303, name), (94, len(members)), *((332, h) for h in members), (95, 0)]
    if damage == "foreign":
        params[params.index((332, "A1"))] = (332, "AB")
    rack = [(0, "ATTDEF"), (5, "AB"), (2, "Mode"), (1, default)]
    rack += ref("A1", "PV Module", 2) + ref("A2", "PV Module")
    if damage == "uncontrolled":
        rack += ref("A3", "PV Module")
    nest = ref("C1", "Rack", 1, **{"66": 1}) + attribute("CF", "C1", override)
    defs = block("PV Module", [(0, "POINT"), (10, 0), (20, 0)]) + block("Rack", rack) + block("Carrier", nest)
    roots = ref("B1", "Rack") + ref("B2", "Carrier", copies) + ref("BF", "Rack", **{"67": 1})
    pairs = []
    for section, records in [("BLOCKS", defs), ("ENTITIES", roots), ("OBJECTS", params)]:
        pairs += [(0, "SECTION"), (2, section), *records, (0, "ENDSEC")]
    return "".join(f"{code}\n{value}\n" for code, value in pairs + [(0, "EOF")]).encode()


def execute(ask, raw):
    _, source, _ = tools_fallback.author_tool(ask)
    intake = dxf_intake.parse_dxf_bytes(raw)
    return tool_loader._run_source_in_sandbox(source, "visible_panels.py", intake, {})


@pytest.mark.parametrize("ask,default,override,copies,default_members,override_members", [
    (ASKS[0], "East", "West", 2, ["A1[0,0]", "A1[0,1]"], ["A2[0,0]"]),
    (ASKS[1], "Off", "Both", 2, [], ["A1[0,0]", "A1[0,1]", "A2[0,0]"]),
    (ASKS[2], "West", "Off", 3, ["A2[0,0]"], []),
])
def test_visible_state_paths(ask, default, override, copies, default_members, override_members):
    status, payload = execute(ask, fixture(default, override, copies))
    assert status == "ok", payload
    result, _ = payload
    expected = ["B1[0,0]/" + member for member in default_members]
    expected += [f"B2[0,{col}]/C1[0,0]/{member}" for col in range(copies) for member in override_members]
    assert result.get("visible_paths") == sorted(expected), result
    assert result["total"] == len(expected)


@pytest.mark.parametrize("default,override,damage", [
    ("East", "Undeclared", ""), ("East", "West", "foreign"), ("East", "West", "uncontrolled"),
])
def test_unresolved_visibility_fails(default, override, damage):
    status, payload = execute(ASKS[0], fixture(default, override, damage=damage))
    assert status == "tool_error" and "visib" in str(payload).lower(), payload


def test_active_clip_on_reached_instance_refuses_partial_visibility_count():
    from test_x03_clipped_block_author import drawing as clipped_drawing

    start = b"0\nSECTION\n2\nOBJECTS\n"
    clip_objects = clipped_drawing().split(start)[1].split(b"0\nENDSEC\n")[0]
    raw = fixture("East", "West")
    raw = raw.replace(start, start + clip_objects)
    raw = raw.replace(b"0\nINSERT\n5\nB1\n", b"0\nINSERT\n5\nB1\n102\n{ACAD_XDICTIONARY\n360\n91\n102\n}\n")
    status, payload = execute(ASKS[0], raw)
    assert status == "tool_error" and "clipping" in str(payload).lower(), payload
