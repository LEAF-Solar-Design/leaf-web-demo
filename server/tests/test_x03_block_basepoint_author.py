"""Independent block-local and world-coordinate endpoint oracles."""
import math
import pytest
from test_x03_offline_drawing_author import invoke
from tools_fallback import author_tool

CASES = [
    ("Measure beam lengths by layer and show world endpoints for the nested members.", (4.12345, -3, 1), "drawing_units", 1),
    ("List the start and end coordinates and beam lengths per layer in centimetres.", (-5, 7.98765, 3), "cm", .1),
    ("Calculate each beam length in metres by layer and include both endpoints.", (10, -8, -.45678), "m", .001),
]


def insert(name, handle, layer, pos, scale, rot, normal, array=()):
    return [(0, "INSERT"), (2, name), (5, handle), (8, layer), *zip((10, 20, 30), pos),
            *zip((41, 42, 43), scale), (50, rot), *zip((210, 220, 230), normal),
            *zip((71, 70, 45, 44), array)]


def block(name, base, children):
    return [(0, "BLOCK"), (2, name), *zip((10, 20, 30), base), *children, (0, "ENDBLK")]


def line(a, b):
    return [(0, "LINE"), (5, "D1"), (8, "0"), *zip((10, 20, 30), a), *zip((11, 21, 31), b)]


def wire(blocks, entities):
    def section(name, tags):
        return [(0, "SECTION"), (2, name), *tags, (0, "ENDSEC")]
    tags = (section("HEADER", [(9, "$INSUNITS"), (70, 4)]) + section("BLOCKS", blocks)
            + section("ENTITIES", entities) + [(0, "EOF")])
    return "".join(f"{k}\n{v}\n" for k, v in tags).encode()


def check(ask, raw, expected, unit, factor):
    _, source, _ = author_tool(ask)
    status, payload = invoke(source, raw)
    assert status == "ok", payload
    result = payload[0]
    assert (result["units"], result["endpoint_units"]) == (unit, "drawing_units")
    members = result["lengths_by_handle"]
    assert set(members) == set(expected)
    for key, (a, b) in expected.items():
        assert len(members[key]["ends"]) == 2
        for got, want in zip(members[key]["ends"], (a, b)):
            assert got == pytest.approx(want, abs=1e-9, rel=0)
        assert members[key][unit] == pytest.approx(math.dist(a, b)*factor)
    assert result["lengths_by_layer"] == pytest.approx(
        {"Roof-Beams": sum(math.dist(a, b) for a, b in expected.values())*factor})


@pytest.mark.parametrize("ask,base,unit,factor", CASES)
def test_nested_basepoints(ask, base, unit, factor):
    a, b = (3, -1, 5), (-2, 6, 0)
    inner = insert("Beam", "A1", "0", (11, 7, -5), (-2, 3, 1), 90, (0, 0, 1))
    outer = insert("Frame", "B1", "Roof-Beams", (8.12345, 3, 4), (1, 2, 3), -90, (0, 3, 4))
    raw = wire(block("Beam", base, line(a, b)) + block("Frame", (2, 3, -4), inner), outer)

    def expand(point):
        x, y, z = (v - origin for v, origin in zip(point, base))
        # Scale, rotate +90 degrees, translate, then subtract the outer base.
        x, y, z = 9 - 3*y, 4 - 2*x, z - 1
        # After the -90 degree rotation and translation, convert OCS to world.
        u, v, w = 8.12345 + 2*y, 3 - x, 4 + 3*z
        return -u, -.8*v + .6*w, .6*v + .8*w

    check(ask, raw, {"B1/A1/D1": (expand(a), expand(b))}, unit, factor)


def test_ellipse_endpoints_after_nonuniform_placement():
    # A quarter ellipse with axes 3 and 2 becomes a radius-6 quarter circle.
    arc = [(0, "ELLIPSE"), (5, "C3"), (8, "0"), (10, 7), (20, -4),
           (11, 3), (21, 0), (40, 2/3), (41, 0), (42, math.pi/2)]
    base = block("Arch", (2, 1, 0), arc)
    placed = insert("Arch", "B1", "Roof-Beams", (10, 20, 3), (2, 3, 1), 90, (0, 0, 1))
    _, source, _ = author_tool("List elliptical beam lengths by layer with their world endpoints.")
    status, payload = invoke(source, wire(base, placed))
    assert status == "ok", payload
    member = payload[0]["lengths_by_handle"]["B1/C3"]
    assert member["ends"][0] == pytest.approx((25, 36, 3), abs=1e-9)
    assert member["ends"][1] == pytest.approx((19, 30, 3), abs=1e-9)
    assert member["drawing_units"] == pytest.approx(3*math.pi, rel=1e-5)
