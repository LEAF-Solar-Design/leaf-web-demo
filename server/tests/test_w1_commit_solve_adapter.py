"""CS2: durable proposal references accepted through the real local graph rail."""
import copy
import hashlib
import json
import sys
import uuid
from pathlib import Path

import pytest

SERVER = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SERVER))
sys.path.insert(0, str(SERVER / "tests"))

import broker_client
import deps
import entitlements
import jobs
import leaf_cloud_client as cloud
import product_capability_availability as availability
import solar_local_graph as local
import solar_proposal_candidate as proposals
import solar_solve_results as solve
import store
import write_loop
from solar_graph_context import resolve_graph_context
from solar_sizing_client import SIZING_URL, digest, sizing_basis, sizing_targets
from test_w1_bound_proposal import (  # noqa: F401
    _configure, graphless_api, body as proposal_body, execution, snapshot, unchanged,
)
from test_w1_design_graph import graph  # noqa: F401
from test_w1_solve_commit import case, seed, publish, commit  # noqa: F401
from test_w1_local_graph_jobs import isolated_jobs, no_network  # noqa: F401
from test_w1_local_graph_rail import _api, TENANT
from test_w1_graph_versions import (  # noqa: F401
    drawing, commit as bundle_commit, request_for, TENANT as BUNDLE_TENANT, DRAWING,
)
from test_jobs_callbacks_postgres import postgres_authority  # noqa: F401

TOOL = "solar-commit-solve"
ABSENT = object()


def test_commit_solve_contextual_availability_without_registry_declaration(monkeypatch):
    from test_w1_design_graph import _four_state_capabilities

    _four_state_capabilities(monkeypatch)
    name = "synthetic-adapter"
    assert availability.solar_tools.get(name) is None
    inputs = {"input_ready": True, "input_reason": None}
    expected = availability.w1_availability(name, entitled=True, inputs=inputs)
    assert expected["runnable"] is True
    assert availability.w1_contextual_availability(
        name, entitled=True, inputs=inputs,
    ) == expected


@pytest.fixture
def api(case, isolated_jobs, no_network, tmp_path, monkeypatch):
    graph, _, response = case
    graph.update(rev=1, parent_rev=0)
    target = sizing_targets(graph, "global")[graph["settings"]["id"]]
    sizing_request = {"schema_version": "leaf.string-length.v1", "module": {
        "model": "fixture-module", "voc": target["voc_cold"]["per_module"],
        "temp_coeff_pct_per_c": 0},
        "inverter": {"model": "fixture-inverter",
                     "max_dc_voltage": target["voc_cold"]["max_dc_voltage"]},
        "design_min_temp_c": 0, "panels_in_sequence": target["panels_in_sequence"],
        "units": "SI"}
    sizing_response = {"panels_in_sequence": target["panels_in_sequence"],
                       "voc_cold": copy.deepcopy(target["voc_cold"])}
    graph["settings"]["global_string_sizing_confirmed"] = True
    graph["settings"]["extra"]["string_sizing"] = {
        "mode": "global", "basis_sha256": sizing_basis(graph), "records": {
            graph["settings"]["id"]: {"endpoint": SIZING_URL, "adapter_version": "1.0.0",
                "request": sizing_request, "response": sizing_response,
                "request_sha256": digest(sizing_request),
                "response_sha256": digest(sizing_response)}}}
    assert availability.w1_graph_readiness(graph)[TOOL] == {
        "input_ready": True, "input_reason": None}
    backend, _ = seed(tmp_path, monkeypatch, graph)
    calls = []

    def outbound(*args):
        calls.append(args)
        return cloud.canonical_bytes(response)

    monkeypatch.setattr(cloud, "post_stringer", outbound)
    for rail in _api(backend, tmp_path, monkeypatch):
        _configure(rail, monkeypatch)
        yield (*rail, calls)


@pytest.fixture
def offline(monkeypatch):
    calls = []

    def fail(*args, **kwargs):
        calls.append((args, kwargs))
        pytest.fail("commit and replay must never call the cloud")

    def seal():
        monkeypatch.setattr(cloud, "post_stringer", fail)

    yield seal
    assert calls == []


def _proposal(api, case, *, bound=True):
    before = snapshot(api)
    response = api[0].post("/api/run?wait=1", json=proposal_body(api, case, bound=bound))
    assert response.status_code == 200, response.text
    result = response.json()["result"]
    job_id = result["proposal"]["job_id"] if bound else result["job_id"]
    assert jobs.get_job(job_id)["status"] == "complete"
    unchanged(api, before, 1)
    return job_id


@pytest.fixture
def ready(api, case, offline):
    job_id = _proposal(api, case)
    offline()
    return job_id


@pytest.fixture
def two(api, case, offline):
    ids = [_proposal(api, case), _proposal(api, case)]
    offline()
    return ids


