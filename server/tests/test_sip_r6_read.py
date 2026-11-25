"""Exact canonical source, read population, computations and deferred output bounds."""
from copy import deepcopy
from dataclasses import replace
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
import test_sip_r5_routes as catalog_cases
from test_sip_r5_routes import lane as catalog_lane  # noqa: F401
from test_w1_design_graph import graph  # noqa: F401

SAFE = frozenset((
    "solar-select-by-zone", "solar-design-presets-list", "solar-autofill-plan",
    "solar-string-rebuild", "solar-string-data", "solar-color-strings", "solar-guardrails-read",
    "solar-electrical-schedules", "solar-cable-export", "solar-nec-ampacity-correction",
    "solar-nec-ac-voltage-drop", "solar-nec-conduit-fill", "solar-nec-feeder-ocpd",
    "solar-solaredge-tracking-read", "solar-takeoff", "solar-assignment-report"))
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
    assert read.SUPPORTED_READ_TOOLS == SAFE and len(SAFE) == 16
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


def test_sip_r6_takeoff_graph_and_absent_physical(lane, monkeypatch):
    monkeypatch.setattr(local.solar_physical_head, "load_physical_head", blocked)
    monkeypatch.setattr(read.native, "_canonical_state", blocked)
    result = run(lane, "solar-takeoff", {})["result"]
    summary = result["output"]["summary"]
    assert summary["maturity"] == "preview" and summary["complete"] is False
    assert summary["head"] is None
    lines = {line["line_id"]: line for line in summary["lines"]}
    assert lines["strings.count"]["quantity"] == str(len(lane.ctx.graph["strings"]))
    assert lines["strings.modules"]["quantity"] == str(sum(s["module_count"] for s in lane.ctx.graph["strings"]))
    assert lines["ground.modules"]["quantity"] is None
    assert lines["ground.modules"]["reason"] == "PHYSICAL_STATE_ABSENT"
    assert lines["phase_b.labor"]["priceable"] is False
    assert result["drawing_changed"] is False


def test_sip_r6_takeoff_selected_native_and_corrupt_refusal(lane, monkeypatch):
    import project_drawing_editor as editor
    original = project.resolve_context
    first = next(value for value in lane.versions.values() if value.seq == 2)
    def selected(*args, **kwargs):
        verified = original(*args, **kwargs)
        if verified.binding.input_version_id != first.version_id:
            return verified
        intake = {**verified.intake, read.native.KEY: {}}
        raw = canonical_bytes(intake)
        return replace(verified, intake=intake, intake_bytes=raw, intake_sha256=sha256(raw).hexdigest())
    monkeypatch.setattr(project, "resolve_context", selected)
    monkeypatch.setattr(local.solar_physical_head, "load_physical_head", blocked)
    # Invalid embedded metadata is a decoder refusal, never absent physical state.
    with pytest.raises(editor.EditorError) as exc:
        read.native._canonical_state(selected(lane.tenant, lane.project, first.version_id,
                                             drawing_id=lane.drawing, write=False))
    assert exc.value.code == "PROJECT_LAYOUT_NATIVE_METADATA_INVALID"
    refused("SIP_R6_ARTIFACT_SOURCE_MISMATCH", lambda:
        run(lane, "solar-takeoff", {}, version=first.version_id))
    response = routes._output_dispatch(lambda:
        run(lane, "solar-takeoff", {}, version=first.version_id))
    assert response.status_code == 500
    assert b'"reason_code":"SIP_R6_ARTIFACT_SOURCE_MISMATCH"' in response.body
    assert b"The project artifact does not match its source version." in response.body
    seen = []
    def decode(verified):
        seen.append(verified.binding.input_version_id)
        return ({"kind": "canonical_version_intake", "version_id": str(verified.binding.input_version_id),
                 "index": verified.binding.version.seq - 1,
                 "state": {"artifact_id": verified.intake_sha256, "content_sha256": "a" * 64}},
                {"units": {"meters_per_unit": 1.0}, "state": {}}, {})
    monkeypatch.setattr(read.native, "_canonical_state", decode)
    result = run(lane, "solar-takeoff", {}, version=first.version_id)["result"]
    assert seen == [first.version_id]
    assert run(lane, "solar-takeoff", {})["result"]["output"]["summary"]["head"] is None
    assert seen == [first.version_id]
    summary = result["output"]["summary"]
    ref = summary["head"]["state"]
    assert ref == {"schema": "leaf.solar-canonical-native-state-ref.v1",
                   "version_id": str(first.version_id), "intake_sha256": result["input_intake_sha256"],
                   "state_sha256": "a" * 64}
    assert "download" not in ref and "artifact_id" not in ref
    ground = next(line for line in summary["lines"] if line["line_id"] == "ground.modules")
    assert ground["reason"] == "TRACKER_ROWS_ABSENT"
    assert ground["source"]["drawing_version"] == first.seq
    module = local._load_builtin("solar-takeoff")
    wrapper = {"head": summary["head"], "document": {"units": {"meters_per_unit": 1.0}, "state": {}}}
    module._physical(wrapper)
    for key, value in (("schema", "other"), ("version_id", "head"), ("intake_sha256", "bad"),
                       ("state_sha256", "bad"), ("download", {})):
        invalid = deepcopy(wrapper)
        invalid["head"]["state"][key] = value
        refused("TAKEOFF_PHYSICAL_INPUT_INVALID", lambda: module._physical(invalid))


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


