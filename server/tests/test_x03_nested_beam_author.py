"""Resolve nested beam lengths through DXF intake, the ordinary author and sandbox."""
import math

import pytest

from test_x03_offline_drawing_author import invoke
from tools_fallback import author_tool


ASKS = [
    ("List beam centerline lengths by layer after resolving nested blocks and drawing units.", "drawing_units", 1),
    ("Sum beam lengths per layer in metres, including nested block references.", "m", .001),
    ("List each beam length by layer in centimetres across nested blocks.", "cm", .1),
]


def drawing():
    def line(handle, layer, a, b):
        return [(0, "LINE"), (5, handle), (8, layer), *zip((10, 20, 30), a), *zip((11, 21, 31), b)]

    def insert(handle, name, layer, point, scale, columns=1):
        return [(0, "INSERT"), (5, handle), (2, name), (8, layer), *zip((10, 20, 30), point),
                *zip((41, 42, 43), scale), (50, 90), (70, columns), (44, 50)]

    def block(name, base, children):
        return [(0, "BLOCK"), (2, name), *zip((10, 20, 30), base), *children, (0, "ENDBLK")]

    section = lambda name, body: [(0, "SECTION"), (2, name), *body, (0, "ENDSEC")]
    records = (section("HEADER", [(9, "$INSUNITS"), (70, 4)])
               + section("BLOCKS", block("BeamPart", (2, 3, 4),
                                         line("D1", "0", (2, 3, 4), (5, 7, 10))
                                         + line("D2", "Beams West", (2, 3, 4), (2, 8, 4))
                                         + line("D3", "Wall", (0, 0, 0), (100, 0, 0)))
                         + block("Frame", (1, 2, 3),
                                 insert("C1", "BeamPart", "0", (1, 2, 3), (2, 1, 1), 2)))
               + section("ENTITIES", insert("B1", "Frame", "Beams East", (20, 30, 40), (3, 4, 2))
                         + line("A1", "Beams East", (0, 0, 0), (0, 0, 9)))
               + [(0, "EOF")])
    return "".join(f"{code}\n{value}\n" for code, value in records).encode()


@pytest.mark.parametrize("ask,unit,factor", ASKS)
def test_nested_beam_lengths(ask, unit, factor):
    _, source, _ = author_tool(ask)
    status, payload = invoke(source, drawing())
    assert status == "ok", payload
    result = payload[0]
    # Independent world endpoints after both quarter turns. The other array copy
    # has a translated origin and the same centerline lengths.
    east = math.dist((20, 30, 40), (-4, 18, 52))
    west = math.dist((20, 30, 40), (20, 15, 40))
    assert result["units"] == unit
    assert result["lengths_by_layer"] == pytest.approx(
        {"Beams East": (9 + 2 * east) * factor, "Beams West": 2 * west * factor})
    if ask.startswith("List"):
        members = result["lengths_by_handle"]
        assert set(members) == {"A1", "B1/C1[0,0]/D1", "B1/C1[0,0]/D2",
                                "B1/C1[0,1]/D1", "B1/C1[0,1]/D2"}
        assert sorted(row[unit] for row in members.values()) == pytest.approx(
            sorted(x * factor for x in (9, east, east, west, west)))

def test_nested_insert_array_world_endpoints():
    import dxf_intake
    from authored_block_transform import TRANSFORM_SOURCE

    helpers = {"math": math}
    exec(TRANSFORM_SOURCE, helpers)
    intake = dxf_intake.parse_dxf_bytes(drawing())
    blocks = intake["blocks"]
    outer = helpers["insert_frame"](helpers["identity_frame"](), intake["inserts"][0], blocks["Frame"]["base"])
    second = helpers["insert_frame"](outer, blocks["Frame"]["nested_inserts"][0],
                                     blocks["BeamPart"]["base"], column=1)
    assert helpers["transform_point"](second, (2, 3, 4)) == pytest.approx((-180, 30, 40), abs=.0002)
    assert helpers["transform_point"](second, (5, 7, 10)) == pytest.approx((-204, 18, 52), abs=.0002)
