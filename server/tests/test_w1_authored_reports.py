"""Authored W1 reports read the tenant's own pinned design graph through the sandbox seam.

Three tenant-authored catalog tools (zone schedule, unassigned-panel selector,
string-length exception report) are registered through POST /api/author. Only the
generator is stubbed: the templater returns each fixture source under
tests/fixtures/w1_authored/ in place of the model, and the router's own lane
validates the record, persists the body under a temporary authored store and
registers it. The broker then hands each tool the graph intake built by
server/solar_authored_graph.py through its ordinary run_tool_dynamic call. The
staging-posture product walk runs them out of process in the subprocess sandbox
tier, pinned to a version, with the in-process loader forbidden.
"""
import copy
import hashlib
import json
import re
import sys
import time
from pathlib import Path

import pytest
import requests

SERVER = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SERVER))
sys.path.insert(0, str(SERVER / "tests"))

import broker  # noqa: E402
import catalog  # noqa: E402
import deps  # noqa: E402
import jobs  # noqa: E402
import solar_authored_graph as authored_graph  # noqa: E402
import solar_local_read  # noqa: E402
import solar_tools  # noqa: E402
import tool_loader  # noqa: E402
import tool_record_fields  # noqa: E402
import write_loop  # noqa: E402
from leaf_cloud_client import canonical_bytes  # noqa: E402
from routers import author as author_router  # noqa: E402
from solar_design_graph import GraphValidationError, validate_graph  # noqa: E402
from solar_graph_context import resolve_graph_context  # noqa: E402
from tool_record_fields import ToolRecordFieldError  # noqa: E402
from test_w1_design_graph import app_id, entity  # noqa: E402
from test_w1_solve_commit import seed as seed_graph  # noqa: E402
from test_w1_local_graph_adapter import held, run as commit_settings  # noqa: E402
from test_w1_seed_product_path import (  # noqa: E402,F401
    TENANT as PRODUCT_TENANT, change, checkout, isolated_jobs, no_network, product,
    run as run_product, seed as seed_upload, upload,
)

TENANT = "fixture-tenant"
DRAWING = "solar"
GRAPH_INPUT = "solar-w1-graph"
FIXTURES = SERVER / "tests" / "fixtures" / "w1_authored"
REPORTS = {
    "zone_schedule": {
        "name": "w1-zone-schedule",
        "description": "Zone schedule: panel and string counts per electrical zone",
        "params": {"type": "object", "properties": {}, "additionalProperties": False},
    },
    "unassigned_panels": {
        "name": "w1-unassigned-panels",
        "description": "Select the grouped panels that no string carries",
        "params": {"type": "object", "properties": {}, "additionalProperties": False},
    },
    "string_length_exceptions": {
        "name": "w1-string-length-exceptions",
        "description": "String-length exception report against the settings bounds",
        "params": {"type": "object", "properties": {
            "min_length": {"type": "integer", "minimum": 0},
            "max_length": {"type": "integer", "minimum": 0},
        }, "additionalProperties": False},
    },
}


# --------------------------------------------------------------------------- #
# harness seams
# --------------------------------------------------------------------------- #
def fixture_source(key):
    return (FIXTURES / f"{key}.py").read_text(encoding="utf-8")


def generator(description):
    """Stands in for the model only: returns (tool, code, preview) like the templater."""
    key = next(key for key, row in REPORTS.items() if row["description"] == description)
    row = REPORTS[key]
    tool = {
        "name": row["name"], "version": "1.0.0", "description": description,
        "kind": "script", "engine_op": row["name"].replace("-", "_"),
        "params": copy.deepcopy(row["params"]), "returns": {"type": "object"},
        "capabilities": ["drawing.read"],
        "provenance": {"author": "agent", "created": "2026-09-28T00:00:00Z"},
        "default_params": {},
    }
    return tool, fixture_source(key), f"Tool '{row['name']}': an authored W1 report."


def local_posture(monkeypatch):
    for name in ("LEAF_RUNTIME_ENV", "LEAF_SANDBOX", "LEAF_TOOL_SANDBOX_PROVIDER",
                 "LEAF_AUTHORED_EXECUTION"):
        monkeypatch.delenv(name, raising=False)


