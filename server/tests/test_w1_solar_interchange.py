"""Synthetic read-side coverage for the W1 solar graph import slice."""
from __future__ import annotations

import copy
import hashlib
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "da"))

from server.solar_design_graph import GraphValidationError, validate_graph
from server.solar_interchange import import_solar_state, parse_inspection, validate_inspection
from da.intake_parse import parse_text, source_binding

FIXTURES = Path(__file__).parent / "fixtures"
ADAPTER = FIXTURES / "w1_solar_adapter_synthetic.json"


@pytest.fixture
def inspection():
    return json.loads((FIXTURES / "w1_solar_inspection_synthetic.json").read_text(encoding="utf-8"))


def test_source_binding_is_additive():
    data = b"synthetic drawing bytes"
    binding = source_binding(data)
    assert binding["source"] == {"dwg_sha256": hashlib.sha256(data).hexdigest(),
                                 "byte_length": len(data), "intake_schema": "v2"}
    intake = parse_text("", "synthetic.dwg", source_bytes=data)
    assert intake["source"] == binding["source"]
    assert intake["source_sha256"] == binding["source"]["dwg_sha256"]
    assert intake["source_byte_length"] == len(data)
    assert intake["dwg"] == "synthetic.dwg"
    assert intake["polylines"] == []


def test_legacy_text_only_intake_does_not_fabricate_source_hash():
    intake = parse_text("", "synthetic.dwg")
    assert intake["source"] == {"dwg_sha256": None, "byte_length": None,
                                "intake_schema": "v2", "binding": "unavailable"}


def test_import_requires_available_matching_intake_binding(inspection):
    for intake in (parse_text("", "synthetic.dwg"), {}, {"source": None}, None):
        with pytest.raises(GraphValidationError, match="SOLAR_INTAKE_BINDING_UNAVAILABLE"):
            import_solar_state(inspection, ADAPTER, intake=intake)
    intake = source_binding(b"synthetic drawing bytes")
    with pytest.raises(GraphValidationError, match="INSPECTION_SOURCE_MISMATCH"):
        import_solar_state(inspection, ADAPTER, intake=intake)
    inspection["dwg_sha256"] = intake["source"]["dwg_sha256"]
    graph, _ = import_solar_state(inspection, ADAPTER, intake=intake)
    assert graph["source_hash"] == intake["source"]["dwg_sha256"]


@pytest.mark.parametrize("status", ["not_represented", "derived"])
def test_named_membership_derivations_need_no_schema_loosening(tmp_path, inspection, status):
    # Loosened fields: none. Polarity and count have named membership rules;
    # project latitude/longitude already accept null in the graph contract.
    adapter = json.loads(ADAPTER.read_text(encoding="utf-8"))
    payload = {"circuit_tag": "SYNTH-1", "circuit_kind": "String",
               "ordered_panel_refs": ["A2", "A1"], "tag_text_ref": None,
               "wire_gauge": "synthetic", "length_ft": 1, "route": [], "inverter_ref": None}
    fields = {key: {"path": key, "status": "preserved"} for key in payload}
    fields.update({key: {"path": "absent", "status": status}
                   for key in ("from_ref", "to_ref", "module_count")})
    adapter["entity_kinds"]["string"] = {
        "select": {"scope": "entity-xdata", "app": "SYNTH-STRING"}, "fields": fields}
    inspection["entities"].append({"handle": "B1", "layer": "SYNTH-STRINGS", "kind": "LINE",
        "block_name": None, "geometry": {"bbox": [1, 1, 4, 1], "points": [[4, 1], [1, 1]]}})
    inspection["stores"].append({"key": "SYNTH-STRING", "scope": "entity-xdata",
        "entity_handle": "B1", "app": "SYNTH-STRING", "payload": payload})
    for store in inspection["stores"]:
        if store["app"] == "SYNTH-PANEL":
            store["payload"]["assignment"] = {
                "string_ref": "B1", "seq": ["A2", "A1"].index(store["entity_handle"])}
    path = tmp_path / "adapter.json"
    path.write_text(json.dumps(adapter), encoding="utf-8")
    graph, mapping = import_solar_state(inspection, path)
    string = graph["strings"][0]
    assert string["from_ref"] == mapping["A2"]
    assert string["to_ref"] == mapping["A1"]
    assert string["module_count"] == 2
    assert string["provenance"]["fields"]["from_ref"] == "derived:polarity-from-membership"
    assert string["provenance"]["fields"]["to_ref"] == "derived:polarity-from-membership"
    assert string["provenance"]["fields"]["module_count"] == "derived:count-from-membership"


