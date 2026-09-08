"""Hosted CAD contract v3: bounded Create Block as atomic REPLACE."""
from __future__ import annotations

import copy
import math
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "da"))
import intake_parse
import dxf_intake
import intake_dxf
import mutation_plan
import write_loop


def base():
    return {"layers": ["0", "SITE"], "polylines": [
        {"handle": "10", "layer": "SITE", "closed": False,
         "pts": [[12, 23, 0], [17, 23, 0]], "xdata": None}],
        "circles": [{"handle": "11", "layer": "SITE", "c": [11, 24, 0], "r": 2, "nrm": [0, 0, 1]}],
        "properties": {"10": {"aci": 3, "linetype": "Continuous", "lineweight": 25, "rgb": None}},
        "blocks": {}}


def replace():
    return {"block_defs": [{"name": "B", "base": [10, 20, 0], "members": ["10", "11"], "insert": 0}],
            "removed": ["10", "11"], "added": [{"handle": "new-insert", "kind": "INSERT",
                "name": "B", "layer": "0", "pt": [10, 20, 0], "rot": 0, "scale": [1, 1, 1]}]}


@pytest.mark.parametrize("case,rule", [
    ("insert", "kind"), ("bulged", "bulge"), ("tilted", "normal"),
    ("byblock", "ByBlock"), ("dimension", "DIMENSION"), ("removal", "removed"),
    ("ordinal", "ordinal"), ("placement", "match"), ("collision", "collides"),
    ("incomplete", "complete"), ("count", "1..60"), ("modified", "UNCHANGED"),
    ("unsaved", "committed"), ("nested", "model-space"),
])
def test_boundary_refuses_before_mutation(case, rule):
    head, plan = base(), replace()
    if case == "insert":
        head["polylines"][0]["kind"] = "INSERT"
    elif case == "bulged":
        head["polylines"][0]["bulges"] = [0, 0.5]
    elif case == "tilted":
        head["circles"][0]["nrm"] = [0, 1, 0]
    elif case == "byblock":
        head["properties"]["10"]["aci"] = 0
    elif case == "dimension":
        head["dimensions"] = [{"handle": "20", "references": ["10"]}]
    elif case == "removal":
        plan["removed"] = ["11"]
    elif case == "ordinal":
        plan["block_defs"][0]["insert"] = 1
    elif case == "placement":
        plan["added"][0]["pt"] = [0, 0, 0]
    elif case == "collision":
        head["blocks"]["b"] = {"base": [0, 0, 0], "count": 0, "complete": True, "children": []}
    elif case == "incomplete":
        head["blocksCapped"] = 201
    elif case == "count":
        plan["block_defs"][0]["members"] = ["10"] * 61
    elif case == "modified":
        plan["set_layer"] = [{"handle": "10", "layer": "0"}]
    elif case == "unsaved":
        plan["block_defs"][0]["members"] = ["99"]
    elif case == "nested":
        head["polylines"][0]["block"] = "Parent"
    before = copy.deepcopy(head)
    with pytest.raises(ValueError, match=rule):
        write_loop.apply_mutations(head, plan)
    assert head == before


def test_lowering_definition_first_and_all_removals_last():
    canonical = mutation_plan.validate_mutations(base(), replace())
    rows = mutation_plan.emit_plan(canonical, base_sha256="1" * 64).decode().splitlines()
    assert rows == ["LEAF_MUTATION_PLAN|3", "BASE_SHA256|" + "1" * 64,
                    "ADDBLOCKDEF|B|10.000,20.000,0.000|H:10;H:11",
                    "ADDINSERT|0|B|10.000,20.000,0.000|0.000000|1.0000,1.0000,1.0000",
                    "REMOVE|10", "REMOVE|11"]


def test_no_definition_keeps_v2_bytes():
    canonical = mutation_plan.validate_mutations(base(), {"removed": ["10"], "added": [
        {"handle": "line", "kind": "LINE", "layer": "0", "pts": [[0, 0], [1, 2]]}]})
    assert mutation_plan.emit_plan(canonical, base_sha256="1" * 64) == (
        "LEAF_MUTATION_PLAN|2\nBASE_SHA256|" + "1" * 64 + "\nREMOVE|10\nADDLINE|0|0,0,0|1,2,0\n").encode()
    assert not mutation_plan.uses_v3(canonical)
    assert not mutation_plan.uses_v3({"block_defs": []})
    assert mutation_plan.uses_v3({"block_defs": [{"name": "B"}]})


