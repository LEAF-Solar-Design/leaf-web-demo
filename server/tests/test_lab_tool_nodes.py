"""Fail-closed tool admission, durable dispatch and remote result boundaries."""
import ast
from copy import deepcopy
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
import threading
from types import SimpleNamespace

import pytest

import jobs
import deps
import lab_registry
import lab_tool_manifests
import lab_tool_nodes as nodes
from lab_evaluate import Budget, GraphRefused, RunBudgetExceeded, RunCancelled, evaluate_graph, runtime_value
from test_lab_evaluate import case

NOW = datetime(2026, 10, 25, 12, tzinfo=timezone.utc)
TENANT = "11111111-1111-1111-1111-111111111111"
OTHER_TENANT = "22222222-2222-2222-2222-222222222222"
DRAWING = {"project_id": "project", "drawing_id": "drawing", "dwg_version": 2}


def tool_graph(monkeypatch):
    # No compute tool is currently seeded. Register the test extension through
    # the real authority, and restore the registry after the test.
    monkeypatch.setattr(deps, "effective_tools_with_provenance", lambda tenant: [])
    monkeypatch.setattr(lab_registry, "REGISTRY", dict(lab_registry.REGISTRY))
    trusted = lab_registry.register_extension({
        "id": "qa-lab-tool", "kind": "qa-lab-tool", "version": "1.0.0",
        "capability": "read.value", "execution_class": "worker",
        "schema": {"version": 1}, "fixtures": [{}], "samples": [{}],
        "ui_schema": {}, "latency": {"p50_ms": 1, "p95_ms": 50},
        "migration": {"policy": "none"}})
    graph, original = case("constant-number")
    manifest = next(iter(original.values()))
    manifest.update(id=trusted.kind, effect="read")
    manifest["settings_schema"] = {
        "type": "object", "additionalProperties": False, "properties": {
            "drawing": {"type": "object", "additionalProperties": False,
                        "properties": {"project_id": {"type": "string"},
                                       "drawing_id": {"type": "string"},
                                       "dwg_version": {"type": "integer"}}}}}
    graph["blocks"][0]["registry"]["id"] = trusted.kind
    graph["blocks"][0]["settings"] = {"drawing": dict(DRAWING)}
    tool = {"name": trusted.kind, "version": trusted.version, "capability": "read.value",
            "execution_class": "worker", "capabilities": ["drawing.read"],
            "params": {"type": "object", "properties": {"drawing_id": {"type": "string"}}}}
    seeds = nodes.catalog.seed_tools()
    monkeypatch.setattr(nodes.catalog, "seed_tools", lambda: seeds + [tool])
    return SimpleNamespace(graph=graph, registry={trusted.kind + "@1.0.0": manifest},
                           trusted=trusted, manifest=manifest, tool=tool)


def projected_graph(monkeypatch, **changes):
    from test_lab_tool_manifests import read_tool
    tool = read_tool(params={"type": "object", "properties": {
        "drawing_id": {"type": "string"},
        "max_rows": {"type": "integer", "default": 3}}}, **changes)
    monkeypatch.setattr(deps, "effective_tools_with_provenance", lambda tenant: [(tool, "tenant_repo")])
    descriptor, = lab_tool_manifests.tenant_tool_projection(TENANT)
    graph = {"schema": "leaf.lab-graph.v1", "blocks": [{
        "id": "node", "registry": {"id": descriptor.name, "version": descriptor.version,
                                     "code_sha256": descriptor.code_sha256},
        "settings": {"drawing": dict(DRAWING)}}], "pipes": [],
        "outputs": {"result": {"node": "node", "port": "result"}}}
    return SimpleNamespace(tool=tool, descriptor=descriptor, graph=graph,
                           trusted=descriptor.trusted, manifest=descriptor.manifest,
                           registry=lab_tool_manifests.registry_for(TENANT))


@pytest.fixture
def rig(monkeypatch):
    monkeypatch.delenv("LEAF_ENTITLEMENTS_FILE", raising=False)
    for name in ("LEAF_LAB_TOOL_DAILY_JOBS", "LEAF_LAB_TOOL_DAILY_SANDBOX_S", "LEAF_LAB_TOOL_TIMEOUT_S"):
        monkeypatch.delenv(name, raising=False)
    result = tool_graph(monkeypatch)
    result.calls = []
    def submit(*args, **kwargs):
        result.calls.append((args, kwargs))
        return "job"
    result.options = dict(cancelled=lambda: False, submit=submit,
                          get_job=lambda job: {"status": "complete", "result": {"ok": True, "result": 7}},
                          list_jobs=lambda *args: [], tier_of=lambda tenant: "hosted_pro",
                          sandbox_seconds_today=lambda *args: 0, now=lambda: NOW,
                          sleep=lambda seconds: None)
    def call(**kwargs):
        return nodes.ToolNodeAdapter(kwargs.get("tenant", TENANT), kwargs.get("run_id", "run"),
                                     **result.options)(
            "node", kwargs.get("trusted", result.trusted), kwargs.get("manifest", result.manifest),
            kwargs.get("inputs", {}), kwargs.get("settings", {"drawing": dict(DRAWING)}),
            deadline_s=kwargs.get("deadline_s"))
    result.call = call
    return result


@pytest.mark.parametrize("flag,expected", [(None, False), ("0", False), ("true", False), ("1", True)])
def test_flag(monkeypatch, flag, expected):
    monkeypatch.delenv("LEAF_LAB_TOOL_NODES", raising=False)
    if flag is not None:
        monkeypatch.setenv("LEAF_LAB_TOOL_NODES", flag)
    assert nodes.enabled() is expected


@pytest.mark.parametrize("change", [None, {"residency": "host"}, {"aps_live": True},
                                  {"capability": "drawing.write"}, {"capability": "solve.child"},
                                  {"capability": "cloud-proposal"}])
def test_effect_refused(rig, change):
    trusted = None if change is None else replace(rig.trusted, **change)
    assert rig.call(trusted=trusted, tenant="not-a-uuid") == {"error": "lab_tool_effect_refused"}
    assert not rig.calls


def test_default_effects_keep_compute_and_read(rig):
    assert nodes.tool_block_admissible(rig.trusted)
    read = replace(rig.trusted, effect="read", execution_class="inline")
    assert nodes.tool_block_admissible(read)
    assert not nodes.tool_block_admissible(replace(read, capability="simulate.value", effect="simulate"))


