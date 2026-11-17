"""One synthetic local drawing through parser, report, listing and selection."""
from copy import deepcopy
import hashlib
import json
from pathlib import Path

from dxf_intake import parse_dxf_bytes
import tool_loader


TOOL = next(tool for tool in json.loads(
    (Path(__file__).resolve().parents[2] / "engine" / "registry.json").read_text()
)["tools"] if tool["name"] == "count-modules")


def test_normal_intake_dependency_listing_and_explicit_selection_keep_identity():
    raw = ("0\nSECTION\n2\nTABLES\n0\nSTYLE\n2\nNotes\n3\nnotes.shx\n0\nENDSEC\n"
           "0\nSECTION\n2\nBLOCKS\n0\nBLOCK\n2\nPanel\n70\n0\n10\n0\n20\n0\n"
           "0\nLINE\n10\n0\n20\n0\n11\n1\n21\n0\n0\nENDBLK\n0\nENDSEC\n"
           "0\nSECTION\n2\nENTITIES\n0\nINSERT\n5\nA\n8\nEast\n2\nPanel\n10\n0\n20\n0\n70\n3\n71\n2\n"
           "0\nINSERT\n5\nB\n8\nWest\n2\nPanel\n10\n10\n20\n0\n0\nENDSEC\n0\nEOF\n").encode()
    digest = hashlib.sha256(raw).hexdigest()
    intake = parse_dxf_bytes(raw, source_name="synthetic-two-set.dxf")
    preserved = deepcopy(intake)
    assert intake["dwg"] == "synthetic-two-set.dxf"
    assert intake["dependency_report"]["dependencies"] == [
        {"kind": "font", "name": "notes.shx", "disposition": "font remapped", "remapped_to": "sans-serif"}]
    listing = tool_loader.run_tool_dynamic(TOOL, intake, {}, aps_live=False)
    assert listing["ok"]
    assert {row["layer"]: row["count"] for row in listing["result"]["sets"]} == {"East": 6, "West": 1}
    params = {"set_ids": [row["set_id"] for row in listing["result"]["sets"]]}
    envelope = tool_loader.run_tool_dynamic(TOOL, intake, params, aps_live=False)
    assert envelope["ok"] and envelope["result"]["count"] == 7
    assert envelope["overlay"]["highlight_handles"] == ["A", "B"]
    assert {row["layer"] for row in envelope["result"]["selected_sets"]} == {"East", "West"}
    assert intake == preserved and hashlib.sha256(raw).hexdigest() == digest
