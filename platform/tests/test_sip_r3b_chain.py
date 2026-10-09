"""Deterministic creation with real checkout, publication and stored-content authority."""
from copy import deepcopy
from hashlib import sha256
import json
import os
from uuid import UUID, uuid4

import pytest
from psycopg.types.json import Jsonb
from leaf_platform import db, project_graph_store as graph_store, store
from test_sip_r3a_graph import scope, context, publish, prove, count, expire, refused  # noqa: F401
from test_sip_r3b_tools import (ROOT, panels_parent, prepare, output, commit, forbidden,
                                local, adapter, MANUAL_SHA)
from solar_design_graph import GraphValidationError

pytestmark = pytest.mark.skipif(
    not (os.environ.get("DATABASE_URL") or (ROOT / "platform/.env.local").exists()),
    reason="PostgreSQL integration test requires DATABASE_URL")

STRING = "solar-string-add"
EQUIPMENT = "solar-assign-equipment"
CONFIG = {
    "id": "leaf:inverter:00000000-0000-4000-8000-000000000001",
    "number": 1, "type_key": "INV", "model": "fixture",
    "position": [5, 0, 0], "rotation": 0, "scale": [1, 1, 1],
    "block_name": "FixtureInverter", "layer": "0",
    "mppt_count": 1, "total_dc_inputs": 1, "mppt_inputs": {"A": 1},
    "max_dc_voltage": 1500, "max_ac_power_kw": 100,
    "max_dc_power_kw": 100, "is_solaredge": False,
}
UNKNOWN = ["EQUIPMENT_POWER_UNKNOWN", "EQUIPMENT_VOLTAGE_UNKNOWN"]


def manual(s):
    ctx = panels_parent(s)
    _, _, ctx = commit(s, "solar-panels-from-drawing", {"expected_rev": 2}, ctx)
    _, _, ctx = commit(s, "solar-size-strings",
        {"expected_rev": 3, "mode": "manual-global", "confirm": True}, ctx)
    assert ctx.graph_sha256 == MANUAL_SHA
    return ctx


def string_params(ctx):
    return {"expected_rev": 4, "operation": "add-string",
            "ordered_panel_refs": [panel["id"] for panel in ctx.graph["panels"]]}


def equipment_params(ctx):
    return {"expected_rev": 5, "equipment": [deepcopy(CONFIG)], "assignments": [
        {"string_ref": ctx.graph["strings"][0]["id"], "inverter_ref": CONFIG["id"],
         "mppt_letter": "A", "input_number": 0}]}


def through_string(s):
    ctx = manual(s)
    return commit(s, STRING, string_params(ctx), ctx)


def through_equipment(s):
    string = through_string(s)
    return string, commit(s, EQUIPMENT, equipment_params(string[2]), string[2])


def graph_bytes(prepared):
    return json.loads(prepared.output_intake_bytes)["solar_design_graph"]


def test_sip_r3b_chain_pg_string_preparation(scope):
    ctx = manual(scope)
    params, original = string_params(ctx), ctx.intake_bytes
    p = prepare(scope, STRING, params, ctx)
    assert prepare(scope, STRING, params, ctx) == p
    assert ctx.intake_bytes == original and params == string_params(ctx)
    string = graph_bytes(p)["strings"][0]
    assert string["id"] == adapter._creation_id(ctx, STRING, params, "string", 0)
    assert string["provenance"]["created_at"] == ctx.created_at
    before = count(scope)
    r = publish(scope, p)
    assert count(scope) == before + 1 and r["before_rev"] == 4 and r["after_rev"] == 5
    assert output(scope, r).graph == graph_bytes(p)
    assert prove(scope, r, p)["output_version_id"] == r["output_version_id"]


