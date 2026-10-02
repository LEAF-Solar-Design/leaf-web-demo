"""W20 admission evidence: 17 tools on the real converted Ground chain."""
import copy
from hashlib import sha256
import importlib

import pytest

from test_solar_ground_equipment import (
    graph, service, pinned, converted, sized, strung, equip, slots, lsha,
    EQUIPPED_SHA, FixedDatetime, app_id,
)
from test_w1_solve_commit import transfer
from test_solar_tool_trackers_to_panel_groups import VIEW, small_doc, drawn
from test_solar_ground_graph_codec import ground_base

import solar_artifacts
import solar_design_graph
import solar_local_graph
import solar_local_read
import store
from leaf_cloud_client import canonical_bytes
from solar_design_graph import GraphValidationError


INV = "leaf:inverter:00000000-0000-4000-8000-0000000000e1"
NEC_ROWS = [
    ("ampacity_correction", "C1_SHA"),
    ("ac_voltage_drop", "V1_SHA"),
    ("conduit_fill", "C1_SHA"),
    ("feeder_ocpd", "F1_SHA"),
]


@pytest.fixture
def admission_base(graph, service):
    return sized(converted(graph))


@pytest.fixture
def admission_equipped(admission_base, pinned):
    return equip(strung(copy.deepcopy(admission_base), pinned))


def _builtin(name, monkeypatch, *, midpoint=False):
    solar_design_graph._reset_validation_caches()
    loader = (solar_local_read if name in solar_local_read.local_graph_read_tools()
              else solar_local_graph)
    module = loader._load_builtin(name)
    if hasattr(module, "datetime"):
        monkeypatch.setattr(module, "datetime", FixedDatetime)
    counter = [901]

    def mint(kind):
        value = app_id(kind, counter[0])
        if not midpoint:
            counter[0] += 1
        return value

    if hasattr(module, "new_id"):
        monkeypatch.setattr(module, "new_id", mint)
    return module


def _run(name, g, params, monkeypatch, *, midpoint=False):
    module = _builtin(name, monkeypatch, midpoint=midpoint)
    received = copy.deepcopy(g)
    snapshot = copy.deepcopy(received)
    try:
        return module.run(received, params)
    finally:
        assert received == snapshot


def test_ground_admission_fixture_is_converted_and_equipped(admission_base, admission_equipped):
    equipped = admission_equipped
    assert lsha(equipped) == EQUIPPED_SHA
    assert "ground_conversion" in equipped["extra"]
    assert equipped["project"]["installation_design"] == "Ground"
    assert slots(admission_base) == [
        "leaf:panel:a4e25cdd-6775-403d-9e0c-275400699308",
        "leaf:panel:670bc2b0-4640-458e-b12e-7d2f664a6766",
        "leaf:panel:a53ced07-0ab7-4dca-8ec1-c8f1982e1fc8",
        "leaf:panel:a89fade1-f331-4578-9d71-332417337b99",
        "leaf:panel:ce43e26e-a661-420b-824d-de3ff7a0be01",
    ]
    assert [s["id"] for s in equipped["strings"]] == [
        "leaf:string:00000000-0000-4000-8000-000000000901",
        "leaf:string:00000000-0000-4000-8000-000000000902",
    ]


GRAPH_ROWS = [
    ("settings", "d4e7aa701e8577ea5e78aed31083e5a1fecb57f2ad0af61495260fb06f2d4924"),
    ("correct_string", "1cb21ed2b109bb675b748a66affccce77c9655389438ae50334fc99a76f17988"),
    ("string_add", "e370249cf073df49fe3474009b4a82d5dc58993038caf0645fbfa61f03c7bcb2"),
    ("string_multi_add", "6abeba90622a000fe6029a9b1fa11d27da3c9a4b097b739593318197a44a67fb"),
    ("string_flip", "fe7dffe7d096ef0f1aceba10a66d7843337b13477f863eae4fd2d17fe9d3506f"),
    ("string_swap", "4c2fb1101859a7a3a1c1d6a9d1e6f74bd7caef90a6e25ea032c3ce47494b3b2c"),
    ("string_conductors", "4fd107efd85035f3ab9ed8ff01edc0d7c41d4a92248510a8947e975ada48cd81"),
]


