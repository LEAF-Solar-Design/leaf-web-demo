"""Stored-outline feeder routing through the real fenced publication and replay rail."""
import copy
import json
import re
import sys
from pathlib import Path

import pytest
from jsonschema import Draft7Validator

SERVER = Path(__file__).resolve().parents[1]
ROOT = SERVER.parent
sys.path.insert(0, str(SERVER))
sys.path.insert(0, str(SERVER / "tests"))

import solar_feeder_graph as fg
import solar_local_graph as local
import solar_tools
import store
import write_loop
from solar_design_graph import GraphValidationError, validate_graph
from solar_sizing_client import digest
from solar_solve_results import finish_mutation
from test_w1_design_graph import app_id, graph  # noqa: F401
from test_w1_local_graph_adapter import held
from test_solar_tool_combiners import i4, place
from test_solar_tool_equipment_move import move
from test_solar_w2_registration import head_graph, latest
import test_solar_ground_equipment as ground
from test_solar_ground_equipment import service, pinned  # noqa: F401

TOOL = "solar-feeders"
TENANT = "fixture-tenant"
INVALID = "INVALID_FEEDER_REQUEST"
EXPECTED_SHA = "3b65cdf69075dc2eb1beda469c785e930165ae7e8162511fcfff079be49aaa7b"


def builtin():
    return local._load_builtin(TOOL)


@pytest.fixture(scope="module")
def rooftop():
    g, intake, groups = i4()
    return place(g, intake, groups), groups


@pytest.fixture(autouse=True)
def memory_transport(monkeypatch):
    """Replace file transport only; store fences, publication and proof remain real."""
    monkeypatch.setenv("LEAF_DRAWING_STORE", "legacy")
    monkeypatch.setenv("LEAF_DRAWING_MUTATIONS_ENABLED", "1")
    monkeypatch.setenv("LEAF_UPLOAD_IMPORT_MUTATIONS_ENABLED", "1")
    monkeypatch.delenv("LEAF_DRAWING_MUTATIONS_FENCE_FILE", raising=False)
    read = store._read
    monkeypatch.setattr(store, "_read", lambda path: path if type(path) is bytes else read(path))

    def put_bytes(backend, tenant_id, drawing_id, data, parent_version, meta, **kwargs):
        return store.put_drawing(backend, tenant_id, drawing_id, bytes(data),
                                 parent_version=parent_version, meta=meta, **kwargs)

    monkeypatch.setattr(write_loop, "_put_bytes_version", put_bytes)


def stored(monkeypatch, g, carried):
    backend = store.InMemoryBackend()
    intake = {"dwg": {}, "layers": [], "polylines": [], "inserts": [], "faces3d": [],
              "blockdefs": [], "geodata": None, **copy.deepcopy(carried),
              "solar_design_graph": g, "solar_design_graph_sha256": digest(g)}
    store.ingest_drawing(backend, TENANT, json.dumps(intake).encode(), drawing_id="solar")
    monkeypatch.setattr(write_loop, "backend_for_tenant", lambda *a, **k: backend)
    return backend


def dispatch(backend, params, version=1, job="feeders-job"):
    with held(backend) as fence:
        return local.run_local_graph_commit(
            backend, TENANT, TOOL, dict(params, drawing_id="solar"), drawing_id="solar",
            source_version=version, holder="fixture-owner", fence=fence, job_id=job)


def refused(fn, code, path=None):
    with pytest.raises(GraphValidationError) as error:
        fn()
    assert error.value.code == code
    if path is not None:
        assert error.value.path == path


def feeder_rows(g):
    return {r["id"]: r for r in g["routes"] if r["route_kind"] == "feeder"}


def expected(g, groups):
    result, _ = fg.route_feeders(g, groups)
    old, new = feeder_rows(g), feeder_rows(result)
    changed = set(old) ^ set(new)
    changed.update(ref for ref in set(old) & set(new)
                   if any(old[ref][k] != new[ref][k] for k in
                          ("points", "length_ft", "from_ref", "to_ref")))
    for schedule in result["schedules"]:
        if set(schedule["source_refs"]) & changed:
            schedule["validity"] = {"state": "stale", "reasons": ["ROUTES_CHANGED"]}
    return finish_mutation(g, result, TOOL)


