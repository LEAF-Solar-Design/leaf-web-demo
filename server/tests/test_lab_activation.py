"""Activation lifecycle, re-admission and atomic scheduling on both authorities."""
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import datetime, timedelta, timezone
import json
from types import SimpleNamespace

import pytest

import lab_activation as activation
import lab_artifacts
import lab_registry
import lab_run_store as runs
from test_lab_run_store import authority  # noqa: F401 -- SQLite/PostgreSQL twin


@pytest.fixture
def rig(authority, monkeypatch):
    now = [datetime(2026, 10, 25, 12, tzinfo=timezone.utc)]
    graph = {"schema": "leaf.lab-graph.v1", "blocks": [{
        "id": "node", "registry": {"id": "constant", "version": "1.0.0", "code_sha256": "a" * 64},
        "settings": {"value": 7}}], "pipes": [], "outputs": {"answer": {"node": "node", "port": "result"}}}
    head = {"published_revision": 2}
    artifact = {"graph": graph, "sha256": "a" * 64}
    trusted = lab_registry.TrustedBlock("constant", "1.0.0", "pure", "none", "pure", None, "inline", False, "production")
    trusted_holder = [trusted]
    monkeypatch.setattr(lab_registry, "lookup", lambda *args: trusted_holder[0])
    monkeypatch.setattr(lab_artifacts, "get", lambda tenant, doc, revision=None: head if revision is None else artifact)
    monkeypatch.setattr(runs, "_now", lambda: now[0])
    monkeypatch.setenv("LEAF_AUTH_LIVE", "1")
    monkeypatch.setenv("LEAF_LAB_TOOL_NODES", "1")
    monkeypatch.setattr(activation.deps, "resolve_active_platform_tenant_authority", lambda subject: (authority, "hosted_pro"))
    monkeypatch.setattr(activation.entitlements, "entitlements_for", lambda tier: {"deploy": True})
    prior_pause = runs.paused()
    runs.set_paused(False, "test", "test")
    def create(interval=60, principal="subject-private"):
        return activation.create(authority, "doc", interval, principal, "subject-private")
    def advance(seconds=60):
        now[0] += timedelta(seconds=seconds)
    result = SimpleNamespace(tenant=authority, now=now, graph=graph, head=head, artifact=artifact,
                             trusted=trusted_holder, create=create, advance=advance,
                             row=lambda: activation.list(authority)[0])
    yield result
    activation._run(authority, lambda sql: sql.execute(
        "DELETE FROM lab_activation_transitions WHERE tenant_id=?", (authority,)), write=True)
    activation._run(authority, lambda sql: sql.execute(
        "DELETE FROM lab_activations WHERE tenant_id=?", (authority,)), write=True)
    runs.set_paused(prior_pause, "", "test")


def test_unpublished_refused(rig):
    rig.head["published_revision"] = None
    with pytest.raises(activation.Refused, match="lab_activation_unpublished"):
        rig.create()
    assert activation.list(rig.tenant) == []


@pytest.mark.parametrize("change", [None, {"effect": "compute", "execution_class": "worker"},
                                  {"residency": "host"}, {"effect": "refused"}, {"aps_live": True}])
def test_effect_refused_names_block(rig, change):
    rig.trusted[0] = replace(rig.trusted[0], **change) if change else None
    with pytest.raises(activation.Refused) as error:
        rig.create()
    assert (error.value.code, error.value.block) == ("lab_activation_effect_refused", "node")


@pytest.mark.parametrize("effect", ["read", "simulate"])
@pytest.mark.parametrize("enabled", [True, False])
def test_tools_require_flag(rig, monkeypatch, effect, enabled):
    rig.trusted[0] = replace(rig.trusted[0], effect=effect, capability=effect + ".value", residency="tenant")
    monkeypatch.setenv("LEAF_LAB_TOOL_NODES", "1" if enabled else "0")
    if enabled:
        assert rig.create()["state"] == "active"
    else:
        with pytest.raises(activation.Refused, match="lab_activation_tools_disabled"):
            rig.create()


