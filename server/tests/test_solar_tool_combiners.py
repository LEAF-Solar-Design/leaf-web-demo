"""LEAFCOMBINERAUTO as a Studio tool (sf-w2-combiners-tool): the drawing's recorded combiner intake, carried by
the stored drawing intake through the source_intake trusted input, bound to the live graph and placed by
solar_combiner_graph; the placed combiner boxes, string reassignments, feeds, DC homeruns and feeders persisted in
one commit that stales every schedule (ROUTES_CHANGED) and advances the revision once. C5 through the tool path:
14 new devices, 346 DC legs, 14 feeders; the commit rail's replay proof; every refusal on a Studio code."""
import copy
import hashlib
import json
import math
import re
import sys
from collections import Counter
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
import solar_combiner_graph as cg
import solar_local_graph
import solar_tools
import store
from solar_design_graph import GraphValidationError, validate_graph
from solar_sizing_client import digest
from solar_solve_results import finish_mutation
from test_w1_design_graph import app_id, graph  # noqa: F401
from test_w1_local_graph_adapter import held
from test_w1_local_graph_jobs import isolated_jobs, no_network  # noqa: F401
from test_w1_local_graph_rail import _api, body
from test_solar_w2_registration import REF, expected_declaration, head_graph, latest
import test_solar_combiner_graph as cgt
import test_solar_ground_route_kinds as rk

TOOL = "solar-combiners"
TENANT = "fixture-tenant"
INVALID = "INVALID_COMBINER_REQUEST"
HARDWARE = {"model": "fixture-combiner", "max_dc_voltage": 1500, "max_ac_power_kw": 1}
CREATED_AT = "2026-09-17T00:00:00Z"
I4_SHA = "fb2a70ff57244ec29292cbaa9ff1febba2233e670ae1e4e3f0e3b4426d02293b"
C5_TOOL_SHA = "2bf76c5170333bfae0c78f48c958947b67c14223662b0db51806795257abaa3f"
FIRST_L1 = "leaf:inverter:18bb0017-c47f-4b1c-b22f-f9fb1ef59105"
NUMBER = {"type": "number", "exclusiveMinimum": 0, "maximum": 1000000}


def sha(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"),
                                     allow_nan=False).encode("utf-8")).hexdigest()


def builtin():
    return solar_local_graph._load_builtin(TOOL)


def i4():
    """(i4 graph, combiner intake, outline groups): the committed i4 drawing as a graph (piece one's fixture)."""
    return cgt.i4_graph(graph.__wrapped__())


def source(intake, groups):
    return {"combiner_intake": intake, "panel_groups": groups}


def request(rev=0, **hardware):
    return {"expected_rev": rev, "hardware": dict(HARDWARE, **hardware)}


def place(g, intake, groups, params=None):
    snapshot = copy.deepcopy((g, intake, groups, params))
    out = builtin().run(g, params or request(g["rev"]), source_intake=source(intake, groups))
    assert (g, intake, groups, params) == snapshot
    return out


def refused(g, intake, groups, params=None, source_intake=None):
    with pytest.raises(GraphValidationError) as error:
        builtin().run(copy.deepcopy(g), params or request(g["rev"]),
                      source_intake=source(intake, groups) if source_intake is None else source_intake)
    return error.value.code, error.value.path


@pytest.fixture(scope="module")
def c5():
    g, intake, groups = i4()
    return g, intake, groups, place(g, intake, groups)


# ------------------------------------------------------------------ constants and codes --

def test_combiners_constants():
    b = builtin()
    assert (b.TOOL, b.INVALID, b.STALE_REASON) == (TOOL, INVALID, "ROUTES_CHANGED")
    assert b.REQUEST_KEYS == frozenset({"expected_rev", "hardware"})
    assert b.HARDWARE_KEYS is cg.HARDWARE_KEYS
    assert (b.MAX_MODEL, b.MAX_RATING) == (128, 1_000_000)
    assert set(b.CODE_MAP) == set(cg.CODES)


