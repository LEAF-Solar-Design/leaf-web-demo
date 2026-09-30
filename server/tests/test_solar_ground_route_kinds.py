"""Feeder and trench route kinds, conductor pathways, their dependency edges and export agreement."""
from __future__ import annotations

from copy import deepcopy
from hashlib import sha256
import importlib.util
import math
from pathlib import Path
import sys

import pytest
from jsonschema import Draft202012Validator

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import write_loop  # noqa: F401; establishes the drawing-store import path
import store
from solar_dependencies import affected_entities, dependency_index
from solar_design_graph import (
    GraphValidationError, deserialize_graph, load_schema, serialize_graph, validate_graph,
)
from solar_solve_results import (
    feeders_follow_topology, invalidate_dependents, require_current_export, upstream_basis,
)
from test_w1_design_graph import app_id, entity, graph  # noqa: F401, fixture
from test_w1_graph_versions import DRAWING, TENANT, commit, drawing, request_for  # noqa: F401, fixture
import test_solar_ground_topology as topology

SERVER = Path(__file__).resolve().parents[1]


def _load(name, path):
    if name in sys.modules:
        return sys.modules[name]
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


ds = _load("solar_ground_dsteps", SERVER / "solar_ground_dsteps.py")
out = _load("solar_inverter_outputs", SERVER / "solar_inverter_outputs.py")
cab = topology.cab
canon_sha = topology.canon_sha

L1_ID, L2_ID = topology.L1_ID, topology.L2_ID
THIRD_ID = app_id("inverter", 3)
S1, S2 = app_id("string", 1), app_id("string", 2)
HOMERUN_ID, FEEDER_ID, TRENCH_ID = app_id("route", 1), app_id("route", 2), app_id("route", 3)
SCHEDULE_ID, FRAME_ID = app_id("schedule", 1), app_id("frame", 1)
PANELS = [app_id("panel", n) for n in (1, 2, 3)]
M_PER_FT = 0.3048


def path_ft(points):
    return sum(math.dist(a, b) for a, b in zip(points, points[1:])) / M_PER_FT


def kernel_trench(start, end):
    """LEAFTRENCH between two picked points (solar_ground_dsteps.trench_command), in metres."""
    result = ds.trench_command(start, end)
    assert result["succeeded"]
    record = result["trench"]
    return ([list(p) for p in record["vertices"]],
            {key: record[key] for key in ("depth_m", "width_m", "voltage_class")})


TRENCH_POINTS, TRENCH_RECORD = kernel_trench((0.0, 1.0), (5.0, 1.0))
MOVED_TRENCH_POINTS, _ = kernel_trench((0.0, 2.0), (5.0, 2.0))


def trench(n=3, from_ref=None, to_ref=None, points=None, **fields):
    points = deepcopy(TRENCH_POINTS if points is None else points)
    return entity("route", n, route_kind="trench", points=points, from_ref=from_ref, to_ref=to_ref,
                  wire_gauge="", length_ft=path_ft(points), point_units="m", length_units="ft",
                  trench=deepcopy(TRENCH_RECORD), **fields)


def feeder(n=2, from_ref=L1_ID, to_ref=L2_ID, **fields):
    """The comb path RouteL2Feeders draws from the fixture's L1 at [5, 0] to its L2 at [20, 0]."""
    points = [list(p) for p in cab.comb_path((5.0, 0.0), (20.0, 0.0), [])]
    return entity("route", n, route_kind="feeder", points=points, from_ref=from_ref, to_ref=to_ref,
                  wire_gauge="", length_ft=path_ft(points), point_units="m", length_units="ft", **fields)


def route(g, route_id):
    return next(item for item in g["routes"] if item["id"] == route_id)


def _feeder(g):
    g["routes"].append(feeder())


def _trench(g):
    g["routes"].append(trench())


def _composite(g):
    """On T: a trench from the frame to an open hub, the feeder and the W1 homerun both riding it."""
    g["routes"].append(feeder(pathway_ref=TRENCH_ID))
    g["routes"].append(trench(from_ref=FRAME_ID))
    g["routes"][0]["pathway_ref"] = TRENCH_ID