@pytest.mark.parametrize("enabled", [True, False])
def test_projected_compute_creation_requires_flag(rig, monkeypatch, enabled):
    from test_lab_tool_nodes import projected_graph
    projected = projected_graph(monkeypatch)
    rig.artifact["graph"] = projected.graph
    # A tenant projection must take precedence even over an unrelated static entry.
    trusted_for = activation.lab_tool_manifests.trusted_for
    tenants = []
    def lookup(tenant, block_id, version):
        tenants.append(tenant)
        assert tenant == str(rig.tenant)
        assert (block_id, version) == (projected.descriptor.name, projected.descriptor.version)
        return trusted_for(tenant, block_id, version)
    monkeypatch.setattr(activation.lab_tool_manifests, "trusted_for", lookup)
    if enabled:
        assert rig.create()["state"] == "active"
    else:
        monkeypatch.delenv("LEAF_LAB_TOOL_NODES", raising=False)
        with pytest.raises(activation.Refused) as error:
            rig.create()
        assert (error.value.code, error.value.block) == ("lab_activation_tools_disabled", "node")
    assert tenants == [str(rig.tenant)] * (2 if enabled else 1)


@pytest.mark.parametrize("change", [{"capability": "drawing.write"}, {"residency": "host"},
                                  {"capability": "solve.child"}, {"capability": "cloud-proposal"},
                                  {"aps_live": True}])
def test_projected_tool_creation_refuses_unsafe_authority(rig, monkeypatch, change):
    from test_lab_tool_nodes import projected_graph
    projected = projected_graph(monkeypatch)
    rig.artifact["graph"] = projected.graph
    tenants = []
    def lookup(tenant, block_id, version):
        tenants.append(tenant)
        assert (block_id, version) == (projected.descriptor.name, projected.descriptor.version)
        return replace(projected.trusted, **change)
    monkeypatch.setattr(activation.lab_tool_manifests, "trusted_for", lookup)
    with pytest.raises(activation.Refused) as error:
        rig.create()
    assert (error.value.code, error.value.block) == ("lab_activation_effect_refused", "node")
    assert tenants == [str(rig.tenant)]


@pytest.mark.parametrize("change,expected", [
    ({}, None), ({"disabled": True}, "lab_activation_tools_disabled"),
    ({"capability": "drawing.write"}, "lab_activation_effect_refused"),
    ({"residency": "host"}, "lab_activation_effect_refused")])
def test_projected_tool_readmission(rig, monkeypatch, change, expected):
    from test_lab_tool_nodes import projected_graph
    projected = projected_graph(monkeypatch)
    rig.artifact["graph"] = projected.graph
    row = rig.create()
    row["principal"] = "tenant:local"
    if change.get("disabled"):
        monkeypatch.delenv("LEAF_LAB_TOOL_NODES", raising=False)
    else:
        def lookup(tenant, block_id, version):
            assert tenant == str(row["tenant_id"])
            assert (block_id, version) == (projected.descriptor.name, projected.descriptor.version)
            return replace(projected.trusted, **change)
        monkeypatch.setattr(activation.lab_tool_manifests, "trusted_for", lookup)
    assert activation.readmit(row) == expected


@pytest.mark.parametrize("interval", [59, 86401, 0, True, 60.0, "60"])
def test_interval_refused(rig, interval):
    with pytest.raises(activation.Refused, match="invalid_lab_activation_interval"):
        rig.create(interval)


def test_ten_activation_limit_includes_paused_and_expired(rig):
    rows = [rig.create() for _ in range(10)]
    activation.pause(rig.tenant, rows[0]["activation_id"], "actor")
    rig.advance(86401)
    activation.tick(rig.now[0])
    with pytest.raises(activation.Refused, match="lab_activation_limit"):
        rig.create()
    activation.off(rig.tenant, rows[1]["activation_id"], "actor")
    assert rig.create()["state"] == "active"


def test_create_pins_publication_not_unsaved_head(rig):
    row = rig.create(86400)
    assert row["revision"] == 2
    assert row["graph_sha256"] == rig.artifact["sha256"]
    assert row["registry_digest"] == runs.registry_digest()
    assert row["ops_revision"] == activation._ops_revision()
    assert row["input_digest"] == activation.graph_digest({})
    assert runs._date(row["lease_expires_at"]) == rig.now[0] + timedelta(hours=24)
    assert row["transitions"][0]["from_state"] is None


@pytest.mark.parametrize("reason", ["binding_changed", "binding_unavailable", "entitlement_revoked",
                                    "policy_unavailable", "publication_changed", "sha_changed",
                                    "registry_changed", "ops_changed", "input_changed"])