def test_combiners_codes_are_studio_codes():
    rail = (ROOT / "web" / "src" / "lib" / "ribbonClusters.js").read_text(encoding="utf-8")
    table = rail[rail.index("export const SOLAR_REFUSAL_REASONS"):]
    table = set(re.findall(r"^\s+([a-z][a-z0-9_]*):", table[:table.index("})")], re.M))
    b = builtin()
    mapped = {code.lower() for code in b.CODE_MAP.values() if code != INVALID}
    assert mapped == {"valid_settings_required", "unassigned_strings_required", "invalid_drawing_context",
                      "solar_output_not_current", "capability_not_ready", "valid_strings_required"}
    assert mapped <= table and all(code == code.upper() for code in b.CODE_MAP.values())
    text = (SERVER / solar_tools.get(TOOL)["builtin"]).read_text(encoding="utf-8")
    reasons = set(re.findall(r'"input_reason":\s*"([a-z][a-z0-9_]{0,63})"', text))
    assert reasons == {"valid_settings_required", "unassigned_strings_required", "string_collectors_required",
                       "valid_strings_required"}
    assert reasons <= table and ".lower()" not in text
    literals = set(re.findall(r'_refuse\(\s*"([A-Z][A-Z0-9_]*)"', text))
    assert literals == {"INVALID_DRAWING_CONTEXT", "CAPABILITY_NOT_READY"}
    assert {code.lower() for code in literals} <= table


# ------------------------------------------------------------------ C5 through the tool --

def test_combiners_c5_places_and_persists(c5):
    g, _, _, out = c5
    assert sha(g) == I4_SHA and sha(out) == C5_TOOL_SHA
    assert (out["rev"], out["parent_rev"]) == (1, 0)
    assert [key for key in g if g[key] != out[key]] == ["rev", "parent_rev", "strings", "inverters", "routes", "extra"]
    l1 = [i for i in out["inverters"] if not i["is_l2"]]
    assert len(out["inverters"]) == 22 and len(l1) == 14 and l1[0]["id"] == FIRST_L1
    assert [i["id"] for i in out["inverters"][:8]] == [i["id"] for i in g["inverters"]]
    assert sorted(i["number"] for i in l1) == list(range(1, 15))
    assert all((i["equipment_type"], i["model"], i["max_dc_voltage"], i["max_ac_power_kw"]) ==
               ("combiner_box", "fixture-combiner", 1500, 1) for i in l1)
    assert Counter(r["route_kind"] for r in out["routes"]) == {"start homerun": 173, "end homerun": 173, "feeder": 14}
    assert all(re.fullmatch(r"leaf:(inverter|route):[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}",
                            e["id"]) for e in l1 + out["routes"])
    old = {e["id"]: e for key in ("inverters", "strings", "routes") for e in g[key]}
    changed = [e for key in ("inverters", "strings", "routes") for e in out[key] if old.get(e["id"]) != e]
    assert Counter((e["kind"], e["id"] in old) for e in changed) == {
        ("route", False): 360, ("string", True): 173, ("inverter", False): 14, ("inverter", True): 8}
    assert all(e["rev"] == 1 and e["provenance"]["last_writer"] == TOOL and e["provenance"]["tool_id"] == TOOL
               and e["provenance"]["source_rev"] == 0 for e in changed)
    assert all(e["provenance"]["created_at"] == CREATED_AT for e in l1 + out["routes"])
    assert out["extra"]["solve_coverage"] == {"duplicate_panel_refs": [], "unassigned_panel_refs": []}


def test_combiners_c5_equals_the_module_placement(c5):
    g, intake, groups, out = c5
    b = builtin()
    expected, receipt = cg.place_combiners(g, intake, groups, hardware=HARDWARE, new_id=b._minter(g),
                                           created_at=CREATED_AT)
    assert (receipt["combiners"], receipt["strings_served"], receipt["l2_fed"], receipt["homerun_routes"],
            receipt["feeder_routes"]) == (14, 173, 8, 346, 14)
    assert receipt["combiner_ids"][0] == FIRST_L1
    assert finish_mutation(g, expected, TOOL) == out