def _string_on_trench(g):
    _trench(g)
    g["strings"][0]["pathway_ref"] = TRENCH_ID


def _unconnected_with_feeder(g):
    g["inverters"][0]["l2_ref"] = None
    g["inverters"][1]["l1_assignments"] = []
    _feeder(g)


def _second_central(g):
    g["inverters"].append(topology.central(3, 2, []))


def _reassigned(g, feeder_follows):
    """The L1 moved to a second central inverter; its feeder either follows it or stays behind."""
    _second_central(g)
    g["inverters"][0]["l2_ref"] = THIRD_ID
    g["inverters"][1]["l1_assignments"] = []
    g["inverters"][2]["l1_assignments"] = [{"inverter_ref": L1_ID, "mppt_index": 0}]
    g["routes"].append(feeder(to_ref=THIRD_ID if feeder_follows else L2_ID))


def _set(path, value):
    """A change that sets g[path...] = value; the last key may be a new key."""
    def change(g):
        target = g
        for key in path[:-1]:
            target = target[key]
        target[path[-1]] = deepcopy(value)
    return change


def _then(*changes):
    def change(g):
        for item in changes:
            item(g)
    return change


def _on_trench(key, value):
    return _then(_trench, lambda g: route(g, TRENCH_ID).__setitem__(key, deepcopy(value)))


def _on_trench_record(key, value):
    return _then(_trench, lambda g: route(g, TRENCH_ID)["trench"].__setitem__(key, deepcopy(value)))


def _on_feeder(key, value):
    return _then(_feeder, lambda g: route(g, FEEDER_ID).__setitem__(key, deepcopy(value)))


def build(w1, base, change):
    g = {"w1": deepcopy, "topology": topology.topology_of}[base](w1)
    change(g)
    return g


# (id, base, change, canon_sha of the admitted graph)
ADMITTED = [
    ("feeder", "topology", _feeder, "8e9fb7a79ac639b6744259137bed6cb4fa7cd8419685a5102c9a645f9df56b3d"),
    ("feeder-from-string-inverter", "topology",
     _then(_set(("inverters", 0, "equipment_type"), "string_inverter"), _feeder),
     "a5b46a14808d58616e3b0cd46d308ad9d8bc269559b006ce9f47948a3d2f9647"),
    ("feeder-from-unconnected-l1", "topology", _unconnected_with_feeder,
     "ad81f1c898af5d32e22fb36d36cf3f996f688891c5ff81b69fdb55e8a8fa14fd"),
    ("trench-open-ends", "w1", _trench, "1dd5852630ab5ea4f7149c6cd716ce7ec4e6c0f9f0e6c791d878a303d3df59e3"),
    ("trench-frame-to-inverter", "w1", lambda g: g["routes"].append(trench(from_ref=FRAME_ID, to_ref=L1_ID)),
     "63335e894973c55f33dfc5b22031fa2b1f3e4852fdaba80629b5aeadf6b4396f"),
    ("trench-ac-mv", "w1", _on_trench_record("voltage_class", "AC-MV"),
     "cd3e85fe7445ed6c2b685060bfd9064058165a9082af579c5b8cba6d28137dce"),
    ("trench-record-unknown-field", "w1", _on_trench_record("schema_version", 1),
     "24b9ee231a3907ad8104010d9560a27b6049d10b53b8d35d49f9c40428c605ff"),
    ("homerun-on-trench", "w1", _then(_trench, _set(("routes", 0, "pathway_ref"), TRENCH_ID)),
     "af6a45705e23152d2c33f8dc3b724710a96b3e1d4a131cfccc12faf7be7fbee6"),
    ("homerun-null-pathway", "w1", _set(("routes", 0, "pathway_ref"), None),
     "0bf4dcc6724212df8f127c7c02fd7383c7d87d63ba73bc7a32681527e754695f"),
    ("string-on-trench", "w1", _string_on_trench, "ac5808bd2824ff572ae4a3a70e24a91bb58a0a07ab61551b4c891e6cfe05c282"),
    ("composite", "topology", _composite, "8c792048449e139cc13e575e7016ff49648bfaa23c407bc2b562b8d7a5b4adf1"),
]

