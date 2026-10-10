"""HTTP transport, exact catalog admission and current graph observation."""
import asyncio
from copy import deepcopy
import json
from types import SimpleNamespace
from uuid import UUID

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

import deps
import entitlements
import envelopes
import platform_link
import solar_project_admission as policy
import solar_project_context as project
import solar_project_graph as graph
import solar_project_jobs as service
import solar_tools
from leaf_platform import canonical_jobs, entitlements as stored
from routers import capabilities as catalog_route, jobs as route
import test_sip_r5_policy as policy_cases
import test_campaign_capability_job_access as campaign_cases
import test_project_scoped_read_tool as read_cases

_FIND_TOOL = deps.find_tool
_ENGINE_TOOLS = deps.load_engine_registry_tools
_PROJECT_READ = route._run_project_read


@pytest.fixture
def lane(monkeypatch):
    a = policy_cases.setup(monkeypatch)
    s = a.s
    tenant = deps.TenantContext(str(s.org), tier="hosted_pro", subject="reader")
    principal = {"tenant": tenant, "member": True}
    tools = solar_tools.trusted_snapshot()
    records, calls = {}, []
    monkeypatch.setattr(deps, "backedge_run_identity", lambda tenant, *args: tenant)
    monkeypatch.setattr(deps, "find_tool", lambda name, *args: tools.get(name))
    monkeypatch.setattr(deps, "effective_tools_with_provenance", lambda *args:
        [(tool, None) for tool in tools.values()])
    monkeypatch.setattr(deps, "load_engine_registry_tools", lambda: [])
    monkeypatch.setattr(catalog_route.mcp_tool_projection, "projected_tools", lambda *args: [])
    monkeypatch.setattr(catalog_route.customization_service, "effective_catalog_pin", lambda *args: None)
    monkeypatch.setattr(route.jobs, "get_job", lambda *args: None)
    monkeypatch.setattr(route.jobs, "list_jobs", lambda *args: [])
    monkeypatch.setattr(route.jobs, "job_store_mode", lambda: "legacy")
    monkeypatch.setattr(route.jobs, "submit_job", policy_cases.r4.forbidden)
    monkeypatch.setattr(platform_link, "get_canonical_job", lambda job, tenant:
        deepcopy(records.get(job)) if records.get(job, {}).get("tenant_id") == tenant else None)
    monkeypatch.setattr(platform_link, "list_canonical_jobs", lambda tenant, **kwargs:
        [deepcopy(rec) for rec in records.values() if rec["tenant_id"] == tenant])
    def access(caller, project_id, *, write):
        calls.append((str(caller), str(project_id), write))
        if str(project_id) != str(s.project):
            raise LookupError()
        if not principal["member"]:
            raise platform_link.ProjectSessionForbidden()
        return str(s.org)
    monkeypatch.setattr(platform_link, "require_project_access", access)
    insert = canonical_jobs.submit_project_graph_job
    def save(org, proj, tenant_id, tool, params, key, **kwargs):
        row = insert(org, proj, tenant_id, tool, params, key, **kwargs)
        records[row["job_id"]] = {
            **row, "tenant_id": tenant_id, "org_id": str(org), "project_id": str(proj),
            "tool_name": tool, "params": deepcopy(params), "status": "submitted",
            "progress": "submitted", "elapsed_ms": None, "error": None,
            "execution_context": deepcopy(kwargs["execution_context"])}
        return row
    monkeypatch.setattr(canonical_jobs, "submit_project_graph_job", save)
    app = FastAPI()
    envelopes.install_error_handlers(app)
    app.include_router(route.router)
    app.include_router(catalog_route.router)
    app.dependency_overrides[deps.require_tenant] = lambda: principal["tenant"]
    with TestClient(app) as client:
        yield SimpleNamespace(a=a, s=s, tools=tools, tenant=tenant, principal=principal,
                              records=records, calls=calls, client=client)


def body(lane, params=None, name="solar-settings", **changes):
    result = {"tool": name, "dwg": str(lane.s.parent.version_id),
        "params": deepcopy(policy_cases.r3a.prepare(lane.s).request["parameters"]
                           if params is None else params),
        "catalog_digest": deps.catalog_tool_digest(lane.tools[name])}
    result["params"].setdefault("drawing_id", str(lane.s.drawing))
    result.update(changes)
    return result


def headers(lane, **changes):
    result = {"X-Org-Id": str(lane.s.org), "X-Project-Id": str(lane.s.project),
              "Idempotency-Key": "key", "X-Checkout-Capability": "cap"}
    result.update(changes)
    return result


def run(lane, params=None, name="solar-settings", *, wait=0, request_headers=None, **changes):
    return lane.client.post(f"/api/run?wait={wait}", json=body(lane, params, name, **changes),
                           headers=headers(lane) if request_headers is None else request_headers)


def preview(lane, params=None, name="solar-settings", *, request_headers=None, **changes):
    request = body(lane, params, name)
    query = {"project_id": str(lane.s.project), "drawing_id": str(lane.s.drawing),
        "input_version_id": request["dwg"], "project_runs": json.dumps({name: {
            "params": request["params"], "catalog_digest": request["catalog_digest"],
            "idempotency_key": "key"}})}
    query.update(changes)
    return lane.client.get("/api/capabilities", params=query,
                          headers=headers(lane) if request_headers is None else request_headers)