def test_sip_r6_takeoff_catalog_uses_read_authority(catalog_lane, monkeypatch):
    import entitlements
    s = catalog_lane
    query = {"project_id": str(s.s.project), "drawing_id": str(s.s.drawing),
             "input_version_id": str(s.s.parent.version_id)}
    def state():
        return catalog_cases.states(s.client.get("/api/capabilities", params=query,
                                    headers=catalog_cases.headers(s)))["solar-takeoff"]
    granted = entitlements.entitlements_for
    monkeypatch.setattr(entitlements, "entitlements_for",
                        lambda *args, **kwargs: {**granted(*args, **kwargs), "run_read": True})
    available = state()
    assert available["project_read"] is True and available["engine_ready"] is True
    assert available["runnable"] is True and available["admission_checked"] is False
    query.pop("input_version_id")
    assert state()["project_read"] is False
    query["input_version_id"] = str(s.s.parent.version_id)
    s.tools["solar-takeoff"]["description"] += " changed"
    assert state()["project_read"] is False and state()["engine_ready"] is False
    s.tools["solar-takeoff"] = solar_tools.trusted_record("solar-takeoff")
    monkeypatch.setattr(entitlements, "entitlements_for",
                        lambda *args, **kwargs: {**granted(*args, **kwargs), "run_read": False})
    assert state()["project_read"] is False and state()["engine_ready"] is False
    assert not s.records


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