def test_combiners_c5_reprojects_to_the_kernel_answer(c5):
    """The persisted graph projects back to C5: 22 devices, 346 DC legs, 14 feeders, and the kernel's own
    L1ToL2Assignments and L1ToL2InputAssignments. (CombinerStringL1Assignments is each string's inverter_ref,
    pinned by the module equality above; HomerunRouting is the legs themselves.)"""
    out = c5[3]
    state, _ = cg.rb.state_from_graph(out)
    assert len(state["rows"]["device"]) == 22
    assert Counter(row.get("cable_kind") for row in state["rows"]["cable"]) == {"dc-homerun": 346, "feeder": 14}
    recorded, intake, groups = cgt.ct.committed_fixture()
    after, _ = cg.cab.combiner_auto_place(recorded, groups, cgt.ct.CAPTURE, cgt.ct.PLAN, intake)
    assert {key for key in after["setting"] if recorded["setting"].get(key) != after["setting"][key]} == {
        "CombinerStringL1Assignments", "HomerunRouting", "L1ToL2Assignments", "L1ToL2InputAssignments"}
    for key in ("L1ToL2Assignments", "L1ToL2InputAssignments"):
        assert state["setting"][key] == after["setting"][key]


def test_combiners_is_deterministic(c5):
    g, intake, groups, out = c5
    assert place(g, intake, groups) == out


def test_combiners_minted_ids_are_fresh_and_repeatable(c5):
    g = c5[0]
    first, second = builtin()._minter(g), builtin()._minter(g)
    ids = [first("inverter") for _ in range(200)] + [first("route") for _ in range(200)]
    assert ids == [second("inverter") for _ in range(200)] + [second("route") for _ in range(200)]
    held_ids = {e["id"] for key in ("inverters", "strings", "routes") for e in g[key]}
    assert len(set(ids)) == 400 and not set(ids) & held_ids and ids[0] == FIRST_L1


def test_combiners_minter_skips_held_ids(c5, monkeypatch):
    g, b = c5[0], builtin()
    monkeypatch.setattr(b, "digest", lambda value: "fixed")
    free = b._minter(g)
    first, second = free("inverter"), free("inverter")
    monkeypatch.setattr(b, "entities", lambda value: [{"id": first}])
    assert b._minter(g)("inverter") == second


@pytest.mark.parametrize("validity", [{"state": "valid", "reasons": []},
                                      {"state": "stale", "reasons": ["settings_changed"]}], ids=["valid", "stale"])
def test_combiners_every_schedule_goes_stale(validity):
    g, intake, groups = i4()
    schedule = copy.deepcopy(graph.__wrapped__()["schedules"][0])
    schedule["source_refs"], schedule["validity"] = [g["strings"][0]["id"]], validity
    g["schedules"] = [schedule]
    g = validate_graph(g)
    out = place(g, intake, groups)
    stale = out["schedules"][0]
    assert stale["validity"] == {"state": "stale", "reasons": ["ROUTES_CHANGED"]}
    assert stale["rev"] == 1 and stale["provenance"]["last_writer"] == TOOL
    if validity["state"] == "valid":
        assert sha(out) == "c3073f64df5ef75132556a9f85b2c9c9f37424a701ed49ec8bf485b46567d993"


def test_combiners_trench_survives():
    g, intake, groups = i4()
    g["routes"] = [rk.trench(900)]
    g = validate_graph(g)
    out = place(g, intake, groups)
    assert out["routes"][0] == g["routes"][0] and len(out["routes"]) == 361
    assert sha(out) == "bcdce2b0bbae8055805aefb8899998048119078f49e4918b1831d6cc18338dee"


