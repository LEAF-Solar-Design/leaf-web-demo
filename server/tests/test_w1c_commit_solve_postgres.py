"""Real PostgreSQL proposal-to-commit fencing, staleness, tamper and reclaim."""
from copy import deepcopy
import json
import os
from uuid import UUID, uuid4

import pytest

import leaf_cloud_client as cloud
import solar_project_graph as graph_adapter
import solar_project_jobs as service
import solar_project_solve as solve
from leaf_platform import canonical_jobs as jobs, project_solve_store as exchanges
from test_w1c_solve_proposal_postgres import (world, http, make_org, postgres_authority,  # noqa: F401
    case, graph, isolated_jobs, helpers, pg, row, expire, submit as submit_proposal)

pytestmark = pytest.mark.skipif(not os.environ.get("DATABASE_URL"),
    reason="PostgreSQL integration test requires DATABASE_URL")


def proposal(s, http):
    identity = submit_proposal(s, http)
    assert service.run_once(uuid4().hex, tool_name=solve.TOOL_NAME, lease_seconds=120)
    assert row(identity)["status"] == "succeeded", row(identity).get("error")
    return identity


def request(s, http, identity, *, key=None, cancel=False):
    http.principal["tenant"] = helpers.principal(s)
    body = {"tool": solve.COMMIT_TOOL, "dwg": str(s.parent),
        "params": {"expected_rev": helpers.context(s).graph["rev"]},
        "proposal_job_id": str(identity), "catalog_digest": pg.manifest(solve.COMMIT_TOOL)}
    if cancel:
        body["params"]["cancel"] = True
    return http.client.post("/api/run", json=body, headers=helpers.headers(s, key or str(uuid4())))


def submit(s, http, identity):
    response = request(s, http, identity)
    assert response.status_code == 202, response.text
    return UUID(response.json()["job_id"])


def assert_refusal(response, reason):
    assert response.status_code == 409, response.text
    assert response.json()["error"]["reason_code"] == reason


def assert_success(s, identity, *, versions):
    saved = row(identity)
    assert saved["status"] == "succeeded", saved.get("error")
    result = saved["result"]
    assert helpers.census(s)[:3] == (versions, 1, 1)
    assert pg.store.verify_history_operation(s.org, UUID(result["history_operation_id"]))
    assert pg.store._get_history_operation(s.org, UUID(result["history_operation_id"])).operation_type == "solar.graph.completed"
    prepared = pg.db.run_transaction(lambda conn: service.prepare_project_graph_job(saved, conn=conn))
    assert pg.db.run_transaction(lambda conn: graph_adapter.project_graph_job_provenance(
        identity, result["graph_commit"], expected=prepared, conn=conn))
    return saved, prepared