def test_effect_override_refuses_compute_before_dispatch(rig):
    adapter = nodes.ToolNodeAdapter(TENANT, "run", effects=("read", "simulate"), **rig.options)
    assert not adapter.admissible(rig.trusted)
    assert adapter("node", rig.trusted, rig.manifest, {}, {"drawing": DRAWING}) == {
        "error": "lab_tool_effect_refused"}
    assert not rig.calls
    assert adapter.admissible(replace(rig.trusted, effect="read", execution_class="inline"))
    assert adapter.admissible(replace(rig.trusted, effect="simulate", execution_class="inline", capability="simulate.value"))


@pytest.mark.parametrize("tenant", ["not-a-uuid", "", TENANT.replace("-", ""),
                                  "AAAAAAAA-AAAA-AAAA-AAAA-AAAAAAAAAAAA", "{" + TENANT + "}"])
def test_tenant_identity_precedes_replay_and_other_admission(rig, tenant):
    reads = []
    def list_jobs(*args):
        reads.append("jobs")
        return [{"idempotency_key": "lab:run:node:1", "created_at": NOW}]
    def ledger(*args):
        reads.append("ledger")
        return 0
    def tier_of(*args):
        reads.append("tier")
        return "hosted_pro"
    rig.options.update(list_jobs=list_jobs, sandbox_seconds_today=ledger, tier_of=tier_of)
    assert rig.call(tenant=tenant) == {"error": "lab_tool_budget_unreadable"}
    assert rig.call(tenant=tenant, inputs={"value": runtime_value(None, error="upstream")},
                    settings={}) == {"error": "lab_tool_budget_unreadable"}
    assert not reads
    assert not rig.calls


def test_input_error(rig):
    assert rig.call(inputs={"value": runtime_value(None, error="upstream")}) == {"error": "lab_tool_input_error"}
    assert not rig.calls


@pytest.mark.parametrize("drawing", [None, {}, {**DRAWING, "dwg_version": True},
                                    {**DRAWING, "dwg_version": 0}, {**DRAWING, "extra": 1},
                                    {**DRAWING, "project_id": ""}, {**DRAWING, "drawing_id": "x" * 201}])
def test_drawing_required(rig, drawing):
    assert rig.call(settings={"drawing": drawing}) == {"error": "lab_tool_drawing_required"}
    assert not rig.calls


def test_unknown(rig):
    assert rig.call(trusted=replace(rig.trusted, kind="missing")) == {"error": "lab_tool_unknown"}
    assert not rig.calls


def test_drawing_mismatch(rig):
    assert rig.call(inputs={"drawing_id": runtime_value("other")}) == {"error": "lab_tool_drawing_mismatch"}
    assert not rig.calls


@pytest.mark.parametrize("tier", [None, "", "  "])
def test_binding_unavailable(rig, tier):
    rig.options["tier_of"] = lambda tenant: tier
    assert rig.call() == {"error": "lab_tool_binding_unavailable"}
    assert not rig.calls


@pytest.mark.parametrize("tier", ["hosted_pro", "demo_live", "", "  ", " hosted_pro "])
def test_default_tier_preserves_broker_record(rig, monkeypatch, tier):
    import platform_link

    reads = []
    platform_reads = []
    def broker_tier(tenant):
        reads.append(tenant)
        return tier
    def platform_store():
        platform_reads.append(True)
        raise AssertionError("broker tier must be authoritative")
    monkeypatch.setattr(deps, "backedge_tier", broker_tier)
    monkeypatch.setattr(platform_link, "platform_store", platform_store)
    assert nodes.lab_tool_tier(TENANT) == tier
    rig.options["tier_of"] = None
    if not tier.strip():
        expected = {"error": "lab_tool_binding_unavailable"}
    elif nodes.entitlements.entitlements_for(tier).get("run_read"):
        expected = {"outputs": {"result": 7}}
    else:
        expected = {"error": "lab_tool_entitlement_required"}
    assert rig.call() == expected
    assert bool(rig.calls) == ("outputs" in expected)
    assert reads == [TENANT, TENANT]
    assert not platform_reads


@pytest.mark.parametrize("capability,required", [("drawing.read", "run_read"), ("drawing.write", "run_write")])
def test_default_tier_active_platform_org_preserves_tier(rig, monkeypatch, capability, required):
    import platform_link

    # SEL-316j2: preserve padded tiers exactly as the API does to prevent elevation.
    reads = []
    def get_org(tenant):
        reads.append(tenant)
        return SimpleNamespace(status="active", tier=" hosted_pro ")
    monkeypatch.setattr(deps, "backedge_tier", lambda tenant: None)
    monkeypatch.setattr(platform_link, "platform_store", lambda: SimpleNamespace(get_org=get_org))
    assert nodes.lab_tool_tier(TENANT) == " hosted_pro "
    rig.tool["capabilities"] = [capability]
    rig.options["tier_of"] = None
    policy = nodes.entitlements.entitlements_for
    tiers = []
    def entitlements_for(resolved):
        tiers.append(resolved)
        return policy(resolved)
    monkeypatch.setattr(nodes.entitlements, "entitlements_for", entitlements_for)
    expected = ({"outputs": {"result": 7}} if policy(" hosted_pro ").get(required)
                else {"error": "lab_tool_entitlement_required"})
    assert rig.call() == expected
    assert tiers == [" hosted_pro "]
    assert reads == [TENANT, TENANT]
    assert bool(rig.calls) == ("outputs" in expected)


def test_default_tier_broker_error_fails_closed(rig, monkeypatch):
    def broker_tier(tenant):
        raise RuntimeError("unavailable")
    monkeypatch.setattr(deps, "backedge_tier", broker_tier)
    assert nodes.lab_tool_tier(TENANT) is None
    rig.options["tier_of"] = None
    assert rig.call() == {"error": "lab_tool_binding_unavailable"}
    assert not rig.calls


@pytest.mark.parametrize("authority", ["inactive", "missing", "store_error", "lookup_error"])
def test_default_tier_platform_authority_fails_closed(rig, monkeypatch, authority):
    import platform_link

    def get_org(tenant):
        assert tenant == TENANT
        if authority == "lookup_error":
            raise RuntimeError("unavailable")
        return None if authority == "missing" else SimpleNamespace(status="inactive", tier="hosted_pro")
    def platform_store():
        if authority == "store_error":
            raise RuntimeError("unavailable")
        return SimpleNamespace(get_org=get_org)
    monkeypatch.setattr(deps, "backedge_tier", lambda tenant: None)
    monkeypatch.setattr(platform_link, "platform_store", platform_store)
    rig.options["tier_of"] = None
    assert rig.call() == {"error": "lab_tool_binding_unavailable"}
    assert not rig.calls