# (id, base, change, refusal code)
REFUSED = [
    ("unknown-route-kind", "w1", _set(("routes", 0, "route_kind"), "tray"), "INVALID_GRAPH_SCHEMA"),
    ("feeder-from-null", "topology", _on_feeder("from_ref", None), "INVALID_GRAPH_SCHEMA"),
    ("feeder-to-null", "topology", _on_feeder("to_ref", None), "INVALID_GRAPH_SCHEMA"),
    ("feeder-from-string", "topology", _on_feeder("from_ref", S1), "ROUTE_ENDPOINT_MISMATCH"),
    ("feeder-from-central", "topology", _on_feeder("from_ref", L2_ID), "ROUTE_ENDPOINT_MISMATCH"),
    ("feeder-to-combiner", "topology", _on_feeder("to_ref", L1_ID), "ROUTE_ENDPOINT_MISMATCH"),
    ("feeder-from-frame-with-l1-equipment-type", "topology",
     _then(_set(("frames", 0, "equipment_type"), "combiner_box"), _on_feeder("from_ref", FRAME_ID)),
     "ROUTE_ENDPOINT_MISMATCH"),
    ("feeder-to-frame-with-central-equipment-type", "topology",
     _then(_set(("frames", 0, "equipment_type"), "central_inverter"), _on_feeder("to_ref", FRAME_ID)),
     "ROUTE_ENDPOINT_MISMATCH"),
    ("feeder-from-missing", "topology", _on_feeder("from_ref", app_id("inverter", 9)), "ROUTE_ENDPOINT_MISMATCH"),
    ("feeder-to-missing", "topology", _on_feeder("to_ref", app_id("inverter", 9)), "ROUTE_ENDPOINT_MISMATCH"),
    ("feeder-on-w1", "w1", lambda g: g["routes"].append(feeder(to_ref=L1_ID)), "ROUTE_ENDPOINT_MISMATCH"),
    ("duplicate-feeder", "topology", _then(_feeder, lambda g: g["routes"].append(feeder(4))), "DUPLICATE_FEEDER"),
    ("feeder-with-trench-record", "topology", _on_feeder("trench", TRENCH_RECORD), "INVALID_GRAPH_SCHEMA"),
    ("homerun-with-trench-record", "w1", _set(("routes", 0, "trench"), TRENCH_RECORD), "INVALID_GRAPH_SCHEMA"),
    ("trench-without-record", "w1", _then(_trench, lambda g: route(g, TRENCH_ID).pop("trench")), "INVALID_GRAPH_SCHEMA"),
    ("trench-with-gauge", "w1", _on_trench("wire_gauge", "10 AWG"), "INVALID_GRAPH_SCHEMA"),
    ("trench-with-null-pathway", "w1", _on_trench("pathway_ref", None), "INVALID_GRAPH_SCHEMA"),
    ("trench-width-zero", "w1", _on_trench_record("width_m", 0), "INVALID_GRAPH_SCHEMA"),
    ("trench-depth-over-bound", "w1", _on_trench_record("depth_m", 10.5), "INVALID_GRAPH_SCHEMA"),
    ("trench-width-boolean", "w1", _on_trench_record("width_m", True), "INVALID_GRAPH_SCHEMA"),
    ("trench-unknown-voltage-class", "w1", _on_trench_record("voltage_class", "HV"), "INVALID_GRAPH_SCHEMA"),
    ("trench-without-voltage-class", "w1",
     _then(_trench, lambda g: route(g, TRENCH_ID)["trench"].pop("voltage_class")), "INVALID_GRAPH_SCHEMA"),
    ("trench-end-string", "w1", _on_trench("from_ref", S1), "ROUTE_ENDPOINT_MISMATCH"),
    ("trench-end-panel", "w1", _on_trench("to_ref", PANELS[0]), "ROUTE_ENDPOINT_MISMATCH"),
    ("trench-end-missing", "w1", _on_trench("to_ref", app_id("frame", 9)), "ROUTE_ENDPOINT_MISMATCH"),
    ("pathway-malformed", "w1", _set(("routes", 0, "pathway_ref"), "trench-1"), "INVALID_GRAPH_SCHEMA"),
    ("pathway-to-homerun", "w1", _set(("routes", 0, "pathway_ref"), HOMERUN_ID), "ROUTE_PATHWAY_MISMATCH"),
    ("pathway-to-missing", "w1", _set(("routes", 0, "pathway_ref"), TRENCH_ID), "ROUTE_PATHWAY_MISMATCH"),
    ("pathway-to-frame", "w1", _set(("routes", 0, "pathway_ref"), FRAME_ID), "ROUTE_PATHWAY_MISMATCH"),
    ("string-pathway-to-homerun", "w1", _set(("strings", 0, "pathway_ref"), HOMERUN_ID), "ROUTE_PATHWAY_MISMATCH"),
    ("string-pathway-malformed", "w1", _set(("strings", 0, "pathway_ref"), 7), "INVALID_GRAPH_SCHEMA"),
]


