"""Exercise expansion limits on deeply nested clipped block arrays."""
import itertools

import pytest

from test_x03_clipped_block_author import drawing
from test_x03_offline_drawing_author import invoke
from tools_fallback import author_tool


def tower(widths):
    raw = drawing().decode()
    blocks, tail = raw.split("0\nENDSEC\n0\nSECTION\n2\nENTITIES\n", 1)
    entity, objects = tail.split("0\nENDSEC\n", 1)
    for index, width in enumerate(reversed(widths)):
        name = f"Tier {index}"
        blocks += f"0\nBLOCK\n2\n{name}\n10\n0\n20\n0\n{entity}0\nENDBLK\n"
        entity = (f"0\nINSERT\n5\n{0x400+index:X}\n2\n{name}\n8\nRoof\n10\n0\n20\n0\n"
                  f"70\n{width}\n71\n1\n44\n25\n45\n0\n")
    return (blocks + "0\nENDSEC\n0\nSECTION\n2\nENTITIES\n" + entity + "0\nENDSEC\n" + objects).encode()


ASKS = (
    "Count the clipped panels in all nested racks using an expansion budget of 80 placements.",
    "Tally PV modules inside the nested clipping boundaries, expanding at most 77 placements.",
    "Count visible panel insertion points after XCLIP and limit expansion to 1000 placements.",
)


@pytest.mark.parametrize("ask,widths,expected", [
    (ASKS[0], [1, 1, 2, 1, 1, 2], (80, 44)),  # Exact requested limit.
    (ASKS[1], [1, 1, 2], (38, 22)),
    (ASKS[2], [1, 1, 1, 1, 1], (22, 11)),
])
def test_small_array_at_or_under_requested_budget(ask, widths, expected):
    visited = sum(len(list(itertools.product(*(range(w) for w in widths[:n]))))
                  for n in range(1, len(widths)+1))
    visible = 0
    for _ in itertools.product(*(range(w) for w in widths)):
        visited += 2
        for y in range(3):
            for x in range(5):
                visited += 1
                visible += (not (1 < x < 3 and 0 < y < 2) and 2 <= 2+2*x <= 8 and 0 <= y <= 2)
    assert (visited, visible) == expected
    _, source, _ = author_tool(ask)
    status, payload = invoke(source, tower(widths))
    assert status == "ok", payload
    assert payload[0]["total"] == visible
    assert payload[0]["counts_by_layer"] == {"Roof": visible}
    assert payload[0]["expanded_placements"] == visited


@pytest.mark.parametrize("ask,widths,limit", [
    (ASKS[1], [2, 1, 1, 2], 77),
    (ASKS[2], [9]*12, 1000),
])
def test_expansion_exhaustion_refuses_the_entire_count(ask, widths, limit):
    _, source, _ = author_tool(ask)
    outcomes = [invoke(source, tower(widths)) for _ in range(2)]
    assert outcomes[0] == outcomes[1]
    status, error = outcomes[0]
    assert status == "tool_error" and f"expansion budget of {limit} placements" in str(error), error
    assert "counts_by_layer" not in str(error)