def author_client(tmp_path, monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    for name in ("LEAF_AUTH_LIVE", "LEAF_AUTHOR_HARNESS_URL", "LEAF_DAILY_AUTHOR_QUOTA",
                 "LEAF_ENTITLEMENTS_FILE"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(author_router, "AUTHORED_DIR", tmp_path / "authored")
    monkeypatch.setattr(tool_loader, "SERVER_DIR", tmp_path)
    monkeypatch.setattr(deps, "AUTHORED_STORE", tmp_path / "authored_tools.json")
    monkeypatch.setattr(deps, "_AUTHORED", [])
    monkeypatch.setattr(deps, "load_tenant_repo_tools", lambda *a, **k: [])
    monkeypatch.setattr(author_router.fb, "author_tool", generator)
    app = FastAPI()
    app.include_router(author_router.router)
    app.dependency_overrides[deps.require_tenant] = lambda: TENANT
    return TestClient(app)


def register(client, key, **overrides):
    body = {"description": REPORTS[key]["description"], "mode": "build",
            "graph_input": GRAPH_INPUT}
    body.update(overrides)
    return client.post("/api/author", json=body)


def catalog_record(name):
    return next(tool for tool in deps.all_tools(TENANT) if tool.get("name") == name)


def registered(tmp_path, monkeypatch):
    client = author_client(tmp_path, monkeypatch)
    records = {}
    for key, row in REPORTS.items():
        response = register(client, key)
        assert response.status_code == 200, response.text
        records[key] = catalog_record(row["name"])
    return records


def forbidden(*args, **kwargs):
    raise AssertionError("must not run")


def execute(monkeypatch, backend, tool, *, tenant=TENANT, version=1, job_id="report-job",
            dwg=DRAWING, params=None):
    monkeypatch.setattr(write_loop, "backend_for_tenant", lambda *a, **k: backend)
    monkeypatch.setattr(broker, "tenant_disabled", lambda _tenant: False)
    monkeypatch.setattr(broker, "_cap_preflight", lambda *a: None)
    request = broker.BrokerRunRequest(
        tenant_id=tenant, tool=tool, params={} if params is None else params, dwg=dwg,
        aps_live=False, dwg_version=version, job_id=job_id)
    return broker._execute(request, tool, str(tool.get("engine_op") or ""),
                           time.perf_counter(), {})


def result_of(monkeypatch, backend, tool, **kwargs):
    env, status = execute(monkeypatch, backend, tool, **kwargs)
    assert status == 200, env
    assert env["ok"] is True, env
    return env["result"]


def output_sha256(result):
    return hashlib.sha256(canonical_bytes(result)).hexdigest()


# --------------------------------------------------------------------------- #
# the synthetic fixture 'w1-authored-mini'
# --------------------------------------------------------------------------- #
STRINGS = {1: [1, 2, 3, 4], 2: [7, 8, 9, 10]}
ZONES = ((1, "A", range(1, 7)), (2, "B", range(7, 11)))


def mini_graph(*, minimum=5, maximum=12):
    """P1..P10 in one panel group; zones A = P1..P6, B = P7..P10; S1 = P1..P4,
    S2 = P7..P10; P5 and P6 in no string. Bounds ride settings.extra."""
    cold = {"passes": True, "override_accepted": False, "suggested_string_length": 4,
            "per_module": 50.0, "string_voltage": 200.0, "max_dc_voltage": 600.0}
    units = {
        "drawing_units": "in", "meters_per_unit": 0.0254, "source": "drawing_marker",
        "compute_units": "m", "wcs_to_ucs": [1, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1],
        "elevation_datum": "local roof", "crs": None,
        "drawing_unit_is_feet": False, "warnings": [],
    }
    member = {n: (sid, seq) for sid, numbers in STRINGS.items() for seq, n in enumerate(numbers)}
    frame_id = app_id("frame", 1)
    panels = []
    for n in range(1, 11):
        sid, seq = member.get(n, (None, None))
        panel = entity("panel", n, frame_ref=frame_id, matrix_cell={"row": 0, "col": n - 1},
                       centre=[n, 0], angle=0,
                       assignment={"string_ref": app_id("string", sid) if sid else None, "seq": seq})
        panel["provenance"]["source_handle"] = f"P{n}"
        panels.append(panel)
    strings = [entity(
        "string", sid, circuit_tag=f"S{sid}", circuit_kind="String",
        ordered_panel_refs=[app_id("panel", n) for n in numbers], module_count=len(numbers),
        from_ref=app_id("panel", numbers[0]), to_ref=None, tag_text_ref=None,
        wire_gauge="10 AWG", length_ft=10, route=[[0, 0], [1, 0]], inverter_ref=None,
    ) for sid, numbers in STRINGS.items()]
    frame = entity(
        "frame", 1, name="Roof group", insertion_point=[0, 0, 0], installation_design="Roof",
        panel_refs=[p["id"] for p in panels], module_rows=1, module_columns=10,
        module_slots=10, module_power_watts=400, module_width_along_row=1,
        module_height_across_row=2, electrical_zone_ref=None,
        matrix=[[{"code": "panel", "panel_ref": p["id"], "seq": p["assignment"]["seq"],
                  "inverter_id": None, "string_input_number": None,
                  "x": p["centre"][0], "y": 0, "angle": 0} for p in panels]],
        sequences=[{"string_ref": s["id"], "ordered_panel_refs": list(s["ordered_panel_refs"])}
                   for s in strings],
        panel_assignments=[{"panel_ref": p["id"], **p["assignment"], "inverter_id": None,
                            "string_input_number": None} for p in panels],
    )
    settings = entity(
        "settings", 1, panel_layer_contains="Panels", panel_group_layer="Groups",
        string_layer="Strings", home_run_layer="Homeruns", panels_in_sequence=4,
        num_mppt=1, strings_per_mppt=2, optimizer_ratio=1, use_l2_collectors=False,
        panel_group_number=2, string_number=3, inverter_number=2, mppt_letter="A",
        global_string_sizing_confirmed=True, voc_cold=copy.deepcopy(cold),
    )
    bounds = {"string_length_min": minimum, "string_length_max": maximum}
    settings["extra"] = {key: value for key, value in bounds.items() if value is not None}
    zones = [entity("zone-el", zid, name=name, color_index=zid,
                    panel_refs=[app_id("panel", n) for n in numbers],
                    module_model="fixture-module", inverter_model_a="fixture-inverter",
                    inverter_count_a=1, panels_in_sequence=4, dc_ac_ratio=1.2,
                    voc_cold=copy.deepcopy(cold), boundary_ref=None)
             for zid, name, numbers in ZONES]
    return validate_graph({
        "graph_schema_version": 1, "rev": 0, "parent_rev": None,
        "source_hash": "a" * 64, "catalog_versions": {"modules": "fixture-v1"},
        "project": entity("project", 1, name="w1-authored-mini", zip_code="00000",
                          latitude=None, longitude=None, installation_design="Roof",
                          units=units, graph_schema_version=1, site_revision="fixture-site-1"),
        "settings": settings, "electrical_zones": zones, "frames": [frame],
        "panels": panels, "strings": strings, "inverters": [], "routes": [],
        "schedules": [], "opaque_stores": {}, "orphaned_xdata": [], "extra": {},
    })


@pytest.fixture
def mini(tmp_path, monkeypatch):
    local_posture(monkeypatch)
    backend, _ = seed_graph(tmp_path, monkeypatch, mini_graph())
    return backend, registered(tmp_path, monkeypatch)


def trusted_reports(backend, tenant, drawing_id, version):
    """What the trusted read builtin and the pinned stored graph say each report is."""
    context = resolve_graph_context(backend, tenant, drawing_id, version)
    graph = context["graph"]
    zones = []
    for zone in graph["electrical_zones"]:
        read = solar_local_read.run_local_graph_read(
            backend, tenant, "solar-select-by-zone", {"zone_name": zone["name"]},
            drawing_id=drawing_id, source_version=version, job_id="trusted-zone-read")
        assert read["graph_sha256"] == context["graph_sha256"]
        selected = read["output"]["panel_refs"]
        zones.append({"zone": zone["name"], "panels": len(selected),
                      "strings": sum(1 for s in graph["strings"]
                                     if set(selected) & set(s["ordered_panel_refs"]))})
    grouped = [ref for frame in graph["frames"] for ref in frame["panel_refs"]]
    strung = {ref for s in graph["strings"] for ref in s["ordered_panel_refs"]}
    unassigned = [ref for ref in grouped if ref not in strung]
    settings = graph["settings"]
    minimum = settings["extra"].get("string_length_min")
    maximum = settings["extra"].get("string_length_max")
    if maximum is None and settings["panels_in_sequence"] >= 1:
        maximum = settings["panels_in_sequence"]
    exceptions = []
    for s in graph["strings"]:
        count = len(s["ordered_panel_refs"])
        if maximum is not None and count > maximum:
            exceptions.append({"string": s["circuit_tag"], "panels": count,
                               "rule": "above_max", "max": maximum})
        elif minimum is not None and count < minimum:
            exceptions.append({"string": s["circuit_tag"], "panels": count,
                               "rule": "below_min", "min": minimum})
    return context, {"zone_schedule": zones, "unassigned_panels": unassigned,
                     "string_length_exceptions": exceptions}


def sorted_json(rows):
    return sorted(json.dumps(row, sort_keys=True) for row in rows)


def assert_matches_trusted(results, backend, tenant, drawing_id, version):
    context, expected = trusted_reports(backend, tenant, drawing_id, version)
    for result in results.values():
        assert result["graph_sha256"] == context["graph_sha256"]
        assert result["source_version"] == version and result["drawing_id"] == drawing_id
    assert sorted_json(results["zone_schedule"]["zones"]) == sorted_json(expected["zone_schedule"])
    unassigned = results["unassigned_panels"]
    assert sorted(unassigned["panel_refs"]) == sorted(expected["unassigned_panels"])
    assert unassigned["count"] == len(expected["unassigned_panels"])
    assert (sorted_json(results["string_length_exceptions"]["exceptions"])
            == sorted_json(expected["string_length_exceptions"]))


# --------------------------------------------------------------------------- #
# 1. record field
# --------------------------------------------------------------------------- #
def test_graph_input_accepts_exactly_the_literal():
    assert len(GRAPH_INPUT) == 14 and re.fullmatch(r"[a-z0-9-]+", GRAPH_INPUT)
    assert tool_record_fields.GRAPH_INPUT_SOLAR_W1 == GRAPH_INPUT
    fields = {"graph_input": GRAPH_INPUT}
    assert tool_record_fields.validate_optional_fields(fields) == fields
    assert tool_record_fields.sanitize_optional_fields({"name": "t", **fields}) == fields
    assert tool_record_fields.validate_optional_fields({"graph_input": None}) == {}


@pytest.mark.parametrize("value", [
    "", "Solar-W1-Graph", " solar-w1-graph", "solar-w1-graph ", "a" * 64, 7, True,
    [GRAPH_INPUT],
])
def test_graph_input_refuses_every_other_value(value):
    with pytest.raises(ToolRecordFieldError) as excinfo:
        tool_record_fields.validate_optional_fields({"graph_input": value})
    assert excinfo.value.field == "graph_input"
    assert tool_record_fields.sanitize_optional_fields({"name": "t", "graph_input": value}) == {}


def test_author_request_types_graph_input():
    assert author_router.AuthorRequest(description="x").graph_input is None
    request = author_router.AuthorRequest(description="x", graph_input=GRAPH_INPUT)
    assert author_router._validated_record_fields(request) == {"graph_input": GRAPH_INPUT}
    for value in ("", "Solar-W1-Graph", " solar-w1-graph", "a" * 64):
        with pytest.raises(Exception):
            author_router.AuthorRequest(description="x", graph_input=value)


def test_reads_graph_only_for_an_untrusted_declared_record():
    declared = {"name": "tenant-report", "graph_input": GRAPH_INPUT,
                "capabilities": ["drawing.read"]}
    assert authored_graph.reads_graph(declared) is True
    assert authored_graph.reads_graph({**declared, "graph_input": "Solar-W1-Graph"}) is False
    assert authored_graph.reads_graph({k: v for k, v in declared.items() if k != "graph_input"}) is False
    assert authored_graph.reads_graph({**declared, "solar": {"adapter": "local-graph-read"}}) is False
    assert authored_graph.reads_graph({**declared, "capabilities": ["drawing.write"]}) is False
    trusted = solar_tools.trusted_record("solar-select-by-zone")
    assert authored_graph.reads_graph({**trusted, "graph_input": GRAPH_INPUT}) is False
    assert authored_graph.reads_graph("tenant-report") is False


# --------------------------------------------------------------------------- #
# 6. lane and persistence
# --------------------------------------------------------------------------- #
def test_reports_register_through_the_author_lane_and_survive_a_reload(tmp_path, monkeypatch):
    local_posture(monkeypatch)
    client = author_client(tmp_path, monkeypatch)
    for key, row in REPORTS.items():
        response = register(client, key)
        assert response.status_code == 200, response.text
        tool = response.json()["tool"]
        assert tool["name"] == row["name"] and tool["version"] == "1.0.0"
        assert tool["graph_input"] == GRAPH_INPUT and tool["tenant_id"] == TENANT
        assert tool["entry"].startswith("authored/")
        assert (tmp_path / tool["entry"]).read_text(encoding="utf-8") == fixture_source(key)
    listed = {row["name"]: catalog_record(row["name"]) for row in REPORTS.values()}
    digests = {name: deps.catalog_tool_digest(record) for name, record in listed.items()}
    for record in listed.values():
        assert record["version"] == "1.0.0"
        assert authored_graph.reads_graph(record) is True
        assert tool_loader.is_trusted_builtin_tool(record, TENANT) is False
        assert catalog._capability_entry(record)["graph_input"] == GRAPH_INPUT
    stored = json.loads((tmp_path / "authored_tools.json").read_text(encoding="utf-8"))
    assert {tool["name"] for tool in stored["tools"]} == set(listed)
    # A fresh process folds the persisted store again: same records, same digests.
    monkeypatch.setattr(deps, "_AUTHORED", deps.load_authored_tools())
    for name, record in listed.items():
        reloaded = catalog_record(name)
        assert reloaded == record
        assert deps.catalog_tool_digest(reloaded) == digests[name]


def test_a_malformed_graph_input_is_refused_before_authoring(tmp_path, monkeypatch):
    local_posture(monkeypatch)
    client = author_client(tmp_path, monkeypatch)
    response = register(client, "zone_schedule", graph_input="Solar-W1-Graph")
    assert response.status_code == 422, response.text
    assert not (tmp_path / "authored_tools.json").exists()
    assert deps._AUTHORED == []


# --------------------------------------------------------------------------- #
# 3. broker seam
# --------------------------------------------------------------------------- #
def test_the_sandbox_seam_receives_the_pinned_graph_intake(mini, monkeypatch):
    backend, records = mini
    real = broker.run_tool_dynamic
    seen = []

    def capture(tool, intake, params, **kwargs):
        seen.append((copy.deepcopy(intake), copy.deepcopy(params)))
        return real(tool, intake, params, **kwargs)

    monkeypatch.setattr(broker, "run_tool_dynamic", capture)
    monkeypatch.setattr(solar_local_read, "_load_builtin", forbidden)
    result_of(monkeypatch, backend, records["zone_schedule"],
              params={"drawing_id": DRAWING})
    context = resolve_graph_context(backend, TENANT, DRAWING, 1)
    [(intake, params)] = seen
    assert params == {}
    assert set(intake) == {"schema", "drawing_id", "source_version", "graph_sha256", "graph"}
    assert intake["schema"] == "leaf.solar-graph-intake.v1"
    assert intake["drawing_id"] == DRAWING and intake["source_version"] == 1
    assert intake["graph"] == context["graph"]
    assert intake["graph_sha256"] == hashlib.sha256(canonical_bytes(context["graph"])).hexdigest()
    assert intake["graph_sha256"] == context["graph_sha256"]
    built = authored_graph.build_graph_intake(backend, TENANT, DRAWING, 1)
    built["graph"]["panels"].clear()
    assert authored_graph.build_graph_intake(backend, TENANT, DRAWING, 1) == intake


def test_intake_limit_equals_the_sandbox_input_limit():
    assert authored_graph.MAX_INTAKE_BYTES == 8388608
    assert authored_graph.MAX_INTAKE_BYTES == tool_loader._SANDBOX_LIMITS["input_bytes"]


@pytest.mark.parametrize("case,reason", [
    ("no_job", "JOB_IDENTITY_MISSING"), ("empty_job", "JOB_IDENTITY_MISSING"),
    ("no_version", "INVALID_SOURCE_VERSION"), ("zero_version", "INVALID_SOURCE_VERSION"),
    ("negative_version", "INVALID_SOURCE_VERSION"),
    ("drawing_conflict", "DRAWING_ID_CONFLICT"), ("empty_drawing", "DRAWING_ID_CONFLICT"),
])
def test_broker_refuses_before_the_sandbox(mini, monkeypatch, case, reason):
    backend, records = mini
    monkeypatch.setattr(broker, "run_tool_dynamic", forbidden)
    monkeypatch.setattr(authored_graph, "build_graph_intake", forbidden)
    kwargs = {
        "no_job": {"job_id": None}, "empty_job": {"job_id": ""},
        "no_version": {"version": None}, "zero_version": {"version": 0},
        "negative_version": {"version": -1},
        "drawing_conflict": {"params": {"drawing_id": "other"}},
        "empty_drawing": {"dwg": ""},
    }[case]
    env, status = execute(monkeypatch, backend, records["unassigned_panels"], **kwargs)
    assert status == 400, env
    assert env["ok"] is False and env["error"]["reason_code"] == reason


@pytest.mark.parametrize("case", ["missing_version", "missing_drawing", "other_tenant"])
def test_an_unreadable_graph_is_a_409(mini, monkeypatch, case):
    backend, records = mini
    monkeypatch.setattr(broker, "run_tool_dynamic", forbidden)
    kwargs = {"missing_version": {"version": 99},
              "missing_drawing": {"dwg": "nope"},
              "other_tenant": {"tenant": "other-tenant"}}[case]
    env, status = execute(monkeypatch, backend, records["zone_schedule"], **kwargs)
    assert status == 409, env
    assert env["error"]["reason_code"] == "GRAPH_CONTEXT_UNAVAILABLE"


def test_an_oversized_intake_is_refused_before_the_sandbox(mini, monkeypatch):
    backend, records = mini
    monkeypatch.setattr(broker, "run_tool_dynamic", forbidden)
    monkeypatch.setattr(authored_graph, "MAX_INTAKE_BYTES", 1024)
    env, status = execute(monkeypatch, backend, records["zone_schedule"])
    assert status == 400, env
    assert env["error"]["reason_code"] == "GRAPH_INTAKE_TOO_LARGE"


@pytest.mark.parametrize("execution,sandbox", [("0", "e2b"), ("1", None)])
def test_the_deployed_posture_gate_still_refuses(mini, monkeypatch, execution, sandbox):
    backend, records = mini
    monkeypatch.setenv("LEAF_RUNTIME_ENV", "staging")
    monkeypatch.setenv("LEAF_AUTHORED_EXECUTION", execution)
    if sandbox is not None:
        monkeypatch.setenv("LEAF_SANDBOX", sandbox)
    monkeypatch.setattr(broker, "run_tool_dynamic", forbidden)
    monkeypatch.setattr(authored_graph, "build_graph_intake", forbidden)
    for record in records.values():
        env, status = execute(monkeypatch, backend, record)
        assert status == 403, env
        assert env["error"]["error_code"] == "TENANT_DISABLED"


# --------------------------------------------------------------------------- #
# 4. the synthetic fixture
# --------------------------------------------------------------------------- #
def test_mini_fixture_reports(mini, monkeypatch):
    backend, records = mini
    zones = result_of(monkeypatch, backend, records["zone_schedule"])
    assert zones["zones"] == [{"zone": "A", "panels": 6, "strings": 1},
                              {"zone": "B", "panels": 4, "strings": 1}]
    unassigned = result_of(monkeypatch, backend, records["unassigned_panels"])
    assert unassigned["handles"] == ["P5", "P6"] and unassigned["count"] == 2
    assert unassigned["panel_refs"] == [app_id("panel", 5), app_id("panel", 6)]
    exceptions = result_of(monkeypatch, backend, records["string_length_exceptions"])
    assert exceptions["exceptions"] == [
        {"string": "S1", "panels": 4, "rule": "below_min", "min": 5},
        {"string": "S2", "panels": 4, "rule": "below_min", "min": 5},
    ]
    assert (exceptions["min"], exceptions["max"]) == (5, 12)


def test_string_length_rules_flip_to_above_max_with_max_three(tmp_path, monkeypatch):
    local_posture(monkeypatch)
    backend, _ = seed_graph(tmp_path, monkeypatch, mini_graph(maximum=3))
    records = registered(tmp_path, monkeypatch)
    expected = [{"string": "S1", "panels": 4, "rule": "above_max", "max": 3},
                {"string": "S2", "panels": 4, "rule": "above_max", "max": 3}]
    report = result_of(monkeypatch, backend, records["string_length_exceptions"])
    assert report["exceptions"] == expected
    # The same bound given as a parameter overrides the settings (max 12 there).
    (tmp_path / "params").mkdir()
    backend, _ = seed_graph(tmp_path / "params", monkeypatch, mini_graph())
    report = result_of(monkeypatch, backend, records["string_length_exceptions"],
                       params={"max_length": 3})
    assert report["exceptions"] == expected


def test_mini_reports_equal_the_trusted_derivation(mini, monkeypatch):
    backend, records = mini
    results = {key: result_of(monkeypatch, backend, record) for key, record in records.items()}
    assert_matches_trusted(results, backend, TENANT, DRAWING, 1)


def test_a_pinned_version_answers_byte_for_byte_after_an_edit(tmp_path, monkeypatch):
    local_posture(monkeypatch)
    # No settings maximum: the committed panels_in_sequence (4) bounds the strings,
    # so the ordinary settings edit below (to 3) changes the report at version 2.
    backend, _ = seed_graph(tmp_path, monkeypatch, mini_graph(maximum=None))
    records = registered(tmp_path, monkeypatch)
    first = {key: result_of(monkeypatch, backend, record, version=1)
             for key, record in records.items()}
    with held(backend) as fence:
        commit_settings(backend, fence=fence)
    assert resolve_graph_context(backend, TENANT, DRAWING, "head")["resolved_version"] == 2
    again = {key: result_of(monkeypatch, backend, record, version=1, job_id="report-again")
             for key, record in records.items()}
    for key in records:
        assert canonical_bytes(again[key]) == canonical_bytes(first[key])
        assert output_sha256(again[key]) == output_sha256(first[key])
    later = result_of(monkeypatch, backend, records["string_length_exceptions"], version=2)
    assert later["graph_sha256"] != first["string_length_exceptions"]["graph_sha256"]
    assert [row["rule"] for row in first["string_length_exceptions"]["exceptions"]] == [
        "below_min", "below_min"]
    assert [row["rule"] for row in later["exceptions"]] == ["above_max", "above_max"]


# --------------------------------------------------------------------------- #
# 5 and 6. the rooftop product walk, out of process, pinned
# --------------------------------------------------------------------------- #
class InlineExecutor:
    def submit(self, fn, *args, **kwargs):
        fn(*args, **kwargs)


def product_report(client, tools, drawing_id, record, version):
    response = run_product(client, tools, drawing_id, {}, tool=record["name"],
                           dwg_version=version)
    assert response.status_code == 200, response.text
    envelope = response.json()
    assert envelope["ok"] is True, envelope
    return envelope["result"]


def test_rooftop_product_walk_runs_the_reports_in_the_sandbox(product, tmp_path, monkeypatch):
    client, backend, tools = product
    drawing_id, digest = upload(client)
    capability = checkout(client, drawing_id)
    seed_upload(client, tools, drawing_id, digest, capability)
    records = registered(tmp_path, monkeypatch)
    for record in records.values():
        tools[record["name"]] = record
    monkeypatch.setattr(jobs, "_executors", {jobs.LANE_FAST: InlineExecutor(),
                                             jobs.LANE_SLOW: InlineExecutor()})
    # Staging posture (the product harness sets it) with the subprocess sandbox
    # tier engaged: authored bodies run out of process, never in this one.
    monkeypatch.setenv("LEAF_SANDBOX", "e2b")
    monkeypatch.delenv("LEAF_TOOL_SANDBOX_PROVIDER", raising=False)
    monkeypatch.delenv("LEAF_AUTHORED_EXECUTION", raising=False)
    monkeypatch.setattr(tool_loader, "_load_module", forbidden)
    trusted_loader = solar_local_read._load_builtin
    monkeypatch.setattr(solar_local_read, "_load_builtin", forbidden)

    first = {key: product_report(client, tools, drawing_id, record, 2)
             for key, record in records.items()}
    # The trusted derivation below is the only caller of the trusted loader.
    monkeypatch.setattr(solar_local_read, "_load_builtin", trusted_loader)
    assert_matches_trusted(first, backend, PRODUCT_TENANT, drawing_id, 2)

    change(client, tools, drawing_id, capability)
    again = {key: product_report(client, tools, drawing_id, record, 2)
             for key, record in records.items()}
    for key in records:
        assert output_sha256(again[key]) == output_sha256(first[key])
    later = product_report(client, tools, drawing_id, records["zone_schedule"], 3)
    assert later["source_version"] == 3
    assert later["graph_sha256"] != first["zone_schedule"]["graph_sha256"]

    conflict = run_product(client, tools, drawing_id, {"drawing_id": "other"},
                           tool=records["zone_schedule"]["name"], dwg_version=2)
    assert conflict.status_code == 409, conflict.text
    assert conflict.json()["reason_code"] == "DRAWING_ID_CONFLICT"

    names = {record["name"] for record in records.values()}
    rows = [row for row in jobs._query("SELECT job_id, tool, status FROM jobs")
            if row["tool"] in names]
    assert len(rows) == 7 and all(row["status"] == "complete" for row in rows)
    for row in rows:
        job = jobs.get_job(row["job_id"])
        assert job["dwg_version"] in (2, 3)
        assert job["params"]["drawing_id"] == drawing_id


# --------------------------------------------------------------------------- #
# correction 1: paths that cannot persist the field refuse it; the size check
# measures the sandbox's own bytes
# --------------------------------------------------------------------------- #
class HarnessResponse:
    status_code = 200

    def __init__(self, body):
        self._body = body

    def json(self):
        return copy.deepcopy(self._body)

    def raise_for_status(self):
        return None


def harness_client(tmp_path, monkeypatch):
    """The templater client with the harness seam armed and requests.post recording."""
    local_posture(monkeypatch)
    client = author_client(tmp_path, monkeypatch)
    monkeypatch.setenv("LEAF_AUTHOR_HARNESS_URL", "http://harness.invalid")
    monkeypatch.delenv("LEAF_HARNESS_SECRET", raising=False)
    calls = []

    def post(url, **kwargs):
        calls.append((url, copy.deepcopy(kwargs.get("json"))))
        tool, code, preview = generator(kwargs["json"]["description"])
        return HarnessResponse({"tool": tool, "code": code, "preview": preview})

    monkeypatch.setattr(requests, "post", post)
    return client, calls


def assert_graph_input_unsupported(response):
    assert response.status_code == 422, response.text
    body = response.json()
    assert body["error_code"] == "GRAPH_INPUT_UNSUPPORTED"
    assert body["reason_code"] == "GRAPH_INPUT_UNSUPPORTED"
    assert body["tool"] is None and body["invalid_field"] == "graph_input"
    assert "templated path only" in body["error"]["message"]


def test_corr1_harness_path_refuses_graph_input(tmp_path, monkeypatch):
    client, calls = harness_client(tmp_path, monkeypatch)
    assert_graph_input_unsupported(register(client, "zone_schedule"))
    assert calls == []
    assert deps._AUTHORED == []
    assert not (tmp_path / "authored_tools.json").exists()


def test_corr1_harness_path_without_graph_input_unchanged(tmp_path, monkeypatch):
    client, calls = harness_client(tmp_path, monkeypatch)
    response = client.post("/api/author", json={
        "description": REPORTS["zone_schedule"]["description"], "mode": "build"})
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["source"] == "harness"
    assert body["tool"]["name"] == REPORTS["zone_schedule"]["name"]
    assert "graph_input" not in body["tool"]
    assert calls == [("http://harness.invalid/author", {
        "description": REPORTS["zone_schedule"]["description"], "tenant_id": TENANT})]
    # The harness registers in the tenant repo itself; the router persists nothing.
    assert deps._AUTHORED == []
    assert not (tmp_path / "authored_tools.json").exists()


def test_corr1_stage_refuses_graph_input(tmp_path, monkeypatch):
    local_posture(monkeypatch)
    client = author_client(tmp_path, monkeypatch)
    staged = []

    class Service:
        @classmethod
        def configured(cls):
            return cls()

        def stage(self, **kwargs):
            staged.append(kwargs)
            return {"contract": "fixture-stage", "ok": True}

    monkeypatch.setattr(deps, "auth_live", lambda: True)
    monkeypatch.setattr(deps, "stage_author_identity", lambda tenant, *_: tenant)
    monkeypatch.setattr(author_router, "customization_enabled", lambda *_: True)
    monkeypatch.setattr(author_router, "CustomizationService", Service)
    body = {"description": REPORTS["zone_schedule"]["description"], "mode": "build",
            "idempotency_key": "corr1-stage", "graph_input": GRAPH_INPUT}
    assert_graph_input_unsupported(client.post("/api/author/stage", json=body))
    assert staged == []
    # The same request without the field reaches the stage: the setup is live.
    del body["graph_input"]
    response = client.post("/api/author/stage", json=body)
    assert response.status_code == 200, response.text
    assert len(staged) == 1 and "graph_input" not in staged[0]


def test_corr1_size_check_matches_sandbox_bytes(mini, monkeypatch):
    backend, records = mini
    intake = authored_graph.build_graph_intake(backend, TENANT, DRAWING, 1)
    compact = len(json.dumps(intake, separators=(",", ":")).encode("utf-8"))
    spaced = len(json.dumps(intake).encode("utf-8"))
    assert spaced > compact
    # Under the bound in the sandbox's own bytes, over it with default separators:
    # admitted, and the tool runs.
    monkeypatch.setattr(authored_graph, "MAX_INTAKE_BYTES", compact)
    assert authored_graph.build_graph_intake(backend, TENANT, DRAWING, 1) == intake
    assert result_of(monkeypatch, backend, records["zone_schedule"])["source_version"] == 1
    # One byte over in the sandbox's own bytes: refused before the sandbox.
    monkeypatch.setattr(authored_graph, "MAX_INTAKE_BYTES", compact - 1)
    with pytest.raises(GraphValidationError) as excinfo:
        authored_graph.build_graph_intake(backend, TENANT, DRAWING, 1)
    assert excinfo.value.code == "GRAPH_INTAKE_TOO_LARGE"
    monkeypatch.setattr(broker, "run_tool_dynamic", forbidden)
    env, status = execute(monkeypatch, backend, records["zone_schedule"])
    assert status == 400, env
    assert env["error"]["reason_code"] == "GRAPH_INTAKE_TOO_LARGE"