def test_solar_tool_feeders_publishes_rooftop(rooftop, monkeypatch):
    g, groups = rooftop
    snapshot = copy.deepcopy((g, groups))
    backend = stored(monkeypatch, g, {"panel_groups": groups})
    receipt = dispatch(backend, {"expected_rev": 1})
    assert receipt["new_version"] == {"drawing_id": "solar", "version": 2, "parent": 1}
    assert (receipt["before_rev"], receipt["after_rev"], receipt["drawing_changed"]) == (1, 2, True)
    out = head_graph(backend)
    assert out == expected(g, groups)
    assert digest(out) == EXPECTED_SHA and len(feeder_rows(out)) == 14
    assert (g, groups) == snapshot


def test_solar_tool_feeders_ground_equipment_refuses(graph, service, pinned, monkeypatch):
    g = ground.ground_base(graph)
    g["project"]["zip_code"] = ground.ZIP
    backend = stored(monkeypatch, g, {})
    ground.ph.publish_physical_state(backend, TENANT, "solar", ground.small_doc())
    # Pin the rail's string builtin to the same deterministic fixture module.
    real_load = local._load_builtin
    monkeypatch.setattr(local, "_load_builtin",
                        lambda tool: pinned if tool == "solar-string-add" else real_load(tool))
    steps = [("solar-trackers-to-panel-groups", lambda h: {"expected_rev": 0}),
             ("solar-size-strings", ground.size_params),
             ("solar-string-add", lambda h: {"operation": "add-string", "expected_rev": h["rev"],
                                            "ordered_panel_refs": ground.slots(h)[:3]}),
             ("solar-string-add", lambda h: {"operation": "add-string", "expected_rev": h["rev"],
                                            "ordered_panel_refs": ground.slots(h)[3:5]}),
             (ground.EQUIPMENT, ground.equipment_params)]
    for version, (tool, make) in enumerate(steps, 1):
        params = make(head_graph(backend))
        job = f"ground-feeders-{version}"
        receipt = ground.dispatch(backend, tool, params, version, job)
        assert receipt["new_version"] == {"drawing_id": "solar", "version": version + 1, "parent": version}
        assert ground.prove(backend, receipt, tool, params, version, job)["execution_mode"] == "local_graph_commit"
        _, key = store.resolve_version(backend, TENANT, "solar", version + 1)
        assert "panel_groups" not in json.loads(backend.get(key))
    out = head_graph(backend)
    assert out["rev"] == 5 and latest(backend) == 6
    assert builtin().input_readiness(out) == {"input_ready": False, "input_reason": "valid_settings_required"}
    refused(lambda: dispatch(backend, {"expected_rev": 5}, 6), "VALID_SETTINGS_REQUIRED")
    assert head_graph(backend) == out and latest(backend) == 6


def test_solar_tool_feeders_stale_revision(rooftop, monkeypatch):
    g, groups = rooftop
    backend = stored(monkeypatch, g, {"panel_groups": groups})
    refused(lambda: dispatch(backend, {"expected_rev": 0}), "STALE_GRAPH_REVISION", "<root>")
    assert latest(backend) == 1 and head_graph(backend) == g


@pytest.mark.parametrize("carried,path", [(None, "source_intake"), ({}, "panel_groups"),
                                         ({"panel_groups": {}}, "panel_groups")])
def test_solar_tool_feeders_drawing_context(rooftop, monkeypatch, carried, path):
    g, _ = rooftop
    assert builtin().input_readiness(g)["input_ready"] is True
    refused(lambda: builtin().run(g, {"expected_rev": 1}, source_intake=carried),
            "INVALID_DRAWING_CONTEXT", path)
    if carried is not None:
        backend = stored(monkeypatch, g, carried)
        refused(lambda: dispatch(backend, {"expected_rev": 1}), "INVALID_DRAWING_CONTEXT", path)
        assert latest(backend) == 1 and head_graph(backend) == g


@pytest.mark.parametrize("group,append", [({"outlines": False}, True),
                                        ({"outlines": False}, False),
                                        ("not-a-group", True), ({"handle": "x"}, True)])
def test_solar_tool_feeders_group_shape(rooftop, monkeypatch, group, append):
    g, groups = rooftop
    carried = groups + [group] if append else [group]
    backend = stored(monkeypatch, g, {"panel_groups": carried})

    def fail(*args):
        raise AssertionError("feeder kernel called for malformed panel group")

    monkeypatch.setattr(fg, "route_feeders", fail)
    index = len(groups) if append else 0
    refused(lambda: dispatch(backend, {"expected_rev": 1}),
            "INVALID_DRAWING_CONTEXT", f"panel_groups[{index}]")
    assert latest(backend) == 1 and head_graph(backend) == g


@pytest.mark.parametrize("params", [{"expected_rev": 1, "extra": 0}, {"expected_rev": True},
                                  {"expected_rev": 0.0}])
