"""Equipment on a converted Ground design (sf-w3-solve-equipment): the drawing's compact Ground frames
(codec leaf.solar-ground-slots.v1) carry slot panels that are panels to string sizing, to the equipment
kernel and to the equipment readiness chain, exactly as on the graph's expansion and without expanding.
Sizing confirms on a converted design, solar-assign-equipment assigns strings over slot panels, and the
whole chain (conversion, sizing, strings, equipment) commits through the local graph rail with every
terminal proof accepted. Every expected value was measured by running the reference with python -B."""
from __future__ import annotations

import copy
from datetime import datetime, timezone
from hashlib import sha256
import importlib.util
import json
from pathlib import Path
import subprocess
import sys

import pytest

SERVER = Path(__file__).resolve().parents[1]
ROOT = SERVER.parent
sys.path.insert(0, str(SERVER))
sys.path.insert(0, str(SERVER / "tests"))

import jobs  # noqa: E402
import product_capability_availability as availability  # noqa: E402
import solar_physical_state as ps  # noqa: E402  (first: its write_loop import puts da/ (store) on sys.path)
import solar_physical_head as ph  # noqa: E402
import solar_design_graph as sdg  # noqa: E402
import solar_equipment  # noqa: E402
import solar_ground_graph_codec as codec  # noqa: E402
import solar_local_graph  # noqa: E402
import solar_sizing_client as cloud  # noqa: E402
import solar_solve_results as ssr  # noqa: E402
import solar_tools  # noqa: E402
import store  # noqa: E402
from leaf_cloud_client import canonical_bytes  # noqa: E402
from leaf_cloud_grants import CloudGrant  # noqa: E402
from solar_design_graph import GraphValidationError, validate_graph  # noqa: E402
from test_solar_ground_graph_codec import _load, ground_base  # noqa: E402
from test_solar_tool_trackers_to_panel_groups import VIEW, small_doc, stored  # noqa: E402
from test_solar_w2_registration import head_graph, latest  # noqa: E402
from test_w1_design_graph import app_id, graph  # noqa: E402,F401
from test_w1_local_graph_adapter import held  # noqa: E402
from test_w1_local_graph_jobs import isolated_jobs, no_network  # noqa: E402,F401
from test_w1_local_graph_rail import _api, body  # noqa: E402

TENANT = "fixture-tenant"
DRAWING = "solar"
ZIP = "44224"
EQUIPMENT = "solar-assign-equipment"
INV = "leaf:inverter:00000000-0000-4000-8000-0000000000e1"
RECORDED = json.loads((SERVER / "tests" / "fixtures" / "w1_string_length_recorded_response.json")
                      .read_text(encoding="utf-8"))
# Measured on the reference (python -B, Windows and the base 3293e8cd).
W1_BASIS = "94c0e240f8f2ce87feb0874374a8592284955719da41bf4a60f3fbc3ea3f550a"
CONVERTED_BASIS = "fb773c11b8a39a7e5786b4301a4a3e66cb568fb6273214f4b92c6409e09a75c2"
SIZED_SHA = "aa7e815c169780207578046380c915903180340502f5c0071f623205b0e07fd8"
STRUNG_SHA = "d9fbe549a60289ec083f339992f9bd580207c0bded2197535627df5dcf9e6717"
EQUIPPED_SHA = "9f344ba1e0746ccf22364e550e5a0ae11f5564f35fda61bfacd70c165ee83b47"
# The whole readiness map has 50 entries after physical shade and physical export.
# Both physical readers add input_reason None on this W1 fixture.
W1_READINESS_SHA = "8a4bc9e338c008b85b9392bfab333697a275475387c566321e4cd8727b62e852"


class FixedDatetime:
    @staticmethod
    def now(tz=None):
        return datetime(2026, 10, 1, tzinfo=timezone.utc)


def lsha(value):
    return sha256(json.dumps(value, sort_keys=True, separators=(",", ":"),
                             allow_nan=False).encode("utf-8")).hexdigest()


def _code(fn, *args, **kwargs):
    sdg._reset_validation_caches()
    with pytest.raises(GraphValidationError) as error:
        fn(*args, **kwargs)
    return error.value.code


