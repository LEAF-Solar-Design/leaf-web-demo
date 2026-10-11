"""Exact canonical source, read population, computations and deferred output bounds."""
from copy import deepcopy
from hashlib import sha256
from pathlib import Path
from types import SimpleNamespace
from uuid import UUID

import pytest
import deps
import solar_artifacts
import solar_local_read as local
import solar_project_context as project
import solar_project_graph as graph_adapter
import solar_project_read as read
import solar_tools
from leaf_cloud_client import canonical_bytes
from solar_design_graph import GraphValidationError
from routers import project_drawings as routes
import test_sip_r3b_tools as tools_cases
import test_sip_r3b_chain as chain_cases

SAFE = frozenset((
    "solar-select-by-zone", "solar-design-presets-list", "solar-autofill-plan",
    "solar-string-rebuild", "solar-string-data", "solar-color-strings", "solar-guardrails-read",
    "solar-electrical-schedules", "solar-cable-export", "solar-nec-ampacity-correction",
    "solar-nec-ac-voltage-drop", "solar-nec-conduit-fill", "solar-nec-feeder-ocpd"))
NEC = {
    "solar-nec-ampacity-correction": dict(base_ampacity=100, temp_factor=.91, conduit_factor=.8),
    "solar-nec-ac-voltage-drop": dict(current_a=100, one_way_length_ft=100,
        r_ohm_per_1000ft=.1, x_ohm_per_1000ft=0, power_factor=1, phase=1, source_voltage=480),
    "solar-nec-conduit-fill": dict(conductor_gauge="12 AWG", current_carrying_count=3,
        conduit_type="EMT", conductor_insulation="THWN2"),
    "solar-nec-feeder-ocpd": dict(continuous_current_a=100, is_continuous=True, egc_material="Copper"),
}
CALC = "solar-nec-ampacity-correction"


def setup(monkeypatch):
    monkeypatch.setenv("LEAF_SOLAR_PROJECT_RUN_ENABLED", "1")
    s = tools_cases.memory.__wrapped__(monkeypatch)
    s.ctx = chain_cases.through_equipment(s)[-1][2]
    s.tenant = deps.TenantContext(str(s.org), org_id=str(s.org), tier="hosted_pro", subject="reader")
    monkeypatch.setattr(deps, "find_tool", lambda name, tenant: solar_tools.trusted_record(name))
    monkeypatch.setattr(read.stored, "stored_job_entitlement_verdict", lambda org, kind: (None, None))
    monkeypatch.setattr(read.artifacts.write_loop, "drawing_mutations_refusal", lambda: None)
    return s


@pytest.fixture
def lane(monkeypatch):
    return setup(monkeypatch)


def run(s, tool=CALC, params=None, *, version=None, current=False, **changes):
    kwargs = {"catalog_digest": tools_cases.manifest(tool), "current": current}
    kwargs.update(changes)
    return read.run_project_read(s.tenant, s.project, s.drawing, version or s.head,
        tool, deepcopy(NEC[CALC] if params is None else params), **kwargs)


def output(s, tool=CALC, params=None, **kwargs):
    return run(s, tool, params, **kwargs)["result"]["output"]


def refused(code, operation):
    with pytest.raises((GraphValidationError, project.ProjectContextError)) as exc:
        operation()
    assert getattr(exc.value, "reason_code", getattr(exc.value, "code", None)) == code


def blocked(*args, **kwargs):
    pytest.fail("unexpected mutation or execution")


def test_sip_r6_exact_read_population(lane, monkeypatch):
    assert read.SUPPORTED_READ_TOOLS == SAFE and len(SAFE) == 13
    for declaration in solar_tools.entries():
        if declaration["name"] not in SAFE:
            refused("SIP_R6_TOOL_UNSUPPORTED", lambda: run(lane, declaration["name"], {}))
    before = list(lane.reads)
    monkeypatch.setattr(local, "_read_output", blocked)
    for tool in SAFE:
        refused("SIP_R6_TOOL_MANIFEST_MISMATCH", lambda: run(
            lane, tool, {}, catalog_digest="sha256:" + "0" * 64))
        with monkeypatch.context() as patch:
            record = solar_tools.trusted_record(tool)
            record["description"] += " changed"
            patch.setattr(deps, "find_tool", lambda *args: record)
            refused("SIP_R6_TOOL_MANIFEST_MISMATCH", lambda: run(lane, tool, {}))
    assert lane.reads == before
    with monkeypatch.context() as patch:
        declaration = deepcopy(solar_tools.get(CALC))
        declaration["trusted_inputs"] = ["pvcase_source"]
        original = solar_tools.get
        patch.setattr(solar_tools, "get", lambda name: declaration if name == CALC else original(name))
        refused("SIP_R6_TOOL_UNSUPPORTED", lambda: run(lane))