def test_combiners_prior_l2_homeruns_are_repointed():
    g, intake, groups = i4()
    by_id = {i["id"]: i for i in g["inverters"]}
    n = 5000
    for string in g["strings"]:
        for kind, end in (("start homerun", string["route"][0]), ("end homerun", string["route"][-1])):
            n += 1
            target = by_id[string["inverter_ref"]]["position"]
            g["routes"].append({
                "id": app_id("route", n), "kind": "route", "rev": 0,
                "provenance": {"created_by": "fixture", "created_at": CREATED_AT, "last_writer": "fixture",
                               "source_rev": 0},
                "extra": {}, "validity": {"state": "valid", "reasons": []}, "route_kind": kind,
                "points": [list(end), list(target)], "from_ref": string["id"], "to_ref": string["inverter_ref"],
                "wire_gauge": "10 AWG", "length_ft": math.dist(end, target) / 0.3048, "point_units": "m",
                "length_units": "ft"})
    g = validate_graph(g)
    out = place(g, intake, groups)
    assert sha(out) == "48609150b7eaa7ec05eb64e05b696090558593a6039719f5f2b6e384a74981fd"
    assert Counter(r["route_kind"] for r in out["routes"]) == {"start homerun": 173, "end homerun": 173, "feeder": 14}
    l1 = {i["id"] for i in out["inverters"] if not i["is_l2"]}
    homeruns = [r for r in out["routes"] if r["route_kind"] != "feeder"]
    assert {r["id"] for r in homeruns} == {r["id"] for r in g["routes"]}
    assert all(r["to_ref"] in l1 for r in homeruns)


# ------------------------------------------------------------------ readiness and refusals --

def _no_l2(g):
    g["inverters"] = []
    for string in g["strings"]:
        string.update(inverter_ref=None, to_ref=None)


def _no_strings(g):
    g["strings"] = []
    for inverter in g["inverters"]:
        inverter["input_assignments"] = []


READINESS = [
    ("i4", None, None),
    ("w1-not-l2-mode", "w1", "valid_settings_required"),
    ("placed", "placed", "unassigned_strings_required"),
    ("no-l2", _no_l2, "string_collectors_required"),
    ("no-strings", _no_strings, "valid_strings_required"),
]


@pytest.mark.parametrize("name,change,reason", READINESS, ids=[row[0] for row in READINESS])
def test_combiners_readiness(c5, name, change, reason):
    g, intake, groups, out = c5
    if change == "w1":
        g = graph.__wrapped__()
    elif change == "placed":
        g = out
    elif change is not None:
        g = copy.deepcopy(g)
        change(g)
        g = validate_graph(g)
    expected = {"input_ready": reason is None, "input_reason": reason}
    assert builtin().input_readiness(g) == expected
    assert availability.w1_local_commit_inputs(copy.deepcopy(g))[TOOL] == expected
    if reason is not None:
        assert refused(g, intake, groups) == (reason.upper(), "<root>")


def test_combiners_unresolved_units_are_the_rails():
    g = i4()[0]
    g["project"]["units"]["meters_per_unit"] *= 2
    assert availability.w1_local_commit_inputs(g)[TOOL] == {"input_ready": False, "input_reason": "unresolved_units"}


@pytest.mark.parametrize("params,code", [
    (None, INVALID), ([], INVALID), ({"expected_rev": 0}, INVALID), ({"hardware": HARDWARE}, INVALID),
    (dict(request(), cancel=True), INVALID), ({"expected_rev": "0", "hardware": HARDWARE}, INVALID),
    (request(model=""), INVALID), (request(model="   "), INVALID), (request(model="m" * 129), INVALID),
    (request(max_dc_voltage=True), INVALID), (request(max_dc_voltage=0), INVALID),
    (request(max_ac_power_kw=1000000.5), INVALID), (request(max_dc_voltage="1500"), INVALID),
    ({"expected_rev": 0, "hardware": {"model": "m", "max_dc_voltage": 1}}, INVALID),
    (request(mppt_count=1), INVALID), ({"expected_rev": 0, "hardware": None}, INVALID),
    (request(max_dc_voltage=float("nan")), "NONFINITE_NUMBER"),
])
def test_combiners_request_shape_fails_closed(c5, params, code):
    g, intake, groups, _ = c5
    original = copy.deepcopy(params)
    with pytest.raises(GraphValidationError) as error:
        builtin().run(copy.deepcopy(g), params, source_intake=source(intake, groups))
    assert error.value.code == code
    assert repr(params) == repr(original)


