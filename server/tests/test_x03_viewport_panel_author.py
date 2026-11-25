"""Independent path enumeration for paper viewport layers, crops, and cameras."""
import math

import pytest

from test_x03_oblique_spline_author import section
from test_x03_offline_drawing_author import invoke, tools_fallback


ASKS = [
    "Count visible panels by layer visibility in viewport A1.",
    "Which PV modules are on thawed layers in viewport handle A2?",
    "List panel placements visible under the selected viewport's frozen-layer settings.",
]


def drawing(frozen="12"):
    tables = [(0,"TABLE"), (2,"LAYER"), (70,5)]
    for handle, name, flags in [("10","0",0), ("11","Roof",0), ("12","Shade",0),
                                ("13","Service",0), ("14","Retired",1)]:
        tables.extend([(0,"LAYER"), (5,handle), (2,name), (70,flags), (62,7)])
    tables.append((0,"ENDTAB"))

    def insert(handle, name, layer, columns=1):
        return [(0,"INSERT"), (5,handle), (2,name), (8,layer), (10,0), (20,0),
                (70,columns), (44,2)]

    def view(handle, number, frozen_handles):
        return [(0,"VIEWPORT"), (5,handle), (100,"AcDbEntity"), (67,1), (410,"Sheet"),
                (100,"AcDbViewport"), (10,0), (20,0), (40,200), (41,200), (45,200),
                (68,1), (69,number), *((331,h) for h in frozen_handles)]

    blocks = [(0,"BLOCK"), (2,"PV Module"), (10,0), (20,0), (0,"POINT"), (10,0), (20,0),
              (0,"ENDBLK"), (0,"BLOCK"), (2,"Rack"), (10,0), (20,0),
              *insert("B1", "PV Module", "0", 2), *insert("B2", "PV Module", "Shade"),
              (0,"ENDBLK")]
    entities = (insert("C1","Rack","Roof") + insert("C2","PV Module","Service")
                + insert("C3","PV Module","Retired")
                + insert("C4","PV Module","Shade")
                + view("A1",2,[frozen]) + view("A2",3,["11","13"]))
    tags = section("TABLES", tables) + section("BLOCKS", blocks) + section("ENTITIES", entities) + [(0,"EOF")]
    return "".join(f"{c}\n{v}\n" for c,v in tags).encode()


@pytest.mark.parametrize("ask,handle,params", [(ASKS[0],"A1",{}), (ASKS[1],"A2",{}),
                                             (ASKS[2],"A1",{"viewport_handle":"a1"})])
def test_viewport_layer_paths(ask, handle, params):
    _, source, _ = tools_fallback.author_tool(ask)
    frozen = "13" if params else "12"
    status, payload = invoke(source, drawing(frozen), params)
    assert status == "ok", payload
    # Both child inserts of C1 require the Roof layer. Layer 0 inherits it.
    layers_by_path = {"C1[0,0]/B1[0,0]": {"Roof"}, "C1[0,0]/B1[0,1]": {"Roof"},
                      "C1[0,0]/B2[0,0]": {"Roof","Shade"}, "C2[0,0]": {"Service"},
                      "C3[0,0]": {"Retired"}, "C4[0,0]": {"Shade"}}
    hidden_a1 = {"Service"} if frozen == "13" else {"Shade"}
    hidden = {"Retired"} | (hidden_a1 if handle == "A1" else {"Roof","Service"})
    expected = sorted(p for p,layers in layers_by_path.items() if hidden.isdisjoint(layers))
    assert payload[0]["viewport_handle"] == handle
    assert payload[0]["visible_paths"] == expected
    assert payload[0]["total"] == len(expected)


@pytest.mark.parametrize("ask,params,raw", [(ASKS[2],{},drawing()), (ASKS[2],{"viewport_handle":"F1"},drawing()),
                                          (ASKS[0],{"viewport_handle":"A2"},drawing()), (ASKS[0],{},drawing("FF"))])
def test_refuse_missing_identity_or_unresolved_layer(ask, params, raw):
    _, source, _ = tools_fallback.author_tool(ask)
    status, payload = invoke(source, raw, params)
    assert status == "tool_error" and "viewport" in str(payload).lower(), payload


def crop_drawing(service_x, viewport_tags=(), boundary=()):
    raw = drawing()
    service = b"5\nC2\n2\nPV Module\n8\nService\n10\n0\n"
    assert raw.count(service) == 1
    raw = raw.replace(service, service[:-2] + f"{service_x}\n".encode())
    view = b"69\n2\n331\n12\n"
    tags = list(viewport_tags)
    if boundary:
        tags.append((340, "F0"))
        outline = [(0, "LWPOLYLINE"), (5, "F0"), (67, 1), (410, "Sheet"),
                   (8, "0"), (70, 1), (90, len(boundary))]
        outline.extend(pair for x, y in boundary for pair in ((10, x), (20, y)))
        raw = raw.replace(b"0\nENDSEC\n0\nEOF\n",
                          "".join(f"{c}\n{v}\n" for c, v in outline).encode()
                          + b"0\nENDSEC\n0\nEOF\n")
    assert raw.count(view) == 1
    return raw.replace(view, view + "".join(f"{c}\n{v}\n" for c, v in tags).encode())


