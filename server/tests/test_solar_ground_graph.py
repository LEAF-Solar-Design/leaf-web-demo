"""Ground tracker contract, lossless transport, and W1 compatibility."""
from __future__ import annotations

from copy import deepcopy
from hashlib import sha256
import importlib.util
import json
from pathlib import Path
import sys

import pytest
from jsonschema import Draft202012Validator, FormatChecker

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import write_loop  # noqa: F401; establishes the drawing-store import path
import store
import solar_ground_buildout
from solar_design_graph import (
    GraphValidationError, deserialize_graph, load_schema, serialize_graph, validate_graph,
)
from solar_solve_results import digest, upstream_basis, version_companion
from test_w1_design_graph import app_id, entity, graph  # noqa: F401, fixture
from test_w1_graph_versions import DRAWING, TENANT, commit, drawing, request_for  # noqa: F401, fixture


ROOT = Path(__file__).resolve().parents[2]
TRACKER = {
    "tracker_model": "single_axis_tracker",
    "axis_start": [1, 0], "axis_end": [3, 0], "axis_units": "drawing",
    "module_slots": 3, "row_index": 0, "length_m": 0.0508,
    "rail_overhang_m": 0.05, "cross_axis_width_m": 2.1, "max_tilt_deg": 60.0,
    "source": {
        "entity_kind": "tracker", "handle": "2A0", "layer": "LEAF-TRACKERS",
        "block_name": "LEAFSAT", "source_command": "LEAFSAT",
    },
}


def canon_sha(value):
    return sha256(json.dumps(value, sort_keys=True, separators=(",", ":"),
                             allow_nan=False).encode("utf-8")).hexdigest()


def ground_of(w1):
    result = deepcopy(w1)
    result["project"]["installation_design"] = "Ground"
    result["frames"][0]["installation_design"] = "Ground"
    result["frames"][0]["tracker"] = deepcopy(TRACKER)
    return result


def second_frame(handle):
    tracker = deepcopy(TRACKER)
    tracker.update(axis_start=[1, 10], axis_end=[2, 10], module_slots=1,
                   row_index=1, length_m=0.0254)
    tracker["source"]["handle"] = handle
    return entity(
        "frame", 2, name="Tracker 2", insertion_point=[1, 10, 0],
        installation_design="Ground", panel_refs=[], module_rows=1, module_columns=1,
        module_slots=1, module_power_watts=400, module_width_along_row=1,
        module_height_across_row=2, electrical_zone_ref=None,
        matrix=[[{"code": "empty", "panel_ref": None, "seq": None, "inverter_id": None,
                  "string_input_number": None, "x": 1, "y": 10, "angle": 0}]],
        sequences=[], panel_assignments=[], tracker=tracker,
    )


def moved(value):
    result = deepcopy(value)
    result.update(rev=1, parent_rev=0)
    result["frames"][0]["tracker"]["axis_end"] = [3, 1]
    return result


def intake_for(value):
    return {"layers": ["Panels"], "polylines": [], "solar_design_graph": value,
            "solar_design_graph_sha256": digest(value)}


def test_ground_graph_w1_fixture_is_byte_identical(graph):
    assert validate_graph(graph) == graph
    assert canon_sha(graph) == "67ab25efcda14a69da65a4184cf5d362ac19a85dcf266f722a9528f5881737c3"
    payload = serialize_graph(graph).encode("utf-8")
    assert len(payload) == 11244
    assert sha256(payload).hexdigest() == "1fd29adc97543a32e739c14bcad1257284b6fcf15eb08d9d3a1407df0b9c84fc"
    assert upstream_basis(graph) == "b013a3c9353bea41c2f9118c5b26bead06496673b6d0ca005f99735722b5dc44"
    after = deepcopy(graph)
    after.update(rev=1, parent_rev=0)
    companion = version_companion(intake_for(graph), graph, after)
    assert canon_sha(companion) == "3b6adb8bc8973ba6ebef8e9e036bb4426be980e0fdaff4d5d2ce5f215a0de99d"
    assert companion["solar_design_graph_sha256"] == "a1cf8d4ed8fa48f297f9072507e0a38718ebf04b6ee3f1cb901aa07d4181fa76"