def test_combiners_request_bounds_are_inclusive(c5):
    g, intake, groups, _ = c5
    out = place(g, intake, groups, request(model="m" * 128, max_dc_voltage=1000000, max_ac_power_kw=1e-9))
    assert sha(out) == "3b8c01f6905e251b6fc650660ecbafb7f6e4e191e5e99bf50920a55c0799ee41"


def test_combiners_stale_revision_refuses(c5):
    g, intake, groups, _ = c5
    assert refused(g, intake, groups, request(1)) == ("STALE_GRAPH_REVISION", "<root>")


@pytest.mark.parametrize("carried,path", [
    (None, "source_intake"), ([], "source_intake"), ({"panel_groups": []}, "combiner_intake"),
    ({"combiner_intake": [], "panel_groups": []}, "combiner_intake"), ({"combiner_intake": {}}, "panel_groups"),
    ({"combiner_intake": {}, "panel_groups": {"A": []}}, "panel_groups"),
], ids=["none", "list", "no-intake", "intake-list", "no-groups", "groups-object"])
def test_combiners_drawing_inputs_fail_closed(c5, carried, path):
    g = c5[0]
    with pytest.raises(GraphValidationError) as error:
        builtin().run(copy.deepcopy(g), request(), source_intake=copy.deepcopy(carried))
    assert (error.value.code, error.value.path) == ("INVALID_DRAWING_CONTEXT", path)


@pytest.mark.parametrize("group", [
    {"handle": "A", "outlines": None},
    {"handle": "A"},
    {"handle": "A", "outlines": [], "extra": True},
    {"handle": [], "outlines": []},
    {"handle": 1, "outlines": []},
    {"handle": "", "outlines": []},
    [],
    {"handle": "A", "outlines": {}},
], ids=["outlines-none", "outlines-missing", "extra-key", "handle-list", "handle-int",
        "handle-empty", "group-list", "outlines-dict"])
def test_combiners_panel_group_shape_refused(group):
    g, intake, _ = i4()
    assert refused(g, intake, [group]) == ("INVALID_DRAWING_CONTEXT", "panel_groups")


def _intake(change):
    def apply(g, intake, groups):
        change(intake)
        return g, intake, groups
    return apply


def _moved_l2(g, intake, groups):
    g["inverters"][0]["position"][0] += 1.0
    return validate_graph(g), intake, groups


def _feet(g, intake, groups):
    g["project"]["units"].update(drawing_units="ft", meters_per_unit=0.3048, drawing_unit_is_feet=True)
    return validate_graph(g), intake, groups


def _reversed(g, intake, groups):
    g["strings"][0]["route"].reverse()
    return validate_graph(g), intake, groups