@pytest.mark.parametrize("ask,raw,expected", [
    ("Count only thawed panels that also lie inside the viewport crop polygon.",
     crop_drawing(1000, boundary=((-10, -10), (10, -10), (10, 10), (-10, 10))),
     ["C1[0,0]/B1[0,0]", "C1[0,0]/B1[0,1]"]),
    ("Which PV modules on unfrozen layers are inside the viewport A1 clipping boundary?",
     crop_drawing(3, boundary=((-1, -1), (4, -1), (4, -.5), (1, -.5), (1, 1), (-1, 1))),
     ["C1[0,0]/B1[0,0]"]),
    ("List panel placements on thawed layers inside the crop of viewport A1.",
     crop_drawing(6, ((12, 5), (17, 5), (45, 10))), ["C2[0,0]"]),
])
def test_crop_and_thaw_composition(ask, raw, expected):
    _, source, _ = tools_fallback.author_tool(ask)
    status, payload = invoke(source, raw, {"viewport_handle": "A1"})
    assert status == "ok", payload
    assert payload[0]["visible_paths"] == expected
    assert payload[0]["total"] == len(expected)
    assert "anchors" in payload[0]["scope"]


@pytest.mark.parametrize("tags,expected", [
    (((340, "FF"),), "missing or ambiguous"),
    (((90, 1),), "unsupported perspective viewport calibration"),
    (((36, 0),), "finite view direction"),
    (((90, 22), (43, -2), (44, 2)), "invalid viewport depth planes"),
])
def test_crop_evidence_refused_without_changing_layer_count(tags, expected):
    raw = crop_drawing(6, tags)
    _, cropped, _ = tools_fallback.author_tool(
        "Count thawed panels in the viewport A1 crop.")
    status, payload = invoke(cropped, raw)
    assert status == "tool_error" and expected in str(payload).lower(), payload
    _, layer_only, _ = tools_fallback.author_tool(ASKS[0])
    status, payload = invoke(layer_only, raw)
    assert status == "ok" and payload[0]["total"] == 3, payload


def with_module_footprint(raw, half_width):
    placeholder = b"0\nPOINT\n10\n0\n20\n0\n"
    assert raw.count(placeholder) == 1
    outline = [(0, "LWPOLYLINE"), (5, "D1"), (8, "0"), (90, 4), (70, 1)]
    outline.extend(pair for x, y in ((-half_width, -.25), (half_width, -.25),
                                     (half_width, .25), (-half_width, .25))
                   for pair in ((10, x), (20, y)))
    return raw.replace(placeholder, "".join(f"{c}\n{v}\n" for c, v in outline).encode())


@pytest.mark.parametrize("ask,twist,boundary,half_width,expected", [
    (
        "Count only thawed panels whose complete footprints fit the crop of a twisted viewport.",
        90, ((-1, 1), (1, 1), (1, 3), (-1, 3)), .25, ["C1[0,0]/B1[0,1]"],
    ),
    (
        "Which PV modules on unfrozen layers are wholly inside the viewport A1 clipping boundary?",
        -90, ((-1, -4), (1, -4), (1, -2.5), (-1, -2.5)), .25, ["C2[0,0]"],
    ),
    (
        "List panel placements on thawed layers whose footprints are entirely inside viewport A1's crop.",
        180, ((-5, -1), (3, -1), (3, 1), (.5, 1), (.5, 0), (.25, 0), (.25, 1), (-5, 1)),
        2, ["C1[0,0]/B1[0,1]", "C2[0,0]"],
    ),
])
def test_complete_footprints_in_twisted_viewports(ask, twist, boundary, half_width, expected):
    # Positive 90 degrees maps x to +y. Negative 90 maps x to -y.
    # At 180 degrees the first module spans the notch even though all four
    # corners remain inside. The second module stays on the negative-x side
    # and the service module touches the left boundary without crossing it.
    raw = with_module_footprint(crop_drawing(3, ((51, twist),), boundary), half_width)
    _, source, _ = tools_fallback.author_tool(ask)
    status, payload = invoke(source, raw, {"viewport_handle": "A1"})
    assert status == "ok", payload
    assert payload[0]["visible_paths"] == expected
    assert payload[0]["total"] == len(expected)
    assert "complete thawed panel footprints" in payload[0]["scope"]


