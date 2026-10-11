"""Authenticated Solar output HTTP contracts and neighboring response preservation."""
from copy import deepcopy
from hashlib import sha256
import json
from types import SimpleNamespace
from uuid import UUID

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from fastapi.responses import JSONResponse
import deps
import entitlements
import envelopes
import solar_artifacts as standalone
import solar_project_context as project
import solar_project_read as read
import solar_project_artifacts as artifacts
from leaf_cloud_client import canonical_bytes
from solar_design_graph import GraphValidationError
from routers import project_drawings as routes, drawings, jobs, capabilities
from route_flatten import iter_leaf_routes
import test_sip_r6_read as reads
import test_sip_r6_artifacts as files
import test_sip_r5_routes as admission_cases
import test_project_scoped_read_tool as generic_cases

_FIND_TOOL = deps.find_tool
_ENGINE_TOOLS = deps.load_engine_registry_tools
_ACCESS = project._access
_DYNAMIC_READ = generic_cases.tool_loader.run_tool_dynamic


@pytest.fixture
def http(monkeypatch):
    s = reads.setup(monkeypatch)
    files.immutable(s, monkeypatch)
    principal = {"tenant": s.tenant, "member": True, "calls": 0}
    def access(tenant, proj, *, write):
        principal["calls"] += 1
        if str(tenant) != str(s.org) or proj != s.project:
            project.refuse("CONTEXT_NOT_FOUND")
        if not principal["member"]:
            project.refuse("PROJECT_FORBIDDEN")
        assert write is False
        return s.org, s.actor
    monkeypatch.setattr(project, "_access", access)
    app = FastAPI()
    envelopes.install_error_handlers(app)
    app.middleware("http")(routes.no_store_responses)
    app.include_router(routes.router)
    app.include_router(drawings.router)
    app.dependency_overrides[deps.require_active_tenant] = lambda: principal["tenant"]
    app.dependency_overrides[deps.require_tenant] = lambda: principal["tenant"]
    with TestClient(app) as client:
        yield SimpleNamespace(s=s, client=client, app=app, principal=principal)


def read_path(http, version=None):
    s = http.s
    return f"/api/projects/{s.project}/drawings/{s.drawing}/versions/{version or s.head}/solar-reads"


def file_path(http, artifact_id):
    s = http.s
    return f"/api/projects/{s.project}/drawings/{s.drawing}/solar-artifacts/{artifact_id}"


def body(tool=reads.CALC, params=None, **changes):
    result = {"tool": tool, "params": deepcopy(reads.NEC[reads.CALC] if params is None else params),
              "catalog_digest": reads.tools_cases.manifest(tool)}
    result.update(changes)
    return result


def post(http, **changes):
    return http.client.post(read_path(http), json=body(**changes))


def refusal(response, reason, status, sentence=None):
    assert response.status_code == status, response.text
    error = response.json()["error"]
    assert error["reason_code"] == reason
    assert error["error_code"] == ("FORBIDDEN" if status == 403 else "INTERNAL" if status >= 500 else "BAD_PARAMS")
    assert response.headers["cache-control"] == "no-store"
    if sentence is not None:
        assert error["message"] == sentence
    return error


def specimen(response):
    stable = ("content-type", "cache-control", "etag", "x-leaf-artifact-id",
              "x-content-type-options", "content-disposition", "x-leaf-source-version")
    return response.status_code, response.content, {key: response.headers[key] for key in stable if key in response.headers}


def forged(s, prepared, **changes):
    meta = {**prepared.meta, **changes}
    binding = {key: meta[key] for key in artifacts._BINDING_KEYS}
    binding["schema"] = artifacts.BINDING_SCHEMA
    meta["artifact_id"] = sha256(canonical_bytes(binding)).hexdigest()
    s.blobs[artifacts._key(meta, meta["artifact_id"])] = canonical_bytes(meta)
    return meta["artifact_id"]


