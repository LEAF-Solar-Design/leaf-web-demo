"""Offline write-contract coverage. Synthetic plans do not prove DWG persistence."""
from __future__ import annotations

import copy
import json
import sys
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from solar_design_graph import GraphValidationError, validate_graph  # noqa: E402
from solar_interchange import import_solar_state  # noqa: E402
from solar_inspection import (  # noqa: E402
    canonical_bytes, digest, entity_digest, load_schema, parse_inspection,
    raw_record_bytes, store_evidence, validate_inspection,
)
from solar_apply_plan import (  # noqa: E402
    build_apply_plan, patch_document, serialize_plan, validate_plan,
)


FIXTURES = Path(__file__).parent / "fixtures"


def _read(name):
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def _id(kind, n=1):
    return f"leaf:{kind}:00000000-0000-4000-8000-{n:012d}"


def _node(kind, **fields):
    return {"id": _id(kind), "kind": kind, "rev": 0,
            "provenance": {"created_by": "fixture", "created_at": "2026-09-17T00:00:00Z",
                           "last_writer": "fixture", "source_rev": 0},
            "extra": {}, "validity": {"state": "valid", "reasons": []}, **fields}


@pytest.fixture
def inputs():
    graph, imported = import_solar_state(
        _read("w1_solar_inspection_synthetic.json"),
        FIXTURES / "w1_solar_adapter_synthetic.json")
    graph["inverters"] = [_node(
        "inverter", number=1, type_key="A", is_l2=False, position=[8, 1],
        model="fixture", mppt_count=1, total_dc_inputs=2, max_dc_voltage=600,
        max_ac_power_kw=1, is_solaredge=False, input_assignments=[])]
    inspection = _read("w1_solar_inspection_v2_synthetic.json")
    mapping = {"drawing_key": "fixture-drawing", "entities": []}
    for entity in inspection["entities"]:
        app_id = _id("inverter") if entity["handle"] == "A3" else imported[entity["handle"]]
        mapping["entities"].append({"app_id": app_id, "role": "primary", "kind": entity["kind"],
                                    "handle": entity["handle"], "entity_sha256": entity["sha256"],
                                    "payload_sha256": None})
    validate_graph(graph)
    return graph, _read("w1_solar_adapter_write_synthetic.json"), inspection, mapping


def _frame(graph):
    after = copy.deepcopy(graph)
    panels = after["panels"]
    for i, panel in enumerate(panels):
        panel["frame_ref"] = _id("frame")
        panel["matrix_cell"] = {"row": 0, "col": i}
    after["frames"] = [_node(
        "frame", name="Synthetic group", insertion_point=[2, 3, 0], installation_design="Roof",
        panel_refs=[p["id"] for p in panels], module_rows=1, module_columns=2,
        module_slots=2, module_power_watts=400, module_width_along_row=1,
        module_height_across_row=2, electrical_zone_ref=None,
        matrix=[[{"code": "panel", "panel_ref": p["id"], "seq": None,
                  "inverter_id": None, "string_input_number": None,
                  "x": p["centre"][0], "y": 1, "angle": 0} for p in panels]],
        sequences=[], panel_assignments=[{"panel_ref": p["id"], "string_ref": None,
                                         "seq": None, "inverter_id": None,
                                         "string_input_number": None} for p in panels])]
    return after


def _string(graph):
    after = copy.deepcopy(graph)
    panels = after["panels"]
    for i, panel in enumerate(panels):
        panel["assignment"] = {"string_ref": _id("string"), "seq": i}
    after["strings"] = [_node(
        "string", circuit_tag="1.A.1", circuit_kind="String",
        ordered_panel_refs=[p["id"] for p in panels], module_count=len(panels),
        from_ref=panels[0]["id"], to_ref=panels[-1]["id"], tag_text_ref=None,
        wire_gauge="10 AWG", length_ft=12, route=[[1, 1], [4, 1]], inverter_ref=_id("inverter"),
        extra={"circuit_assignment": {"num_mppt": 1, "strings_per_mppt": 2,
                                     "string_number": 1, "inverter_number": 1,
                                     "mppt_letter": "A", "strings_on_inverter": 1,
                                     "terminal_number": 1, "colour_counter": 0}})]
    after["inverters"][0]["input_assignments"] = [
        {"string_ref": _id("string"), "mppt_letter": "A", "input_number": 0}]
    for frame in after["frames"]:
        for i, cell in enumerate(frame["matrix"][0]):
            cell.update(seq=i, inverter_id=_id("inverter"), string_input_number=0)
        for i, assignment in enumerate(frame["panel_assignments"]):
            assignment.update(string_ref=_id("string"), seq=i,
                              inverter_id=_id("inverter"), string_input_number=0)
        frame["sequences"] = [{"string_ref": _id("string"),
                               "ordered_panel_refs": [p["id"] for p in panels]}]
    return after