@pytest.mark.parametrize("tier", ["", "  ", None, 7])
@pytest.mark.parametrize("capability,required", [("drawing.read", "run_read"), ("drawing.write", "run_write")])
def test_default_tier_blank_platform_tier_uses_restricted_policy(rig, monkeypatch, tier, capability, required):
    import platform_link

    monkeypatch.setattr(deps, "backedge_tier", lambda tenant: None)
    monkeypatch.setattr(platform_link, "platform_store", lambda: SimpleNamespace(
        get_org=lambda tenant: SimpleNamespace(status="active", tier=tier)))
    assert nodes.lab_tool_tier(TENANT) == "restricted"
    rig.tool["capabilities"] = [capability]
    rig.options["tier_of"] = None
    policy = nodes.entitlements.entitlements_for
    tiers = []
    def entitlements_for(resolved):
        tiers.append(resolved)
        return policy(resolved)
    monkeypatch.setattr(nodes.entitlements, "entitlements_for", entitlements_for)
    expected = ({"outputs": {"result": 7}} if policy("restricted").get(required)
                else {"error": "lab_tool_entitlement_required"})
    assert rig.call() == expected
    assert tiers == ["restricted"]
    assert bool(rig.calls) == ("outputs" in expected)


def test_entitlement_required(rig, monkeypatch):
    monkeypatch.setattr(nodes.entitlements, "entitlements_for", lambda tier: {})
    assert rig.call() == {"error": "lab_tool_entitlement_required"}
    assert not rig.calls


def fail(*args, **kwargs):
    raise RuntimeError("unavailable")


def test_policy_unavailable(rig, monkeypatch):
    def policy(tier):
        raise nodes.entitlements.EntitlementsError("unreadable")
    monkeypatch.setattr(nodes.entitlements, "entitlements_for", policy)
    assert rig.call() == {"error": "lab_tool_policy_unavailable"}
    assert not rig.calls


@pytest.mark.parametrize("reader", ["list_jobs", "sandbox_seconds_today"])
def test_ledger_unavailable(rig, reader):
    rig.options[reader] = fail
    assert rig.call() == {"error": "lab_tool_budget_unreadable"}
    assert not rig.calls


def test_daily_jobs_exhausted(rig):
    rig.options["list_jobs"] = lambda *args: [
        {"idempotency_key": f"lab:other:{i}:1", "created_at": NOW.timestamp()} for i in range(100)]
    assert rig.call() == {"error": "lab_tool_daily_jobs_exhausted"}
    assert not rig.calls


def test_same_tenant_concurrent_admission_serialises_through_submit(rig, monkeypatch):
    monkeypatch.setenv("LEAF_LAB_TOOL_DAILY_JOBS", "1")
    lock = nodes._tenant_lock(TENANT)
    contended, submitting, release = threading.Event(), threading.Event(), threading.Event()
    rows, reads, results = [], [], {}

    class ObservedLock:
        def __enter__(self):
            if lock.locked():
                contended.set()
            lock.acquire()

        def __exit__(self, *args):
            lock.release()

    monkeypatch.setitem(nodes._tenant_locks, TENANT, ObservedLock())
    def list_jobs(*args):
        assert lock.locked()
        reads.append(len(rows))
        return list(rows)
    def ledger(*args):
        assert lock.locked()
        return 0
    def submit(*args, **kwargs):
        assert lock.locked()
        submitting.set()
        assert release.wait(5)
        rig.calls.append((args, kwargs))
        rows.append({"idempotency_key": kwargs["idempotency_key"], "created_at": NOW})
        return "job"
    rig.options.update(list_jobs=list_jobs, sandbox_seconds_today=ledger, submit=submit)
    first = threading.Thread(target=lambda: results.update(first=rig.call(run_id="first")))
    second = threading.Thread(target=lambda: results.update(second=rig.call(run_id="second")))
    first.start()
    try:
        assert submitting.wait(5)
        second.start()
        assert contended.wait(5)
        assert reads == [0]
    finally:
        release.set()
        first.join(5)
        if second.ident is not None:
            second.join(5)
    assert not first.is_alive() and not second.is_alive()
    assert results == {"first": {"outputs": {"result": 7}},
                       "second": {"error": "lab_tool_daily_jobs_exhausted"}}
    assert reads == [0, 1]
    assert len(rig.calls) == 1


def test_different_tenants_admit_while_first_tenant_is_submitting(rig):
    submitting, release, second_done = threading.Event(), threading.Event(), threading.Event()
    results = {}
    def submit(tenant, *args, **kwargs):
        if tenant == TENANT:
            submitting.set()
            assert release.wait(5)
        rig.calls.append((tenant, kwargs))
        return "job"
    rig.options["submit"] = submit
    def second_call():
        results["second"] = rig.call(tenant=OTHER_TENANT)
        second_done.set()
    first = threading.Thread(target=lambda: results.update(first=rig.call()))
    second = threading.Thread(target=second_call)
    first.start()
    try:
        assert submitting.wait(5)
        second.start()
        assert second_done.wait(5)
        assert results["second"] == {"outputs": {"result": 7}}
    finally:
        release.set()
        first.join(5)
        if second.ident is not None:
            second.join(5)
    assert not first.is_alive() and not second.is_alive()
    assert results["first"] == {"outputs": {"result": 7}}
    assert len(rig.calls) == 2


def test_result_wait_does_not_hold_tenant_admission_lock(rig):
    waiting, release, second_done = threading.Event(), threading.Event(), threading.Event()
    results = {}
    def submit(*args, **kwargs):
        return kwargs["idempotency_key"]
    def get_job(job):
        if job == "lab:first:node:1":
            waiting.set()
            assert release.wait(5)
        return {"status": "complete", "result": {"result": 7}}
    rig.options.update(submit=submit, get_job=get_job)
    def second_call():
        results["second"] = rig.call(run_id="second")
        second_done.set()
    first = threading.Thread(target=lambda: results.update(first=rig.call(run_id="first")))
    second = threading.Thread(target=second_call)
    first.start()
    try:
        assert waiting.wait(5)
        second.start()
        assert second_done.wait(5)
        assert results["second"] == {"outputs": {"result": 7}}
    finally:
        release.set()
        first.join(5)
        if second.ident is not None:
            second.join(5)
    assert not first.is_alive() and not second.is_alive()
    assert results["first"] == {"outputs": {"result": 7}}


