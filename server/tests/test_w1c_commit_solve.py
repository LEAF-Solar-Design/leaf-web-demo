"""Canonical commit admission and offline candidate reconstruction, without publication IO."""
from copy import deepcopy
import json
from uuid import UUID, uuid4

import pytest

import deps
import leaf_cloud_client as cloud
import platform_link
import solar_project_graph as adapter
import solar_project_jobs as service
import solar_project_solve as solve
from leaf_platform import canonical_jobs as jobs, project_solve_store as exchanges
from solar_design_graph import GraphValidationError
from test_w1c_solve_proposal import (admitted, case, graph, graph_fixture,  # noqa: F401
                                     world as proposal_world)
import test_sip_r4_jobs as r4
import test_sip_r5_policy as policy
import test_sip_r5_routes as routes


class Connection:
    def __init__(self, world):
        self.world, self.selected = world, None

    def cursor(self):
        return self

    def __enter__(self):
        return self

    def __exit__(self, *args):
        pass

    def execute(self, sql, args):
        self.is_job = sql.startswith("SELECT * FROM jobs")
        if self.is_job:
            self.selected = self.world.rows.get(UUID(str(args[0])))
        else:
            self.selected = None
            self.world.s.execute(sql, args)

    def fetchone(self):
        return deepcopy(self.selected) if self.is_job else self.world.s.fetchone()


@pytest.fixture
def world(proposal_world, monkeypatch):
    a = proposal_world
    result = solve.prepare_job(a.row, a.guard)
    a.row.update(status="succeeded", provenance=result["execution_provenance"], lease_owner=None)
    # The commit must never depend on refetching the proposal's projected job result.
    a.row.pop("result")
    a.proposal_id = a.row["job_id"]
    a.rows = {UUID(a.proposal_id): a.row}
    a.conn = Connection(a)
    monkeypatch.setattr(platform_link.platform_db(), "run_transaction", lambda op: op(a.conn))
    monkeypatch.setattr(adapter, "_stored_job", lambda job, conn: deepcopy(a.rows[UUID(str(job))]))
    monkeypatch.setattr(jobs, "_graph_scope", lambda *args: None)
    a.accepted.clear()
    a.params = {"expected_rev": a.context.graph["rev"]}
    a.frozen = solve.freeze_proposal(a.context, a.proposal_id, conn=a.conn)
    def forbidden(*args, **kwargs):
        pytest.fail("commit accessed network, credentials, or a legacy proposal")
    monkeypatch.setattr(cloud, "post_stringer", forbidden)
    monkeypatch.setattr(cloud, "resolve_grant", forbidden)
    import solar_proposal_candidate
    monkeypatch.setattr(solar_proposal_candidate, "resolve_proposal", forbidden)
    return a


def request(a, **changes):
    return policy.request(a, a.params, tool_name=solve.COMMIT_TOOL,
        proposal_job_id=changes.pop("proposal_job_id", a.proposal_id), preview=False, **changes)


def refused(a, response, reason):
    assert response["status_code"] == 409, response
    assert response["content"]["error"]["reason_code"] == reason
    assert not a.accepted and not a.s.writes


@pytest.mark.parametrize("identity", [None, "", " ", "not-a-uuid", 3, {}, [], "ABCD0000-0000-4000-8000-000000000001"])
def test_required_proposal_id_before_insert(world, identity):
    refused(world, request(world, proposal_job_id=identity), "SOLVE_PROPOSAL_REQUIRED")


@pytest.mark.parametrize("scope", ["missing", "org_id", "project_id", "drawing_id", "tool_name", "deleted_at"])
def test_not_found_or_foreign_scope_before_insert(world, scope):
    if scope == "missing":
        world.rows.clear()
    elif scope == "drawing_id":
        world.row["execution_context"][scope] = str(uuid4())
    else:
        world.row[scope] = "solar-settings" if scope == "tool_name" else str(uuid4())
    refused(world, request(world), "SOLVE_PROPOSAL_NOT_FOUND")


@pytest.mark.parametrize("status", ["queued", "running", "failed", "cancelled"])
def test_unsucceeded_proposal_before_insert(world, status):
    world.row["status"] = status
    refused(world, request(world), "SOLVE_PROPOSAL_NOT_SUCCEEDED")


