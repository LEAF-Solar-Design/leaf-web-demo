"""Canonical proposal admission, single-send recovery and offline proof seams."""
import json
from fastapi import FastAPI
from fastapi.testclient import TestClient
import deployment_identity
import solar_project_context as assembly_project
import solar_project_graph as assembly_graph_api
import solar_tools
from leaf_platform import project_solve_store as assembly_store
from routers import jobs as assembly_route
from solar_design_graph import validate_graph
from test_w1_design_graph import app_id
from test_w1_grid_restitch import GROUPS

from copy import deepcopy
from datetime import datetime, timedelta, timezone
import base64
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest
import requests

import deps
import entitlements
import leaf_cloud_client as cloud
import platform_link
import solar_project_admission as policy
import solar_project_graph as graph
import solar_project_jobs as service
import solar_project_solve as solve
import solar_sizing_client as sizing
from leaf_cloud_grants import CloudError, CloudGrant
from leaf_platform import canonical_jobs as jobs, project_solve_store as real_store
from product_capability_availability import w1_graph_readiness
from solar_design_graph import GraphValidationError
from test_w1_design_graph import graph as graph_fixture  # noqa: F401
from test_w1_solve_commit import case, graph as graph  # noqa: F401
import solar_project_graph as adapter
import test_sip_r3b_tools as b3
import test_sip_r5_policy as r5
import test_sip_r5_routes as routes

_blocking_transport = cloud.post_stringer


@pytest.mark.parametrize("stage", ["split.restitch", "split.coverage", "split.string_length"])
def test_split_failure_diagnostics_persist_stage_and_piece_order(monkeypatch, caplog, stage):
    """The real assembly refusal must remain classifiable through GET job.error."""
    children = [{"piece_index": index, **{key: str(index + 1) * 64 for key in (
        "request_sha256", "response_sha256", "candidate_sha256", "proof_sha256")}}
        for index in range(2)]
    store = SimpleNamespace(get_exchange=lambda *args: {})
    monkeypatch.setattr(solve, "_store", lambda: store)
    monkeypatch.setattr(solve, "_piece_scope", lambda *args: None)
    monkeypatch.setattr(solve, "_piece_plan", lambda *args: {})
    monkeypatch.setattr(solve, "_reservation", lambda *args: {})
    monkeypatch.setattr(solve, "_prepare_piece", lambda *args: children[args[3]])
    failure = GraphValidationError("INVALID_FINAL_GRID_ORDER", stage)
    monkeypatch.setattr(solve, "_derive_split", lambda *args: (_ for _ in ()).throw(failure))
    job_id = str(uuid4())
    with caplog.at_level("WARNING"), pytest.raises(GraphValidationError) as caught:
        solve._prepare_split({"job_id": job_id}, None,
            {"split_binding": {"pieces": [{}, {}]}}, {"classification": "live"},
            None, None, SimpleNamespace(lost=False))
    assert caught.value is failure
    error = service._preparation_error(failure)
    assert error["reason_code"] == "INVALID_FINAL_GRID_ORDER"
    assert error["retryable"] is False
    assert error["solve_progress"] == {"status": "partial", "completed_pieces": 2,
        "total_pieces": 2, "phase": "assembly", "validation_stage": stage,
        "piece_order": [0, 1], "piece_order_truncated": False}
    assert job_id in caplog.text and stage in caplog.text
    assert "request_sha256" not in caplog.text
    assert "response_sha256" not in caplog.text


def test_split_failure_diagnostics_bound_order_and_omit_exception_text(monkeypatch, caplog):
    children = [{"piece_index": index, **{key: "a" * 64 for key in (
        "request_sha256", "response_sha256", "candidate_sha256", "proof_sha256")}}
        for index in range(40)]
    monkeypatch.setattr(solve, "_store", lambda: SimpleNamespace(get_exchange=lambda *args: {}))
    monkeypatch.setattr(solve, "_piece_scope", lambda *args: None)
    monkeypatch.setattr(solve, "_piece_plan", lambda *args: {})
    monkeypatch.setattr(solve, "_reservation", lambda *args: {})
    monkeypatch.setattr(solve, "_prepare_piece", lambda *args: children[args[3]])
    failure = GraphValidationError("INVALID_FINAL_GRID_ORDER", "private-exception-material")
    monkeypatch.setattr(solve, "_derive_split", lambda *args: (_ for _ in ()).throw(failure))
    with caplog.at_level("WARNING"), pytest.raises(GraphValidationError):
        solve._prepare_split({"job_id": str(uuid4())}, None,
            {"split_binding": {"pieces": [{}] * 40}}, {}, None, None, SimpleNamespace(lost=False))
    diagnostic = service._preparation_error(failure)["solve_progress"]
    assert diagnostic["validation_stage"] == "split.unknown"
    assert diagnostic["piece_order"] == list(range(32))
    assert diagnostic["piece_order_truncated"] is True
    assert "private-exception-material" not in caplog.text


def ready_graph(value):
    value = deepcopy(value)
    value["settings"].update(global_string_sizing_confirmed=True,
                             voc_cold=sizing.manual_voltage())
    value["settings"]["extra"]["string_sizing"] = sizing.manual_sizing_evidence(value)
    return value


def solve_context(value):
    return {"frame_ref": value["frames"][0]["id"], "expected_rev": value["rev"], "phase": "initial"}


@pytest.fixture
def admitted(case, monkeypatch):
    a = r5.setup(monkeypatch)
    value = ready_graph(case[0])
    a.s.set_parent(b3.embedded(value))
    a.params = {"grant_ref": "g1", "request": case[1]}
    a.solve_context = solve_context(value)
    a.context = adapter.resolve_project_graph_context(a.s.context())
    a.grants = []
    monkeypatch.setattr(cloud, "resolve_grant", lambda ref, tenant:
        a.grants.append((ref, tenant)) or CloudGrant(tenant, "private-test-token"))
    return a


def request(a, **changes):
    return r5.request(a, a.params, tool_name=solve.TOOL_NAME,
                      solve_context=changes.pop("solve_context", a.solve_context), **changes)


class Store:
    """Protocol double; PostgreSQL suite exercises the actual locks and transitions."""
    def __init__(self):
        self.exchange = None
        self.events = []

    def __getattr__(self, name):
        return getattr(real_store, name)

    def get_exchange(self, scope, lease):
        if self.exchange is None:
            raise self.ExchangeNotFound()
        return deepcopy(self.exchange)

    def prepare_exchange(self, scope, lease, **values):
        self.events.append("prepare")
        self.exchange = {**values, "state": "prepared", "org_id": scope.org_id,
            "project_id": scope.project_id, "drawing_id": scope.drawing_id, "job_id": scope.job_id,
            "response_bytes": None, "candidate_bytes": None, "proof_bytes": None,
            "grid_id": None, "send_protocol": None, "sent_at": None, "send_principal_sha256": None}
        return deepcopy(self.exchange)

    def resume_exchange(self, scope, lease, *, send_principal_sha256=None):
        state = self.exchange["state"]
        if state == "sending":
            if self.exchange["send_protocol"] == "async-idempotent-v1":
                if self.exchange["grid_id"] is not None:
                    return SimpleNamespace(action="poll_result", exchange=deepcopy(self.exchange))
                if datetime.now(timezone.utc) < self.exchange["sent_at"] + timedelta(hours=3):
                    if send_principal_sha256 != self.exchange["send_principal_sha256"]:
                        raise self.ExchangeConflict()
                    return SimpleNamespace(action="resubmit_same_key", exchange=deepcopy(self.exchange))
            self.record_outcome_unknown(scope, lease)
            state = "outcome_unknown"
        return SimpleNamespace(action={"prepared": "send_ready", "responded": "use_response"}.get(state, state),
                               exchange=deepcopy(self.exchange))

    def begin_send(self, scope, lease, **options):
        self.events.append("begin_send")
        assert self.exchange["state"] == "prepared"
        self.exchange["state"] = "sending"
        self.exchange.update(options, sender_worker=lease.worker_id, sender_attempt=lease.attempt)
        if options:
            self.exchange["sent_at"] = datetime.now(timezone.utc)
        return True

    def record_grid(self, scope, lease, grid_id):
        self.events.append("grid")
        assert self.exchange["state"] == "sending"
        assert self.exchange["send_protocol"] == "async-idempotent-v1"
        assert self.exchange["grid_id"] in (None, grid_id)
        self.exchange["grid_id"] = grid_id

    def capture_response(self, scope, lease, **values):
        self.events.append("capture")
        self.exchange.update(values, state="responded")

    def complete_exchange(self, scope, lease, **values):
        self.events.append("complete")
        self.exchange.update(values, state="completed")

    def refuse_exchange(self, scope, lease, *, reason_code):
        self.events.append("refuse")
        self.exchange.update(state="refused", refusal_code=reason_code)

    def record_outcome_unknown(self, scope, lease):
        self.exchange.update(state="outcome_unknown", refusal_code="SOLVE_OUTCOME_UNKNOWN")

    def read_completed_exchange(self, *args, conn):
        assert self.exchange["state"] == "completed"
        for name, bound in (("request", self.MAX_REQUEST_BYTES), ("response", self.MAX_RESPONSE_BYTES),
                            ("candidate", self.MAX_CANDIDATE_BYTES), ("proof", self.MAX_PROOF_BYTES)):
            real_store._validate_bytes(self.exchange[name + "_bytes"], self.exchange[name + "_sha256"], bound)
        return deepcopy(self.exchange)