@pytest.fixture
def estimate_lane(lane, monkeypatch):
    """Instrument the real editor boundary; selected successor is a verified read double."""
    import json
    import project_estimate_custody as custody
    s = lane
    s.source_id = s.head
    s.source = project.resolve_context(s.tenant, s.project, s.source_id, drawing_id=s.drawing)
    s.raw_dxf = b"unchanged canonical DXF source\r\n"
    s.publications = []
    s.successor_id = UUID(int=999999)
    s.card = {"schema": "leaf.solar-estimate-rate-card.v1", "label": "TEST RATES",
              "version": "test-v7", "currency": "USD", "rates": [
                  {"line_id": "strings.modules", "unit": "module", "rate": "1.25"}]}
    # Match the real E1 unit, rather than inventing quantities or a takeoff result.
    summary = run(s, "solar-takeoff", {})["result"]["output"]["summary"]
    s.card["rates"][0]["unit"] = next(row["unit"] for row in summary["lines"]
                                                 if row["line_id"] == "strings.modules")
    s.request_id = UUID(int=999998)
    s.capability = "boundary-capability"
    s.receipt = {"drawing_version": {"version_id": str(s.successor_id)}}
    def capture(tenant, proj, drawing, parent, raw, **kwargs):
        s.publications.append((tenant, proj, drawing, parent, raw, deepcopy(kwargs)))
        return deepcopy(s.receipt)
    monkeypatch.setattr(custody.editor, "save_dxf", capture)
    monkeypatch.setattr(custody.editor, "read_dxf", lambda *args: (s.raw_dxf, sha256(s.raw_dxf).hexdigest()))
    s.resolve_original = project.resolve_context
    def selected(tenant, proj, version, **kwargs):
        if version == s.successor_id and proj == s.project and kwargs.get("drawing_id") == s.drawing:
            return s.successor
        return s.resolve_original(tenant, proj, version, **kwargs)
    monkeypatch.setattr(project, "resolve_context", selected)
    def save(card=None):
        return custody.save_estimate(s.tenant, s.project, s.drawing, s.source_id,
            s.card if card is None else card, catalog_digest=tools_cases.manifest("solar-takeoff"),
            request_id=s.request_id, capability=s.capability)
    s.save_estimate = save
    s.saved = save()
    s.snapshot_bytes = s.publications[-1][-1]["_restored_intake"]
    version = deepcopy(s.source.binding.version)
    version.version_id = s.successor_id
    version.seq += 1
    binding = replace(s.source.binding, input_version_id=s.successor_id,
                      head_version_id=s.successor_id, is_head=True, version=version)
    s.successor = replace(s.source, binding=binding, intake_bytes=s.snapshot_bytes,
        intake=json.loads(s.snapshot_bytes), intake_sha256=sha256(s.snapshot_bytes).hexdigest())
    return s


def test_estimate_canonical_save_and_exact_historical_retry(estimate_lane):
    import project_estimate_custody as custody
    s = estimate_lane
    original = deepcopy(s.source.intake)
    card = deepcopy(s.card)
    result = s.saved["result"]
    estimate = result["estimate"]
    assert result["publication"] == s.receipt and result["saved"] is True
    assert result["content_sha256"] == estimate["content_sha256"]
    assert estimate["rate_version"] == "test-v7"
    assert estimate["source_binding"] == {"organization_id": str(s.org),
        "project_id": str(s.project), "drawing_id": str(s.drawing),
        "input_version_id": str(s.source_id), "intake_sha256": s.source.intake_sha256,
        "graph_sha256": s.source.intake["solar_design_graph_sha256"]}
    takeoff = run(s, "solar-takeoff", {})["result"]["output"]["summary"]
    assert estimate == custody.material.price_takeoff(takeoff, card,
                                                     source_binding=estimate["source_binding"])
    companion = deepcopy(s.successor.intake)
    snapshot = companion.pop(custody.KEY)
    assert companion == original and s.card == card and s.source.intake == original
    assert snapshot == {"schema": custody.SNAPSHOT_SCHEMA,
        "estimate": custody.material.encode_estimate(estimate).decode(),
        "source_dxf_sha256": sha256(s.raw_dxf).hexdigest(), "source_version_id": str(s.source_id)}
    first = deepcopy(s.publications[0])
    assert first[1:5] == (s.project, s.drawing, s.source_id, s.raw_dxf)
    assert first[-1]["capability"] == s.capability and first[-1]["request_id"] == s.request_id
    s.head = s.successor_id
    assert s.save_estimate() == s.saved
    assert s.publications[-1] == first


