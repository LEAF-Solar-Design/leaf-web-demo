"""Real PostgreSQL authority with an isolated immutable-content backend."""
from copy import deepcopy
from dataclasses import replace
from hashlib import sha256
import json
from pathlib import Path
import sys
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest
from psycopg.types.json import Jsonb

from leaf_platform import db, project_graph_store as graph, store

# Match the server's import convention while retaining the platform alias set
# by conftest. No root pytest process or fake database authority is involved.
# da/ goes in too, ahead of the cwd: the server's `import store` means
# da/store.py, and run from platform/ the cwd would otherwise hand it
# platform/store.py (test_drawing_import.py inserts both the same way).
_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_ROOT / "da"))
sys.path.insert(0, str(_ROOT / "server"))
import deps
import solar_local_graph as local
import solar_project_context as project
import solar_project_graph as adapter
import solar_tools

UNITS = {"drawing_units": "ft", "wcs_to_ucs": [1, 0, 0, 0, 0, 1, 0, 0,
          0, 0, 1, 0, 0, 0, 0, 1], "elevation_datum": "unknown", "crs": None}
INTAKE = {"dwg": {}, "layers": [], "polylines": [], "inserts": [], "faces3d": [],
          "blockdefs": [], "geodata": None, "custom": {"keep": [1, 2, 3]}}


class Blobs:
    def __init__(self):
        self.data, self.reads, self.writes = {}, [], []

    def get(self, key):
        self.reads.append(key)
        return self.data[key]

    def put_if_absent_or_verify(self, key, raw):
        self.writes.append(key)
        assert key not in self.data or self.data[key] == raw
        self.data[key] = raw


@pytest.fixture
def scope(make_org, monkeypatch):
    org = make_org("Solar canonical graph")
    proj = store.create_project(org.org_id, "Solar", authority_mode="postgres_canonical")
    actors = [store.create_identity_binding(org.org_id, "auth0", str(uuid4()), role="editor")
              for _ in range(2)]
    source = str(uuid4())
    key = f"tenants/{org.org_id}/drawings/{source}/v/00000001.intake.json"
    parent = store.create_drawing_version(org.org_id, proj.project_id,
                                        oss_object=key[:-12] + ".dwg", intake_ref=key)
    blobs = Blobs()
    raw = local.canonical_bytes(INTAKE)
    blobs.data[key] = raw
    provenance = {"schema": "leaf.drawing-import.v1", "source": {
        "kind": "account_upload", "tenant_id": str(org.org_id), "drawing_id": source,
        "version": 1, "stored_object": {"ref": parent.oss_object},
        "intake": {"ref": key, "sha256": sha256(raw).hexdigest()}}}
    with db.cursor() as cur:
        cur.execute("UPDATE drawing_versions SET provenance=%s WHERE org_id=%s AND version_id=%s",
                    (Jsonb(provenance), org.org_id, parent.version_id))
        for actor in actors:
            cur.execute("INSERT INTO project_member_bindings "
                "(membership_id, org_id, project_id, binding_id, role, invited_by_binding_id) "
                "VALUES (%s, %s, %s, %s, 'editor', %s)",
                (uuid4(), org.org_id, proj.project_id, actor.binding_id, actor.binding_id))
    monkeypatch.setattr(project.write_loop, "upload_backend_for_tenant", lambda tenant: blobs)
    # Only the authentication provider is isolated; project membership, scope,
    # intake verification, checkout rows, and publication are real R1/R2 code.
    monkeypatch.setattr(project.platform_link, "resolve_caller_binding", lambda tenant:
        SimpleNamespace(platform_tenant_id=org.org_id, binding_id=actors[0].binding_id))
    lease = db.run_transaction(lambda conn: graph.acquire_checkout(org.org_id, proj.project_id,
        parent.drawing_id, actor_binding_id=actors[0].binding_id, holder="Solar editor",
        ttl_s=60, expected_fence=None, conn=conn))
    return SimpleNamespace(org=org.org_id, project=proj.project_id, drawing=parent.drawing_id,
        parent=parent.version_id, actor=actors[0].binding_id, other=actors[1].binding_id,
        lease=lease, job=uuid4(), blobs=blobs)


def context(s, version=None):
    return project.resolve_context(str(s.org), s.project, version or s.parent,
                                   drawing_id=s.drawing, write=True)


def prepare(s, *, version=None, seed=True, lease=None, attempt=1):
    ctx = context(s, version)
    params = {"expected_rev": 0 if seed else 1,
              "changes": {"panels_in_sequence": 3} if seed else {"num_mppt": 2}}
    if seed:
        params["initialize"] = {"schema_version": 1, "source_intake_sha256": ctx.intake_sha256,
                                "units": deepcopy(UNITS)}
    args = dict(checkout=lease or s.lease, job_id=s.job, attempt=attempt,
                tool_manifest_sha256=deps.catalog_tool_digest(solar_tools.trusted_record("solar-settings")))
    if seed:
        return adapter.prepare_project_graph_seed(ctx, params, **args)
    return adapter.prepare_project_graph_commit(ctx, "solar-settings", params, **args)