def test_solar_tool_feeders_request_shape(rooftop, monkeypatch, params):
    g, groups = rooftop
    backend = stored(monkeypatch, g, {"panel_groups": groups})
    refused(lambda: dispatch(backend, params), INVALID, "<root>")
    assert latest(backend) == 1 and head_graph(backend) == g
    validator = Draft7Validator(solar_tools.trusted_record(TOOL)["params"])
    assert validator.is_valid(params) is (type(params["expected_rev"]) is float)


@pytest.mark.parametrize("code,mapped", [
    ("FEEDER_L2_MODE_REQUIRED", "VALID_SETTINGS_REQUIRED"),
    ("FEEDER_COMBINERS_REQUIRED", "EQUIPMENT_ASSIGNMENT_REQUIRED"),
    ("FEEDER_COLLECTORS_REQUIRED", "STRING_COLLECTORS_REQUIRED"),
    ("FEEDER_CAPACITY_AMBIGUOUS", "VALID_SETTINGS_REQUIRED"),
    ("FEEDER_OUTLINES_INVALID", "INVALID_DRAWING_CONTEXT"),
    ("FEEDER_NOT_PORTED", "CAPABILITY_NOT_READY"),
    ("FEEDER_KERNEL_REFUSED", "CAPABILITY_NOT_READY"),
    ("FEEDER_POSTCONDITION_FAILED", "CAPABILITY_NOT_READY"),
])
def test_solar_tool_feeders_code_map(rooftop, monkeypatch, code, mapped):
    assert set(builtin().CODE_MAP) == set(fg.CODES)
    monkeypatch.setattr(fg, "route_feeders", lambda *a: fg._fail(code))
    g, groups = rooftop
    refused(lambda: builtin().run(g, {"expected_rev": 1}, source_intake={"panel_groups": groups}),
            mapped, code)


@pytest.mark.parametrize("case,reason", [("mode", "valid_settings_required"),
                                       ("l1", "equipment_assignment_required"),
                                       ("l2", "string_collectors_required"),
                                       ("capacity", "valid_settings_required")])
def test_solar_tool_feeders_readiness_matches_refusal(rooftop, case, reason):
    g, groups = copy.deepcopy(rooftop)
    if case == "mode":
        g = graph.__wrapped__()
    elif case == "l1":
        g = i4()[0]
    elif case == "l2":
        g["inverters"] = [i for i in g["inverters"] if not i["is_l2"]]
        for i in g["inverters"]:
            i["l2_ref"] = None
        g["routes"] = [r for r in g["routes"] if r["route_kind"] != "feeder"]
    else:
        next(i for i in g["inverters"] if i["is_l2"])["collector_capacity"] += 1
    g = validate_graph(g)
    assert builtin().input_readiness(g) == {"input_ready": False, "input_reason": reason}
    refused(lambda: builtin().run(g, {"expected_rev": g["rev"]}, source_intake={"panel_groups": groups}),
            reason.upper(), "<root>")


@pytest.mark.parametrize("case", ["moved", "length", "unchanged"])
def test_solar_tool_feeders_schedule_dependencies(rooftop, graph, case):
    g, groups = rooftop
    baseline, _ = fg.route_feeders(g, groups)
    l1 = next(i for i in baseline["inverters"] if not i["is_l2"])
    target = next(r for r in feeder_rows(baseline).values() if r["from_ref"] == l1["id"])
    other = next(r for r in feeder_rows(baseline).values() if r["id"] != target["id"])
    if case == "moved":
        mpu = baseline["project"]["units"]["meters_per_unit"]
        baseline = move(baseline, l1["id"], [l1["position"][0] / mpu + .001,
                                           l1["position"][1] / mpu - .001])["graph"]
    elif case == "length":
        feeder_rows(baseline)[target["id"]]["length_ft"] += 1
        assert fg.route_feeders(baseline, groups)[1]["redrawn"] == []
    baseline["schedules"] = []
    for n, route in enumerate((target, other), 1):
        schedule = copy.deepcopy(graph["schedules"][0])
        schedule.update(id=app_id("schedule", n), source_refs=[route["id"]])
        baseline["schedules"].append(schedule)
    baseline = validate_graph(baseline)
    snapshot = copy.deepcopy((baseline, groups))
    out = builtin().run(baseline, {"expected_rev": baseline["rev"]}, source_intake={"panel_groups": groups})
    valid = {"state": "valid", "reasons": []}
    assert out["schedules"][0]["validity"] == (valid if case == "unchanged" else
                                               {"state": "stale", "reasons": ["ROUTES_CHANGED"]})
    assert out["schedules"][1]["validity"] == valid
    assert out == expected(baseline, groups) and (baseline, groups) == snapshot