def test_ground_graph_schema_checks_and_admits_ground(graph):
    schema = load_schema()
    Draft202012Validator.check_schema(schema)
    validator = Draft202012Validator(schema, format_checker=FormatChecker())
    validator.validate(graph)
    validator.validate(ground_of(graph))


def test_ground_graph_single_tracker_round_trip(graph):
    value = ground_of(graph)
    assert validate_graph(value) == value
    assert canon_sha(value) == "a683159cabc207f33211a34840a81e530732a3d3dc553724e69016cbb1eef3bb"
    payload = serialize_graph(value).encode("utf-8")
    assert sha256(payload).hexdigest() == "49c40dac28b4a632bae0753370deee41264fdb8a66751488d13b3f0d157aaae2"
    assert deserialize_graph(payload) == value


@pytest.mark.parametrize("variant,expected", [
    ("polyline-source", "4115434246a15faaad2cf39a5dd669834bf46c2c68645468b58c2ee416f25162"),
    ("unknown-fields", "a6ec96a524ff8a8c3a949aedd91be3dd6cedcd3912a6e9e8a3ef3bd90b79130d"),
    ("two-trackers", "0e0c1667901181d1862615932eac0ec8402e31a46508fe43a3506812b201d659"),
    ("two-null-handles", "2b90da0ffd9a6815cad105fbf4bf294995fe658ef54895106c071cfa4fe75c35"),
    ("axis-above-epsilon", "e357f63546d976c6f6527f4fb7251d8afaae80201216aa5b5864dd1c049a2188"),
    ("integral-float-slots", "89cdb8c34fe02fd10167dc60dd3b3c28ab463345ce3c188fb2ced67fc6afc25f"),
], ids=lambda value: value)
def test_ground_graph_admitted_variants(graph, variant, expected):
    value = ground_of(graph)
    tracker = value["frames"][0]["tracker"]
    if variant == "polyline-source":
        tracker.update(max_tilt_deg=None, cross_axis_width_m=None, source={
            "entity_kind": "polyline", "handle": None, "layer": "LEAF-TRACKERS",
            "block_name": None, "source_command": None,
        })
    elif variant == "unknown-fields":
        tracker["future"] = {"k": [1, None]}
        tracker["source"]["future_src"] = "x"
    elif variant == "two-trackers":
        value["frames"].append(second_frame("2A1"))
    elif variant == "two-null-handles":
        tracker["source"]["handle"] = None
        value["frames"].append(second_frame(None))
    elif variant == "axis-above-epsilon":
        tracker["axis_end"] = [1 + 2e-9, 0]
    elif variant == "integral-float-slots":
        tracker["module_slots"] = 3.0
    assert validate_graph(value) == value
    assert canon_sha(value) == expected
    assert deserialize_graph(serialize_graph(value)) == value