def composite(w1):
    return build(w1, "topology", _composite)


def moved_trench(g):
    h = deepcopy(g)
    item = route(h, TRENCH_ID)
    item.update(points=deepcopy(MOVED_TRENCH_POINTS), length_ft=path_ft(MOVED_TRENCH_POINTS))
    return h


def moved_inverter(g, i, position):
    h = deepcopy(g)
    h["inverters"][i]["position"] = position
    return h


def without_trench(g):
    h = deepcopy(g)
    h["routes"] = [item for item in h["routes"] if item["id"] != TRENCH_ID]
    for item in h["routes"] + h["strings"]:
        item.pop("pathway_ref", None)
    return h


def string_on_trench(w1):
    return build(w1, "w1", _string_on_trench)


# (id, builder over W giving (before, after), changed ids, affected ids)
AFFECTED = [
    ("move-trench", lambda w: (composite(w), moved_trench(composite(w))), [TRENCH_ID],
     [TRENCH_ID, FEEDER_ID, HOMERUN_ID]),
    ("delete-trench", lambda w: (composite(w), without_trench(composite(w))), [TRENCH_ID],
     [TRENCH_ID, FEEDER_ID, HOMERUN_ID]),
    ("move-l2", lambda w: (composite(w), moved_inverter(composite(w), 1, [30, 5])), [L2_ID], [L2_ID, FEEDER_ID]),
    ("move-l1", lambda w: (composite(w), moved_inverter(composite(w), 0, [9, 1])), [L1_ID],
     [L1_ID, L2_ID, HOMERUN_ID, FEEDER_ID]),
    ("string-edit", lambda w: (composite(w), composite(w)), [S1],
     [S1, L1_ID, L2_ID, HOMERUN_ID, FEEDER_ID, SCHEDULE_ID]),
    ("frame-edit", lambda w: (composite(w), composite(w)), [FRAME_ID],
     [FRAME_ID, *PANELS, S1, S2, L1_ID, L2_ID, HOMERUN_ID, FEEDER_ID, TRENCH_ID, SCHEDULE_ID]),
    ("string-on-trench", lambda w: (string_on_trench(w), moved_trench(string_on_trench(w))), [TRENCH_ID],
     [TRENCH_ID, S1, L1_ID, HOMERUN_ID, SCHEDULE_ID]),
]