MODULE_REFUSALS = [
    ("format", _intake(lambda i: i.update(format="combiner-intake-v2")),
     "INVALID_DRAWING_CONTEXT", "COMBINER_INTAKE_INVALID"),
    ("existing-l1", _intake(lambda i: i["inputs"].update(existingL1s=[{"number": 1}])),
     "UNASSIGNED_STRINGS_REQUIRED", "COMBINER_EXISTING_L1"),
    ("units", _feet, "SOLAR_OUTPUT_NOT_CURRENT", "COMBINER_INTAKE_UNITS_MISMATCH"),
    ("context", _intake(lambda i: i["commandContext"].update(l2NumMppt=5)),
     "SOLAR_OUTPUT_NOT_CURRENT", "COMBINER_INTAKE_CONTEXT_MISMATCH"),
    ("l2-moved", _moved_l2, "SOLAR_OUTPUT_NOT_CURRENT", "COMBINER_INTAKE_L2_MISMATCH"),
    ("string-reversed", _reversed, "SOLAR_OUTPUT_NOT_CURRENT", "COMBINER_INTAKE_STRING_MISMATCH"),
    ("string-dropped", _intake(lambda i: i["inputs"]["preBuiltStrings"].pop()),
     "SOLAR_OUTPUT_NOT_CURRENT", "COMBINER_INTAKE_STRING_MISMATCH"),
    ("outlines-bad-point", lambda g, i, _: (g, i, [{"handle": "A", "outlines": [[[0, "x"]]]}]),
     "INVALID_DRAWING_CONTEXT", "COMBINER_OUTLINES_INVALID"),
    ("outlines-empty", lambda g, i, _: (g, i, []), "CAPABILITY_NOT_READY", "COMBINER_NOT_PORTED"),
    ("tracker-rows", _intake(lambda i: i["inputs"].update(trackerRows=[{"id": 1}])),
     "CAPABILITY_NOT_READY", "COMBINER_NOT_PORTED"),
    ("kernel", _intake(lambda i: i["inputs"].setdefault("options", {}).update(EndPolicy=1)),
     "CAPABILITY_NOT_READY", "COMBINER_KERNEL_REFUSED"),
]


@pytest.mark.parametrize("name,change,code,module_code", MODULE_REFUSALS, ids=[row[0] for row in MODULE_REFUSALS])
def test_combiners_module_refusals_map_to_studio_codes(name, change, code, module_code):
    g, intake, groups = change(*i4())
    assert refused(g, intake, groups) == (code, module_code)
    assert builtin().CODE_MAP[module_code] == code


def test_combiners_postcondition_and_bridge_failures_fail_closed(c5, monkeypatch):
    g, intake, groups, _ = c5
    monkeypatch.setattr(cg, "_prove", lambda *a: cg._fail("COMBINER_POSTCONDITION_FAILED"))
    assert refused(g, intake, groups) == ("CAPABILITY_NOT_READY", "COMBINER_POSTCONDITION_FAILED")
    monkeypatch.undo()
    late = copy.deepcopy(g)
    late["project"]["provenance"]["created_at"] = "2026-09-17T00:00:00+00:00"
    late = validate_graph(late)
    code, path = refused(late, intake, groups)
    assert (code, path) == ("CAPABILITY_NOT_READY", "BRIDGE_INVALID_REQUEST")


# ------------------------------------------------------------------ registry and schema --

def test_combiners_registry_and_catalog(monkeypatch):
    declaration = solar_tools.get(TOOL)
    actual = copy.deepcopy(declaration)
    description = actual["record"].pop("description")
    assert type(description) is str and description.strip() and len(description) <= 1024
    expected = expected_declaration(
        TOOL, "equipment", 105, INVALID, [], ["combiner-auto-place"], None,
        {"hardware": {"type": "object", "properties": {
            "model": {"type": "string", "minLength": 1, "maxLength": 128, "pattern": "\\S"},
            "max_dc_voltage": NUMBER, "max_ac_power_kw": NUMBER},
            "required": ["model", "max_dc_voltage", "max_ac_power_kw"], "additionalProperties": False}},
        ["hardware"])
    expected.update(readiness={"kind": "hook"}, trusted_inputs=["source_intake"])
    assert actual == expected
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
    assert family["family_id"] == "equipment"
    assert row["solar"] == solar_tools.catalog_view(declaration["record"])
    assert row["params_schema"] == declaration["record"]["params"]