def test_sip_r6_routes_mounted_and_flagged(http, monkeypatch):
    expected = {
        ("POST", "/api/projects/{project_id}/drawings/{drawing_id}/versions/{input_version_id}/solar-reads"),
        ("GET", "/api/projects/{project_id}/drawings/{drawing_id}/solar-artifacts/{artifact_id}")}
    counts = dict.fromkeys(expected, 0)
    for path, route in iter_leaf_routes(http.app.routes):
        for method in getattr(route, "methods", ()) or ():
            if (method, path) in expected:
                counts[method, path] += 1
                assert deps.require_active_tenant in [dep.call for dep in route.dependant.dependencies]
    assert set(counts.values()) == {1}
    for value in (None, "0", "true", "1 ", " 1", "01", "", "1"):
        if value is None:
            monkeypatch.delenv("LEAF_SOLAR_PROJECT_RUN_ENABLED", raising=False)
        else:
            monkeypatch.setenv("LEAF_SOLAR_PROJECT_RUN_ENABLED", value)
        if value == "1":
            assert post(http).status_code == 200
        else:
            before = list(http.s.reads), http.principal["calls"]
            for response in (post(http), http.client.get(file_path(http, "0" * 64))):
                refusal(response, "project_execution_disabled", 409,
                        "Project Solar outputs are not enabled on this server.")
            assert (http.s.reads, http.principal["calls"]) == before


def test_sip_r6_request_validation(http):
    sentence = "Project Solar read request is invalid."
    writes = list(http.s.writes)
    for raw in (b"[]", b"null", b'"text"', b"{", b"{}", b'{"tool":1}',
                b'{"tool":"x","tool":"y","catalog_digest":"a"}',
                b'{"tool":"x","catalog_digest":"a","params":{"x":1,"x":2}}',
                b'{"tool":"x","catalog_digest":"a","params":{"x":NaN}}',
                b'{"tool":"x","catalog_digest":"a","params":{"x":Infinity}}',
                b'{"tool":"x","catalog_digest":"a","params":{"x":1e999}}'):
        refusal(http.client.post(read_path(http), content=raw), "SIP_R6_INVALID_REQUEST", 400, sentence)
    for change in ({"params": []}, {"params": None}, {"current": 1}, {"current": "true"},
                   {"tool": ""}, {"catalog_digest": ""}, {"catalog_digest": 1},
                   {"job_id": "supplied"}, {"checkout": "cap"}, {"source_version": 1},
                   {"params": {**reads.NEC[reads.CALC], "drawing_id": str(UUID(int=999))}}):
        # Each case is sent as written: body(**change) would read params=None as the defaults
        # and would hash a manifest for a tool named "" before any request is made.
        refusal(http.client.post(read_path(http), json={**body(), **change}),
                "SIP_R6_INVALID_REQUEST", 400, sentence)
    refusal(http.client.post(read_path(http), content=b" " * 262145),
            "SIP_R6_REQUEST_TOO_LARGE", 413, "Project Solar read request is too large.")
    for identity in (http.s.project, http.s.drawing, http.s.head):
        refusal(http.client.post(read_path(http).replace(str(identity), "invalid"), json=body()),
                "SIP_R6_INVALID_REQUEST", 400, sentence)
    for header in ("X-Org-Id", "X-Project-Id"):
        refusal(http.client.post(read_path(http), json=body(), headers={header: "invalid"}),
                "SIP_R6_INVALID_REQUEST", 400, sentence)
        refusal(http.client.post(read_path(http), json=body(), headers={header: str(UUID(int=999))}),
                "SIP_R1_CONTEXT_NOT_FOUND", 404, "Project drawing operation unavailable.")
    assert http.s.writes == writes


def test_sip_r6_read_response_binding(http):
    response = post(http)
    assert response.status_code == 200 and response.headers["cache-control"] == "no-store"
    result = response.json()["result"]
    assert set(result) == {"schema", "adapter", "organization_id", "project_id", "drawing_id",
        "input_version_id", "input_intake_sha256", "graph_sha256", "graph_project_id",
        "head_version_id", "is_head", "current_required", "tool", "tool_manifest_sha256",
        "request_sha256", "output", "output_sha256", "output_bytes", "drawing_changed"}
    assert result["schema"] == "leaf.solar-project-read.v1"
    assert result["adapter"] == "project-local-graph-read"
    for key, identity in (("organization_id", http.s.org), ("project_id", http.s.project),
                          ("drawing_id", http.s.drawing), ("input_version_id", http.s.head)):
        assert result[key] == str(identity)
    assert result["input_intake_sha256"] == http.s.ctx.intake_sha256
    assert result["graph_sha256"] == http.s.ctx.graph_sha256
    assert result["graph_project_id"] == http.s.ctx.graph_project_id != str(http.s.project)
    request = {key: result[key] for key in ("organization_id", "project_id", "drawing_id",
        "input_version_id", "input_intake_sha256", "graph_sha256", "tool", "tool_manifest_sha256")}
    request.update(schema="leaf.solar-project-read-request.v1", parameters=reads.NEC[reads.CALC])
    assert result["request_sha256"] == sha256(canonical_bytes(request)).hexdigest()
    assert result["output_sha256"] == sha256(canonical_bytes(result["output"])).hexdigest()
    assert result["output_bytes"] == len(canonical_bytes(result["output"]))
    assert result["is_head"] is True and result["current_required"] is False
    assert result["drawing_changed"] is False and "job_id" not in response.text
    for private in ("tenants/", "intake_ref", "checkout_capability", "verified-editor"):
        assert private not in response.text