def test_no_adapter_refuses(monkeypatch, inspection):
    monkeypatch.delenv("LEAF_SOLAR_ADAPTER_FILE", raising=False)
    with pytest.raises(GraphValidationError, match="SOLAR_ADAPTER_REQUIRED"):
        import_solar_state(inspection)


def test_import_preserves_unknowns_and_maps_every_handle(monkeypatch, inspection):
    monkeypatch.setenv("LEAF_SOLAR_ADAPTER_FILE", str(ADAPTER))
    original = copy.deepcopy(inspection)
    graph, mapping = import_solar_state(inspection)
    assert validate_graph(graph) == graph
    assert inspection == original
    assert set(mapping) == {entity["handle"] for entity in inspection["entities"]}
    assert len(set(mapping.values())) == len(mapping)
    assert {p["id"] for p in graph["panels"]} == set(mapping.values())
    assert "handle_mapping" not in graph
    first = next(p for p in graph["panels"] if p["id"] == mapping["A1"])
    assert first["extra"]["stores"] == [s for s in inspection["stores"] if s["entity_handle"] == "A1"]
    assert first["extra"]["stores"][0]["payload"]["future_panel_field"] == ["keep", "this"]
    assert graph["project"]["extra"]["stores"][2]["payload"] == "opaque|%\\\ntext"
    assert graph["project"]["latitude"] is None
    assert graph["project"]["provenance"]["fields"]["latitude"] == "not-represented"
    second_graph, second_mapping = import_solar_state(inspection, ADAPTER)
    assert set(mapping.values()).isdisjoint(second_mapping.values())
    assert second_graph["source_hash"] == inspection["dwg_sha256"]


@pytest.mark.parametrize("units", [
    {"drawing_units": None, "meters_per_unit": None},
    {"drawing_units": "unknown", "meters_per_unit": 1},
    {"drawing_units": "ft", "meters_per_unit": 1},
    {"drawing_units": "m", "meters_per_unit": True},
])
def test_unknown_or_inconsistent_units_refuse(inspection, units):
    inspection["units"] = units
    with pytest.raises(GraphValidationError, match="UNKNOWN_UNITS"):
        import_solar_state(inspection, ADAPTER)


def test_duplicate_handle_and_unmapped_entity_refuse(inspection):
    inspection["entities"][1]["handle"] = "a1"
    with pytest.raises(GraphValidationError, match="INVALID_INSPECTION_HANDLE"):
        import_solar_state(inspection, ADAPTER)


def test_missing_store_refuses_instead_of_dropping_entity(inspection):
    inspection["stores"] = [s for s in inspection["stores"] if s["entity_handle"] != "A2"]
    with pytest.raises(GraphValidationError, match="UNMAPPED_SOLAR_ENTITY"):
        import_solar_state(inspection, ADAPTER)


def test_ambiguous_store_refuses(inspection):
    inspection["stores"].append(copy.deepcopy(inspection["stores"][3]))
    with pytest.raises(GraphValidationError, match="AMBIGUOUS_SOLAR_ENTITY"):
        import_solar_state(inspection, ADAPTER)


def test_joined_opaque_chunks_are_interpreted_only_by_adapter_path(tmp_path, inspection):
    adapter = json.loads(ADAPTER.read_text(encoding="utf-8"))
    for field in adapter["entity_kinds"]["panel"]["fields"].values():
        field["path"] = "strings." + field["path"]
    for store in inspection["stores"]:
        if store["app"] == "SYNTH-PANEL":
            store["payload"] = {"dxf": "synthetic opaque DXF record", "strings": json.dumps(store["payload"])}
    path = tmp_path / "adapter.json"
    path.write_text(json.dumps(adapter), encoding="utf-8")
    graph, mapping = import_solar_state(inspection, path)
    assert len(mapping) == 2
    assert graph["panels"][0]["extra"]["stores"][0]["payload"]["dxf"] == "synthetic opaque DXF record"