def test_combiners_params_schema():
    validator = Draft7Validator(solar_tools.trusted_record(TOOL)["params"])
    valid = request()
    assert validator.is_valid(valid)
    assert validator.is_valid(dict(valid, drawing_id="solar", expected_rev=2147483647,
                                   hardware=dict(HARDWARE, model="m" * 128, max_dc_voltage=1000000)))
    for key in ("expected_rev", "hardware"):
        missing = dict(valid)
        del missing[key]
        assert not validator.is_valid(missing), key
    for patch in [{"unknown": 1}, {"cancel": True}, {"expected_rev": -1}, {"expected_rev": 2147483648},
                  {"drawing_id": "d" * 129}, {"hardware": dict(HARDWARE, model="")},
                  {"hardware": dict(HARDWARE, model="   ")}, {"hardware": dict(HARDWARE, model="m" * 129)},
                  {"hardware": dict(HARDWARE, max_dc_voltage=0)}, {"hardware": dict(HARDWARE, max_ac_power_kw=1000001)},
                  {"hardware": dict(HARDWARE, max_dc_voltage="1500")}, {"hardware": dict(HARDWARE, extra=1)},
                  {"hardware": {"model": "m", "max_dc_voltage": 1}}]:
        assert not validator.is_valid(dict(valid, **patch)), patch


# ------------------------------------------------------------------ the commit rail --

def stored(tmp_path, monkeypatch, g, carried):
    """A drawing whose stored intake carries the graph and `carried` (real ingest, no stubs)."""
    monkeypatch.setenv("LEAF_DRAWING_STORE", "legacy")
    monkeypatch.setenv("LEAF_DRAWING_MUTATIONS_ENABLED", "1")
    monkeypatch.delenv("LEAF_DRAWING_MUTATIONS_FENCE_FILE", raising=False)
    monkeypatch.setenv("LEAF_UPLOAD_IMPORT_MUTATIONS_ENABLED", "1")
    backend = store.FilesystemBackend(str(tmp_path / "drawings"))
    intake = {"dwg": {}, "layers": [], "polylines": [], "inserts": [], "faces3d": [], "blockdefs": [],
              "geodata": None, **copy.deepcopy(carried), "solar_design_graph": g,
              "solar_design_graph_sha256": digest(g)}
    path = tmp_path / "seed.json"
    path.write_text(json.dumps(intake), encoding="utf-8")
    store.ingest_drawing(backend, TENANT, str(path), drawing_id="solar")
    return backend


def dispatch(backend, params, *, source_version=1, job_id="combiners-job"):
    with held(backend) as fence:
        return solar_local_graph.run_local_graph_commit(
            backend, TENANT, TOOL, dict(params, drawing_id="solar"), drawing_id="solar",
            source_version=source_version, holder="fixture-owner", fence=fence, job_id=job_id)


def test_combiners_rail_persists_proves_and_refuses_a_second_run(c5, tmp_path, monkeypatch):
    g, intake, groups, out = c5
    backend = stored(tmp_path, monkeypatch, g, source(intake, groups))
    receipt = dispatch(backend, request())
    assert receipt["new_version"] == {"drawing_id": "solar", "version": 2, "parent": 1}
    assert (receipt["before_rev"], receipt["after_rev"], receipt["drawing_changed"]) == (0, 1, True)
    assert head_graph(backend) == out and latest(backend) == 2
    proof = solar_local_graph.graph_commit_provenance(
        receipt, dict(request(), drawing_id="solar"), TENANT, "combiners-job", TOOL, 1, backend=backend)
    assert (proof["execution_mode"], proof["new_version"]) == ("local_graph_commit", 2)
    with pytest.raises(ValueError, match="graph commit terminal proof rejected"):
        solar_local_graph.graph_commit_provenance(
            receipt, dict(request(max_ac_power_kw=2), drawing_id="solar"), TENANT, "combiners-job", TOOL, 1,
            backend=backend)
    _, key = store.resolve_version(backend, TENANT, "solar", 2)
    kept = json.loads(backend.get(key))
    assert kept["combiner_intake"] == intake and kept["panel_groups"] == groups
    with pytest.raises(GraphValidationError) as error:
        dispatch(backend, request(1), source_version=2, job_id="combiners-again")
    assert error.value.code == "UNASSIGNED_STRINGS_REQUIRED"
    with pytest.raises(GraphValidationError) as error:
        dispatch(backend, request(0), source_version=2, job_id="combiners-stale")
    assert error.value.code == "STALE_GRAPH_REVISION"
    assert latest(backend) == 2


