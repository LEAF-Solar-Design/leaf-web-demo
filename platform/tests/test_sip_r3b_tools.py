"""Real R1 checkout and R2 publication for the six canonical Solar tools."""
from copy import deepcopy
from dataclasses import replace
from hashlib import sha256
import json
import os
from pathlib import Path
import sys
from uuid import UUID, uuid4

import pytest
from psycopg.types.json import Jsonb
from leaf_platform import db, project_graph_store as graph_store, store
from test_sip_r3a_graph import scope, context, publish, prove, count, expire, refused, INTAKE, UNITS  # noqa: F401

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "server/tests"))
import deps
import solar_local_graph as local
import solar_project_context as project
import solar_project_graph as adapter
import solar_tools
from solar_graph_seed import new_empty_graph
from test_w1_design_graph import graph  # noqa: F401
from test_w1_equipment import case, equipment, licensed  # noqa: F401

pytestmark = pytest.mark.skipif(
    not (os.environ.get("DATABASE_URL") or (ROOT / "platform/.env.local").exists()),
    reason="PostgreSQL integration test requires DATABASE_URL")

PANELS_SHA = "53ab57c100a67be2f30688763a6dda13af3a08d6ebbb1c48e33a16028df2d853"
MANUAL_SHA = "34b08719dbbe25e80459a6d705945d8ae4220c35772c78c8feeecafc248dcab1"
COMBINERS_SHA = "2bf76c5170333bfae0c78f48c958947b67c14223662b0db51806795257abaa3f"
FEEDERS_SHA = "3b65cdf69075dc2eb1beda469c785e930165ae7e8162511fcfff079be49aaa7b"


def embed(g, source):
    return {**deepcopy(source), "solar_design_graph": deepcopy(g),
            "solar_design_graph_sha256": local.digest(g)}


def set_parent(s, intake):
    version = store.get_drawing_version(s.org, s.project, s.parent)
    raw = local.canonical_bytes(intake)
    s.blobs.data[version.intake_ref] = raw
    provenance = deepcopy(version.provenance)
    provenance["source"]["intake"]["sha256"] = sha256(raw).hexdigest()
    with db.cursor() as cur:
        cur.execute("UPDATE drawing_versions SET provenance=%s WHERE org_id=%s AND version_id=%s",
                    (Jsonb(provenance), s.org, s.parent))
    return adapter.resolve_project_graph_context(context(s))


def panels_parent(s):
    source = deepcopy(INTAKE)
    source["polylines"] = [
        {"layer": "Panels", "closed": True, "handle": format(26 + i, "X"),
         "pts": [[100*i, 0], [100*i+77, 0], [100*i+77, 38.5], [100*i, 38.5]]}
        for i in range(3)]
    # Graph identity belongs to the embedded graph, independently of database scope.
    # The fixed seed reproduces the server chain's rev-2 import fixture exactly.
    g = new_empty_graph(tenant_id=str(UUID(int=1)), drawing_id=str(UUID(int=3)),
        source_hash=local.digest(source), units=deepcopy(UNITS), created_at="2026-01-01T00:00:00Z")
    settings = local._load_builtin("solar-settings")
    g = settings.run(g, {"expected_rev": 0, "changes": {"panels_in_sequence": 3}})
    g = settings.run(g, {"expected_rev": 1,
        "changes": {"num_mppt": 2, "panel_layer_contains": "Panel"}})
    assert local.digest(g) == "76637c9607c2e780a91070d2e928c234092a0ef09b185aeaffe2381f1d5ae489"
    return set_parent(s, embed(g, source))


def combiners_parent(s):
    import test_solar_tool_combiners as tc
    g, intake, groups = tc.i4()
    return set_parent(s, embed(g, {**deepcopy(INTAKE),
        "combiner_intake": intake, "panel_groups": groups})), tc.request(0)


def assigned_parent(s, case):
    g, params, intake = case
    assigned = equipment.assign_equipment(g, params, drawing_intake=intake,
                                          licensed_equipment=licensed)["graph"]
    return set_parent(s, embed(assigned, {**deepcopy(INTAKE), **intake}))


