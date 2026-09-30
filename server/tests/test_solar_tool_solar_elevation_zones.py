"""Elevation zones on the W1 graph: ZONEHEIGHT and the two zone assigns, receipts replayed."""
import copy
import hashlib
import json
import sys
from pathlib import Path

import pytest
from jsonschema import Draft7Validator

SERVER = Path(__file__).resolve().parents[1]
ROOT = SERVER.parent
sys.path.insert(0, str(SERVER))
sys.path.insert(0, str(SERVER / "tests"))

import catalog
import deps
import entitlements
import jobs
import product_capability_availability as availability
import solar_batch2_simple as kernel
import solar_local_graph
import solar_tools
import store
from solar_design_graph import GraphValidationError, entities, validate_graph
from solar_sizing_client import sizing_basis
from solar_solve_results import sync_assignments, upstream_basis
from test_w1_design_graph import app_id, graph  # noqa: F401
from test_w1_local_graph_adapter import held
from test_w1_local_graph_jobs import isolated_jobs, no_network  # noqa: F401
from test_w1_local_graph_rail import api, body  # noqa: F401
from test_w1_solve_commit import seed
from test_solar_w2_registration import dispatch, head_graph, latest

TOOL = "solar-elevation-zones"
TENANT = "fixture-tenant"
P1, P2, P3 = [app_id("panel", n) for n in (1, 2, 3)]
S1, S2 = [app_id("string", n) for n in (1, 2)]
F1, F2 = [app_id("frame", n) for n in (1, 2)]
DANGLING = app_id("panel", 99)
BASE = {
    "G": "67ab25efcda14a69da65a4184cf5d362ac19a85dcf266f722a9528f5881737c3",
}


def sha(value):
    return hashlib.sha256(json.dumps(
        value, sort_keys=True, separators=(",", ":"), allow_nan=False,
        ensure_ascii=False).encode("utf-8")).hexdigest()


def zone(name, value, colour, strings=(), panels=()):
    return {"name": name, "offset": {"kind": "length", "value": value, "unit": "in"}, "colour": colour,
            "strings": list(strings), "panels": list(panels)}


ZONE_1 = zone("Zone 1", 24.0, 1)
ZONE_2 = zone("Zone 2", 6.0, 3, [S2], [P2, DANGLING])


def variant(graph, name="G"):
    g = copy.deepcopy(graph)
    validate_graph(g)
    assert sha(g) == BASE["G"]
    extra = g["extra"]
    if name in ("Z1", "TF"):
        extra["elevation_zones"] = [copy.deepcopy(ZONE_1)]
    elif name == "Z2":
        extra["elevation_zones"] = [copy.deepcopy(ZONE_1), copy.deepcopy(ZONE_2)]
    elif name == "GR":
        # A Ground frame needs a tracker block; a Ground drawing without panel groups is enough here.
        g["project"]["installation_design"] = "Ground"
        g["frames"] = []
        for panel in g["panels"]:
            panel.update(frame_ref=None, matrix_cell=None)
    elif name == "YD":
        g["project"]["units"].update(drawing_units="yd", meters_per_unit=0.9144)
    if name == "TF":
        frame = g["frames"][0]
        second = copy.deepcopy(frame)
        second.update(id=F2, name="Attic", panel_refs=[P2, P3], module_columns=2, module_slots=2,
                      module_width_along_row=2, module_height_across_row=1)
        second["matrix"] = [frame["matrix"][0][1:]]
        second["panel_assignments"] = frame["panel_assignments"][1:]
        frame.update(panel_refs=[P1], module_columns=1, module_slots=1)
        frame["matrix"] = [frame["matrix"][0][:1]]
        frame["panel_assignments"] = frame["panel_assignments"][:1]
        for panel in g["panels"][1:]:
            panel["frame_ref"] = F2
            panel["matrix_cell"] = {"row": 0, "col": panel["matrix_cell"]["col"] - 1}
        g["frames"].append(second)
        sync_assignments(g)
    return validate_graph(g)


def builtin():
    return solar_local_graph._load_builtin(TOOL)


def call(g, operation, **params):
    """One operation's full answer (graph, zones and, for panels, recoloured and skipped)."""
    return builtin().OPERATIONS[operation](g, dict(params, expected_rev=g["rev"]))