def test_footprint_request_rejects_missing_outline():
    _, source, _ = tools_fallback.author_tool(
        "Count thawed panels whose complete footprints fit in the viewport A1 crop.")
    status, payload = invoke(source, crop_drawing(3, ((51, 45),)))
    assert status == "tool_error" and "missing complete panel footprint" in str(payload).lower()


@pytest.mark.parametrize("ask,tags,boundary,half_width,tilt_rack,expected", [
    ("Count whole thawed panel footprints inside an oblique orthographic viewport crop.",
     ((26, 3), (36, 4)), ((-3, -1), (-1, -1), (-1, 1), (-3, 1)), .25, False,
     ["C1[0,0]/B1[0,1]"]),
    ("Which unfrozen PV modules are entirely inside the crop of side viewport A1?",
     ((16, 1), (36, 0)), ((-1, -1), (3, -1), (3, 1), (-1, 1)), .25, True,
     ["C1[0,0]/B1[0,0]", "C1[0,0]/B1[0,1]"]),
    ("List thawed panel footprints wholly inside viewport A1's oblique clipping boundary.",
     ((26, 3), (36, 4), (37, 5)),
     ((-5, -4), (3, -4), (3, -2), (.5, -2), (.5, -3), (.25, -3), (.25, -2), (-5, -2)),
     2, False, ["C1[0,0]/B1[0,1]", "C2[0,0]"]),
])
def test_complete_footprints_in_oblique_viewports(ask, tags, boundary, half_width, tilt_rack, expected):
    # For the (0,3,4) view, DCS X = -WCS X and DCS Y = (-4Y + 3Z)/5.
    # A target at z=5 shifts the footprints down three units into the notch.
    # The side view sees the rack in YZ fully and the service module edge-on.
    raw = with_module_footprint(crop_drawing(3, tags, boundary), half_width)
    if tilt_rack:
        rack = b"5\nC1\n2\nRack\n"
        assert raw.count(rack) == 1
        raw = raw.replace(rack, rack + b"210\n1\n220\n0\n230\n0\n")
    _, source, _ = tools_fallback.author_tool(ask)
    status, payload = invoke(source, raw, {"viewport_handle": "A1"})
    assert status == "ok", payload
    assert payload[0]["visible_paths"] == expected
    assert payload[0]["total"] == len(expected)


def perspective_drawing(service_x, boundary, front, back, lens=50, eye=100):
    tags = ((90, 23), (36, eye), (37, 0), (42, lens), (43, front), (44, back))
    raw = with_module_footprint(crop_drawing(service_x, tags, boundary), .25)
    # Square paper viewport: image-plane height is its diagonal divided by sqrt(2).
    view_height = eye * 42 / (lens * math.sqrt(2))
    marker = b"41\n200\n45\n200\n68\n1\n69\n2\n"
    assert raw.count(marker) == 1
    return raw.replace(marker, f"41\n200\n45\n{view_height}\n68\n1\n69\n2\n".encode())


@pytest.mark.parametrize("ask,case,expected", [
    ("Count only complete thawed panel footprints that fit a perspective viewport crop and its depth planes.",
     "target", ["C1[0,0]/B1[0,0]", "C1[0,0]/B1[0,1]"]),
    ("Which unfrozen PV modules fit entirely inside perspective viewport A1's clipping boundary?",
     "far", ["C2[0,0]"]),
    ("List whole panel footprints on thawed layers within viewport A1's perspective crop and front plane.",
     "straddle", ["C2[0,0]"]),
])
def test_complete_footprints_in_perspective_viewports(ask, case, expected):
    square = ((-12, -12), (12, -12), (12, 12), (-12, 12))
    if case == "target":
        # Both array slots are at the target plane and the direct module is offscreen.
        raw = perspective_drawing(1000, square, 2, -2)
    elif case == "far":
        # At z=-100, the module at X=3 shrinks by exactly 1/2. Its paper
        # X bounds are approximately [4.63, 5.47]; the X=2 slot misses the crop.
        raw = perspective_drawing(3, ((4.5, -1), (5.5, -1), (5.5, 1), (4.5, 1)), 2, -120)
        marker = b"5\nC2\n2\nPV Module\n"
        assert raw.count(marker) == 1
        raw = raw.replace(marker, marker + b"30\n-100\n")
    else:
        # Rack-local Y contributes 0.6*Y to world Z. Both rack footprints
        # straddle the front plane at z=0.1 while the service footprint is flat.
        raw = perspective_drawing(3, square, .1, -2)
        marker = b"5\nC1\n2\nRack\n"
        assert raw.count(marker) == 1
        raw = raw.replace(marker, marker + b"210\n0\n220\n3\n230\n4\n")
    _, source, _ = tools_fallback.author_tool(ask)
    status, payload = invoke(source, raw, {"viewport_handle": "A1"})
    assert status == "ok", payload
    assert payload[0]["visible_paths"] == expected
    assert payload[0]["total"] == len(expected)