@pytest.fixture
def world(admitted, case, monkeypatch):
    a = admitted
    assert request(a, preview=False)["status_code"] == 202
    execution = a.accepted[-1][-1]["execution_context"]
    a.row = {"job_id": str(uuid4()), "org_id": str(a.s.org), "project_id": str(a.s.project),
        "input_version_id": str(a.s.parent.version_id), "request_tenant_id": str(a.s.org),
        "kind": "run", "tool_name": solve.TOOL_NAME, "params": a.accepted[-1][4],
        "status": "running", "attempt": 1, "max_attempts": 3, "lease_owner": "sender",
        "execution_context": execution, "result": None, "provenance": None}
    a.store, a.guard, a.calls = Store(), SimpleNamespace(lost=False), []
    monkeypatch.setattr(solve, "_store", lambda: a.store)
    monkeypatch.setattr(solve, "SOLVE_CLASSIFICATION", "synthetic")
    monkeypatch.setattr(solve, "SOLVE_ASYNC_PROTOCOL_ENABLED", False)
    monkeypatch.setattr(service, "_job_context", lambda *args: a.context)
    monkeypatch.setattr(adapter, "_stored_job", lambda *args: deepcopy(a.row))
    def send(req, grant, *, preserve_uncertainty):
        assert preserve_uncertainty and a.store.events[-1] == "begin_send"
        assert a.grants[-1] == ("g1", str(a.s.org))
        a.calls.append((req, grant))
        return cloud.canonical_bytes(case[2])
    monkeypatch.setattr(cloud, "post_stringer", send)
    return a


def test_admission_both_entitlement_checks(admitted, monkeypatch):
    a, consulted = admitted, []
    monkeypatch.setattr(entitlements, "tool_required_capability", lambda tool:
        consulted.append("solve") or "solve")
    from leaf_platform import entitlements as stored
    monkeypatch.setattr(stored, "stored_job_entitlement_verdict", lambda org, kind:
        consulted.append((org, kind)) or (None, SimpleNamespace(tier="hosted_pro")))
    assert request(a, preview=False)["status_code"] == 202
    assert consulted == ["solve", (a.s.org, "run")]
    assert a.grants == [("g1", str(a.s.org))]
    context = a.accepted[-1][-1]["execution_context"]
    assert set(context) == {"schema", "authority_mode", "execution_path", "drawing_id",
        "actor_binding_id", "checkout_fence", "tool_manifest_sha256", "parent_intake_sha256",
        "initialized", "solve_binding"}
    assert "private-test-token" not in repr(a.accepted)


@pytest.mark.parametrize("field", ["proposals", "proofs", "split_state", "pieces", "row_indices"])
def test_split_precedes_schema(admitted, field):
    admitted.params = {field: [{}]}
    result = request(admitted, preview=False)
    assert result["content"]["error"]["reason_code"] == "SOLVE_SPLIT_UNSUPPORTED"
    assert not admitted.accepted


@pytest.mark.parametrize("code,status", [("cloud_auth_missing", 401), ("cloud_tenant_unauthorized", 403)])
def test_missing_or_foreign_grant_before_insert(admitted, monkeypatch, code, status):
    def refuse(*args):
        raise CloudError(code, status)
    monkeypatch.setattr(cloud, "resolve_grant", refuse)
    assert request(admitted, preview=False)["status_code"] == status
    assert not admitted.accepted


def test_solve_context_in_fingerprint(admitted, monkeypatch):
    a = admitted
    assert request(a, preview=False)["status_code"] == 202
    saved = a.accepted[-1][-1]
    monkeypatch.setattr(jobs, "get_project_graph_job_by_key", lambda *args, **kwargs:
        {"job_id": "saved", "deleted_at": None, **saved})
    assert request(a, preview=False)["content"]["job_id"] == "saved"
    changed = {**a.solve_context, "phase": "background"}
    result = request(a, solve_context=changed, preview=False)
    assert result["content"]["error"]["reason_code"] == "SIP_R4_IDEMPOTENCY_CONFLICT"


def test_split_admission_replays_same_principal_without_reinsertion(admitted, monkeypatch):
    from test_w1_solve_split import group_graph
    from solar_solve_request import build_stringer_request
    a = admitted
    value, _ = group_graph(a.context.graph, {"group_grid": [
        [f"generated-{r}-{c}" for c in range(16)] for r in range(16)]})
    value["settings"]["panels_in_sequence"] = 14
    value = ready_graph(value)
    a.s.set_parent(b3.embedded(value))
    a.params = {"grant_ref": "g1", "request": build_stringer_request(value,
        value["frames"][0]["id"], max_string_length=14, dwgname="synthetic-frame")}
    a.solve_context = solve_context(value)
    assert request(a, preview=False)["status_code"] == 202
    saved = a.accepted[-1][-1]
    inserted = len(a.accepted)
    monkeypatch.setattr(jobs, "get_project_graph_job_by_key", lambda *args, **kwargs:
        {"job_id": "saved-split", "deleted_at": None, **saved})
    assert request(a, preview=False)["content"]["job_id"] == "saved-split"
    assert len(a.accepted) == inserted


def test_background_requires_initial(admitted):
    result = request(admitted, solve_context={**admitted.solve_context, "phase": "background"})
    assert result["content"]["error"]["reason_code"] == "INITIAL_SOLVE_REQUIRED"
    assert not admitted.accepted


def test_not_ready_refused(admitted):
    value = admitted.context.graph
    value["settings"]["global_string_sizing_confirmed"] = False
    admitted.s.set_parent(b3.embedded(value))
    result = request(admitted)
    assert result["content"]["error"]["reason_code"] == "SIZED_PANEL_GROUPS_REQUIRED"


def test_not_ready_precedes_invalid_request(admitted):
    value = admitted.context.graph
    value["settings"]["global_string_sizing_confirmed"] = False
    admitted.s.set_parent(b3.embedded(value))
    admitted.params = {**admitted.params, "request": {"grid": {}}}
    result = request(admitted)
    reason_code = result["content"]["error"]["reason_code"]
    assert reason_code == "SIZED_PANEL_GROUPS_REQUIRED"
    assert reason_code != "CLOUD_REQUEST_INVALID"


def test_ready_invalid_request_still_refused(admitted):
    assert w1_graph_readiness(admitted.context.graph)[solve.TOOL_NAME]["input_ready"]
    params = {**admitted.params, "request": {"grid": {}}}
    with pytest.raises(CloudError) as exc:
        solve.plan_request(admitted.context, params, admitted.solve_context)
    assert exc.value.classification.upper() == "CLOUD_REQUEST_INVALID"


def test_route_forwards_context_and_derives_drawing(case, monkeypatch):
    # A closed catalog proposal carries no drawing_id; derive it from the verified version.
    for lane in routes.lane.__wrapped__(monkeypatch):
        monkeypatch.setattr(cloud, "resolve_grant", lambda ref, tenant: CloudGrant(tenant, "t1"))
        value = ready_graph(case[0])
        lane.s.set_parent(b3.embedded(value))
        body = {"tool": solve.TOOL_NAME, "dwg": str(lane.s.parent.version_id),
            "params": {"grant_ref": "g1", "request": case[1]},
            "solve_context": solve_context(value),
            "catalog_digest": deps.catalog_tool_digest(lane.tools[solve.TOOL_NAME])}
        response = lane.client.post("/api/run", json=body, headers=routes.headers(lane))
        assert response.status_code == 202, response.text
        bound = lane.a.accepted[-1][-1]["execution_context"]["solve_binding"]
        assert bound["frame_id"] == body["solve_context"]["frame_ref"]
        assert lane.a.accepted[-1][4].keys() == {"grant_ref", "request"}
        saved = lane.a.accepted[-1][-1]
        monkeypatch.setattr(jobs, "get_project_graph_job_by_key", lambda *args, **kwargs:
            {"job_id": str(lane.s.job), "deleted_at": None, **saved})
        replay = lane.client.post("/api/run", json=body, headers=routes.headers(lane))
        assert replay.status_code == 202 and replay.json()["job_id"] == str(lane.s.job)
        body["solve_context"]["phase"] = "background"
        conflict = lane.client.post("/api/run", json=body, headers=routes.headers(lane))
        assert conflict.status_code == 409, conflict.text
        assert conflict.json()["error"]["reason_code"] == "SIP_R4_IDEMPOTENCY_CONFLICT"


def test_exact_wire_and_nonpublishing_candidate(world):
    a = world
    before = deepcopy((a.s.versions, a.s.writes))
    result = solve.prepare_job(a.row, a.guard)
    assert len(a.calls) == 1 and len(a.grants) == 2
    assert a.store.events == ["prepare", "begin_send", "capture", "complete"]
    assert a.store.exchange["request_bytes"] == cloud.canonical_bytes(a.calls[0][0].wire_payload())
    assert result.keys() == {"schema", "candidate_sha256", "request_sha256", "response_sha256",
        "frame_id", "phase", "candidate_summary", "execution_provenance"}
    assert sum(row["panel_count"] for row in result["candidate_summary"]) == len(a.context.graph["panels"])
    assert all(len(row["endpoints"]) == 2 for row in result["candidate_summary"])
    assert (a.s.versions, a.s.writes) == before
    proof = solve.project_solve_job_provenance(UUID(a.row["job_id"]), result, conn=None)
    assert proof["candidate"]["scope"]["input_version_id"] == a.row["input_version_id"]


def test_captured_response_reused_after_crash(world, monkeypatch):
    a = world
    complete = a.store.complete_exchange
    monkeypatch.setattr(a.store, "complete_exchange", lambda *args, **kwargs:
        (_ for _ in ()).throw(RuntimeError("crash after capture")))
    with pytest.raises(RuntimeError):
        solve.prepare_job(a.row, a.guard)
    assert a.store.exchange["state"] == "responded"
    a.row.update(attempt=2, lease_owner="reclaimer")
    monkeypatch.setattr(a.store, "complete_exchange", complete)
    assert solve.prepare_job(a.row, a.guard)["schema"] == solve.RESULT_SCHEMA
    assert len(a.calls) == 1