def test_stale_parent_before_insert(world):
    world.row["input_version_id"] = str(uuid4())
    refused(world, request(world), "SOLVE_PROPOSAL_STALE")


def test_proposal_manifest_drift_before_insert(world):
    world.row["execution_context"]["tool_manifest_sha256"] = "sha256:" + "0" * 64
    refused(world, request(world), "SOLVE_PROPOSAL_MANIFEST_MISMATCH")


def test_missing_exchange_before_insert(world, monkeypatch):
    def missing(*args, **kwargs):
        raise exchanges.ExchangeNotFound()
    monkeypatch.setattr(world.store, "read_completed_exchange", missing)
    refused(world, request(world), "SOLVE_PROPOSAL_EVIDENCE_INVALID")


@pytest.mark.parametrize("field", ["request_bytes", "request_sha256", "response_bytes", "response_sha256",
                                    "candidate_bytes", "candidate_sha256", "proof_bytes", "proof_sha256"])
def test_corrupt_exchange_before_insert(world, field):
    value = world.store.exchange[field]
    world.store.exchange[field] = value + b" " if type(value) is bytes else "0" * 64
    refused(world, request(world), "SOLVE_PROPOSAL_EVIDENCE_INVALID")


def test_happy_admission_freezes_exact_proposal_identity(world):
    a = world
    assert request(a)["status_code"] == 202
    bound = a.accepted[-1][-1]["execution_context"]
    assert set(bound["trusted_proposal"]) == {"proposal_job_id", "proposal_tool_manifest_sha256",
        "candidate_sha256", "request_sha256", "response_sha256"}
    assert bound["trusted_proposal"] == a.frozen
    assert bound["tool_manifest_sha256"] == deps.catalog_tool_digest(
        solve.solar_tools.trusted_record(solve.COMMIT_TOOL))
    assert bound["tool_manifest_sha256"] != a.frozen["proposal_tool_manifest_sha256"]
    assert "solve_binding" not in bound and not a.s.writes


def prepare(a):
    return adapter.prepare_project_graph_commit(a.context, solve.COMMIT_TOOL, a.params,
        checkout=a.s.lease, job_id=a.s.job, attempt=1,
        tool_manifest_sha256=deps.catalog_tool_digest(solve.solar_tools.trusted_record(solve.COMMIT_TOOL)),
        trusted_proposal=a.frozen, conn=a.conn)


def test_candidate_runs_through_generic_path_and_proves_offline(world):
    a = world
    prepared = prepare(a)
    candidate = json.loads(a.store.exchange["candidate_bytes"])["candidate"]
    expected = adapter.local._load_builtin(solve.COMMIT_TOOL).run(
        deepcopy(a.context.graph), a.params, candidate=deepcopy(candidate))
    assert json.loads(prepared.output_intake_bytes)["solar_design_graph"] == expected
    assert prepared.request["trusted_proposal"] == a.frozen
    a.rows[a.s.job] = r4.record(a.s, prepared)
    result = adapter.publish_project_graph_commit(prepared, actor_binding_id=a.s.actor, conn=a.conn)
    assert len(a.s.writes) == 1 and len(a.s.versions) == 2
    assert adapter.project_graph_job_provenance(a.s.job, result, expected=prepared, conn=a.conn)


@pytest.mark.parametrize("field", ["proposal_tool_manifest_sha256", "candidate_sha256",
                                    "request_sha256", "response_sha256"])
def test_worker_rechecks_all_frozen_digests(world, field):
    world.frozen[field] = ("sha256:" if field == "proposal_tool_manifest_sha256" else "") + "0" * 64
    with pytest.raises(GraphValidationError, match="SOLVE_PROPOSAL_EVIDENCE_INVALID"):
        prepare(world)
    assert not world.s.writes


def test_worker_rechecks_current_proposal_manifest(world):
    world.row["execution_context"]["tool_manifest_sha256"] = "sha256:" + "0" * 64
    with pytest.raises(GraphValidationError, match="SOLVE_PROPOSAL_MANIFEST_MISMATCH"):
        prepare(world)
    assert not world.s.writes