# (id, base, change, expected refusal code or None when the export is current)
EXPORTS = [
    ("feeder", "topology", _feeder, None),
    ("composite", "topology", _composite, None),
    ("trench-only", "w1", _trench, None),
    ("feeder-follows-reassigned-l1", "topology", lambda g: _reassigned(g, True), None),
    ("feeder-left-behind", "topology", lambda g: _reassigned(g, False), "SOLAR_OUTPUT_NOT_CURRENT"),
    ("feeder-from-unconnected-l1", "topology", _unconnected_with_feeder, "SOLAR_OUTPUT_NOT_CURRENT"),
]


def sorted_index(g):
    return {key: sorted(value) for key, value in dependency_index(g).items()}


def test_ground_route_kinds_w1_fixture_is_byte_identical(graph):
    assert validate_graph(graph) == graph
    assert canon_sha(graph) == "67ab25efcda14a69da65a4184cf5d362ac19a85dcf266f722a9528f5881737c3"
    encoded = serialize_graph(graph).encode("utf-8")
    assert len(encoded) == 11244
    assert sha256(encoded).hexdigest() == "1fd29adc97543a32e739c14bcad1257284b6fcf15eb08d9d3a1407df0b9c84fc"
    assert canon_sha(sorted_index(graph)) == "6cdde121f6ad885364960e0494e2e3e6b12e1da1f3b56078ebebfdc0ad733587"
    assert require_current_export(graph) == graph


def test_ground_route_kinds_schema_contract():
    schema = load_schema()
    Draft202012Validator.check_schema(schema)
    defs = schema["$defs"]
    assert defs["route"]["properties"]["route_kind"]["enum"] == ["start homerun", "end homerun", "feeder", "trench"]
    assert defs["trench"]["properties"]["voltage_class"]["enum"] == ["DC-PV", "AC-LV", "AC-MV", "MIXED"]
    assert defs["route"]["properties"]["pathway_ref"] == {"$ref": "#/$defs/ref"}
    assert defs["string"]["properties"]["pathway_ref"] == {"$ref": "#/$defs/ref"}
    assert "pathway_ref" not in defs["route"]["required"] and "pathway_ref" not in defs["string"]["required"]
    assert canon_sha(defs["route"]) == "ee98527744370cad92bfff043ebce14b9f083db9ef30ee16daf566d91de06cbc"
    assert canon_sha(defs["trench"]) == "0f3437842dd315c2eacdcdb6058628c543c7ed90d8e73df6933bafd03ca5280d"
    assert canon_sha(defs["string"]) == "481d58eefbea2a57f33fc5f3eacd220597bf143f708da6471e24af546349d4ac"


@pytest.mark.parametrize("name,base,change,expected", ADMITTED, ids=[row[0] for row in ADMITTED])
def test_ground_route_kinds_admitted(graph, name, base, change, expected):
    g = build(graph, base, change)
    assert validate_graph(g) == g
    assert canon_sha(g) == expected
    assert deserialize_graph(serialize_graph(g)) == g


@pytest.mark.parametrize("name,base,change,expected", REFUSED, ids=[row[0] for row in REFUSED])
def test_ground_route_kinds_refused(graph, name, base, change, expected):
    g = build(graph, base, change)
    before = deepcopy(g)
    with pytest.raises(GraphValidationError) as refused:
        validate_graph(g)
    assert refused.value.code == expected
    assert g == before


def test_ground_route_kinds_dependency_index(graph):
    g = composite(graph)
    index = sorted_index(g)
    assert canon_sha(index) == "1df5030edf169eafdb475a8675cf0ce96d2b6814ce3bbd983e876761e6693be2"
    assert index[TRENCH_ID] == sorted([HOMERUN_ID, FEEDER_ID])
    assert index[FEEDER_ID] == []
    assert index[L1_ID] == sorted([L2_ID, HOMERUN_ID, FEEDER_ID])
    assert index[L2_ID] == [FEEDER_ID]
    assert TRENCH_ID in index[FRAME_ID]
    assert sorted_index(string_on_trench(graph))[TRENCH_ID] == [S1]
    assert upstream_basis(g) == upstream_basis(topology.topology_of(graph)) == (
        "feca2c152f9341a1f857ca5848c33d7ed01918884c079098bea42571c74a7f91")