@pytest.mark.parametrize("row,digest", GRAPH_ROWS, ids=[row for row, _ in GRAPH_ROWS])
def test_ground_admission_graph_tools(admission_base, admission_equipped, monkeypatch, row, digest):
    panels = slots(admission_base)
    strings = [s["id"] for s in admission_equipped["strings"]]
    requests = {
        "settings": {"expected_rev": 5, "changes": {"panels_in_sequence": 3}},
        "correct_string": transfer(admission_equipped),
        "string_add": {"operation": "add-string", "expected_rev": 2,
                       "ordered_panel_refs": panels[:3]},
        "string_multi_add": {"operation": "add-strings", "expected_rev": 2,
                             "string_length": 3, "ordered_panel_refs": panels},
        "string_flip": {"operation": "flip-string", "expected_rev": 5, "string_ref": strings[0]},
        "string_swap": {"operation": "swap-strings", "expected_rev": 5, "string_refs": strings},
        "string_conductors": {"operation": "set-conductors", "expected_rev": 5,
                              "assignments": [{"string_ref": ref, "wire_gauge": "8 AWG"}
                                              for ref in strings]},
    }
    g = admission_base if row in ("string_add", "string_multi_add") else admission_equipped
    expected_rev = 2 if g is admission_base else 5
    out = _run("solar-" + row.replace("_", "-"), g, requests[row], monkeypatch)
    assert (g["rev"], out["rev"]) == (expected_rev, expected_rev + 1)
    assert lsha(out) == digest
    counts = [s["module_count"] for s in out["strings"]]
    if row == "settings":
        assert out["settings"]["panels_in_sequence"] == 3
        assert out["settings"]["global_string_sizing_confirmed"] is False
    elif row == "correct_string":
        assert counts == [1, 4]
        stale = {"state": "stale", "reasons": ["upstream_corrected"]}
        assert [s["validity"] for s in out["strings"]] == [stale, stale]
        assert out["inverters"][0]["validity"] == stale
    elif row == "string_add":
        assert len(out["strings"]) == 1 and counts == [3]
        assert out["strings"][0]["circuit_tag"] == "S3"
        assert out["extra"]["solve_coverage"]["unassigned_panel_refs"] == panels[3:]
    elif row == "string_multi_add":
        assert counts == [3, 2]
        assert [s["circuit_tag"] for s in out["strings"]] == ["S3", "S4"]
        assert out["extra"]["solve_coverage"]["unassigned_panel_refs"] == []
    elif row == "string_flip":
        first = out["strings"][0]
        assert first["ordered_panel_refs"] == panels[:3][::-1]
        assert first["route"] == [[0.0, 5.0], [0.0, 3.0], [0.0, 1.0]]
        assert (first["from_ref"], first["to_ref"]) == (INV, panels[0])
    elif row == "string_swap":
        assert [s["circuit_tag"] for s in out["strings"]] == ["S4", "S3"]
        inverter = out["inverters"][0]
        assert [a["string_ref"] for a in inverter["input_assignments"]] == strings[::-1]
        assert [r["string_ref"] for r in out["extra"]["equipment"]["assignment_requests"]] == strings[::-1]
        assert inverter["validity"] == {"state": "valid", "reasons": []}
    elif row == "string_conductors":
        assert [s["wire_gauge"] for s in out["strings"]] == ["8 AWG", "8 AWG"]
        assert counts == [3, 2]
        assert out["extra"]["solve_coverage"]["unassigned_panel_refs"] == []


def test_ground_admission_string_delete(admission_base, pinned, monkeypatch):
    g = strung(copy.deepcopy(admission_base), pinned)
    out = _run("solar-string-delete", g, {"operation": "delete-strings", "expected_rev": 4,
                                        "string_refs": [g["strings"][1]["id"]]}, monkeypatch)
    assert (g["rev"], out["rev"]) == (4, 5)
    assert len(out["strings"]) == 1
    assert [s["module_count"] for s in out["strings"]] == [3]
    assert lsha(out) == "d57f5dddefdc59c6dee7048e0bc10ef2193e5da58a767d47796e3f21f9224eaf"