def _body(api, reference=ABSENT, *, params=None):
    result = {"tool": TOOL, "dwg": "solar", "dwg_version": 1,
              "catalog_digest": deps.catalog_tool_digest(api[2][TOOL]),
              "params": {"expected_rev": 1} if params is None else copy.deepcopy(params)}
    if reference is not ABSENT:
        result["proposal_job_id"] = reference
    return result


def _refused(api, request, reason, *, status=409):
    before = snapshot(api)
    response = api[0].post("/api/run?wait=1", json=request)
    assert response.status_code == status, response.text
    env = response.json()
    if reason is None:
        assert "reason_code" not in env
        assert env["error"]["error_code"] == "BAD_PARAMS"
    else:
        assert env["reason_code"] == reason
    unchanged(api, before)
    return env


def _frozen(job_id):
    return proposals.resolve_proposal(TENANT, "solar", 1, job_id, {"expected_rev": 1})


def _save(job_id, record, context=None):
    jobs._exec(
        "UPDATE jobs SET tenant_id=?, tool=?, params_json=?, dwg=?, dwg_version=?, "
        "status=?, result_json=?, provenance_json=? WHERE job_id=?",
        (record["tenant_id"], record["tool"], json.dumps(record["params"]), record["dwg"],
         record["dwg_version"], record["status"], json.dumps(record["result"]),
         json.dumps(record["provenance"]), job_id))
    if context is not None:
        jobs._exec("UPDATE jobs SET execution_json=? WHERE job_id=?", (json.dumps(context), job_id))


def _commit(api, job_id):
    before = snapshot(api)
    response = api[0].post("/api/run?wait=1", json=_body(api, job_id))
    assert response.status_code == 200, response.text
    env = response.json()
    assert env["ok"] is True and "reason_code" not in env
    assert snapshot(api) == (before[0] + 1, 2)
    assert jobs.get_job(env["result"]["job_id"])["status"] == "complete"
    return env


def _delivery(api, job_id, frozen=None):
    record = jobs.get_job(job_id)
    context = execution(job_id)
    return api[4].BrokerRunRequest(
        tenant_id=TENANT, tool=context["tool"], params=record["params"], dwg=record["dwg"],
        dwg_version=context["dwg_version"], job_id=job_id, ledger_event_key=job_id + ":broker-run",
        checkout_holder=context["checkout_holder"], checkout_fence=context["checkout_fence"],
        proposal_candidate=context["proposal_candidate"] if frozen is None else frozen)


def _deliver(api, request):
    response = api[4]._broker_run(request)
    return response.status_code, json.loads(response.body)


def _advance(api):
    parent = store.load_manifest(api[1], TENANT, "solar")["head"]
    before = resolve_graph_context(api[1], TENANT, "solar", parent)["graph"]
    after = copy.deepcopy(before)
    after.update(rev=before["rev"] + 1, parent_rev=before["rev"])
    after["project"]["name"] = "Competing fixture publication"
    # This competing fixture must remain sized so proposal staleness is reached.
    after["settings"]["extra"]["string_sizing"]["basis_sha256"] = sizing_basis(after)
    holder, fence = api[3]._checkout_identity()
    publish(api[1], parent, before, after, holder=holder, fence=fence, acquire=False)


class _Queue:
    def __init__(self):
        self.calls = []

    def submit(self, fn, *args, **kwargs):
        self.calls.append((fn, args, kwargs))

    def execute(self):
        fn, args, kwargs = self.calls.pop(0)
        fn(*args, **kwargs)


def _queue(api, monkeypatch, reference, key="commit-key"):
    queue = _Queue()
    monkeypatch.setitem(jobs._executors, jobs.lane_for(api[2][TOOL], False), queue)
    before = snapshot(api)
    response = api[0].post("/api/run", json=_body(api, reference), headers={"Idempotency-Key": key})
    assert response.status_code == 202, response.text
    job_id = response.json()["job_id"]
    assert jobs.get_job(job_id)["status"] == "submitted"
    unchanged(api, before, 1)
    return queue, job_id


def _worker_refusal(api, reference, params, reason):
    before = snapshot(api)
    response = api[0].post("/api/run?wait=1", json=_body(api, reference, params=params))
    assert response.status_code == 400, response.text
    assert response.json()["error"]["reason_code"] == reason
    unchanged(api, before, 1)
    rows = [r for r in jobs.list_jobs(TENANT) if r["tool"] == TOOL]
    assert rows[0]["status"] == "failed"


