"""Independent path enumeration through each paper viewport's frozen layer handles."""
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