def test_readmission_stops_with_transition(rig, monkeypatch, reason):
    rig.create()
    def fail(*args):
        raise RuntimeError("unavailable")
    def policy(*args):
        raise activation.entitlements.EntitlementsError("unavailable")
    if reason == "binding_changed":
        monkeypatch.setattr(activation.deps, "resolve_active_platform_tenant_authority", lambda subject: ("other", "hosted_pro"))
    elif reason == "binding_unavailable":
        monkeypatch.setattr(activation.deps, "resolve_active_platform_tenant_authority", fail)
    elif reason == "entitlement_revoked":
        monkeypatch.setattr(activation.entitlements, "entitlements_for", lambda tier: {})
    elif reason == "policy_unavailable":
        monkeypatch.setattr(activation.entitlements, "entitlements_for", policy)
    elif reason == "publication_changed":
        rig.head["published_revision"] = 3
    elif reason == "sha_changed":
        rig.artifact["sha256"] = "b" * 64
    elif reason == "registry_changed":
        monkeypatch.setattr(runs, "registry_digest", lambda: "b" * 64)
    elif reason == "ops_changed":
        monkeypatch.setattr(activation, "_ops_revision", lambda: "ops-next")
    else:
        monkeypatch.setattr(activation, "graph_digest", lambda graph: "changed")
    activation.tick(rig.now[0])
    expected = "publication_changed" if reason == "sha_changed" else reason
    assert (rig.row()["state"], rig.row()["reason"]) == ("stopped", expected)
    assert rig.row()["transitions"][0]["reason"] == expected
    assert rig.row()["in_flight_run_id"] is None


@pytest.mark.parametrize("auth_live", [False, True])
def test_tenant_principal_skips_binding(rig, monkeypatch, auth_live):
    monkeypatch.setenv("LEAF_AUTH_LIVE", "1" if auth_live else "0")
    def forbidden(*args):
        pytest.fail("binding lookup must be skipped")
    monkeypatch.setattr(activation.deps, "resolve_active_platform_tenant_authority", forbidden)
    assert rig.create(principal="tenant:local")["state"] == "active"


@pytest.mark.parametrize("inflight", [False, True])
def test_subject_activation_stops_on_first_tick_after_auth_off(rig, monkeypatch, inflight):
    rig.create()
    if inflight:
        activation.tick(rig.now[0])
        rig.advance()
    monkeypatch.setenv("LEAF_AUTH_LIVE", "0")
    activation.tick(rig.now[0])
    row = rig.row()
    assert (row["state"], row["reason"]) == ("stopped", "auth_mode_changed")
    assert row["transitions"][0]["reason"] == "auth_mode_changed"
    if inflight:
        assert runs.get(rig.tenant, row["in_flight_run_id"])["state"] == "cancelled"
    else:
        assert row["in_flight_run_id"] is None


def test_tenant_principal_keeps_scheduling_with_auth_off(rig, monkeypatch):
    monkeypatch.setenv("LEAF_AUTH_LIVE", "0")
    rig.create(principal="tenant:local")
    activation.tick(rig.now[0])
    first = rig.row()["in_flight_run_id"]
    assert first
    finish(rig, first, 7)
    rig.advance()
    activation.tick(rig.now[0])
    assert rig.row()["state"] == "active"
    assert rig.row()["in_flight_run_id"] not in (None, first)


def test_expiry_requires_explicit_renewal(rig):
    row = rig.create()
    rig.advance(86400)
    activation.tick(rig.now[0])
    assert rig.row()["state"] == "expired"
    with pytest.raises(activation.Refused, match="lab_activation_state_conflict"):
        activation.resume(rig.tenant, row["activation_id"], "actor")
    activation.tick(rig.now[0])
    assert rig.row()["state"] == "expired"
    row = activation.renew(rig.tenant, row["activation_id"], "actor")
    assert row["state"] == "active"
    assert runs._date(row["lease_expires_at"]) == rig.now[0] + timedelta(hours=24)


