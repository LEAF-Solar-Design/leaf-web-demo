"""Replay actual nested DXF INSERT records through the template author and sandbox."""
from itertools import product

import pytest

from test_x03_offline_drawing_author import invoke
from tools_fallback import author_tool


def drawing(active="Roof"):
    def insert(handle, name, layer, rows, columns, degrees):
        return [(0, "INSERT"), (5, handle), (2, name), (8, layer), (10, 0), (20, 0),
                (70, columns), (71, rows), (44, 4), (45, 7), (50, degrees)]

    def block(name, children):
        return [(0, "BLOCK"), (2, name), (10, 0), (20, 0), *children, (0, "ENDBLK")]

    header = [(9, "$CLAYER"), (8, active)] if active else []
    blocks = [*block("PV Module", [(0, "POINT"), (10, 0), (20, 0)]),
              *block("Roof Vent", [(0, "POINT"), (10, 0), (20, 0)]),
              *block("Rack", insert("A2", "PV Module", "0", 3, 2, 45)
                     + insert("A3", "Roof Vent", "0", 1, 1, 0)
                     + insert("A4", "PV Module", "East", 1, 2, 0))]
    tops = insert("B1", "Rack", "Roof", 2, 1, 90) + insert("B2", "Rack", "West", 1, 1, 30)
    records = []
    for section, body in (("HEADER", header), ("BLOCKS", blocks), ("ENTITIES", tops)):
        records += [(0, "SECTION"), (2, section), *body, (0, "ENDSEC")]
    records.append((0, "EOF"))
    return "".join(f"{code}\n{value}\n" for code, value in records).encode()


ASKS = [
    "Count nested panel block arrays on the current layer, including rotated references.",
    "How many PV modules are on the active layer inside repeated racks?",
    "Tally the solar panels on the current layer across nested block references.",
]


@pytest.mark.parametrize("ask", ASKS)
@pytest.mark.parametrize("layer", ["Roof", "West", "East"])
def test_resolve_nested_paths(ask, layer):
    _, source, _ = author_tool(ask)
    status, payload = invoke(source, drawing(layer))
    assert status == "ok", payload
    report = payload[0]
    # Expand each source placement independently in row x column space.
    expected = {}
    for top, top_layer, copies in [("B1", "Roof", 2), ("B2", "West", 1)]:
        for child, actual_layer, shape in [("A2", top_layer, (3, 2)), ("A4", "East", (1, 2))]:
            if actual_layer == layer:
                expected[(top, child)] = len(list(product(range(copies), range(shape[0]), range(shape[1]))))
    assert report["total"] == sum(expected.values())
    assert {tuple(row["path"]): row["multiplicity"] for row in report["instance_paths"]} == expected


def test_nested_missing_current_layer():
    _, source, _ = author_tool(ASKS[0])
    status, payload = invoke(source, drawing(None))
    assert status == "tool_error" and "layer" in str(payload).lower()

@pytest.mark.parametrize("pad_module,expected", [(False, 12), (True, 14)])
def test_reference_index_independent_of_geometry_preview(pad_module, expected):
    raw = drawing()
    # The rack preview ends before its first nested reference. A large detail
    # symbol also has a truncated preview, but contains no references at all.
    points = b"0\nPOINT\n10\n1\n20\n1\n" * 64
    raw = raw.replace(b"0\nINSERT\n5\nA2", points + b"0\nINSERT\n5\nA2")
    detail = b"0\nBLOCK\n2\nDetail\n" + points
    if pad_module:
        detail += b"0\nINSERT\n5\nA8\n2\nPV Module\n8\n0\n70\n2\n"
    raw = raw.replace(b"0\nENDSEC\n0\nSECTION\n2\nENTITIES",
                      detail + b"0\nENDBLK\n0\nENDSEC\n0\nSECTION\n2\nENTITIES")
    raw = raw.replace(b"0\nINSERT\n5\nB1", b"0\nINSERT\n5\nB8\n2\nDetail\n8\nRoof\n0\nINSERT\n5\nB1")
    _, source, _ = author_tool(ASKS[0])
    status, payload = invoke(source, raw)
    assert status == "ok", payload
    assert payload[0]["total"] == expected