def test_sip_r6_exact_version_and_historical_read(lane):
    first = next(value for value in lane.versions.values() if value.seq == 2)
    selected = project.resolve_context(lane.tenant, lane.project, first.version_id,
                                       drawing_id=lane.drawing, write=False)
    ctx = graph_adapter.resolve_project_graph_context(selected)
    result = run(lane, version=first.version_id)["result"]
    assert result["input_version_id"] == str(first.version_id)
    assert result["input_intake_sha256"] == selected.intake_sha256
    assert result["graph_sha256"] == ctx.graph_sha256
    assert ctx.graph["rev"] == 1 and result["is_head"] is False
    assert result["head_version_id"] == str(lane.head)
    assert result["project_id"] == str(lane.project)
    assert result["graph_project_id"] == ctx.graph_project_id != str(lane.project)
    assert result["output_sha256"] == sha256(canonical_bytes(result["output"])).hexdigest()
    assert result["output_bytes"] == len(canonical_bytes(result["output"]))
    current = run(lane, current=True)["result"]
    historical = run(lane, current=False)["result"]
    assert current["request_sha256"] == historical["request_sha256"]


def test_sip_r6_nec_outputs(lane):
    results = {name: output(lane, name, params) for name, params in NEC.items()}
    for name, params in NEC.items():
        assert results[name] == local._load_builtin(name).run(deepcopy(lane.ctx.graph), deepcopy(params))
    assert results[CALC]["result"] == 72.8 and results[CALC]["units"] == "A"
    assert results["solar-nec-ac-voltage-drop"]["result"] == 0.4166666666666667
    fill = results["solar-nec-conduit-fill"]
    assert fill["success"] is True and fill["trade_size"] == "1/2"
    assert fill["fill_pct"] == 13.125 and fill["max_fill_pct"] == 40.0
    ocpd = results["solar-nec-feeder-ocpd"]
    assert ocpd["min_ocpd_a"] == ocpd["ocpd_rating_a"] == 125.0
    assert ocpd["egc_gauge"] == "6 AWG"


def test_sip_r6_graph_reports_and_no_file(lane):
    for tool in ("solar-select-by-zone", "solar-design-presets-list", "solar-autofill-plan",
                 "solar-string-rebuild", "solar-color-strings", "solar-guardrails-read"):
        params = {"zone_name": "Roof"} if tool == "solar-select-by-zone" else {}
        try:
            expected = local._load_builtin(tool).run(deepcopy(lane.ctx.graph), deepcopy(params))
        except GraphValidationError as exc:
            refused(exc.code, lambda: run(lane, tool, params))
        else:
            assert output(lane, tool, params) == expected
    assert output(lane, "solar-string-data", {}) == {
        "status": "no-file", "reason": "no-groups", "selected_strings": 1, "groups": 0}