def only_zones_changed(before, after):
    """Every entity, every other top-level key and both design digests are byte-identical."""
    def rest(g):
        return {key: value for key, value in g.items() if key not in ("rev", "parent_rev", "extra")}

    def extra(g):
        return {key: value for key, value in g["extra"].items() if key != "elevation_zones"}

    return (rest(before) == rest(after) and extra(before) == extra(after)
            and entities(before) == entities(after)
            and upstream_basis(before) == upstream_basis(after)
            and sizing_basis(before) == sizing_basis(after)
            and (after["rev"], after["parent_rev"]) == (before["rev"] + 1, before["rev"]))


# --- the receipts ------------------------------------------------------------------------------------------------


def receipt(capability, fixture):
    path = ROOT / "docs" / "parity" / "receipts" / capability / f"{fixture}.json"
    value = json.loads(path.read_text(encoding="utf-8"))
    assert value["capability"] == capability and value["comparator"]["verdict"] == "pass"
    return value["comparison"]


def row_zone(row):
    return {key: row[key] for key in ("name", "offset", "colour", "strings", "panels")}


SIZES = {"77.0x38.5": (77.0, 38.5), "79.3x38.6": (79.3, 38.6)}


def replay_graph(graph, intake):
    """The z1-assign intake as a valid W1 graph: panel i (intake order) is app_id("panel", i + 1), one
    one-row frame per intake size with that size's module width and height, one empty string per intake
    string handle; no inverters, routes, schedules or electrical zones. Returns (graph, {id: handle})."""
    g = copy.deepcopy(graph)
    handles, members = {}, {}
    for number, row in enumerate(intake["panels"], 1):
        handles[app_id("panel", number)] = row["panel"]
        members.setdefault(row["size"], []).append(app_id("panel", number))
    frames, place = [], {}
    for number, size in enumerate(sorted(members), 1):
        refs = members[size]
        frame = copy.deepcopy(graph["frames"][0])
        frame.update(
            id=app_id("frame", number), name=f"Group {number}", panel_refs=list(refs), module_rows=1,
            module_columns=len(refs), module_slots=len(refs), module_width_along_row=SIZES[size][0],
            module_height_across_row=SIZES[size][1], electrical_zone_ref=None, sequences=[],
            matrix=[[{"code": "panel", "panel_ref": ref, "seq": None, "inverter_id": None,
                      "string_input_number": None, "x": float(col), "y": 0.0, "angle": 0}
                     for col, ref in enumerate(refs)]],
            panel_assignments=[{"panel_ref": ref, "string_ref": None, "seq": None, "inverter_id": None,
                                "string_input_number": None} for ref in refs])
        frames.append(frame)
        place.update({ref: (frame["id"], col) for col, ref in enumerate(refs)})
    panels = []
    for number in range(1, len(intake["panels"]) + 1):
        panel = copy.deepcopy(graph["panels"][0])
        ref = app_id("panel", number)
        panel.update(id=ref, frame_ref=place[ref][0], matrix_cell={"row": 0, "col": place[ref][1]},
                     centre=[float(number - 1), 0.0], assignment={"string_ref": None, "seq": None})
        panels.append(panel)
    strings = []
    for number, handle in enumerate(intake["strings"], 1):
        handles[app_id("string", number)] = handle
        string = copy.deepcopy(graph["strings"][0])
        string.update(id=app_id("string", number), circuit_tag=f"S{number}", ordered_panel_refs=[],
                      module_count=0, from_ref=None, to_ref=None, route=[], inverter_ref=None)
        strings.append(string)
    g.update(panels=panels, frames=frames, strings=strings, electrical_zones=[], inverters=[],
             routes=[], schedules=[])
    sync_assignments(g)
    return validate_graph(g), handles


def test_elevation_zone_kernel_c6():
    state = json.loads((ROOT / "docs/parity/evidence/batch2/z0-state.json").read_text(encoding="utf-8"))
    zones = kernel.zone_height(state, {"add": 1, "offset_in": 24})
    assert zones == [ZONE_1]
    assert sha(zones) == "50ca8584fd5290e0fc51ed8c4fd8a938531cc66cfaa0cbed81e3b5e262ae8391"
    for side in ("plugin", "studio"):
        rows = receipt("zone-height-settings", "batch2-z1")[side]["after"]["rows"]
        assert [row_zone(row) for row in rows] == zones