def test_sip_r6_membership_and_foreign_scope(http, monkeypatch):
    prepared = files.save(http.s)
    for role in ("viewer", "editor"):
        http.principal["tenant"] = deps.TenantContext(str(http.s.org), org_id=str(http.s.org),
            tier="hosted_pro", subject=role)
        assert post(http).status_code == 200
        assert http.client.get(file_path(http, prepared.meta["artifact_id"])).content == prepared.content
    for tenant, member, status, reason in (
        (http.s.tenant, False, 403, "SIP_R1_PROJECT_FORBIDDEN"),
        (deps.TenantContext(str(UUID(int=999)), tier="hosted_pro", subject="foreign"),
         True, 404, "SIP_R1_CONTEXT_NOT_FOUND")):
        http.principal.update(tenant=tenant, member=member)
        before = list(http.s.reads)
        refusal(post(http), reason, status, "Project drawing operation unavailable.")
        refusal(http.client.get(file_path(http, prepared.meta["artifact_id"])), reason, status,
                "Project drawing operation unavailable.")
        assert http.s.reads == before
    http.principal.update(tenant=http.s.tenant, member=True)
    original = project.resolve_context
    calls = []
    def revoked(*args, **kwargs):
        calls.append(True)
        if len(calls) == 2:
            http.principal["member"] = False
        return original(*args, **kwargs)
    value = standalone.ArtifactOutput({}, "application/json", "Deferred.json", b"{}")
    before = list(http.s.writes)
    monkeypatch.setattr(project, "resolve_context", revoked)
    monkeypatch.setattr(reads.local._load_builtin(reads.CALC), "run", lambda *args: value)
    refusal(post(http), "SIP_R1_PROJECT_FORBIDDEN", 403)
    assert http.s.writes == before


def test_sip_r6_download_and_etag(http):
    files.string_source(http.s)
    generated = post(http, tool="solar-string-data", params={})
    assert generated.status_code == 200, generated.text
    ref = generated.json()["result"]["output"]["artifact"]
    path = ref["download"]
    response = http.client.get(path)
    assert response.status_code == 200 and len(response.content) == 973
    assert response.content == reads.local._load_builtin("solar-string-data").run(http.s.ctx.graph, {}).content
    assert response.headers["content-type"] == "application/json"
    assert response.headers["etag"] == '"' + ref["content_sha256"] + '"'
    assert response.headers["x-leaf-artifact-id"] == ref["artifact_id"]
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.headers["content-disposition"] == 'attachment; filename="StringData.json"'
    assert "x-leaf-source-version" not in response.headers
    conditional = http.client.get(path, headers={"If-None-Match": response.headers["etag"]})
    assert conditional.status_code == 304 and conditional.content == b""
    assert conditional.headers["etag"] == response.headers["etag"]
    assert http.client.get(path, headers={"If-None-Match": '"other"'}).status_code == 200
    http.principal["member"] = False
    refusal(http.client.get(path, headers={"If-None-Match": response.headers["etag"]}),
            "SIP_R1_PROJECT_FORBIDDEN", 403)