def test_solar_tool_feeders_identical_rerun(rooftop, monkeypatch):
    g, groups = rooftop
    backend = stored(monkeypatch, g, {"panel_groups": groups})
    dispatch(backend, {"expected_rev": 1})
    before = head_graph(backend)
    receipt = dispatch(backend, {"expected_rev": 2}, 2, "feeders-again")
    after = head_graph(backend)
    assert receipt["new_version"] == {"drawing_id": "solar", "version": 3, "parent": 2}
    assert (receipt["before_rev"], receipt["after_rev"], receipt["drawing_changed"]) == (2, 3, True)
    for key in ("project", "settings", "frames", "panels", "strings", "inverters", "routes", "schedules",
                "electrical_zones"):
        assert before[key] == after[key]
    assert digest(before) != digest(after)


def test_solar_tool_feeders_receipt_replay(rooftop, monkeypatch):
    g, groups = rooftop
    backend = stored(monkeypatch, g, {"panel_groups": groups})
    receipt = dispatch(backend, {"expected_rev": 1})
    proof = local.graph_commit_provenance(receipt, {"expected_rev": 1, "drawing_id": "solar"},
                                          TENANT, "feeders-job", TOOL, 1, backend=backend)
    assert proof["execution_mode"] == "local_graph_commit"
    assert proof["graph_sha256"] == digest(head_graph(backend))
    assert digest(builtin().run(g, {"expected_rev": 1}, source_intake={"panel_groups": groups})) == proof["graph_sha256"]


def test_solar_tool_feeders_other_version_intake(rooftop, monkeypatch):
    g, groups = rooftop
    backend = stored(monkeypatch, g, {"panel_groups": groups})
    dispatch(backend, {"expected_rev": 1})
    before = head_graph(backend)
    resolver = local._TRUSTED_RESOLVERS["source_intake"]
    monkeypatch.setitem(local._TRUSTED_RESOLVERS, "source_intake",
                        lambda b, t, d, v, sha: resolver(b, t, d, 1, sha))
    refused(lambda: dispatch(backend, {"expected_rev": 2}, 2, "wrong-intake"), "SOURCE_INTAKE_UNAVAILABLE")
    assert latest(backend) == 2 and head_graph(backend) == before


def test_solar_tool_feeders_bridge_refusal(rooftop, monkeypatch):
    def fail(*args):
        raise fg.rb.ElectricalBridgeError("BRIDGE_INVALID_REQUEST")
    monkeypatch.setattr(fg, "route_feeders", fail)
    g, groups = rooftop
    refused(lambda: builtin().run(g, {"expected_rev": 1}, source_intake={"panel_groups": groups}),
            "CAPABILITY_NOT_READY", "BRIDGE_INVALID_REQUEST")


def test_solar_tool_feeders_refusal_copy_scan():
    declaration = solar_tools.get(TOOL)
    assert declaration["readiness"] == {"kind": "hook"}
    assert declaration["trusted_inputs"] == ["source_intake"]
    assert (declaration["family"], declaration["wave"], declaration["order"], declaration["scenario"]) == (
        "routing", 3, 60, "w3-ground-routing")
    assert declaration["invalid_request_code"] == INVALID and declaration["maturity"] == "preview"
    path = ROOT / "server" / declaration["builtin"]
    assert path.is_file() and path.resolve().is_relative_to((SERVER / "builtins").resolve())
    text = path.read_text(encoding="utf-8")
    assert ".lower()" not in text
    reasons = set(re.findall(r'"input_reason":\s*"([a-z][a-z0-9_]{0,63})"', text))
    assert reasons == {"valid_settings_required", "equipment_assignment_required", "string_collectors_required", "invalid_drawing_context"}
    rail = (ROOT / "web/src/lib/ribbonClusters.js").read_text(encoding="utf-8")
    table = rail[rail.index("export const SOLAR_REFUSAL_REASONS"):]
    keys = set(re.findall(r"^\s+([a-z][a-z0-9_]*):", table[:table.index("})")], re.M))
    literals = set(re.findall(r'_refuse\(\s*"([A-Z][A-Z0-9_]*)"', text))
    assert reasons <= keys
    assert {code.lower() for code in set(builtin().CODE_MAP.values()) | literals} <= keys
    assert '"input_ready": True, "input_reason": None' in text