def test_publication_rechecks_custody_after_preparation(world):
    a = world
    prepared = prepare(a)
    a.rows[a.s.job] = r4.record(a.s, prepared)
    a.store.exchange["candidate_bytes"] += b" "
    with pytest.raises(adapter.project.ProjectContextError, match="SIP_R3_PROOF_REJECTED"):
        adapter.publish_project_graph_commit(prepared, actor_binding_id=a.s.actor, conn=a.conn)
    assert not a.s.writes and len(a.s.versions) == 1


def test_cancel_preserves_kernel_refusal(world):
    world.params["cancel"] = True
    refused(world, request(world), "GRAPH_COMMIT_CANCELLED")


def test_same_submission_key_different_proposal_conflicts(world, monkeypatch):
    assert request(world)["status_code"] == 202
    fingerprint = world.accepted[-1][-1]["submission_fingerprint"]
    saved = {"job_id": str(world.s.job), "deleted_at": None, "submission_fingerprint": fingerprint,
        "execution_context": {"schema": jobs.PROJECT_GRAPH_JOB_SCHEMA}}
    monkeypatch.setattr(jobs, "get_project_graph_job_by_key", lambda *args, **kwargs: saved)
    world.accepted.clear()
    monkeypatch.setattr(solve, "freeze_proposal", r4.forbidden)
    assert request(world)["status_code"] == 202
    refused(world, request(world, proposal_job_id=str(uuid4())), "SIP_R4_IDEMPOTENCY_CONFLICT")


def test_closed_context_is_conditional_and_typed(world):
    row = deepcopy(world.row)
    row.update(tool_name=solve.COMMIT_TOOL, params=world.params)
    row["execution_context"].pop("solve_binding")
    row["execution_context"]["trusted_proposal"] = deepcopy(world.frozen)
    jobs._graph_ids(row)
    bad_values = [None, {}, {**world.frozen, "extra": 1}]
    bad_values += [{**world.frozen, field: value} for field in world.frozen for value in (3, "bad")]
    for value in bad_values:
        broken = deepcopy(row)
        broken["execution_context"]["trusted_proposal"] = value
        with pytest.raises(adapter.project.ProjectContextError, match="SIP_R4_JOB_BINDING_MISMATCH"):
            jobs._graph_ids(broken)
    for tool in adapter.SUPPORTED_TOOLS - {solve.COMMIT_TOOL, solve.TOOL_NAME}:
        broken = deepcopy(row)
        broken["tool_name"] = tool
        with pytest.raises(adapter.project.ProjectContextError, match="SIP_R4_JOB_BINDING_MISMATCH"):
            jobs._graph_ids(broken)
        broken["execution_context"].pop("trusted_proposal")
        jobs._graph_ids(broken)
    row["execution_context"].pop("trusted_proposal")
    with pytest.raises(adapter.project.ProjectContextError, match="SIP_R4_JOB_BINDING_MISMATCH"):
        jobs._graph_ids(row)


def test_prepared_identity_must_match_frozen_job(world):
    prepared = prepare(world)
    row = r4.record(world.s, prepared)
    jobs._validate_graph_request(row, prepared.request)
    altered = prepared.request
    altered["trusted_proposal"]["candidate_sha256"] = "0" * 64
    with pytest.raises(adapter.project.ProjectContextError, match="SIP_R4_JOB_BINDING_MISMATCH"):
        jobs._validate_graph_request(row, altered)


def test_route_forwards_top_level_proposal_and_derives_drawing(monkeypatch):
    for lane in routes.lane.__wrapped__(monkeypatch):
        calls = []
        identity = str(uuid4())
        monkeypatch.setattr(service, "submit_project_graph_job", lambda *args, **kwargs:
            calls.append((args, kwargs)) or {"job_id": str(lane.s.job)})
        body = {"tool": solve.COMMIT_TOOL, "dwg": str(lane.s.parent.version_id),
            "params": {"expected_rev": 0}, "proposal_job_id": identity,
            "catalog_digest": deps.catalog_tool_digest(lane.tools[solve.COMMIT_TOOL])}
        response = lane.client.post("/api/run", json=body, headers=routes.headers(lane))
        assert response.status_code == 202, response.text
        assert calls[-1][0][2] == lane.s.drawing
        assert calls[-1][1]["proposal_job_id"] == identity
        assert calls[-1][0][5] == {"expected_rev": 0}