def test_sip_r6_download_refusals(http, monkeypatch):
    prepared = files.save(http.s)
    path = file_path(http, prepared.meta["artifact_id"])
    for reason, (status, sentence) in routes._OUTPUT_FAILURES.items():
        with monkeypatch.context() as patch:
            def failure(*args, **kwargs):
                raise GraphValidationError(reason)
            patch.setattr(artifacts, "read_project_artifact", failure)
            error = refusal(http.client.get(path), reason, status, sentence)
            assert error["retryable"] is (reason in ("ARTIFACT_STORE_UNAVAILABLE", "ARTIFACT_WRITES_DRAINED"))
    for reason, status, retry in (("CONTEXT_NOT_FOUND", 404, False), ("PROJECT_FORBIDDEN", 403, False),
                                ("INTAKE_DIGEST_MISMATCH", 500, False), ("STORE_UNAVAILABLE", 503, True)):
        with monkeypatch.context() as patch:
            patch.setattr(artifacts, "read_project_artifact", lambda *args, **kwargs: project.refuse(reason))
            assert refusal(http.client.get(path), "SIP_R1_" + reason, status,
                "Project drawing operation unavailable.")["retryable"] is retry
    refusal(http.client.get(file_path(http, "invalid")), "ARTIFACT_ID_INVALID", 400)
    refusal(http.client.get(file_path(http, "0" * 64)), "ARTIFACT_NOT_FOUND", 404)
    http.s.blobs[files.blob_key(prepared)] = b"tampered"
    refusal(http.client.get(path, headers={"If-None-Match": '"' + prepared.meta["content_sha256"] + '"'}),
            "ARTIFACT_CORRUPT", 500)
    http.s.blobs[files.blob_key(prepared)] = prepared.content
    fake = forged(http.s, prepared, source_intake_sha256="d" * 64)
    refusal(http.client.get(file_path(http, fake)), "SIP_R6_ARTIFACT_SOURCE_MISMATCH", 500)
    http.s.head = next(v.version_id for v in http.s.versions.values() if v.seq == 2)
    refusal(http.client.get(path + "?current=true"), "ARTIFACT_STALE", 409)
    assert http.client.get(path).status_code == 200
    original = project._access
    accesses = []
    def revoked(tenant, proj, *, write):
        accesses.append(True)
        if len(accesses) == 3:
            http.principal["member"] = False
        return original(tenant, proj, write=write)
    monkeypatch.setattr(project, "_access", revoked)
    refusal(http.client.get(path, headers={"If-None-Match": "*"}), "SIP_R1_PROJECT_FORBIDDEN", 403)


def test_sip_r6_entitlements_and_sanitization(http, monkeypatch):
    before = list(http.s.reads), list(http.s.writes)
    with monkeypatch.context() as patch:
        patch.setattr(entitlements, "entitlements_for", lambda *args: {"run_read": False})
        expected = entitlements.entitlement_denied_response("run_read", "hosted_pro")
        response = post(http)
        assert response.status_code == expected.status_code and response.content == expected.body
        assert (http.s.reads, http.s.writes) == before
    with monkeypatch.context() as patch:
        def unavailable(*args):
            raise entitlements.EntitlementsError("private policy detail")
        patch.setattr(entitlements, "entitlements_for", unavailable)
        expected = entitlements.policy_unavailable_response("run_read", "hosted_pro")
        response = post(http)
        assert response.status_code == expected.status_code and response.content == expected.body
        assert (http.s.reads, http.s.writes) == before
    denial = JSONResponse(content={"policy": "stored refusal"}, status_code=403)
    with monkeypatch.context() as patch:
        seen = []
        patch.setattr(read.stored, "stored_job_entitlement_verdict", lambda org, kind:
            (seen.append((org, kind)) or denial, None))
        patch.setattr(reads.local, "_read_output", reads.blocked)
        response = post(http)
        assert response.status_code == 403 and response.content == denial.body
        assert seen == [(http.s.org, "extract")] and http.s.writes == before[1]
    with monkeypatch.context() as patch:
        def unexpected(*args, **kwargs):
            raise RuntimeError("private key SQL token detail")
        patch.setattr(read, "run_project_read", unexpected)
        refusal(post(http), "SIP_R6_INTERNAL", 500, "Project Solar output is unavailable.")
        assert "private" not in post(http).text
    for reason in routes._INVALID_ARTIFACT:
        response = routes._output_failure(reason)
        assert response.status_code == 500
        assert json.loads(response.body)["error"]["message"] == "The Solar read produced an invalid artifact."