def states(response):
    assert response.status_code == 200, response.text
    return {row["name"]: row["availability"] for family in response.json()["families"]
            for row in family["capabilities"] if "availability" in row}


def refusal(response, reason, status=409, message="Project Solar admission unavailable.", retry=False):
    expected = policy._refusal(reason, status=status, message=message, retryable=retry)
    assert response.status_code == expected["status_code"]
    assert response.json() == expected["content"]


def specimen(response):
    return response.status_code, response.content, {
        key: response.headers[key] for key in ("content-type", "cache-control") if key in response.headers}


DIGEST_SENTENCE = "catalog tool changed or confirmation digest is missing; refresh tools and confirm again"
# Text a digest can never be: one accented letter, a lone surrogate, a digest-length string with one
# accented letter, and sixty-four full-width zeros.
UNCOMPARABLE_DIGESTS = (chr(0xE9), chr(0xD800), "0" * 63 + chr(0xE9), chr(0xFF10) * 64)


# Every ASCII character Python's str.strip() removes, then the two-character line ending and a NUL.
NEAR_MISS_EDGES = (" ", "\t", "\n", "\r", "\x0b", "\x0c", "\x1c", "\x1d", "\x1e", "\x1f", "\r\n", "\x00")


def near_miss_digests(digest):
    # What a normalising comparison would wrongly accept: the digest with one edge character after it
    # or before it, the bare hex without its scheme, and the scheme respelled (upper case, a space
    # after the colon, a space before it). The list is finite, so it names the normalisations it
    # covers and proves nothing about one it does not name.
    scheme, colon, bare = digest.partition(":")
    assert scheme == "sha256" and colon == ":" and len(bare) == 64
    return (*(digest + edge for edge in NEAR_MISS_EDGES), *(edge + digest for edge in NEAR_MISS_EDGES),
            bare, "SHA256:" + bare, "Sha256:" + bare, "sha256: " + bare, "sha256 :" + bare)


def raw_run(lane, digest, request_headers):
    # The body travels as ASCII JSON so a lone surrogate reaches the route as the client sent it.
    payload = json.dumps({**body(lane), "catalog_digest": digest}).encode("ascii")
    return lane.client.post("/api/run?wait=0", content=payload,
                            headers={**request_headers, "content-type": "application/json"})


def replay_row(lane, monkeypatch):
    params = policy_cases.r3a.prepare(lane.s).request["parameters"]
    s = lane.s
    row = {"job_id": str(s.job), "execution_context": {"schema": canonical_jobs.PROJECT_GRAPH_JOB_SCHEMA},
        "deleted_at": None, "submission_fingerprint": service._submission_fingerprint(
            s.org, s.project, s.drawing, s.parent.version_id, str(s.org), s.actor,
            "solar-settings", params, deps.catalog_tool_digest(lane.tools["solar-settings"]))}
    monkeypatch.setattr(canonical_jobs, "get_project_graph_job_by_key", lambda *args, **kwargs: row)
    return row


def test_sip_r5_off_bytes(lane, monkeypatch):
    job_id = run(lane).json()["job_id"]
    monkeypatch.setattr(entitlements, "w1_tool_availability", lambda *args, **kwargs: None)
    monkeypatch.setattr(route, "_checkout_identity", lambda *args: ("Editor", 1))
    def samples():
        result = [specimen(run(lane, dwg=str(lane.s.drawing), dwg_version=1)),
            specimen(run(lane, catalog_digest="stale")),
            specimen(lane.client.get("/api/capabilities", params={"project_id": str(lane.s.project)})),
            specimen(lane.client.get(f"/api/jobs/{job_id}"))]
        with monkeypatch.context() as patch:
            patch.setattr(entitlements, "entitlements_for", lambda *args: {})
            result.append(specimen(run(lane)))
        with monkeypatch.context() as patch:
            patch.setattr(entitlements, "w1_tool_availability", lambda *args, **kwargs:
                {"runnable": False, "refusal_reasons": ["drawing_context_required"]})
            result.append(specimen(run(lane)))
        with monkeypatch.context() as patch:
            patch.setattr(route, "_checkout_identity", lambda *args: (route._store().ANONYMOUS_HOLDER, None))
            result.append(specimen(run(lane, dwg=str(lane.s.drawing), dwg_version=1)))
        return result
    monkeypatch.delenv("LEAF_SOLAR_PROJECT_RUN_ENABLED", raising=False)
    expected = samples()
    assert expected[0][0] == 409
    assert json.loads(expected[0][1])["error"]["message"] == (
        "project-scoped canonical execution is enabled only for a connected solver adapter")
    assert [row[0] for row in expected] == [409, 409, 200, 200, 403, 409, 403]
    for value in ("0", "true", "TRUE", "yes", "on", "2", "1 ", " 1", "01", "", "\t1\n"):
        monkeypatch.setenv("LEAF_SOLAR_PROJECT_RUN_ENABLED", value)
        assert samples() == expected
    assert expected[3][0] == 200 and json.loads(expected[3][1])["job_id"] == job_id


