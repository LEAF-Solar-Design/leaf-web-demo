"""Bound solve proposals through the app, durable job store and real broker."""
import copy
import hashlib
import json
import sys
from pathlib import Path

import pytest

SERVER = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SERVER))
sys.path.insert(0, str(SERVER / "tests"))

import deps
import jobs
import leaf_cloud_client as cloud
import solar_proposal_candidate as candidate_validation
import solar_solve_results as solve
import store
from test_w1_design_graph import graph  # noqa: F401
from test_w1_solve_commit import case, seed, seed_graphless, publish  # noqa: F401
from test_w1_local_graph_jobs import isolated_jobs, no_network  # noqa: F401
from test_w1_local_graph_rail import _api, TENANT


def _configure(api, monkeypatch):
    tool = next(t for t in json.loads((SERVER / "catalog_tools.json").read_text())["tools"]
                if t["name"] == cloud.TOOL_NAME)
    api[2][cloud.TOOL_NAME] = tool
    executor = next(iter(jobs._executors.values()))
    monkeypatch.setitem(jobs._executors, jobs.lane_for(tool, False), executor)
    monkeypatch.setattr(api[3].deps, "auth_live", lambda: True)
    monkeypatch.setattr(api[3], "_active_binding_tenant", lambda *a: None)
    monkeypatch.setattr(api[3].catalog, "live_aps_runtime_authorized", lambda *a, **k: False)


@pytest.fixture
def api(case, isolated_jobs, no_network, tmp_path, monkeypatch):
    graph, _, response = case
    graph.update(rev=1, parent_rev=0)
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
def graphless_api(case, isolated_jobs, no_network, tmp_path, monkeypatch):
    backend, _ = seed_graphless(tmp_path, monkeypatch)
    for rail in _api(backend, tmp_path, monkeypatch):
        _configure(rail, monkeypatch)
        yield rail


def body(api, case, *, bound=True):
    graph, request, _ = case
    result = {"tool": cloud.TOOL_NAME, "dwg": "solar",
              "catalog_digest": deps.catalog_tool_digest(api[2][cloud.TOOL_NAME]),
              "params": {"grant_ref": "fixture-grant", "request": copy.deepcopy(request)}}
    if bound:
        result["solve_context"] = {"frame_ref": graph["frames"][0]["id"],
                                   "expected_rev": graph["rev"], "phase": "initial"}
    return result


def snapshot(api):
    return (len(jobs._query("SELECT job_id FROM jobs")),
            store.load_manifest(api[1], TENANT, "solar")["head"])


def unchanged(api, before, added=0):
    assert snapshot(api) == (before[0] + added, before[1])


def execution(job_id):
    return json.loads(jobs._query(
        "SELECT execution_json FROM jobs WHERE job_id = ?", (job_id,))[0]["execution_json"])


def direct_scope(case, job_id="direct-job", version=1):
    graph, request, _ = case
    return {"drawing_id": "solar", "source_version": version,
            "binding": solve.bind_request(
                graph, request, expected_rev=graph["rev"], frame_ref=graph["frames"][0]["id"],
                tenant_id=TENANT, job_id=job_id)}


def direct(api, case, scope, job_id="direct-job"):
    broker = api[4]
    response = broker._broker_run(broker.BrokerRunRequest(
        tenant_id=TENANT, tool=api[2][cloud.TOOL_NAME], params=body(api, case)["params"],
        dwg="solar", dwg_version=scope["source_version"], solve_scope=scope,
        job_id=job_id, ledger_event_key=job_id + ":broker-run"))
    return response.status_code, json.loads(response.body)


def test_bound_proposal_complete_candidate(api, case):
    before = snapshot(api)
    response = api[0].post("/api/run?wait=1", json=body(api, case))
    assert response.status_code == 200, response.text
    result = response.json()["result"]
    assert result["schema"] == candidate_validation.RESULT_SCHEMA
    receipt = result["proposal"]
    job_id = receipt["job_id"]
    assert jobs.get_job(job_id)["status"] == "complete"
    expected = solve.complete_search(case[0], direct_scope(case, job_id)["binding"], receipt)
    assert candidate_validation.canonical(result["candidate"]) == candidate_validation.canonical(expected)
    assert result["scope"] == {"drawing_id": "solar", "source_version": 1}
    unchanged(api, before, 1)


def test_bound_proposal_omitted_context_is_raw(api, case):
    before = snapshot(api)
    request = body(api, case, bound=False)
    response = api[0].post("/api/run?wait=1", json=request)
    assert response.status_code == 200, response.text
    result = response.json()["result"]
    assert "candidate" not in result and "schema" not in result
    assert result == cloud.proposal(request["params"], TENANT, result["job_id"])
    assert jobs.get_job(result["job_id"])["status"] == "complete"
    assert "solve_scope" not in execution(result["job_id"])
    unchanged(api, before, 1)