@pytest.mark.parametrize("name,pair,changed,expected", AFFECTED, ids=[row[0] for row in AFFECTED])
def test_ground_route_kinds_affected(graph, name, pair, changed, expected):
    before, after = pair(graph)
    snapshot = deepcopy((before, after))
    assert affected_entities(before, after, list(changed)) == sorted(expected)
    assert (before, after) == snapshot


def test_ground_route_kinds_trench_move_stales_its_riders(graph):
    before = composite(graph)
    nonrider_id = app_id("route", 4)
    nonrider = deepcopy(route(before, HOMERUN_ID))
    nonrider["id"] = nonrider_id
    nonrider.pop("pathway_ref")
    before["routes"].append(nonrider)
    nonrider_validity = deepcopy(nonrider["validity"])
    after = moved_trench(before)
    assert invalidate_dependents(before, after, [TRENCH_ID]) == sorted([TRENCH_ID, FEEDER_ID, HOMERUN_ID])
    states = {e["id"]: e["validity"]["state"] for e in after["inverters"] + after["routes"] + after["strings"]}
    assert states == {L1_ID: "valid", L2_ID: "valid", HOMERUN_ID: "stale", FEEDER_ID: "stale",
                      TRENCH_ID: "stale", S1: "valid", S2: "valid", nonrider_id: "valid"}
    for route_id in (TRENCH_ID, FEEDER_ID, HOMERUN_ID):
        validity = route(after, route_id)["validity"]
        assert validity["state"] == "stale"
        assert validity["reasons"] == ["upstream_corrected"]
    assert route(after, nonrider_id)["validity"] == nonrider_validity
    assert validate_graph(after) == after
    with pytest.raises(GraphValidationError) as refused:
        require_current_export(after)
    assert refused.value.code == "SOLAR_OUTPUT_NOT_CURRENT"


@pytest.mark.parametrize("name,base,change,expected", EXPORTS, ids=[row[0] for row in EXPORTS])
def test_ground_route_kinds_export(graph, name, base, change, expected):
    g = build(graph, base, change)
    assert validate_graph(g) == g
    before = deepcopy(g)
    assert feeders_follow_topology(g) is (expected is None)
    if expected is None:
        assert require_current_export(g) == g
    else:
        with pytest.raises(GraphValidationError) as refused:
            require_current_export(g)
        assert refused.value.code == expected
    assert g == before


def test_ground_route_kinds_store_publish_reopen(graph, drawing):
    backend, _ = drawing
    value = composite(graph)
    first = request_for(backend, value)
    assert commit(drawing, first) == 2
    reopened = store.read_graph_bundle(store.FilesystemBackend(backend.root), TENANT, DRAWING,
                                       project_id=first["project_id"])["graph"]
    assert reopened == first["graph"]
    assert [item["route_kind"] for item in reopened["routes"]] == ["start homerun", "feeder", "trench"]
    assert route(reopened, FEEDER_ID)["pathway_ref"] == TRENCH_ID
    assert route(reopened, TRENCH_ID)["trench"] == {"depth_m": 1.0, "width_m": 0.6, "voltage_class": "MIXED"}