def test_commit_solve_completed_proposal_publishes(api, ready, case, monkeypatch):
    frozen = _frozen(ready)
    original = copy.deepcopy(frozen)
    env = _commit(api, ready)
    result = env["result"]
    assert result["new_version"] == {"drawing_id": "solar", "version": 2, "parent": 1}
    assert (result["before_rev"], result["after_rev"]) == (1, 2)
    stored = resolve_graph_context(api[1], TENANT, "solar", 2)["graph"]
    assert stored == commit.run(case[0], {"expected_rev": 1}, candidate=frozen["candidate"])
    # Apart from stable allocation, the dispatcher must retain pure acceptance.
    with monkeypatch.context() as patch:
        identifiers = iter(string["id"] for string in stored["strings"])
        patch.setattr(solve, "new_id", lambda kind: next(identifiers))
        assert stored == commit.commit_solve(case[0], {"expected_rev": 1}, candidate=frozen["candidate"])
    assert frozen == original
    context = execution(result["job_id"])
    assert context["proposal_candidate"] == original and "solve_scope" not in context
    assert jobs.get_job(ready)["result"]["result"]["candidate"] == original["candidate"]


def test_commit_solve_missing_reference(api, offline):
    offline()
    for reference in (ABSENT, None):
        env = _refused(api, _body(api, reference), "proposal_job_required")
        assert env["availability"]["engine_ready"] is True
        assert env["availability"]["input_ready"] is False
        assert env["availability"]["refusal_reasons"] == ["proposal_job_required"]


def test_commit_solve_malformed_reference(api, offline):
    offline()
    for value in ("", " ", 3, True, [], {}, "x" * 257):
        _refused(api, _body(api, value), "invalid_commit_request")


def test_commit_solve_private_inputs_in_params(api, ready):
    for field, value in (("proposal_job_id", ready), ("candidate", {}),
                         ("candidate_sha256", "0" * 64), ("scope", {}), ("proof", {}),
                         ("initialize", None), ("cancel", 1)):
        _refused(api, _body(api, ready, params={"expected_rev": 1, field: value}),
                 "invalid_commit_request")
    _refused(api, _body(api, params={"expected_rev": 1, "proposal_job_id": ready}),
             "invalid_commit_request")


def test_commit_solve_reference_on_other_tool(api, ready):
    request = _body(api, ready)
    request.update(tool="solar-settings", params={"expected_rev": 1, "changes": {"num_mppt": 2}},
                   catalog_digest=deps.catalog_tool_digest(api[2]["solar-settings"]))
    _refused(api, request, None, status=400)


def test_commit_solve_unknown_reference(api, offline):
    offline()
    _refused(api, _body(api, "missing"), "invalid_solve_candidate")


def test_commit_solve_foreign_reference(api, ready):
    missing = _refused(api, _body(api, "missing"), "invalid_solve_candidate")
    record = jobs.get_job(ready)
    record["tenant_id"] = "other-tenant"
    _save(ready, record)
    foreign = _refused(api, _body(api, ready), "invalid_solve_candidate")
    assert foreign == missing


@pytest.mark.parametrize("status", ["submitted", "running", "failed"])
def test_commit_solve_unfinished_reference(api, ready, status):
    record = jobs.get_job(ready)
    record["status"] = status
    _save(ready, record)
    _refused(api, _body(api, ready), "invalid_solve_candidate")


def test_commit_solve_wrong_tool_or_envelope(api, ready):
    original = jobs.get_job(ready)
    for changes in ({"tool": "solar-settings"}, {"result": None},
                    {"result": {"ok": False}}, {"result": {"ok": 1}}):
        record = copy.deepcopy(original)
        record.update(changes)
        _save(ready, record)
        _refused(api, _body(api, ready), "invalid_solve_candidate")


def test_commit_solve_raw_proposal_has_no_binding(api, case, offline):
    job_id = _proposal(api, case, bound=False)
    offline()
    _refused(api, _body(api, job_id), "invalid_solve_binding")


def test_commit_solve_row_scope_disagreement(api, ready):
    original = jobs.get_job(ready)
    original_context = execution(ready)
    for field, value in (("dwg", "another-drawing"), ("dwg_version", 2)):
        record = copy.deepcopy(original)
        record[field] = value
        _save(ready, record, original_context)
        _refused(api, _body(api, ready), "invalid_solve_binding")
    # Internally consistent, but belongs to a different drawing than the commit.
    record = copy.deepcopy(original)
    context = copy.deepcopy(original_context)
    record["dwg"] = context["solve_scope"]["drawing_id"] = "another-drawing"
    record["result"]["result"]["scope"]["drawing_id"] = "another-drawing"
    _save(ready, record, context)
    _refused(api, _body(api, ready), "invalid_solve_binding")


def test_commit_solve_binding_identity(api, ready):
    original = execution(ready)
    for field in ("tenant_id", "job_id"):
        context = copy.deepcopy(original)
        context["solve_scope"]["binding"][field] = "other"
        _save(ready, jobs.get_job(ready), context)
        _refused(api, _body(api, ready), "invalid_solve_binding")


def test_commit_solve_result_scope_disagreement(api, ready):
    record = jobs.get_job(ready)
    record["result"]["result"]["scope"]["source_version"] = 2
    _save(ready, record)
    _refused(api, _body(api, ready), "invalid_solve_binding")


