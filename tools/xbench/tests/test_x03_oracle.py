import importlib.util
from pathlib import Path

import pytest

ezdxf = pytest.importorskip("ezdxf")

spec = importlib.util.spec_from_file_location("x03_oracle", Path(__file__).parents[1] / "benches/x03_oracle.py")
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


def test_queries_use_model_space_and_keep_blocks_unexpanded(tmp_path):
    doc = ezdxf.new()
    msp = doc.modelspace()
    line = msp.add_line((0, 0), (3, 4), dxfattribs={"layer": "steel"})
    doc.blocks.new("module").add_line((0, 0), (100, 0))
    ref = msp.add_blockref("module", (0, 0))
    doc.layout().add_line((0, 0), (1000, 0))
    assert module.oracle(doc, {"operation": "count"}) == 2
    assert module.oracle(doc, {"operation": "line_length"}) == 5
    assert module.oracle(doc, {"operation": "blocks"}) == {"module": 1}
    assert module.oracle(doc, {"operation": "handles", "type": "INSERT"}) == [ref.dxf.handle]
    assert module.oracle(doc, {"operation": "bounds", "layer": "steel"}) == [0, 0, 3, 4]


def test_fifty_rows_are_deterministic_and_have_frozen_trade_split(tmp_path):
    path = tmp_path / "synthetic.dxf"
    doc = ezdxf.new()
    for i in range(3):
        doc.modelspace().add_line((0, i), (i+1, i+1), dxfattribs={"layer": f"L{i}"})
    doc.saveas(path)
    drawings = [{"id": str(i), "trade": trade, "dxf_path": str(path)} for i, trade in
                enumerate(("solar", "solar", "structural", "structural", "mechanical"))]
    first = module.build_rows(drawings)
    assert first == module.build_rows(drawings)
    assert len(first) == 50
    assert len({row["id"] for row in first}) == 50
    assert all(row["oracle_method"]["dxf_sha256"] == module.digest(path) for row in first)
    with pytest.raises(ValueError, match="requires two solar"):
        module.build_rows(drawings[:-1])