def test_sip_r5_existing_early_refusals(lane, monkeypatch):
    monkeypatch.setenv("LEAF_SOLAR_PROJECT_RUN_ENABLED", "1")
    monkeypatch.setattr(policy, "project_tool_admission", policy_cases.r4.forbidden)
    with monkeypatch.context() as patch:
        patch.setattr(deps, "backedge_run_identity", lambda *args: None)
        assert run(lane).status_code == 403
    assert run(lane, tool="missing").json()["error"]["error_code"] == "UNKNOWN_TOOL"
    for fields, sentence in (({"solve_context": {"frame_ref": "frame", "expected_rev": 0}},
                              "solve_context requires a cloud proposal"),
                             ({"proposal_job_id": "proposal"},
                              "proposal_job_id requires a proposal candidate input")):
        response = run(lane, **fields)
        assert response.status_code == 400 and response.json()["error"]["message"] == sentence
    assert run(lane, catalog_digest="stale").status_code == 409
    monkeypatch.setattr(route.entity_scope, "resolve_turn_binding", lambda *args: SimpleNamespace())
    response = run(lane, request_headers=headers(lane,
        **{"X-Authority-Session-Id": "session", "X-Authority-Turn-Id": "turn"}))
    assert response.status_code == 403
    assert response.json()["reason_code"] == route.entity_scope.SCOPED_MUTATION_REASON
    assert not lane.a.accepted


def test_sip_r5_seed_submission(lane, monkeypatch):
    monkeypatch.setenv("LEAF_SOLAR_PROJECT_RUN_ENABLED", "1")
    monkeypatch.setattr(graph, "publish_project_graph_commit", policy_cases.r4.forbidden)
    before = deepcopy((lane.s.blobs, lane.s.versions, lane.s.fingerprints, lane.s.writes))
    response = run(lane)
    assert response.status_code == 202
    assert response.json() == deps.tenant_echo({"job_id": str(lane.s.job), "status": "submitted",
                               "error": None, "degraded_mode": False}, lane.tenant)
    assert lane.a.events.count("insert") == 1 and len(lane.records) == 1
    assert lane.a.accepted[0][6]["execution_context"]["schema"] == canonical_jobs.PROJECT_GRAPH_JOB_SCHEMA
    assert (lane.s.blobs, lane.s.versions, lane.s.fingerprints, lane.s.writes) == before


def test_sip_r5_supported_tools(lane, monkeypatch):
    monkeypatch.setenv("LEAF_SOLAR_PROJECT_RUN_ENABLED", "1")
    calls = []
    monkeypatch.setattr(service, "submit_project_graph_job", lambda *args, **kwargs:
        calls.append((args, kwargs)) or {"job_id": str(lane.s.job)})
    for name in sorted(policy_cases.NINE):
        before = deepcopy(lane.tools[name])
        assert run(lane, {}, name).status_code == 202
        assert calls[-1][0][4] == name and calls[-1][0][0] is lane.tenant
        assert lane.tools[name] == before
    for name in ("solar-string-multi-add", "solar-pvcase-convert", "solar-commit-solve"):
        refusal(run(lane, {}, name), "SIP_R3_TOOL_UNSUPPORTED")
    lane.tools["solar-settings"]["description"] = "Changed record"
    refusal(run(lane, {}), "SIP_R3_TOOL_MANIFEST_MISMATCH")
    assert len(calls) == 9


def test_sip_r5_binding_and_legacy_pins(lane, monkeypatch):
    monkeypatch.setenv("LEAF_SOLAR_PROJECT_RUN_ENABLED", "1")
    calls = []
    monkeypatch.setattr(policy, "project_tool_admission", lambda *args, **kwargs:
        calls.append((args, kwargs)) or {"status_code": 202, "content": {"job_id": "job"}})
    cases = [({"catalog_digest": "stale", "dwg_version": 1},
              "catalog tool changed or confirmation digest is missing; refresh tools and confirm again"),
             ({"dwg_version": 1, "expected_drawing_head": 1},
              "dwg_version applies to the legacy path; canonical runs pin by version UUID in dwg"),
             ({"expected_drawing_head": 1, "tool_manifest_sha256": "stale"},
              "expected_drawing_head applies only to legacy versioned drawings"),
             ({"tool_manifest_sha256": "stale"},
              "tool manifest changed after approval; refresh tools and confirm again")]
    for fields, sentence in cases:
        response = run(lane, **fields)
        assert response.status_code == 409 and response.json()["error"]["message"] == sentence
        assert "reason_code" not in response.json()["error"]
    assert not calls
    params = {"drawing_id": str(lane.s.drawing)}
    sent = headers(lane, Authorization="Bearer t1")
    assert run(lane, params, request_headers=sent).status_code == 202
    args, kwargs = calls[-1]
    assert args[0] == lane.tools["solar-settings"] and args[1] is lane.tenant
    assert kwargs["params"] == params and kwargs["preview"] is False
    assert kwargs["input_version_id"] == str(lane.s.parent.version_id)
    forwarded = {"org_header": str(lane.s.org), "project_header": str(lane.s.project),
        "authorization": "Bearer t1", "input_version_id": str(lane.s.parent.version_id),
        "drawing_id": str(lane.s.drawing), "params": params,
        "catalog_digest": deps.catalog_tool_digest(lane.tools["solar-settings"]),
        "idempotency_key": "key", "checkout_capability": "cap"}
    assert args == (lane.tools["solar-settings"], lane.tenant)
    assert kwargs == {**forwarded, "preview": False}
    before = len(calls)
    assert preview(lane, params, request_headers=sent).status_code == 200
    assert len(calls) == before + 1
    args, kwargs = calls[-1]
    assert args == (lane.tools["solar-settings"], lane.tenant)
    assert kwargs == {**forwarded, "preview": True}
    req = route.RunRequest(**body(lane, {}))
    route._project_solar_admission(lane.tools[req.tool], lane.tenant, req,
        None, None, None, None, idempotency_key=None, preview=True)
    assert calls[-1][1]["idempotency_key"] is None
    lane.tools["solar-settings"].setdefault("default_params", {})["drawing_id"] = str(lane.s.drawing)
    req.params.pop("drawing_id")
    route._project_solar_admission(lane.tools[req.tool], lane.tenant,
        route.RunRequest(**{**req.model_dump(), "catalog_digest": deps.catalog_tool_digest(lane.tools[req.tool])}),
        None, None, None, None, idempotency_key=None, preview=True)
    assert calls[-1][1]["drawing_id"] == str(lane.s.drawing)
    before = len(calls)
    with monkeypatch.context() as patch:
        req.catalog_digest = deps.catalog_tool_digest(lane.tools[req.tool])
        patch.setattr(entitlements, "w1_tool_availability", lambda *args, **kwargs:
            {"runnable": False, "refusal_reasons": ["drawing_context_required"]})
        response = route.run(req, tenant_id=lane.tenant)
        assert response.status_code == 409 and len(calls) == before


