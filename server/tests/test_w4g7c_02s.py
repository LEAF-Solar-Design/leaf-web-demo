"""Hosted CAD contract v3: bounded Create Block as atomic REPLACE."""
from __future__ import annotations

import copy
import hashlib
import io
import json
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
    return {"memberEvidenceCovered": True, "layers": ["0", "SITE"], "polylines": [
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
    ("incomplete", "complete"), ("count", "1..60"), ("modified-property", "colour, linetype and lineweight"),
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
    elif case == "modified-property":
        plan["set_color"] = [{"handle": "10", "aci": 1}]
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
        assert "blockMembers" not in covered
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


@pytest.mark.parametrize("kind", ["LINE", "LWPOLYLINE"])
def test_two_vertex_member_verifies_with_line_or_lwpolyline_bke(kind):
    geometry = ("LN|SITE|12,23,0|17,23,0|10\n" if kind == "LINE" else
                "PL|SITE|0|0|0,0,1|10\nPV|12,23\nPV|17,23\n")
    records = ("MEC|1\nLAYER|0\nLAYER|SITE\n" + geometry
               + f"BM|10|{kind}|0,0,1|0|0|0\n"
               + "CI|SITE|11,24,0|2|0,0,1|11\nBM|11|CIRCLE|0,0,1|0|0|0\n"
               + "EP|10|3|~|Continuous|25\n")
    head = intake_parse.parse_text(records, "canary")
    assert not head.get("parseErrors")
    assert "kind" not in head["polylines"][0]
    canonical = mutation_plan.validate_mutations(head, replace())
    result = write_loop.apply_mutations(head, canonical)
    assert result["blocks"]["B"]["children"][0]["kind"] == "LINE"
    actual = reopened()
    if kind == "LWPOLYLINE":
        child = intake_parse.parse_text(
            "BK|B|10,20,0|1|1\n"
            "BKE|B|LWPOLYLINE|0|0,0,1|0|12,23;17,23;|SITE\n", "canary")
        actual["blocks"]["B"]["children"][0] = {
            **child["blocks"]["B"]["children"][0],
            "properties": actual["blocks"]["B"]["children"][0]["properties"],
        }
    assert write_loop.verify_live_mutation_effects(head, actual, canonical) is None


@pytest.mark.parametrize("closed,points", [(False, 2), (True, 2), (False, 3)])
def test_polyline_records_keep_frozen_shape(closed, points):
    vertices = [[float(i), 0.0, 0.0] for i in range(points)]
    geometry = (f"PL|0|{int(closed)}|0|0,0,1|10\n"
                + "".join(f"PV|{i},0\n" for i in range(points)))
    legacy = intake_parse.parse_text(geometry, "head")
    covered = intake_parse.parse_text("MEC|1\n" + geometry
                                      + "BM|10|LWPOLYLINE|0,0,1|0|0|0\n", "head")
    expected = {"layer": "0", "closed": closed, "pts": vertices, "xdata": None, "handle": "10"}
    assert legacy["polylines"] == [expected]
    assert covered["polylines"] == [expected]
    dxf = ("0\nSECTION\n2\nENTITIES\n0\nLWPOLYLINE\n5\n10\n8\n0\n"
           + f"90\n{points}\n70\n{int(closed)}\n"
           + "".join(f"10\n{i}\n20\n0\n" for i in range(points))
           + "0\nENDSEC\n0\nEOF\n")
    assert dxf_intake.parse_dxf_bytes(dxf.encode())["polylines"] == [expected]


@pytest.mark.parametrize("source", ["dxf", "inspection"])
@pytest.mark.parametrize("code,evidence,rule", [
    (42, {"bulges": [0.5]}, "block LWPOLYLINE must have straight segments, every bulge 0"),
    (40, {"width": True}, "block LWPOLYLINE must have zero constant and vertex width"),
    (41, {"width": True}, "block LWPOLYLINE must have zero constant and vertex width"),
])
def test_classic_polyline_member_refused_without_mutating_document(source, code, evidence, rule):
    if source == "dxf":
        text = ("0\nSECTION\n2\nENTITIES\n0\nPOLYLINE\n5\n10\n8\n0\n70\n0\n"
                f"0\nVERTEX\n10\n0\n20\n0\n{code}\n0.5\n"
                "0\nVERTEX\n10\n1\n20\n0\n0\nSEQEND\n0\nENDSEC\n0\nEOF\n")
        head = dxf_intake.parse_dxf_bytes(text.encode())
    else:
        head = intake_parse.parse_text(
            "MEC|1\nPL|0|0|0|0,0,1|10\nPV|0,0\nPV|1,0\n"
            f"BM|10|POLYLINE|0,0,1|{'0.5' if code == 42 else '0'}|0|{int(code != 42)}\n", "head")
    assert head["polylines"] == [{"layer": "0", "closed": False,
        "pts": [[0.0, 0.0, 0.0], [1.0, 0.0, 0.0]], "xdata": None,
        "handle": "10", **evidence}]
    before = copy.deepcopy(head)
    plan = replace()
    plan["block_defs"][0]["members"] = ["10"]
    plan["removed"] = ["10"]
    with pytest.raises(ValueError, match=rule):
        write_loop.apply_mutations(head, plan)
    assert head == before


@pytest.mark.parametrize("source", ["dxf", "inspection"])
def test_straight_classic_polyline_keeps_frozen_record(source):
    if source == "dxf":
        head = dxf_intake.parse_dxf_bytes(
            b"0\nSECTION\n2\nENTITIES\n0\nPOLYLINE\n5\n10\n8\n0\n70\n0\n"
            b"0\nVERTEX\n10\n0\n20\n0\n42\n0\n40\n0\n41\n0\n"
            b"0\nVERTEX\n10\n1\n20\n0\n0\nSEQEND\n0\nENDSEC\n0\nEOF\n")
    else:
        head = intake_parse.parse_text(
            "MEC|1\nPL|0|0|0|0,0,1|10\nPV|0,0\nPV|1,0\n"
            "BM|10|POLYLINE|0,0,1|0|0|0\n", "head")
    assert head["polylines"] == [{"layer": "0", "closed": False,
        "pts": [[0.0, 0.0, 0.0], [1.0, 0.0, 0.0]], "xdata": None, "handle": "10"}]
    plan = replace()
    plan["block_defs"][0]["members"] = ["10"]
    plan["removed"] = ["10"]
    assert mutation_plan.validate_mutations(head, plan)["block_defs"][0]["members"] == ["10"]


def test_dimension_evidence_is_member_specific():
    head = base()
    head["dimensions"] = [{"handle": "20", "references": ["99"]}]
    assert mutation_plan.validate_mutations(head, replace())["block_defs"][0]["name"] == "B"


def test_missing_member_coverage_is_refused():
    head = base()
    head.pop("memberEvidenceCovered")
    with pytest.raises(ValueError, match="member evidence unavailable in this inspection"):
        mutation_plan.validate_mutations(head, replace())


@pytest.mark.parametrize("groups", [
    {"groups": [{"members": ["10"]}]},
    {"group_memberships": [{"member": "10", "group": "20"}]},
])
def test_grouped_member_requires_ungrouping(groups):
    with pytest.raises(ValueError, match="ungroup it first"):
        mutation_plan.validate_mutations({**base(), **groups}, replace())


@pytest.mark.parametrize("code,value,field,sentence", [
    (42, "0.5", "bulges", "bulge"),
    (230, "-1", "normal", "normal"),
    (43, "2", "width", "zero constant and vertex width"),
    (40, "2", "width", "zero constant and vertex width"),
    (41, "2", "width", "zero constant and vertex width"),
])
def test_blockless_dxf_member_evidence(code, value, field, sentence):
    text = ("0\nSECTION\n2\nENTITIES\n0\nLWPOLYLINE\n5\n10\n8\n0\n"
            "90\n2\n70\n0\n10\n0\n20\n0\n"
            f"{code}\n{value}\n10\n1\n20\n0\n0\nENDSEC\n0\nEOF\n")
    head = dxf_intake.parse_dxf_bytes(text.encode())
    assert head["memberEvidenceCovered"] is True
    assert field in head["polylines"][0]
    plan = replace()
    plan["block_defs"][0]["members"] = ["10"]
    plan["removed"] = ["10"]
    with pytest.raises(ValueError, match=sentence):
        mutation_plan.validate_mutations(head, plan)


@pytest.mark.parametrize("normal,bulges,dimension,width,expected", [
    ("0,0,1", "0;0", "0", "0", {}),
    ("0,1,0", "0;0", "0", "0", {"normal": [0, 1, 0]}),
    ("0,0,1", "0;0.5", "0", "0", {"bulges": [0, 0.5]}),
    ("0,0,1", "0;0", "1", "0", {"dimensionRef": True}),
    ("0,0,1", "0;0", "0", "1", {"width": True}),
])
def test_mec_evidence_is_independent_of_block_properties(normal, bulges, dimension, width, expected):
    geometry = "LN|0|0,0,0|1,0,0|10\n"
    records = geometry + f"BM|10|LINE|{normal}|{bulges}|{dimension}|{width}\n"
    legacy = intake_parse.parse_text(geometry, "head")
    assert intake_parse.parse_text(records, "head") == legacy
    covered = intake_parse.parse_text("MEC|1\n" + records, "head")
    assert covered.pop("memberEvidenceCovered") is True
    legacy["polylines"][0].update(expected)
    assert covered == legacy


@pytest.mark.parametrize("normal,bulges,expected_pts,expected_bulges", [
    ("0,0,-1", "1;0", [[0, 0, 0], [-10, 0, 0]], [-1.0, 0.0]),
    ("0,0,1", "1;0", [[0, 0, 0], [10, 0, 0]], [1.0, 0.0]),
    ("0,0,-1", "0;0", [[0, 0, 0], [-10, 0, 0]], None),
])
def test_mec_polyline_bulges_follow_ocs_reflection(normal, bulges, expected_pts, expected_bulges):
    parsed = intake_parse.parse_text(
        f"MEC|1\nPL|0|0|0|{normal}|10\nPV|0,0\nPV|10,0\n"
        f"BM|10|LWPOLYLINE|{normal}|{bulges}|0|0\n", "head")
    assert not parsed.get("parseErrors")
    polyline, = parsed["polylines"]
    assert polyline["pts"] == expected_pts
    if expected_bulges is None:
        assert "bulges" not in polyline
    else:
        assert polyline["bulges"] == expected_bulges


@pytest.mark.parametrize("member", ["10", "99"])
def test_blockless_dxf_dimension_association_is_member_specific(member):
    text = ("0\nSECTION\n2\nENTITIES\n0\nLINE\n5\n10\n8\n0\n"
            "10\n0\n20\n0\n11\n1\n21\n0\n"
            "0\nDIMENSION\n5\n20\n70\n2\n0\nENDSEC\n"
            "0\nSECTION\n2\nOBJECTS\n0\nDIMASSOC\n5\n30\n330\n20\n"
            f"331\n{member}\n0\nENDSEC\n0\nEOF\n")
    head = dxf_intake.parse_dxf_bytes(text.encode())
    assert ("dimensionRef" in head["polylines"][0]) == (member == "10")
    plan = replace()
    plan["block_defs"][0]["members"] = ["10"]
    plan["removed"] = ["10"]
    if member == "10":
        with pytest.raises(ValueError, match="DIMENSION"):
            mutation_plan.validate_mutations(head, plan)
    else:
        assert mutation_plan.validate_mutations(head, plan)["block_defs"]


def test_insert_ordinal_tracks_submitted_addition_after_sort():
    head, plan = base(), replace()
    plan["added"].append({"handle": "circle", "kind": "CIRCLE", "layer": "0", "c": [0, 0, 0], "r": 1})
    plan["added_groups"] = [{"name": "PAIR", "members": [{"add": 0}, {"add": 1}]}]
    canonical = mutation_plan.validate_mutations(head, plan)
    assert canonical["added"][0]["kind"] == "CIRCLE"
    assert canonical["block_defs"][0]["insert"] == 1
    assert canonical["added_groups"][0]["members"] == [{"add": 1}, {"add": 0}]
    assert mutation_plan.validate_mutations(head, canonical) == canonical
    assert write_loop.apply_mutations(head, canonical)["blocks"]["B"]["complete"]


def test_two_replacement_insert_ordinals_swap():
    head, plan = base(), replace()
    plan["block_defs"][0]["members"] = ["10"]
    plan["added"][0]["handle"] = "z-insert"
    plan["block_defs"].append({"name": "A", "base": [10, 20, 0], "members": ["11"], "insert": 1})
    plan["added"].append({**plan["added"][0], "handle": "a-insert", "name": "A"})
    canonical = mutation_plan.validate_mutations(head, plan)
    assert {b["name"]: b["insert"] for b in canonical["block_defs"]} == {"A": 0, "B": 1}
    assert mutation_plan.validate_mutations(head, canonical) == canonical
    assert set(write_loop.apply_mutations(head, canonical)["blocks"]) == {"A", "B"}


def test_replacement_insert_properties_lower_with_group():
    head, plan = base(), replace()
    head["polylines"].append({"handle": "12", "layer": "0", "closed": False,
                              "pts": [[0, 0, 0], [1, 0, 0]], "xdata": None})
    plan["added"][0].update(color=3, linetype="Continuous", lineweight=25)
    plan["added_groups"] = [{"name": "PAIR", "members": [{"add": 0}, "12"]}]
    canonical = mutation_plan.validate_mutations(head, plan)
    assert mutation_plan.validate_mutations(head, canonical) == canonical
    rows = mutation_plan.emit_plan(canonical, base_sha256="1" * 64).decode().splitlines()
    assert "SETCOLOR|A:0|3" in rows
    assert "SETLINETYPE|A:0|Continuous" in rows
    assert "SETLINEWEIGHT|A:0|25" in rows


def test_ordinary_plan_preserves_untouched_definition_digest():
    head = base()
    head["blocks"]["Old"] = {"base": [0, 0, 0], "count": 0, "complete": True,
                              "children": [], "digest": "1111111111111111"}
    canonical = mutation_plan.validate_mutations(head, {"removed": ["10"]})
    actual = write_loop.apply_mutations(head, canonical)
    actual["blocks"]["Old"]["digest"] = "2222222222222222"
    with pytest.raises(ValueError, match="unchanged block definitions"):
        write_loop.verify_live_mutation_effects(head, actual, canonical)


@pytest.mark.parametrize("damage", ["none", "missing-insert", "retained-original"])
@pytest.mark.parametrize("sidecar", [False, True])
def test_uploaded_block_result_is_bound_on_mock_and_sidecar(tmp_path, monkeypatch, damage, sidecar):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    import deps
    import store
    from envelopes import install_error_handlers
    from routers import drawings

    monkeypatch.setenv("LEAF_STORE_DIR", str(tmp_path / "drawings"))
    monkeypatch.delenv("LEAF_AUTH_LIVE", raising=False)
    monkeypatch.setattr(deps, "APS_LIVE", False)
    app = FastAPI()
    install_error_handlers(app)
    app.include_router(drawings.router)
    client = TestClient(app)
    tenant, drawing = "tenant-block-save", "block-save"
    head, plan = base(), replace()
    # The uploaded DXF and the submitted plan must name the same new handle.
    plan["added"][0]["handle"] = "301"
    backend = store.FilesystemBackend(str(tmp_path / "drawings"))
    source = tmp_path / "base.dwg"
    source.write_bytes(json.dumps(head).encode() if sidecar else b"AC1032" + b"\x00" * 64)
    store.ingest_drawing(backend, tenant, str(source), drawing_id=drawing)
    write_loop.publish_intake_cache(backend, tenant, drawing, 1, source.read_bytes(), head)
    upload = write_loop.apply_mutations(head, plan)
    if damage == "missing-insert":
        upload["inserts"] = []
    elif damage == "retained-original":
        upload["polylines"] = copy.deepcopy(head["polylines"])
    data = intake_dxf.intake_to_dxf(upload)
    checkout = client.post(f"/api/drawings/{drawing}/checkout", headers={"X-Tenant-Id": tenant},
                           json={"holder": "block-editor", "ttl_s": 3600})
    assert checkout.status_code == 200, checkout.text
    response = client.post(f"/api/drawings/{drawing}/versions/plan",
        headers={"X-Tenant-Id": tenant,
                 "X-Checkout-Capability": checkout.json()["checkout_capability"]},
        files={"file": ("edited.dxf", io.BytesIO(data), "application/dxf")},
        data={"parent_version": "1", "source_digest": hashlib.sha256(data).hexdigest(),
              "plan": json.dumps({"mutations": plan})})
    assert response.status_code == (201 if damage == "none" else 422), response.text
    if damage != "none":
        assert "uploaded DXF does not carry the plan's result" in response.text
        assert store.resolve_version(backend, tenant, drawing, "head")[0] == 1


def inline_base():
    return {"memberEvidenceCovered": True, "layers": ["0"], "polylines": [],
            "circles": [{"handle": "11", "layer": "0", "c": [4, 2, 0], "r": 1,
                         "nrm": [0, 0, 1]}], "blocks": {}}


def inline_replace(*, moved=False):
    plan = {"block_defs": [{"name": "B", "base": [1, 1, 0], "members": ["11"],
             "children": [{"kind": "LINE", "layer": "0", "pts": [[0, 0, 0], [3, 0, 0]]}],
             "order": ["C:0", "H:11"], "insert": 0}], "removed": ["11"],
            "added": [{"handle": "new-insert", "kind": "INSERT", "name": "B", "layer": "0",
                       "pt": [1, 1, 0], "rot": 0, "scale": [1, 1, 1]}]}
    if moved:
        plan["set_circle"] = [{"handle": "11", "c": [6, 1, 0], "r": 1}]
        plan["set_layer"] = [{"handle": "11", "layer": "SITE"}]
    return plan


@pytest.mark.parametrize("moved", [False, True])
def test_inline_mixed_hand_derived_plan_and_mock(moved):
    head, plan = inline_base(), inline_replace(moved=moved)
    canonical = mutation_plan.validate_mutations(head, plan)
    assert mutation_plan.validate_mutations(head, canonical) == canonical
    setters = (["RELAYER|11|SITE", "SETCIRCLE|11|6,1,0|1"] if moved else [])
    assert mutation_plan.emit_plan(canonical, base_sha256="1" * 64).decode().splitlines() == [
        "LEAF_MUTATION_PLAN|3", "BASE_SHA256|" + "1" * 64, *setters,
        "BLOCKCHILD|B|0|LINE|0|0,0,0|3,0,0|256|ByLayer|-1",
        "ADDBLOCKDEF|B|1.000,1.000,0.000|C:0;H:11",
        "ADDINSERT|0|B|1.000,1.000,0.000|0.000000|1.0000,1.0000,1.0000", "REMOVE|11"]
    before = copy.deepcopy(head)
    result = write_loop.apply_mutations(head, canonical)
    assert head == before
    block = result["blocks"]["B"]
    assert block["base"] == [1, 1, 0] and block["count"] == 2 and block["complete"] is True
    assert len(block["digest"]) == 16
    line, circle = block["children"]
    defaults = {"aci": 256, "rgb": None, "linetype": "ByLayer", "lineweight": -1}
    assert line == {"kind": "LINE", "layer": "0", "pts": [[0, 0, 0], [3, 0, 0]],
                    "properties": defaults}
    assert circle == {"kind": "CIRCLE", "layer": "SITE" if moved else "0",
                      "c": [6, 1, 0] if moved else [4, 2, 0], "r": 1,
                      "nrm": [0, 0, 1], "properties": defaults}
    assert all("handle" not in child for child in block["children"])
    assert result["polylines"] == [] and result["circles"] == []
    insert, = result["inserts"]
    assert [insert[k] for k in ("name", "x", "y", "z")] == ["B", 1, 1, 0]
    assert result["created"] == [{"ordinal": 0, "handle": insert["handle"]}]


def test_inline_all_new_definition_has_no_removal_or_child_add_ordinals():
    head, plan = inline_base(), inline_replace()
    head["circles"] = []
    definition = plan["block_defs"][0]
    definition["members"] = []
    definition["children"].append({"kind": "CIRCLE", "layer": "0", "c": [4, 2, 0], "r": 1})
    definition["order"] = ["C:0", "C:1"]
    plan["removed"] = []
    canonical = mutation_plan.validate_mutations(head, plan)
    rows = mutation_plan.emit_plan(canonical, base_sha256="1" * 64).decode().splitlines()
    assert rows[2:5] == [
        "BLOCKCHILD|B|0|LINE|0|0,0,0|3,0,0|256|ByLayer|-1",
        "BLOCKCHILD|B|1|CIRCLE|0|4,2,0|1|256|ByLayer|-1",
        "ADDBLOCKDEF|B|1.000,1.000,0.000|C:0;C:1"]
    assert not any(row.startswith("REMOVE|") for row in rows)
    result = write_loop.apply_mutations(head, canonical)
    assert len(result["inserts"]) == len(result["created"]) == 1
    assert result["blocks"]["B"]["count"] == 2
    assert not result["circles"] and not result["polylines"]


def test_inline_children_do_not_shift_unrelated_add_or_insert_receipts():
    head, plan = inline_base(), inline_replace()
    plan["added"].append({"handle": "other-circle", "kind": "CIRCLE", "layer": "0", "c": [9, 9, 0], "r": 2})
    canonical = mutation_plan.validate_mutations(head, plan)
    assert [e["kind"] for e in canonical["added"]] == ["CIRCLE", "INSERT"]
    assert canonical["block_defs"][0]["insert"] == 1
    result = write_loop.apply_mutations(head, canonical)
    assert result["created"] == [{"ordinal": 0, "handle": result["circles"][0]["handle"]},
                                 {"ordinal": 1, "handle": result["inserts"][0]["handle"]}]
    assert write_loop.verify_live_mutation_effects(head, result, canonical) is None
    result["created"].append({"ordinal": 2, "handle": "999"})
    with pytest.raises(ValueError, match="handoff"):
        write_loop.verify_live_mutation_effects(head, result, canonical)


@pytest.mark.parametrize("case,rule", [
    ("handle", "block children carry no handle"), ("kind", "block child kind must be"),
    ("bulged", "block LWPOLYLINE must have straight segments, every bulge 0"),
    ("missing-order", "block order is required with inline children"),
    ("duplicate-member", "block order must name each member and inline child exactly once"),
    ("missing-child", "block order must name each member and inline child exactly once"),
    ("unknown-child", "block order must name each member and inline child exactly once"),
    ("empty", "1..60"), ("61", "1..60"),
    ("property", "colour, linetype and lineweight"),
    ("transform", "block members cannot be transformed; use set_points"),
    ("two-geometry", "is a CIRCLE"), ("removed-child", "not an AutoCAD handle"),
    ("removed-add", "not an AutoCAD handle"), ("xdata", "no xdata"),
    ("ownership", "unknown fields"), ("association", "unknown fields"),
])
def test_inline_refusals_leave_the_head_unchanged(case, rule):
    head, plan = inline_base(), inline_replace()
    definition = plan["block_defs"][0]
    if case == "handle":
        definition["children"][0]["handle"] = "99"
    elif case == "kind":
        definition["children"][0]["kind"] = "INSERT"
    elif case == "bulged":
        definition["children"][0] = {"kind": "LWPOLYLINE", "layer": "0", "closed": False,
                                     "pts": [[0, 0], [3, 0]], "bulges": [0.5, 0]}
    elif case == "missing-order":
        definition.pop("order")
    elif case == "duplicate-member":
        definition["order"] = ["C:0", "H:11", "H:11"]
    elif case == "missing-child":
        definition["order"] = ["H:11"]
    elif case == "unknown-child":
        definition["order"] = ["C:1", "H:11"]
    elif case == "empty":
        definition.update(members=[], children=[], order=[])
    elif case == "61":
        definition["children"] *= 60
    elif case == "property":
        plan["set_color"] = [{"handle": "11", "aci": 1}]
    elif case == "transform":
        plan["transforms"] = [{"handle": "11", "dx": 1, "dy": 0}]
    elif case == "two-geometry":
        plan["set_points"] = [{"handle": "11", "pts": [[0, 0], [3, 0]]}]
        plan["set_circle"] = [{"handle": "11", "c": [6, 1, 0], "r": 1}]
    elif case in ("removed-child", "removed-add"):
        plan["removed"].append("C:0" if case == "removed-child" else "A:0")
    elif case == "xdata":
        definition["children"][0]["xdata"] = None
    elif case == "ownership":
        definition["children"][0]["owner"] = "11"
    elif case == "association":
        definition["children"][0]["association"] = ["11"]
    before = copy.deepcopy(head)
    with pytest.raises(ValueError, match=rule):
        write_loop.apply_mutations(head, plan)
    assert head == before


def test_inline_extension_preserves_main_replacement_plan_digest():
    # main 6ceaf4ab: the exact replace() plan text predates BLOCKCHILD.
    main_plan_sha256 = "3dde62183a824be6660119c86ec78c3599f6711dc020e100ad95be3c9d7f0b22"
    canonical = mutation_plan.validate_mutations(base(), replace())
    assert hashlib.sha256(mutation_plan.emit_plan(canonical, base_sha256="1" * 64)).hexdigest() == main_plan_sha256


def inline_reopened(*, aci=256, moved=False, leaked=False, missing=False):
    circle = "6,1,0" if moved else "4,2,0"
    layer = "SITE" if moved else "0"
    records = ("GRC|1\nBKEPC|1\nLAYER|0\nLAYER|SITE\nCA|0|301\n"
               "IN|B|0|1,1,0|0|0,0,1|1,1,1|301\nEP|301|256|~|ByLayer|-1\n"
               f"BK|B|1,1,0|{1 if missing else 2}|1\n"
               "BKE|B|LINE|0,0,0|3,0,0|0\n"
               f"BKEP|B|0|{aci}|ByLayer|-1|~\n")
    if not missing:
        records += f"BKE|B|CIRCLE|{circle}|1|0,0,1|{layer}\nBKEP|B|1|256|ByLayer|-1|~\n"
    if leaked:
        records += "LN|0|0,0,0|3,0,0|302\nEP|302|256|~|ByLayer|-1\n"
    return intake_parse.parse_text(records, "inline-canary")


@pytest.mark.parametrize("damage", ["none", "leaked", "missing", "property"])
@pytest.mark.parametrize("aci", [256, 1])
def test_inline_reopened_definition_properties_and_model_space_inventory(damage, aci):
    head, plan = inline_base(), inline_replace()
    if aci != 256:
        plan["block_defs"][0]["children"][0]["aci"] = aci
    canonical = mutation_plan.validate_mutations(head, plan)
    actual = inline_reopened(aci=(3 if damage == "property" else aci),
                              leaked=damage == "leaked", missing=damage == "missing")
    assert not actual.get("parseErrors")
    if damage == "none":
        assert write_loop.verify_live_mutation_effects(head, actual, canonical) is None
        assert actual["blocks"]["B"]["children"][0]["properties"] == {
            "aci": aci, "rgb": None, "linetype": "ByLayer", "lineweight": -1}
    else:
        with pytest.raises(ValueError):
            write_loop.verify_live_mutation_effects(head, actual, canonical)


def test_inline_moved_dxf_legs_preserve_child_sequence():
    head, plan = inline_base(), inline_replace(moved=True)
    canonical = mutation_plan.validate_mutations(head, plan)
    result = write_loop.apply_mutations(head, canonical)
    parsed = dxf_intake.parse_dxf_bytes(intake_dxf.intake_to_dxf(result))
    assert parsed["blocks"]["B"]["children"] == result["blocks"]["B"]["children"]
    assert [c["kind"] for c in parsed["blocks"]["B"]["children"]] == ["LINE", "CIRCLE"]
    assert parsed["blocks"]["B"]["children"][1]["c"] == [6, 1, 0]
    assert parsed["blocks"]["B"]["children"][1]["layer"] == "SITE"
    parsed["created"] = result["created"]
    assert write_loop.verify_live_mutation_effects(head, parsed, canonical) is None
    assert write_loop.verify_live_mutation_effects(head, inline_reopened(moved=True), canonical) is None


@pytest.mark.parametrize("reverse_children", [False, True])
@pytest.mark.parametrize("sidecar", [False, True])
def test_inline_uploaded_dxf_is_bound_to_child_order(tmp_path, monkeypatch, reverse_children, sidecar):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    import deps
    import store
    from envelopes import install_error_handlers
    from routers import drawings

    monkeypatch.setenv("LEAF_STORE_DIR", str(tmp_path / "drawings"))
    monkeypatch.delenv("LEAF_AUTH_LIVE", raising=False)
    monkeypatch.setattr(deps, "APS_LIVE", False)
    app = FastAPI()
    install_error_handlers(app)
    app.include_router(drawings.router)
    client = TestClient(app)
    tenant, drawing = "tenant-inline-save", "inline-save"
    head, plan = inline_base(), inline_replace(moved=True)
    plan["added"][0]["handle"] = "301"
    backend = store.FilesystemBackend(str(tmp_path / "drawings"))
    source = tmp_path / "base.dwg"
    source.write_bytes(json.dumps(head).encode() if sidecar else b"AC1032" + b"\x00" * 64)
    store.ingest_drawing(backend, tenant, str(source), drawing_id=drawing)
    write_loop.publish_intake_cache(backend, tenant, drawing, 1, source.read_bytes(), head)
    upload = write_loop.apply_mutations(head, plan)
    if reverse_children:
        upload["blocks"]["B"]["children"].reverse()
        assert write_loop._block_digest(upload["blocks"]["B"]) != upload["blocks"]["B"]["digest"]
    data = intake_dxf.intake_to_dxf(upload)
    checkout = client.post(f"/api/drawings/{drawing}/checkout", headers={"X-Tenant-Id": tenant},
                           json={"holder": "inline-editor", "ttl_s": 3600})
    assert checkout.status_code == 200, checkout.text
    response = client.post(f"/api/drawings/{drawing}/versions/plan",
        headers={"X-Tenant-Id": tenant,
                 "X-Checkout-Capability": checkout.json()["checkout_capability"]},
        files={"file": ("edited.dxf", io.BytesIO(data), "application/dxf")},
        data={"parent_version": "1", "source_digest": hashlib.sha256(data).hexdigest(),
              "plan": json.dumps({"mutations": plan})})
    # Binding compares a child multiset while plan order governs the digest and native draw order.
    assert response.status_code == 201, response.text
    version, key = store.resolve_version(backend, tenant, drawing, "head")
    assert version == 2
    if sidecar:
        assert response.json()["source_stored"] is True
        assert backend.get(write_loop.edited_source_key(tenant, drawing, version)) == data
    else:
        stored = json.loads(backend.get(key))
        assert [child["kind"] for child in stored["blocks"]["B"]["children"]] == ["LINE", "CIRCLE"]


@pytest.mark.parametrize("child,geometry", [
    ({"kind": "LINE", "pts": [[0.12349, -0.0001, 0], [3.12349, 0, 0]]},
     "LINE|0|0.12349,-0.0001,0|3.12349,0,0"),
    ({"kind": "CIRCLE", "c": [4.12349, 2, 0], "r": 1.12349},
     "CIRCLE|0|4.12349,2,0|1.12349"),
    ({"kind": "ARC", "c": [4, 2, 0], "r": 1, "start_deg": 10.12345649, "end_deg": 80.12345649},
     "ARC|0|4,2,0|1|10.12345649|80.12345649"),
    ({"kind": "LWPOLYLINE", "closed": False, "pts": [[0, 0, 2], [3, 0, 2]]},
     "LWPOLYLINE|0|0|0,0,1|2|0,0;3,0"),
    ({"kind": "LWPOLYLINE", "closed": True, "pts": [[0, 0, 2], [3, 0, 2], [3, 3, 2]]},
     "LWPOLYLINE|0|1|0,0,1|2|0,0;3,0;3,3"),
])
def test_inline_child_kinds_use_add_validation_precision_and_style(child, geometry):
    head, plan = inline_base(), inline_replace()
    plan["block_defs"][0]["children"] = [{**child, "layer": "0", "aci": 1,
                                         "linetype": "Continuous", "lineweight": 25}]
    canonical = mutation_plan.validate_mutations(head, plan)
    assert mutation_plan.validate_mutations(head, canonical) == canonical
    ordinary = mutation_plan.validate_mutations(head, {
        "added": [{**child, "handle": "ordinary", "layer": "0"}]})["added"][0]
    for field in child.keys() - {"kind"}:
        assert canonical["block_defs"][0]["children"][0][field] == ordinary[field]
    row = mutation_plan.emit_plan(canonical, base_sha256="1" * 64).decode().splitlines()[2]
    assert row == f"BLOCKCHILD|B|0|{geometry}|1|Continuous|25"
    result = write_loop.apply_mutations(head, canonical)
    first = result["blocks"]["B"]["children"][0]
    assert "handle" not in first
    assert first["properties"] == {"aci": 1, "rgb": None, "linetype": "Continuous", "lineweight": 25}
    parsed = dxf_intake.parse_dxf_bytes(intake_dxf.intake_to_dxf(result))
    # DXF intake quantizes geometry for inspection, while adds retain precision.
    assert write_loop._block_semantics(parsed["blocks"]["B"]) == write_loop._block_semantics(result["blocks"]["B"])


@pytest.mark.parametrize("op,member,setter,expected", [
    ("set_points", {"polylines": [{"handle": "11", "layer": "0", "closed": False,
                                  "pts": [[0, 0, 0], [3, 0, 0]], "xdata": None}], "circles": []},
     {"handle": "11", "pts": [[1, 2, 0], [4, 2, 0]], "closed": False},
     {"pts": [[1, 2, 0], [4, 2, 0]]}),
    ("set_arc", {"circles": [], "arcs": [{"handle": "11", "layer": "0", "c": [4, 2, 0],
                 "r": 1, "start_deg": 10, "end_deg": 80, "nrm": [0, 0, 1]}]},
     {"handle": "11", "c": [6, 1, 0], "r": 2, "start_deg": 20, "end_deg": 90},
     {"c": [6, 1, 0], "r": 2, "start_deg": 20, "end_deg": 90}),
])
def test_consumed_geometry_setters_copy_final_geometry(op, member, setter, expected):
    head, plan = {**inline_base(), **member}, inline_replace()
    plan[op] = [setter]
    canonical = mutation_plan.validate_mutations(head, plan)
    rows = mutation_plan.emit_plan(canonical, base_sha256="1" * 64).decode().splitlines()
    assert rows[2].startswith(op.replace("_", "").upper() + "|11|")
    assert rows[3].startswith("BLOCKCHILD|")
    result = write_loop.apply_mutations(head, canonical)
    copied = result["blocks"]["B"]["children"][1]
    assert {key: copied[key] for key in expected} == expected
    assert write_loop.verify_live_mutation_effects(head, result, canonical) is None
    plan[op].append(copy.deepcopy(setter))
    with pytest.raises(ValueError, match="more than one geometry operation"):
        mutation_plan.validate_mutations(head, plan)


def test_removed_setter_exception_only_covers_consumed_members():
    head, plan = inline_base(), inline_replace()
    head["circles"].append({"handle": "12", "layer": "0", "c": [9, 9, 0], "r": 2, "nrm": [0, 0, 1]})
    plan["removed"].append("12")
    plan["set_layer"] = [{"handle": "12", "layer": "SITE"}]
    with pytest.raises(ValueError, match="cannot be removed and replaced"):
        mutation_plan.validate_mutations(head, plan)
    plan["removed"].remove("12")
    canonical = mutation_plan.validate_mutations(head, plan)
    rows = mutation_plan.emit_plan(canonical, base_sha256="1" * 64).decode().splitlines()
    assert rows[2].startswith("BLOCKCHILD|")
    assert rows[3].startswith("ADDBLOCKDEF|")
    assert rows[4] == "RELAYER|12|SITE"


def test_consumed_setters_use_ordinary_lines_before_block_children():
    head, plan = inline_base(), inline_replace()
    head["polylines"] = [{"handle": "10", "layer": "0", "closed": False,
                          "pts": [[0, 0, 0], [3, 0, 0]], "xdata": None}]
    head["arcs"] = [{"handle": "12", "layer": "0", "c": [4, 2, 0], "r": 1,
                     "start_deg": 10, "end_deg": 80, "nrm": [0, 0, 1]}]
    plan["block_defs"][0].update(members=["10", "11", "12"],
                                  order=["C:0", "H:10", "H:11", "H:12"])
    plan["removed"] = ["10", "11", "12"]
    plan["set_layer"] = [{"handle": "11", "layer": "SITE"}]
    plan["set_points"] = [{"handle": "10", "closed": False,
                           "pts": [[0.12349, -0.0001, 0.0004], [3.12349, 0, 0.0004]]}]
    plan["set_circle"] = [{"handle": "11", "c": [6, 1, 0], "r": 0.0004}]
    plan["set_arc"] = [{"handle": "12", "c": [6.12349, 1, 0], "r": 0.0004,
                        "start_deg": 20.12345649, "end_deg": 90.12345649}]
    ordinary = {op: copy.deepcopy(plan[op])
                for op in ("set_layer", "set_points", "set_circle", "set_arc")}
    canonical = mutation_plan.validate_mutations(head, plan)
    ordinary = mutation_plan.validate_mutations(head, ordinary)
    rows = mutation_plan.emit_plan(canonical, base_sha256="1" * 64).decode().splitlines()
    ordinary_rows = mutation_plan.emit_plan(ordinary, base_sha256="1" * 64).decode().splitlines()[2:]
    assert rows[2:6] == ordinary_rows
    assert [row.split("|", 1)[0] for row in ordinary_rows] == [
        "RELAYER", "SETPOINTS", "SETCIRCLE", "SETARC"]
    assert rows[4] == "SETCIRCLE|11|6,1,0|0.0004"
    assert rows[6].startswith("BLOCKCHILD|")
    assert rows[7].startswith("ADDBLOCKDEF|")


def test_tilted_inline_polyline_preserves_add_geometry_and_dxf_vertex():
    head, plan = inline_base(), inline_replace()
    child = {"kind": "LWPOLYLINE", "layer": "0", "closed": True,
             "pts": [[0, 0, 0], [10000, 0, 0.009], [0, 10000, 0]]}
    plan["block_defs"][0]["children"] = [child]
    canonical = mutation_plan.validate_mutations(head, plan)
    ordinary = mutation_plan.validate_mutations(head, {
        "added": [{**child, "handle": "ordinary"}]})
    assert canonical["block_defs"][0]["children"][0]["pts"] == ordinary["added"][0]["pts"]
    child_row = mutation_plan.emit_plan(canonical, base_sha256="1" * 64).decode().splitlines()[2]
    add_row = mutation_plan.emit_plan(ordinary, base_sha256="1" * 64).decode().splitlines()[2]
    fields = child_row.split("|")
    assert fields[:6] == ["BLOCKCHILD", "B", "0", "LWPOLYLINE", "0", "1"]
    assert fields[6:-3] == add_row.split("|")[2:]
    assert fields[-3:] == ["256", "ByLayer", "-1"]
    normal = [float(value) for value in fields[6].split(",")]
    second = [float(value) for value in fields[8].split(";")[1].split(",")]
    assert dxf_intake._ocs_to_wcs(second + [float(fields[7])], normal)[2] == pytest.approx(0.009)
    lowered = mutation_plan.world_to_ocs(child["pts"])
    result = write_loop.apply_mutations(head, canonical)
    copied = result["blocks"]["B"]["children"][0]
    assert copied["nrm"] == lowered["normal"]
    assert copied["elev"] == lowered["elevation"]
    assert copied["pts"] == lowered["points"]
    parsed = dxf_intake.parse_dxf_bytes(intake_dxf.intake_to_dxf(result))
    assert not parsed.get("parseErrors")
    reopened = parsed["blocks"]["B"]["children"][0]
    second = dxf_intake._ocs_to_wcs(reopened["pts"][1] + [reopened["elev"]], reopened["nrm"])
    # The DXF leg rounds normals to six decimals.
    assert reopened["nrm"] == [round(value, 6) for value in lowered["normal"]]
    assert second[2] == pytest.approx(0.009, abs=0.011)

    planar_plan = copy.deepcopy(plan)
    planar_plan["block_defs"][0]["children"][0]["pts"] = [
        [0, 0, 0.009], [10000, 0, 0.009], [0, 10000, 0.009]]
    planar_result = write_loop.apply_mutations(head, planar_plan)
    planar_parsed = dxf_intake.parse_dxf_bytes(intake_dxf.intake_to_dxf(planar_result))
    assert not planar_parsed.get("parseErrors")
    planar_reopened = planar_parsed["blocks"]["B"]["children"][0]
    for point in planar_reopened["pts"]:
        assert dxf_intake._ocs_to_wcs(
            point + [planar_reopened["elev"]], planar_reopened["nrm"])[2] == 0.009