def _plan(inputs, after=None, **overrides):
    before, adapter, inspection, mapping = inputs
    kwargs = {"drawing_key": mapping["drawing_key"], "idempotency_key": "fixture-request",
              "expected_revision": {"graph_rev": before["rev"],
                                    "site_revision": before["project"]["site_revision"]}}
    kwargs.update(overrides)
    return build_apply_plan(before, after if after is not None else before,
                            adapter, inspection, mapping, **kwargs)


def _raw_payload(payload):
    if payload["encoding"] == "xdata-typed":
        types = {1001: "string", 1000: "string", 1005: "handle", 1040: "double", 1070: "int16"}
        return [{**r, "type": types[r["code"]]} for r in payload["records"]]
    records = []
    for document in payload["documents"]:
        if payload["encoding"] == "json":
            records.append({"code": 1000, "type": "string", "value": document})
        else:
            chunks = [document[i:i + 240] for i in range(0, len(document), 240)]
            records.append({"code": 90, "type": "int32", "value": len(chunks)})
            records.extend({"code": 1000, "type": "string", "value": c} for c in chunks)
    return records


def _captured(inputs, after, plan):
    """Construct producer-shaped synthetic inspection, not native execution."""
    _, adapter, inspection, mapping = copy.deepcopy(inputs)
    allocations = {}
    for i, op in enumerate(plan["operations"]):
        if op["op"] != "create_entity":
            continue
        handle = format(0xB0 + i, "X")
        allocations[op["op_id"]] = handle
        entity = {"handle": handle, "kind": op["kind"], "layer": op["layer"],
                  "block_name": op["block_name"], "geometry": {"bbox": [0, 0, 20, 20], "points": None},
                  "native_geometry": copy.deepcopy(op["geometry"]), "attributes": copy.deepcopy(op["attributes"])}
        entity["sha256"] = entity_digest(entity)
        inspection["entities"].append(entity)
        mapping["entities"].append({"app_id": op["app_id"], "role": op["role"], "kind": op["kind"],
                                    "handle": handle, "entity_sha256": entity["sha256"], "payload_sha256": None})
    for op in plan["operations"]:
        if op["op"] != "set_store":
            continue
        payload = copy.deepcopy(op["payload"])
        for binding in op["bindings"]:
            index = int(binding["pointer"].split("/")[2])
            source = binding["source"]
            payload["records"][index]["value"] = source.get("handle") or allocations[source["create_ref"]]
        raw = _raw_payload(payload)
        store = {k: copy.deepcopy(op[k]) for k in ("scope", "dictionary_path", "key", "app")}
        store.update(entity_handle=op["entity_handle"] or allocations[op["entity_create_ref"]],
                     payload=payload, raw_records=raw, **store_evidence(raw))
        inspection["stores"].append(store)
        for entry in mapping["entities"]:
            if entry["handle"] == store["entity_handle"]:
                entry["payload_sha256"] = store["sha256"]
    return copy.deepcopy(after), adapter, inspection, mapping


def test_closed_schemas_and_lossless_reader(inputs):
    for kind in ("inspection", "apply"):
        Draft202012Validator.check_schema(load_schema(kind))
    inspection = inputs[2]
    restored = parse_inspection(json.dumps(inspection), "a" * 64, 4096)
    assert restored == inspection
    assert restored is not inspection
    assert restored["stores"][0]["raw_records"][2]["value"] == "AAH/"
    restored["unknown"] = True
    with pytest.raises(GraphValidationError, match="SCHEMA"):
        validate_inspection(restored)


def test_no_change_empty_preserves_every_input(inputs):
    saved = copy.deepcopy(inputs)
    plan = _plan(inputs)
    assert plan["operations"] == []
    assert plan["preserve_untouched"] is True
    assert plan["expected_revision"]["site_revision"] == "synthetic-1"
    assert inputs == saved