def _r1_specimens(http, monkeypatch):
    """R1's five neighbouring responses under this file's fixture, once per flag state."""
    s = http.s
    monkeypatch.setattr(project, "checkout_status", lambda *args: SimpleNamespace(
        organization_id=s.org, checkout=None, observed_at=s.lease.acquired_at))
    monkeypatch.setattr(project, "acquire_project_checkout", lambda *args, **kwargs: (s.lease, "cap"))
    monkeypatch.setattr(project, "release_project_checkout", lambda *args, **kwargs: True)
    monkeypatch.setattr(drawings, "_backend", lambda tenant: s)
    legacy = standalone.ArtifactSink(s, str(s.org), str(s.drawing), {
        "project_id": s.ctx.graph_project_id, "resolved_version": 1,
        "graph_sha256": s.ctx.graph_sha256}, reads.CALC, "c" * 64, False)
    prepared = legacy.prepare(standalone.ArtifactOutput({}, "application/json", "Legacy.json", b"{}"))
    legacy.finish(prepared)
    context = f"/api/projects/{s.project}/drawing-versions/{s.head}/context?drawing_id={s.drawing}"
    checkout = f"/api/projects/{s.project}/drawings/{s.drawing}/checkout"
    standalone_path = f"/api/drawings/{s.drawing}/artifacts/{prepared.meta['artifact_id']}"
    specimens = []
    for enabled in (False, True):
        if enabled:
            monkeypatch.setenv("LEAF_SOLAR_PROJECT_RUN_ENABLED", "1")
        else:
            monkeypatch.delenv("LEAF_SOLAR_PROJECT_RUN_ENABLED", raising=False)
        specimens.append([specimen(http.client.request(method, path)) for method, path in (
            ("GET", context), ("GET", checkout), ("POST", checkout), ("DELETE", checkout),
            ("GET", standalone_path))])
    return specimens


def test_sip_r6_existing_responses_unchanged(http, monkeypatch):
    specimens = _r1_specimens(http, monkeypatch)
    assert specimens[0] == specimens[1]
    assert [row[0] for row in specimens[0]] == [200] * 5
    with monkeypatch.context() as patch:
        patch.setattr(deps, "find_tool", _FIND_TOOL)
        patch.setattr(deps, "load_engine_registry_tools", _ENGINE_TOOLS)
        patch.setattr(project, "_access", _ACCESS)
        def stable_read(*args, **kwargs):
            result = _DYNAMIC_READ(*args, **kwargs)
            result["timing_ms"] = 0
            return result
        patch.setattr(generic_cases.tool_loader, "run_tool_dynamic", stable_read)
        memory = generic_cases.memory.__wrapped__(patch)
        generator = generic_cases.lane.__wrapped__(memory, patch)
        actual = next(generator)
        try:
            for enabled in (False, True):
                if enabled:
                    patch.setenv("LEAF_SOLAR_PROJECT_RUN_ENABLED", "1")
                else:
                    patch.delenv("LEAF_SOLAR_PROJECT_RUN_ENABLED", raising=False)
                before = generic_cases.request(actual)
                generic_cases.success(actual, before)
                actual.client.app.include_router(routes.router)
                after = generic_cases.request(actual)
                generic_cases.success(actual, after)
                assert specimen(before) == specimen(after)
        finally:
            with pytest.raises(StopIteration):
                next(generator)
    with monkeypatch.context() as patch:
        generator = admission_cases.lane.__wrapped__(patch)
        actual = next(generator)
        try:
            invalid = {"expected_rev": 0, "surprise": True}
            for enabled in (False, True):
                if enabled:
                    patch.setenv("LEAF_SOLAR_PROJECT_RUN_ENABLED", "1")
                else:
                    patch.delenv("LEAF_SOLAR_PROJECT_RUN_ENABLED", raising=False)
                before = [specimen(admission_cases.run(actual, invalid)),
                          specimen(admission_cases.preview(actual, invalid)),
                          specimen(actual.client.get("/api/jobs/unknown"))]
                actual.client.app.include_router(routes.router)
                after = [specimen(admission_cases.run(actual, invalid)),
                         specimen(admission_cases.preview(actual, invalid)),
                         specimen(actual.client.get("/api/jobs/unknown"))]
                assert before == after
        finally:
            with pytest.raises(StopIteration):
                next(generator)


