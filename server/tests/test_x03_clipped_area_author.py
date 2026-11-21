"""Independently intersect panel rectangles in rack coordinates before scaling to roof area."""
import pytest

from test_x03_clipped_block_author import drawing
from test_x03_offline_drawing_author import invoke
from tools_fallback import author_tool


ASKS = [
    "Count partially clipped panels by visible footprint area on each roof.",
    "Measure the exposed area of PV modules after nested XCLIP filters.",
    "Sum visible panel square units per layer, subtracting the inverted clipping boundary.",
]


def footprints(width, height, origin=-0.25, damaged=False):
    raw = drawing(broken=damaged)
    point = b"0\nPOINT\n10\n0\n20\n0\n"
    outline = [(0, "LWPOLYLINE"), (5, "EA"), (90, 4), (70, 1)]
    for x, y in [(origin, origin), (origin+width, origin),
                 (origin+width, origin+height), (origin, origin+height)]:
        outline += [(10, x), (20, y)]
    # A second roof has the rack hole but no outer clip and a distinct affine
    # determinant. Reporting the combined area on one layer would be wrong.
    objects = b"0\nENDSEC\n0\nSECTION\n2\nOBJECTS\n"
    other = b"0\nINSERT\n5\nB2\n2\nArray\n8\nSouth\n10\n-7\n20\n20\n41\n0.5\n42\n1.5\n50\n37\n"
    raw = raw.replace(objects, other + objects)
    return raw.replace(point, "".join(f"{c}\n{v}\n" for c, v in outline).encode(), 1)


@pytest.mark.parametrize("ask,width,height,offset", [
    (ASKS[0], 1, 1, -.25), (ASKS[1], .75, .5, 0), (ASKS[2], 1.5, .5, -.5),
])
def test_clipped_areas(ask, width, height, offset):
    _, source, _ = author_tool(ask)
    status, payload = invoke(source, footprints(width, height, offset))
    assert status == "ok", payload
    # Pull the parent's [2,8] x [0,2] clip back to rack coordinates.
    # The inner hole [1,3] x [0,2] leaves only [0,1] x [0,2].
    expected, south = [], []
    for row in range(3):
        for col in range(5):
            x, y = col+offset, row+offset
            dx = max(0, min(1, x+width)-max(0, x))
            dy = max(0, min(2, y+height)-max(0, y))
            if dx*dy > 0:
                expected.append(2*dx*dy)
            hole_x = max(0, min(3, x+width)-max(1, x))
            hole_y = max(0, min(2, y+height)-max(0, y))
            south_area = 2*.5*1.5*(width*height - hole_x*hole_y)
            if south_area > 0:
                south.append(south_area)
    result = payload[0]
    assert result.get("area_by_layer") == pytest.approx({"Roof": sum(expected), "South": sum(south)}), result
    assert result["visible_panels"] == len(expected)+len(south)
    assert result["area_units"] == "square drawing units"


@pytest.mark.parametrize("raw", [drawing(), footprints(1, 1, damaged=True),
                                 footprints(1, 1).replace(b"90\n4\n", b"42\n0.5\n90\n4\n")])
def test_unproved_footprint_or_clip_is_refused(raw):
    _, source, _ = author_tool(ASKS[0])
    assert invoke(source, raw)[0] == "tool_error"
