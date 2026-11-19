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
