"""Panel areas from an L-shaped retained boundary and two nested XCLIP exclusions."""
import pytest

from test_x03_offline_drawing_author import invoke
from tools_fallback import author_tool


CASES = [
    ("Subtract clipped holes from each panel footprint and report the net visible area by roof.", False, 0),
    ("Measure each PV module's area after the concave boundary and interior exclusion loops.", True, 1),
    ("Give the clipped area per panel and the roof total after removing both cutouts.", False, -1),
]


def geometry(reverse=False, shift=0, crossed=False):
    tags = []

    def record(kind, *fields):
        tags.extend([(0, kind), *fields])

    def coords(points):
        return [tag for x, y in points for tag in ((10, x), (20, y))]

    def xdict(handle):
        return [(102, "{ACAD_XDICTIONARY"), (360, handle), (102, "}")]

    def insert(handle, block, layer, x, extension=None):
        record("INSERT", (5, handle), (2, block), (8, layer), (10, x), (20, 0),
               *(xdict(extension) if extension else []))

    record("SECTION", (2, "BLOCKS"))
    record("BLOCK", (2, "PV panel"), (10, 0), (20, 0))
    record("LWPOLYLINE", (5, "A0"), (90, 4), (70, 1), *coords([(0, 0), (4, 0), (4, 8), (0, 8)]))
    record("ENDBLK")
    for name, members in [
        ("Row", [(f"A{i+1}", "PV panel", 4*i+shift, None) for i in range(3)]),
        ("Trim", [("B1", "Row", 0, "E0")]),
        ("Rack", [("C1", "Trim", 0, "D0")]),
    ]:
        record("BLOCK", (2, name), (10, 0), (20, 0))
        for handle, child, x, ext in members:
            insert(handle, child, "0", x, ext)
        record("ENDBLK")
    record("ENDSEC")
    record("SECTION", (2, "ENTITIES"))
    insert("F1", "Rack", "West roof", 10, "90")
    record("ENDSEC")
    record("SECTION", (2, "OBJECTS"))

    def directory(handle, owner, key, child):
        record("DICTIONARY", (5, handle), (330, owner), (3, key), (360, child))

    outer = [(0, 0), (8, 0), (8, 4), (4, 4), (4, 8), (0, 8)]
    hole_a = [(1, 1), (3, 1), (3, 2), (2, 2), (2, 3), (1, 3)]
    hole_b = [(3, 2.5), (5, 2.5), (5, 3.5), (3, 3.5)]
    if crossed:
        hole_b[1], hole_b[2] = hole_b[2], hole_b[1]
    matrix = (1, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1, 0)
    for prefix, owner, points, inverted in [
        ("9", "F1", outer, False), ("D", "C1", hole_a, True), ("E", "B1", hole_b, True)
    ]:
        if reverse:
            points = points[::-1]
        directory(prefix+"0", owner, "ACAD_FILTER", prefix+"1")
        directory(prefix+"1", prefix+"0", "SPATIAL", prefix+"2")
        extent = [(-1, -1), (-1, 9), (9, 9), (9, -1), (-1, -1)]
        if reverse:
            extent.reverse()
        boundary = points + [points[0]] + extent + [points[0]] if inverted else points
        record("SPATIAL_FILTER", (5, prefix+"2"), (330, prefix+"1"),
               *(xdict(prefix+"3") if inverted else []), (70, len(boundary)), *coords(boundary),
               (71, 1), *((40, v) for v in matrix*2))
        if inverted:
            directory(prefix+"3", prefix+"2", "ACAD_XREC_ROUNDTRIP", prefix+"4")
            record("XRECORD", (5, prefix+"4"), (330, prefix+"3"),
                   (102, "ACAD_INVERTEDCLIP_ROUNDTRIP"), *coords(points),
                   (102, "ACAD_INVERTEDCLIP_ROUNDTRIP_COMPARE"), *coords(boundary))
    record("ENDSEC")
    record("EOF")
    return "".join(f"{code}\n{value}\n" for code, value in tags).encode()


@pytest.mark.parametrize("ask,reverse,shift", CASES)
def test_net_clip_areas(ask, reverse, shift):
    _, source, _ = author_tool(ask)
    status, payload = invoke(source, geometry(reverse, shift))
    assert status == "ok", payload
    result = payload[0]
    expected = {}
    # Independent disjoint rectangle decomposition. Panels span y=[0,8].
    for i in range(3):
        left = i*4 + shift
        def strip(x0, x1, height):
            return max(0, min(left+4, x1)-max(left, x0))*height
        outer = strip(0, 4, 8) + strip(4, 8, 4)
        cutouts = strip(1, 3, 1) + strip(1, 2, 1) + strip(3, 5, 1)
        expected[f"F1[0,0]/C1[0,0]/B1[0,0]/A{i+1}[0,0]"] = outer-cutouts
    assert result.get("area_by_placement") == pytest.approx(expected)
    assert result["area_by_layer"] == pytest.approx({"West roof": sum(expected.values())})
    assert result["visible_panels"] == sum(area > 0 for area in expected.values())
    reversed_status, reversed_payload = invoke(source, geometry(not reverse, shift))
    assert reversed_status == "ok", reversed_payload
    assert reversed_payload[0]["area_by_placement"] == pytest.approx(expected)
    assert reversed_payload[0]["area_by_layer"] == pytest.approx(result["area_by_layer"])


def test_self_crossing_interior_loop_refuses_entire_report():
    _, source, _ = author_tool(CASES[0][0])
    status, payload = invoke(source, geometry(crossed=True))
    assert status == "tool_error" and "intersect" in str(payload).lower()