def test_ground_admission_string_midpoint(graph, service, monkeypatch):
    g0 = ground_base(graph)
    g0["project"]["zip_code"] = "44224"
    solar_design_graph._reset_validation_caches()
    g = solar_local_graph._load_builtin("solar-trackers-to-panel-groups").run(
        g0, {"expected_rev": 0}, physical_state={
            "view": copy.deepcopy(VIEW), "document": small_doc([drawn(0., 0., 0., 12., 6)])})
    g = sized(g)
    assert lsha(g) == "14b164c9931d12fa899326e8ff0378723d4952d140db3b3e06810c0ed458fbb7"
    ids = slots(g)
    assert (ids[0], ids[-1]) == (
        "leaf:panel:a4e25cdd-6775-403d-9e0c-275400699308",
        "leaf:panel:1b79263b-8340-4b1b-a29f-452c16c9542a")
    out = _run("solar-string-midpoint", g, {
        "operation": "add-midpoint-string", "expected_rev": 2,
        "start_panel_ref": ids[0], "end_panel_ref": ids[-1]}, monkeypatch, midpoint=True)
    assert (g["rev"], out["rev"]) == (2, 3)
    string = out["strings"][0]
    assert string["module_count"] == 4
    assert string["extra"]["midpoint"]["tag_index"] == 2
    assert string["extra"]["midpoint"]["label_height_m"] == 1.161958988686972
    assert string["length_ft"] == 32.808398950131235
    assert lsha(out) == "c2e5b0d1edf1434a634aab90d3eaaa3f364a98f07f4150adc1467e56e294770b"


def test_ground_admission_string_midpoint_refuses_short_row(admission_base, monkeypatch):
    ids = slots(admission_base)
    with pytest.raises(GraphValidationError) as exc:
        _run("solar-string-midpoint", admission_base, {
            "operation": "add-midpoint-string", "expected_rev": 2,
            "start_panel_ref": ids[0], "end_panel_ref": ids[2]}, monkeypatch)
    assert exc.value.code == "MIDPOINT_PATH_TOO_SHORT"


def test_ground_admission_string_delete_after_equipment_prunes_the_request(admission_equipped, monkeypatch):
    # This row is the evidence of the delete's behaviour after equipment.
    deleted_ref = admission_equipped["strings"][1]["id"]
    requests = admission_equipped["extra"]["equipment"]["assignment_requests"]
    assert sum(request["string_ref"] == deleted_ref for request in requests) == 1
    params = {"operation": "delete-strings", "expected_rev": 5,
              "string_refs": [deleted_ref]}
    remaining_requests = [request for request in requests if request["string_ref"] != deleted_ref]
    out = _run("solar-string-delete", admission_equipped, params, monkeypatch)
    assert (admission_equipped["rev"], out["rev"]) == (5, 6)
    assert [string["id"] for string in out["strings"]] == [admission_equipped["strings"][0]["id"]]
    assert out["extra"]["equipment"]["assignment_requests"] == remaining_requests
    for inverter in out["inverters"]:
        assert all(assignment["string_ref"] != deleted_ref
                   for assignment in inverter["input_assignments"])
        assert inverter["validity"] == {"state": "stale", "reasons": ["upstream_corrected"]}
    assert lsha(out) == "cdf2dcc14e65c2729a1e22e7731e5fddce39b4b50c23430d6e9b0da1a18ceeac"


READ_ROWS = [
    ("solar-string-rebuild", None, None, None,
     "5d4756f4d560961cf395e8d0991ea550a235239019389763a86b04faa9dba33a"),
    ("solar-string-data", "StringData.json", 1105,
     {"selected_strings": 2, "groups": 2, "grouped_strings": 2, "lines": 38},
     "97462b9623215dc6ff6f8375598cb60d21cb474834aec5640220a33a534a46a7"),
    ("solar-cable-export", "CableExport.xlsx", 3366,
     {"sheets": ["Homeruns", "Inverter Schedule", "String Schedule"], "rows": [3, 7, 8],
      "strings": 2, "modules": 5},
     "425c685ea39d17250a3c0aa9bceeec88370bd3e63347230040e8c070a9b72728"),
    ("solar-electrical-schedules", "ElectricalSchedules.json", 943,
     {"tables": ["INVERTER SCHEDULE", "STRING SCHEDULE"], "strings": 2, "modules": 5,
      "label_mismatches": 0},
     "79514c2e7d924b1b1dd8a2b26d99c240a3765ef2ac259360f1ba93711361736a"),
]