def test_lost_sender_never_resends(world, monkeypatch):
    a = world
    def lost(*args, **kwargs):
        a.calls.append("sent")
        raise RuntimeError("crash after begin_send")
    monkeypatch.setattr(cloud, "post_stringer", lost)
    assert solve.prepare_job(a.row, a.guard) is None
    a.row.update(attempt=2, lease_owner="reclaimer")
    with pytest.raises(CloudError) as exc:
        solve.prepare_job(a.row, a.guard)
    assert exc.value.classification == "solve_outcome_unknown"
    assert a.calls == ["sent"] and a.store.exchange["state"] == "outcome_unknown"


@pytest.mark.parametrize("code", ["cloud_upstream_failure", "cloud_auth_missing"])
def test_known_prewrite_failure_refuses(world, monkeypatch, code):
    def failed(*args, **kwargs):
        raise CloudError(code, 502)
    monkeypatch.setattr(cloud, "post_stringer", failed)
    with pytest.raises(CloudError):
        solve.prepare_job(world.row, world.guard)
    assert world.store.events == ["prepare", "begin_send", "refuse"]
    assert world.store.exchange["state"] == "refused"
    assert world.store.exchange["refusal_code"] == code


def test_grant_revoked_before_send(world, monkeypatch):
    def missing(*args):
        raise CloudError("cloud_auth_missing", 401)
    monkeypatch.setattr(cloud, "resolve_grant", missing)
    with pytest.raises(CloudError):
        solve.prepare_job(world.row, world.guard)
    assert world.store.events == ["prepare", "refuse"] and not world.calls


@pytest.mark.parametrize("failure,expected", [
    (requests.ConnectTimeout(), "cloud_upstream_failure"),
    (requests.ConnectionError(ConnectionRefusedError()), "cloud_upstream_failure"),
    (requests.ReadTimeout(), "solve_outcome_unknown"),
    (requests.ConnectionError(ConnectionResetError()), "solve_outcome_unknown"),
])
def test_transport_delivery_classification(monkeypatch, failure, expected):
    # The case fixture replaces post_stringer; exercise the real transport here.
    request = cloud.StringerRequest.model_validate({"grid": {
        "Dwgname": "transport-test", "Sequences": [[1, 0], [1, 0]], "Modify": [],
        "Rows": [{"Panels": [{"Code": 1, "Id": "panel-1", "Seq": 0,
            "InverterId": -1, "StringInputNumber": 0, "X": 0.0, "Y": 0.0, "Angle": 0.0}]}]}})
    def fail(*args, **kwargs):
        raise failure
    monkeypatch.setattr(requests, "post", fail)
    with pytest.raises(CloudError) as exc:
        cloud.post_stringer(request, CloudGrant("tenant", ""),
                            preserve_uncertainty=True)
    assert exc.value.classification == expected
    with pytest.raises(CloudError) as exc:
        cloud.post_stringer(request, CloudGrant("tenant", ""))
    assert exc.value.classification == "cloud_upstream_failure"


@pytest.mark.parametrize("field", ["request_bytes", "request_sha256", "response_bytes", "response_sha256",
                                    "candidate_bytes", "candidate_sha256", "proof_bytes", "proof_sha256"])
def test_offline_provenance_rejects_tampering(world, monkeypatch, field):
    result = solve.prepare_job(world.row, world.guard)
    def forbidden(*args, **kwargs):
        pytest.fail("provenance performed network or credential IO")
    monkeypatch.setattr(cloud, "post_stringer", forbidden)
    monkeypatch.setattr(cloud, "resolve_grant", forbidden)
    solve.project_solve_job_provenance(UUID(world.row["job_id"]), result, conn=None)
    value = world.store.exchange[field]
    world.store.exchange[field] = value + b" " if type(value) is bytes else "0" * 64
    with pytest.raises((real_store.SolveExchangeError, GraphValidationError)):
        solve.project_solve_job_provenance(UUID(world.row["job_id"]), result, conn=None)


def test_completed_exchange_reclaimed_without_transport(world, monkeypatch):
    first = solve.prepare_job(world.row, world.guard)
    world.row.update(attempt=2, lease_owner="reclaimer")
    second = solve.prepare_job(world.row, world.guard)
    assert first["candidate_sha256"] == second["candidate_sha256"]
    assert second["execution_provenance"]["attempt"] == 2 and len(world.calls) == 1


def test_invalid_captured_response_refused(world, monkeypatch):
    monkeypatch.setattr(cloud, "post_stringer", lambda *args, **kwargs: b"{}")
    with pytest.raises(CloudError):
        solve.prepare_job(world.row, world.guard)
    assert world.store.exchange["state"] == "refused"
    assert world.store.events[-2:] == ["capture", "refuse"]


def test_lease_loss_before_send_prevents_transport(world):
    world.guard.lost = True
    assert solve.prepare_job(world.row, world.guard) is None
    assert world.store.events == ["prepare"] and not world.calls


def test_execution_context_keys_are_tool_conditional(world):
    proposal = deepcopy(world.row)
    expected = jobs._graph_ids(proposal)
    seeded = deepcopy(proposal)
    seeded["tool_name"] = "solar-settings"
    seeded["execution_context"].pop("solve_binding")
    seeded["execution_context"]["initialized"] = True
    seeded["params"] = {"initialize": {}}
    assert jobs._graph_ids(seeded) == expected
    # Commit's distinct trusted_proposal context is covered by W1C-3.
    for tool in adapter.SUPPORTED_TOOLS - {solve.TOOL_NAME, solve.COMMIT_TOOL}:
        row = deepcopy(proposal)
        row["tool_name"] = tool
        del row["execution_context"]["solve_binding"]
        assert jobs._graph_ids(row) == expected
        row["execution_context"]["solve_binding"] = proposal["execution_context"]["solve_binding"]
        with pytest.raises(adapter.project.ProjectContextError, match="SIP_R4_JOB_BINDING_MISMATCH"):
            jobs._graph_ids(row)
    for change in ("missing", "trusted_proposal", "foreign"):
        row = deepcopy(proposal)
        if change == "missing":
            del row["execution_context"]["solve_binding"]
        else:
            row["execution_context"][change] = {}
        with pytest.raises(adapter.project.ProjectContextError, match="SIP_R4_JOB_BINDING_MISMATCH"):
            jobs._graph_ids(row)


@pytest.fixture
def async_world(world, case, monkeypatch):
    a = world
    token = "e30." + base64.urlsafe_b64encode(b'{"iss":"issuer","sub":"solver"}').decode().rstrip("=") + ".sig"
    monkeypatch.setattr(cloud, "resolve_grant", lambda *args: CloudGrant(str(a.s.org), token))
    monkeypatch.setattr(solve, "SOLVE_ASYNC_PROTOCOL_ENABLED", True)
    monkeypatch.setattr(solve, "SOLVE_POLL_BUDGET_SECONDS", 600)
    monkeypatch.setattr(solve, "SOLVE_POLL_INTERVAL_SECONDS", 5)
    a.clock = SimpleNamespace(now=0.0)
    def sleep(seconds):
        a.clock.now += seconds
    monkeypatch.setattr(solve, "time", SimpleNamespace(monotonic=lambda: a.clock.now, sleep=sleep))
    a.raw = cloud.canonical_bytes(case[2])
    a.grid_id = UUID(case[2]["job_id"])
    a.submits, a.polls = [], []
    a.submit_results = [("accepted", a.grid_id)]
    a.poll_results = [("completed", a.raw)]
    def submit(req, grant, key, *, remaining_seconds):
        assert a.store.exchange["state"] == "sending"
        assert a.store.exchange["send_protocol"] == "async-idempotent-v1"
        assert 0 < remaining_seconds <= solve.SOLVE_POLL_BUDGET_SECONDS
        a.submits.append(key)
        return a.submit_results.pop(0)
    def poll(grid_id, grant, *, remaining_seconds):
        assert a.store.exchange["grid_id"] == grid_id == a.grid_id
        a.polls.append(grid_id)
        return a.poll_results.pop(0)
    monkeypatch.setattr(cloud, "submit_stringer_async", submit)
    monkeypatch.setattr(cloud, "get_stringer_result", poll)
    monkeypatch.setattr(cloud, "post_stringer", lambda *args, **kwargs: pytest.fail("blocking async send"))
    return a


def test_async_accepted_grid_precedes_poll_and_proposal(async_world):
    a = async_world
    a.poll_results.insert(0, ("pending", None))
    assert solve.prepare_job(a.row, a.guard)["schema"] == solve.RESULT_SCHEMA
    assert a.store.events == ["prepare", "begin_send", "grid", "capture", "complete"]
    assert a.submits == [a.row["job_id"]] and a.polls == [a.grid_id, a.grid_id]
    assert a.clock.now == 5


def test_async_submit_accepts_blocking_manager_completed_body(async_world):
    a = async_world
    a.submit_results = [("completed", a.raw)]
    assert solve.prepare_job(a.row, a.guard)["schema"] == solve.RESULT_SCHEMA
    assert not a.polls and a.store.exchange["grid_id"] is None


def test_async_busy_retries_same_key(async_world):
    a = async_world
    a.submit_results.insert(0, ("busy", 12))
    assert solve.prepare_job(a.row, a.guard)["schema"] == solve.RESULT_SCHEMA
    assert a.submits == [a.row["job_id"]] * 2 and a.clock.now == 12