@pytest.mark.parametrize("case,code", [
    ("roof-project-ground-frame", "INSTALLATION_DESIGN_MISMATCH"),
    ("ground-project-roof-frame", "INSTALLATION_DESIGN_MISMATCH"),
    ("ground-frame-without-tracker", "INVALID_GRAPH_SCHEMA"),
    ("roof-frame-with-tracker", "INVALID_GRAPH_SCHEMA"),
    ("unknown-design", "INVALID_GRAPH_SCHEMA"),
    ("slot-mismatch", "TRACKER_SLOT_MISMATCH"),
    ("zero-slots", "INVALID_GRAPH_SCHEMA"),
    ("zero-axis", "DEGENERATE_TRACKER_AXIS"),
    ("axis-below-epsilon", "DEGENERATE_TRACKER_AXIS"),
    ("duplicate-handle", "DUPLICATE_TRACKER_SOURCE"),
    ("duplicate-handle-and-wrong-string-count", "DUPLICATE_TRACKER_SOURCE"),
    ("newline-handle-beside-valid-handle", "INVALID_GRAPH_SCHEMA"),
    ("three-d-axis", "INVALID_GRAPH_SCHEMA"),
    ("axis-out-of-range", "INVALID_GRAPH_SCHEMA"),
    ("axis-units", "INVALID_GRAPH_SCHEMA"),
    ("unknown-model", "INVALID_GRAPH_SCHEMA"),
    ("zero-length", "INVALID_GRAPH_SCHEMA"),
    ("long-length", "INVALID_GRAPH_SCHEMA"),
    ("negative-overhang", "INVALID_GRAPH_SCHEMA"),
    ("zero-width", "INVALID_GRAPH_SCHEMA"),
    ("tilt-over-90", "INVALID_GRAPH_SCHEMA"),
    ("negative-row", "INVALID_GRAPH_SCHEMA"),
    ("bool-row", "INVALID_GRAPH_SCHEMA"),
    ("lowercase-handle", "INVALID_GRAPH_SCHEMA"),
    ("long-handle", "INVALID_GRAPH_SCHEMA"),
    ("newline-handle", "INVALID_GRAPH_SCHEMA"),
    ("max-handle-with-newline", "INVALID_GRAPH_SCHEMA"),
    ("unknown-entity-kind", "INVALID_GRAPH_SCHEMA"),
    ("empty-layer", "INVALID_GRAPH_SCHEMA"),
    ("long-layer", "INVALID_GRAPH_SCHEMA"),
    ("long-block-name", "INVALID_GRAPH_SCHEMA"),
    ("long-command", "INVALID_GRAPH_SCHEMA"),
    ("missing-source", "INVALID_GRAPH_SCHEMA"),
    ("missing-tilt", "INVALID_GRAPH_SCHEMA"),
], ids=lambda value: value)
def test_ground_graph_refusals(graph, case, code):
    value = ground_of(graph)
    frame = value["frames"][0]
    tracker = frame["tracker"]
    tracker_changes = {
        "slot-mismatch": ("module_slots", 4),
        "zero-slots": ("module_slots", 0),
        "zero-axis": ("axis_end", [1, 0]),
        "axis-below-epsilon": ("axis_end", [1 + 1e-10, 0]),
        "three-d-axis": ("axis_start", [1, 0, 0]),
        "axis-out-of-range": ("axis_end", [1e9 + 1, 0]),
        "axis-units": ("axis_units", "m"),
        "unknown-model": ("tracker_model", "fixed_tilt"),
        "zero-length": ("length_m", 0),
        "long-length": ("length_m", 100000.5),
        "negative-overhang": ("rail_overhang_m", -0.1),
        "zero-width": ("cross_axis_width_m", 0),
        "tilt-over-90": ("max_tilt_deg", 91),
        "negative-row": ("row_index", -1),
        "bool-row": ("row_index", True),
    }
    source_changes = {
        "lowercase-handle": ("handle", "2a0"),
        "long-handle": ("handle", "A" * 17),
        "newline-handle": ("handle", "2A0\n"),
        "max-handle-with-newline": ("handle", "A" * 16 + "\n"),
        "unknown-entity-kind": ("entity_kind", "block"),
        "empty-layer": ("layer", ""),
        "long-layer": ("layer", "L" * 256),
        "long-block-name": ("block_name", "B" * 256),
        "long-command": ("source_command", "C" * 65),
    }
    if case in tracker_changes:
        key, replacement = tracker_changes[case]
        tracker[key] = replacement
    elif case in source_changes:
        key, replacement = source_changes[case]
        tracker["source"][key] = replacement
    elif case == "roof-project-ground-frame":
        value["project"]["installation_design"] = "Roof"
    elif case == "ground-project-roof-frame":
        frame["installation_design"] = "Roof"
        del frame["tracker"]
    elif case == "ground-frame-without-tracker":
        del frame["tracker"]
    elif case == "roof-frame-with-tracker":
        value["project"]["installation_design"] = frame["installation_design"] = "Roof"
    elif case == "unknown-design":
        value["project"]["installation_design"] = frame["installation_design"] = "Carport"
    elif case == "duplicate-handle":
        value["frames"].append(second_frame("2A0"))
    elif case == "duplicate-handle-and-wrong-string-count":
        value["frames"].append(second_frame("2A0"))
        value["strings"][0]["module_count"] += 1
    elif case == "newline-handle-beside-valid-handle":
        value["frames"].append(second_frame("2A0\n"))
    elif case == "missing-source":
        del tracker["source"]
    elif case == "missing-tilt":
        del tracker["max_tilt_deg"]
    else:
        raise AssertionError(case)
    before = deepcopy(value)
    with pytest.raises(GraphValidationError) as caught:
        validate_graph(value)
    assert caught.value.code == code
    assert value == before


