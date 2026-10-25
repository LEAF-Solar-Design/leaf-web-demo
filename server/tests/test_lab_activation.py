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


@pytest.mark.parametrize("auth_live,principal", [(False, "subject-private"), (True, "tenant:local")])
def test_non_subject_and_auth_off_skip_binding(rig, monkeypatch, auth_live, principal):
    monkeypatch.setenv("LEAF_AUTH_LIVE", "1" if auth_live else "0")
    def forbidden(*args):
        pytest.fail("binding lookup must be skipped")
    monkeypatch.setattr(activation.deps, "resolve_active_platform_tenant_authority", forbidden)
    assert rig.create(principal=principal)["state"] == "active"


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