def test_controls_and_expired_paused_resume(rig):
    aid = rig.create()["activation_id"]
    assert activation.pause(rig.tenant, aid, "operator")["state"] == "paused"
    assert activation.resume(rig.tenant, aid, "operator")["state"] == "active"
    activation.pause(rig.tenant, aid, "operator")
    rig.advance(86401)
    with pytest.raises(activation.Refused):
        activation.resume(rig.tenant, aid, "operator")
    assert rig.row()["state"] == "expired"
    assert activation.renew(rig.tenant, aid, "operator")["state"] == "active"
    assert activation.off(rig.tenant, aid, "operator")["state"] == "off"
    actors = activation._run(rig.tenant, lambda sql: [row["actor"] for row in sql.execute(
        "SELECT actor FROM lab_activation_transitions WHERE tenant_id=? AND seq>1", (rig.tenant,)).fetchall()])
    assert actors and set(actors) == {"operator"}


@pytest.mark.parametrize("state,action", [("expired", "resume"), ("off", "resume"), ("stopped", "resume"),
                                         ("off", "renew"), ("stopped", "renew"), ("paused", "pause"),
                                         ("stopped", "pause"), ("off", "off")])
def test_state_conflicts(rig, state, action):
    aid = rig.create()["activation_id"]
    activation._run(rig.tenant, lambda sql: sql.execute(
        "UPDATE lab_activations SET state=? WHERE tenant_id=?", (state, rig.tenant)), write=True)
    with pytest.raises(activation.Refused, match="lab_activation_state_conflict"):
        getattr(activation, action)(rig.tenant, aid, "actor")


@pytest.mark.parametrize("action", ["resume", "renew"])
def test_control_readmits(rig, monkeypatch, action):
    aid = rig.create()["activation_id"]
    activation.pause(rig.tenant, aid, "actor")
    monkeypatch.setattr(activation.entitlements, "entitlements_for", lambda tier: {})
    row = getattr(activation, action)(rig.tenant, aid, "actor")
    assert (row["state"], row["reason"]) == ("stopped", "entitlement_revoked")


def finish(rig, run_id, value):
    lease = runs.claim(rig.tenant, run_id, "worker", 30, build_revision="build", color="active")
    assert lease
    assert runs.append_events(rig.tenant, run_id, "worker", lease["fence"], [
        {"kind": "value", "block_id": "node", "payload": {"result": {"data": value}}},
        {"kind": "value", "block_id": "private", "payload": {"secret": "not-output"}}])
    assert runs.finish(rig.tenant, run_id, "worker", lease["fence"])


def test_one_in_flight_coalesces_and_harvest_replaces(rig):
    rig.create()
    activation.tick(rig.now[0])
    first = rig.row()["in_flight_run_id"]
    row = runs.get(rig.tenant, first)
    assert row["adapter_revision"] == "lab-activation.v1"
    for _ in range(3):
        rig.advance()
        activation.tick(rig.now[0])
        assert rig.row()["in_flight_run_id"] == first
    assert rig.row()["coalesced_ticks"] == 3
    finish(rig, first, 7)
    rig.advance()
    activation.tick(rig.now[0])
    row = rig.row()
    assert row["last_output"] == {"answer": {"data": 7}}
    assert row["last_summary"]["run_id"] == first
    assert row["last_summary"]["coalesced_ticks"] == 3
    assert row["coalesced_ticks"] == 0
    second = row["in_flight_run_id"]
    assert second != first
    finish(rig, second, 8)
    rig.advance()
    activation.tick(rig.now[0])
    assert rig.row()["last_output"] == {"answer": {"data": 8}}
    assert rig.row()["last_summary"]["run_id"] == second


def test_output_retention_is_bounded(rig):
    rig.create()
    activation.tick(rig.now[0])
    finish(rig, rig.row()["in_flight_run_id"], "x" * 65536)
    rig.advance()
    activation.tick(rig.now[0])
    assert rig.row()["last_output"] == {"truncated": True}


def test_inflight_is_readmitted_each_due_tick(rig, monkeypatch):
    rig.create()
    activation.tick(rig.now[0])
    rig.advance()
    monkeypatch.setattr(activation.entitlements, "entitlements_for", lambda tier: {})
    activation.tick(rig.now[0])
    assert rig.row()["reason"] == "entitlement_revoked"
    assert rig.row()["state"] == "stopped"


def test_global_pause_skips_everything(rig):
    before = rig.create()
    runs.set_paused(True, "maintenance", "actor")
    rig.advance(86401)
    activation.tick(rig.now[0])
    assert rig.row() == before


