"""Saved geometry graphs need more nodes than individual tool requests."""
import pytest

import solar_design_graph as design
from test_w1_design_graph import graph  # noqa: F401


def test_saved_graph_roundtrips_above_request_node_budget(graph):
    # Imported modules plus sparse group matrices cross 500k nodes on ordinary roofs.
    graph["extra"]["preserved_geometry_metadata"] = [[None] * 10000 for _ in range(60)]
    restored = design.deserialize_graph(design.serialize_graph(graph))
    assert restored == graph
    restored["extra"]["preserved_geometry_metadata"][0][0] = True
    assert graph["extra"]["preserved_geometry_metadata"][0][0] is None


def test_tool_payload_node_budget_stays_at_500k():
    with pytest.raises(design.GraphValidationError, match="GRAPH_LIMIT_EXCEEDED"):
        design._bounded_json([[None] * 10000 for _ in range(50)])


def test_saved_graph_node_budget_is_still_bounded(graph):
    graph["extra"]["oversized"] = [[None] * 10000 for _ in range(100)]
    with pytest.raises(design.GraphValidationError, match="GRAPH_LIMIT_EXCEEDED"):
        design.validate_graph(graph)


@pytest.mark.parametrize("limit", ["bytes", "depth"])
def test_graph_byte_and_depth_limits_remain(graph, limit):
    extra = "x" * (16 * 1024 * 1024) if limit == "bytes" else None
    if limit == "depth":
        for _ in range(32):
            extra = [extra]
    graph["extra"]["oversized"] = extra
    with pytest.raises(design.GraphValidationError, match="GRAPH_LIMIT_EXCEEDED"):
        design.validate_graph(graph)
