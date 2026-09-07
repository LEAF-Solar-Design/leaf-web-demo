"""Named groups in the Leaf contract v3 server round trip."""
from __future__ import annotations

import copy
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "server"))
sys.path.insert(0, str(ROOT / "da"))

import dxf_intake
import intake_dxf
import intake_parse
import mutation_apply
import mutation_plan
import write_loop
from lisp import MAX_SCRIPT_LINE_CHARS


def base():
    return {"layers": ["0"], "polylines": [
        {"handle": "10", "layer": "0", "closed": False,
         "pts": [[0, 0, 0], [3, 0, 0]], "xdata": None}],
        "circles": [{"handle": "11", "layer": "0", "c": [4, 2, 0], "r": 1,
                     "nrm": [0, 0, 1]}]}


def group(name="RACK", members=None):
    return {"name": name, "members": ["10", "11"] if members is None else members}


@pytest.mark.parametrize("case,rule", [
    ("collision", "collides"), ("one", "at least two"),
    ("duplicate", "distinct"), ("removed", "also removed"),
    ("ordinal", "ordinal"), ("child", "model-space"),
    ("paper", "model-space"), ("name", "ASCII"),
])
def test_validation_refuses_invalid_group(case, rule):
    head = base()
    plan = {"added_groups": [group()]}
    if case == "collision":
        head["groups"] = [group()]
        plan["added_groups"][0]["name"] = "rack"
    elif case == "one":
        plan["added_groups"][0]["members"] = ["10"]
    elif case == "duplicate":
        plan["added_groups"][0]["members"] = ["10", "10"]
    elif case == "removed":
        plan["removed"] = ["10"]
    elif case == "ordinal":
        plan["added_groups"][0]["members"] = ["10", {"add": 0}]
    elif case == "child":
        head["blocks"] = {"B": {"children": [{"handle": "11"}]}}
    elif case == "paper":
        head["circles"][0]["paper_space"] = True
    else:
        plan["added_groups"][0]["name"] = "bad|name"
    with pytest.raises(ValueError, match=rule):
        mutation_plan.validate_mutations(head, plan)


def test_replacement_canonicalizes_existing_removal_spelling():
    head = base()
    head["groups"] = [group("Rack")]
    canonical = mutation_plan.validate_mutations(head, {
        "removed_groups": ["rack"], "added_groups": [group("rack")]})
    assert canonical == {"removed_groups": ["RACK"], "added_groups": [group("RACK")]}
    assert mutation_plan.validate_mutations(head, canonical) == canonical


def test_group_capability_is_additive_and_empty_lists_stay_v2():
    assert mutation_plan.uses_v3({"added_groups": [group()]})
    assert mutation_plan.uses_v3({"removed_groups": ["RACK"]})
    assert not mutation_plan.uses_v3({"added_groups": [], "removed_groups": []})
    with pytest.raises(ValueError, match="must exist"):
        mutation_plan.validate_mutations(base(), {"removed_groups": ["missing"]})


def test_lowering_orders_removals_adds_properties_then_groups():
    head = base()
    head["groups"] = [group()]
    canonical = mutation_plan.validate_mutations(head, {
        "removed_groups": ["RACK"],
        "added": [{"handle": "new", "kind": "LINE", "layer": "0",
                   "pts": [[5, 0], [8, 0]], "color": 2}],
        "added_groups": [group(members=["10", {"add": 0}])]})
    assert mutation_plan.emit_plan(canonical, base_sha256="1" * 64).decode() == (
        "LEAF_MUTATION_PLAN|3\nBASE_SHA256|" + "1" * 64 + "\n"
        "REMOVEGROUP|RACK\nADDLINE|0|5,0,0|8,0,0\nSETCOLOR|A:0|2\n"
        "ADDGROUP|RACK|H:10;A:0\n")


def test_mock_group_move_ungroup_and_membership_repair():
    head = base()
    grouped = write_loop.apply_mutations(head, {"added_groups": [group("rack")]})
    assert grouped["groups"][0]["name"] == "RACK"
    assert grouped["polylines"] == head["polylines"]
    assert grouped["circles"] == head["circles"]
    moved = write_loop.apply_mutations(grouped, {"set_points": [
        {"handle": "10", "closed": False, "pts": [[2, -1, 0], [5, -1, 0]]}]})
    assert moved["polylines"][0]["pts"] == [[2, -1, 0], [5, -1, 0]]
    assert moved["groups"] == grouped["groups"]
    ungrouped = write_loop.apply_mutations(moved, {"removed_groups": ["RACK"]})
    assert ungrouped["groups"] == []
    assert ungrouped["polylines"] == moved["polylines"]
    assert ungrouped["circles"] == moved["circles"]
    singleton = write_loop.apply_mutations(grouped, {"removed": ["10"]})
    assert singleton["groups"][0]["members"] == ["11"]
    empty = write_loop.apply_mutations(singleton, {"removed": ["11"]})
    assert empty["groups"] == []
    assert empty["polylines"] == empty["circles"] == []


