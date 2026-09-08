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
from test_save_plan_version import client as plan_client


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


def test_pre_group_validator_unknown_field_guard(monkeypatch):
    # Main's pre-group field set stops here. The validation rows above
    # exercise their stated rules only after this capability guard admits v3.
    monkeypatch.setattr(mutation_plan, "_MUTATION_FIELDS",
                        mutation_plan._MUTATION_FIELDS - {"added_groups", "removed_groups"})
    with pytest.raises(ValueError, match="unknown mutation fields: added_groups"):
        mutation_plan.validate_mutations(base(), {"added_groups": [group()]})


@pytest.mark.parametrize("name", ["RACK;A", "A/B", "A" * 256, chr(92), *'<>":?*|,=`'])
def test_dictionary_name_refused_by_validator_mock_and_dxf(name):
    head = base()
    plan = {"added_groups": [group(name)]}
    with pytest.raises(ValueError, match="group name"):
        mutation_plan.validate_mutations(head, plan)
    with pytest.raises(ValueError, match="group name"):
        write_loop.apply_mutations(head, plan)
    head["groups"] = [group(name)]
    with pytest.raises(intake_dxf.IntakeDxfError, match="safe unique names"):
        intake_dxf.intake_to_dxf(head)


def test_dictionary_name_with_space_is_accepted():
    result = write_loop.apply_mutations(base(), {"added_groups": [group("Rack A")]})
    parsed = dxf_intake.parse_dxf_bytes(intake_dxf.intake_to_dxf(result))
    assert parsed["groups"][0]["name"] == "RACK A"


@pytest.mark.parametrize("name", [" RACK", "RACK ", "   ", " " * 255])
def test_group_edge_whitespace_refused_everywhere(name):
    head = base()
    sentence = "group name must not have leading or trailing whitespace or be whitespace only"
    for operation in ({"added_groups": [group(name)]}, {"removed_groups": [name]}):
        for apply in (mutation_plan.validate_mutations, write_loop.apply_mutations):
            with pytest.raises(ValueError) as error:
                apply(head, operation)
            assert str(error.value) == sentence
    head["groups"] = [group(name)]
    with pytest.raises(intake_dxf.IntakeDxfError, match="safe unique names"):
        intake_dxf.intake_to_dxf(head)


@pytest.mark.parametrize("paper", [True, False])
def test_uploaded_group_addition_requires_model_space(plan_client, tmp_path, monkeypatch, paper):
    import deps
    import jobs
    from test_save_plan_version import _seed_dwg_backed, _checkout, _post, DWG_DRAWING

    monkeypatch.setattr(deps, "APS_LIVE", True)
    monkeypatch.setenv("LEAF_PLAN_LIVE_LEG", "1")
    monkeypatch.setattr(jobs, "job_max_s", lambda: 540)
    submissions = []
    monkeypatch.setattr(jobs, "submit_plan_job",
                        lambda *args, **kwargs: submissions.append(args) or "group-job")
    head = base()
    _seed_dwg_backed(tmp_path, intake=head)
    capability = _checkout(plan_client)
    plan = {"added": [{"handle": "20", "kind": "LINE", "layer": "0",
                       "pts": [[5, 0], [8, 0]]}],
            "added_groups": [group(members=["10", {"add": 0}])]}
    actual = write_loop.apply_mutations(head, plan)
    if paper:
        actual["polylines"][-1].update(space="paper", layout="Layout1")
    data = intake_dxf.intake_to_dxf(actual)
    if paper:
        assert b"67\n1\n410\nLayout1\n" in data
    response = _post(plan_client, DWG_DRAWING, plan, data=data, capability=capability)
    if paper:
        assert response.status_code == 422, response.text
        assert "group member '20' must be a model-space entity" in response.text
        assert not submissions
    else:
        assert response.status_code == 202, response.text
        assert len(submissions) == 1