def offline(monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("canonical commit performed network, credential, or legacy proposal IO")
    monkeypatch.setattr(cloud, "post_stringer", forbidden)
    monkeypatch.setattr(cloud, "resolve_grant", forbidden)
    import solar_proposal_candidate
    monkeypatch.setattr(solar_proposal_candidate, "resolve_proposal", forbidden)


def test_postgres_commit_publishes_direct_builtin_graph_and_history(world, http, monkeypatch):
    s = world
    source = proposal(s, http)
    parent = helpers.context(s)
    exchange = pg.db.run_transaction(lambda conn: exchanges.read_completed_exchange(
        s.org, s.project, s.drawing, source, conn=conn))
    candidate = json.loads(exchange["candidate_bytes"])["candidate"]
    expected = graph_adapter.local._load_builtin(solve.COMMIT_TOOL).run(deepcopy(parent.graph),
        {"expected_rev": parent.graph["rev"]}, candidate=deepcopy(candidate))
    offline(monkeypatch)
    identity = submit(s, http, source)
    frozen = row(identity)["execution_context"]["trusted_proposal"]
    assert frozen == {"proposal_job_id": str(source), "proposal_tool_manifest_sha256": pg.manifest(solve.TOOL_NAME),
        **{key: exchange[key] for key in ("candidate_sha256", "request_sha256", "response_sha256")}}
    assert service.run_once(uuid4().hex, tool_name=solve.COMMIT_TOOL, lease_seconds=120)
    saved, prepared = assert_success(s, identity, versions=2)
    assert json.loads(prepared.output_intake_bytes)["solar_design_graph"] == expected
    s.parent = UUID(saved["result"]["output_version_id"])
    assert helpers.context(s).graph == expected
    assert pg.store_graph.resolve_version_binding(s.org, s.project, s.parent).is_head
    before = helpers.census(s)
    assert_refusal(request(s, http, source), "SOLVE_PROPOSAL_STALE")
    assert helpers.census(s) == before and len(s.calls) == 1


def test_postgres_intervening_edit_makes_proposal_stale(world, http):
    s = world
    source = proposal(s, http)
    response = helpers.request(http, s, "solar-settings", {
        "expected_rev": helpers.context(s).graph["rev"], "changes": {"num_mppt": 2}})
    assert response.status_code == 202, response.text
    edit = UUID(response.json()["job_id"])
    assert service.run_once(uuid4().hex, tool_name="solar-settings", lease_seconds=120)
    assert row(edit)["status"] == "succeeded", row(edit).get("error")
    s.parent = UUID(row(edit)["result"]["output_version_id"])
    before = helpers.census(s)
    with pg.db.cursor() as cur:
        cur.execute("SELECT count(*) AS n FROM jobs WHERE org_id=%s", (s.org,))
        count = cur.fetchone()["n"]
    assert_refusal(request(s, http, source), "SOLVE_PROPOSAL_STALE")
    with pg.db.cursor() as cur:
        cur.execute("SELECT count(*) AS n FROM jobs WHERE org_id=%s", (s.org,))
        assert cur.fetchone()["n"] == count
    assert helpers.census(s) == before


@pytest.mark.parametrize("damage", ["candidate_bytes", "delete"])
def test_postgres_exchange_tamper_before_execution_publishes_nothing(world, http, monkeypatch, damage):
    s = world
    source = proposal(s, http)
    identity = submit(s, http, source)
    before = helpers.census(s)
    # Simulate corruption below the immutable-store API; production callers cannot
    # update a completed exchange. Re-enable the trigger in the same transaction.
    def corrupt(conn):
        with conn.cursor() as cur:
            cur.execute("ALTER TABLE project_solve_exchanges DISABLE TRIGGER USER")
            try:
                if damage == "delete":
                    cur.execute("DELETE FROM project_solve_exchanges WHERE job_id=%s", (source,))
                else:
                    # A coherent replacement still cannot match the identity frozen at admission.
                    cur.execute("UPDATE project_solve_exchanges SET "
                                "candidate_sha256=encode(sha256(candidate_bytes || ' '::bytea),'hex'), "
                                "candidate_bytes=candidate_bytes || ' '::bytea "
                                "WHERE job_id=%s", (source,))
            finally:
                cur.execute("ALTER TABLE project_solve_exchanges ENABLE TRIGGER USER")
    pg.db.run_transaction(corrupt)
    offline(monkeypatch)
    assert service.run_once(uuid4().hex, tool_name=solve.COMMIT_TOOL, lease_seconds=120)
    saved = row(identity)
    assert saved["status"] == "failed", saved.get("error")
    assert saved["error"]["reason_code"] == "SOLVE_PROPOSAL_EVIDENCE_INVALID"
    assert saved["output_version_id"] is None and helpers.census(s) == before
    assert pg.store_graph.resolve_version_binding(s.org, s.project, s.parent).is_head


def test_postgres_crash_after_preparation_reclaims_and_publishes_once(world, http, monkeypatch):
    s = world
    source = proposal(s, http)
    identity = submit(s, http, source)
    offline(monkeypatch)
    claimed = jobs.claim_project_graph_job(uuid4().hex, tool_name=solve.COMMIT_TOOL, lease_seconds=120)
    assert UUID(str(claimed["job_id"])) == identity
    first = pg.db.run_transaction(lambda conn: service.prepare_project_graph_job(claimed, conn=conn))
    assert helpers.census(s)[:3] == (1, 0, 0)
    expire(identity)
    assert service.complete_project_graph_job(identity, claimed["lease_owner"], 1, first)[0] == "not_owner"
    owner = uuid4().hex
    assert service.run_once(owner, tool_name=solve.COMMIT_TOOL, lease_seconds=120)
    saved, prepared = assert_success(s, identity, versions=2)
    assert saved["attempt"] == 2 and first.output_intake_bytes == prepared.output_intake_bytes
    replay, result = service.complete_project_graph_job(identity, owner, 2, prepared)
    assert replay == "duplicate" and result == saved["result"]
    assert service.complete_project_graph_job(identity, owner, 1, first)[0] == "conflict"
    assert not service.run_once(uuid4().hex, tool_name=solve.COMMIT_TOOL, lease_seconds=120)
    assert helpers.census(s)[:3] == (2, 1, 1) and len(s.calls) == 1


def test_postgres_cancel_publishes_nothing(world, http, monkeypatch):
    s = world
    source = proposal(s, http)
    before = helpers.census(s)
    offline(monkeypatch)
    assert_refusal(request(s, http, source, cancel=True), "GRAPH_COMMIT_CANCELLED")
    assert helpers.census(s) == before
