import pytest
from benches.x03_layer_count_oracle import layer_count_quality


@pytest.mark.parametrize("layer,expected", [("roof",2), ("east",4), ("empty",0)])
def test_counts_only_matching_block_instances(layer, expected):
    doc = pytest.importorskip("ezdxf").new()
    doc.blocks.new("Panel").add_line((0,0),(1,0))
    doc.blocks.new("Other")
    space = doc.modelspace()
    for i, (name, target) in enumerate([("Panel","ROOF"),("Panel","roof"),("Panel","east"),("Other","east")]):
        space.add_blockref(name, (i,0), dxfattribs={"layer":target})
    entities = list(space)
    entities[2].grid((2, 2), (1, 1))
    entities.append(entities[0])
    assert layer_count_quality(entities, layer, "panel", expected) == "PASS"
    assert layer_count_quality(entities, layer, "panel", expected+1) == "FAIL"


@pytest.mark.parametrize("change", [{"layer":"other"}, {"column_count":0}, {"row_count":-2}])
def test_ambiguous_count_in_either_order(change):
    from types import SimpleNamespace
    class Attr(dict):
        __getattr__ = dict.__getitem__
    def entity(**override):
        attrs = dict(layer="roof", name="panel", handle="a")
        attrs.update(override)
        return SimpleNamespace(dxftype=lambda:"INSERT", dxf=Attr(attrs))
    entities = [entity(), entity(**change)]
    assert layer_count_quality(entities,"roof","panel",1) == "INCOMPLETE"
    assert layer_count_quality(reversed(entities),"roof","panel",1) == "INCOMPLETE"


@pytest.mark.parametrize("query,count", [("roof",2),("east",1),("other",0)])
def test_nested_panel_layer_inheritance(query,count):
    doc = pytest.importorskip("ezdxf").new()
    doc.blocks.new("panel")
    inner = doc.blocks.new("inner")
    inner.add_blockref("panel",(0,0)).grid((2,1),(1,1))
    outer = doc.blocks.new("outer")
    outer.add_blockref("inner",(0,0))
    outer.add_blockref("panel",(0,0),dxfattribs={"layer":"east"})
    space = doc.modelspace()
    space.add_blockref("outer",(0,0),dxfattribs={"layer":"roof"})
    assert layer_count_quality(space,query,"panel",count) == "PASS"
    ref = inner.add_blockref("outer",(0,0))
    assert layer_count_quality(space,query,"panel",count) == "INCOMPLETE"
    ref.dxf.name = "missing"
    assert layer_count_quality(space,query,"panel",count) == "INCOMPLETE"