def test_elevation_zone_receipts_replay(graph):
    intake = json.loads((ROOT / "docs/parity/evidence/batch2/z1-assign.json").read_text(encoding="utf-8"))
    replay, handles = replay_graph(graph, intake)
    assert (len(replay["panels"]), len(replay["strings"]), len(replay["frames"])) == (2345, 173, 2)
    assert sha(replay) == "9637fee22c46fa800ccf6e0458cc499a39df8e94d156a05f7bee7f5df9c99fc6"

    def as_handles(zones):
        return [dict(z, panels=[handles[r] for r in z["panels"]], strings=[handles[r] for r in z["strings"]])
                for z in zones]

    added = call(replay, "add-zones", add=1, offset=24)
    assert sha(added["graph"]) == "a47aa295249812d3a023659ebda6d64feeafa85bf7769aec11ae73cd9a420ea9"
    reference = next(ref for ref, handle in handles.items() if handle == "7FA3")
    panels = call(added["graph"], "assign-panels", zone_name="Zone 1", reference_panel_ref=reference)
    assert (len(panels["zones"][0]["panels"]), len(panels["recoloured"]), panels["skipped"]) == (1660, 1660, 685)
    assert sha(panels["graph"]) == "7e455cb8d4d284b46339bdebee40e3bc1cd2746025f02697d70569412d7c124a"
    strings = call(panels["graph"], "assign-strings", zone_name="Zone 1")
    assert len(strings["zones"][0]["strings"]) == 173
    assert sha(strings["graph"]) == "364f549b1906bfca701b4fc1b6db0cdd1ac8a54d6eb20cae23cfde957f933bb6"
    for side in ("plugin", "studio"):
        z1 = receipt("zone-height-settings", "batch2-z1")[side]["after"]["rows"]
        z2 = receipt("elevation-zone-assign-panels", "batch2-z2")[side]["after"]["rows"]
        z3 = receipt("elevation-zone-assign-strings", "batch2-z3")[side]["after"]["rows"]
        assert added["zones"] == [row_zone(row) for row in z1]
        assert as_handles(panels["zones"]) == [row_zone(r) for r in z2 if r["type"] == "elevation-zone"]
        recoloured = [r for r in z2 if r["type"] == "recoloured"]
        assert [handles[ref] for ref in panels["recoloured"]] == [r["panel"] for r in recoloured]
        assert {r["colour"] for r in recoloured} == {1}
        assert as_handles(strings["zones"]) == [row_zone(row) for row in z3]
    assert sha(as_handles(panels["zones"])) == "0ef2ca6f532028266528ad19b52c46713e65ba7ce2c0b076649782a9d5ef5b81"
    assert sha(as_handles(strings["zones"])) == "1f05a6207e4b7d5149291b17fd1f4eca2d0e168268ee58717253c66da5ac031f"


# --- the fixture cases -------------------------------------------------------------------------------------------

NAMES = {P1: "P1", P2: "P2", P3: "P3", S1: "S1", S2: "S2", DANGLING: "D"}


def short(zones):
    return [dict(z, panels=[NAMES[r] for r in z["panels"]], strings=[NAMES[r] for r in z["strings"]])
            for z in zones]


def shown(name, value, colour, strings=(), panels=()):
    return {"name": name, "offset": {"kind": "length", "value": value, "unit": "in"}, "colour": colour,
            "strings": list(strings), "panels": list(panels)}