def test_async_postwrite_timeout_retries_exactly_one_key(async_world, monkeypatch):
    a = async_world
    # Exercise the real client mapping too: a write can precede the timeout.
    keys = []
    class Accepted:
        status_code = 202
        def __enter__(self):
            return self
        def __exit__(self, *args):
            return False
        def iter_content(self, size):
            yield cloud.canonical_bytes({"status": "accepted", "grid_id": str(a.grid_id)})
    def post(url, **kwargs):
        keys.append(kwargs["headers"]["Idempotency-Key"])
        if len(keys) == 1:
            raise requests.ReadTimeout("write already sent")
        return Accepted()
    monkeypatch.setattr(cloud.requests, "post", post)
    # The fixture installed a submit double. Bind the production transport seam.
    monkeypatch.setattr(cloud, "submit_stringer_async", lambda req, grant, key, **kwargs:
        cloud._async_stringer_call(cloud.requests.post, cloud.SOLVER_URL, grant,
            request=req, idempotency_key=key, **kwargs))
    assert solve.prepare_job(a.row, a.guard)["schema"] == solve.RESULT_SCHEMA
    assert keys == [a.row["job_id"]] * 2 and len(set(keys)) == 1


@pytest.mark.parametrize("status", ["expired", "missing"])
def test_async_unavailable_result_refused(async_world, status):
    a = async_world
    a.poll_results = [(status, None)]
    with pytest.raises(CloudError, match="^cloud_upstream_failure$"):
        solve.prepare_job(a.row, a.guard)
    assert a.store.exchange["state"] == "refused"
    assert a.store.exchange["refusal_code"] == "cloud_upstream_failure"


def test_async_budget_then_resumed_attempt_polls_even_with_flag_off(async_world, monkeypatch):
    a = async_world
    monkeypatch.setattr(solve, "SOLVE_POLL_BUDGET_SECONDS", 5)
    a.poll_results = [("pending", None)]
    assert solve.prepare_job(a.row, a.guard) is None
    before = deepcopy(a.store.exchange)
    assert before["state"] == "sending" and before["grid_id"] == a.grid_id
    a.row.update(attempt=2, lease_owner="reclaimer")
    monkeypatch.setattr(solve, "SOLVE_ASYNC_PROTOCOL_ENABLED", False)
    a.poll_results = [("completed", a.raw)]
    assert solve.prepare_job(a.row, a.guard)["schema"] == solve.RESULT_SCHEMA
    assert a.submits == [a.row["job_id"]]
    assert a.store.exchange["sender_worker"] == before["sender_worker"]
    assert a.store.exchange["sender_attempt"] == 1


@pytest.mark.parametrize("code", ["cloud_auth_missing", "cloud_upstream_failure"])
def test_async_grant_failure_preserves_grid_for_next_attempt(async_world, monkeypatch, code):
    a = async_world
    monkeypatch.setattr(solve, "SOLVE_POLL_BUDGET_SECONDS", 5)
    a.poll_results = [("pending", None)]
    assert solve.prepare_job(a.row, a.guard) is None
    before = deepcopy(a.store.exchange)
    resolve = cloud.resolve_grant
    def failed(*args):
        raise CloudError(code, 502)
    monkeypatch.setattr(cloud, "resolve_grant", failed)
    a.row.update(attempt=2, lease_owner="reclaimer")
    with pytest.raises(CloudError, match="^" + code + "$"):
        solve.prepare_job(a.row, a.guard)
    assert a.store.exchange == before
    assert before["state"] == "sending" and before["grid_id"] == a.grid_id
    monkeypatch.setattr(cloud, "resolve_grant", resolve)
    a.row.update(attempt=3, lease_owner="next-reclaimer")
    a.poll_results = [("completed", a.raw)]
    assert solve.prepare_job(a.row, a.guard)["schema"] == solve.RESULT_SCHEMA
    assert a.submits == [a.row["job_id"]]
    assert a.polls == [a.grid_id, a.grid_id]


def test_async_principal_read_failure_preserves_custody(async_world, monkeypatch):
    a = async_world
    submit = cloud.submit_stringer_async
    monkeypatch.setattr(cloud, "submit_stringer_async", lambda *args, **kwargs:
        (_ for _ in ()).throw(RuntimeError("lost handle")))
    assert solve.prepare_job(a.row, a.guard) is None
    before = deepcopy(a.store.exchange)
    principal = cloud.grant_principal_sha256
    def failed(*args):
        raise CloudError("cloud_auth_missing", 401)
    monkeypatch.setattr(cloud, "grant_principal_sha256", failed)
    a.row.update(attempt=2, lease_owner="reclaimer")
    with pytest.raises(CloudError, match="^cloud_auth_missing$"):
        solve.prepare_job(a.row, a.guard)
    assert a.store.exchange == before
    assert before["state"] == "sending" and before["grid_id"] is None
    monkeypatch.setattr(cloud, "grant_principal_sha256", principal)
    monkeypatch.setattr(cloud, "submit_stringer_async", submit)
    a.row.update(attempt=3, lease_owner="next-reclaimer")
    assert solve.prepare_job(a.row, a.guard)["schema"] == solve.RESULT_SCHEMA
    assert a.submits == [a.row["job_id"]]


@pytest.mark.parametrize("code", ["cloud_auth_missing", "solve_outcome_unknown", "cloud_piece_failed"])
def test_async_poll_error_preserves_grid_for_next_attempt(async_world, monkeypatch, code):
    a = async_world
    poll = cloud.get_stringer_result
    def failed(grid_id, grant, **kwargs):
        assert a.store.exchange["grid_id"] == grid_id == a.grid_id
        raise CloudError(code, 502)
    monkeypatch.setattr(cloud, "get_stringer_result", failed)
    with pytest.raises(CloudError, match="^" + code + "$"):
        solve.prepare_job(a.row, a.guard)
    assert a.store.exchange["state"] == "sending"
    assert a.store.exchange["grid_id"] == a.grid_id
    assert a.store.events == ["prepare", "begin_send", "grid"]
    monkeypatch.setattr(cloud, "get_stringer_result", poll)
    a.row.update(attempt=2, lease_owner="reclaimer")
    assert solve.prepare_job(a.row, a.guard)["schema"] == solve.RESULT_SCHEMA
    assert a.submits == [a.row["job_id"]] and a.polls == [a.grid_id]


@pytest.mark.parametrize("code", ["cloud_request_invalid", "cloud_response_invalid"])
def test_async_definitive_poll_error_refuses(async_world, monkeypatch, code):
    def failed(*args, **kwargs):
        raise CloudError(code, 502)
    monkeypatch.setattr(cloud, "get_stringer_result", failed)
    with pytest.raises(CloudError, match="^" + code + "$"):
        solve.prepare_job(async_world.row, async_world.guard)
    assert async_world.store.exchange["state"] == "refused"
    assert async_world.store.exchange["refusal_code"] == code


def test_async_resubmit_same_key_after_no_handle_crash(async_world, monkeypatch):
    a = async_world
    submit = cloud.submit_stringer_async
    monkeypatch.setattr(cloud, "submit_stringer_async", lambda *args, **kwargs:
        (_ for _ in ()).throw(RuntimeError("crash after write before grid custody")))
    assert solve.prepare_job(a.row, a.guard) is None
    assert a.store.exchange["state"] == "sending" and a.store.exchange["grid_id"] is None
    a.row.update(attempt=2, lease_owner="reclaimer")
    monkeypatch.setattr(cloud, "submit_stringer_async", submit)
    assert solve.prepare_job(a.row, a.guard)["schema"] == solve.RESULT_SCHEMA
    assert a.submits == [a.row["job_id"]]


def test_flag_on_does_not_retry_legacy_sending(world, monkeypatch):
    a = world
    monkeypatch.setattr(cloud, "post_stringer", lambda *args, **kwargs:
        (_ for _ in ()).throw(RuntimeError("legacy sender crashed")))
    assert solve.prepare_job(a.row, a.guard) is None
    monkeypatch.setattr(solve, "SOLVE_ASYNC_PROTOCOL_ENABLED", True)
    monkeypatch.setattr(cloud, "submit_stringer_async", lambda *args, **kwargs:
        pytest.fail("legacy custody must never resubmit"))
    a.row.update(attempt=2, lease_owner="reclaimer")
    with pytest.raises(CloudError, match="^solve_outcome_unknown$"):
        solve.prepare_job(a.row, a.guard)
    assert a.store.exchange["send_protocol"] is None


def test_async_principal_change_prevents_resubmission(async_world, monkeypatch):
    a = async_world
    monkeypatch.setattr(cloud, "submit_stringer_async", lambda *args, **kwargs:
        (_ for _ in ()).throw(RuntimeError("lost handle")))
    assert solve.prepare_job(a.row, a.guard) is None
    token = "e30." + base64.urlsafe_b64encode(b'{"iss":"issuer","sub":"different"}').decode().rstrip("=") + ".sig"
    monkeypatch.setattr(cloud, "resolve_grant", lambda *args: CloudGrant(str(a.s.org), token))
    with pytest.raises(CloudError, match="^cloud_tenant_unauthorized$"):
        solve.prepare_job(a.row, a.guard)
    assert a.store.exchange["state"] == "refused" and not a.submits


def test_async_no_handle_after_retention_cutoff_is_unknown(async_world, monkeypatch):
    a = async_world
    monkeypatch.setattr(cloud, "submit_stringer_async", lambda *args, **kwargs:
        (_ for _ in ()).throw(RuntimeError("lost handle")))
    assert solve.prepare_job(a.row, a.guard) is None
    a.store.exchange["sent_at"] -= timedelta(hours=3)
    with pytest.raises(CloudError, match="^solve_outcome_unknown$"):
        solve.prepare_job(a.row, a.guard)
    assert a.store.exchange["state"] == "outcome_unknown" and not a.submits