def test_incomplete_job_window(rig):
    rig.options["list_jobs"] = lambda *args: [{"idempotency_key": None, "created_at": NOW}] * 200
    assert rig.call() == {"error": "lab_tool_budget_unreadable"}
    assert not rig.calls


def test_sandbox_exhausted(rig):
    rig.options["sandbox_seconds_today"] = lambda *args: 3600
    assert rig.call() == {"error": "lab_tool_daily_sandbox_exhausted"}
    assert not rig.calls


@pytest.mark.parametrize("name,value", [
    ("LEAF_LAB_TOOL_DAILY_JOBS", "0"), ("LEAF_LAB_TOOL_DAILY_JOBS", "201"),
    ("LEAF_LAB_TOOL_DAILY_JOBS", "bad"), ("LEAF_LAB_TOOL_DAILY_SANDBOX_S", "nan"),
    ("LEAF_LAB_TOOL_DAILY_SANDBOX_S", "0"), ("LEAF_LAB_TOOL_DAILY_SANDBOX_S", "86401"),
    ("LEAF_LAB_TOOL_TIMEOUT_S", "0"), ("LEAF_LAB_TOOL_TIMEOUT_S", "601")])
def test_invalid_budget(rig, monkeypatch, name, value):
    monkeypatch.setenv(name, value)
    assert rig.call() == {"error": "lab_tool_budget_unreadable"}
    assert not rig.calls


@pytest.mark.parametrize("error,code", [(jobs.TenantInflightCapExceeded("tenant", 1, 1), "inflight_cap"),
                                       (ValueError("refused"), "submit_refused"),
                                       (RuntimeError("down"), "submit_unavailable")])
def test_submit_refusal(rig, error, code):
    def reject(*args, **kwargs):
        raise error
    rig.options["submit"] = reject
    assert rig.call() == {"error": "lab_tool_" + code}
    assert not rig.calls


def test_submit_identity_and_result(rig):
    assert rig.call() == {"outputs": {"result": 7}}
    assert len(rig.calls) == 1
    args, kwargs = rig.calls[0]
    assert args == (TENANT, {**rig.tool, "lab_execution": {
        "run_id": "run", "block_id": "node", "attempt": 1}}, {"drawing_id": "drawing"}, "drawing", False)
    assert kwargs == {"org_id": TENANT, "project_id": "project", "dwg_version": 2,
                      "idempotency_key": "lab:run:node:1"}


def test_projected_row_and_parameter_names(rig, monkeypatch):
    projected = projected_graph(monkeypatch)
    assert rig.call(trusted=projected.trusted, manifest=projected.manifest,
                    inputs={"max-rows": runtime_value(8)}) == {"outputs": {"result": 7}}
    args, kwargs = rig.calls[0]
    assert args == (TENANT, {**projected.tool, "lab_execution": {
        "run_id": "run", "block_id": "node", "attempt": 1}},
        {"max_rows": 8, "drawing_id": "drawing"}, "drawing", False)
    assert kwargs["idempotency_key"] == "lab:run:node:1"


def test_projected_row_changed(rig, monkeypatch):
    projected = projected_graph(monkeypatch)
    monkeypatch.setattr(lab_tool_manifests, "row_for", lambda *args: None)
    assert rig.call(trusted=projected.trusted, manifest=projected.manifest) == {
        "error": "lab_tool_tool_changed"}
    assert not rig.calls


def test_refused_tenant_tool_never_falls_back_to_a_seed(rig, monkeypatch):
    from dataclasses import replace
    projected = projected_graph(monkeypatch)
    descriptor = next(tool for tool in lab_tool_manifests.tenant_tool_projection(TENANT)
                      if tool.name == projected.trusted.kind)
    refused = replace(descriptor, manifest=None, reason="live APS tools are not Lab nodes")
    monkeypatch.setattr(lab_tool_manifests, "tenant_tool_projection", lambda tenant: (refused,))
    monkeypatch.setattr(lab_tool_manifests, "trusted_for", lambda *args: projected.trusted)
    monkeypatch.setattr(lab_tool_manifests, "row_for", lambda *args: None)
    seeds = nodes.catalog.seed_tools()
    monkeypatch.setattr(nodes.catalog, "seed_tools", lambda: seeds + [dict(projected.tool)])
    assert rig.call(trusted=projected.trusted, manifest=projected.manifest) == {
        "error": "lab_tool_tool_changed"}
    assert not rig.calls


@pytest.mark.parametrize("data,error", [({"z": 2, "a": [True, None]}, None),
                                       ("x" * 65535, "output-refused")],
                         ids=["within-limit", "over-limit"])
def test_projected_json_text_pin(rig, monkeypatch, data, error):
    from lab_evaluate import canonical_json
    projected = projected_graph(monkeypatch, returns_schema=None)
    rig.options["get_job"] = lambda job: {"status": "complete", "result": {"result": data}}
    result = evaluate_graph(projected.graph, projected.registry,
                            trusted_lookup=lambda block_id, version: projected.trusted,
                            tool_adapter=nodes.ToolNodeAdapter(TENANT, "run", **rig.options))
    assert result["outputs"]["result"].error == error
    if error is None:
        assert result["outputs"]["result"].data == canonical_json(data)


def test_takeover_reuses_key_and_skips_budgets(rig, monkeypatch):
    assert rig.call() == {"outputs": {"result": 7}}
    rig.options["list_jobs"] = lambda *args: [
        {"idempotency_key": "lab:run:node:1", "created_at": NOW}] * 100
    rig.options["sandbox_seconds_today"] = fail
    monkeypatch.setenv("LEAF_LAB_TOOL_DAILY_JOBS", "1")
    assert rig.call() == {"outputs": {"result": 7}}
    assert len(rig.calls) == 2
    assert rig.calls[0] == rig.calls[1]