def test_commit_solve_candidate_binding_tamper(api, ready):
    record = jobs.get_job(ready)
    record["result"]["result"]["candidate"]["binding"]["frame_ref"] = "unknown"
    _save(ready, record)
    _refused(api, _body(api, ready), "invalid_solve_candidate")


def test_commit_solve_unknown_bound_frame(api, ready):
    context = execution(ready)
    context["solve_scope"]["binding"]["frame_ref"] = "unknown"
    _save(ready, jobs.get_job(ready), context)
    _refused(api, _body(api, ready), "solve_grid_mismatch")


def test_commit_solve_receipt_and_terminal_proof_tamper(api, ready):
    original = jobs.get_job(ready)
    for target in ("receipt", "proof"):
        record = copy.deepcopy(original)
        if target == "receipt":
            record["result"]["result"]["proposal"]["request_sha256"] = "0" * 64
        else:
            record["provenance"]["response_sha256"] = "0" * 64
        _save(ready, record)
        _refused(api, _body(api, ready), "invalid_solve_proposal")


def test_commit_solve_candidate_recomputation(api, ready):
    record = jobs.get_job(ready)
    record["result"]["result"]["candidate"]["accepted"] = True
    _save(ready, record)
    _refused(api, _body(api, ready), "invalid_solve_candidate")


def test_commit_solve_stale_proposal_basis(api, ready):
    before = snapshot(api)
    _advance(api)
    assert snapshot(api) == (before[0], 2)
    request = _body(api, ready)
    request.pop("dwg_version")
    _refused(api, request, "stale_solve_result")


def test_commit_solve_historical_parent_precedence(api, ready):
    before = snapshot(api)
    _advance(api)
    assert snapshot(api) == (before[0], 2)
    _refused(api, _body(api, ready), "not_current_head")


def test_commit_solve_stale_expected_revision(api, ready):
    _worker_refusal(api, ready, {"expected_rev": 0}, "STALE_GRAPH_REVISION")


def test_commit_solve_competing_publication(api, ready, monkeypatch):
    before = snapshot(api)
    original = local.publish_version

    def competing(*args, **kwargs):
        _advance(api)
        return original(*args, **kwargs)

    monkeypatch.setattr(local, "publish_version", competing)
    response = api[0].post("/api/run?wait=1", json=_body(api, ready))
    assert response.status_code == 400, response.text
    assert response.json()["error"]["reason_code"] == "STALE_GRAPH_REVISION"
    assert snapshot(api) == (before[0] + 1, 2)
    assert [r for r in jobs.list_jobs(TENANT) if r["tool"] == TOOL][0]["status"] == "failed"
    assert resolve_graph_context(api[1], TENANT, "solar", 2)["graph"]["strings"] == []


def test_commit_solve_anonymous_checkout(api, ready, monkeypatch):
    monkeypatch.setattr(api[3], "_checkout_identity", lambda *a: (store.ANONYMOUS_HOLDER, None))
    _refused(api, _body(api, ready), "CHECKOUT_REQUIRED", status=403)


def test_commit_solve_cancel(api, ready):
    _worker_refusal(api, ready, {"expected_rev": 1, "cancel": True}, "GRAPH_COMMIT_CANCELLED")


def test_commit_solve_sqlite_same_key(api, ready, monkeypatch):
    queue, job_id = _queue(api, monkeypatch, ready)
    before = snapshot(api)
    response = api[0].post("/api/run", json=_body(api, ready), headers={"Idempotency-Key": "commit-key"})
    assert response.status_code == 202, response.text
    assert response.json()["job_id"] == job_id
    assert len(queue.calls) == 1
    unchanged(api, before)


def test_commit_solve_sqlite_key_conflict(api, two, monkeypatch):
    queue, _ = _queue(api, monkeypatch, two[0])
    before = snapshot(api)
    response = api[0].post("/api/run", json=_body(api, two[1]), headers={"Idempotency-Key": "commit-key"})
    assert response.status_code == 409, response.text
    assert "reason_code" not in response.json()
    assert "different run input" in response.json()["error"]["message"]
    assert len(queue.calls) == 1
    unchanged(api, before)


def test_commit_solve_publisher_replay(api, ready):
    env = _commit(api, ready)
    before = snapshot(api)
    status, replay = _deliver(api, _delivery(api, env["result"]["job_id"]))
    assert status == 200 and replay["ok"] is True
    assert replay["result"]["replayed"] is True
    assert replay["result"]["new_version"] == env["result"]["new_version"]
    unchanged(api, before)


def test_commit_solve_publisher_replay_after_head_advance(api, ready):
    env = _commit(api, ready)
    _advance(api)
    before = snapshot(api)
    assert before[1] == 3
    status, replay = _deliver(api, _delivery(api, env["result"]["job_id"]))
    assert status == 200 and replay["result"]["replayed"] is True
    assert replay["result"]["new_version"]["version"] == 2
    unchanged(api, before)


