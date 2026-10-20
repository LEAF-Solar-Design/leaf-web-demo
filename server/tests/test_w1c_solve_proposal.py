"""Canonical proposal admission, single-send recovery and offline proof seams."""
from copy import deepcopy
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
from solar_design_graph import GraphValidationError
from test_w1_design_graph import graph as graph_fixture  # noqa: F401
from test_w1_solve_commit import case, graph as graph  # noqa: F401
import solar_project_graph as adapter
import test_sip_r3b_tools as b3
import test_sip_r5_policy as r5
import test_sip_r5_routes as routes


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
            "response_bytes": None, "candidate_bytes": None, "proof_bytes": None}
        return deepcopy(self.exchange)

    def resume_exchange(self, scope, lease):
        state = self.exchange["state"]
        if state == "sending":
            self.record_outcome_unknown(scope, lease)
            state = "outcome_unknown"
        return SimpleNamespace(action={"prepared": "send_ready", "responded": "use_response"}.get(state, state),
                               exchange=deepcopy(self.exchange))

    def begin_send(self, scope, lease):
        self.events.append("begin_send")
        assert self.exchange["state"] == "prepared"
        self.exchange["state"] = "sending"
        return True

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


@pytest.mark.parametrize("field", ["proposals", "proofs", "split_state"])
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


def test_known_prewrite_failure_refuses(world, monkeypatch):
    def failed(*args, **kwargs):
        raise CloudError("cloud_upstream_failure", 502)
    monkeypatch.setattr(cloud, "post_stringer", failed)
    with pytest.raises(CloudError):
        solve.prepare_job(world.row, world.guard)
    assert world.store.events == ["prepare", "begin_send", "refuse"]
    assert world.store.exchange["state"] == "refused"


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
    for tool in adapter.SUPPORTED_TOOLS - {solve.TOOL_NAME}:
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