@pytest.mark.parametrize("name,code,path", [
    ("no-intake", "INVALID_DRAWING_CONTEXT", "combiner_intake"),
    ("l2-moved", "SOLAR_OUTPUT_NOT_CURRENT", "COMBINER_INTAKE_L2_MISMATCH"),
])
def test_combiners_rail_refusals_are_atomic(tmp_path, monkeypatch, name, code, path):
    g, intake, groups = i4()
    carried = source(intake, groups)
    if name == "no-intake":
        del carried["combiner_intake"]
    else:
        g, _, _ = _moved_l2(g, intake, groups)
    backend = stored(tmp_path, monkeypatch, g, carried)
    with pytest.raises(GraphValidationError) as error:
        dispatch(backend, request())
    assert (error.value.code, error.value.path) == (code, path)
    assert head_graph(backend) == g and latest(backend) == 1


def _api_for(isolated_jobs, no_network, tmp_path, monkeypatch, carried_change=None):
    g, intake, groups = i4()
    carried = source(intake, groups)
    if carried_change is not None:
        carried_change(carried)
    backend = stored(tmp_path, monkeypatch, g, carried)
    yield from _api(backend, tmp_path, monkeypatch)


@pytest.fixture
def api_c5(isolated_jobs, no_network, tmp_path, monkeypatch):
    yield from _api_for(isolated_jobs, no_network, tmp_path, monkeypatch)


@pytest.fixture
def api_no_intake(isolated_jobs, no_network, tmp_path, monkeypatch):
    yield from _api_for(isolated_jobs, no_network, tmp_path, monkeypatch, lambda c: c.pop("combiner_intake"))


def post(api, params):
    api[2][TOOL] = solar_tools.trusted_record(TOOL)
    return api[0].post("/api/run?wait=1", json=body(api, TOOL, params))


def jobs_now():
    return [jobs.get_job(row["job_id"]) for row in jobs._query("SELECT job_id FROM jobs")]


def test_combiners_run_rail_commits_one_job(api_c5):
    response = post(api_c5, request())
    assert response.status_code == 200, response.text
    env = response.json()
    assert env["ok"] is True and env["execution_provenance"]["execution_mode"] == "local_graph_commit"
    assert env["result"]["new_version"] == {"drawing_id": "solar", "version": 2, "parent": 1}
    assert store.load_manifest(api_c5[1], TENANT, "solar")["head"] == 2
    records = jobs_now()
    assert len(records) == 1 and records[0]["status"] == "complete"
    assert sha(head_graph(api_c5[1])) == C5_TOOL_SHA


@pytest.mark.parametrize("params", [{"expected_rev": 0}, dict(request(), unknown=1), request(model="")],
                         ids=["no-hardware", "unknown-key", "empty-model"])
def test_combiners_broker_refuses_schema_violations(api_c5, params):
    response = post(api_c5, params)
    env = response.json()
    assert env.get("ok") is not True, response.text
    records = jobs_now()
    assert all(record["status"] != "complete" for record in records)
    reasons = [env.get("reason_code"), (env.get("error") or {}).get("reason_code")]
    reasons += [(record.get("error") or {}).get("reason_code") for record in records]
    assert "tool_params_invalid" in reasons, response.text
    manifest = store.load_manifest(api_c5[1], TENANT, "solar")
    assert manifest["head"] == manifest["latest"] == 1


def test_combiners_builtin_refusal_reaches_the_rail(api_no_intake):
    response = post(api_no_intake, request())
    assert response.status_code == 400, response.text
    env = response.json()
    assert env.get("ok") is not True
    assert env["error"]["message"] == env["reason_code"] == "INVALID_DRAWING_CONTEXT"
    assert store.load_manifest(api_no_intake[1], TENANT, "solar")["head"] == 1
    records = jobs_now()
    assert len(records) == 1 and records[0]["status"] == "failed"