def _pg_broker(api, monkeypatch):
    from broker_pg_store import PostgresBrokerStore
    from test_broker_pg_store import _Db

    database = _Db()
    authority = PostgresBrokerStore(database)
    monkeypatch.setenv("LEAF_BROKER_STORE", "postgres")
    monkeypatch.setattr(api[4], "_pg_store", authority)
    monkeypatch.setattr(api[4], "_get_usage", lambda: None)
    return database, authority


def test_commit_solve_postgres_cached_broker_replay(api, ready, monkeypatch):
    database, _ = _pg_broker(api, monkeypatch)
    env = _commit(api, ready)
    before = snapshot(api)
    request = _delivery(api, env["result"]["job_id"])
    cached = copy.deepcopy(database.pool.conn.admissions[request.ledger_event_key]["result_json"])
    if isinstance(cached, str):
        cached = json.loads(cached)
    status, replay = _deliver(api, request)
    assert status == 200 and replay == cached
    assert replay["result"]["replayed"] is False
    unchanged(api, before)


def test_commit_solve_broker_event_key_mismatch(api, two, monkeypatch):
    other = _frozen(two[1])
    _pg_broker(api, monkeypatch)
    env = _commit(api, two[0])
    before = snapshot(api)
    status, refused = _deliver(api, _delivery(api, env["result"]["job_id"], other))
    assert status == 400
    assert refused["error"]["reason_code"] == "broker_event_key_invalid"
    unchanged(api, before)


def test_commit_solve_published_job_binding_reused(api, two):
    other = _frozen(two[1])
    env = _commit(api, two[0])
    before = snapshot(api)
    status, refused = _deliver(api, _delivery(api, env["result"]["job_id"], other))
    assert status == 400 and refused["error"]["reason_code"] == "JOB_BINDING_REUSED"
    unchanged(api, before)


def test_commit_solve_terminal_receipt_tamper_leaves_publication(api, ready, monkeypatch):
    before = snapshot(api)
    original = api[4].ok_envelope

    def tamper(*args, **kwargs):
        env = original(*args, **kwargs)
        env["result"]["graph_sha256"] = "0" * 64
        return env

    monkeypatch.setattr(api[4], "ok_envelope", tamper)
    response = api[0].post("/api/run?wait=1", json=_body(api, ready))
    assert response.status_code == 500, response.text
    assert response.json()["error"]["error_code"] == "INTERNAL"
    assert response.json()["error"]["message"] == "graph commit terminal proof rejected"
    assert "reason_code" not in response.json()
    assert snapshot(api) == (before[0] + 1, 2)
    assert [r for r in jobs.list_jobs(TENANT) if r["tool"] == TOOL][0]["status"] == "failed"


def test_commit_solve_frozen_snapshot_survives_proposal_changes(api, ready, monkeypatch):
    queue, job_id = _queue(api, monkeypatch, ready)
    frozen = execution(job_id)["proposal_candidate"]
    record = jobs.get_job(ready)
    record["result"] = None
    record["status"] = "failed"
    _save(ready, record, {})
    before = snapshot(api)
    queue.execute()
    assert jobs.get_job(job_id)["status"] == "complete"
    assert snapshot(api) == (before[0], 2)
    assert execution(job_id)["proposal_candidate"] == frozen


def test_commit_solve_restart_recovers_frozen_parent(api, ready, monkeypatch):
    queue, job_id = _queue(api, monkeypatch, ready)
    frozen = execution(job_id)["proposal_candidate"]
    queue.calls.clear()
    jobs.reset_connection()
    before = snapshot(api)
    monkeypatch.setattr(proposals, "resolve_proposal", lambda *a, **k: pytest.fail("proposal refetched"))
    assert jobs._redispatch_record(job_id) is True
    assert len(queue.calls) == 1
    assert queue.calls[0][1][6] == 1
    queue.execute()
    assert jobs.get_job(job_id)["status"] == "complete"
    assert snapshot(api) == (before[0], 2)
    assert jobs.proposal_candidate_context(job_id) == frozen


def test_commit_solve_graphless_precedes_reference(graphless_api, offline):
    offline()
    _refused(graphless_api, _body(graphless_api), "persisted_graph_unavailable")


def test_commit_solve_bundle_precedes_reference(api, drawing, case, monkeypatch, offline):
    offline()
    backend, _ = drawing
    bundle_commit(drawing, request_for(backend, case[0]))
    monkeypatch.setattr(write_loop, "backend_for_tenant", lambda *a, **k: backend)
    tenant = deps.TenantContext(BUNDLE_TENANT, tier="demo", subject="fixture-subject", authority_resolved=True)
    api[0].app.dependency_overrides[deps.require_tenant] = lambda: tenant
    head = store.load_manifest(backend, BUNDLE_TENANT, DRAWING)["head"]
    count = len(jobs._query("SELECT job_id FROM jobs"))
    request = _body(api)
    request.update(dwg=DRAWING, dwg_version=head)
    response = api[0].post("/api/run?wait=1", json=request)
    assert response.status_code == 409, response.text
    assert response.json()["reason_code"] == "licensed_graph_commit_required"
    assert store.load_manifest(backend, BUNDLE_TENANT, DRAWING)["head"] == head
    assert len(jobs._query("SELECT job_id FROM jobs")) == count