# R1's five neighbouring responses (the project context GET, checkout GET, POST and DELETE, and the
# standalone artifact GET) as measured on main 9b7366cc, the parent of this change, with this file's
# fixture: status, the sha256 of the body and the stable headers. Both flag states must return exactly
# these. The row above compares the two flag states with each other, so a change that alters R1's
# responses in both states at once passes it; this row compares them with main.
_R1_JSON_HEADERS = {"cache-control": "no-store", "content-type": "application/json"}
_R1_ON_MAIN = [
    # project context GET
    (200, "94f85a53f91439091f37640396e96b17be525ce37041590ad4dd2ea01c564fd0",
     _R1_JSON_HEADERS),
    # checkout GET
    (200, "f8b011d6fe81ff3dd7cf998b0a35903a97cd69a16d65857fed7ff73dbbec23d4",
     _R1_JSON_HEADERS),
    # checkout POST
    (200, "f7d0ce9dd4fba070fa6a52bee1b4344d5bd1f0496c92b7da27fa5e5c8992bc13",
     _R1_JSON_HEADERS),
    # checkout DELETE
    (200, "4f134f844e172589b75b6bcf037d76363ac7534f318631ffc8ec9134213c8fc8",
     _R1_JSON_HEADERS),
    # standalone artifact GET
    (200, "44136fa355b3678a1146ad16f7e8649e94fb4fc21fe77e8310c060f61caaff8a",
     {
        "cache-control": "private, no-cache",
        "content-disposition": "attachment; filename=\"Legacy.json\"",
        "content-type": "application/json",
        "etag": "\"44136fa355b3678a1146ad16f7e8649e94fb4fc21fe77e8310c060f61caaff8a\"",
        "x-content-type-options": "nosniff",
        "x-leaf-artifact-id": "6c2072cec098e1982c7e5b0d5d56e30578a2cb088c5234c35c0b29e4e215b430",
        "x-leaf-source-version": "1"}),
]


def test_sip_r6_existing_responses_match_main(http, monkeypatch):
    for state in _r1_specimens(http, monkeypatch):
        assert [(status, sha256(content).hexdigest(), headers)
                for status, content, headers in state] == _R1_ON_MAIN


_UNSET = object()


def test_sip_r6_parameters_refuse_directly():
    """The service validates its own inputs, whatever the route checked before it."""
    project_id, drawing_id, version_id = UUID(int=1), UUID(int=2), UUID(int=3)

    def call(project_id=project_id, drawing_id=drawing_id, version_id=version_id, tool=reads.CALC,
             params=_UNSET, digest="d" * 64, current=False):
        return read._parameters(project_id, drawing_id, version_id, tool,
                                {"value": 1} if params is _UNSET else params, digest, current)

    assert call() == {"value": 1}
    assert call(params={"value": 1, "drawing_id": str(drawing_id)}) == {"value": 1}
    assert call(current=True) == {"value": 1}
    for case in (dict(tool=""), dict(tool=1), dict(tool=None),
                 dict(params=[]), dict(params=None), dict(params="value"), dict(params=1),
                 dict(digest=""), dict(digest=1), dict(digest=None),
                 dict(current=1), dict(current=0), dict(current="true"), dict(current=None),
                 dict(project_id=str(project_id)), dict(drawing_id=str(drawing_id)),
                 dict(version_id=str(version_id)), dict(version_id=3),
                 dict(params={"value": 1, "drawing_id": str(UUID(int=9))}),
                 dict(params={"value": 1, "drawing_id": 2})):
        with pytest.raises(GraphValidationError) as raised:
            call(**case)
        assert raised.value.code == "SIP_R6_INVALID_REQUEST", case


def test_sip_r6_route_refuses_before_service(http, monkeypatch):
    """A malformed body is refused by the route itself: the service is never called."""
    calls = []
    real = read.run_project_read

    def spy(*args, **kwargs):
        calls.append((args, kwargs))
        return real(*args, **kwargs)

    monkeypatch.setattr(read, "run_project_read", spy)
    sentence = "Project Solar read request is invalid."
    for change in ({"params": []}, {"params": None}, {"params": "value"}, {"tool": ""}, {"tool": 1},
                   {"catalog_digest": ""}, {"catalog_digest": 1}, {"current": 1},
                   {"current": "true"}):
        refusal(http.client.post(read_path(http), json={**body(), **change}),
                "SIP_R6_INVALID_REQUEST", 400, sentence)
    assert calls == []
    response = post(http)
    assert response.status_code == 200 and len(calls) == 1
    args, kwargs = calls[0]
    assert args[1:] == (UUID(str(http.s.project)), UUID(str(http.s.drawing)), UUID(str(http.s.head)),
                        reads.CALC, reads.NEC[reads.CALC])
    assert kwargs == {"catalog_digest": body()["catalog_digest"], "current": False}