def test_async_lease_loss_stops_polling(async_world, monkeypatch):
    a = async_world
    def lost(grid_id, grant, **kwargs):
        a.polls.append(grid_id)
        a.guard.lost = True
        return "pending", None
    monkeypatch.setattr(cloud, "get_stringer_result", lost)
    assert solve.prepare_job(a.row, a.guard) is None
    assert a.polls == [a.grid_id] and a.store.exchange["state"] == "sending"


def test_async_lease_loss_between_polls_leaves_grid_custody(async_world, monkeypatch):
    a = async_world
    a.poll_results = [("pending", None)]
    def sleep(seconds):
        a.clock.now += seconds
        a.guard.lost = True
    monkeypatch.setattr(solve.time, "sleep", sleep)
    assert solve.prepare_job(a.row, a.guard) is None
    assert a.polls == [a.grid_id]
    assert a.store.exchange["state"] == "sending"
    assert a.store.exchange["grid_id"] == a.grid_id
    assert "capture" not in a.store.events and "refuse" not in a.store.events


def test_async_retention_cutoff_is_rechecked_before_retry(async_world, monkeypatch):
    a = async_world
    a.submit_results = [("retryable", 5)]
    def sleep(seconds):
        a.clock.now += seconds
        a.store.exchange["sent_at"] -= timedelta(hours=3)
    monkeypatch.setattr(solve.time, "sleep", sleep)
    with pytest.raises(CloudError, match="^solve_outcome_unknown$"):
        solve.prepare_job(a.row, a.guard)
    assert a.submits == [a.row["job_id"]]
    assert a.store.exchange["state"] == "outcome_unknown"
    assert a.store.exchange["grid_id"] is None and not a.polls


def test_flag_off_read_timeout_keeps_single_blocking_post(world, monkeypatch):
    a = world
    calls = []
    def post(*args, **kwargs):
        calls.append(kwargs)
        raise requests.ReadTimeout()
    monkeypatch.setattr(cloud.requests, "post", post)
    monkeypatch.setattr(cloud, "post_stringer", lambda request, grant, **kwargs:
        _blocking_transport(request, grant, **kwargs))
    with pytest.raises(CloudError, match="^solve_outcome_unknown$"):
        solve.prepare_job(a.row, a.guard)
    assert len(calls) == 1
    assert "Idempotency-Key" not in calls[0]["headers"]
    assert a.store.exchange["send_protocol"] is None
    assert a.store.exchange["state"] == "outcome_unknown"


# X02: real staging-only header and durable single-send fault custody.
def _fault_staging(monkeypatch):
    import xbench_solver_fault as fault
    monkeypatch.setattr(fault.runtime_posture, "_deployed_runtime", lambda: True)
    monkeypatch.setattr(fault.runtime_posture, "_production_runtime", lambda: False)
    monkeypatch.setattr(fault.deployment_identity, "deployment_identity",
                        lambda: {"environment": "staging"})
    return fault


@pytest.mark.parametrize("mode", ["upstream-400", "timeout"])
def test_x02_fault_requires_both_runtime_and_deployment_identity(monkeypatch, mode):
    fault = _fault_staging(monkeypatch)
    assert fault.validate(mode) == mode
    monkeypatch.setattr(fault.runtime_posture, "_production_runtime", lambda: True)
    with pytest.raises(GraphValidationError, match="XBENCH_SOLVER_FAULT_UNAVAILABLE"):
        fault.validate(mode)
    monkeypatch.setattr(fault.runtime_posture, "_production_runtime", lambda: False)
    monkeypatch.setattr(fault.runtime_posture, "_deployed_runtime", lambda: False)
    with pytest.raises(GraphValidationError, match="XBENCH_SOLVER_FAULT_UNAVAILABLE"):
        fault.validate(mode)
    monkeypatch.setattr(fault.runtime_posture, "_deployed_runtime", lambda: True)
    monkeypatch.setattr(fault.deployment_identity, "deployment_identity",
                        lambda: {"environment": "production"})
    with pytest.raises(GraphValidationError, match="XBENCH_SOLVER_FAULT_UNAVAILABLE"):
        fault.validate(mode)
    assert fault.validate(None) is None


@pytest.mark.parametrize("mode", [True, "", "TIMEOUT", "upstream-500", {}, []])
def test_x02_fault_mode_is_closed(monkeypatch, mode):
    fault = _fault_staging(monkeypatch)
    with pytest.raises(GraphValidationError, match="XBENCH_SOLVER_FAULT_INVALID"):
        fault.validate(mode)


def test_x02_fault_context_resets_after_exception(monkeypatch):
    fault = _fault_staging(monkeypatch)
    assert fault.current_mode() is None
    with pytest.raises(RuntimeError):
        with fault.request_mode("timeout"):
            assert fault.current_mode() == "timeout"
            with fault.request_mode("upstream-400"):
                assert fault.current_mode() == "upstream-400"
            assert fault.current_mode() == "timeout"
            raise RuntimeError("synthetic scope exit")
    assert fault.current_mode() is None


def test_x02_fault_in_durable_identity_and_replay(admitted, monkeypatch):
    fault = _fault_staging(monkeypatch)
    with fault.request_mode("timeout"):
        assert request(admitted, preview=False)["status_code"] == 202
    saved = admitted.accepted[-1][-1]
    assert saved["execution_context"]["xbench_solver_fault"] == "timeout"
    monkeypatch.setattr(jobs, "get_project_graph_job_by_key", lambda *args, **kwargs:
        {"job_id": "saved-fault", "deleted_at": None, **saved})
    count = len(admitted.accepted)
    with fault.request_mode("timeout"):
        assert request(admitted, preview=False)["content"]["job_id"] == "saved-fault"
    assert len(admitted.accepted) == count
    with fault.request_mode("upstream-400"):
        conflict = request(admitted, preview=False)
    assert conflict["content"]["error"]["reason_code"] == "SIP_R4_IDEMPOTENCY_CONFLICT"
    assert request(admitted, preview=False)["content"]["error"]["reason_code"] == "SIP_R4_IDEMPOTENCY_CONFLICT"


@pytest.mark.parametrize("mode,cause,state", [("upstream-400", "CLOUD_REQUEST_REJECTED", "refused"),
                                              ("timeout", "solve_outcome_unknown", "outcome_unknown")])
def test_x02_fault_real_adapter_and_reconcile_no_second_send(world, monkeypatch, mode, cause, state):
    fault = _fault_staging(monkeypatch)
    a = world
    a.row["execution_context"]["xbench_solver_fault"] = mode
    jobs._graph_ids(a.row)  # The real closed durable-context validator admits only the enum.
    monkeypatch.setattr(solve, "SOLVE_ASYNC_PROTOCOL_ENABLED", True)
    monkeypatch.setattr(cloud, "post_stringer", _blocking_transport)
    monkeypatch.setattr(requests, "post", lambda *args, **kwargs:
                        pytest.fail("fault must not contact a provider"))
    calls = []
    real_post = fault.Transport.post
    def post(self, *args, **kwargs):
        assert a.store.exchange["state"] == "sending"
        calls.append(self.mode)
        return real_post(self, *args, **kwargs)
    monkeypatch.setattr(fault.Transport, "post", post)
    before = deepcopy((a.s.versions, a.s.writes))
    for attempt in [1, 2]:
        a.row["attempt"] = attempt
        with pytest.raises(CloudError) as exc:
            solve.prepare_job(a.row, a.guard)
        assert exc.value.classification == cause
        assert service._preparation_error(exc.value)["reason_code"] == cause
    assert a.store.exchange["state"] == state
    assert a.store.exchange["classification"] == "synthetic"
    assert a.store.exchange["send_protocol"] is None
    assert calls == [mode] and a.store.events.count("begin_send") == 1
    assert (a.s.versions, a.s.writes) == before and not a.calls


def test_x02_stored_fault_cannot_activate_in_production(world, monkeypatch):
    fault = _fault_staging(monkeypatch)
    world.row["execution_context"]["xbench_solver_fault"] = "timeout"
    monkeypatch.setattr(fault.runtime_posture, "_production_runtime", lambda: True)
    with pytest.raises(GraphValidationError, match="XBENCH_SOLVER_FAULT_UNAVAILABLE"):
        solve.prepare_job(world.row, world.guard)
    assert world.store.events == [] and not world.calls


@pytest.mark.parametrize("value", [True, "invalid", None])
def test_x02_durable_fault_context_rejects_invalid_enum(world, value):
    world.row["execution_context"]["xbench_solver_fault"] = value
    from leaf_platform.project_graph_store import ProjectContextError
    with pytest.raises(ProjectContextError, match="SIP_R4_JOB_BINDING_MISMATCH"):
        jobs._graph_ids(world.row)


@pytest.mark.parametrize("auth,mode,status", [(True, "timeout", 202),
    (False, "timeout", 403), (True, "invalid", 403)])