@pytest.mark.parametrize("name,params,zones,graph_sha", [
    ("G", {"add": 1, "offset": 24}, [shown("Zone 1", 24.0, 1)],
     "c2835ec5e4d6a805a7493084c474783b862104744632bd895fd69bc6ae583566"),
    ("G", {"add": 3, "offset": 12.345},
     [shown("Zone 1", 0.0, 1), shown("Zone 2", 0.0, 3), shown("Zone 3", 12.35, 5)],
     "2eb514e5798d36fb9f95431fe38fb7dfed34ed55b72df385b84cd516c7b500a8"),
    ("Z2", {"add": 1, "offset": 0},
     [shown("Zone 1", 24.0, 1), shown("Zone 2", 6.0, 3, ["S2"], ["P2", "D"]), shown("Zone 3", 0.0, 1)],
     "3299f86c2a5f9d61bf5b29abb512bea5a17f5e8f1af4de8ca03a0592b9b629eb"),
    ("G", {"add": 1, "offset": 10000}, [shown("Zone 1", 10000.0, 1)],
     "a088dda44bb3af27e1043568cefe40b61f660b94002424adf6741cb524cf093d"),
    ("G", {"add": 1, "offset": 0.005}, [shown("Zone 1", 0.01, 1)],
     "ede7d2d9375bd4037edb0e684f7c182260b67af64051cdf8091c7714705a3c2d"),
])
def test_elevation_zone_add(graph, name, params, zones, graph_sha):
    g = variant(graph, name)
    before = copy.deepcopy(g)
    answer = call(g, "add-zones", **params)
    after = answer["graph"]
    assert answer["added"] == params["add"]
    assert g == before
    assert short(answer["zones"]) == zones
    assert after["extra"]["elevation_zones"] == answer["zones"]
    assert sha(after) == graph_sha
    assert only_zones_changed(before, after)
    assert builtin().run(copy.deepcopy(before), dict(params, operation="add-zones", expected_rev=0)) == after


@pytest.mark.parametrize("name,params,zones,recoloured,skipped,graph_sha", [
    ("Z1", {"zone_name": "zone 1", "reference_panel_ref": P1}, [shown("Zone 1", 24.0, 1, [], ["P1", "P2", "P3"])],
     ["P1", "P2", "P3"], 0, "8a64186e93237c8f785517f7e6cb08f1c5beeb7315b24f18dc1f5fdfb2780183"),
    ("TF", {"zone_name": "Zone 1", "reference_panel_ref": P1}, [shown("Zone 1", 24.0, 1, [], ["P1"])],
     ["P1"], 2, "db3df34df2329032171ea6554e0ab5b25dd98896f45ef09fa093ec7dfe387ae8"),
    ("TF", {"zone_name": "Zone 1", "reference_panel_ref": P2}, [shown("Zone 1", 24.0, 1, [], ["P2", "P3"])],
     ["P2", "P3"], 1, "77635dee9888adb342eb7ee4a7555ffe62398a187deef9a26604d3eba494a612"),
    ("Z2", {"zone_name": "Zone 1", "reference_panel_ref": P1},
     [shown("Zone 1", 24.0, 1, [], ["P1", "P2", "P3"]), shown("Zone 2", 6.0, 3, ["S2"], ["D"])],
     ["P1", "P2", "P3"], 0, "1d1aa04f42c6014384ad76fbc0504330f8ac9dcbc36f184bbada3c9cc1f5ca1b"),
    ("Z2", {"zone_name": "ZONE 2", "reference_panel_ref": P3},
     [shown("Zone 1", 24.0, 1), shown("Zone 2", 6.0, 3, ["S2"], ["D", "P1", "P2", "P3"])],
     ["P1", "P3"], 0, "934eb8bed662376cb0f86bb08d128ceddd453fa75a8aecdab1a3c3f29d3eeb3e"),
])
def test_elevation_zone_assign_panels(graph, name, params, zones, recoloured, skipped, graph_sha):
    g = variant(graph, name)
    before = copy.deepcopy(g)
    answer = call(g, "assign-panels", **params)
    assert g == before
    assert short(answer["zones"]) == zones
    assert [NAMES[ref] for ref in answer["recoloured"]] == recoloured and answer["skipped"] == skipped
    assert answer["graph"]["extra"]["elevation_zones"] == answer["zones"]
    assert sha(answer["graph"]) == graph_sha
    assert only_zones_changed(before, answer["graph"])


