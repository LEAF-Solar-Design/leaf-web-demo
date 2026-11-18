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
