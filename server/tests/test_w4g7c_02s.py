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