@pytest.mark.parametrize("name,zone_name,zones,graph_sha", [
    ("Z1", "Zone 1", [shown("Zone 1", 24.0, 1, ["S1", "S2"])],
     "2b6becdfd5eb855f126220a732a35390156172b2231206b8639803a2ed7d1f2a"),
    ("Z2", "Zone 1", [shown("Zone 1", 24.0, 1, ["S1", "S2"]), shown("Zone 2", 6.0, 3, [], ["P2", "D"])],
     "8c5158db892178aeb2d34f7bb838f4c7074a36eca3d3693e8a8b6ea9c364bcd2"),
    ("Z2", "Zone 2", [shown("Zone 1", 24.0, 1), shown("Zone 2", 6.0, 3, ["S1", "S2"], ["P2", "D"])],
     "e457cc2c6d6372623d3dfefd64faec495ad3bdc876a12f10e2a1bdae5e009116"),
])
def test_elevation_zone_assign_strings(graph, name, zone_name, zones, graph_sha):
    g = variant(graph, name)
    before = copy.deepcopy(g)
    answer = call(g, "assign-strings", zone_name=zone_name)
    assert g == before
    assert short(answer["zones"]) == zones
    assert sha(answer["graph"]) == graph_sha
    assert only_zones_changed(before, answer["graph"])


def test_elevation_zone_zone_colours(graph):
    assert builtin().zone_colours(variant(graph, "Z2")) == {P1: 256, P2: 3, P3: 256}
    assert builtin().zone_colours(variant(graph)) == {P1: 256, P2: 256, P3: 256}


# --- refusals ----------------------------------------------------------------------------------------------------


def _stored(value):
    def patch(g):
        g["extra"]["elevation_zones"] = value
    return patch


def _units_doubled(g):
    g["project"]["units"]["meters_per_unit"] *= 2


def _no_strings(g):
    g.update(strings=[], routes=[], schedules=[])
    for inverter in g["inverters"]:
        inverter["input_assignments"] = []
    sync_assignments(g)


ADD = {"operation": "add-zones", "add": 1, "offset": 24}
PANELS = {"operation": "assign-panels", "zone_name": "Zone 1", "reference_panel_ref": P1}
STRINGS = {"operation": "assign-strings", "zone_name": "Zone 1"}
FT_ZONE = dict(ZONE_1, offset={"kind": "length", "value": 2.0, "unit": "ft"})


@pytest.mark.parametrize("name,patch,params,code", [
    ("GR", None, ADD, "ROOFTOP_REQUIRED"),
    ("YD", None, ADD, "UNSUPPORTED_DRAWING_UNITS"),
    ("G", _units_doubled, ADD, "UNRESOLVED_UNITS"),
    ("G", None, dict(ADD, expected_rev=5), "STALE_GRAPH_REVISION"),
    ("G", _stored([FT_ZONE]), ADD, "ELEVATION_ZONE_UNITS_CHANGED"),
    ("G", _stored({"Zone 1": ZONE_1}), ADD, "INVALID_STORED_ELEVATION_ZONES"),
    ("G", _stored([dict(ZONE_1, colour="1")]), PANELS, "INVALID_STORED_ELEVATION_ZONES"),
    ("G", _stored([dict(ZONE_1, panels=["7FA3"])]), PANELS, "INVALID_STORED_ELEVATION_ZONES"),
    ("G", _stored([dict(ZONE_1, strings=[P1])]), STRINGS, "INVALID_STORED_ELEVATION_ZONES"),
    ("G", _stored([dict(ZONE_1, name="")]), STRINGS, "INVALID_STORED_ELEVATION_ZONES"),
    ("G", _stored([dict(ZONE_1, name="Z" * 256)]), STRINGS, "INVALID_STORED_ELEVATION_ZONES"),
    ("G", None, PANELS, "MISSING_ELEVATION_ZONE"),
    ("Z1", None, dict(PANELS, zone_name="Zone 9"), "MISSING_ELEVATION_ZONE"),
    ("Z1", None, dict(PANELS, reference_panel_ref=DANGLING), "MISSING_PANEL"),
    ("Z1", _no_strings, STRINGS, "ELEVATION_ZONES_UNCHANGED"),
])
def test_elevation_zone_refusals_are_atomic(graph, name, patch, params, code):
    g = variant(graph, name)
    if patch is not None:
        patch(g)
    request = dict({"expected_rev": 0}, **params)
    before, sent = copy.deepcopy(g), copy.deepcopy(request)
    with pytest.raises(GraphValidationError) as error:
        builtin().run(g, request)
    assert error.value.code == code
    assert g == before and request == sent