def test_mock_resolves_canonical_add_ordinal_to_allocated_handle():
    result = write_loop.apply_mutations(base(), {
        "added": [{"handle": "new", "kind": "LINE", "layer": "0", "pts": [[5, 0], [8, 0]]}],
        "added_groups": [group(members=["10", {"add": 0}])]})
    assert result["created"] == [{"ordinal": 0, "handle": "100"}]
    assert result["groups"][0]["members"] == ["10", "100"]


def test_dxf_round_trip_and_dictionary_key_not_description():
    head = base()
    head["groups"] = [group()]
    data = intake_dxf.intake_to_dxf(head)
    assert b"ACAD_GROUP" in data and b"AcDbGroup" in data
    data = data.replace(b"300\n\n", b"300\nDifferent description\n")
    parsed = dxf_intake.parse_dxf_bytes(data)
    assert [(g["name"], g["members"]) for g in parsed["groups"]] == [("RACK", ["10", "11"])]
    assert "groups" not in dxf_intake.parse_dxf_bytes(intake_dxf.intake_to_dxf(base()))


def test_coincident_additions_use_created_ordinal_not_geometry():
    head = base()
    canonical = mutation_plan.validate_mutations(head, {
        "added": [{"handle": h, "kind": "LINE", "layer": "0", "pts": [[5, 0], [8, 0]]}
                  for h in ("20", "21")],
        "added_groups": [group(members=["10", {"add": 1}])]})
    actual = write_loop.apply_mutations(head, canonical)
    actual["created"] = [{"ordinal": 0, "handle": "20"}, {"ordinal": 1, "handle": "21"}]
    write_loop.verify_live_mutation_effects(head, actual, canonical)
    actual["groups"][0]["members"] = ["10", "20"]
    with pytest.raises(ValueError, match="member sets"):
        write_loop.verify_live_mutation_effects(head, actual, canonical)


def test_missing_group_and_missing_created_handoff_are_refused():
    head = base()
    canonical = mutation_plan.validate_mutations(head, {
        "added": [{"handle": "20", "kind": "LINE", "layer": "0", "pts": [[5, 0], [8, 0]]}],
        "added_groups": [group(members=["10", {"add": 0}])]})
    actual = write_loop.apply_mutations(head, canonical)
    without_group = copy.deepcopy(actual)
    without_group.pop("groups")
    with pytest.raises(ValueError, match="member sets"):
        write_loop.verify_live_mutation_effects(head, without_group, canonical)
    actual.pop("created")
    with pytest.raises(ValueError, match="handoff"):
        write_loop.verify_live_mutation_effects(head, actual, canonical)


def test_untouched_group_and_v2_intake_still_verify():
    head = base()
    canonical = mutation_plan.validate_mutations(head, {"set_circle": [{"handle": "11", "c": [4, 2], "r": 2}]})
    write_loop.verify_live_mutation_effects(head, write_loop.apply_mutations(head, canonical), canonical)
    head["groups"] = [group()]
    actual = write_loop.apply_mutations(head, canonical)
    write_loop.verify_live_mutation_effects(head, actual, canonical)
    actual["groups"][0]["members"] = ["10"]
    with pytest.raises(ValueError, match="member sets"):
        write_loop.verify_live_mutation_effects(head, actual, canonical)


def test_inspection_records_and_malformed_group(tmp_path):
    path = tmp_path / "intake.txt"
    path.write_text("GR|30|R%25ACK|31|0|1|10;11\nGM|10|30\nCA|1|21\n")
    actual = intake_parse.parse(path, "test")
    assert actual["groups"] == [{"handle": "30", "name": "R%ACK", "owner": "31",
                                 "flags": 0, "selectable": 1, "members": ["10", "11"]}]
    assert actual["created"] == [{"ordinal": 1, "handle": "21"}]
    assert actual["group_memberships"] == [{"member": "10", "group": "30"}]
    path.write_text("GR|30|RACK|31|0|1|not-hex\n")
    assert intake_parse.parse(path, "test")["parseErrors"][0].startswith("GR:")
    path.write_text("")
    for empty in (intake_parse.parse(path, "test"), intake_parse.parse_text("", "test")):
        assert empty["groups"] == []
        assert empty["created"] == []


def test_contracts_share_group_inspection_and_v3_writes_receipt():
    v2 = mutation_apply.activity_spec(2)["settings"]
    v3 = mutation_apply.activity_spec(3)["settings"]
    assert v2["inspectScript"] == v3["inspectScript"]
    assert '"GR|"' in v3["inspectScript"]["value"]
    assert '"ACAD_GROUP"' in v3["inspectScript"]["value"]
    assert "created-handles.txt" in v3["script"]["value"]
    assert "created-handles.txt" in v3["inspectScript"]["value"]
    remove = next(line for line in v3["script"]["value"].splitlines()
                  if line.startswith("(defun leaf-removegroup-op"))
    assert "dictremove" in remove and "entdel" not in remove
    assert all(len(line) <= MAX_SCRIPT_LINE_CHARS
               for setting in v3.values() for line in setting["value"].splitlines())