def prepare(s, tool, params, ctx, **changes):
    args = dict(checkout=s.lease, job_id=s.job, attempt=1,
                tool_manifest_sha256=deps.catalog_tool_digest(solar_tools.trusted_record(tool)))
    args.update(changes)
    return adapter.prepare_project_graph_commit(ctx, tool, params, **args)


def output(s, result):
    return adapter.resolve_project_graph_context(context(s, UUID(result["output_version_id"])))


def commit(s, tool, params, ctx):
    p = prepare(s, tool, params, ctx)
    r = publish(s, p)
    return p, r, output(s, r)


def direct(ctx, tool, params):
    trusted = {}
    if "source_intake" in solar_tools.get(tool)["trusted_inputs"]:
        trusted["source_intake"] = {k: v for k, v in ctx.intake.items()
            if k not in ("solar_design_graph", "solar_design_graph_sha256")}
    return local._load_builtin(tool).run(deepcopy(ctx.graph), deepcopy(params), **trusted)


def forbidden(*args, **kwargs):
    pytest.fail("historical replay repeated content IO or execution")


def test_sip_r3b_pg_panels_manual(scope):
    ctx = panels_parent(scope)
    p, r, ctx = commit(scope, "solar-panels-from-drawing", {"expected_rev": 2}, ctx)
    assert r["after_rev"] == 3 and ctx.graph_sha256 == PANELS_SHA
    assert prove(scope, r, p)["graph_sha256"] == PANELS_SHA
    p, r, ctx = commit(scope, "solar-size-strings",
        {"expected_rev": 3, "mode": "manual-global", "confirm": True}, ctx)
    assert r["after_rev"] == 4 and ctx.graph_sha256 == MANUAL_SHA
    assert prove(scope, r, p)["graph_sha256"] == MANUAL_SHA
    assert count(scope) == 3


def test_sip_r3b_pg_combiners_feeders(scope):
    ctx, params = combiners_parent(scope)
    expected = direct(ctx, "solar-combiners", params)
    p, r, ctx = commit(scope, "solar-combiners", params, ctx)
    assert r["after_rev"] == 1 and ctx.graph_sha256 == COMBINERS_SHA and ctx.graph == expected
    assert prove(scope, r, p)["graph_sha256"] == COMBINERS_SHA
    expected = direct(ctx, "solar-feeders", {"expected_rev": 1})
    p, r, ctx = commit(scope, "solar-feeders", {"expected_rev": 1}, ctx)
    assert r["after_rev"] == 2 and ctx.graph_sha256 == FEEDERS_SHA and ctx.graph == expected
    assert prove(scope, r, p)["graph_sha256"] == FEEDERS_SHA
    assert count(scope) == 3


def test_sip_r3b_pg_homeruns_schedule(scope, case):
    ctx = assigned_parent(scope, case)
    rev = ctx.graph["rev"]
    params = {"expected_rev": rev}
    expected = direct(ctx, "solar-homeruns", params)
    p, r, ctx = commit(scope, "solar-homeruns", params, ctx)
    assert r["after_rev"] == rev + 1 and ctx.graph == expected
    assert prove(scope, r, p)["graph_sha256"] == local.digest(expected)
    params = {"expected_rev": rev + 1, "insertion_point": [0, 0, 0]}
    expected = direct(ctx, "solar-schedule", params)
    p, r, ctx = commit(scope, "solar-schedule", params, ctx)
    assert r["after_rev"] == rev + 2 and ctx.graph == expected
    assert prove(scope, r, p)["graph_sha256"] == local.digest(expected)
    assert count(scope) == 3


def test_sip_r3b_pg_historical_replay(scope, monkeypatch):
    ctx = panels_parent(scope)
    p, r, ctx = commit(scope, "solar-panels-from-drawing", {"expected_rev": 2}, ctx)
    commit(scope, "solar-size-strings",
        {"expected_rev": 3, "mode": "manual-global", "confirm": True}, ctx)
    assert context(scope, UUID(r["output_version_id"])).binding.is_head is False
    expire(scope, monkeypatch)
    before = count(scope), len(scope.blobs.reads), len(scope.blobs.writes)
    with monkeypatch.context() as patch:
        patch.setattr(scope.blobs, "get", forbidden)
        patch.setattr(scope.blobs, "put_if_absent_or_verify", forbidden)
        patch.setattr(local, "_load_builtin", forbidden)
        assert publish(scope, p) == r
    assert (count(scope), len(scope.blobs.reads), len(scope.blobs.writes)) == before
    assert prove(scope, r, p)["output_version_id"] == r["output_version_id"]