def test_transition_retention_and_private_projection(rig):
    aid = rig.create()["activation_id"]
    for _ in range(101):
        activation.pause(rig.tenant, aid, "subject-private")
        activation.resume(rig.tenant, aid, "subject-private")
    rows = activation._run(rig.tenant, lambda sql: sql.execute(
        "SELECT seq FROM lab_activation_transitions WHERE tenant_id=? ORDER BY seq", (rig.tenant,)).fetchall())
    assert len(rows) == 200 and rows[-1]["seq"] == 203
    row = rig.row()
    assert len(row["transitions"]) == 20
    assert row["principal_bound"] is True
    assert "subject-private" not in json.dumps(row, default=str)
    assert "principal" not in row and "created_by" not in row


def test_tenant_isolation(rig):
    aid = rig.create()["activation_id"]
    assert activation.list(rig.tenant + "-other") == []
    for action in (activation.pause, activation.resume, activation.renew, activation.off):
        with pytest.raises(lab_artifacts.NotFound):
            action(rig.tenant + "-other", aid, "actor")
    assert rig.row()["state"] == "active"


def test_concurrent_ticks_enqueue_once(rig):
    rig.create()
    with ThreadPoolExecutor(max_workers=2) as pool:
        assert len([future.result() for future in [pool.submit(activation.tick, rig.now[0]) for _ in range(2)]]) == 2
    count = runs._run(rig.tenant, lambda sql: sql.execute(
        "SELECT COUNT(*) AS count FROM lab_runs WHERE tenant_id=?", (rig.tenant,)).fetchone()["count"])
    assert count == 1


def test_queue_full_coalesces(rig):
    rig.create()
    for index in range(16):
        runs.create(rig.tenant, "doc", "r" + str(index), 2, "a" * 64, activation._ops_revision(),
                    activation.graph_digest({}), "lab-worker.v1", "build")
    activation.tick(rig.now[0])
    assert rig.row()["in_flight_run_id"] is None
    assert rig.row()["coalesced_ticks"] == 1
    assert runs._date(rig.row()["next_due_at"]) == rig.now[0] + timedelta(seconds=60)


@pytest.mark.parametrize("elevated", [False, True])
def test_K1_role_only_deploy_is_refused_at_create(rig, monkeypatch, elevated):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from routers import lab_activation as router
    from routers.lab_artifacts import _admit
    class Tenant(str):
        tier = "restricted"
        roles = ("platform_admin",)
        subject = "subject-private"
    tenant = Tenant(rig.tenant)
    tenant.elevated = elevated
    monkeypatch.setattr(activation.entitlements, "entitlements_for",
                        lambda tier, roles=(), elevated=False: {"deploy": tier != "restricted" or bool(roles) or elevated})
    monkeypatch.setattr(activation.deps, "resolve_active_platform_tenant_authority", lambda subject: (rig.tenant, "restricted"))
    _admit(tenant, "deploy")  # Ordinary deployment admits this verified role.
    app = FastAPI()
    app.include_router(router.router)
    app.dependency_overrides[activation.deps.require_tenant] = lambda: tenant
    with TestClient(app) as client:
        response = client.post("/api/lab/activations", json={"doc_id": "doc", "interval_s": 60})
    assert response.status_code == 403
    assert response.json()["detail"] == "lab_activation_entitlement_required"
    with pytest.raises(activation.Refused) as error:
        rig.create()
    assert (error.value.status, error.value.code) == (403, "lab_activation_entitlement_required")
    assert activation.list(rig.tenant) == []


def test_K2_failed_ticks_are_counted_and_success_resets(rig, monkeypatch):
    rig.create()
    original = lab_artifacts.get
    unavailable = [True]
    def get(*args):
        if unavailable[0]:
            raise RuntimeError("private store failure")
        return original(*args)
    monkeypatch.setattr(lab_artifacts, "get", get)
    for count in (1, 2):
        activation.tick(rig.now[0])
        row = rig.row()
        assert row["state"] == "active" and row["failure_count"] == count
        assert row["in_flight_run_id"] is None
        rig.advance()
    unavailable[0] = False
    activation.tick(rig.now[0])
    assert rig.row()["failure_count"] == 0
    run_id = rig.row()["in_flight_run_id"]
    assert run_id
    unavailable[0] = True
    for count in (1, 2, 3):
        rig.advance()
        activation.tick(rig.now[0])
        assert rig.row()["failure_count"] == count
        assert rig.row()["in_flight_run_id"] == run_id
    assert (rig.row()["state"], rig.row()["reason"]) == ("stopped", "readmit_unavailable")
    assert runs.get(rig.tenant, run_id)["state"] == "cancelled"