def test_frame_projects_two_documents_and_native_block(inputs):
    after = _frame(inputs[0])
    plan = _plan(inputs, after)
    create, store = plan["operations"]
    assert create["kind"] == "block_reference"
    assert create["block_name"] == "*U"
    assert create["geometry"]["definition"]["polylines"][0]["vertices"][2] == [2, 2, 0]
    assert [a["tag"] for a in create["attributes"]] == ["TEST_NAME", "TEST_DETAIL"]
    assert store["expected_payload_sha256"] is None
    assert store["entity_create_ref"] == create["op_id"]
    assert store["payload"]["encoding"] == "chunked-ascii(240)"
    matrix, group = map(json.loads, store["payload"]["documents"])
    assert [p["Handle"] for p in matrix["Rows"][0]["Panels"]] == ["A1", "A2"]
    assert matrix["Rows"][0]["Panels"][0]["Code"] == 1
    assert group["Title"] == "Synthetic group"
    assert "leaf:" not in json.dumps(store["payload"])


def test_string_carriers_ordered_xdata_and_endpoint_bindings(inputs):
    plan = _plan(inputs, _string(inputs[0]))
    creates = [op for op in plan["operations"] if op["op"] == "create_entity"]
    by_role = {op["role"]: op for op in creates}
    assert set(by_role) == {"primary", "tag", "start", "end"}
    assert by_role["start"]["geometry"]["position"] == [4, 1, 0]
    assert by_role["end"]["geometry"]["position"] == [1, 1, 0]
    assert by_role["tag"]["geometry"]["contents"] == "1.A.1"
    cable, tag = [op for op in plan["operations"] if op["op"] == "set_store"]
    assert [r["code"] for r in cable["payload"]["records"]] == [1001, 1000, 1000, 1005, 1005, 1005, 1000, 1000, 1000, 1040]
    assert cable["payload"]["records"][7]["value"] == "A1,A2"
    assert cable["payload"]["records"][9]["value"] == 12
    assert [b["source"]["create_ref"] for b in cable["bindings"]] == [by_role[r]["op_id"] for r in ("start", "end", "tag")]
    assert [r["value"] for r in tag["payload"]["records"]] == ["TEST_APP", 1, 2, 1, 1, "A", 1, 1, 0]


def test_same_inputs_have_byte_stable_plan_and_no_input_mutation(inputs):
    after = _string(_frame(inputs[0]))
    saved = copy.deepcopy((inputs, after))
    first = serialize_plan(_plan(inputs, after))
    assert first == serialize_plan(_plan(inputs, after))
    assert (inputs, after) == saved
    assert _plan(inputs, after)["adapter_sha256"] == digest(inputs[1])
    assert _plan(inputs, after)["mapping_sha256"] == digest(inputs[3])


@pytest.mark.parametrize("revision", [{"graph_rev": 1, "site_revision": "synthetic-1"},
                                      {"graph_rev": 0, "site_revision": "drift"},
                                      {"graph_rev": True, "site_revision": "synthetic-1"}])
def test_expected_revision_refuses(inputs, revision):
    with pytest.raises(GraphValidationError, match="EXPECTED_REVISION_MISMATCH"):
        _plan(inputs, expected_revision=revision)


@pytest.mark.parametrize("keyword", ["dwg_sha256", "adapter_sha256", "mapping_sha256"])
def test_source_bindings_refuse(inputs, keyword):
    with pytest.raises(GraphValidationError, match="MISMATCH"):
        _plan(inputs, **{keyword: "f" * 64})


def test_v1_is_importable_but_not_write_capable(inputs):
    inputs[2]["schema"] = "leaf.solar-inspection.v1"
    with pytest.raises(GraphValidationError, match="WRITE_REQUIRES_INSPECTION_V2"):
        _plan(inputs)


@pytest.mark.parametrize("kind", ["inverter", "route", "schedule"])
def test_other_carrier_kinds_name_the_parked_projection(inputs, kind):
    after = copy.deepcopy(inputs[0])
    if kind == "inverter":
        after["inverters"][0]["number"] = 2
    elif kind == "route":
        after["routes"] = [_node("route", route_kind="start homerun", points=[[0, 0], [1, 1]],
                                 from_ref=None, to_ref=None, wire_gauge="10 AWG", length_ft=1,
                                 point_units="m", length_units="ft")]
    else:
        # Reuse the existing public schema's constraint; do not copy its private
        # layer literal into another public module or fixture.
        from solar_design_graph import load_schema as graph_schema
        layer = graph_schema()["$defs"]["schedule"]["properties"]["layer"]["const"]
        after["schedules"] = [_node("schedule", rows=[["text"]], headers=["Header"],
                                    insertion_point=[0, 0], layer=layer, source_rev=0,
                                    source_refs=[], column_units=[None])]
    with pytest.raises(NotImplementedError, match=kind):
        _plan(inputs, after)


