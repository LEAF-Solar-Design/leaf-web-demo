"""Expand each rotated MINSERT grid slot using signed array spacing."""
import pytest
from test_x03_block_basepoint_author import block, check, insert, line, wire

CASES = [
    ("List beam lengths by layer with world endpoints for each array copy.", 2, 3, -5.7, 8.3, "drawing_units", 1),
    ("Show the endpoints and beam lengths per layer in centimetres across all rows.", 3, 2, 2.9, -4.1, "cm", .1),
    ("Measure beam lengths by layer in metres and give start and end coordinates for every instance.", 2, 4, -3.1, -7.2, "m", .001),
]


@pytest.mark.parametrize("ask,rows,cols,dy,dx,unit,factor", CASES)
def test_rotated_minsert_instances(ask, rows, cols, dy, dx, unit, factor):
    a, b = (2, 1, -4), (8, 7, 3)
    placement = insert("Truss", "B2", "Roof-Beams", (8, -7.12345, 1), (-2, 3, 4),
                       90, (0, 0, 1), (rows, cols, dy, dx))
    raw = wire(block("Truss", (3, 2, -1), line(a, b)), placement)

    def expand(p, row, column):
        x, y, z = p
        return (8 - 3*(y-2) - dy*row, -7.12345 - 2*(x-3) + dx*column, 1 + 4*(z+1))

    expected = {f"B2[{r},{c}]/D1": (expand(a, r, c), expand(b, r, c))
                for r in range(rows) for c in range(cols)}
    check(ask, raw, expected, unit, factor)