@pytest.mark.parametrize("exit", ["pause", "off", "expired", "revoked"])
def test_K3_leaving_active_cancels_the_queued_run(rig, monkeypatch, exit):
    aid = rig.create()["activation_id"]
    activation.tick(rig.now[0])
    run_id = rig.row()["in_flight_run_id"]
    cancel = runs.request_cancel
    calls = []
    def request_cancel(tenant, held_run, actor):
        # Reading through another transaction proves the transition committed
        # and its tenant lock was released before the public cancel API runs.
        assert activation.list(tenant)[0]["state"] != "active"
        calls.append((tenant, held_run, actor))
        return cancel(tenant, held_run, actor)
    monkeypatch.setattr(runs, "request_cancel", request_cancel)
    if exit in {"pause", "off"}:
        getattr(activation, exit)(rig.tenant, aid, "operator")
    else:
        rig.advance(86400 if exit == "expired" else 60)
        if exit == "revoked":
            monkeypatch.setattr(activation.entitlements, "entitlements_for", lambda tier: {})
        activation.tick(rig.now[0])
    run = runs.get(rig.tenant, run_id)
    assert run["state"] == "cancelled"
    assert run["cancel_requested_by"] == "activation"
    assert not activation.holds_run(rig.tenant, run_id)
    assert calls == [(rig.tenant, run_id, "activation")]


@pytest.mark.parametrize("exit", ["pause", "off", "expired", "revoked", "failed_ticks"])
def test_K3_cancel_failure_keeps_committed_transition_and_logs_class_only(rig, monkeypatch, caplog, exit):
    aid = rig.create()["activation_id"]
    activation.tick(rig.now[0])
    run_id = rig.row()["in_flight_run_id"]
    calls = []
    def unavailable(tenant, held_run, actor):
        assert activation.list(tenant)[0]["state"] != "active"
        calls.append((tenant, held_run, actor))
        raise RuntimeError("private cancellation details")
    monkeypatch.setattr(runs, "request_cancel", unavailable)
    if exit in {"pause", "off"}:
        getattr(activation, exit)(rig.tenant, aid, "operator")
    else:
        if exit == "revoked":
            monkeypatch.setattr(activation.entitlements, "entitlements_for", lambda tier: {})
        elif exit == "failed_ticks":
            def fail(*args):
                raise RuntimeError("private artifact details")
            monkeypatch.setattr(lab_artifacts, "get", fail)
        for _ in range(3 if exit == "failed_ticks" else 1):
            rig.advance(86400 if exit == "expired" else 60)
            activation.tick(rig.now[0])
    assert calls == [(rig.tenant, run_id, "activation")]
    assert not activation.holds_run(rig.tenant, run_id)
    assert runs.get(rig.tenant, run_id)["state"] == "queued"
    assert "lab_activation_cancel_failed class=RuntimeError" in caplog.text
    assert "private" not in caplog.text


@pytest.mark.parametrize("exit", ["pause", "off", "expired"])
def test_K3_rolled_back_transition_does_not_request_cancellation(rig, monkeypatch, exit):
    aid = rig.create()["activation_id"]
    activation.tick(rig.now[0])
    run_id = rig.row()["in_flight_run_id"]
    transition = activation._transition
    def fail_after_transition(*args, **kwargs):
        transition(*args, **kwargs)
        raise RuntimeError("transition rolled back")
    monkeypatch.setattr(activation, "_transition", fail_after_transition)
    calls = []
    monkeypatch.setattr(runs, "request_cancel", lambda *args: calls.append(args))
    if exit == "expired":
        rig.advance(86400)
        activation.tick(rig.now[0])
        assert rig.row()["failure_count"] == 1
    else:
        with pytest.raises(RuntimeError, match="transition rolled back"):
            getattr(activation, exit)(rig.tenant, aid, "operator")
    assert calls == []
    assert activation.holds_run(rig.tenant, run_id)
    assert runs.get(rig.tenant, run_id)["state"] == "queued"