def test_sip_r3b_chain_pg_equipment_preparation(scope):
    _, _, ctx = through_string(scope)
    params = equipment_params(ctx)
    p = prepare(scope, EQUIPMENT, params, ctx)
    assert prepare(scope, EQUIPMENT, params, ctx) == p
    assert params == equipment_params(ctx)
    g = graph_bytes(p)
    inverter, string = g["inverters"][0], g["strings"][0]
    assert inverter["id"] == CONFIG["id"]
    assert inverter["provenance"]["created_at"] == ctx.created_at
    assert string["inverter_ref"] == string["to_ref"] == inverter["id"]
    assert inverter["input_assignments"] == [{"string_ref": string["id"],
        "mppt_letter": "A", "input_number": 0}]
    assert g["extra"]["equipment"]["assignment_requests"] == params["assignments"]
    blank = {"expected_rev": 5, "equipment": [{**CONFIG, "id": ""}], "assignments": []}
    derived = prepare(scope, EQUIPMENT, blank, ctx)
    assert prepare(scope, EQUIPMENT, blank, ctx) == derived
    assert graph_bytes(derived)["inverters"][0]["id"] == adapter._creation_id(
        ctx, EQUIPMENT, blank, "inverter", 0)
    before = count(scope)
    r = publish(scope, p)
    assert count(scope) == before + 1 and r["after_rev"] == 6
    assert output(scope, r).graph == g
    assert prove(scope, r, p)["output_version_id"] == r["output_version_id"]


def test_sip_r3b_chain_pg_scope_isolation(scope):
    first = manual(scope)
    # A second real drawing in the same project embeds identical graph and intake bytes.
    source = str(uuid4())
    key = f"tenants/{scope.org}/drawings/{source}/v/00000001.intake.json"
    parent = store.create_drawing_version(scope.org, scope.project,
        oss_object=key[:-12] + ".dwg", intake_ref=key)
    scope.blobs.data[key] = first.intake_bytes
    provenance = {"schema": "leaf.drawing-import.v1", "source": {
        "kind": "account_upload", "tenant_id": str(scope.org), "drawing_id": source,
        "version": 1, "stored_object": {"ref": parent.oss_object},
        "intake": {"ref": key, "sha256": first.intake_sha256}}}
    with db.cursor() as cur:
        cur.execute("UPDATE drawing_versions SET provenance=%s WHERE org_id=%s AND version_id=%s",
                    (Jsonb(provenance), scope.org, parent.version_id))
    lease = db.run_transaction(lambda conn: graph_store.acquire_checkout(
        scope.org, scope.project, parent.drawing_id, actor_binding_id=scope.actor,
        holder="Second Solar editor", ttl_s=60, expected_fence=None, conn=conn))
    second_scope = deepcopy(scope)
    second_scope.blobs = scope.blobs
    second_scope.drawing, second_scope.parent, second_scope.lease = (
        parent.drawing_id, parent.version_id, lease)
    second = adapter.resolve_project_graph_context(context(second_scope))
    assert first.graph == second.graph and first.intake_bytes == second.intake_bytes
    assert first.drawing_id != second.drawing_id
    params = string_params(first)
    candidates = [prepare(s, STRING, params, ctx) for s, ctx in
                  ((scope, first), (second_scope, second))]
    assert len({graph_bytes(p)["strings"][0]["id"] for p in candidates}) == 2
    for s, ctx, p in zip((scope, second_scope), (first, second), candidates):
        assert prepare(s, STRING, params, ctx) == p
        foreign_lease = second_scope.lease if s is scope else scope.lease
        refused("SIP_R3_PROOF_REJECTED", lambda: prepare(
            s, STRING, params, ctx, checkout=foreign_lease))
        r = publish(s, p)
        assert prove(s, r, p)["output_version_id"] == r["output_version_id"]
        other = candidates[1] if p is candidates[0] else candidates[0]
        refused("SIP_R3_PROOF_REJECTED", lambda: prove(s, r, other))


def test_sip_r3b_chain_pg_historical_replay(scope, monkeypatch):
    string, equipment = through_equipment(scope)
    ctx = equipment[2]
    commit(scope, "solar-settings", {"expected_rev": 6, "changes": {"num_mppt": 2}}, ctx)
    for _, r, _ in (string, equipment):
        assert context(scope, UUID(r["output_version_id"])).binding.is_head is False
    expire(scope, monkeypatch)
    before = count(scope), len(scope.blobs.reads), len(scope.blobs.writes)
    with monkeypatch.context() as patch:
        patch.setattr(scope.blobs, "get", forbidden)
        patch.setattr(scope.blobs, "put_if_absent_or_verify", forbidden)
        patch.setattr(local, "_load_builtin", forbidden)
        for p, r, _ in (string, equipment):
            assert publish(scope, p) == r
    assert (count(scope), len(scope.blobs.reads), len(scope.blobs.writes)) == before
    for p, r, _ in (string, equipment):
        assert prove(scope, r, p)["output_version_id"] == r["output_version_id"]