def test_x02_header_route_preserves_auth_and_durable_context(case, monkeypatch, auth, mode, status):
    fault = _fault_staging(monkeypatch)
    for lane in routes.lane.__wrapped__(monkeypatch):
        monkeypatch.setattr(deps, "auth_live", lambda: auth)
        monkeypatch.setattr(cloud, "resolve_grant", lambda ref, tenant: CloudGrant(tenant, "t1"))
        value = ready_graph(case[0]);lane.s.set_parent(b3.embedded(value))
        body = {"tool": solve.TOOL_NAME, "dwg": str(lane.s.parent.version_id),
            "params": {"grant_ref": "g1", "request": case[1]}, "solve_context": solve_context(value),
            "catalog_digest": deps.catalog_tool_digest(lane.tools[solve.TOOL_NAME])}
        response = lane.client.post("/api/run", json=body,
            headers={**routes.headers(lane), fault.HEADER: mode})
        assert response.status_code == status, response.text
        if status == 202:
            assert lane.a.accepted[-1][-1]["execution_context"]["xbench_solver_fault"] == mode
        else:
            assert not lane.a.accepted
        assert fault.current_mode() is None


@pytest.mark.parametrize("kind", [
    "production", "local", "missing_project", "conversation", "wrong_tool",
    "unknown_tool", "invalid_identity",
])
def test_x02_fault_header_cannot_escape_scope(case, monkeypatch, kind):
    fault = _fault_staging(monkeypatch)
    for lane in routes.lane.__wrapped__(monkeypatch):
        monkeypatch.setattr(deps, "auth_live", lambda: True)
        body = {"tool": solve.TOOL_NAME, "dwg": str(lane.s.parent.version_id),
                "params": {}, "catalog_digest": deps.catalog_tool_digest(lane.tools[solve.TOOL_NAME])}
        headers = {**routes.headers(lane), fault.HEADER: "timeout"}
        if kind == "production":
            monkeypatch.setattr(fault.runtime_posture, "_production_runtime", lambda: True)
        elif kind == "local":
            monkeypatch.setattr(fault.runtime_posture, "_deployed_runtime", lambda: False)
        elif kind == "missing_project":
            headers = {key: value for key, value in headers.items() if key.lower() != "x-project-id"}
        elif kind == "conversation":
            headers.update({"X-Authority-Session-Id": "session", "X-Authority-Turn-Id": "turn"})
        elif kind == "wrong_tool":
            body["tool"] = "solar-settings"
        elif kind == "unknown_tool":
            body["tool"] = "xbench-unknown-tool"
        else:
            monkeypatch.setattr(fault.deployment_identity, "deployment_identity", lambda:
                (_ for _ in ()).throw(ValueError("unattested deployment")))
        response = lane.client.post("/api/run", json=body, headers=headers)
        assert response.status_code == 403, response.text
        if kind in ("wrong_tool", "unknown_tool"):
            assert response.json()["error"]["reason_code"] == "XBENCH_SOLVER_FAULT_SCOPE_REQUIRED"
        assert not lane.a.accepted and fault.current_mode() is None


# Read-only saved-child assembly attribution.
ASSEMBLY_ORG, ASSEMBLY_PROJECT, ASSEMBLY_DRAWING, ASSEMBLY_VERSION, ASSEMBLY_JOB, ASSEMBLY_ACTOR = [UUID(int=n) for n in range(1, 7)]
ASSEMBLY_SOURCE = "a" * 40


def assembly_native_response(request):
    """Hand-built producer response, with genuine wire echo and visited topology."""
    model = cloud.PieceStringerRequest.model_validate(request)
    final = model.wire_payload()["grid"]
    path = []
    for r, row in enumerate(final["Rows"], 1):
        columns = range(len(row["Panels"])) if r % 2 else reversed(range(len(row["Panels"])))
        for c in columns:
            cell = row["Panels"][c]
            if cell["Code"] == 1:
                path.append([r, c + 1])
                cell["Seq"] = len(path)
    lengths, counts = model.grid.Sequences
    sequence = [length for length, count in zip(lengths, counts) for _ in range(count)]
    info = {name: 0 for name in cloud.StringerInfo.model_fields}
    info.update(steps_taken=len(path), sequence_length=sequence, num_panels=len(path),
                visited_path=path, terminated=True, allow_one_gap_hops=False,
                last_agent_x=path[-1][0], last_agent_y=path[-1][1])
    return {"status": "completed", "job_id": "synthetic-grid", "data": {
        "status": "completed", "best_result": {"last_action": 0, "terminated": True,
            "info": info, "beam_idx": 0, "grid_id": "synthetic-grid", "model_id": "synthetic",
            "gumbel_scale": 0, "string_start_split_count": 0, "model": "synthetic"},
        "final_grid": final, "model_used": "synthetic", "gumbel_scale_used": 0,
        "distance_total": 0, "total_valid_solutions": 1, "gumbel_summary": {},
        "first_pass_best_distance": 0, "improvement": None, "second_pass_triggered": False}}


class AssemblyReadStore:
    """Two scoped tables behind the real bounded SELECT projection."""
    def __init__(self, parent, children):
        self.parent = parent
        self.children = children
        self.queries = []
        self.oversized = False

    def cursor(self):
        return self

    def __enter__(self):
        return self

    def __exit__(self, *args):
        pass

    def execute(self, sql, args):
        assert sql.startswith("SELECT ") and "FOR UPDATE" not in sql
        assert args["org_id"] == ASSEMBLY_ORG and args["project_id"] == ASSEMBLY_PROJECT
        assert args["drawing_id"] == ASSEMBLY_DRAWING and args["job_id"] == ASSEMBLY_JOB
        self.queries.append(sql)
        if sql.startswith("SELECT count"):
            rows = [self.parent, *self.children]
            size = sum(len(row[key] or b"") for row in rows for key in (
                "request_bytes", "response_bytes", "candidate_bytes", "proof_bytes"))
            if self.oversized:
                size = 16 * assembly_store.MAX_RESPONSE_BYTES + 1
            self.value = {"count": len(rows), "bytes": size}
        elif "project_solve_piece_exchanges" in sql:
            self.value = deepcopy(self.children)
        else:
            self.value = deepcopy(self.parent)

    def fetchone(self):
        return self.value

    def fetchall(self):
        return self.value


@pytest.fixture
def assembly_world(graph, monkeypatch):
    frame = graph["frames"][0]
    template = graph["panels"][0]
    panels, matrix = [], []
    for r in range(15):
        row = []
        for c in range(14):
            n = r * 14 + c
            ref = app_id("panel", n + 1) if n < 201 else None
            if ref:
                panel = deepcopy(template)
                panel.update(id=ref, centre=[float(c), float(r * 2)], matrix_cell={"row": r, "col": c},
                             assignment={"string_ref": None, "seq": None})
                panels.append(panel)
            row.append({"code": "panel" if ref else "empty", "panel_ref": ref,
                        "seq": None, "inverter_id": None, "string_input_number": None,
                        "x": float(c), "y": float(r * 2), "angle": 0})
        matrix.append(row)
    frame.update(panel_refs=[p["id"] for p in panels], matrix=matrix, module_rows=15,
                 module_columns=14, module_slots=210, sequences=[], electrical_zone_ref=None,
                 panel_assignments=[{"panel_ref": p["id"], "string_ref": None, "seq": None,
                                     "inverter_id": None, "string_input_number": None} for p in panels])
    graph.update(panels=panels, strings=[], inverters=[], electrical_zones=[], routes=[], schedules=[])
    graph["settings"].update(panels_in_sequence=14, voc_cold=sizing.manual_voltage())
    graph["settings"]["extra"]["string_sizing"] = sizing.manual_sizing_evidence(graph)
    validate_graph(graph)
    intake = {"solar_design_graph": graph, "solar_design_graph_sha256": solve.kernel.digest(graph)}
    raw = cloud.canonical_bytes(intake)
    version = SimpleNamespace(org_id=ASSEMBLY_ORG, project_id=ASSEMBLY_PROJECT, drawing_id=ASSEMBLY_DRAWING,
        version_id=ASSEMBLY_VERSION, deleted_at=None, intake_ref="synthetic-private-input",
        created_at=datetime(2030, 1, 1, tzinfo=timezone.utc))
    binding = assembly_project.VersionBinding(ASSEMBLY_ORG, ASSEMBLY_PROJECT, ASSEMBLY_DRAWING, ASSEMBLY_VERSION, ASSEMBLY_VERSION, True, version)
    verified = assembly_project.VerifiedProjectContext(binding, raw, intake, solve._sha(raw), version.intake_ref)
    context = assembly_graph_api.resolve_project_graph_context(verified)
    request = solve.requests.build_stringer_request(graph, frame["id"], max_string_length=14,
                                                    dwgname=graph["project"]["name"], full_frame=True)
    params = {"grant_ref": "synthetic-grant", "request": request}
    plan = solve.plan_request(context, params, {"frame_ref": frame["id"], "expected_rev": 0,
                                              "phase": "initial"}, job_id=ASSEMBLY_JOB)
    job = {"kind": "run", "job_id": ASSEMBLY_JOB, "org_id": ASSEMBLY_ORG, "project_id": ASSEMBLY_PROJECT,
        "input_version_id": ASSEMBLY_VERSION, "request_tenant_id": str(ASSEMBLY_ORG), "tool_name": solve.TOOL_NAME,
        "params": params, "status": "failed", "attempt": 1, "max_attempts": 3, "lease_owner": None,
        "execution_context": {"schema": jobs.PROJECT_GRAPH_JOB_SCHEMA,
            "authority_mode": "postgres_canonical", "execution_path": "local",
            "drawing_id": str(ASSEMBLY_DRAWING), "actor_binding_id": str(ASSEMBLY_ACTOR), "checkout_fence": "1",
            "tool_manifest_sha256": deps.catalog_tool_digest(solar_tools.trusted_record(solve.TOOL_NAME)),
            "parent_intake_sha256": context.intake_sha256, "initialized": False,
            "solve_binding": plan["solve_binding"]}}
    scope = assembly_store.ExchangeScope(ASSEMBLY_ORG, ASSEMBLY_PROJECT, ASSEMBLY_DRAWING, ASSEMBLY_JOB)
    parent = {**solve._reservation(job, context, plan, "live"), **vars(scope), "state": "prepared"}
    for name in ("response", "candidate", "proof"):
        parent[name + "_bytes"] = parent[name + "_sha256"] = None
    children = []
    for i, piece in enumerate(plan["split_binding"]["pieces"]):
        child_scope = solve._piece_scope(scope, plan["split_binding"], i)
        child = {**solve._reservation(job, context, solve._piece_plan(plan, piece), "live"),
                 **vars(child_scope), "state": "completed", "solver_endpoint": cloud.SOLVER_URL,
                 "solver_version": "1.0.0", "response_bytes": cloud.canonical_bytes(assembly_native_response(piece["request"]))}
        child["response_sha256"] = solve._sha(child["response_bytes"])
        _, child["candidate_bytes"], child["proof_bytes"] = solve._piece_evidence(job, context, plan, i, child)
        child["candidate_sha256"] = solve._sha(child["candidate_bytes"])
        child["proof_sha256"] = solve._sha(child["proof_bytes"])
        children.append(child)
    db = AssemblyReadStore(parent, children)
    monkeypatch.setattr(platform_link, "platform_db", lambda: SimpleNamespace(
        run_transaction=lambda fn, **kwargs: fn(db)))
    monkeypatch.setattr(assembly_graph_api, "_stored_job", lambda job_id, conn: deepcopy(job))
    monkeypatch.setattr(jobs, "_graph_scope", lambda *args: None)
    monkeypatch.setattr(assembly_project, "resolve_context", lambda *args, **kwargs: verified)
    monkeypatch.setattr(deployment_identity, "release_source_identity", lambda:
                        {"release_source_sha": ASSEMBLY_SOURCE, "source_identity": "built"})
    principal = {"tenant": deps.TenantContext(str(ASSEMBLY_ORG), subject="synthetic-reader"), "member": True}
    observed = platform_link._canonical_record(jobs._record(job))
    monkeypatch.setattr(assembly_route, "_job_for_tenant", lambda job_id, tenant:
                        deepcopy(observed) if tenant == str(ASSEMBLY_ORG) and job_id == str(ASSEMBLY_JOB) else None)
    def access(tenant, project_id, **kwargs):
        if not principal["member"]:
            raise platform_link.ProjectSessionForbidden()
        assert project_id == ASSEMBLY_PROJECT
        return str(ASSEMBLY_ORG)
    monkeypatch.setattr(platform_link, "require_project_access", access)
    def forbidden(*args, **kwargs):
        pytest.fail("dispatch, grant, write or checkout called by attribution")
    for name in ("post_stringer", "submit_stringer_async", "get_stringer_result", "resolve_grant"):
        monkeypatch.setattr(cloud, name, forbidden)
    for name in ("prepare_exchange", "begin_send", "capture_response", "complete_exchange", "complete_split_manifest"):
        monkeypatch.setattr(assembly_store, name, forbidden)
    monkeypatch.setattr(assembly_project.graph_store(), "acquire_checkout", forbidden)
    app = FastAPI()
    app.include_router(assembly_route.router)
    app.dependency_overrides[deps.require_tenant] = lambda: principal["tenant"]
    with TestClient(app) as client:
        yield SimpleNamespace(client=client, db=db, parent=parent, children=children,
                              job=job, observed=observed, principal=principal,
                              context=context, plan=plan)


