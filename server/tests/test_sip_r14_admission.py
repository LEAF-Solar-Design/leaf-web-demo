"""R14-C admission: pure planning and tenant-bound private grant resolution."""
from copy import deepcopy
from types import SimpleNamespace
from uuid import uuid4

import pytest
import deps
import platform_link
import solar_project_context as project
import solar_project_graph as graph
import solar_project_jobs as service
import solar_project_sizing as sizing
import solar_sizing_client as cloud
from leaf_cloud_grants import CloudError, CloudGrant
from leaf_platform import canonical_jobs as jobs, entitlements
import test_sip_r3a_graph as a3
import test_sip_r3b_tools as b3
import test_sip_r4_jobs as r4
import test_sip_r5_policy as r5
import test_w1_design_graph as design


def forbidden(*args, **kwargs):
    raise AssertionError("Unexpected network, grant, or publication operation")


def source():
    value = design.graph.__wrapped__()
    value["project"]["zip_code"] = "44224"
    value["settings"]["id"] = "leaf:settings:" + str(uuid4())
    return value


def parameters(value):
    return {"expected_rev": value["rev"], "mode": "global", "confirm": True,
        "grant_ref": "g1", "requests": {value["settings"]["id"]: {
            "module_name": "M", "full_inverter_name": "I", "bifacial": False,
            "bifacial_coefficient": "0", "racking_params": {"racking_type": "fixed_tilt",
                "surface_tilt": "20", "surface_azimuth": "180", "albedo": "0.2"},
            "max_voltage": "600", "thermal_model_type": "sapm", "open_circuit_rise": False,
            "zip_code": "44224"}}}


def setup(monkeypatch):
    # Use fresh identities, independently of the older deterministic byte-pin fixtures.
    a = r5.setup(monkeypatch)
    monkeypatch.setattr(a3, "uuid4", uuid4)
    s = a3.memory.__wrapped__(monkeypatch)
    a = r4.admission.__wrapped__(s, monkeypatch)
    monkeypatch.setattr(platform_link, "resolve_submission_context", lambda *args:
        {"org_id": s.org, "project_id": s.project, "authority_mode": "postgres_canonical"})
    value = source()
    s.set_parent(b3.embedded(value))
    a.params = parameters(value)
    a.key = str(uuid4())
    a.grants = []
    monkeypatch.setattr(cloud, "resolve_grant", lambda ref, tenant:
        a.grants.append((ref, tenant)) or CloudGrant(tenant, "t1"))
    monkeypatch.setattr(cloud, "post_string_length", forbidden)
    monkeypatch.setattr(cloud.requests, "post", forbidden)
    return a


def request(a, params=None, **changes):
    return r5.request(a, a.params if params is None else params,
        tool_name="solar-size-strings", idempotency_key=a.key, **changes)


def reason(response, code, status):
    assert response["status_code"] == status
    assert response["content"]["error"]["reason_code"] == code
    assert response["content"]["error"]["retryable"] is False


def test_R14C_01_pure_preview_and_submission(monkeypatch):
    """R14C-01 Pure preview and submission parity, without transport or output preparation."""
    a = setup(monkeypatch)
    plans = []
    original = sizing.plan_sizing_request
    def plan(*args):
        result = original(*args)
        plans.append(result)
        return result
    monkeypatch.setattr(sizing, "plan_sizing_request", plan)
    monkeypatch.setattr(graph, "prepare_project_graph_commit", forbidden)
    assert request(a)["status_code"] == 200
    assert not a.accepted and not a.s.writes
    assert request(a, preview=False)["status_code"] == 202
    assert len(plans) == 2
    assert a.accepted[0][4] == plans[0]["normalized_params"] == plans[1]["normalized_params"]
    assert len(a.grants) == 2 and not a.s.writes


def test_R14C_02_missing_grant(monkeypatch):
    """R14C-02 Missing grant refuses both entrypoints before inserting a job."""
    a = setup(monkeypatch)
    def missing(*args):
        raise CloudError("cloud_auth_missing", 401)
    monkeypatch.setattr(cloud, "resolve_grant", missing)
    for preview in (True, False):
        reason(request(a, preview=preview), "CLOUD_AUTH_MISSING", 401)
    assert not a.accepted


def test_R14C_03_canonical_tenant(monkeypatch):
    """R14C-03 Grant resolution uses the canonical organization, not a selected tenant."""
    a = setup(monkeypatch)
    foreign = str(uuid4())
    seen = []
    def resolve(ref, tenant):
        seen.append(tenant)
        if tenant != foreign:
            raise CloudError("cloud_tenant_unauthorized", 403)
        return CloudGrant(tenant, "t1")
    monkeypatch.setattr(cloud, "resolve_grant", resolve)
    for preview in (True, False):
        reason(request(a, preview=preview), "CLOUD_TENANT_UNAUTHORIZED", 403)
    assert seen == [str(a.s.org)] * 2 and not a.accepted