def test_unit_conversion_once_and_fixed_feet(inputs):
    inputs[0]["project"]["units"].update(drawing_units="mm", meters_per_unit=.001)
    inputs[2]["units"].update(drawing_units="mm", meters_per_unit=.001)
    plan = _plan(inputs, _string(_frame(inputs[0])))
    frame, string = [op for op in plan["operations"] if op["op"] == "create_entity" and op["role"] == "primary"]
    assert frame["geometry"]["position"] == [2000, 3000, 0]
    assert string["geometry"]["vertices"] == [[1000, 1000, 0], [4000, 1000, 0]]
    cable = next(op for op in plan["operations"] if op["op"] == "set_store" and op["scope"] == "entity-xdata")
    assert cable["payload"]["records"][9]["value"] == 12


def test_targeted_string_correction_retains_handles_and_unknown_stores(inputs):
    after = _string(inputs[0])
    captured = _captured(inputs, after, _plan(inputs, after))
    saved = copy.deepcopy(captured[2])
    edited = copy.deepcopy(after)
    edited["strings"][0]["route"][1] = [5, 1]
    edited["strings"][0]["length_ft"] = 15
    plan = _plan(captured, edited)
    assert not any(op["op"] == "create_entity" for op in plan["operations"])
    assert len([op for op in plan["operations"] if op["op"] == "update_entity"]) == 2
    writes = [op for op in plan["operations"] if op["op"] == "set_store"]
    assert len(writes) == 1
    assert writes[0]["expected_payload_sha256"] is not None
    assert writes[0]["entity_handle"] is not None
    assert captured[2] == saved
    assert _plan(captured)["operations"] == []


def test_changed_json_preserves_unknown_tokens_and_documents(inputs):
    after = _frame(inputs[0])
    captured = _captured(inputs, after, _plan(inputs, after))
    group = captured[2]["stores"][-1]
    documents = copy.deepcopy(group["payload"]["documents"])
    documents[1] = documents[1][:-1] + ', "Future" : { "raw": 1.2300e+02, "escaped": "\\u0061" }}'
    raw = _raw_payload({"encoding": "chunked-ascii(240)", "documents": documents})
    group.update(raw_records=raw, **store_evidence(raw))
    captured[3]["entities"][-1]["payload_sha256"] = group["sha256"]
    edited = copy.deepcopy(after)
    edited["frames"][0]["name"] = "Renamed"
    plan = _plan(captured, edited)
    store = next(op for op in plan["operations"] if op["op"] == "set_store")
    assert store["payload"]["documents"][0] == documents[0]
    assert '"raw": 1.2300e+02, "escaped": "\\u0061"' in store["payload"]["documents"][1]
    assert json.loads(store["payload"]["documents"][1])["Title"] == "Renamed"


def test_raw_serialization_preserves_types_order_binary_and_signed_zero():
    records = [{"code": 1, "type": "string", "value": "same"},
               {"code": 90, "type": "int32", "value": 1},
               {"code": 310, "type": "binary", "value": "AAH/"},
               {"code": 1040, "type": "double", "value": -0.0}]
    assert raw_record_bytes(records) != raw_record_bytes(list(reversed(records)))
    changed = copy.deepcopy(records)
    changed[-1]["value"] = 0.0
    assert store_evidence(changed)["sha256"] != store_evidence(records)["sha256"]
    changed[1]["type"] = "int16"
    assert store_evidence(changed)["sha256"] != store_evidence(records)["sha256"]


@pytest.mark.parametrize("defect", ["raw", "length", "hash", "source-length"])
def test_lossless_evidence_refuses_tampering(inputs, defect):
    inspection = inputs[2]
    if defect == "raw":
        inspection["stores"][0]["raw_records"].reverse()
    elif defect == "length":
        inspection["stores"][0]["byte_length"] += 1
    elif defect == "hash":
        inspection["entities"][0]["sha256"] = "0" * 64
    else:
        inspection["byte_length"] += 1
    with pytest.raises(GraphValidationError, match="MISMATCH"):
        parse_inspection(json.dumps(inspection), "a" * 64, 4096)


@pytest.mark.parametrize("defect", ["missing", "duplicate", "drawing", "role", "kind"])
def test_identity_never_remints_failed_candidates(inputs, defect):
    after = _frame(inputs[0])
    captured = _captured(inputs, after, _plan(inputs, after))
    mapping = captured[3]
    if defect == "missing":
        mapping["entities"][-1]["handle"] = "FFFF"
    elif defect == "duplicate":
        mapping["entities"].append(copy.deepcopy(mapping["entities"][-1]))
    elif defect == "drawing":
        mapping["drawing_key"] = "different"
    elif defect == "role":
        mapping["entities"][-1]["role"] = "wrong"
    else:
        mapping["entities"][-1]["kind"] = "point"
    with pytest.raises(GraphValidationError):
        _plan(captured, drawing_key="fixture-drawing")