@pytest.mark.parametrize("group", GROUPS, ids=lambda group: group["group_block"])
def test_preview_producer_uses_pinned_split_membership(group):
    children = [{"candidate_bytes": cloud.canonical_bytes({"proposal": response})}
                for response in group["responses"]]
    pieces = [{"row_indices": piece["row_indices"], "request": {"grid": {"Rows": [
        {"Panels": [{"Id": cell, "Code": 1} for cell in row if cell]}
        for row in piece["cells"]
    ]}}} for piece in group["pieces"]]
    saved = solve.completed_prefix_strings(group["responses"], [p["row_indices"] for p in pieces])
    for count in range(1, len(children) + 1):
        actual = solve._preview_groups(children[:count], pieces)
        assert actual and actual == saved[:len(actual)]

    first_panel = next(panel for row in pieces[0]["request"]["grid"]["Rows"] for panel in row["Panels"])
    first_panel["Id"] = "outside-pinned-split"
    assert solve._preview_groups(children, pieces) == []


def test_split_progress_read_is_opt_in_and_project_authorized(assembly_world, monkeypatch):
    from leaf_platform import project_solve_store
    a = assembly_world
    a.observed["status"] = "running"
    monkeypatch.setenv("LEAF_SOURCE_SHA", ASSEMBLY_SOURCE)
    calls = []

    def read(scope, **kwargs):
        calls.append((scope, kwargs))
        return {"completed_pieces": 1, "total_pieces": 3}

    monkeypatch.setattr(project_solve_store, "read_running_split_progress", read)
    url = f"/api/jobs/{ASSEMBLY_JOB}"
    plain = a.client.get(url)
    assert plain.status_code == 200 and "solve_progress" not in plain.json()
    assert calls == []
    response = a.client.get(url + "?include_solve_progress=true")
    assert response.status_code == 200
    assert response.json()["solve_progress"] == {"completed_pieces": 1, "total_pieces": 3}
    assert len(calls) == 1
    scope, args = calls[0]
    assert scope == project_solve_store.ExchangeScope(
        ASSEMBLY_ORG, ASSEMBLY_PROJECT, ASSEMBLY_DRAWING, ASSEMBLY_JOB)
    assert args == {"tenant_id": str(ASSEMBLY_ORG), "input_version_id": ASSEMBLY_VERSION,
                    "graph_revision": a.context.graph["rev"],
                    "graph_sha256": a.plan["solve_binding"]["graph_sha256"], "conn": a.db,
                    "source_sha": ASSEMBLY_SOURCE}

    a.observed["status"] = "failed"
    terminal = a.client.get(url + "?include_solve_progress=true")
    assert terminal.status_code == 200 and "solve_progress" not in terminal.json()
    assert len(calls) == 1

    a.observed["status"] = "running"
    a.principal["member"] = False
    denied = a.client.get(url + "?include_solve_progress=true")
    assert denied.status_code == 403 and "solve_progress" not in denied.json()
    assert len(calls) == 1


def read_assembly(assembly_world, version=ASSEMBLY_VERSION):
    return assembly_world.client.get(f"/api/jobs/{ASSEMBLY_JOB}?assembly_input_version={version}")


def test_assembly_attribution_valid_completed_children_rederive_without_mutation(assembly_world):
    before = deepcopy((assembly_world.parent, assembly_world.children, assembly_world.job))
    response = read_assembly(assembly_world)
    assert response.status_code == 200
    result = response.json()["assembly_attribution"]
    assert result["status"] == "validated", result
    assert result["historical_instruction_attributed"] is False
    assert result["implementation_source_sha"] == ASSEMBLY_SOURCE
    assert result["completed_pieces"] == len(assembly_world.children)
    assert (assembly_world.parent, assembly_world.children, assembly_world.job) == before
    assert len(assembly_world.db.queries) == 3


def test_assembly_attribution_real_assembly_refusal_is_sanitized_current_source_attribution(assembly_world):
    # Positive but repeated sequence numbers pass the piece echo/path checks,
    # then lose members in final cut coverage. Recompute genuine stored custody.
    for i, child in enumerate(assembly_world.children):
        response = json.loads(child["response_bytes"])
        for row in response["data"]["final_grid"]["Rows"]:
            for cell in row["Panels"]:
                cell["Seq"] = 1 if cell["Code"] == 1 else 0
        child["response_bytes"] = cloud.canonical_bytes(response)
        child["response_sha256"] = solve._sha(child["response_bytes"])
        _, child["candidate_bytes"], child["proof_bytes"] = solve._piece_evidence(
            assembly_world.job, assembly_world.context, assembly_world.plan, i, child)
        child["candidate_sha256"] = solve._sha(child["candidate_bytes"])
        child["proof_sha256"] = solve._sha(child["proof_bytes"])
    before = deepcopy((assembly_world.parent, assembly_world.children, assembly_world.job))
    result = read_assembly(assembly_world).json()["assembly_attribution"]
    assert result == {"schema": "leaf.solar-assembly-attribution.v1",
        "mode": "current-source-read-only-rederivation", "historical_instruction_attributed": False,
        "implementation_source_sha": ASSEMBLY_SOURCE, "job_id": str(ASSEMBLY_JOB), "input_version_id": str(ASSEMBLY_VERSION),
        "status": "attributed", "reason": "INVALID_FINAL_GRID_ORDER", "validation_stage": "split.coverage",
        "completed_pieces": len(assembly_world.children), "total_pieces": len(assembly_world.children),
        "piece_order": list(range(len(assembly_world.children))), "piece_order_truncated": False,
        "coverage": {"empty_extraction": False, "unknown_identity": False,
            "repeated_identity": False, "missing_members": True,
            "expected_members": 201, "extracted_members": len(assembly_world.children), "unknown_members": 0,
            "repeated_members": 0, "missing_member_count": 201 - len(assembly_world.children),
            "before_restitch": {"identified_cells": 201, "positive_sequences": 201,
                "nonpositive_sequences": 0, "repeated_positive_sequences": 200},
            "after_restitch": {"identified_cells": 201, "positive_sequences": 201,
                "nonpositive_sequences": 0,
                "repeated_positive_sequences": 201 - len(assembly_world.children)}}}
    assert (assembly_world.parent, assembly_world.children, assembly_world.job) == before