def i5_chain(w1):
    """The i5 kernel result (topology.i5_graph), then LEAFTRENCHAUTO (i6) and LEAFCABLETOTRAYAUTO (i7)
    on the same state, projected onto the route contract (test scaffolding, not a writer)."""
    g, after, intake = topology.i5_graph(w1)
    _, _, groups = topology.cabling_tests.committed_fixture()
    trenched, lines6 = out.trench_routing_auto(after, groups)
    snapped, lines7 = out.cable_to_tray_snap_auto(trenched)
    mpu = intake["drawing"]["metersPerUnit"]

    def metres(vertices):
        return [[p["value"][0] * mpu, p["value"][1] * mpu] if isinstance(p, dict) else [p[0] * mpu, p[1] * mpu]
                for p in vertices]

    feeders = [row for row in after["rows"]["cable"] if row["cable_kind"] == "feeder"]
    for row in feeders:
        g["routes"].append(entity(
            "route", 300 + row["from"], route_kind="feeder", points=metres(row["vertices"]),
            from_ref=app_id("inverter", 100 + row["from"]), to_ref=app_id("inverter", 200 + row["to"]),
            wire_gauge=row["_detail"]["gauge"], length_ft=row["length"]["value"], point_units="m",
            length_units="ft"))
    trenches = snapped["_trenches"]
    assert all(len(item["vertices"]) >= 2 for item in trenches)
    for k, item in enumerate(trenches, 1):
        points = metres(item["vertices"])
        g["routes"].append(entity(
            "route", 500 + k, route_kind="trench", points=points, from_ref=None, to_ref=None, wire_gauge="",
            length_ft=path_ft(points), point_units="m", length_units="ft",
            trench={"depth_m": ds.TRENCH_DEFAULT_DEPTH_M, "width_m": ds.TRENCH_DEFAULT_WIDTH_M,
                    "voltage_class": ds.TRENCH_DEFAULT_VOLTAGE_CLASS}))
    handles = cab._string_ids(after, intake)
    number_of = {handle: n for n, handle in handles.items()}
    snapped_cables = [row for row in snapped["rows"]["cable"] if row.get("_trench") is not None]
    snapped_strings = [item for item in snapped["geometry"]["strings"]
                       if isinstance(item, dict) and item.get("_trench") is not None]
    for item in snapped_strings:
        string = next(s for s in g["strings"] if s["id"] == app_id("string", number_of[item["string"]]))
        string.update(route=metres(item["vertices"]), pathway_ref=app_id("route", 501 + item["_trench"]))
    return {"graph": g, "after": after, "lines": (lines6, lines7), "feeders": feeders,
            "trenches": trenches, "snapped_cables": snapped_cables, "snapped_strings": snapped_strings}


def test_ground_route_kinds_i5_kernel_feeders_trenches_and_tray(graph):
    chain = i5_chain(graph)
    g = chain["graph"]
    assert chain["lines"] == (["LEAFTRENCHAUTO: routed 11 panel groups; 0 skipped (existing); 0 failed."],
                                 ["LEAFCABLETOTRAYAUTO: snapped 1 cables; 532 skipped; 0 bend-radius warnings."])
    assert (len(chain["feeders"]), len(chain["trenches"])) == (14, 11)
    assert (len(chain["snapped_cables"]), len(chain["snapped_strings"])) == (0, 1)
    assert sorted((row["from"], row["to"]) for row in chain["feeders"]) == sorted(
        (int(k), v) for k, v in chain["after"]["setting"]["L1ToL2Assignments"].items())
    assert validate_graph(g) == g
    assert canon_sha(g) == "d76ce856b7b9342ab599a686de08a99e578ab3c783f642463145c16352c27b01"
    assert deserialize_graph(serialize_graph(g)) == g
    assert feeders_follow_topology(g) is True
    assert require_current_export(g) == g
    index = dependency_index(g)
    feeders = [item for item in g["routes"] if item["route_kind"] == "feeder"]
    by_source = {item["from_ref"]: item["id"] for item in feeders}
    for inverter in g["inverters"]:
        if inverter["is_l2"]:
            assert index[inverter["id"]] == {item["id"] for item in feeders if item["to_ref"] == inverter["id"]}
        else:
            assert index[inverter["id"]] == {inverter["l2_ref"], by_source[inverter["id"]]}
    riders = [s for s in g["strings"] if s.get("pathway_ref") is not None]
    assert len(riders) == 1
    for item in g["routes"]:
        if item["route_kind"] == "trench":
            assert index[item["id"]] == {s["id"] for s in riders if s["pathway_ref"] == item["id"]}
    assert canon_sha({k: sorted(v) for k, v in index.items()}) == (
        "436461a0d203e1bb907e1d6644731fdcb4d8a48ef42cbacd3fbfb7e54d19ddf5")