def test_sip_r5_checkout_and_scope(lane, monkeypatch):
    monkeypatch.setenv("LEAF_SOLAR_PROJECT_RUN_ENABLED", "1")
    refusal(run(lane, dwg="bad"), None, 400, "a canonical drawing version UUID is required")
    refusal(run(lane, {"drawing_id": "bad"}), None, 400,
            "a canonical drawing artifact UUID is required in params.drawing_id")
    for reason, status, retry in (("SIP_R1_CONTEXT_NOT_FOUND", 404, False),
            ("SIP_R1_PROJECT_FORBIDDEN", 403, False), ("SIP_R1_STALE_VERSION", 409, False),
            ("SIP_R1_CHECKOUT_REQUIRED", 409, False), ("SIP_R1_CHECKOUT_DENIED", 403, False),
            ("SIP_R1_CHECKOUT_EXPIRED", 409, False), ("SIP_R1_CHECKOUT_STALE", 409, False),
            ("SIP_R1_CHECKOUT_CONFLICT", 409, True)):
        with monkeypatch.context() as patch:
            def fail(*args, **kwargs):
                raise project.ProjectContextError(reason)
            patch.setattr(project, "verify_at_admission", fail)
            refusal(run(lane), reason, status, retry=retry)
    with monkeypatch.context() as patch:
        def missing(*args):
            raise ValueError("X-Project-Id is required with X-Org-Id")
        patch.setattr(platform_link, "resolve_submission_context", missing)
        refusal(run(lane), None, 400, "X-Project-Id is required with X-Org-Id")
    assert not lane.a.accepted


def test_sip_r5_entitlement_order(lane, monkeypatch):
    monkeypatch.setenv("LEAF_SOLAR_PROJECT_RUN_ENABLED", "1")
    policy_cases.watch_prepare(monkeypatch, lane.a.events)
    denied = entitlements.entitlement_denied_response("run_write", "restricted")
    with monkeypatch.context() as patch:
        patch.setattr(entitlements, "entitlements_for", lambda *args: {})
        patch.setattr(entitlements, "entitlement_denied_response", lambda *args: denied)
        response = run(lane)
        assert specimen(response)[:2] == (denied.status_code, denied.body)
        assert not lane.a.events
    def stored_denial(*args):
        lane.a.events.append("stored")
        return denied, None
    monkeypatch.setattr(stored, "stored_job_entitlement_verdict", stored_denial)
    assert run(lane).json() == json.loads(denied.body)
    assert [event for event in lane.a.events if event in ("prepare", "stored")] == ["prepare", "stored"]
    unavailable = stored.policy_unavailable_response(None, "run")
    monkeypatch.setattr(stored, "stored_job_entitlement_verdict", lambda *args: (unavailable, None))
    response = run(lane)
    assert response.status_code == 503 and response.json() == json.loads(unavailable.body)
    assert not lane.a.accepted


def test_sip_r5_replay(lane, monkeypatch):
    monkeypatch.setenv("LEAF_SOLAR_PROJECT_RUN_ENABLED", "1")
    row = replay_row(lane, monkeypatch)
    # The request is built before the stubs go in: building it reads the tool, and from here on
    # a tool read, an admission check or a publication is exactly what a replay must not do.
    params = body(lane)["params"]
    for target, name in ((project, "verify_at_admission"),
            (service, "_validate_new_project_graph_params"), (stored, "stored_job_entitlement_verdict"),
            (graph, "_tool"), (graph, "publish_project_graph_commit")):
        monkeypatch.setattr(target, name, policy_cases.r4.forbidden)
    for status in ("queued", "succeeded"):
        row["status"] = status
        response = run(lane, params, request_headers=headers(lane, **{"X-Checkout-Capability": "expired"}))
        assert response.status_code == 202 and response.json()["job_id"] == row["job_id"]
    assert states(preview(lane, params))["solar-settings"]["admission"] == {"status_code": 200, "error": None}
    assert not lane.a.accepted and "entitlement" not in lane.a.events and not lane.s.writes