def test_assembly_attribution_byte_limit_refuses_before_loading_evidence(assembly_world):
    assembly_world.db.oversized = True
    result = read_assembly(assembly_world).json()["assembly_attribution"]
    assert result["status"] == "unavailable"
    assert result["reason"] == "evidence_integrity_or_limit_refusal"
    assert len(assembly_world.db.queries) == 1


@pytest.mark.parametrize("kind", ["unknown", "repeated", "missing"])
def test_assembly_coverage_predicates_preserve_refusal_and_hide_identities(assembly_world, monkeypatch, kind):
    members = assembly_world.context.graph["frames"][0]["panel_refs"]
    extracted = [ref.upper() for ref in members]
    if kind == "unknown":
        extracted[0] = "PRIVATE-UNKNOWN-IDENTITY"
    elif kind == "repeated":
        extracted[0] = extracted[1]
    else:
        extracted.pop()
    # Isolate the extraction boundary after real stored evidence verification.
    monkeypatch.setattr(solve.kernel, "ordered_strings", lambda merged: [extracted])
    before = deepcopy((assembly_world.parent, assembly_world.children, assembly_world.job))
    result = read_assembly(assembly_world).json()["assembly_attribution"]
    assert result["status"] == "attributed"
    assert result["reason"] == "INVALID_FINAL_GRID_ORDER"
    assert result["validation_stage"] == "split.coverage"
    coverage = result["coverage"]
    assert coverage["empty_extraction"] is False
    assert coverage["unknown_identity"] is (kind == "unknown")
    assert coverage["repeated_identity"] is (kind == "repeated")
    assert coverage["missing_members"] is True
    assert coverage["expected_members"] == 201
    assert coverage["extracted_members"] == (200 if kind == "missing" else 201)
    assert coverage["unknown_members"] == int(kind == "unknown")
    assert coverage["repeated_members"] == int(kind == "repeated")
    assert coverage["missing_member_count"] == 1
    for stage in ("before_restitch", "after_restitch"):
        counts = coverage[stage]
        assert set(counts) == {"identified_cells", "positive_sequences",
                              "nonpositive_sequences", "repeated_positive_sequences"}
        assert counts["identified_cells"] == counts["positive_sequences"] == 201
        assert counts["nonpositive_sequences"] == 0
        assert all(type(value) is int and 0 <= value <= 201 for value in counts.values())
    assert set(coverage) == {"empty_extraction", "unknown_identity", "repeated_identity",
        "missing_members", "expected_members", "extracted_members", "unknown_members",
        "repeated_members", "missing_member_count", "before_restitch", "after_restitch"}
    serialized = json.dumps(result)
    assert "PRIVATE-UNKNOWN-IDENTITY" not in serialized
    assert all(ref not in serialized and ref.upper() not in serialized for ref in members)
    assert (assembly_world.parent, assembly_world.children, assembly_world.job) == before


def test_assembly_attribution_unattested_current_source_is_explicitly_unavailable(assembly_world, monkeypatch):
    monkeypatch.setattr(deployment_identity, "release_source_identity", lambda:
                        {"release_source_sha": None, "source_identity": "unattested"})
    result = read_assembly(assembly_world).json()["assembly_attribution"]
    assert result["reason"] == "implementation_source_unavailable"
    assert assembly_world.db.queries == []


@pytest.mark.parametrize("corrupt", [False, True])
def test_assembly_attribution_completed_parent_custody_is_still_checked(assembly_world, corrupt):
    manifest = solve._piece_manifest(assembly_world.children)
    assembly_world.parent.update(response_bytes=manifest, response_sha256=solve._sha(manifest),
                        solver_endpoint=cloud.SOLVER_URL, solver_version="1.0.0")
    candidate, proof, _ = solve._derive_split(assembly_world.job, assembly_world.context, assembly_world.plan,
                                             assembly_world.parent, children=assembly_world.children)
    if corrupt:
        candidate = b'{"private-candidate":"must-not-leak"}'
    assembly_world.parent.update(state="completed", candidate_bytes=candidate, proof_bytes=proof,
                        candidate_sha256=solve._sha(candidate), proof_sha256=solve._sha(proof))
    response = read_assembly(assembly_world)
    result = response.json()["assembly_attribution"]
    assert result["status"] == ("unavailable" if corrupt else "validated")
    assert "must-not-leak" not in response.text


@pytest.mark.parametrize("fault", ["tenant", "member", "version", "stored_parent", "stored_tenant",
    "missing", "duplicate", "incomplete", "corrupt", "child_version", "child_parent", "proof"])
def test_assembly_attribution_invalid_authority_or_evidence_fails_closed(assembly_world, fault):
    version = ASSEMBLY_VERSION
    if fault == "tenant":
        assembly_world.principal["tenant"] = deps.TenantContext(str(UUID(int=99)), subject="other")
    elif fault == "member":
        assembly_world.principal["member"] = False
    elif fault == "version":
        version = UUID(int=99)
    elif fault == "stored_parent":
        assembly_world.job["input_version_id"] = UUID(int=99)
    elif fault == "stored_tenant":
        assembly_world.job["request_tenant_id"] = "other"
    elif fault == "missing":
        assembly_world.children.pop()
    elif fault == "duplicate":
        assembly_world.children[-1] = deepcopy(assembly_world.children[0])
    elif fault == "incomplete":
        assembly_world.children[0]["state"] = "responded"
    elif fault == "corrupt":
        assembly_world.children[0]["response_sha256"] = "0" * 64
    elif fault == "child_version":
        assembly_world.children[0]["input_version_id"] = UUID(int=99)
    elif fault == "child_parent":
        assembly_world.children[0]["job_id"] = UUID(int=99)
    elif fault == "proof":
        assembly_world.children[0]["proof_bytes"] = b'{"private-response":"must-not-leak"}'
        assembly_world.children[0]["proof_sha256"] = solve._sha(assembly_world.children[0]["proof_bytes"])
    response = read_assembly(assembly_world, version)
    if fault in {"tenant", "member"}:
        assert response.status_code in {403, 404}
    else:
        assert response.status_code == 200
        assert response.json()["assembly_attribution"]["status"] == "unavailable"
    assert "must-not-leak" not in response.text
    if fault in {"tenant", "member", "version", "stored_parent", "stored_tenant"}:
        assert assembly_world.db.queries == []


def test_roof_continuity_single_refuses_before_proposal_completion(case, monkeypatch):
    from test_w1_solve_commit import roof_continuity_case
    roof_continuity_case(case, step=40.0)
    a = world.__wrapped__(admitted.__wrapped__(case, monkeypatch), case, monkeypatch)
    before = deepcopy((a.context.graph, a.s.versions, a.s.writes))
    with pytest.raises(CloudError) as refused:
        solve.prepare_job(a.row, a.guard)
    assert refused.value.classification == "cloud_response_invalid"
    assert a.store.events == ["prepare", "begin_send", "capture", "refuse"]
    assert a.store.exchange["state"] == "refused"
    assert a.store.exchange["candidate_bytes"] is None
    assert a.store.exchange["proof_bytes"] is None
    assert (a.context.graph, a.s.versions, a.s.writes) == before


@pytest.mark.parametrize("long_step", [False, True])
def test_roof_continuity_split_refuses_before_proposal_return(graph, monkeypatch, long_step):
    from test_w1_solve_split import roof_seam_candidate
    value, candidate = roof_seam_candidate(graph, monkeypatch, long_step=long_step)
    envelopes = candidate["proposals"]
    children = [{"piece_index": i, "classification": "synthetic",
        "request_sha256": envelope["request_sha256"], "response_sha256": envelope["response_sha256"],
        "candidate_bytes": b"candidate", "candidate_sha256": solve._sha(b"candidate"),
        "proof_bytes": b"proof", "proof_sha256": solve._sha(b"proof")} for i, envelope in enumerate(envelopes)]
    response = solve._piece_manifest(children)
    exchange = {"classification": "synthetic", "response_bytes": response,
        "response_sha256": solve._sha(response), "solver_endpoint": cloud.SOLVER_URL, "solver_version": "1.0.0"}
    context = SimpleNamespace(graph=value, organization_id=uuid4(), project_id=uuid4(),
        drawing_id=uuid4(), parent_version_id=uuid4(), intake_sha256="c" * 64)
    plan = {"split_binding": candidate["binding"],
        "binding": {"frame_ref": value["frames"][0]["id"]}, "max_string_length": 3}
    monkeypatch.setattr(solve, "_authority", lambda job: (None, None))
    monkeypatch.setattr(solve, "_reservation", lambda *args: {})
    monkeypatch.setattr(solve, "_piece_evidence", lambda job, context, plan, i, child:
        (envelopes[i], child["candidate_bytes"], child["proof_bytes"]))
    store = Store()
    monkeypatch.setattr(solve, "_store", lambda: store)
    before = deepcopy((value, candidate, children, exchange))
    job = {"job_id": "split-job", "execution_context": {"tool_manifest_sha256": "d" * 64}}
    if long_step:
        with pytest.raises(GraphValidationError, match="DISCONTIGUOUS_SOLVE_STRING"):
            solve._derive_split(job, context, plan, exchange, children=children)
    else:
        envelope, proof, summary = solve._derive_split(job, context, plan, exchange, children=children)
        assert json.loads(envelope)["candidate"] == candidate
        assert json.loads(proof)["candidate_sha256"] == solve._sha(envelope)
        assert [row["panel_count"] for row in summary] == [3, 1]
    assert (value, candidate, children, exchange) == before
    assert store.events == [] and store.exchange is None