@pytest.mark.parametrize("phase", ["readmit", "harvest"])
def test_F1_one_tenants_failure_does_not_abort_another(rig, monkeypatch, phase):
    rig.create()
    if phase == "harvest":
        activation.tick(rig.now[0])
        finish(rig, rig.row()["in_flight_run_id"], 7)
        rig.advance()
    other = rig.tenant + "-other"
    second = activation.create(other, "doc", 60, "tenant:other", "actor")
    activation._run(rig.tenant, lambda sql: sql.execute(
        "UPDATE lab_activations SET next_due_at=? WHERE tenant_id=?",
        ((rig.now[0] - timedelta(seconds=1)).isoformat(), rig.tenant)), write=True)
    original = lab_artifacts.get
    def get(tenant, *args):
        if tenant == rig.tenant:
            raise RuntimeError("unavailable")
        return original(tenant, *args)
    monkeypatch.setattr(lab_artifacts, "get", get)
    try:
        activation.tick(rig.now[0])
        assert rig.row()["failure_count"] == 1
        row = activation.list(other)[0]
        assert row["activation_id"] == second["activation_id"]
        assert row["in_flight_run_id"] and row["failure_count"] == 0
    finally:
        def cleanup(sql):
            sql.execute("DELETE FROM lab_activation_transitions WHERE tenant_id=?", (other,))
            sql.execute("DELETE FROM lab_activations WHERE tenant_id=?", (other,))
        activation._run(other, cleanup, write=True)


def test_F1_missing_pinned_artifact_records_unavailable_and_stops(rig, monkeypatch):
    rig.create()
    activation.tick(rig.now[0])
    finish(rig, rig.row()["in_flight_run_id"], 7)
    original = lab_artifacts.get
    def get(tenant, doc, revision=None):
        if revision is not None:
            raise lab_artifacts.NotFound("missing")
        return original(tenant, doc)
    monkeypatch.setattr(lab_artifacts, "get", get)
    rig.advance()
    activation.tick(rig.now[0])
    row = rig.row()
    assert (row["state"], row["reason"]) == ("stopped", "publication_changed")
    assert row["last_summary"]["harvest"] == "unavailable"
    assert row["in_flight_run_id"] is None


@pytest.mark.parametrize("action", ["resume", "renew"])
def test_F2_resume_and_renew_respect_last_start_interval(rig, action):
    aid = rig.create()["activation_id"]
    started = rig.now[0]
    activation.tick(started)
    first = rig.row()["in_flight_run_id"]
    finish(rig, first, 7)
    activation.pause(rig.tenant, aid, "actor")
    rig.advance(1)
    row = getattr(activation, action)(rig.tenant, aid, "actor")
    assert runs._date(row["last_started_at"]) == started
    assert runs._date(row["next_due_at"]) == started + timedelta(seconds=60)
    activation.tick(rig.now[0])
    assert rig.row()["in_flight_run_id"] == first
    rig.advance(59)
    activation.tick(rig.now[0])
    assert rig.row()["in_flight_run_id"] != first


def test_F3_list_caps_terminal_rows_and_batches_transitions(rig, monkeypatch):
    seed = rig.create()["activation_id"]
    def populate(sql):
        template = activation._row(sql, rig.tenant, seed)
        for index in range(25):
            row = dict(template, activation_id="terminal-" + str(index), state="off",
                       created_at=(rig.now[0] - timedelta(seconds=index + 1)).isoformat())
            columns = tuple(row)
            sql.execute("INSERT INTO lab_activations (" + ",".join(columns) + ") VALUES ("
                        + ",".join("?" for _ in columns) + ")", tuple(row.values()))
            for seq in range(1, 26):
                sql.execute("INSERT INTO lab_activation_transitions"
                            " (tenant_id,activation_id,seq,to_state,actor,at) VALUES (?,?,?,?,?,?)",
                            (rig.tenant, row["activation_id"], seq, "off", "actor", rig.now[0].isoformat()))
    activation._run(rig.tenant, populate, write=True)
    queries = []
    original = runs._SQL.execute
    def execute(sql, query, *args):
        if query.startswith("SELECT") and "lab_activation" in query:
            queries.append(query)
        return original(sql, query, *args)
    monkeypatch.setattr(runs._SQL, "execute", execute)
    rows = activation.list(rig.tenant)
    assert len(queries) == 2
    assert len(rows) == 21
    assert {row["activation_id"] for row in rows if row["state"] == "off"} == {"terminal-" + str(i) for i in range(20)}
    assert all([item["seq"] for item in row["transitions"]] == list(range(25, 5, -1))
               for row in rows if row["state"] == "off")
    activation.pause(rig.tenant, seed, "actor")
    queries.clear()
    assert len(activation.list(rig.tenant)) == 21
    assert len(queries) == 2