def test_elevation_zone_repeat_is_unchanged(graph):
    once = call(variant(graph, "Z1"), "assign-panels", zone_name="Zone 1", reference_panel_ref=P1)["graph"]
    with pytest.raises(GraphValidationError, match="ELEVATION_ZONES_UNCHANGED"):
        call(once, "assign-panels", zone_name="Zone 1", reference_panel_ref=P2)
    twice = call(variant(graph, "Z1"), "assign-strings", zone_name="Zone 1")["graph"]
    with pytest.raises(GraphValidationError, match="ELEVATION_ZONES_UNCHANGED"):
        call(twice, "assign-strings", zone_name="zone 1")


def test_elevation_zone_limit_and_kernel_refusal(graph, monkeypatch):
    module = builtin()
    with monkeypatch.context() as patch:
        patch.setattr(module.kernel, "MAX_ZONES", 1)
        with pytest.raises(GraphValidationError, match="ELEVATION_ZONE_LIMIT_EXCEEDED"):
            call(variant(graph, "Z1"), "add-zones", add=1, offset=1)

    def refuse(*args, **kwargs):
        raise kernel.BatchTwoError("refused")

    for attribute, operation, params in (
            ("zone_height", "add-zones", {"add": 1, "offset": 1}),
            ("zone_assign_panels", "assign-panels", {"zone_name": "Zone 1", "reference_panel_ref": P1}),
            ("zone_assign_strings", "assign-strings", {"zone_name": "Zone 1"})):
        with monkeypatch.context() as patch:
            patch.setattr(module.kernel, attribute, refuse)
            with pytest.raises(GraphValidationError, match="ELEVATION_ZONE_MAPPING_FAILED"):
                call(variant(graph, "Z1"), operation, **params)


@pytest.mark.parametrize("params", [
    None, [], {}, {"expected_rev": 0}, {"operation": 7, "expected_rev": 0},
    {"operation": ["add-zones"], "expected_rev": 0}, {"operation": "delete-zone", "expected_rev": 0},
    dict(ADD, expected_rev=0, zone_name="Zone 1"), {"operation": "add-zones", "expected_rev": 0, "add": 1},
    dict(ADD, expected_rev=0, add=0), dict(ADD, expected_rev=0, add=True), dict(ADD, expected_rev=0, add=1001),
    dict(ADD, expected_rev=0, add=1.0), dict(ADD, expected_rev=0, offset=-0.5),
    dict(ADD, expected_rev=0, offset=10000.01), dict(ADD, expected_rev=0, offset=True),
    dict(ADD, expected_rev=0, offset="24"),
    dict(PANELS, expected_rev=0, zone_name=""), dict(PANELS, expected_rev=0, zone_name="Z" * 256),
    dict(PANELS, expected_rev=0, zone_name=7), dict(PANELS, expected_rev=0, reference_panel_ref=""),
    dict(PANELS, expected_rev=0, reference_panel_ref="x" * 129),
    dict(PANELS, expected_rev=0, reference_panel_ref=[P1]),
    {"operation": "assign-panels", "expected_rev": 0, "zone_name": "Zone 1"},
    dict(STRINGS, expected_rev=0, reference_panel_ref=P1), dict(STRINGS, expected_rev=0, drawing_id="solar"),
])
def test_elevation_zone_request_shape_fails_closed(graph, params):
    g = variant(graph, "Z1")
    before = copy.deepcopy(g)
    with pytest.raises(GraphValidationError, match="INVALID_ELEVATION_ZONE_REQUEST"):
        builtin().run(g, copy.deepcopy(params))
    assert g == before


# --- registry, catalog, readiness --------------------------------------------------------------------------------