def publish(s, prepared, *, conn=None, actor=None):
    operation = lambda c: adapter.publish_project_graph_commit(prepared,
        actor_binding_id=actor or s.actor, conn=c)
    return operation(conn) if conn is not None else db.run_transaction(operation)


def prove(s, result, prepared):
    return db.run_transaction(lambda conn: adapter.project_graph_commit_provenance(
        str(s.org), result, expected=prepared, conn=conn))


def count(s):
    with db.cursor() as cur:
        cur.execute("SELECT count(*) AS n FROM drawing_versions WHERE org_id=%s AND project_id=%s",
                    (s.org, s.project))
        return cur.fetchone()["n"]


def expire(s, monkeypatch):
    # The checkout trigger refuses rewriting a grant's interval in place, so the lease
    # expires by reading the database clock at its own expiry instant (R2's idiom).
    monkeypatch.setattr(graph, "_clock", lambda cur: s.lease.expires_at)


def refused(code, operation):
    with pytest.raises(project.ProjectContextError) as exc:
        operation()
    assert exc.value.reason_code == code


def test_sip_r3a_pg_seed_settings(scope):
    seeded = prepare(scope)
    first = publish(scope, seeded)
    settings = prepare(scope, version=UUID(first["output_version_id"]), seed=False)
    second = publish(scope, settings)
    assert first["after_rev"] == 1 and second["after_rev"] == 2
    assert first["graph_project_id"] == second["graph_project_id"] != str(scope.project)
    assert first["output_version_id"] != second["output_version_id"] != str(scope.parent)
    assert count(scope) == 3
    for prepared, result in ((seeded, first), (settings, second)):
        assert prove(scope, result, prepared)["output_version_id"] == result["output_version_id"]


def test_sip_r3a_pg_replay(scope, monkeypatch):
    seeded = prepare(scope)
    first = publish(scope, seeded)
    settings = prepare(scope, version=UUID(first["output_version_id"]), seed=False)
    second = publish(scope, settings)
    expire(scope, monkeypatch)
    before = count(scope), len(scope.blobs.writes), len(scope.blobs.reads)
    def forbidden(*args, **kwargs):
        pytest.fail("prepared replay repeated content IO or execution")
    with monkeypatch.context() as patch:
        patch.setattr(scope.blobs, "get", forbidden)
        patch.setattr(scope.blobs, "put_if_absent_or_verify", forbidden)
        patch.setattr(local, "_load_builtin", forbidden)
        assert publish(scope, seeded) == first
        assert publish(scope, settings) == second
    assert (count(scope), len(scope.blobs.writes), len(scope.blobs.reads)) == before
    assert "replayed" not in first
    assert prove(scope, first, seeded)["request_id"] == first["request_id"]


def test_sip_r3a_pg_stale_parent(scope):
    pending = prepare(scope)
    publish(scope, prepare(scope, attempt=2))
    refused("SIP_R1_STALE_VERSION", lambda: publish(scope, pending))
    assert count(scope) == 2


def test_sip_r3a_pg_historical_parent(scope):
    publish(scope, prepare(scope))
    assert context(scope).binding.is_head is False
    historical = prepare(scope, attempt=2)
    refused("SIP_R1_STALE_VERSION", lambda: publish(scope, historical))
    assert count(scope) == 2


def test_sip_r3a_pg_expired_fence(scope, monkeypatch):
    prepared = prepare(scope)
    expire(scope, monkeypatch)
    refused("SIP_R1_CHECKOUT_EXPIRED", lambda: publish(scope, prepared))
    assert count(scope) == 1


def test_sip_r3a_pg_foreign_fence(scope):
    foreign = prepare(scope, lease=replace(scope.lease, holder_binding_id=scope.other))
    refused("SIP_R1_CHECKOUT_DENIED", lambda: publish(scope, foreign, actor=scope.other))
    stale = prepare(scope)
    newer = db.run_transaction(lambda conn: graph.acquire_checkout(scope.org, scope.project, scope.drawing,
        actor_binding_id=scope.actor, holder="Solar editor", ttl_s=60,
        expected_fence=scope.lease.fence, conn=conn))
    assert newer.fence > scope.lease.fence
    refused("SIP_R1_CHECKOUT_STALE", lambda: publish(scope, stale))
    assert count(scope) == 1