@pytest.mark.parametrize("ordinal", [False, True])
def test_group_verifier_refuses_resolved_non_model_member(ordinal):
    head = base()
    plan = {"added_groups": [group()]}
    handle, field = "11", "circles"
    if ordinal:
        plan = {"added": [{"handle": "20", "kind": "LINE", "layer": "0",
                           "pts": [[5, 0], [8, 0]]}],
                "added_groups": [group(members=["10", {"add": 0}])]}
        handle, field = "20", "polylines"
    canonical = mutation_plan.validate_mutations(head, plan)
    actual = write_loop.apply_mutations(head, canonical)
    actual[field][-1]["space"] = "paper"
    with pytest.raises(ValueError, match=f"group member '{handle}' must be a model-space entity"):
        write_loop._verify_group_effects(head, actual, canonical)


def test_addition_matcher_includes_space_with_model_default():
    expected = base()["polylines"][0]
    assert write_loop._polyline_effect_matches(expected, dict(expected, space="model"))
    assert not write_loop._polyline_effect_matches(expected, dict(expected, space="paper"))


def test_dxf_skipped_blank_text_does_not_shift_group_handles():
    head = base()
    head["texts"] = [{"handle": "20", "kind": "TEXT", "layer": "0",
                      "pt": [0, 0], "text": "  "}]
    head["circles"].append(dict(head["circles"][0], handle="12"))
    head["groups"] = [group()]
    parsed = dxf_intake.parse_dxf_bytes(intake_dxf.intake_to_dxf(head))
    assert parsed["groups"][0]["members"] == ["10", "11"]
    head["groups"] = [group(members=["10", "20"])]
    with pytest.raises(intake_dxf.IntakeDxfError, match="20.*emitted entity"):
        intake_dxf.intake_to_dxf(head)


def test_dxf_paper_space_round_trip_refuses_mixed_space_group():
    data = ("0\nSECTION\n2\nENTITIES\n"
            "0\nLINE\n5\n10\n8\n0\n67\n1\n410\nLayout1\n"
            "10\n0\n20\n0\n11\n3\n21\n0\n"
            "0\nLINE\n5\n11\n8\n0\n10\n4\n20\n0\n11\n7\n21\n0\n"
            "0\nENDSEC\n0\nEOF\n").encode()
    parsed = dxf_intake.parse_dxf_bytes(data)
    assert parsed["polylines"][0]["space"] == "paper"
    assert parsed["polylines"][0]["layout"] == "Layout1"
    assert "space" not in parsed["polylines"][1]
    assert "layout" not in parsed["polylines"][1]
    emitted = intake_dxf.intake_to_dxf(parsed)
    assert b"67\n1\n410\nLayout1\n" in emitted
    reopened = dxf_intake.parse_dxf_bytes(emitted)
    assert reopened["polylines"] == parsed["polylines"]
    for head in (parsed, reopened):
        with pytest.raises(ValueError, match="model-space"):
            mutation_plan.validate_mutations(head, {"added_groups": [group()]})


def test_uncovered_baseline_accepts_unnamed_group_but_checks_named_group():
    head = base()
    canonical = mutation_plan.validate_mutations(head, {
        "set_circle": [{"handle": "11", "c": [4, 2], "r": 2}]})
    inspection = intake_parse.parse_text("GRC|1\nGR|30|RACK|31|0|1|10;11\n", "test")
    # Keep the geometry from the mutation; the inspection fixture supplies
    # group coverage and records only.
    actual = write_loop.apply_mutations(head, canonical) | {
        "groups": inspection["groups"], "created": inspection["created"]}
    write_loop.verify_live_mutation_effects(head, actual, canonical)
    canonical = mutation_plan.validate_mutations(head, {"added_groups": [group("NEW")]})
    actual = write_loop.apply_mutations(head, canonical)
    actual["groups"].append(group())
    write_loop.verify_live_mutation_effects(head, actual, canonical)
    actual["groups"][0]["members"] = ["10"]
    with pytest.raises(ValueError, match="member sets"):
        write_loop.verify_live_mutation_effects(head, actual, canonical)