def test_estimate_reopen_empty_and_changed_source(estimate_lane, monkeypatch):
    import project_estimate_custody as custody
    s = estimate_lane
    def reopen():
        return custody.read_estimate(s.tenant, s.project, s.drawing, s.successor_id)
    reopened = reopen()["result"]
    assert reopened["estimate"] == s.saved["result"]["estimate"]
    identity = {"organization_id": str(s.org), "project_id": str(s.project),
                "drawing_id": str(s.drawing), "input_version_id": str(s.successor_id)}
    assert {key: reopened[key] for key in identity} == identity
    assert reopened["estimate"]["source_binding"]["input_version_id"] == str(s.source_id)
    empty = custody.read_estimate(s.tenant, s.project, s.drawing, s.source_id)["result"]
    assert empty == {"schema": custody.RESULT_SCHEMA, "saved": False,
                     **identity, "input_version_id": str(s.source_id)}
    monkeypatch.setattr(custody.editor, "read_dxf", lambda *args:
        (b"edited" if args[-1] == s.successor_id else s.raw_dxf, "unused"))
    refused("ESTIMATE_SOURCE_CHANGED", reopen)
    monkeypatch.setattr(custody.editor, "read_dxf", lambda *args: (s.raw_dxf, "unused"))
    intake = deepcopy(s.successor.intake)
    intake["solar_design_graph"]["project"]["name"] = "Changed estimate source"
    intake["solar_design_graph_sha256"] = sha256(canonical_bytes(intake["solar_design_graph"])).hexdigest()
    raw = canonical_bytes(intake)
    s.successor = replace(s.successor, intake=intake, intake_bytes=raw, intake_sha256=sha256(raw).hexdigest())
    refused("ESTIMATE_SOURCE_CHANGED", reopen)
    assert custody.read_estimate(s.tenant, s.project, s.drawing, s.source_id)["result"]["saved"] is False


@pytest.mark.parametrize("change", ["shape", "version", "project", "intake"])
def test_estimate_refuses_wrong_snapshot_source(estimate_lane, change):
    import project_estimate_custody as custody
    s = estimate_lane
    intake = deepcopy(s.successor.intake)
    snapshot = intake[custody.KEY]
    if change == "shape":
        snapshot["extra"] = True
        reason = "ESTIMATE_SNAPSHOT_INVALID"
    elif change == "version":
        snapshot["source_version_id"] = str(UUID(int=11111))
        reason = "ESTIMATE_SOURCE_MISMATCH"
    else:
        estimate = custody.material.decode_estimate(snapshot["estimate"])
        binding = deepcopy(estimate["source_binding"])
        takeoff = deepcopy(estimate["takeoff"])
        if change == "project":
            binding["project_id"] = str(UUID(int=11111))
            takeoff["source"]["project_id"] = binding["project_id"]
        else:
            binding["intake_sha256"] = "0" * 64
        changed = custody.material.price_takeoff(takeoff, estimate["rate_card"], source_binding=binding)
        snapshot["estimate"] = custody.material.encode_estimate(changed).decode()
        reason = "ESTIMATE_SOURCE_MISMATCH"
    raw = canonical_bytes(intake)
    s.successor = replace(s.successor, intake=intake, intake_bytes=raw, intake_sha256=sha256(raw).hexdigest())
    refused(reason, lambda: custody.read_estimate(s.tenant, s.project, s.drawing, s.successor_id))