DECLARATION = {
    "schema": "leaf.solar-tool.v1", "name": TOOL, "builtin": "builtins/solar_elevation_zones.py",
    "family": "placement", "adapter": "local-graph-commit", "entitlement": "run_write",
    "requires_persisted_graph": True, "seedable": False,
    "invalid_request_code": "INVALID_ELEVATION_ZONE_REQUEST", "readiness": {"kind": "hook"},
    "engine": "server-builtin", "interaction": {"mode": "form"}, "record_store": "registry",
    "record": {
        "name": TOOL, "version": "1.0.0", "kind": "script", "family_id": "placement",
        "engine_op": "solar_elevation_zones", "entry": "builtins/solar_elevation_zones.py",
        "params": {
            "type": "object",
            "properties": {
                "drawing_id": {"type": "string", "maxLength": 128},
                "operation": {"type": "string", "enum": ["add-zones", "assign-panels", "assign-strings"]},
                "expected_rev": {"type": "integer", "minimum": 0, "maximum": 2147483647},
                "add": {"type": "integer", "minimum": 1, "maximum": 1000},
                "offset": {"type": "number", "minimum": 0, "maximum": 10000},
                "zone_name": {"type": "string", "minLength": 1, "maxLength": 255},
                "reference_panel_ref": {"type": "string", "minLength": 1, "maxLength": 128},
            },
            "required": ["operation", "expected_rev"],
            "additionalProperties": False,
        },
        "returns": {"type": "object"}, "capabilities": ["drawing.write"], "allow_local_fallback": False,
    },
    "ledger": ["zone-height-settings", "elevation-zone-assign-panels", "elevation-zone-assign-strings"],
    "trusted_inputs": [], "maturity": "preview", "wave": 2, "order": 25, "scenario": "w2-rooftop",
}


def test_elevation_zone_registry_and_catalog(monkeypatch):
    declaration = solar_tools.get(TOOL)
    actual = copy.deepcopy(declaration)
    description = actual["record"].pop("description")
    assert type(description) is str and description.strip() and len(description) <= 1024
    assert actual == DECLARATION
    assert TOOL in solar_tools.local_graph_tools() and TOOL not in solar_tools.local_graph_read_tools()
    assert availability.SOLAR_CAPABILITIES[TOOL]["adapter"] == "local-graph-commit"
    assert TOOL not in availability.W1_CAPABILITIES and len(availability.W1_CAPABILITIES) == 9
    assert entitlements.tool_required_capability(solar_tools.trusted_record(TOOL)) == "run_write"
    monkeypatch.setattr(deps, "tenant_repo_dir", lambda tenant: None)
    monkeypatch.setattr(deps, "load_tenant_repo_tools", lambda tenant: [])
    monkeypatch.setattr(deps, "_AUTHORED", [])
    families = catalog.build_catalog(deps.all_tools(TENANT))
    found = [(family, row) for family in families for row in family["capabilities"] if row["name"] == TOOL]
    assert len(found) == 1
    family, row = found[0]
    assert family["family_id"] == "placement"
    assert row["solar"] == solar_tools.catalog_view(declaration["record"])
    assert row["params_schema"] == declaration["record"]["params"]


def test_elevation_zone_params_schema():
    validator = Draft7Validator(solar_tools.trusted_record(TOOL)["params"])
    for params in (dict(ADD, expected_rev=0), dict(PANELS, expected_rev=3), dict(STRINGS, expected_rev=0),
                   dict(ADD, expected_rev=0, offset=10000, drawing_id="solar")):
        assert validator.is_valid(params), params
    for params in (ADD, dict(ADD, expected_rev=0, add=0), dict(ADD, expected_rev=0, add=1001),
                   dict(ADD, expected_rev=0, offset=-1), dict(ADD, expected_rev=0, offset=10000.5),
                   dict(ADD, expected_rev=0, operation="delete-zone"), dict(ADD, expected_rev=-1),
                   dict(PANELS, expected_rev=0, zone_name=""), dict(PANELS, expected_rev=0, zone_name="Z" * 256),
                   dict(PANELS, expected_rev=0, reference_panel_ref="x" * 129), dict(STRINGS, expected_rev=0, x=1)):
        assert not validator.is_valid(params), params


def test_elevation_zone_readiness(graph):
    g = variant(graph)
    assert availability.w1_graph_readiness(g)[TOOL] == {"input_ready": True, "input_reason": None}
    assert availability.w1_local_commit_inputs(variant(graph, "GR"))[TOOL] == {
        "input_ready": False, "input_reason": "rooftop_required"}
    assert availability.w1_local_commit_inputs(variant(graph, "YD"))[TOOL] == {
        "input_ready": False, "input_reason": "drawing_units_unsupported"}
    _units_doubled(g)
    assert availability.w1_local_commit_inputs(g)[TOOL] == {
        "input_ready": False, "input_reason": "unresolved_units"}


# --- the rail ----------------------------------------------------------------------------------------------------