def test_sip_r6_currentness_and_sizing_refusals(lane, monkeypatch):
    first = next(value for value in lane.versions.values() if value.seq == 2)
    refused("SIP_R6_STALE_CURRENT", lambda: run(lane, version=first.version_id, current=True))
    for tool in ("solar-electrical-schedules", "solar-cable-export"):
        refused("SOLAR_OUTPUT_NOT_CURRENT", lambda: run(lane, tool, {}))
        response = routes._output_dispatch(lambda: run(lane, tool, {}))
        assert response.status_code == 409
        assert b"The Solar read was refused for this version." in response.body
    refused("SCHEDULES_ZONE_SIZING_UNSUPPORTED", lambda:
        local._load_builtin("solar-cable-export")._sizer(lane.ctx.graph))
    original = local._load_builtin(CALC).run
    source = lane.head
    def moved(graph, params):
        lane.head = first.version_id
        return original(graph, params)
    monkeypatch.setattr(local._load_builtin(CALC), "run", moved)
    refused("SIP_R6_STALE_CURRENT", lambda: run(lane, version=source, current=True))
    assert routes._output_failure("SIP_R6_STALE_CURRENT").status_code == 409
    lane.head = source
    writes = list(lane.writes)
    def moved_file(graph, params):
        lane.head = first.version_id
        return solar_artifacts.ArtifactOutput({}, "application/json", "Moved.json", b"{}")
    monkeypatch.setattr(local._load_builtin(CALC), "run", moved_file)
    refused("SIP_R6_STALE_CURRENT", lambda: run(lane, version=source, current=True))
    assert lane.writes == writes


def test_sip_r6_no_publication_and_bounds(lane, monkeypatch):
    params = {**NEC[CALC], "drawing_id": str(lane.drawing)}
    before = deepcopy(params), deepcopy(lane.versions), list(lane.calls), list(lane.writes)
    assert read.run_project_read(lane.tenant, lane.project, lane.drawing, lane.head,
        CALC, params, catalog_digest=tools_cases.manifest(CALC))["result"]["drawing_changed"] is False
    assert (params, lane.versions, lane.calls, lane.writes) == before
    for args in ((str(lane.project), lane.drawing, lane.head, NEC[CALC], False),
                 (lane.project, lane.drawing, lane.head, [], False),
                 (lane.project, lane.drawing, lane.head, {"x": float("nan")}, False),
                 (lane.project, lane.drawing, lane.head, NEC[CALC], 1)):
        proj, drawing, version, parameters, current = args
        refused("SIP_R6_INVALID_REQUEST", lambda: read.run_project_read(
            lane.tenant, proj, drawing, version, CALC, parameters,
            catalog_digest=tools_cases.manifest(CALC), current=current))
    for module_name in ("solar_project_read.py", "solar_project_artifacts.py"):
        source = (Path(__file__).resolve().parents[1] / module_name).read_text(encoding="utf-8")
        for name in ("acquire_project_checkout", "submit_project_graph_job", "publish_project_graph_commit",
                     "publish_project_version", "load_manifest", "broker"):
            assert name not in source
    for count in (read.MAX_OUTPUT_BYTES, read.MAX_OUTPUT_BYTES - 100):
        with monkeypatch.context() as patch:
            patch.setattr(local._load_builtin(CALC), "run", lambda *args: {"x": "a" * count})
            refused("READ_OUTPUT_LIMIT_EXCEEDED", lambda: run(lane))
        assert lane.writes == before[-1]
    with monkeypatch.context() as patch:
        patch.setattr(local._load_builtin(CALC), "run", lambda *args:
            solar_artifacts.ArtifactOutput({"x": "a" * read.MAX_OUTPUT_BYTES},
                "application/json", "Bound.json", b"{}"))
        refused("READ_OUTPUT_LIMIT_EXCEEDED", lambda: run(lane))
    assert lane.writes == before[-1]
    with monkeypatch.context() as patch:
        prepared = []
        finish = read.artifacts.ProjectArtifactSink.finish
        def retained(sink, candidate):
            prepared.append(candidate.ref)
            return finish(sink, candidate)
        patch.setattr(read.artifacts.ProjectArtifactSink, "finish", retained)
        patch.setattr(local._load_builtin(CALC), "run", lambda *args:
            solar_artifacts.ArtifactOutput({"x": "a" * (read.MAX_OUTPUT_BYTES - 1400)},
                "application/json", "Bound.json", b"{}"))
        refused("READ_OUTPUT_LIMIT_EXCEEDED", lambda: run(lane))
        assert len(prepared) == 1
    assert lane.writes == before[-1]
    for value in (None, "0", "true", "1 ", " 1", "01", ""):
        if value is None:
            monkeypatch.delenv("LEAF_SOLAR_PROJECT_RUN_ENABLED", raising=False)
        else:
            monkeypatch.setenv("LEAF_SOLAR_PROJECT_RUN_ENABLED", value)
        refused("project_execution_disabled", lambda: run(lane))