def test_estimate_publication_refusals_propagate(estimate_lane, monkeypatch):
    import project_estimate_custody as custody
    s = estimate_lane
    before = deepcopy(s.source.intake), len(s.publications)
    for reason in ("STALE_VERSION", "CHECKOUT_DENIED", "CHECKOUT_REQUIRED"):
        def deny(*args, **kwargs):
            project.refuse(reason)
        monkeypatch.setattr(custody.editor, "save_dxf", deny)
        refused("SIP_R1_" + reason, s.save_estimate)
    def conflict(*args, **kwargs):
        raise project.ProjectContextError("SIP_R2_IDEMPOTENCY_CONFLICT")
    monkeypatch.setattr(custody.editor, "save_dxf", conflict)
    changed = {**s.card, "version": "changed-card"}
    refused("SIP_R2_IDEMPOTENCY_CONFLICT", lambda: s.save_estimate(changed))
    def no_write(tenant, proj, *, write):
        if write:
            project.refuse("PROJECT_FORBIDDEN")
        return s.org, s.actor
    monkeypatch.setattr(project, "_access", no_write)
    refused("SIP_R1_PROJECT_FORBIDDEN", s.save_estimate)
    monkeypatch.setattr(custody.entitlements, "entitlements_for", lambda *args: {"run_read": False})
    assert custody.read_estimate(s.tenant, s.project, s.drawing, s.successor_id).status_code == 403
    monkeypatch.setattr(project, "_access", lambda *args, **kwargs: (s.org, s.actor))
    assert s.save_estimate().status_code == 403
    assert (s.source.intake, len(s.publications)) == before