def test_bound_proposal_context_on_other_tool(api, case):
    before = snapshot(api)
    request = body(api, case)
    request.update(tool="solar-settings", params={"expected_rev": 1, "changes": {"panels_in_sequence": 3}},
                   catalog_digest=deps.catalog_tool_digest(api[2]["solar-settings"]))
    response = api[0].post("/api/run?wait=1", json=request)
    assert response.status_code == 400, response.text
    assert response.json()["error"]["error_code"] == "BAD_PARAMS"
    assert "solve_context" in response.json()["error"]["message"]
    unchanged(api, before)


def test_bound_proposal_stale_revision(api, case):
    before = snapshot(api)
    request = body(api, case)
    request["solve_context"]["expected_rev"] -= 1
    response = api[0].post("/api/run?wait=1", json=request)
    assert response.status_code == 409, response.text
    assert response.json()["reason_code"] == "stale_graph_revision"
    unchanged(api, before)


def test_bound_proposal_unknown_frame(api, case):
    before = snapshot(api)
    request = body(api, case)
    request["solve_context"]["frame_ref"] = "unknown-frame"
    response = api[0].post("/api/run?wait=1", json=request)
    assert response.status_code == 409, response.text
    assert response.json()["reason_code"] == "solve_grid_mismatch"
    unchanged(api, before)


def test_bound_proposal_background_requires_initial(api, case):
    before = snapshot(api)
    request = body(api, case)
    request["solve_context"]["phase"] = "background"
    response = api[0].post("/api/run?wait=1", json=request)
    assert response.status_code == 409, response.text
    assert response.json()["reason_code"] == "initial_solve_required"
    unchanged(api, before)


def test_bound_proposal_requires_embedded_graph(graphless_api, case):
    api = graphless_api
    before = snapshot(api)
    response = api[0].post("/api/run?wait=1", json=body(api, case))
    assert response.status_code == 409, response.text
    assert response.json()["reason_code"] == "persisted_graph_unavailable"
    unchanged(api, before)


def test_bound_proposal_persisted_server_identity(api, case):
    before = snapshot(api)
    request = body(api, case)
    for extra in ({"tenant_id": "attacker"}, {"job_id": "chosen"}):
        bad = copy.deepcopy(request)
        bad["solve_context"].update(extra)
        assert api[0].post("/api/run?wait=1", json=bad).status_code == 422
        unchanged(api, before)
    for value in (True, "1", 1.0, -1):
        bad = copy.deepcopy(request)
        bad["solve_context"]["expected_rev"] = value
        assert api[0].post("/api/run?wait=1", json=bad).status_code == 422
        unchanged(api, before)
    request.update(tenant_id="attacker", job_id="chosen")
    response = api[0].post("/api/run?wait=1", json=request)
    assert response.status_code == 200, response.text
    job_id = response.json()["result"]["proposal"]["job_id"]
    assert job_id != "chosen"
    assert execution(job_id)["solve_scope"] == direct_scope(case, job_id)
    unchanged(api, before, 1)


def test_bound_proposal_idempotency_scope_conflict(api, case):
    before = snapshot(api)
    request = body(api, case)
    headers = {"Idempotency-Key": "bound-key"}
    first = api[0].post("/api/run?wait=1", json=request, headers=headers)
    assert first.status_code == 200, first.text
    assert api[0].post("/api/run?wait=1", json=request, headers=headers).json() == first.json()
    for change in ({"frame_ref": "different-frame"}, {"expected_rev": 0}, {"phase": "background"}):
        altered = copy.deepcopy(request)
        altered["solve_context"].update(change)
        response = api[0].post("/api/run?wait=1", json=altered, headers=headers)
        assert response.status_code == 409, response.text
        assert "different run input" in response.json()["error"]["message"]
        unchanged(api, before, 1)
    assert len(api[6]) == 1


def test_bound_proposal_broker_rejects_wrong_job(api, case):
    before = snapshot(api)
    scope = direct_scope(case, "wrong-job")
    status, env = direct(api, case, scope)
    assert status == 409 and env["ok"] is False
    assert env["reason_code"] == "invalid_solve_binding"
    assert not api[6]
    unchanged(api, before)