def test_mock_preserves_coordinates_and_hand_derived_expansion():
    result = write_loop.apply_mutations(base(), replace())
    assert not result["polylines"] and not result["circles"]
    insert, = result["inserts"]
    assert [insert[k] for k in ("name", "x", "y", "z", "rot", "scale")] == ["B", 10, 20, 0, 0, [1, 1, 1]]
    block = result["blocks"]["B"]
    line, circle = block["children"]
    assert line["pts"] == [[12, 23, 0], [17, 23, 0]]
    assert circle["c"] == [11, 24, 0]
    assert line["properties"]["aci"] == 3 and line["layer"] == "SITE"
    assert block["complete"] is True and len(block["digest"]) == 16
    def expand(q, insertion, angle, scale):
        x, y = [(q[i] - block["base"][i]) * scale[i] for i in range(2)]
        a = math.radians(angle)
        return [round(insertion[0] + x * math.cos(a) - y * math.sin(a), 6),
                round(insertion[1] + x * math.sin(a) + y * math.cos(a), 6)]
    assert [expand(p, [10, 20], 0, [1, 1]) for p in line["pts"]] == [[12, 23], [17, 23]]
    assert [expand(p, [100, 200], 90, [2, 3]) for p in line["pts"]] == [[91, 204], [91, 214]]
    assert expand(circle["c"], [100, 200], 90, [2, 3]) == [88, 202]


def test_dxf_legs_preserve_child_properties_and_geometry():
    result = write_loop.apply_mutations(base(), replace())
    parsed = dxf_intake.parse_dxf_bytes(intake_dxf.intake_to_dxf(result))
    assert parsed["blocks"]["B"]["children"] == result["blocks"]["B"]["children"]
    assert parsed["blocks"]["B"]["base"] == [10, 20, 0]
    parsed["created"] = result["created"]
    assert write_loop.verify_live_mutation_effects(base(), parsed, mutation_plan.validate_mutations(base(), replace())) is None

    legacy = copy.deepcopy(result)
    for child in legacy["blocks"]["B"]["children"]:
        child.pop("properties")
    parsed_legacy = dxf_intake.parse_dxf_bytes(intake_dxf.intake_to_dxf(legacy))
    assert "blockMembers" not in parsed_legacy
    assert parsed_legacy["blocks"]["B"]["children"] == legacy["blocks"]["B"]["children"]


def reopened():
    return intake_parse.parse_text(
        "GRC|1\nBKEPC|1\nLAYER|0\nLAYER|SITE\nCA|0|301\n"
        "IN|B|0|10,20,0|0|0,0,1|1,1,1|301\n"
        "EP|301|256|~|ByLayer|-1\n"
        "BK|B|10,20,0|2|1\n"
        "BKE|B|LINE|12,23,0|17,23,0|SITE\n"
        "BKE|B|CIRCLE|11,24,0|2|0,0,1|SITE\n"
        "BKEP|B|0|3|Continuous|25|~\n"
        "BKEP|B|1|256|ByLayer|-1|~\n", "canary")


@pytest.mark.parametrize("change", ["none", "property", "removal", "untouched", "handoff"])
def test_reopened_verifier_binds_definition_insert_and_removals(change):
    head, actual = base(), reopened()
    if change == "property":
        actual["blocks"]["B"]["children"][0]["properties"]["aci"] = 1
    elif change == "removal":
        actual["polylines"] = copy.deepcopy(head["polylines"])
    elif change == "untouched":
        head["blocks"]["Old"] = {"base": [0, 0, 0], "count": 0, "complete": True, "children": []}
        actual["blocks"]["Old"] = {"base": [1, 0, 0], "count": 0, "complete": True, "children": []}
    elif change == "handoff":
        actual["created"] = []
    canonical = mutation_plan.validate_mutations(head, replace())
    if change == "none":
        assert not actual.get("parseErrors")
        assert write_loop.verify_live_mutation_effects(head, actual, canonical) is None
        records = ("BK|B|0,0,0|1|1\nBKE|B|LINE|0,0,0|1,0,0|0\n"
                   "BKEP|B|0|3|Continuous|25|~\nBM|10|LINE|0,0,1|0|0\n")
        uncovered = intake_parse.parse_text(records, "legacy")
        assert "properties" not in uncovered["blocks"]["B"]["children"][0]
        assert "blockMembers" not in uncovered
        covered = intake_parse.parse_text("BKEPC|1\n" + records, "covered")
        assert covered["blocks"]["B"]["children"][0]["properties"]["aci"] == 3
        assert covered["blockMembers"]["10"]["kind"] == "LINE"
    else:
        with pytest.raises(ValueError):
            write_loop.verify_live_mutation_effects(head, actual, canonical)


def test_catalogue_beyond_display_cap_is_incomplete():
    head = base()
    head["blocksCapped"] = 201
    head["blocks"] = {f"Old{i}": {"base": [0, 0, 0], "count": 0,
                                  "complete": True, "children": []} for i in range(200)}
    with pytest.raises(ValueError, match="incomplete: 201 definitions, 200 listed"):
        mutation_plan.validate_mutations(head, replace())


def test_dimension_evidence_is_member_specific():
    head = base()
    head["dimensions"] = [{"handle": "20", "references": ["99"]}]
    assert mutation_plan.validate_mutations(head, replace())["block_defs"][0]["name"] == "B"