def test_cancel_wait(rig):
    cancelled = [False]
    rig.options.update(cancelled=lambda: cancelled[0], get_job=lambda job: {"status": "running"},
                       sleep=lambda seconds: cancelled.__setitem__(0, True))
    with pytest.raises(RunCancelled):
        rig.call()
    assert len(rig.calls) == 1


def test_timeout(rig):
    clock = [0]
    rig.options.update(monotonic=lambda: clock[0], get_job=lambda job: None,
                       sleep=lambda seconds: clock.__setitem__(0, clock[0] + 301))
    assert rig.call() == {"error": "lab_tool_timeout"}


@pytest.mark.parametrize("deadline_s", [1, 300])
def test_run_deadline_expires(rig, deadline_s):
    clock = [0]
    sleeps = []
    def sleep(seconds):
        sleeps.append(seconds)
        clock[0] += seconds
    rig.options.update(monotonic=lambda: clock[0], get_job=lambda job: None, sleep=sleep)
    with pytest.raises(RunBudgetExceeded):
        rig.call(deadline_s=deadline_s)
    assert clock[0] == deadline_s
    assert all(0 < seconds <= 0.5 for seconds in sleeps)


def test_tool_timeout_before_run_deadline(rig):
    clock = [0]
    rig.options.update(monotonic=lambda: clock[0], get_job=lambda job: None,
                       sleep=lambda seconds: clock.__setitem__(0, clock[0] + seconds))
    assert rig.call(deadline_s=600) == {"error": "lab_tool_timeout"}
    assert clock[0] == 300


def test_run_deadline_includes_submission(rig):
    clock = [0]
    polls = []
    def submit(*args, **kwargs):
        clock[0] = 2
        return "job"
    rig.options.update(monotonic=lambda: clock[0], submit=submit,
                       get_job=lambda job: polls.append(job))
    with pytest.raises(RunBudgetExceeded):
        rig.call(deadline_s=1)
    assert not polls


def test_run_deadline_refuses_late_result(rig):
    clock = [0]
    def get(job):
        clock[0] = 1
        return {"status": "complete", "result": {"result": 7}}
    rig.options.update(monotonic=lambda: clock[0], get_job=get)
    with pytest.raises(RunBudgetExceeded):
        rig.call(deadline_s=1)


def test_sequential_adapter_blocks_stay_within_run_budget(rig):
    clock = [0]
    deadlines, published = [], []
    rig.manifest["inputs"]["value"] = deepcopy(rig.manifest["outputs"]["result"])
    first = rig.graph["blocks"][0]
    rig.graph["blocks"] = [dict(deepcopy(first), id=f"block-{i}") for i in range(3)]
    rig.graph["pipes"] = [
        {"from": {"node": f"block-{i}", "port": "result"},
         "to": {"node": f"block-{i + 1}", "port": "value"}} for i in range(2)]
    rig.graph["outputs"]["result"]["node"] = "block-2"
    class Adapter:
        admissible = staticmethod(nodes.tool_block_admissible)

        def __call__(self, block_id, trusted, manifest, inputs, settings, *, deadline_s):
            deadlines.append(deadline_s)
            if block_id != "block-0":
                assert inputs["value"].data == 7
            clock[0] += 299
            return {"outputs": {"result": 7}}

    with pytest.raises(RunBudgetExceeded):
        evaluate_graph(rig.graph, rig.registry, tool_adapter=Adapter(), clock=lambda: clock[0],
                       on_block=lambda block_id, outputs: published.append((block_id, outputs)))
    assert deadlines == [600, 301, 2]
    assert [block_id for block_id, _ in published] == ["block-0", "block-1"]
    assert all(outputs["result"].data == 7 for _, outputs in published)


@pytest.mark.parametrize("elapsed", [600, 601])
def test_exhausted_run_budget_does_not_call_adapter(rig, elapsed):
    clock = iter([0, 0, 0, elapsed]).__next__
    class Adapter:
        admissible = staticmethod(nodes.tool_block_admissible)

        def __call__(self, *args, **kwargs):
            pytest.fail("adapter called after run budget exhausted")

    published = []
    with pytest.raises(RunBudgetExceeded):
        evaluate_graph(rig.graph, rig.registry, tool_adapter=Adapter(), clock=clock,
                       on_block=lambda *args: published.append(args))
    assert not published


def test_failed(rig):
    rig.options["get_job"] = lambda job: {"status": "failed"}
    assert rig.call() == {"error": "lab_tool_failed"}


@pytest.mark.parametrize("data,error", [(7, None), ("wrong", "output-refused")])
def test_evaluator_schema(rig, data, error):
    rig.options["get_job"] = lambda job: {"status": "complete", "result": {"result": data}}
    result = evaluate_graph(rig.graph, rig.registry,
                            tool_adapter=nodes.ToolNodeAdapter(TENANT, "run", **rig.options))
    assert result["outputs"]["result"].error == error
    if error is None:
        assert result["outputs"]["result"].data == data


def test_remote_wait_keeps_output_budget(rig):
    clock = [0]
    def get(job):
        clock[0] += 10
        return {"status": "complete", "result": {"result": 12345}}
    rig.options["get_job"] = get
    adapter = nodes.ToolNodeAdapter(TENANT, "run", **rig.options)
    result = evaluate_graph(rig.graph, rig.registry, tool_adapter=adapter, clock=lambda: clock[0])
    assert result["outputs"]["result"].error is None
    result = evaluate_graph(rig.graph, rig.registry, tool_adapter=adapter,
                            budget=Budget(block_output_bytes=1))
    assert result["outputs"]["result"].error == "budget-exceeded"


def test_multiport_output(rig):
    manifest = {"outputs": {"first": {}, "second": {}}}
    assert rig.call(manifest=manifest) == {"error": "lab_tool_output_invalid"}
    rig.options["get_job"] = lambda job: {"status": "complete", "result": {"result": {"first": 7}}}
    assert rig.call(manifest=manifest) == {"outputs": {"first": 7, "second": None}}


def test_no_adapter_refuses_tool(rig):
    with pytest.raises(GraphRefused, match="lab_graph_effect_refused"):
        evaluate_graph(rig.graph, rig.registry)