def test_sip_r5_replay_conflict(lane, monkeypatch):
    monkeypatch.setenv("LEAF_SOLAR_PROJECT_RUN_ENABLED", "1")
    row = replay_row(lane, monkeypatch)
    params = body(lane)["params"]
    params["cancel"] = False
    refusal(run(lane, params), "SIP_R4_IDEMPOTENCY_CONFLICT")
    row["deleted_at"] = "deleted"
    refusal(run(lane), "SIP_R1_CONTEXT_NOT_FOUND", 404)
    assert not lane.a.accepted and "entitlement" not in lane.a.events


def test_sip_r5_catalog_unchecked(lane, monkeypatch):
    monkeypatch.setenv("LEAF_SOLAR_PROJECT_RUN_ENABLED", "1")
    monkeypatch.setattr(policy, "project_tool_admission", policy_cases.r4.forbidden)
    monkeypatch.setattr(project, "verify_at_admission", policy_cases.r4.forbidden)
    response = lane.client.get("/api/capabilities", params={"project_id": str(lane.s.project)})
    rows = states(response)
    for name in policy_cases.NINE:
        assert rows[name] == policy.project_catalog_availability(lane.tools[name], enabled=True)
        assert rows[name]["engine_ready"] and not rows[name]["admission_checked"]
    assert rows["solar-string-multi-add"]["refusal_reasons"] == ["project_adapter_unavailable"]
    lane.tools["solar-settings"]["description"] = "Changed record"
    assert states(preview(lane))["solar-settings"]["refusal_reasons"] == ["project_adapter_unavailable"]
    assert not lane.a.events


def test_sip_r5_catalog_preview(lane, monkeypatch):
    monkeypatch.setenv("LEAF_SOLAR_PROJECT_RUN_ENABLED", "1")
    monkeypatch.setattr(graph, "publish_project_graph_commit", policy_cases.r4.forbidden)
    original = policy.project_tool_admission
    calls = []
    def admission(*args, **kwargs):
        calls.append((args[0]["name"], kwargs["preview"]))
        return original(*args, **kwargs)
    monkeypatch.setattr(policy, "project_tool_admission", admission)
    build = catalog_route.catalog.build_catalog
    def duplicate(*args, **kwargs):
        families = build(*args, **kwargs)
        for family in families:
            family["capabilities"] += deepcopy(family["capabilities"])
        return families
    monkeypatch.setattr(catalog_route.catalog, "build_catalog", duplicate)
    before = deepcopy((lane.s.blobs, lane.s.versions, lane.s.fingerprints, lane.s.writes))
    state = states(preview(lane))["solar-settings"]
    assert state["runnable"] and state["admission_checked"]
    assert state["admission"] == {"status_code": 200, "error": None}
    assert calls == [("solar-settings", True)] and not lane.a.accepted
    assert (lane.s.blobs, lane.s.versions, lane.s.fingerprints, lane.s.writes) == before
    calls.clear()
    monkeypatch.setattr(service, "submit_project_graph_job", lambda *args, **kwargs:
        {"admissible": True, "replay": False})
    entries = {name: {"params": {}, "catalog_digest": deps.catalog_tool_digest(lane.tools[name]),
                      "idempotency_key": "key"} for name in policy_cases.NINE}
    response = preview(lane, project_runs=json.dumps(entries))
    assert all(states(response)[name]["runnable"] for name in policy_cases.NINE)
    assert len(calls) == 9 and len({name for name, _ in calls}) == 9
    missing = states(preview(lane, input_version_id=""))["solar-settings"]
    assert missing["admission"]["status_code"] == 400
    assert missing["admission"]["error"]["message"] == "a canonical drawing version UUID is required"


def test_sip_r5_catalog_run_parity(lane, monkeypatch):
    monkeypatch.setenv("LEAF_SOLAR_PROJECT_RUN_ENABLED", "1")
    def compare(params=None, name="solar-settings", **query):
        state = states(preview(lane, params, name, **query))[name]
        fields = {"dwg": query["input_version_id"]} if "input_version_id" in query else {}
        if "project_runs" in query:
            fields["catalog_digest"] = json.loads(query["project_runs"])[name]["catalog_digest"]
        response = run(lane, params, name, **fields)
        assert state["admission"]["error"] == response.json().get("error")
        assert state["admission"]["status_code"] == (200 if response.status_code == 202 else response.status_code)
        return state, response
    assert compare()[0]["runnable"]
    nested = body(lane)["params"]
    nested["changes"]["surprise"] = 1
    state, response = compare(nested)
    assert not state["runnable"]
    refusal(response, "INVALID_SETTINGS_REQUEST", 409, "Canonical Solar request was refused.")
    assert state["admission"]["error"] == response.json()["error"]
    entry = {"solar-settings": {"params": body(lane)["params"], "catalog_digest": "stale",
                                "idempotency_key": "key"}}
    assert not compare(project_runs=json.dumps(entry))[0]["runnable"]
    with monkeypatch.context() as patch:
        def denied(*args):
            raise HTTPException(403, "platform role does not permit mutation")
        patch.setattr(platform_link, "resolve_submission_context", denied)
        assert compare()[1].status_code == 403
    replay_row(lane, monkeypatch)
    assert compare()[1].json()["job_id"] == str(lane.s.job)
    with monkeypatch.context() as patch:
        patch.setattr(canonical_jobs, "get_project_graph_job_by_key", lambda *args, **kwargs: None)
        ctx = policy_cases.r3b.chain(lane.s, 4)[-2][2]
        policy_cases.r4.at(lane.a, patch, ctx, "solar-size-strings")
        params = {"expected_rev": 3, "mode": "manual-global", "confirm": True}
        assert compare(params, "solar-size-strings", input_version_id=str(ctx.parent_version_id))[0]["runnable"]


