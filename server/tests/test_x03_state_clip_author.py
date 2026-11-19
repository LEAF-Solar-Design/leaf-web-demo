"""The expected path set comes from the fixture's state table and analytic clip bounds."""
import pytest

from test_x03_clipped_block_author import drawing
from test_x03_offline_drawing_author import invoke
from tools_fallback import author_tool


ASKS = [
    "Count the visible rack modules after applying both the active state and the nested clip.",
    "List panel placements that survive the rack visibility overrides and the inverted XCLIP.",
    "How many solar panels remain after block clipping and visibility-state selection?",
]


def fixture(state, broken=False):
    raw = drawing(broken=broken)
    parameter = [(0, "BLOCKVISIBILITYPARAMETER"), (5, "A9"), (100, "AcDbBlockVisibilityParameter"),
                 (301, "Mode"), (302, "Layout"), (93, 2), (331, "D1"), (331, "D2"), (92, 3)]
    for label, members in [("Rows", ["D1"]), ("Spare", ["D2"]), ("All", ["D1", "D2"])]:
        parameter += [(303, label), (94, len(members)), *((332, h) for h in members), (95, 0)]
    objects = b"0\nSECTION\n2\nOBJECTS\n"
    raw = raw.replace(objects, objects+"".join(f"{c}\n{v}\n" for c,v in parameter).encode())
    row = b"0\nINSERT\n5\nD1\n"
    raw = raw.replace(row, b"0\nATTDEF\n5\nD0\n2\nMode\n1\nRows\n"
                      b"0\nINSERT\n5\nD2\n2\nPV Module\n8\n0\n10\n0.5\n20\n1\n"+row)
    # Put the state attribute immediately after its owner rack reference.
    rack = raw.index(b"0\nINSERT\n5\nC1\n")
    raw = raw[:rack]+raw[rack:].replace(b"5\nC1\n", b"5\nC1\n66\n1\n", 1)
    end = raw.index(b"0\nENDBLK\n", rack)
    return raw[:end]+f"0\nATTRIB\n5\nC2\n330\nC1\n2\nMode\n1\n{state}\n0\nSEQEND\n".encode()+raw[end:]


@pytest.mark.parametrize("ask,state", zip(ASKS, ["Rows", "Spare", "All"]))
def test_combined_filters(ask, state):
    _, source, _ = author_tool(ask)
    status, payload = invoke(source, fixture(state))
    assert status == "ok", payload
    names = []
    if state in ("Rows", "All"):
        names = [f"D1[{y},{x}]" for y in range(3) for x in range(4)
                 if not (1 < x < 3 and 0 < y < 2)]
    if state in ("Spare", "All"):
        names.append("D2[0,0]")
    expected = sorted("B1[0,0]/C1[0,0]/"+name for name in names)
    assert payload[0].get("visible_paths") == expected, payload
    assert payload[0]["total"] == len(expected)


@pytest.mark.parametrize("state,broken", [("Typo", False), ("Rows", True)])
def test_missing_state_or_filter_fails(state, broken):
    _, source, _ = author_tool(ASKS[0])
    assert invoke(source, fixture(state, broken))[0] == "tool_error"