def test_commit_solve_all_three_identities(api, two, monkeypatch):
    before = snapshot(api)
    frozen = [_frozen(job_id) for job_id in two]
    ids = [_queue(api, monkeypatch, proposal, key="identity-" + str(i))[1]
           for i, proposal in enumerate(two)]
    app_hashes = [jobs._query("SELECT submission_fingerprint FROM jobs WHERE job_id=?", (job_id,))[0]
                  ["submission_fingerprint"] for job_id in ids]
    broker_hashes = [api[4]._broker_request_fingerprint(_delivery(api, job_id)) for job_id in ids]
    publication_hashes = [local.request_digest(TOOL, "solar", 1, {"expected_rev": 1},
                                              proposal_candidate=value) for value in frozen]
    for hashes in (app_hashes, broker_hashes, publication_hashes):
        assert hashes[0] != hashes[1]
    for job_id, value in zip(two, frozen):
        identity = proposals.proposal_identity(value)
        assert identity == {"proposal_job_id": job_id, "scope": value["solve_scope"],
                            "candidate_sha256": hashlib.sha256(proposals.canonical(
                                value["candidate"]).encode("utf-8")).hexdigest()}
        changed = copy.deepcopy(value)
        changed["candidate"]["proof"]["response_sha256"] = "0" * 64
        assert proposals.proposal_identity(changed)["candidate_sha256"] != identity["candidate_sha256"]
    unchanged(api, before, 2)


def test_commit_solve_absent_context_legacy_formulas(api, monkeypatch, offline):
    offline()
    before = snapshot(api)
    tool = api[2]["solar-settings"]
    params = {"drawing_id": "solar", "expected_rev": 1, "changes": {"num_mppt": 2}}
    queue = _Queue()
    monkeypatch.setitem(jobs._executors, jobs.lane_for(tool, False), queue)
    holder, fence = api[3]._checkout_identity()
    job_id = jobs.submit_job(TENANT, tool, params, "solar", False, dwg_version=1,
                             checkout_holder=holder, checkout_fence=fence)
    legacy_app = {"tenantId": TENANT, "orgId": None, "projectId": None, "tool": tool,
                  "params": params, "dwg": "solar", "apsLive": False,
                  "authorityMode": "legacy_sqlite", "dwgVersion": 1}
    expected = hashlib.sha256(json.dumps(legacy_app, sort_keys=True, separators=(",", ":"),
                                         default=str).encode("utf-8")).hexdigest()
    assert jobs._query("SELECT submission_fingerprint FROM jobs WHERE job_id=?", (job_id,))[0][
        "submission_fingerprint"] == expected
    legacy_broker = {"tenant_id": TENANT, "tool": tool, "params": params, "dwg": "solar",
                     "aps_live": False, "dwg_version": 1}
    expected = hashlib.sha256(json.dumps(legacy_broker, sort_keys=True, separators=(",", ":"),
                                         ensure_ascii=False).encode("utf-8")).hexdigest()
    req = api[4].BrokerRunRequest(**legacy_broker)
    assert api[4]._broker_request_fingerprint(req) == expected
    builtin_params = {k: v for k, v in params.items() if k != "drawing_id"}
    legacy_local = {"tool": tool["name"], "drawing_id": "solar", "source_version": 1,
                    "params": builtin_params}
    expected = hashlib.sha256(json.dumps(legacy_local, sort_keys=True, separators=(",", ":"),
                                         allow_nan=False).encode()).hexdigest()
    assert local.request_digest(tool["name"], "solar", 1, builtin_params) == expected
    wires = []

    def capture(url, *, json, **kwargs):
        wires.append(copy.deepcopy(json))

        class Reply:
            status_code = 400

            def json(self):
                return {"ok": False, "error": {"error_code": "BAD_PARAMS"}}

        return Reply()

    monkeypatch.setattr(broker_client.requests, "post", capture)
    broker_client.run_via_broker(TENANT, tool, params, "solar", False, dwg_version=1,
                                 job_id=job_id, checkout_holder=holder, checkout_fence=fence)
    assert wires == [dict(legacy_broker, ledger_event_key=None, checkout_holder=holder,
                         checkout_fence=fence, job_id=job_id)]
    assert "proposal_candidate" not in execution(job_id)
    unchanged(api, before, 1)