def test_sip_r5_catalog_request_validation(lane, monkeypatch):
    monkeypatch.setenv("LEAF_SOLAR_PROJECT_RUN_ENABLED", "1")
    monkeypatch.setattr(policy, "project_tool_admission", policy_cases.r4.forbidden)
    good = {"params": {}, "catalog_digest": "digest", "idempotency_key": "key"}
    bad = ["{" , "[]", "null",
        '{"solar-settings":{},"solar-settings":{}}',
        '{"solar-settings":{"params":{"x":1,"x":2},"catalog_digest":"d","idempotency_key":"k"}}',
        '{"solar-settings":{"params":{"x":NaN},"catalog_digest":"d","idempotency_key":"k"}}',
        '{"solar-settings":{"params":{"x":Infinity},"catalog_digest":"d","idempotency_key":"k"}}',
        '{"solar-settings":{"params":{"x":1e999},"catalog_digest":"d","idempotency_key":"k"}}',
        json.dumps({"unknown": good})]
    for value in (None, [], 1, True, "params"):
        bad.append(json.dumps({"solar-settings": {**good, "params": value}}))
    for entry in ({}, {**good, "extra": 1}, {**good, "catalog_digest": 1},
                  {**good, "idempotency_key": None}, [], None,
                  {**good, "params": {"drawing_id": str(UUID(int=999))}}):
        bad.append(json.dumps({"solar-settings": entry}))
    for text in bad:
        response = preview(lane, project_runs=text)
        assert response.status_code == 400
        assert response.json()["error"]["message"] == "project_runs must contain valid project admission requests"
    # The size bound is measured on the decoder itself: an HTTP client refuses a query this long
    # before any request leaves, so no request can carry it. One valid request padded to the
    # bound is read; one byte more is not, and neither is a valid request whose characters fit
    # the bound while its bytes do not.
    names, drawing = {"solar-settings"}, str(lane.s.drawing)
    entry = json.dumps({"solar-settings": good})
    assert catalog_route._project_requests(entry + " " * (262144 - len(entry)), names, drawing)
    wide = json.dumps({"solar-settings": {**good, "params": {"note": chr(0xE9) * 131073}}},
                      ensure_ascii=False)
    assert len(wide) <= 262144 < len(wide.encode("utf-8"))
    for text in (entry + " " * (262145 - len(entry)), wide):
        with pytest.raises(ValueError):
            catalog_route._project_requests(text, names, drawing)
    for fields, sentence in (({"project_id": str(UUID(int=999))}, "project_id must match X-Project-Id"),
            ({"drawing_version": "2"}, "drawing_version applies only to legacy versioned drawings")):
        response = preview(lane, **fields)
        assert response.status_code == 400 and response.json()["error"]["message"] == sentence
    unsupported = {"solar-string-multi-add": good}
    assert not states(preview(lane, project_runs=json.dumps(unsupported)))["solar-string-multi-add"]["admission_checked"]
    assert not lane.a.events


def test_sip_r5_uncomparable_digest_refused(lane, monkeypatch):
    monkeypatch.setattr(policy, "project_tool_admission", policy_cases.r4.forbidden)
    digest = deps.catalog_tool_digest(lane.tools["solar-settings"])
    assert route._catalog_digest_matches(digest, digest)
    near = near_miss_digests(digest)
    assert len(set(near)) == 29 and digest not in near and all(isinstance(value, str) for value in near)
    for value in (None, 1, True, digest.encode("ascii"), [digest], "", "stale", digest + "0",
                  digest[:-1], digest.upper(), " " + digest, *UNCOMPARABLE_DIGESTS, *near):
        assert route._catalog_digest_matches(value, digest) is False
    entry = lambda value: json.dumps({"solar-settings": {
        "params": body(lane)["params"], "catalog_digest": value, "idempotency_key": "key"}})
    for enabled in (True, False):
        if enabled:
            monkeypatch.setenv("LEAF_SOLAR_PROJECT_RUN_ENABLED", "1")
        else:
            monkeypatch.delenv("LEAF_SOLAR_PROJECT_RUN_ENABLED", raising=False)
        for request_headers in (headers(lane), {}):
            stale = specimen(raw_run(lane, "stale", request_headers))
            assert stale[0] == 409 and json.loads(stale[1])["error"]["message"] == DIGEST_SENTENCE
            for value in (*UNCOMPARABLE_DIGESTS, *near):
                assert specimen(raw_run(lane, value, request_headers)) == stale
        if enabled:
            stale = states(preview(lane, project_runs=entry("stale")))["solar-settings"]
            assert not stale["runnable"] and stale["admission"]["status_code"] == 409
            assert stale["admission"]["error"]["message"] == DIGEST_SENTENCE
            for value in (*UNCOMPARABLE_DIGESTS, *near):
                assert states(preview(lane, project_runs=entry(value)))["solar-settings"] == stale
    assert not lane.a.accepted and not lane.a.events