def test_sip_r3a_pg_foreign_project(scope):
    refused("SIP_R1_CONTEXT_NOT_FOUND", lambda: project.resolve_context(
        str(scope.org), uuid4(), scope.parent, drawing_id=scope.drawing))
    refused("SIP_R1_CONTEXT_NOT_FOUND", lambda: project.resolve_context(
        str(scope.org), scope.project, scope.parent, drawing_id=uuid4()))
    refused("SIP_R1_CONTEXT_NOT_FOUND", lambda: project.resolve_context(
        str(scope.org), scope.project, uuid4(), drawing_id=scope.drawing))
    assert count(scope) == 1


def test_sip_r3a_pg_stored_proof_tampering(scope):
    prepared = prepare(scope)
    result = publish(scope, prepared)
    version_id = UUID(result["output_version_id"])
    version = store.get_drawing_version(scope.org, scope.project, version_id)
    original_bytes = scope.blobs.data[version.intake_ref]
    original_provenance = deepcopy(version.provenance)
    intake = json.loads(scope.blobs.data[version.intake_ref])
    intake["custom"]["keep"].append(99)
    raw = local.canonical_bytes(intake)
    digest = sha256(raw).hexdigest()
    key = graph.publication_intake_key(scope.org, scope.project, scope.drawing, digest)
    scope.blobs.data[key] = raw
    provenance = deepcopy(version.provenance)
    provenance["intake"] = {"ref": key, "sha256": digest}
    with db.cursor() as cur:
        cur.execute("UPDATE drawing_versions SET intake_ref=%s, provenance=%s, import_fingerprint=%s "
                    "WHERE org_id=%s AND version_id=%s",
                    (key, Jsonb(provenance), graph._publication_fingerprint(provenance), scope.org, version_id))
    refused("SIP_R3_PROOF_REJECTED", lambda: prove(scope, result, prepared))
    scope.blobs.data[key] = raw + b" "
    refused("SIP_R1_INTAKE_DIGEST_MISMATCH", lambda: prove(scope, result, prepared))
    version = store.get_drawing_version(scope.org, scope.project, version_id)
    intake = json.loads(original_bytes)
    intake["solar_design_graph"]["settings"]["num_mppt"] = 9
    intake["solar_design_graph_sha256"] = local.digest(intake["solar_design_graph"])
    raw = local.canonical_bytes(intake)
    digest = sha256(raw).hexdigest()
    key = graph.publication_intake_key(scope.org, scope.project, scope.drawing, digest)
    scope.blobs.data[key] = raw
    provenance = deepcopy(original_provenance)
    provenance["intake"] = {"ref": key, "sha256": digest}
    with db.cursor() as cur:
        cur.execute("UPDATE drawing_versions SET intake_ref=%s, provenance=%s, import_fingerprint=%s "
                    "WHERE org_id=%s AND version_id=%s",
                    (key, Jsonb(provenance), graph._publication_fingerprint(provenance), scope.org, version_id))
    forged = deepcopy(result)
    forged["graph_sha256"] = local.digest(intake["solar_design_graph"])
    forged["output_intake_sha256"] = digest
    refused("SIP_R3_PROOF_REJECTED", lambda: prove(scope, forged, prepared))
    assert count(scope) == 2


def test_sip_r3a_pg_replay_conflict(scope, monkeypatch):
    prepared = prepare(scope)
    result = publish(scope, prepared)
    version = store.get_drawing_version(scope.org, scope.project, UUID(result["output_version_id"]))
    provenance = deepcopy(version.provenance)
    provenance["checkout_fence"] = str(scope.lease.fence + 1)
    with db.cursor() as cur:
        cur.execute("UPDATE drawing_versions SET provenance=%s WHERE org_id=%s AND version_id=%s",
                    (Jsonb(provenance), scope.org, version.version_id))
    def forbidden(*args, **kwargs):
        pytest.fail("conflicting replay read a blob")
    monkeypatch.setattr(scope.blobs, "get", forbidden)
    refused("SIP_R2_IDEMPOTENCY_CONFLICT", lambda: publish(scope, prepared))
    assert count(scope) == 2


def test_sip_r3a_pg_rollback(scope):
    prepared = prepare(scope)
    def abort(conn):
        result = publish(scope, prepared, conn=conn)
        assert UUID(result["output_version_id"]) != scope.parent
        raise RuntimeError("abort after canonical graph publication")
    with pytest.raises(RuntimeError, match="abort after canonical graph publication"):
        db.run_transaction(abort)
    assert count(scope) == 1 and len(scope.blobs.writes) == 1
    # The immutable blob may survive; the version row and request key do not.
    result = publish(scope, prepared)
    assert count(scope) == 2
    assert prove(scope, result, prepared)["output_version_id"] == result["output_version_id"]