def _fresh(name):
    """A fresh load of one builtin file, so a patched name never leaks into the rail's cached copy."""
    spec = importlib.util.spec_from_file_location("_ground_equipment_" + name, SERVER / "builtins" / (name + ".py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def service(monkeypatch):
    """The String Sizer, recorded (the size-strings suite's own fixture shape): every call answers the
    recorded response under a resolved grant; nothing reaches the network."""
    calls = []

    def post(request, grant):
        calls.append(request.wire())
        return canonical_bytes(RECORDED["response"])

    monkeypatch.setattr(cloud, "post_string_length", post)
    monkeypatch.setattr(cloud, "resolve_grant", lambda reference, tenant: CloudGrant(tenant, "fixture-token"))
    return calls


@pytest.fixture
def pinned(monkeypatch):
    """Deterministic string ids app_id("string", 901), 902, ... and one clock for strings and equipment."""
    counter = [901]

    def fake_new_id(kind):
        value = app_id(kind, counter[0])
        counter[0] += 1
        return value

    module = _fresh("solar_string_add")
    monkeypatch.setattr(module, "new_id", fake_new_id)
    monkeypatch.setattr(module, "datetime", FixedDatetime)
    monkeypatch.setattr(solar_equipment, "datetime", FixedDatetime)
    return module


def converted(w1):
    """The W1 graph as an empty Ground design, converted from the small two-tracker layout: frame A holds
    three slots, frame B two (rev 1)."""
    g = ground_base(w1)
    g["project"]["zip_code"] = ZIP
    conversion = solar_local_graph._load_builtin("solar-trackers-to-panel-groups")
    return conversion.run(g, {"expected_rev": 0},
                          physical_state={"view": copy.deepcopy(VIEW), "document": small_doc()})


def size_params(g, mode="global", targets=None):
    ids = [g["settings"]["id"]] if mode == "global" else targets
    return {"expected_rev": g["rev"], "mode": mode,
            "requests": {ref: dict(copy.deepcopy(RECORDED["request"]), **(
                {} if mode == "global" else {"module_name": "fixture-module",
                                             "full_inverter_name": "fixture-inverter"}))
                for ref in ids},
            "grant_ref": "fixture-grant", "confirm": True}


def sized(g, mode="global", targets=None):
    sdg._reset_validation_caches()
    return _fresh("solar_size_strings").run_bound(copy.deepcopy(g), size_params(g, mode, targets),
                                                  tenant_id=TENANT, job_id="size-job")


def slots(g):
    return list(ssr.slot_panel_ids(g))


def strung(g, add, runs=((0, 3), (3, 5))):
    """One string per run of slot panels, in slot order, through solar-string-add."""
    ids = slots(g)
    for start, stop in runs:
        sdg._reset_validation_caches()
        g = add.run(g, {"operation": "add-string", "expected_rev": g["rev"],
                        "ordered_panel_refs": ids[start:stop]})
    return g


def config(**changes):
    value = {"id": INV, "number": 1, "type_key": "A", "model": "fixture-inverter", "position": [5, 0, 0],
             "rotation": 0, "scale": [1, 1, 1], "block_name": "FixtureInverter", "layer": "0",
             "mppt_count": 1, "total_dc_inputs": 2, "mppt_inputs": {"A": 2}, "max_dc_voltage": 1500,
             "max_ac_power_kw": 5, "max_dc_power_kw": 5, "is_solaredge": False}
    value.update(changes)
    return value


def equipment_params(g, **changes):
    return {"expected_rev": g["rev"], "equipment": [config(**changes)],
            "assignments": [{"string_ref": s["id"], "inverter_ref": INV, "mppt_letter": "A", "input_number": n}
                            for n, s in enumerate(g["strings"])]}


def equip(g, **changes):
    sdg._reset_validation_caches()
    return solar_local_graph._load_builtin(EQUIPMENT).run(copy.deepcopy(g), equipment_params(g, **changes))


def reasons(g):
    sdg._reset_validation_caches()
    return {name: row["input_reason"] for name, row in availability.w1_graph_readiness(copy.deepcopy(g)).items()}


def zones_over(w1, g, runs):
    """Electrical zones over runs of slot panels (the W1 zone, re-pointed), on a copy of g."""
    g = copy.deepcopy(g)
    ids = slots(g)
    g["electrical_zones"] = []
    for n, (start, stop) in enumerate(runs, 1):
        zone = copy.deepcopy(w1["electrical_zones"][0])
        zone["id"] = app_id("zone-el", 700 + n)
        zone["name"] = f"Z{n}"
        zone["panel_refs"] = ids[start:stop]
        g["electrical_zones"].append(zone)
    return g


# ------------------------------------------------------------------ sizing on slot panels --

def test_ground_equipment_sizing_targets_count_slot_panels(graph):
    g = converted(graph)
    assert g["panels"] == [] and len(slots(g)) == 5
    assert cloud.sizing_targets(g, "global") == {g["settings"]["id"]: g["settings"]}
    # A Ground design with no frame and no panel still has nothing to size.
    assert _code(cloud.sizing_targets, ground_base(graph), "global") == "MISSING_PANEL"


@pytest.mark.parametrize("runs,expected", [
    (((0, 3), (3, 5)), ["Z1", "Z2"]),
    (((0, 4),), "INVALID_ZONE_COVERAGE"),
    (((0, 3), (2, 5)), "INVALID_ZONE_COVERAGE"),
], ids=["two-zones-cover-every-slot", "one-slot-uncovered", "slot-in-two-zones"])
def test_ground_equipment_sizing_zones_cover_slot_panels(graph, runs, expected):
    g = zones_over(graph, converted(graph), runs)
    validate_graph(copy.deepcopy(g))
    if isinstance(expected, list):
        targets = cloud.sizing_targets(g, "zones")
        assert [targets[ref]["name"] for ref in targets] == expected
    else:
        assert _code(cloud.sizing_targets, g, "zones") == expected


def test_ground_equipment_sizing_basis_equals_expansion(graph):
    g = converted(graph)
    assert cloud.sizing_basis(g) == cloud.sizing_basis(codec.expand_graph(g)) == CONVERTED_BASIS
    # The slot panels are what the basis adds: the same design without its blocks digests differently.
    bare = copy.deepcopy(g)
    for frame in bare["frames"]:
        del frame["ground_slots"]
    assert cloud.sizing_basis(bare) != CONVERTED_BASIS


@pytest.mark.parametrize("change", ["centre", "angle"])
def test_ground_equipment_sizing_basis_reads_slot_geometry(graph, change):
    g = converted(graph)
    frame = g["frames"][0]
    table = codec.decode_slots(frame["ground_slots"])
    centres = [[table.centres[2 * n], table.centres[2 * n + 1]] for n in range(len(table.ids))]
    angle = table.angle
    if change == "centre":
        centres[1] = [centres[1][0] + 0.5, centres[1][1]]
    else:
        angle = angle + 0.25
    frame["ground_slots"] = codec.encode_slots(list(table.ids), centres, angle, table.panel)
    assert cloud.sizing_basis(g) != CONVERTED_BASIS
    assert cloud.sizing_basis(g) == cloud.sizing_basis(codec.expand_graph(g))


def test_ground_equipment_plain_graph_never_imports_the_codec(graph, monkeypatch):
    w1 = copy.deepcopy(graph)
    monkeypatch.setitem(sys.modules, "solar_ground_graph_codec", None)
    assert cloud.compact_slot_tables(w1) == {}
    assert cloud.sizing_basis(w1) == W1_BASIS
    assert cloud.sizing_targets(w1, "global") == {w1["settings"]["id"]: w1["settings"]}
    assert lsha(reasons(w1)) == W1_READINESS_SHA


def test_ground_equipment_module_import_never_loads_the_codec():
    program = (
        "import sys\n"
        "sys.modules['solar_ground_graph_codec'] = None\n"
        "import solar_sizing_client, solar_equipment, product_capability_availability\n"
        "assert sys.modules['solar_ground_graph_codec'] is None\n"
        "print('OK')\n"
    )
    result = subprocess.run([sys.executable, "-B", "-c", program], cwd=str(SERVER),
                            capture_output=True, text=True, timeout=120)
    assert result.returncode == 0, result.stderr
    assert "OK" in result.stdout


def test_ground_equipment_size_strings_commits_on_a_converted_design(graph, service):
    g = converted(graph)
    out = sized(g)
    assert (out["rev"], out["parent_rev"], out["settings"]["panels_in_sequence"]) == (2, 1, 27)
    assert [frame["module_power_watts"] for frame in out["frames"]] == [595.0, 595.0]
    assert out["settings"]["extra"]["string_sizing"]["basis_sha256"] == cloud.sizing_basis(out)
    assert out["settings"]["global_string_sizing_confirmed"] is True
    cloud.require_sizing(out)
    assert validate_graph(copy.deepcopy(out)) == out
    assert lsha(out) == SIZED_SHA and len(service) == 1
    assert codec.expand_graph(out) == sized(codec.expand_graph(g))


def test_ground_equipment_size_strings_zones_read_each_frames_slots(graph, service):
    g = zones_over(graph, converted(graph), ((0, 3), (3, 5)))
    zone_ids = [zone["id"] for zone in g["electrical_zones"]]
    out = sized(g, "zones", zone_ids)
    # Each compact frame lies inside exactly one zone, so each takes that zone's recorded module power.
    assert [frame["module_power_watts"] for frame in out["frames"]] == [595.0, 595.0]
    assert [zone["panels_in_sequence"] for zone in out["electrical_zones"]] == [27, 27]
    assert out["settings"]["global_string_sizing_confirmed"] is False
    cloud.require_sizing(out)
    assert len(service) == 2
    assert codec.expand_graph(out) == sized(codec.expand_graph(g), "zones", zone_ids)


# ------------------------------------------------------------------ equipment on slot strings --

def test_ground_equipment_assigns_slot_strings(graph, service, pinned):
    g = strung(sized(converted(graph)), pinned)
    assert lsha(g) == STRUNG_SHA and [s["module_count"] for s in g["strings"]] == [3, 2]
    out = equip(g)
    assert (out["rev"], out["parent_rev"]) == (5, 4)
    assert [s["inverter_ref"] for s in out["strings"]] == [INV, INV]
    assert [s["to_ref"] for s in out["strings"]] == [INV, INV]
    assert [s["validity"] for s in out["strings"]] == [{"state": "valid", "reasons": []}] * 2
    assert out["inverters"][0]["input_assignments"] == [
        {"string_ref": s["id"], "mppt_letter": "A", "input_number": n} for n, s in enumerate(g["strings"])]
    assert out["inverters"][0]["validity"] == {"state": "valid", "reasons": []}
    assert out["extra"]["equipment"]["assignment_requests"] == equipment_params(g)["assignments"]
    # A compact frame stores no slot record: its blocks are untouched, the slot views derive.
    assert [f["ground_slots"] for f in out["frames"]] == [f["ground_slots"] for f in g["frames"]]
    assert solar_equipment.equipment_ready(out) is True
    assert validate_graph(copy.deepcopy(out)) == out
    assert lsha(out) == EQUIPPED_SHA


def test_ground_equipment_equals_the_expansion(graph, service, pinned):
    g = strung(sized(converted(graph)), pinned)
    assert codec.expand_graph(equip(g)) == equip(codec.expand_graph(g))
    # Frames of different module power: every slot is weighed against its own frame.
    g["frames"][1]["module_power_watts"] = 300.0
    validate_graph(copy.deepcopy(g))
    assert codec.expand_graph(equip(g)) == equip(codec.expand_graph(g))


@pytest.mark.parametrize("limit,state", [(2.4, "valid"), (2.3, "invalid")])
def test_ground_equipment_power_reads_each_slot_frame(graph, service, pinned, limit, state):
    g = strung(sized(converted(graph)), pinned)
    g["frames"][1]["module_power_watts"] = 300.0
    out = equip(g, max_dc_power_kw=limit)
    # 3 x 0.595 kW on frame A plus 2 x 0.300 kW on frame B is 2.385 kW.
    expected = [] if state == "valid" else ["EQUIPMENT_POWER_EXCEEDED"]
    assert out["inverters"][0]["validity"] == {"state": state, "reasons": expected}
    assert [s["validity"] for s in out["strings"]] == [{"state": state, "reasons": expected}] * 2


def test_ground_equipment_voltage_counts_slot_modules(graph, service, pinned):
    g = strung(sized(converted(graph)), pinned)
    # 54.49452456029573 V cold per module: 3 modules are 163.48 V, 2 modules 108.99 V.
    out = equip(g, max_dc_voltage=150)
    assert [s["validity"] for s in out["strings"]] == [
        {"state": "invalid", "reasons": ["EQUIPMENT_VOLTAGE_EXCEEDED"]}, {"state": "valid", "reasons": []}]
    assert out["inverters"][0]["validity"] == {"state": "invalid", "reasons": ["EQUIPMENT_VOLTAGE_EXCEEDED"]}
    assert solar_equipment.equipment_ready(out) is False


def test_ground_equipment_refuses_without_sizing(graph, service, pinned):
    g = strung(sized(converted(graph)), pinned)
    del g["settings"]["extra"]["string_sizing"]
    g["settings"]["global_string_sizing_confirmed"] = False
    validate_graph(copy.deepcopy(g))
    assert _code(solar_local_graph._load_builtin(EQUIPMENT).run, copy.deepcopy(g),
                 equipment_params(g)) == "SIZING_CONFIRMATION_REQUIRED"
    assert reasons(g)[EQUIPMENT] == "valid_strings_required"


def test_ground_equipment_b18_site(graph, service):
    """Studio's b18 site: 237 compact frames, 69,678 slots, every slot strung in runs of 27 inside its
    frame (2,607 strings) and assigned to one inverter input each."""
    evidence = _load("solar_ground_dsteps_evidence", ROOT / "scripts" / "solar_ground_dsteps_evidence.py")
    raw = (ROOT / "docs" / "parity" / "evidence" / "ground" / "terrain" / "intake.json").read_bytes()
    document = ps.physical_document(evidence.b18_state(json.loads(raw.decode("utf-8"))), drawing_units="m",
                                    source_sha256=sha256(raw).hexdigest(), capability="trackers-to-panelgroups")
    g = ground_base(graph)
    g["project"]["zip_code"] = ZIP
    g = solar_local_graph._load_builtin("solar-trackers-to-panel-groups").run(
        g, {"expected_rev": 0}, physical_state={"view": copy.deepcopy(VIEW), "document": document})
    g = sized(g)
    assert [frame["module_power_watts"] for frame in g["frames"]] == [595.0] * 237
    tables = cloud.compact_slot_tables(g)
    template = copy.deepcopy(graph["strings"][0])
    for frame in g["frames"]:
        ids = list(tables[frame["id"]].ids)
        for start in range(0, len(ids), 27):
            refs = ids[start:start + 27]
            string = copy.deepcopy(template)
            string.update(id=app_id("string", len(g["strings"]) + 1), circuit_tag=f"S{len(g['strings']) + 1}",
                          ordered_panel_refs=refs, module_count=len(refs), from_ref=refs[0], to_ref=refs[-1],
                          inverter_ref=None)
            g["strings"].append(string)
    count = len(g["strings"])
    assert count == 2607 and sum(s["module_count"] for s in g["strings"]) == 69678
    g["extra"]["solve_coverage"] = ssr.coverage(g)
    assert g["extra"]["solve_coverage"]["unassigned_panel_refs"] == []
    sdg._reset_validation_caches()
    g = validate_graph(g)
    params = {"expected_rev": g["rev"],
              "equipment": [config(mppt_inputs={"A": count}, total_dc_inputs=count, max_dc_power_kw=1000000,
                                   max_ac_power_kw=1000000)],
              "assignments": [{"string_ref": s["id"], "inverter_ref": INV, "mppt_letter": "A", "input_number": n}
                              for n, s in enumerate(g["strings"])]}
    sdg._reset_validation_caches()
    out = solar_local_graph._load_builtin(EQUIPMENT).run(copy.deepcopy(g), params)
    assert sum(s["validity"]["state"] == "valid" for s in out["strings"]) == 2607
    assert [s["inverter_ref"] for s in out["strings"]] == [INV] * 2607
    assert len(out["inverters"][0]["input_assignments"]) == 2607
    # 69,678 modules of 595 W: 41,458.41 kW on the one inverter, voltage 27 x 54.4945 V = 1,471.35 V.
    assert out["inverters"][0]["validity"] == {"state": "valid", "reasons": []}
    assert solar_equipment.equipment_ready(out) is True


# ------------------------------------------------------------------ readiness --

STAGES = {
    "sized": {"solar-size-strings": None, EQUIPMENT: "valid_strings_required",
              "solar-homeruns": "equipment_assignment_required"},
    "partly-strung": {"solar-size-strings": None, EQUIPMENT: "valid_strings_required",
                      "solar-homeruns": "equipment_assignment_required"},
    "strung": {"solar-size-strings": None, EQUIPMENT: None,
               "solar-homeruns": "equipment_assignment_required"},
    "equipped": {"solar-size-strings": None, EQUIPMENT: None,
                 "solar-homeruns": "routing_topology_required"},
}


def _stage(graph, pinned, name):
    g = sized(converted(graph))
    if name == "sized":
        return g
    if name == "partly-strung":
        return strung(g, pinned, runs=((0, 3),))
    g = strung(g, pinned)
    return g if name == "strung" else equip(g)


@pytest.mark.parametrize("name", list(STAGES))
def test_ground_equipment_readiness_chain(graph, service, pinned, name):
    g = _stage(graph, pinned, name)
    found = reasons(g)
    assert {key: found[key] for key in STAGES[name]} == STAGES[name]
    # The solve keeps refusing a compact design: its request binding refuses a compact frame.
    assert found["solar-solve-proposal"] == found["solar-commit-solve"] == "sized_panel_groups_required"
    # The equipment answer is the expansion's answer, decided without expanding.
    assert found[EQUIPMENT] == reasons(codec.expand_graph(g))[EQUIPMENT]


@pytest.mark.parametrize("frame", [0, 1])
def test_ground_equipment_readiness_reads_slot_template_validity(graph, service, pinned, frame):
    g = strung(sized(converted(graph)), pinned)
    g["frames"][frame]["ground_slots"]["panel"]["validity"] = {"state": "stale", "reasons": ["upstream_corrected"]}
    validate_graph(copy.deepcopy(g))
    assert reasons(g)[EQUIPMENT] == "valid_strings_required"
    assert reasons(codec.expand_graph(g))[EQUIPMENT] == "valid_strings_required"


def test_ground_equipment_partial_compaction_keeps_solve_unready(graph, service, pinned):
    g = strung(sized(converted(graph)), pinned)
    expanded = codec.expand_graph(g)
    partial = codec.compact_graph(expanded, [g["frames"][1]["id"]])
    validate_graph(copy.deepcopy(partial))
    sdg._reset_validation_caches()
    compact_readiness = availability.w1_graph_readiness(copy.deepcopy(partial))
    sdg._reset_validation_caches()
    expanded_readiness = availability.w1_graph_readiness(copy.deepcopy(expanded))
    for tool in ("solar-solve-proposal", "solar-commit-solve"):
        assert compact_readiness[tool] == {
            "input_ready": False, "input_reason": "sized_panel_groups_required"}
        assert expanded_readiness[tool] == {"input_ready": True, "input_reason": None}


def test_ground_equipment_ready_reads_slot_template_validity(graph, service, pinned):
    g = equip(strung(sized(converted(graph)), pinned))
    assert solar_equipment.equipment_ready(g) is True
    assert solar_equipment.equipment_ready(codec.expand_graph(g)) is True
    g["frames"][0]["ground_slots"]["panel"]["validity"] = {
        "state": "stale", "reasons": ["upstream_corrected"]}
    sdg._reset_validation_caches()
    validate_graph(copy.deepcopy(g))
    assert solar_equipment.equipment_ready(g) is False
    assert solar_equipment.equipment_ready(codec.expand_graph(g)) is False


# ------------------------------------------------------------------ the rail, end to end --

def dispatch(backend, tool, params, source_version, job_id):
    with held(backend) as fence:
        return solar_local_graph.run_local_graph_commit(
            backend, TENANT, tool, dict(params, drawing_id=DRAWING), drawing_id=DRAWING,
            source_version=source_version, holder="fixture-owner", fence=fence, job_id=job_id)


def prove(backend, receipt, tool, params, source_version, job_id):
    return solar_local_graph.graph_commit_provenance(
        receipt, dict(params, drawing_id=DRAWING), TENANT, job_id, tool, source_version, backend=backend)


def test_ground_equipment_rail_end_to_end(graph, service, monkeypatch, tmp_path):
    g = ground_base(graph)
    g["project"]["zip_code"] = ZIP
    backend = stored(tmp_path, monkeypatch, g)
    ph.publish_physical_state(backend, TENANT, DRAWING, small_doc())
    steps = [("solar-trackers-to-panel-groups", lambda head: {"expected_rev": 0}),
             ("solar-size-strings", lambda head: size_params(head)),
             ("solar-string-add", lambda head: {"operation": "add-string", "expected_rev": head["rev"],
                                                "ordered_panel_refs": slots(head)[0:3]}),
             ("solar-string-add", lambda head: {"operation": "add-string", "expected_rev": head["rev"],
                                                "ordered_panel_refs": slots(head)[3:5]}),
             (EQUIPMENT, lambda head: equipment_params(head))]
    for version, (tool, make) in enumerate(steps, 1):
        if tool == EQUIPMENT:
            assert availability.w1_input_readiness(TENANT, DRAWING)[EQUIPMENT] == {
                "input_ready": True, "input_reason": None}
        params = make(head_graph(backend))
        job = f"ground-equipment-{version}"
        receipt = dispatch(backend, tool, params, version, job)
        assert receipt["new_version"] == {"drawing_id": DRAWING, "version": version + 1, "parent": version}
        proof = prove(backend, receipt, tool, params, version, job)
        assert (proof["execution_mode"], proof["new_version"]) == ("local_graph_commit", version + 1)
    out = head_graph(backend)
    assert latest(backend) == 6 and out["rev"] == 5
    assert [s["inverter_ref"] for s in out["strings"]] == [INV, INV]
    assert solar_equipment.equipment_ready(out) is True
    assert out["extra"]["solve_coverage"] == {"duplicate_panel_refs": [], "unassigned_panel_refs": []}
    readiness = availability.w1_input_readiness(TENANT, DRAWING)
    assert readiness[EQUIPMENT] == {"input_ready": True, "input_reason": None}
    assert readiness["solar-homeruns"] == {"input_ready": False, "input_reason": "routing_topology_required"}


@pytest.fixture
def api_strung(isolated_jobs, no_network, graph, service, pinned, tmp_path, monkeypatch):
    backend = stored(tmp_path, monkeypatch, strung(sized(converted(graph)), pinned))
    yield from _api(backend, tmp_path, monkeypatch)


@pytest.fixture
def api_partly_strung(isolated_jobs, no_network, graph, service, pinned, tmp_path, monkeypatch):
    backend = stored(tmp_path, monkeypatch, strung(sized(converted(graph)), pinned, runs=((0, 3),)))
    yield from _api(backend, tmp_path, monkeypatch)


def post(api, params):
    api[2][EQUIPMENT] = solar_tools.trusted_record(EQUIPMENT)
    return api[0].post("/api/run?wait=1", json=body(api, EQUIPMENT, params))


def jobs_now():
    return [jobs.get_job(row["job_id"]) for row in jobs._query("SELECT job_id FROM jobs")]


def test_ground_equipment_run_route_commits_one_job(api_strung):
    before = head_graph(api_strung[1])
    response = post(api_strung, equipment_params(before))
    assert response.status_code == 200, response.text
    env = response.json()
    assert env["ok"] is True and env["execution_provenance"]["execution_mode"] == "local_graph_commit"
    assert env["result"]["new_version"] == {"drawing_id": DRAWING, "version": 2, "parent": 1}
    records = jobs_now()
    assert len(records) == 1 and records[0]["status"] == "complete"
    out = head_graph(api_strung[1])
    assert [s["inverter_ref"] for s in out["strings"]] == [INV, INV]
    assert solar_equipment.equipment_ready(out) is True


def test_ground_equipment_run_route_refuses_unstrung_slots(api_partly_strung):
    before = head_graph(api_partly_strung[1])
    response = post(api_partly_strung, equipment_params(before))
    assert response.status_code == 409, response.text
    env = response.json()
    assert env.get("ok") is not True and env["reason_code"] == "valid_strings_required"
    assert store.load_manifest(api_partly_strung[1], TENANT, DRAWING)["head"] == 1 and jobs_now() == []