def test_lowercase_handles_match_without_rewriting_inspection(inputs):
    for entry in inputs[3]["entities"]:
        entry["handle"] = entry["handle"].lower()
    plan = _plan(inputs, _frame(inputs[0]))
    assert "A1" in plan["operations"][-1]["payload"]["documents"][0]


def test_duplicate_json_keys_refuse_changed_document():
    with pytest.raises(GraphValidationError, match="DUPLICATE_JSON_KEY"):
        patch_document('{"Title":"a","Title":"b"}', {"/Title": "c"})


def test_malformed_chunk_counts_refuse(inputs):
    after = _frame(inputs[0])
    captured = _captured(inputs, after, _plan(inputs, after))
    store = captured[2]["stores"][-1]
    store["raw_records"][0]["value"] = 999
    store.update(store_evidence(store["raw_records"]))
    captured[3]["entities"][-1]["payload_sha256"] = store["sha256"]
    edited = copy.deepcopy(after)
    edited["frames"][0]["name"] = "Changed"
    with pytest.raises(GraphValidationError, match="MALFORMED_CHUNK_COUNT"):
        _plan(captured, edited)


def test_circuit_assignment_and_int16_bounds(inputs):
    after = _string(inputs[0])
    after["strings"][0]["extra"]["circuit_assignment"]["inverter_number"] = 2
    with pytest.raises(GraphValidationError, match="CIRCUIT_ASSIGNMENT_MISMATCH"):
        _plan(inputs, after)
    after = _string(inputs[0])
    after["strings"][0]["extra"]["circuit_assignment"]["colour_counter"] = 32768
    with pytest.raises(GraphValidationError, match="INVALID_TYPED_INTEGER"):
        _plan(inputs, after)


@pytest.mark.parametrize("defect", ["duplicate", "conflict", "unknown", "reference", "nonfinite", "vertices"])
def test_closed_plan_refusals(inputs, defect):
    plan = _plan(inputs, _string(inputs[0]))
    if defect == "duplicate":
        plan["operations"].append(copy.deepcopy(plan["operations"][0]))
    elif defect == "conflict":
        duplicate = copy.deepcopy(plan["operations"][-1])
        duplicate["op_id"] = "another-write"
        plan["operations"].append(duplicate)
    elif defect == "unknown":
        plan["operations"][0]["execute"] = True
    elif defect == "reference":
        plan["operations"][-1]["entity_create_ref"] = "not-created"
    elif defect == "nonfinite":
        plan["operations"][0]["geometry"]["vertices"][0][0] = float("nan")
    else:
        plan["operations"][0]["geometry"]["vertices"] = [[0, 0, 0]] * 100001
    with pytest.raises(GraphValidationError):
        validate_plan(plan)


def test_schema_freezes_point_table_delete_and_update_types(inputs):
    plan = _plan(inputs)
    plan["operations"] = [
        {"op": "create_entity", "op_id": "point", "app_id": "future", "role": "primary", "kind": "point",
         "layer": "TEST_POINTS", "geometry": {"kind": "point", "position": [0, 0, 0], "visible": True},
         "block_name": None, "attributes": []},
        {"op": "create_entity", "op_id": "table", "app_id": "future-table", "role": "primary", "kind": "table",
         "layer": "TEST_TABLES", "geometry": {"kind": "table", "position": [0, 0, 0], "title": "Title",
             "headers": ["Header"], "rows": [["Cell"]], "row_heights": [1, 1, 1], "column_widths": [1],
             "text_heights": [1, 1, 1], "table_style": "TEST_TABLE"}, "block_name": None, "attributes": []},
        {"op": "update_entity", "op_id": "update", "entity_handle": "A1", "expected_entity_sha256": "a" * 64,
         "changes": {"layer": "TEST_UPDATED"}},
        {"op": "delete_store", "op_id": "delete", "scope": "extension-dictionary", "dictionary_path": [],
         "key": "TEST_CREATED", "app": None, "entity_handle": "A1", "entity_create_ref": None,
         "expected_payload_sha256": "a" * 64}]
    Draft202012Validator(load_schema("apply")).validate(plan)
    with pytest.raises(GraphValidationError, match="DELETE_STORE_OWNERSHIP_REQUIRED"):
        validate_plan(plan)
    plan["operations"].pop()
    assert validate_plan(plan) == plan
    plan["operations"][2]["changes"] = {}
    with pytest.raises(GraphValidationError, match="SCHEMA"):
        validate_plan(plan)