def test_bound_proposal_broker_rejects_changed_graph(api, case):
    graph = case[0]
    after = solve.correct_graph(graph, {"expected_rev": graph["rev"],
                                       "settings_changes": {"panels_in_sequence": 5}})
    fence = store.load_manifest(api[1], TENANT, "solar")["checkout"]["fence"]
    publish(api[1], 1, graph, after, acquire=False, fence=fence)
    before = snapshot(api)
    assert before[1] == 2
    status, env = direct(api, case, direct_scope(case, version=2))
    assert status == 409 and env["ok"] is False
    assert env["reason_code"] == "stale_solve_result"
    assert not api[6]
    unchanged(api, before)


def test_bound_proposal_terminal_rejects_candidate(api, case, monkeypatch):
    before = snapshot(api)
    original = solve.complete_search

    def tamper(*args):
        candidate = original(*args)
        candidate["accepted"] = True
        return candidate

    # The shared app validator retains the real recomputation function.
    assert candidate_validation.complete_search is original
    monkeypatch.setattr(solve, "complete_search", tamper)
    response = api[0].post("/api/run?wait=1", json=body(api, case))
    assert response.json()["ok"] is False
    row = jobs._query("SELECT job_id FROM jobs")[0]
    assert jobs.get_job(row["job_id"])["status"] == "failed"
    unchanged(api, before, 1)


def test_bound_proposal_terminal_rejects_source_version(api, case, monkeypatch):
    before = snapshot(api)
    original = api[4].ok_envelope

    def tamper(*args, **kwargs):
        env = original(*args, **kwargs)
        result = env.get("result")
        if isinstance(result, dict) and result.get("schema") == candidate_validation.RESULT_SCHEMA:
            result["scope"]["source_version"] = 2
        return env

    monkeypatch.setattr(api[4], "ok_envelope", tamper)
    response = api[0].post("/api/run?wait=1", json=body(api, case))
    assert response.json()["ok"] is False
    row = jobs._query("SELECT job_id FROM jobs")[0]
    assert jobs.get_job(row["job_id"])["status"] == "failed"
    unchanged(api, before, 1)


def test_bound_proposal_second_terminal_check_failure_fails_job(api, case, monkeypatch):
    before = snapshot(api)
    original_resolve = candidate_validation.resolve_graph_context
    original_broker_run = api[4]._broker_run
    app_checks = []

    def resolve(*args, **kwargs):
        app_checks.append(args)
        if len(app_checks) == 2:
            raise candidate_validation.GraphValidationError("PERSISTED_GRAPH_UNAVAILABLE")
        return original_resolve(*args, **kwargs)

    def broker_run(*args, **kwargs):
        response = original_broker_run(*args, **kwargs)
        # Arm the failure only after the broker has validated the pinned graph.
        monkeypatch.setattr(candidate_validation, "resolve_graph_context", resolve)
        return response

    monkeypatch.setattr(api[4], "_broker_run", broker_run)
    response = api[0].post("/api/run?wait=1", json=body(api, case))
    assert response.json()["ok"] is False
    assert len(app_checks) == 2
    row = jobs._query("SELECT job_id FROM jobs")[0]
    assert jobs.get_job(row["job_id"])["status"] == "failed"
    unchanged(api, before, 1)


def test_bound_proposal_raw_fingerprints_unchanged(api, case):
    before = snapshot(api)
    request = body(api, case, bound=False)
    response = api[0].post("/api/run?wait=1", json=request)
    assert response.status_code == 200, response.text
    job_id = response.json()["result"]["job_id"]
    tool = api[2][cloud.TOOL_NAME]
    legacy_submission = {
        "tenantId": TENANT, "orgId": None, "projectId": None,
        "tool": tool, "params": request["params"], "dwg": "solar", "apsLive": False,
        "authorityMode": "legacy_sqlite", "dwgVersion": None,
    }
    expected = hashlib.sha256(json.dumps(legacy_submission, sort_keys=True,
                                         separators=(",", ":"), default=str).encode("utf-8")).hexdigest()
    row = jobs._query("SELECT submission_fingerprint FROM jobs WHERE job_id = ?", (job_id,))[0]
    assert row["submission_fingerprint"] == expected
    broker = api[4]
    req = broker.BrokerRunRequest(tenant_id=TENANT, tool=tool, params=request["params"], dwg="solar")
    legacy_admission = {"tenant_id": TENANT, "tool": tool, "params": request["params"],
                        "dwg": "solar", "aps_live": False, "dwg_version": None}
    expected = hashlib.sha256(json.dumps(legacy_admission, sort_keys=True,
                                         separators=(",", ":"), ensure_ascii=False).encode("utf-8")).hexdigest()
    assert broker._broker_request_fingerprint(req) == expected
    explicit_none = broker.BrokerRunRequest(**req.model_dump())
    assert explicit_none.solve_scope is None
    assert broker._broker_request_fingerprint(explicit_none) == expected
    unchanged(api, before, 1)