def test_sip_r3b_pg_publication_admission(scope, monkeypatch):
    ctx = panels_parent(scope)
    tool, params = "solar-panels-from-drawing", {"expected_rev": 2}
    pending = prepare(scope, tool, params, ctx)
    with monkeypatch.context() as patch:
        expire(scope, patch)
        refused("SIP_R1_CHECKOUT_EXPIRED", lambda: publish(scope, pending))
    assert count(scope) == 1
    foreign = prepare(scope, tool, params, ctx,
                      checkout=replace(scope.lease, holder_binding_id=scope.other))
    refused("SIP_R1_CHECKOUT_DENIED", lambda: publish(scope, foreign, actor=scope.other))
    assert count(scope) == 1
    for proj, drawing, version in ((uuid4(), scope.drawing, scope.parent),
                                   (scope.project, uuid4(), scope.parent),
                                   (scope.project, scope.drawing, uuid4())):
        refused("SIP_R1_CONTEXT_NOT_FOUND", lambda: project.resolve_context(
            str(scope.org), proj, version, drawing_id=drawing))
    refused("SIP_R1_CONTEXT_NOT_FOUND", lambda: graph_store.resolve_version_binding(
        uuid4(), scope.project, scope.parent, drawing_id=scope.drawing))
    assert count(scope) == 1
    scope.lease = db.run_transaction(lambda conn: graph_store.acquire_checkout(
        scope.org, scope.project, scope.drawing, actor_binding_id=scope.actor,
        holder="Solar editor", ttl_s=60, expected_fence=scope.lease.fence, conn=conn))
    refused("SIP_R1_CHECKOUT_STALE", lambda: publish(scope, pending))
    assert count(scope) == 1
    stale = prepare(scope, tool, params, ctx)
    current = prepare(scope, tool, params, ctx, attempt=2)
    publish(scope, current)
    refused("SIP_R1_STALE_VERSION", lambda: publish(scope, stale))
    assert count(scope) == 2


def test_sip_r3b_pg_stored_proof(scope):
    ctx = panels_parent(scope)
    p, r, ctx = commit(scope, "solar-panels-from-drawing", {"expected_rev": 2}, ctx)
    p, r, ctx = commit(scope, "solar-size-strings",
        {"expected_rev": 3, "mode": "manual-global", "confirm": True}, ctx)
    assert prove(scope, r, p)["graph_sha256"] == MANUAL_SHA
    version_id = UUID(r["output_version_id"])
    version = store.get_drawing_version(scope.org, scope.project, version_id)
    intake = ctx.intake
    intake["solar_design_graph"]["settings"]["num_mppt"] = 9
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
    refused("SIP_R3_PROOF_REJECTED", lambda: prove(scope, r, p))
    forged = {**r, "graph_sha256": intake["solar_design_graph_sha256"],
              "output_intake_sha256": digest}
    refused("SIP_R3_PROOF_REJECTED", lambda: prove(scope, forged, p))
    assert count(scope) == 3


def test_sip_r3b_pg_rollback(scope):
    ctx = panels_parent(scope)
    p = prepare(scope, "solar-panels-from-drawing", {"expected_rev": 2}, ctx)
    def abort(conn):
        publish(scope, p, conn=conn)
        raise RuntimeError("abort canonical Solar publication")
    with pytest.raises(RuntimeError, match="abort canonical Solar publication"):
        db.run_transaction(abort)
    assert count(scope) == 1 and len(scope.blobs.writes) == 1
    result = publish(scope, p)
    assert count(scope) == 2
    assert prove(scope, result, p)["output_version_id"] == result["output_version_id"]