def test_sip_r5_preview_has_no_turn_gate(lane, monkeypatch):
    # Declared boundary: the catalog route takes no conversational headers, so a preview does not model
    # the run route's turn gate. A session header without its turn is refused by the run alone.
    monkeypatch.setenv("LEAF_SOLAR_PROJECT_RUN_ENABLED", "1")
    sent = headers(lane, **{"X-Authority-Session-Id": "session"})
    state = states(preview(lane, request_headers=sent))["solar-settings"]
    assert state["runnable"] and state["admission"] == {"status_code": 200, "error": None}
    response = run(lane, request_headers=sent)
    assert response.status_code == 403 and response.json()["error"]["message"] == (
        "active same-account turn authority is required for conversational runs")
    assert not lane.a.accepted


def test_sip_r5_wait_and_observation(lane, monkeypatch):
    monkeypatch.setenv("LEAF_SOLAR_PROJECT_RUN_ENABLED", "1")
    monkeypatch.setattr(route.jobs, "job_max_s", lambda: -30)
    response = run(lane, wait=1)
    job_id = response.json()["job_id"]
    assert response.status_code == 202 and response.json()["status"] == "submitted"
    record = lane.records[job_id]
    assert record["status"] == "submitted" and record["result"] is None
    monkeypatch.setattr(route.jobs, "wait_for_terminal", policy_cases.r4.forbidden)
    submitted = route.JSONResponse(status_code=202, content=response.json())
    record.update(status="complete", result={"ok": True, "output_version_id": str(UUID(int=88))})
    assert route._wait_project_solar(submitted, lane.tenant).body == route.JSONResponse(record["result"]).body
    record.update(status="failed", error=envelopes.error_obj(envelopes.ErrorCode.BAD_PARAMS, "Refused", False))
    expected = route.jobs.failed_envelope_from(record)
    terminal = route._wait_project_solar(submitted, lane.tenant)
    assert terminal.status_code == 400 and json.loads(terminal.body) == expected
    record.update(status="running", progress="running", error=None)
    lane.principal["member"] = False
    assert route._wait_project_solar(submitted, lane.tenant).status_code == 403
    lane.principal["member"] = True
    monkeypatch.setattr(route.jobs, "job_max_s", lambda: 0)
    async def consume():
        stream = await route.stream_job(job_id, lane.tenant)
        assert json.loads((await anext(stream.body_iterator)).removeprefix("data: "))["status"] == "running"
        lane.principal["member"] = False
        assert json.loads((await anext(stream.body_iterator)).removeprefix("data: ")) == {
            "job_id": job_id, "status": "unknown"}
        with pytest.raises(StopAsyncIteration):
            await anext(stream.body_iterator)
    asyncio.run(consume())
    record.update(status="complete", result={"ok": True})
    for enabled in (True, False):
        if enabled:
            monkeypatch.setenv("LEAF_SOLAR_PROJECT_RUN_ENABLED", "1")
        else:
            monkeypatch.delenv("LEAF_SOLAR_PROJECT_RUN_ENABLED", raising=False)
        lane.principal.update(tenant=lane.tenant, member=True)
        observed = lane.client.get(f"/api/jobs/{job_id}")
        assert observed.status_code == 200 and observed.json() == route._record_body(record)
        assert lane.client.get(f"/api/jobs/{job_id}", params={"project_id": str(UUID(int=999))}).status_code == 200
        lane.principal["tenant"] = deps.TenantContext(str(lane.s.org), tier="hosted_pro", subject="other-member")
        assert lane.client.get(f"/api/jobs/{job_id}").status_code == 200
        lane.principal["tenant"] = lane.tenant
        lane.principal["member"] = False
        denied = lane.client.get(f"/api/jobs/{job_id}")
        assert denied.status_code == 403
        assert denied.json() == envelopes.err_envelope(
            envelopes.ErrorCode.BAD_PARAMS, "project access forbidden", False)
        assert lane.client.get("/api/jobs").json()["jobs"] == []
        lane.principal["tenant"] = deps.TenantContext(str(UUID(int=999)), tier="hosted_pro", subject="foreign")
        refusal(lane.client.get(f"/api/jobs/{job_id}"), None, 404, f"unknown job_id: {job_id}")
        assert lane.client.get("/api/jobs").json()["jobs"] == []
    lane.principal.update(tenant=lane.tenant, member=True)
    for fields in ({"org_id": "bad"}, {"project_id": "bad"}, {"org_id": str(UUID(int=999))}):
        corrupt = {**record, **fields}
        assert route._access_error(corrupt, lane.tenant, job_id).status_code == 404
    unverified = deps.TenantContext(str(lane.s.org), tier="hosted_pro")
    assert route._access_error(record, unverified, job_id).status_code == 404
    with monkeypatch.context() as patch:
        patch.setattr(platform_link, "require_project_access", lambda *args, **kwargs: str(UUID(int=999)))
        assert route._access_error(record, lane.tenant, job_id).status_code == 404
    del lane.records[job_id]
    assert route._wait_project_solar(submitted, lane.tenant).status_code == 500