def test_sip_r3b_chain_pg_stored_content_tampering(scope):
    rows = through_equipment(scope)
    for (p, r, ctx), collection in zip(rows, ("strings", "inverters")):
        assert prove(scope, r, p)["output_version_id"] == r["output_version_id"]
        version_id = UUID(r["output_version_id"])
        version = store.get_drawing_version(scope.org, scope.project, version_id)
        for change in ("timestamp", "identity", "unrelated"):
            intake = ctx.intake
            entity = intake["solar_design_graph"][collection][0]
            if change == "timestamp":
                entity["provenance"]["created_at"] = "2026-02-03T04:05:06Z"
            elif change == "identity":
                ref = entity["id"]
                replacement = ref.rsplit(":", 1)[0] + ":00000000-0000-4000-8000-0000000000aa"
                intake = json.loads(json.dumps(intake).replace(ref, replacement))
            else:
                intake["custom"]["keep"].append(99)
            intake["solar_design_graph_sha256"] = local.digest(intake["solar_design_graph"])
            raw = local.canonical_bytes(intake)
            digest = sha256(raw).hexdigest()
            key = graph_store.publication_intake_key(scope.org, scope.project, scope.drawing, digest)
            scope.blobs.data[key] = raw
            provenance = deepcopy(version.provenance)
            provenance["intake"] = {"ref": key, "sha256": digest}
            with db.cursor() as cur:
                cur.execute("UPDATE drawing_versions SET intake_ref=%s, provenance=%s, import_fingerprint=%s "
                    "WHERE org_id=%s AND version_id=%s",
                    (key, Jsonb(provenance), graph_store._publication_fingerprint(provenance),
                     scope.org, version_id))
            forged = {**r, "graph_sha256": intake["solar_design_graph_sha256"],
                      "output_intake_sha256": digest}
            refused("SIP_R3_PROOF_REJECTED", lambda: prove(scope, r, p))
            refused("SIP_R3_PROOF_REJECTED", lambda: prove(scope, forged, p))
            with db.cursor() as cur:
                cur.execute("UPDATE drawing_versions SET intake_ref=%s, provenance=%s, import_fingerprint=%s "
                    "WHERE org_id=%s AND version_id=%s",
                    (version.intake_ref, Jsonb(version.provenance),
                     graph_store._publication_fingerprint(version.provenance), scope.org, version_id))
        assert prove(scope, r, p)["output_version_id"] == r["output_version_id"]


def test_sip_r3b_chain_pg_manual_power_boundary(scope):
    _, (_, r, ctx) = through_equipment(scope)
    g = ctx.graph
    assert r["after_rev"] == g["rev"] == 6
    assert tuple(len(g[name]) for name in ("panels", "strings", "inverters", "routes", "schedules")) == (
        3, 1, 1, 0, 0)
    for entity in (g["inverters"][0], g["strings"][0]):
        assert entity["validity"] == {"state": "invalid", "reasons": UNKNOWN}
    assert g["settings"]["extra"]["string_sizing"]["records"] == {}
    before = count(scope), len(scope.blobs.writes)
    for tool, params, code in (
        ("solar-homeruns", {"expected_rev": 6}, "EQUIPMENT_ASSIGNMENT_REQUIRED"),
        ("solar-schedule", {"expected_rev": 6, "insertion_point": [0, 0, 0]}, "ROUTING_TOPOLOGY_REQUIRED")):
        with pytest.raises(GraphValidationError) as exc:
            prepare(scope, tool, params, ctx)
        assert exc.value.code == code
    assert (count(scope), len(scope.blobs.writes)) == before