def test_ground_graph_max_length_block_name(graph):
    value = ground_of(graph)
    value["frames"][0]["tracker"]["source"]["block_name"] = "B" * 255
    assert validate_graph(value) == value
    assert deserialize_graph(serialize_graph(value)) == value


def test_ground_graph_upstream_basis_tracks_tracker(graph):
    value = ground_of(graph)
    assert upstream_basis(value) == "d9ca169052635684440a676db79b00ee2f0c1a2d7d19c151f80e54c8be207a71"
    assert upstream_basis(moved(value)) == "0d159caa72ae9a4fc52f6a62adb146518cc54d048c18278a9a0fe4fc325accb9"


def test_ground_graph_version_companion_publishes_ground(graph):
    value = ground_of(graph)
    after = moved(value)
    companion = version_companion(intake_for(value), value, after)
    assert companion["solar_design_graph"] == after
    assert companion["solar_design_graph_sha256"] == "8a4371a9de8eb3bd8f77793b84c62db3fabf922f1d45f27a09db9552adc6df11"
    assert canon_sha(companion) == "124c08d6782d8585e2bab5ff606974597b45f92a868ec7ad777ec0375918b868"


def test_ground_graph_store_publish_reopen(drawing, graph):
    backend, _ = drawing
    value = ground_of(graph)
    first = request_for(backend, value)
    assert commit(drawing, first) == 2
    bundle = store.read_graph_bundle(store.FilesystemBackend(backend.root), TENANT, DRAWING,
                                     project_id=first["project_id"])
    assert bundle["graph"] == first["graph"]
    assert bundle["graph"]["frames"][0]["tracker"] == TRACKER
    second = request_for(backend, moved(value), apply_id="apply-2", dwg=b"second DWG")
    assert commit(drawing, second, dwg=b"second DWG") == 3
    bundle = store.read_graph_bundle(store.FilesystemBackend(backend.root), TENANT, DRAWING,
                                     project_id=second["project_id"])
    assert bundle["graph"]["rev"] == 1
    assert bundle["graph"]["frames"][0]["tracker"]["axis_end"] == [3, 1]


def test_ground_graph_real_tracker_rows_fit_the_contract():
    spec = importlib.util.spec_from_file_location(
        "ground_graph_dsteps_evidence", ROOT / "scripts" / "solar_ground_dsteps_evidence.py",
    )
    evidence = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = evidence
    spec.loader.exec_module(evidence)
    intake = json.loads((ROOT / "docs/parity/evidence/ground/terrain/intake.json").read_text(
        encoding="utf-8",
    ))
    entities = evidence.bev.tracker_entities(evidence.b18_state(intake))
    mpu = evidence.bev.MPU
    schema = load_schema()
    validator = Draft202012Validator({"$defs": schema["$defs"], "$ref": "#/$defs/tracker"})
    projections = []
    for ent in entities:
        if ent["kind"] != "tracker":
            continue
        row = solar_ground_buildout.read_tracker_rows([ent], mpu)[0]
        if row["module_slots"] <= 0:
            continue
        projection = {
            "tracker_model": ent["tracker_model"],
            "axis_start": list(row["axis_start"]), "axis_end": list(row["axis_end"]),
            "axis_units": "drawing", "module_slots": row["module_slots"],
            "row_index": row["row_index"], "length_m": row["length_m"],
            "rail_overhang_m": row["rail_overhang_m"],
            "cross_axis_width_m": ent["cross_axis_width_du"] * mpu,
            "max_tilt_deg": ent["max_tilt_deg"],
            "source": {"entity_kind": "tracker", "handle": None, "layer": None,
                       "block_name": ent["block"], "source_command": ent["source_command"]},
        }
        assert list(validator.iter_errors(projection)) == []
        projections.append(projection)
    assert len(projections) == 237
    assert sum(item["module_slots"] for item in projections) == 69678
    assert canon_sha(projections) == "af66b6a9ef8f8114388e82120a2a5e25e684ef4280ba35661410e3278e353e71"