def test_commit_solve_contextual_readiness(api, ready, case):
    before = snapshot(api)
    graph_inputs = availability.w1_graph_readiness(case[0])[TOOL]
    assert graph_inputs == {"input_ready": True, "input_reason": None}
    assert availability.w1_availability(TOOL, entitled=True, inputs=graph_inputs)["runnable"] is True
    tool = api[2][TOOL]
    absent = entitlements.w1_tool_availability(tool, api[5], "solar", version=1)
    assert absent["engine_ready"] is True and absent["input_ready"] is False
    assert absent["refusal_reasons"] == ["proposal_job_required"]
    frozen = _frozen(ready)
    current = entitlements.w1_tool_availability(
        tool, api[5], "solar", version=1, proposal_state={"input_reason": None, "snapshot": frozen})
    assert current["runnable"] is True and current["refusal_reasons"] == []
    unchanged(api, before)


def test_commit_solve_resolution_happens_once(api, ready, monkeypatch):
    original = jobs.get_job
    lookups = []

    def get_job(job_id):
        if job_id == ready:
            lookups.append(job_id)
        return original(job_id)

    monkeypatch.setattr(jobs, "get_job", get_job)
    _commit(api, ready)
    assert lookups == [ready]


def test_commit_solve_terminal_callback_recomputes_snapshot(api, two):
    other = _frozen(two[1])
    env = _commit(api, two[0])
    job_id = env["result"]["job_id"]
    record = jobs.get_job(job_id)
    context = execution(job_id)
    before = snapshot(api)
    jobs._validate_terminal_context("complete", env, record["provenance"], record["attempt"],
                                    context, job_id=job_id, durable_params=record["params"])
    for mutation in ("candidate", "identity", "receipt"):
        changed = copy.deepcopy(context)
        result = copy.deepcopy(env)
        if mutation == "candidate":
            changed["proposal_candidate"]["candidate"]["accepted"] = True
        elif mutation == "identity":
            changed["proposal_candidate"] = other
        else:
            result["result"]["graph_sha256"] = "0" * 64
        with pytest.raises(ValueError, match="graph commit terminal proof rejected"):
            jobs._validate_terminal_context("complete", result, record["provenance"], record["attempt"],
                                            changed, job_id=job_id, durable_params=record["params"])
        unchanged(api, before)


def test_commit_solve_terminal_reexecutes_acceptance(api, ready):
    env = _commit(api, ready)
    before = snapshot(api)
    result = env["result"]
    job_id = result["job_id"]
    record = jobs.get_job(job_id)
    _, key, _ = store.resolve_version_entry(api[1], TENANT, "solar", 2)
    intake = json.loads(api[1].get(key))
    intake["solar_design_graph"]["project"]["name"] = "Unaccepted graph content"
    intake["solar_design_graph_sha256"] = local.digest(intake["solar_design_graph"])
    data = json.dumps(intake).encode("utf-8")
    api[1].put(key, data)
    manifest = store.load_manifest(api[1], TENANT, "solar")
    entry = next(row for row in manifest["versions"] if row["v"] == 2)
    entry["sha256"] = hashlib.sha256(data).hexdigest()
    store.save_manifest(api[1], TENANT, "solar", manifest)
    result["graph_sha256"] = intake["solar_design_graph_sha256"]
    result["intake_sha256"] = entry["sha256"]
    proof = copy.deepcopy(record["provenance"])
    proof.update(graph_sha256=result["graph_sha256"], intake_sha256=result["intake_sha256"])
    # Every stored digest and receipt agrees; only acceptance against parent 1 can refuse.
    with pytest.raises(ValueError, match="graph commit terminal proof rejected"):
        jobs._validate_terminal_context("complete", env, proof, record["attempt"],
                                        execution(job_id), job_id=job_id, durable_params=record["params"])
    unchanged(api, before)


def test_commit_solve_sqlite_snapshot_is_detached(api, ready, monkeypatch):
    _, job_id = _queue(api, monkeypatch, ready)
    before = snapshot(api)
    original = jobs.proposal_candidate_context(job_id)
    altered = jobs.proposal_candidate_context(job_id)
    altered["candidate"]["accepted"] = True
    altered["params"]["grant_ref"] = "changed"
    jobs.reset_connection()
    assert jobs.proposal_candidate_context(job_id) == original
    assert "execution" not in jobs.get_job(job_id)
    unchanged(api, before)


def test_commit_solve_revision_validation_stays_in_kernel(api, ready):
    for params in ({}, {"expected_rev": None}, {"expected_rev": True},
                   {"expected_rev": "1"}, {"expected_rev": 1.0}, {"expected_rev": -0.0}):
        _worker_refusal(api, ready, params, "STALE_GRAPH_REVISION")


def test_commit_solve_broker_malformed_context_is_structured(api, ready, monkeypatch):
    _, job_id = _queue(api, monkeypatch, ready)
    before = snapshot(api)
    original = _delivery(api, job_id)
    for value in (None, [], "candidate", {}, dict(original.proposal_candidate, extra=True)):
        request = original.model_copy(deep=True)
        request.proposal_candidate = value
        status, env = _deliver(api, request)
        assert status == 400 and env["error"]["reason_code"] == "INVALID_SOLVE_CANDIDATE"
        unchanged(api, before)