def post(api, params):
    api[2][TOOL] = solar_tools.trusted_record(TOOL)
    return api[0].post("/api/run?wait=1", json=body(api, TOOL, params))


def test_elevation_zone_run_rail_commits_two_versions(api, graph):
    g = variant(graph)
    first = post(api, dict(ADD, expected_rev=0))
    assert first.status_code == 200, first.text
    env = first.json()
    assert env["ok"] is True and env["execution_provenance"]["execution_mode"] == "local_graph_commit"
    assert env["result"]["new_version"] == {"drawing_id": "solar", "version": 2, "parent": 1}
    assert sha(head_graph(api[1])) == "c2835ec5e4d6a805a7493084c474783b862104744632bd895fd69bc6ae583566"
    second = post(api, dict(PANELS, expected_rev=1))
    assert second.status_code == 200, second.text
    assert second.json()["result"]["new_version"] == {"drawing_id": "solar", "version": 3, "parent": 2}
    head = head_graph(api[1])
    assert short(head["extra"]["elevation_zones"]) == [
        shown("Zone 1", 24.0, 1, [], ["P1", "P2", "P3"])]
    assert head == call(call(g, "add-zones", add=1, offset=24)["graph"], "assign-panels",
                        zone_name="Zone 1", reference_panel_ref=P1)["graph"]
    assert store.load_manifest(api[1], TENANT, "solar")["head"] == 3
    records = [jobs.get_job(row["job_id"]) for row in jobs._query("SELECT job_id FROM jobs")]
    assert len(records) == 2 and all(record["status"] == "complete" for record in records)


@pytest.mark.parametrize("patch", [{"add": 0}, {"unknown": 1}, {"operation": "delete-zone"}])
def test_elevation_zone_broker_refuses_schema_violations(api, patch):
    response = post(api, dict(ADD, expected_rev=0, **patch))
    env = response.json()
    assert env.get("ok") is not True, response.text
    records = [jobs.get_job(row["job_id"]) for row in jobs._query("SELECT job_id FROM jobs")]
    assert all(record["status"] != "complete" for record in records)
    reasons = [env.get("reason_code"), (env.get("error") or {}).get("reason_code")]
    reasons += [(record.get("error") or {}).get("reason_code") for record in records]
    assert "tool_params_invalid" in reasons, response.text
    manifest = store.load_manifest(api[1], TENANT, "solar")
    assert manifest["head"] == manifest["latest"] == 1


def test_elevation_zone_builtin_refusal_reaches_the_rail(api):
    response = post(api, dict(PANELS, expected_rev=0))
    assert response.status_code == 400, response.text
    env = response.json()
    assert env.get("ok") is not True
    assert env["error"]["message"] == env["reason_code"] == "MISSING_ELEVATION_ZONE"
    assert store.load_manifest(api[1], TENANT, "solar")["head"] == 1
    records = [jobs.get_job(row["job_id"]) for row in jobs._query("SELECT job_id FROM jobs")]
    assert len(records) == 1 and records[0]["status"] == "failed"


def test_elevation_zone_dispatch_replays_and_refuses_stale(graph, tmp_path, monkeypatch):
    backend, _ = seed(tmp_path, monkeypatch, variant(graph, "Z1"))
    with held(backend) as fence:
        receipt = dispatch(backend, fence, TOOL, dict(STRINGS, expected_rev=0))
        assert receipt["new_version"] == {"drawing_id": "solar", "version": 2, "parent": 1}
        assert (receipt["before_rev"], receipt["after_rev"], receipt["replayed"]) == (0, 1, False)
        again = dispatch(backend, fence, TOOL, dict(STRINGS, expected_rev=0))
        assert again["new_version"] == receipt["new_version"] and again["replayed"] is True
        with pytest.raises(GraphValidationError, match="STALE_GRAPH_REVISION"):
            solar_local_graph.run_local_graph_commit(
                backend, TENANT, TOOL, dict(STRINGS, expected_rev=0, drawing_id="solar"), drawing_id="solar",
                source_version=2, holder="fixture-owner", fence=fence, job_id="elevation-zone-stale")
    assert latest(backend) == 2
    assert short(head_graph(backend)["extra"]["elevation_zones"]) == [
        shown("Zone 1", 24.0, 1, ["S1", "S2"])]