def test_R14C_04_stored_entitlement(monkeypatch):
    """R14C-04 Stored entitlement refuses insertion and transport after pure validation."""
    a = setup(monkeypatch)
    from fastapi.responses import JSONResponse
    denial = JSONResponse(status_code=403, content={"entitlement_required": "run"})
    monkeypatch.setattr(entitlements, "stored_job_entitlement_verdict", lambda *args: (denial, None))
    assert request(a, preview=False)["status_code"] == 403
    assert not a.accepted and not a.s.writes


def test_R14C_05_historical_replay(monkeypatch):
    """R14C-05 Identical replay needs neither a fresh grant nor checkout; changed intent conflicts."""
    a = setup(monkeypatch)
    assert request(a, preview=False)["status_code"] == 202
    accepted = a.accepted[0]
    row = {"job_id": str(uuid4()), "deleted_at": None,
        "execution_context": accepted[-1]["execution_context"],
        "submission_fingerprint": accepted[-1]["submission_fingerprint"]}
    monkeypatch.setattr(jobs, "get_project_graph_job_by_key", lambda *args, **kw: row)
    monkeypatch.setattr(cloud, "resolve_grant", forbidden)
    monkeypatch.setattr(project, "verify_at_admission", forbidden)
    normalized = accepted[4]
    for replay_params in (a.params, normalized):
        for preview in (True, False):
            assert request(a, replay_params, preview=preview, checkout_capability=None)["status_code"] == (200 if preview else 202)
            reason(request(a, {**replay_params, "grant_ref": "g2"}, preview=preview),
                   "SIP_R4_IDEMPOTENCY_CONFLICT", 409)


def test_R14C_06_closed_bounded_request(monkeypatch):
    """R14C-06 Closed parameters and independent normalized and wire bounds precede insert."""
    a = setup(monkeypatch)
    for field in ("evidence", "endpoint", "tenant_id", "allow_synthetic_for_test", "access_token"):
        assert request(a, {**a.params, field: "x"}, preview=False)["status_code"] == 409
    huge = {**a.params, "grant_ref": "x" * 65537}
    assert request(a, huge, preview=False)["status_code"] == 409
    # The wire representation has its own bound even when normalized input is small.
    with monkeypatch.context() as patch:
        patch.setattr(cloud, "wire_bytes", lambda request: b"x" * 65537)
        reason(request(a, preview=False), "CLOUD_REQUEST_INVALID", 409)
    with monkeypatch.context() as patch:
        wire = cloud.SizingRequest.wire
        patch.setattr(cloud.SizingRequest, "wire", lambda self: {**wire(self), "padding": "x" * 65537})
        patch.setattr(cloud, "wire_bytes", lambda request: b"{}")
        reason(request(a, preview=False), "CLOUD_REQUEST_INVALID", 409)
    assert not a.accepted


def test_R14C_07_planner_refusal_parity(monkeypatch):
    """R14C-07 Planner graph refusals retain 409; a service CloudError request refusal is 400."""
    a = setup(monkeypatch)
    for params, code in (({k: v for k, v in a.params.items() if k != "requests"}, "INVALID_SIZING_COVERAGE"),
            ({**a.params, "requests": {"surprise": 1}}, "CLOUD_REQUEST_INVALID"),
            ({**a.params, "expected_rev": 99}, "STALE_GRAPH_REVISION")):
        for preview in (True, False):
            result = request(a, params, preview=preview)
            reason(result, code, 409)  # Planner CLOUD_REQUEST_INVALID: 409 BAD_PARAMS.
            assert result["content"]["error"]["error_code"] == "BAD_PARAMS"
    def reject(*args):
        raise CloudError("cloud_request_invalid", 400)
    monkeypatch.setattr(cloud, "resolve_grant", reject)
    reason(request(a), "CLOUD_REQUEST_INVALID", 400)  # CloudError branch: 400 BAD_PARAMS.
    assert not a.accepted


def test_R14C_08_manual_and_zones(monkeypatch):
    """R14C-08 Manual admission is unchanged; zones and missing mode never enter cloud planning."""
    a = setup(monkeypatch)
    monkeypatch.setattr(cloud, "resolve_grant", forbidden)
    monkeypatch.setattr(sizing, "plan_sizing_request", forbidden)
    manual = {"expected_rev": 0, "mode": "manual-global", "confirm": True}
    assert request(a, manual)["status_code"] == 200
    for params in ({**manual, "mode": "zones"}, {k: v for k, v in manual.items() if k != "mode"}):
        reason(request(a, params), "SIP_R3_SERVICE_EVIDENCE_REQUIRED", 409)