def test_commit_solve_broker_revalidates_identity_proof_and_candidate(api, ready, monkeypatch):
    _, job_id = _queue(api, monkeypatch, ready)
    before = snapshot(api)
    original = _delivery(api, job_id)
    for defect, reason in (("tenant", "INVALID_SOLVE_BINDING"), ("job", "INVALID_SOLVE_BINDING"),
                           ("version", "INVALID_SOLVE_BINDING"), ("proof", "INVALID_SOLVE_PROPOSAL"),
                           ("candidate", "INVALID_SOLVE_CANDIDATE")):
        request = original.model_copy(deep=True)
        frozen = request.proposal_candidate
        if defect == "tenant":
            frozen["solve_scope"]["binding"]["tenant_id"] = "other"
        elif defect == "job":
            frozen["proposal_job_id"] = "other"
        elif defect == "version":
            frozen["solve_scope"]["source_version"] = 2
        elif defect == "proof":
            frozen["proposal_proof"]["response_sha256"] = "0" * 64
        else:
            frozen["candidate"]["accepted"] = True
        status, env = _deliver(api, request)
        assert status == 400 and env["error"]["reason_code"] == reason
        unchanged(api, before)


def test_commit_solve_broker_rejects_snapshot_on_other_tool(api, ready, monkeypatch):
    _, job_id = _queue(api, monkeypatch, ready)
    before = snapshot(api)
    request = _delivery(api, job_id)
    request.tool = api[2]["solar-settings"]
    status, env = _deliver(api, request)
    assert status == 400 and env["error"]["reason_code"] == "INVALID_COMMIT_REQUEST"
    unchanged(api, before)


def test_commit_solve_invalid_durable_params(api, ready):
    original = jobs.get_job(ready)
    for params in ({}, dict(original["params"], unexpected=True)):
        record = copy.deepcopy(original)
        record["params"] = params
        _save(ready, record)
        _refused(api, _body(api, ready), "invalid_solve_binding")


def test_commit_solve_postgres_snapshot_and_null_project_key(api, case, request, monkeypatch, offline):
    # Use the existing real PostgreSQL authority, including its schema setup.
    # Its DATABASE_URL skip remains visible to the planner, never counted as proof.
    db, _ = request.getfixturevalue("postgres_authority")
    monkeypatch.setattr(jobs, "job_store_mode", lambda: "postgres")
    monkeypatch.setattr(jobs, "ensure_started", jobs._pg_store.ensure_ready)

    class Inline:
        def submit(self, fn, *args, **kwargs):
            fn(*args, **kwargs)

    monkeypatch.setitem(jobs._executors, jobs.lane_for(api[2][cloud.TOOL_NAME], False), Inline())
    created = []

    def count():
        with db.cursor() as cursor:
            cursor.execute("SELECT count(*) AS count FROM async_jobs WHERE tenant_id=%s", (TENANT,))
            return cursor.fetchone()["count"]

    initial = count()
    try:
        for _ in range(2):
            response = api[0].post("/api/run?wait=1", json=proposal_body(api, case))
            assert response.status_code == 200, response.text
            created.append(response.json()["result"]["proposal"]["job_id"])
            assert jobs.get_job(created[-1])["status"] == "complete"
        offline()
        assert count() == initial + 2
        frozen = _frozen(created[0])
        assert jobs.solve_scope_context(created[0]) == frozen["solve_scope"]
        queue = _Queue()
        monkeypatch.setitem(jobs._executors, jobs.lane_for(api[2][TOOL], False), queue)
        key = "cs2-" + uuid.uuid4().hex
        before = count()
        headers = {"Idempotency-Key": key}
        first = api[0].post("/api/run", json=_body(api, created[0]), headers=headers)
        assert first.status_code == 202, first.text
        job_id = first.json()["job_id"]
        created.append(job_id)
        assert count() == before + 1
        same = api[0].post("/api/run", json=_body(api, created[0]), headers=headers)
        assert same.status_code == 202 and same.json()["job_id"] == job_id
        changed = api[0].post("/api/run", json=_body(api, created[1]), headers=headers)
        assert changed.status_code == 409
        assert "different run input" in changed.json()["error"]["message"]
        assert count() == before + 1 and len(queue.calls) == 1
        assert jobs.get_job(job_id)["project_id"] is None
        assert jobs.proposal_candidate_context(job_id) == frozen
        assert store.load_manifest(api[1], TENANT, "solar")["head"] == 1
        # Recover from the selected authority, with no in-memory delivery left.
        queue.calls.clear()
        assert jobs._redispatch_record(job_id) is True
        queue.execute()
        assert jobs.get_job(job_id)["status"] == "complete"
        assert jobs.proposal_candidate_context(job_id) == frozen
        assert count() == before + 1
        assert store.load_manifest(api[1], TENANT, "solar")["head"] == 2
    finally:
        with db.transaction() as connection:
            for job_id in reversed(created):
                connection.execute("DELETE FROM async_jobs WHERE job_id=%s", (job_id,))