def test_F3_purge_removes_at_most_one_hundred_old_terminal_rows(rig):
    seed = rig.create()["activation_id"]
    old = (rig.now[0] - timedelta(days=8)).isoformat()
    def populate(sql):
        template = activation._row(sql, rig.tenant, seed)
        for index in range(102):
            row = dict(template, activation_id="old-" + str(index), state="stopped" if index % 2 else "off",
                       updated_at=old if index < 101 else rig.now[0].isoformat())
            columns = tuple(row)
            sql.execute("INSERT INTO lab_activations (" + ",".join(columns) + ") VALUES ("
                        + ",".join("?" for _ in columns) + ")", tuple(row.values()))
            sql.execute("INSERT INTO lab_activation_transitions"
                        " (tenant_id,activation_id,seq,to_state,actor,at) VALUES (?,?,?,?,?,?)",
                        (rig.tenant, row["activation_id"], 1, row["state"], "actor", old))
        sql.execute("UPDATE lab_activations SET state='paused',updated_at=? WHERE tenant_id=? AND activation_id=?",
                    (old, rig.tenant, seed))
    activation._run(rig.tenant, populate, write=True)
    activation.tick(rig.now[0])
    rows = activation.list(rig.tenant)
    assert len(rows) == 3
    assert any(row["activation_id"] == seed for row in rows)
    assert all(row["transitions"] for row in rows)
    activation.tick(rig.now[0])
    assert {row["activation_id"] for row in activation.list(rig.tenant)} == {seed, "old-101"}


def test_F4_harvest_never_decodes_nonoutputs_or_oversized_payloads(rig, monkeypatch):
    rig.create()
    activation.tick(rig.now[0])
    run_id = rig.row()["in_flight_run_id"]
    finish(rig, run_id, 7)
    huge = json.dumps({"result": {"data": "x" * (1024 * 1024)}})
    def populate(sql):
        runs._insert_event(sql, rig.tenant, run_id, 100, "private", "value", huge, rig.now[0].isoformat())
    activation._run(rig.tenant, populate, write=True)
    original = json.loads
    decoded = []
    def decode(payload, *args, **kwargs):
        if isinstance(payload, str):
            assert len(payload.encode("utf-8")) <= 65536
            decoded.append(payload)
        return original(payload, *args, **kwargs)
    monkeypatch.setattr(activation.json, "loads", decode)
    rig.advance()
    activation.tick(rig.now[0])
    assert rig.row()["last_output"] == {"answer": {"data": 7}}
    assert not any("not-output" in payload for payload in decoded)
    next_run = rig.row()["in_flight_run_id"]
    finish(rig, next_run, 8)
    activation._run(rig.tenant, lambda sql: runs._insert_event(
        sql, rig.tenant, next_run, 100, "node", "value", huge, rig.now[0].isoformat()), write=True)
    rig.advance()
    activation.tick(rig.now[0])
    assert rig.row()["last_output"] == {"truncated": True}


def test_F4_combined_output_budget_stops_before_later_values_are_decoded(rig, monkeypatch):
    for node in ("second", "third"):
        rig.graph["blocks"].append(dict(rig.graph["blocks"][0], id=node))
        rig.graph["outputs"][node] = {"node": node, "port": "result"}
    rig.create()
    activation.tick(rig.now[0])
    run_id = rig.row()["in_flight_run_id"]
    finish(rig, run_id, "x" * 40000)
    def populate(sql):
        for seq, node, value in ((100, "second", "y" * 40000), (101, "third", "must-not-decode")):
            runs._insert_event(sql, rig.tenant, run_id, seq, node, "value",
                               json.dumps({"result": {"data": value}}), rig.now[0].isoformat())
    activation._run(rig.tenant, populate, write=True)
    original = json.loads
    def decode(payload, *args, **kwargs):
        if isinstance(payload, str):
            assert "must-not-decode" not in payload
        return original(payload, *args, **kwargs)
    monkeypatch.setattr(activation.json, "loads", decode)
    rig.advance()
    activation.tick(rig.now[0])
    assert rig.row()["last_output"] == {"truncated": True}