def test_estimate_parent_router_save_read_export_and_request_guards(estimate_lane, monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    import project_estimate_custody as custody
    s = estimate_lane
    app = FastAPI()
    app.include_router(routes.router)
    app.dependency_overrides[deps.require_active_tenant] = lambda: s.tenant
    client = TestClient(app)
    base = f"/api/projects/{s.project}/drawings/{s.drawing}/versions/"
    url = base + str(s.source_id) + "/estimates"
    saved_url = base + str(s.successor_id) + "/estimates"
    headers = {"Idempotency-Key": str(s.request_id), "X-Checkout-Capability": s.capability}
    body = {"rate_card": s.card, "catalog_digest": tools_cases.manifest("solar-takeoff")}
    saved = client.post(url, json=body, headers=headers)
    assert saved.status_code == 200 and saved.json()["result"] == s.saved["result"]
    reopened = client.get(saved_url)
    assert reopened.status_code == 200 and reopened.json()["result"]["estimate"] == s.saved["result"]["estimate"]
    identity = {"organization_id": str(s.org), "project_id": str(s.project),
                "drawing_id": str(s.drawing), "input_version_id": str(s.successor_id)}
    assert {key: reopened.json()["result"][key] for key in identity} == identity
    empty = client.get(url)
    assert empty.status_code == 200
    assert empty.json()["result"] == {"schema": custody.RESULT_SCHEMA, "saved": False,
                                     **identity, "input_version_id": str(s.source_id)}
    exported = client.get(saved_url + "/export")
    assert exported.status_code == 200
    assert exported.content == custody.material.encode_estimate(s.saved["result"]["estimate"])
    assert exported.headers["etag"] == '"' + s.saved["result"]["content_sha256"] + '"'
    assert exported.headers["content-disposition"] == 'attachment; filename="estimate.json"'
    assert exported.headers["content-type"] == "application/json"
    assert client.get(url + "/export").status_code == 404
    before = len(s.publications)
    for content, media, status in ((b'{"rate_card":{},"rate_card":{},"catalog_digest":"x"}', "application/json", 400),
            (b'{"rate_card":{},"catalog_digest":NaN}', "application/json", 400),
            (b" " * 262145, "application/json", 413), (b"{}", "text/plain", 415),
            (b'{"rate_card":{},"catalog_digest":"x","total":1}', "application/json", 400)):
        response = client.post(url, content=content, headers={**headers, "Content-Type": media})
        assert response.status_code == status, response.text
        assert response.headers["cache-control"] == "no-store"
    response = client.post(url, json=body, headers={**headers, "X-Project-Id": str(UUID(int=111))})
    assert response.status_code == 404 and response.headers["cache-control"] == "no-store"
    response = client.post(url, json=body)
    assert response.status_code == 400 and response.headers["cache-control"] == "no-store"
    assert len(s.publications) == before
    assert saved.headers["cache-control"] == reopened.headers["cache-control"] == exported.headers["cache-control"] == "no-store"
    monkeypatch.setattr(custody.editor, "read_dxf", lambda *args:
        (b"later drawing edit" if args[-1] == s.successor_id else s.raw_dxf, "unused"))
    stale = client.get(saved_url + "/export")
    assert stale.status_code == 409 and stale.headers["cache-control"] == "no-store"
    assert stale.json()["error"]["reason_code"] == "ESTIMATE_SOURCE_CHANGED"
    assert "priced_subtotal" not in stale.text
    assert s.successor.intake[custody.KEY]["estimate"].encode() == exported.content
    def no_tenant():
        from fastapi import HTTPException
        raise HTTPException(status_code=401, detail="private authentication detail")
    app.dependency_overrides[deps.require_active_tenant] = no_tenant
    response = client.get(saved_url)
    assert response.status_code == 401 and response.headers["cache-control"] == "no-store"
    assert "private authentication detail" not in response.text


def test_estimate_manifest_flag_and_unavailable_read_are_not_empty(estimate_lane, monkeypatch):
    import project_estimate_custody as custody
    from routers import project_estimates as estimate_routes
    s = estimate_lane
    before = len(s.publications)
    refused("SIP_R6_TOOL_MANIFEST_MISMATCH", lambda: custody.save_estimate(
        s.tenant, s.project, s.drawing, s.source_id, s.card, catalog_digest="changed",
        request_id=s.request_id, capability=s.capability))
    monkeypatch.setenv("LEAF_SOLAR_PROJECT_RUN_ENABLED", "0")
    refused("project_execution_disabled", s.save_estimate)
    refused("project_execution_disabled", lambda:
        custody.read_estimate(s.tenant, s.project, s.drawing, s.source_id))
    monkeypatch.setenv("LEAF_SOLAR_PROJECT_RUN_ENABLED", "1")
    def unavailable(*args, **kwargs):
        raise custody.editor.EditorError("PROJECT_EDITOR_SOURCE_UNAVAILABLE", 503, True)
    monkeypatch.setattr(custody.editor, "read_dxf", unavailable)
    response = estimate_routes._dispatch(lambda:
        custody.read_estimate(s.tenant, s.project, s.drawing, s.successor_id))
    assert response.status_code == 503 and b'"saved":false' not in response.body
    assert response.headers["cache-control"] == "no-store"
    assert len(s.publications) == before


@pytest.mark.parametrize("collectors", [True, False])
def test_project_assignment_report_uses_real_kernel_without_mutation(lane, monkeypatch, collectors):
    from solar_design_graph import validate_graph
    from solar_solve_results import sync_assignments
    g = deepcopy(lane.ctx.graph)
    for string in g["strings"]:
        string["inverter_ref"] = string["to_ref"] = None
    for inverter in g["inverters"]:
        inverter["input_assignments"] = []
    if not collectors:
        g["inverters"] = []
    sync_assignments(g)
    g = validate_graph(g)
    lane.set_parent(tools_cases.embedded(g))
    before = deepcopy((lane.versions, lane.blobs, lane.writes, lane.head))
    # Setup publication calls retain the Memory store itself; keep their identity.
    calls_before = list(lane.calls)
    monkeypatch.setattr(project.graph_store(), "publish_version", blocked)
    monkeypatch.setattr(read.native, "_canonical_state", blocked)
    params = {"expected_rev": g["rev"], "accept_excess_capacity": True}
    tool = "solar-assignment-report"
    if collectors:
        result = run(lane, tool, params, version=lane.parent.version_id)["result"]
        assert result["drawing_changed"] is False
        assert result["input_version_id"] == str(lane.parent.version_id)
        assert result["output"] == local._load_builtin(tool).run(deepcopy(g), params)
        assert result["output"]["committed"] is False
        assert result["output"]["status"] == "complete"
        assert result["output"]["remaining_string_refs"] == []
        assert {item["string_ref"] for item in result["output"]["candidate_assignments"]} == {
            string["id"] for string in g["strings"]}
        refused("STALE_GRAPH_REVISION", lambda: run(lane, tool, dict(params, expected_rev=g["rev"] + 1),
                                                   version=lane.parent.version_id))
    else:
        assert local._load_builtin(tool).input_readiness(g) == {
            "input_ready": False, "input_reason": "string_collectors_required"}
        refused("NO_STRING_COLLECTORS", lambda: run(lane, tool, params, version=lane.parent.version_id))
    assert (lane.versions, lane.blobs, lane.writes, lane.head) == before
    assert lane.calls == calls_before


def test_project_assignment_report_catalog_has_synchronous_read_authority(catalog_lane, monkeypatch, graph):
    import entitlements
    s = catalog_lane
    import test_solar_tool_solar_assign_strings as assignment_cases
    g = assignment_cases.variant(graph, "U")
    s.s.set_parent(tools_cases.embedded(g))
    s.tools["solar-assignment-report"] = solar_tools.trusted_record("solar-assignment-report")
    granted = entitlements.entitlements_for
    monkeypatch.setattr(entitlements, "entitlements_for",
                        lambda *args, **kwargs: {**granted(*args, **kwargs), "run_read": True})
    query = {"project_id": str(s.s.project), "drawing_id": str(s.s.drawing),
             "input_version_id": str(s.s.parent.version_id)}
    def unavailable(*args, **kwargs):
        pytest.fail("read catalog must not require native worker admission")
    monkeypatch.setattr(read.policy, "project_tool_admission", unavailable)
    response = s.client.get("/api/capabilities", params=query, headers=catalog_cases.headers(s))
    state = catalog_cases.states(response)["solar-assignment-report"]
    assert state["runnable"] is True and state["project_read"] is True
    assert state["admission_checked"] is False

    assert state["engine_ready"] is True
    baseline = catalog_cases.states(response)["solar-settings"]
    g["inverters"] = []
    from solar_solve_results import sync_assignments
    sync_assignments(g)
    s.s.set_parent(tools_cases.embedded(g))
    response = s.client.get("/api/capabilities", params=query, headers=catalog_cases.headers(s))
    states = catalog_cases.states(response)
    assert states["solar-assignment-report"]["input_reason"] == "string_collectors_required"
    assert states["solar-assignment-report"]["runnable"] is False
    assert states["solar-settings"] == baseline


def test_assignment_report_catalog_refuses_missing_version_metadata(lane, monkeypatch):
    from types import SimpleNamespace
    # Reproduce the retained context-only producer used by the demo tenancy matrix.
    version = lane.parent
    incomplete = SimpleNamespace(**{key: value for key, value in vars(version).items()
                                    if key != "created_at"})
    assert not hasattr(incomplete, "created_at")
    lane.versions[version.version_id] = incomplete
    monkeypatch.setattr(project.graph_store(), "publish_version", blocked)
    monkeypatch.setattr(read.native, "_canonical_state", blocked)
    state = read.assignment_report_catalog_availability(
        solar_tools.trusted_record("solar-assignment-report"), lane.tenant,
        project_id=lane.project, drawing_id=lane.drawing, input_version_id=version.version_id)
    assert state["runnable"] is False and state["input_ready"] is False
    assert state["input_reason"] == "invalid_drawing_context"
    assert state["refusal_reasons"] == ["invalid_drawing_context"]

def test_sip_r6_string_continuity_read(lane):
    params = {"format": "continuity-json", "max_panel_pitch_m": 0.001}
    response = run(lane, "solar-string-data", params)["result"]
    result = response["output"]
    assert result["summary"]["max_panel_pitch_m"] == params["max_panel_pitch_m"]
    assert result["summary"]["strings"] == 1
    assert result["artifact"]["filename"] == "StringContinuity.json"
    assert response["input_version_id"] == str(lane.head)
    assert response["drawing_changed"] is False
    assert response["graph_sha256"] == lane.ctx.graph_sha256