def test_sip_r5_standalone_bytes(lane, monkeypatch):
    monkeypatch.setattr(policy, "project_tool_admission", policy_cases.r4.forbidden)
    monkeypatch.setattr(platform_link, "resolve_submission_context", lambda *args: None)
    monkeypatch.setattr(entitlements, "w1_tool_availability", lambda *args, **kwargs: None)
    monkeypatch.setattr(route, "_checkout_identity", lambda *args: ("Editor", 1))
    monkeypatch.setattr(route.jobs, "submit_job", lambda *args, **kwargs: "legacy-job")
    def samples():
        return [specimen(run(lane, dwg=str(lane.s.drawing), dwg_version=1, request_headers={})),
                specimen(run(lane, catalog_digest="stale", request_headers={})),
                specimen(lane.client.get("/api/capabilities", params={"project_runs": "invalid"}))]
    monkeypatch.delenv("LEAF_SOLAR_PROJECT_RUN_ENABLED", raising=False)
    expected = samples()
    monkeypatch.setenv("LEAF_SOLAR_PROJECT_RUN_ENABLED", "1")
    assert samples() == expected and expected[0][0] == 202
    assert not lane.a.accepted


def test_sip_r5_solver_and_generic_read_isolation(lane, monkeypatch):
    monkeypatch.setattr(policy, "project_tool_admission", policy_cases.r4.forbidden)
    monkeypatch.setattr(entitlements, "entitlements_for", lambda *args: {"run_read": True, "run_write": True})
    monkeypatch.setattr(entitlements, "w1_tool_availability", lambda *args, **kwargs: None)
    monkeypatch.setattr(route, "_checkout_identity", lambda *args: ("Editor", 1))
    solver = {"name": "string-autofill-opt", "canonical_only": True, "capabilities": ["drawing.read"],
              "default_params": {}}
    lane.tools[solver["name"]] = solver
    calls = []
    monkeypatch.setattr(platform_link, "submit_canonical_solve", lambda *args:
        calls.append(args) or "solver-job")
    ordinary = {"name": "ordinary-read", "capabilities": ["drawing.read"], "default_params": {}}
    lane.tools[ordinary["name"]] = ordinary
    reads = []
    monkeypatch.setattr(route, "_run_project_read", lambda *args:
        reads.append(args) or route.JSONResponse(status_code=200, content={"ok": True}))
    for enabled in (False, True):
        if enabled:
            monkeypatch.setenv("LEAF_SOLAR_PROJECT_RUN_ENABLED", "1")
        else:
            monkeypatch.delenv("LEAF_SOLAR_PROJECT_RUN_ENABLED", raising=False)
        assert run(lane, {"groups": [{}], "panelsPerString": 10}, solver["name"]).status_code == 202
        assert run(lane, {}, ordinary["name"]).status_code == 200
        for context in (None, [], {"schema": "another.schema"}):
            rec = {"job_id": "ordinary-job", "tenant_id": str(lane.tenant), "status": "complete",
                   "result": {}, "error": None}
            if context is not None:
                rec["execution_context"] = context
            before = len(lane.calls)
            assert route._access_error(rec, lane.tenant, rec["job_id"]) is None
            assert len(lane.calls) == before
    assert len(calls) == len(reads) == 2
    ctx = campaign_cases.completion_context()
    ctx["tenant_id"] = ctx["org_id"]
    caller = deps.TenantContext(ctx["org_id"], tier="hosted_pro", subject="campaign-reader")
    rec = {"job_id": "campaign-job", "tenant_id": ctx["org_id"], "org_id": ctx["org_id"],
        "project_id": ctx["project_id"], "tool": ctx["tool_name"], "completion_provenance": ctx,
        "status": "running"}
    monkeypatch.setattr(platform_link, "require_project_access", lambda *args, **kwargs: ctx["org_id"])
    assert route._access_error(rec, caller, "campaign-job") is None
    closed = []
    monkeypatch.setattr(route.jobs, "get_job", lambda job: rec if job == "campaign-job" else None)
    monkeypatch.setattr(route.jobs, "mark_job_closed", lambda job: closed.append(job) or True)
    assert route.close_job("campaign-job", caller)["closed"] is True and closed == ["campaign-job"]
    lane.records["graph-only"] = {"tenant_id": str(lane.tenant)}
    assert route.close_job("graph-only", lane.tenant).status_code == 404
    with monkeypatch.context() as patch:
        patch.setattr(deps, "find_tool", _FIND_TOOL)
        patch.setattr(deps, "load_engine_registry_tools", _ENGINE_TOOLS)
        patch.setattr(route, "_run_project_read", _PROJECT_READ)
        memory = read_cases.memory.__wrapped__(patch)
        generator = read_cases.lane.__wrapped__(memory, patch)
        actual = next(generator)
        try:
            for enabled in (False, True):
                if enabled:
                    patch.setenv("LEAF_SOLAR_PROJECT_RUN_ENABLED", "1")
                else:
                    patch.delenv("LEAF_SOLAR_PROJECT_RUN_ENABLED", raising=False)
                read_cases.success(actual, read_cases.request(actual))
        finally:
            with pytest.raises(StopIteration):
                next(generator)