def test_closed_payload_and_source_binding(inspection):
    source_hash = inspection["dwg_sha256"]
    inspection["dwg_sha256"] = None
    assert parse_inspection(json.dumps(inspection), source_hash)["dwg_sha256"] == source_hash
    inspection["dwg_sha256"] = "b" * 64
    with pytest.raises(GraphValidationError, match="INSPECTION_SOURCE_MISMATCH"):
        parse_inspection(json.dumps(inspection), source_hash)
    inspection["execute"] = "forbidden"
    with pytest.raises(GraphValidationError, match="INVALID_INSPECTION"):
        validate_inspection(inspection)


def test_bad_adapter_and_missing_fields_refuse(tmp_path, inspection):
    path = tmp_path / "adapter.json"
    adapter = json.loads(ADAPTER.read_text(encoding="utf-8"))
    adapter["entity_kinds"]["panel"]["fields"]["angle"]["path"] = "missing"
    path.write_text(json.dumps(adapter), encoding="utf-8")
    with pytest.raises(GraphValidationError, match="SOLAR_FIELD_NOT_FOUND"):
        import_solar_state(inspection, path)
    path.write_text('{"schema":1,"schema":2}', encoding="utf-8")
    with pytest.raises(GraphValidationError):
        import_solar_state(inspection, path)


def test_fixed_read_activity_has_no_drawing_output():
    from da.client import tool_activity_spec
    from da.lisp import MAX_SCRIPT_LINE_CHARS
    spec = tool_activity_spec({"name": "inspect_solar_state", "engine_op": "inspect_solar_state",
                               "engine_script": "ignored untrusted script"})
    assert set(spec["parameters"]) == {"HostDwg", "Params", "Result"}
    assert spec["parameters"]["Result"]["localName"] == "result.json"
    script = spec["settings"]["script"]["value"]
    assert "ignored untrusted script" not in script
    assert "namedobjdict" in script and "entity-xdata" in script and "extension-dictionary" in script
    assert all(len(line) <= MAX_SCRIPT_LINE_CHARS for line in script.splitlines())
    assert not any(word in script.lower() for word in ("entmod", "entmake", "entdel", "dictadd", "qsave", "saveas"))


def test_read_tool_uses_existing_client_boundary(monkeypatch, inspection):
    import da.client as client
    source = b"synthetic licensed input"
    inspection["dwg_sha256"] = None
    monkeypatch.setattr(client, "activity_qualified", lambda name: name)
    monkeypatch.setattr(client, "upload_object", lambda *a, **k: None)
    monkeypatch.setattr(client, "signed_download_url", lambda *a, **k: "synthetic-input")
    monkeypatch.setattr(client, "signed_upload_url", lambda *a, **k: ("synthetic-key", "synthetic-output"))
    monkeypatch.setattr(client, "finalize_upload", lambda *a, **k: None)
    submitted = []

    def submit(activity, arguments, **kwargs):
        submitted.append((activity, arguments, kwargs))
        return {"status": "success"}

    monkeypatch.setattr(client, "submit_workitem", submit)
    monkeypatch.setattr(client, "download_object", lambda key: json.dumps(inspection).encode()
                        if key.endswith(".result.json") else source)
    result = client.run_tool("synthetic.dwg", {"name": "inspect_solar_state", "version": 1}, {})
    assert result["ok"] is True
    assert result["result"]["dwg_sha256"] == hashlib.sha256(source).hexdigest()
    assert submitted[0][0].endswith("inspect_solar_state")
    assert submitted[0][2]["poll"] is True
    with pytest.raises(ValueError, match="accepts no parameters"):
        client.run_tool("synthetic.dwg", {"name": "inspect_solar_state"}, {"execute": "refuse"})