@pytest.mark.parametrize("filename", ["lab_tool_nodes.py", "lab_worker.py", "lab_tool_manifests.py"])
def test_import_boundary(filename):
    tree = ast.parse((Path(__file__).parents[1] / filename).read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            assert all(alias.name.split(".")[0] != "tool_loader" for alias in node.names)
        if isinstance(node, ast.ImportFrom):
            assert (node.module or "").split(".")[0] != "tool_loader"
        if isinstance(node, ast.Call) and ("importlib" in ast.unparse(node.func)
                                           or "__import__" in ast.unparse(node.func)):
            assert "tool_loader" not in ast.unparse(node)


@pytest.mark.parametrize("count,tenant", [(10000, "11111111-1111-1111-1111-111111111111"),
                                         (0, "not-a-uuid")])
def test_default_ledger_fails_closed(rig, monkeypatch, count, tenant):
    import agent_pg_store
    from contextlib import nullcontext
    import sys
    platform = SimpleNamespace(tenant_cost_store=SimpleNamespace(list_cost_events=lambda *a, **k: [
        {"unit": "sandbox_seconds", "quantity": 0}] * count))
    monkeypatch.setattr(agent_pg_store, "_load_platform", lambda: (
        SimpleNamespace(transaction=lambda: nullcontext(object())), None))
    monkeypatch.setitem(sys.modules, "leaf_platform", platform)
    rig.options["sandbox_seconds_today"] = None
    adapter = nodes.ToolNodeAdapter(tenant, "run", **rig.options)
    assert adapter("node", rig.trusted, rig.manifest, {}, {"drawing": DRAWING}) == {
        "error": "lab_tool_budget_unreadable"}
    assert not rig.calls


def test_default_ledger_reads_tenant_utc_day(monkeypatch):
    import agent_pg_store
    from contextlib import nullcontext
    from datetime import timedelta
    from decimal import Decimal
    import sys

    calls = []
    conn = object()
    tenant = "11111111-1111-1111-1111-111111111111"
    midnight = NOW.replace(hour=0)
    def read(*args, **kwargs):
        calls.append((args, kwargs))
        return [{"unit": "sandbox_seconds", "quantity": Decimal("1.5")},
                {"unit": "sandbox_seconds", "quantity": Decimal("2.25")},
                {"unit": "other", "quantity": 100}]
    monkeypatch.setattr(agent_pg_store, "_load_platform", lambda: (
        SimpleNamespace(transaction=lambda: nullcontext(conn)), None))
    monkeypatch.setitem(sys.modules, "leaf_platform", SimpleNamespace(
        tenant_cost_store=SimpleNamespace(list_cost_events=read)))
    assert nodes._sandbox_seconds_today(tenant, midnight) == 3.75
    assert calls == [((conn, tenant, midnight, midnight + timedelta(days=1)),
                      {"addon": "e2b_exec", "limit": 10000})]


@pytest.mark.parametrize('failure,expected', [
    ({'corpus_failure': 'result_identity'}, 'lab_corpus_failed_result_identity'),
    ({'corpus_failure': 'lease_or_budget'}, 'lab_corpus_failed_lease_or_budget'),
    ({'corpus_failure': 'synthetic-private-label'}, 'lab_corpus_failed'),
    ({'message': 'synthetic-private-label'}, 'lab_corpus_failed'),
    (None, 'lab_corpus_failed'),
])
def test_corpus_failure_report_projects_only_fixed_categories(monkeypatch, failure, expected):
    import lab_corpus_executor as corpus
    authority = {'tenant_id': TENANT, 'project_id': 'project', 'actor_id': 'actor',
                 'doc_id': 'doc', 'graph_revision': 2, 'graph_sha256': 'a' * 64}
    adapter = nodes.ToolNodeAdapter(TENANT, 'retained-run', cancelled=lambda: False,
        corpus_authority=authority, corpus_owned=lambda: True)
    monkeypatch.setattr(corpus, 'validate_context', lambda value: value)
    monkeypatch.setattr(corpus, 'compile_policy', lambda *args: {})
    monkeypatch.setattr(corpus, 'submit_policy', lambda *args, **kwargs: 'retained-job')
    monkeypatch.setattr(jobs, 'get_job', lambda job_id: {
        'tenant_id': TENANT, 'status': 'failed', 'error': failure})
    output = adapter._corpus('corpus-evaluator-1',
        {'project_id': 'project', 'corpus': {}, 'suite': {}}, None)
    assert output == {'error': expected}
    assert 'synthetic-private-label' not in str(output)


def independent_corpus_rig(monkeypatch, labels=('slow', 'fast')):
    """Real graph admission and adapter, with inert native custody/job boundaries."""
    import lab_corpus_executor as corpus

    monkeypatch.setenv('LEAF_LAB_TOOL_NODES', '1')
    manifest, trusted = lab_tool_manifests.corpus_capability()
    authority = {'tenant_id': TENANT, 'project_id': OTHER_TENANT, 'actor_id': 'actor',
                 'doc_id': 'doc', 'graph_revision': 2, 'graph_sha256': 'a' * 64}
    material = {'material_id': TENANT, 'sha256': 'b' * 64, 'byte_size': 1}
    graph = {'schema': 'leaf.lab-graph.v1', 'blocks': [
        {'id': label, 'registry': {'id': 'corpus-evaluator', 'version': '1.0.0',
            'code_sha256': manifest['version']['code_sha256']},
         'settings': {'project_id': OTHER_TENANT, 'corpus': dict(material), 'suite': dict(material)}}
        for label in labels], 'pipes': [],
        'outputs': {label: {'node': label, 'port': 'result'} for label in labels}}
    registry = {'corpus-evaluator@1.0.0': manifest}
    rig = SimpleNamespace(graph=graph, registry=registry, trusted=trusted, authority=authority,
        now=0, owned=True, requested=False, trace=[], durable={}, creates=[], cancels=[],
        receipts={}, durations={label: (len(labels) - index) * 0.25 for index, label in enumerate(labels)})
    monkeypatch.setattr(lab_tool_manifests, 'trusted_for', lambda *args: trusted)
    monkeypatch.setattr(corpus, 'validate_context', lambda value: value)
    monkeypatch.setattr(corpus, 'compile_policy', lambda auth, block, settings:
                        {'digest': 'policy-' + block, 'block_id': block})

    def read(context):
        rig.trace.append(('custody', context['policy']['block_id']))
        return {'suite': b'{}'}

    def submit(context, *, idempotency_key):
        rig.trace.append(('submit', idempotency_key))
        if idempotency_key not in rig.durable:
            block = context['policy']['block_id']
            job_id = 'job-' + block
            value = {'context': context, 'job_id': job_id, 'worker_id': 'native', 'attempt': 1,
                'row_count': 1, 'rows_sha256': 'c' * 64, 'result_sha256': 'd' * 64,
                'evaluated_suite_sha256': 'e' * 64, 'result': {'verdict': 'pass', 'reasons': []}}
            rig.durable[idempotency_key] = {'tenant_id': TENANT, 'status': 'complete',
                'job_id': job_id, 'result': {'result': value}, 'ready_at': rig.now + rig.durations[block]}
            rig.creates.append(idempotency_key)
        return rig.durable[idempotency_key]['job_id']

    def get(job_id):
        rig.trace.append(('observe', job_id))
        row = next(row for row in rig.durable.values() if row['job_id'] == job_id)
        return dict(row, status='submitted') if rig.now < row['ready_at'] else row

    def cancel(block, job_id, deadline):
        rig.cancels.append((block, job_id, deadline))
        if not rig.owned:
            return None
        tag = {'block_id': block, 'fence': 1}
        rig.receipts[job_id] = {'cancel': tag, 'not_started': True}
        return tag

    def sleep(seconds):
        rig.now += seconds

    monkeypatch.setattr(corpus, 'read_sources', read)
    monkeypatch.setattr(corpus, 'compiled_suite', lambda *args: b'{}')
    monkeypatch.setattr(corpus, 'submit_policy', submit)
    monkeypatch.setattr(corpus, 'validate_result', lambda *args: None)
    monkeypatch.setattr(jobs, 'get_job', get)
    monkeypatch.setattr(jobs, 'corpus_cleanup', lambda job, tenant: rig.receipts.get(job))
    rig.options = dict(cancelled=lambda: rig.requested, corpus_authority=authority,
        corpus_owned=lambda: rig.owned, corpus_requested=lambda: rig.requested,
        corpus_cancel=cancel, monotonic=lambda: rig.now, sleep=sleep)
    rig.adapter = nodes.ToolNodeAdapter(TENANT, 'run', **rig.options)
    return rig


@pytest.mark.parametrize('labels', [('slow', 'fast'), ('z', 'm', 'a')])
def test_independent_corpus_admits_all_before_ordered_observation(monkeypatch, labels):
    import json
    rig = independent_corpus_rig(monkeypatch, labels)
    assert rig.adapter.prime_corpus(rig.graph, rig.registry, deadline=600)
    # Every material was checked before the first durable admission, with no waits.
    assert [kind for kind, _ in rig.trace] == ['custody'] * len(labels) + ['submit'] * len(labels)
    committed = []
    evaluate_graph(rig.graph, rig.registry, tool_adapter=rig.adapter,
        trusted_lookup=lambda *args: rig.trusted, clock=lambda: rig.now,
        on_block=lambda block, values: committed.append((block, values['result'].data)))
    assert [block for block, _ in committed] == list(labels)
    assert rig.creates == [f'lab:run:{block}:corpus:v1' for block in labels]
    assert sum(kind == 'submit' for kind, _ in rig.trace) == len(labels)
    assert rig.now == max(rig.durations.values())
    for block, encoded in committed:
        value = json.loads(encoded)
        assert value['block_id'] == block
        assert value['job_id'] == 'job-' + block
        assert value['run_id'] == 'run'
        assert value['graph_sha256'] == rig.authority['graph_sha256']
        assert value['policy_digest'] == 'policy-' + block
    assert rig.adapter.cleanup_corpus() is True
    assert not rig.cancels


@pytest.mark.parametrize('bad', ['project', 'settings', 'pin', 'registry', 'custody'])
def test_corpus_preflight_rejects_entire_graph_before_first_intent(monkeypatch, bad):
    import lab_corpus_executor as corpus
    rig = independent_corpus_rig(monkeypatch)
    second = rig.graph['blocks'][1]
    if bad == 'project':
        second['settings']['project_id'] = TENANT
    elif bad == 'settings':
        del second['settings']['suite']
    elif bad == 'pin':
        second['registry']['code_sha256'] = 'f' * 64
    elif bad == 'registry':
        monkeypatch.setattr(lab_tool_manifests, 'trusted_for', lambda *args: None)
    else:
        read = corpus.read_sources
        def refuse(context):
            if context['policy']['block_id'] == 'fast':
                raise ValueError('foreign material')
            return read(context)
        monkeypatch.setattr(corpus, 'read_sources', refuse)
    with pytest.raises((ValueError, GraphRefused)):
        rig.adapter.prime_corpus(rig.graph, rig.registry, deadline=600)
    assert not rig.creates


@pytest.mark.parametrize('shape', ['mixed', 'connected'])
def test_corpus_priming_leaves_other_graphs_on_serial_path(monkeypatch, shape):
    rig = independent_corpus_rig(monkeypatch)
    if shape == 'mixed':
        rig.graph['blocks'][1]['registry']['id'] = 'constant-number'
    else:
        rig.graph['pipes'] = [{'from': {'node': 'slow', 'port': 'result'},
                               'to': {'node': 'fast', 'port': 'input'}}]
    assert rig.adapter.prime_corpus(rig.graph, rig.registry, deadline=600) is False
    assert rig.trace == []


@pytest.mark.parametrize('stop', ['cancel', 'deadline', 'lease'])
def test_partial_priming_stops_before_next_submission(monkeypatch, stop):
    import lab_corpus_executor as corpus
    rig = independent_corpus_rig(monkeypatch)
    rig.durations['slow'] = 1000
    submit = corpus.submit_policy
    def first_only(*args, **kwargs):
        job = submit(*args, **kwargs)
        if stop == 'cancel':
            rig.requested = True
        elif stop == 'deadline':
            rig.now = 600
        else:
            rig.owned = False
        return job
    monkeypatch.setattr(corpus, 'submit_policy', first_only)
    with pytest.raises(RunBudgetExceeded if stop == 'deadline' else RunCancelled):
        rig.adapter.prime_corpus(rig.graph, rig.registry, deadline=600)
    assert rig.creates == ['lab:run:slow:corpus:v1']
    assert list(rig.adapter._corpus_jobs) == ['slow']
    assert rig.adapter.cleanup_corpus(deadline=stop == 'deadline') is (stop != 'lease')
    assert len(rig.cancels) == (0 if stop == 'lease' else 1)


@pytest.mark.parametrize('stop', ['cancel', 'deadline'])
def test_cleanup_covers_unobserved_intents_after_committed_value(monkeypatch, stop):
    rig = independent_corpus_rig(monkeypatch, ('a', 'b', 'c'))
    rig.durations.update(a=0, b=1000, c=1000)
    rig.adapter.prime_corpus(rig.graph, rig.registry, deadline=600)
    committed = []
    def publish(block, values):
        committed.append((block, values['result'].data))
        if stop == 'cancel':
            rig.requested = True
        else:
            rig.now = 600
    with pytest.raises(RunCancelled if stop == 'cancel' else RunBudgetExceeded):
        evaluate_graph(rig.graph, rig.registry, tool_adapter=rig.adapter,
            trusted_lookup=lambda *args: rig.trusted, clock=lambda: rig.now,
            cancelled=lambda: rig.requested, on_block=publish)
    retained = list(committed)
    assert rig.adapter.cleanup_corpus(deadline=stop == 'deadline')
    assert committed == retained and [block for block, _ in committed] == ['a']
    assert rig.cancels == [('b', 'job-b', stop == 'deadline'), ('c', 'job-c', stop == 'deadline')]
    assert rig.creates == [f'lab:run:{block}:corpus:v1' for block in ('a', 'b', 'c')]


def test_corpus_takeover_reuses_durable_intents_and_stale_owner_cannot_cancel(monkeypatch):
    rig = independent_corpus_rig(monkeypatch)
    old = rig.adapter
    old.prime_corpus(rig.graph, rig.registry, deadline=600)
    successor = nodes.ToolNodeAdapter(TENANT, 'run', **rig.options)
    successor.prime_corpus(rig.graph, rig.registry, deadline=600)
    assert successor._corpus_jobs == old._corpus_jobs
    assert len(rig.creates) == 2
    rig.owned = False
    with pytest.raises(RunCancelled):
        old._corpus('slow', rig.graph['blocks'][0]['settings'], 600)
    assert old.cleanup_corpus(deadline=True) is False
    assert rig.cancels == []
    rig.owned = True
    rig.now = 1
    before = successor._corpus('slow', rig.graph['blocks'][0]['settings'], 600)
    replay = nodes.ToolNodeAdapter(TENANT, 'run', **rig.options)
    replay.prime_corpus(rig.graph, rig.registry, deadline=600)
    assert replay._corpus('slow', rig.graph['blocks'][0]['settings'], 600) == before
    assert len(rig.creates) == 2


def test_corpus_cleanup_uses_one_receipt_budget_and_never_claims_confirmation(monkeypatch):
    rig = independent_corpus_rig(monkeypatch, ('a', 'b', 'c'))
    rig.durations = dict.fromkeys(('a', 'b', 'c'), 1000)
    rig.adapter.prime_corpus(rig.graph, rig.registry, deadline=600)
    monkeypatch.setattr(jobs, 'corpus_cleanup', lambda *args: None)
    assert rig.adapter.cleanup_corpus(deadline=True) is False
    assert rig.now == 10
    assert len(rig.cancels) == 3
    assert rig.adapter.cleanup_corpus(deadline=True) is False
    assert rig.now == 10 and len(rig.cancels) == 3


@pytest.mark.parametrize('observed', [False, True])
@pytest.mark.parametrize('receipt_state', ['absent', 'delayed', 'wrong_fence'])
def test_failed_corpus_with_existing_cancel_requires_matching_reap(monkeypatch, observed, receipt_state):
    rig = independent_corpus_rig(monkeypatch, ('failed',))
    rig.adapter.prime_corpus(rig.graph, rig.registry, deadline=600)
    row = rig.durable['lab:run:failed:corpus:v1']
    row.update(status='failed', ready_at=0, error={'corpus_failure': 'lease_or_budget'})
    tag = {'run_id': 'run', 'fence': 1, 'block_id': 'failed', 'attempt': 1, 'reason': 'cancel'}
    row['corpus_cancel_json'] = tag

    def existing_cancel(block, job, deadline):
        rig.cancels.append((block, job, deadline))
        return row['corpus_cancel_json']

    def receipt(job, tenant):
        assert (job, tenant) == ('job-failed', TENANT)
        if receipt_state == 'delayed' and rig.now >= 0.5:
            return {'cancel': tag, 'reaped': True}
        if receipt_state == 'wrong_fence':
            return {'cancel': dict(tag, fence=2), 'reaped': True}
        return None

    rig.adapter.corpus_cancel = existing_cancel
    monkeypatch.setattr(jobs, 'corpus_cleanup', receipt)
    if observed:
        assert rig.adapter._corpus('failed', rig.graph['blocks'][0]['settings'], 600) == {
            'error': 'lab_corpus_failed_lease_or_budget'}
    assert list(rig.adapter._corpus_jobs) == ['failed']
    assert rig.adapter.cleanup_corpus(deadline=True) is (receipt_state == 'delayed')
    assert rig.cancels == [('failed', 'job-failed', True)]
    assert rig.now == (0.5 if receipt_state == 'delayed' else 10)
    assert list(rig.adapter._corpus_jobs) == ([] if receipt_state == 'delayed' else ['failed'])
    assert row['corpus_cancel_json'] == tag


@pytest.mark.parametrize('race', ['cancel', 'terminal_read', 'receipt'])
def test_failed_corpus_cleanup_rechecks_ownership_on_races(monkeypatch, race):
    rig = independent_corpus_rig(monkeypatch, ('failed',))
    rig.adapter.prime_corpus(rig.graph, rig.registry, deadline=600)
    row = rig.durable['lab:run:failed:corpus:v1']
    row.update(status='failed', ready_at=0)
    tag = {'block_id': 'failed', 'fence': 1}

    def cancel(*args):
        if race == 'cancel':
            rig.owned = False
        return None if race != 'receipt' else tag

    reads = []
    def get(job):
        reads.append(job)
        if race == 'terminal_read' and len(reads) == 2:
            rig.owned = False
        return row

    def receipt(*args):
        rig.owned = False
        return {'cancel': tag, 'reaped': True}

    rig.adapter.corpus_cancel = cancel
    monkeypatch.setattr(jobs, 'get_job', get)
    monkeypatch.setattr(jobs, 'corpus_cleanup', receipt)
    assert rig.adapter.cleanup_corpus(deadline=True) is False
    assert list(rig.adapter._corpus_jobs) == ['failed']
    assert rig.now == 0
