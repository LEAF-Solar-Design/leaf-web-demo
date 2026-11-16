"""Local INSERT array contract: weighted modules, original parent highlights."""
from copy import deepcopy
import json
import math
from pathlib import Path

import pytest

from dxf_intake import parse_dxf_bytes
import tool_loader


TOOL = next(tool for tool in json.loads(
    (Path(__file__).resolve().parents[2] / "engine" / "registry.json").read_text()
)["tools"] if tool["name"] == "count-modules")


def selected(intake):
    listing = tool_loader.run_tool_dynamic(TOOL, intake, {}, aps_live=False)
    if not listing["ok"]:
        return listing
    return tool_loader.run_tool_dynamic(TOOL, intake,
        {"set_ids": [row["set_id"] for row in listing["result"]["sets"]]}, aps_live=False)


@pytest.mark.parametrize("angle", [0.0, math.pi / 2, -0.4])
def test_array_rotation_preserves_module_count_and_original_highlight(angle):
    entity = {"layer": "Modules", "name": "Panel", "handle": "A",
              "rows": 2, "columns": 3, "rot": angle, "row_spacing": 5, "column_spacing": 7}
    intake = {"inserts": [entity, {**entity, "handle": "0xa"}]}
    original = deepcopy(intake)
    envelope = selected(intake)
    assert envelope["ok"]
    assert envelope["result"]["count"] == 6
    assert envelope["result"]["selected_sets"][0]["count"] == 6
    assert envelope["overlay"]["highlight_handles"] == ["A"]
    assert intake == original


@pytest.mark.parametrize("field,value", [("rows", 0), ("columns", -1), ("rows", True),
    ("columns", 1.5), ("rows", "2"), ("columns", None), ("rows", 65536)])
def test_invalid_multiplicity_refuses_without_partial_count(field, value):
    entity = {"layer": "Modules", "name": "Panel", "handle": "B", "rows": 2, "columns": 3, field: value}
    envelope = selected({"inserts": [{"layer": "Modules", "name": "Panel", "handle": "A"}, entity]})
    assert not envelope["ok"]
    assert "multiplicity" in envelope["error"]["message"]
    assert not envelope.get("result") and not envelope.get("overlay")


def test_large_uint16_array_counts_without_expanding_cells():
    envelope = selected({"inserts": [{"layer": "Modules", "name": "Panel", "handle": "A",
                                       "rows": 65535, "columns": 65535}]})
    assert envelope["ok"]
    assert envelope["result"]["count"] == 4294836225
    assert envelope["overlay"]["highlight_handles"] == ["A"]


def test_conflicting_array_metadata_for_one_handle_refuses_to_guess():
    base = {"name": "Panel", "handle": "A", "rows": 2, "columns": 3}
    envelope = selected({"inserts": [{**base, "layer": "A"}, {**base, "layer": "B", "rows": 3}]})
    assert not envelope["ok"]
    assert "conflicting" in envelope["error"]["message"].lower()
    assert not envelope.get("result") and not envelope.get("overlay")


def test_normal_local_reader_preserves_dxf_array_multiplicity_and_spacing():
    raw = ("0\nSECTION\n2\nBLOCKS\n0\nBLOCK\n2\nPanel\n70\n0\n10\n0\n20\n0\n"
           "0\nLINE\n10\n0\n20\n0\n11\n1\n21\n0\n0\nENDBLK\n0\nENDSEC\n"
           "0\nSECTION\n2\nENTITIES\n0\nINSERT\n5\nA\n8\nModules\n2\nPanel\n"
           "10\n0\n20\n0\n70\n3\n71\n2\n44\n7\n45\n5\n50\n90\n0\nENDSEC\n0\nEOF\n").encode()
    intake = parse_dxf_bytes(raw)
    insert = intake["inserts"][0]
    assert (insert["rows"], insert["columns"], insert["row_spacing"], insert["column_spacing"]) == (2, 3, 5, 7)
    assert selected(intake)["result"]["count"] == 6