def test_untouched_groups_require_coverage_on_both_sides():
    head = base()
    head["groups"] = [group()]
    actual = base()
    write_loop.verify_live_mutation_effects(head, actual, {})
    actual["groups"] = []
    with pytest.raises(ValueError, match="member sets"):
        write_loop.verify_live_mutation_effects(head, actual, {})


def test_group_records_without_marker_do_not_claim_coverage():
    parsed = intake_parse.parse_text("GR|30|RACK|31|0|1|10;11\nCA|0|20\n", "test")
    assert "groups" not in parsed and "created" not in parsed


def test_named_removal_still_checked_without_baseline_coverage():
    head = base()
    canonical = {"removed_groups": ["RACK"]}
    actual = base()
    actual["groups"] = [group()]
    with pytest.raises(ValueError, match="member sets"):
        write_loop._verify_group_effects(head, actual, canonical)
    actual["groups"] = [group("OTHER")]
    write_loop._verify_group_effects(head, actual, canonical)
    actual.pop("groups")
    with pytest.raises(ValueError, match="coverage"):
        write_loop._verify_group_effects(head, actual, canonical)


def test_dictionary_name_lisp_preflight_mirrors_server_rule():
    script = mutation_apply.activity_spec(3)["settings"]["script"]["value"]
    predicate = next(line for line in script.splitlines()
                     if line.startswith("(defun leaf-group-name-p"))
    assert "(<= (strlen s) 255)" in predicate
    assert '(/= (substr s 1 1) " ")' in predicate
    assert '(/= (substr s (strlen s) 1) " ")' in predicate
    assert "(member c (list 60 62 47 92 34 58 59 63 42 124 44 61 96))" in predicate
    add = next(line for line in script.splitlines()
               if line.startswith("(defun leaf-addgroup-op"))
    assert "(leaf-group-name-p name)" in add


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
    assert empty["groups"][0]["name"] == "RACK"
    assert empty["groups"][0]["members"] == []
    assert empty["polylines"] == empty["circles"] == []
    write_loop.verify_live_mutation_effects(singleton, empty, {"removed": ["11"]})


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
    path.write_text("GRC|1\nGR|30|R%25ACK|31|0|1|10;11\nGM|10|30\nCA|1|21\n")
    actual = intake_parse.parse(path, "test")
    assert actual["groups"] == [{"handle": "30", "name": "R%ACK", "owner": "31",
                                 "flags": 0, "selectable": 1, "members": ["10", "11"]}]
    assert actual["created"] == [{"ordinal": 1, "handle": "21"}]
    assert actual["group_memberships"] == [{"member": "10", "group": "30"}]
    path.write_text("GR|30|RACK|31|0|1|not-hex\n")
    assert intake_parse.parse(path, "test")["parseErrors"][0].startswith("GR:")
    path.write_text("")
    for empty in (intake_parse.parse(path, "test"), intake_parse.parse_text("", "test")):
        assert "groups" not in empty
        assert "created" not in empty
    path.write_text("GRC|1\n")
    for empty in (intake_parse.parse(path, "test"), intake_parse.parse_text("GRC|1\n", "test")):
        assert empty["groups"] == []
        assert empty["created"] == []


def test_contracts_share_group_inspection_and_v3_writes_receipt():
    v2 = mutation_apply.activity_spec(2)["settings"]
    v3 = mutation_apply.activity_spec(3)["settings"]
    assert v2["inspectScript"] == v3["inspectScript"]
    assert '(write-line "GRC|1" f)' in v3["inspectScript"]["value"]
    assert '"GR|"' in v3["inspectScript"]["value"]
    assert '"ACAD_GROUP"' in v3["inspectScript"]["value"]
    assert "created-handles.txt" in v3["script"]["value"]
    assert "created-handles.txt" in v3["inspectScript"]["value"]
    remove = next(line for line in v3["script"]["value"].splitlines()
                  if line.startswith("(defun leaf-removegroup-op"))
    assert "dictremove" in remove and "entdel" not in remove
    assert all(len(line) <= MAX_SCRIPT_LINE_CHARS
               for setting in v3.values() for line in setting["value"].splitlines())