@pytest.mark.parametrize("name,filename,length,summary,digest", READ_ROWS,
                         ids=[row[0] for row in READ_ROWS])
def test_ground_admission_read_tools(admission_equipped, monkeypatch, name, filename, length,
                                     summary, digest):
    out = _run(name, admission_equipped, {}, monkeypatch)
    assert admission_equipped["rev"] == 5
    if filename is None:
        assert out == {"status": "rebuilt", "rebuilt_strings": 2,
                       "message": "Rebuilt panel associations for 2 string(s)"}
        assert lsha(out) == digest
    else:
        assert isinstance(out, solar_artifacts.ArtifactOutput)
        assert out.filename == filename
        assert {key: out.summary[key] for key in summary} == summary
        assert len(out.content) == length
        assert sha256(out.content).hexdigest() == digest


@pytest.mark.parametrize("suffix,pin", NEC_ROWS, ids=[row[0] for row in NEC_ROWS])
def test_ground_admission_nec_tools(admission_equipped, monkeypatch, suffix, pin):
    mod = importlib.import_module("test_solar_tool_nec_" + suffix)
    out = _run(mod.TOOL, admission_equipped, copy.deepcopy(mod.PARAMS), monkeypatch)
    assert admission_equipped["rev"] == 5
    assert lsha(out) == getattr(mod, pin)


ADAPTER_ROWS = [
    ("solar-string-rebuild", None,
     "5d4756f4d560961cf395e8d0991ea550a235239019389763a86b04faa9dba33a"),
    ("solar-string-data", None,
     "232fe568b3a42ceab82bbd6b7419a8d360f5b64eaf761bbf8adf4add86473f19"),
    ("solar-cable-export", None,
     "a955506ad3a105700d27b945c085df130276e7260a5e1e9c33288f8971586e1b"),
    ("solar-electrical-schedules", None,
     "e1b1c9b166ade476ccc5be280ee9abcc5d9ce1a6afa826c9e522f691ee11f2c6"),
] + [(suffix, pin, None) for suffix, pin in NEC_ROWS]


@pytest.mark.parametrize("name,pin,digest", ADAPTER_ROWS, ids=[row[0] for row in ADAPTER_ROWS])
def test_ground_admission_read_adapter(admission_equipped, monkeypatch, name, pin, digest):
    params = {}
    if pin is not None:
        mod = importlib.import_module("test_solar_tool_nec_" + name)
        name, params, digest = mod.TOOL, copy.deepcopy(mod.PARAMS), getattr(mod, pin)
    monkeypatch.setenv("LEAF_DRAWING_STORE", "legacy")
    g = admission_equipped
    before = copy.deepcopy(g)
    backend = store.InMemoryBackend()
    intake = {"solar_design_graph": g, "solar_design_graph_sha256": lsha(g)}
    version_key = store.drawing_version_key("fixture-tenant", "solar", 1)
    manifest_key = store.manifest_key("fixture-tenant", "solar")
    backend.put(version_key, canonical_bytes(intake))
    manifest = store._new_manifest("fixture-tenant", "solar")
    manifest["versions"] = [{"v": 1, "note": "", "sha256": "a" * 64}]
    backend.put(manifest_key, canonical_bytes(manifest))
    solar_design_graph._reset_validation_caches()
    out = solar_local_read.run_local_graph_read(
        backend, "fixture-tenant", name, params, drawing_id="solar", source_version=1,
        job_id="admission-" + name)
    assert out["drawing_changed"] is False
    assert out["graph_sha256"] == EQUIPPED_SHA
    assert out["output_sha256"] == digest
    assert g == before and g["rev"] == 5
