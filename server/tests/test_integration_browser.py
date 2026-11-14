"""Browser bridge protocol checks with synthetic authority and an inert upstream."""
import asyncio
import base64
import gzip
import inspect
import json
from types import SimpleNamespace

import httpx
import pytest
from fastapi import FastAPI

import deps
import service_browser_sessions as sessions
from integration_config import IntegrationConfigError
from routers import integration_browser as bridge

LOCAL = "https://platform-staging.leafdesign.ai"
UPSTREAM = "https://integrations.example"
PROJECT = "11111111-1111-4111-8111-111111111111"
OTHER = "22222222-2222-4222-8222-222222222222"
BINDING = "33333333-3333-4333-8333-333333333333"
SUBJECT = "fixture-service@clients"
TOKEN = "fixture.invalid.signature"  # Synthetic, never a provider credential.
SECRET = base64.urlsafe_b64encode(b"s" * 32).decode().rstrip("=")
SCOPE = {"tenantId": OTHER, "projectId": PROJECT, "platformUserId": OTHER,
         "bindingId": BINDING, "externalAuthority": "auth0", "externalSubject": SUBJECT,
         "projectRole": "owner"}
SCOPE_PATH = f"/api/v1/projects/{PROJECT}/scope"
ELEMENT_PATH = f"/api/v1/projects/{PROJECT}/integrations/elements"


@pytest.fixture
def context(monkeypatch):
    state = {"calls": [], "revoked": False, "respond": lambda _: httpx.Response(200, json=SCOPE)}
    monkeypatch.setattr(sessions, "config", lambda: SimpleNamespace(origin=LOCAL, environment="staging"))

    def resolve(cfg, binding, secret):
        if state["revoked"] or binding != BINDING or secret != SECRET:
            sessions.deny()
        return {"subject": SUBJECT, "binding_id": BINDING}, TOKEN

    monkeypatch.setattr(sessions.store, "resolve", resolve)

    def authorize(request, tenant):
        if request.headers.get("authorization") != "Bearer " + TOKEN or tenant.subject != SUBJECT:
            sessions.deny(403)
        return TOKEN, SUBJECT, 9999999999

    monkeypatch.setattr(sessions, "authorize", authorize)
    monkeypatch.setattr(bridge, "load_integration_config", lambda: SimpleNamespace(api_origin=UPSTREAM))

    async def upstream(request):
        state["calls"].append(request)
        result = state["respond"](request)
        return await result if inspect.isawaitable(result) else result

    monkeypatch.setattr(bridge, "_client", lambda: httpx.AsyncClient(
        transport=httpx.MockTransport(upstream), trust_env=False, follow_redirects=False))
    app = FastAPI()
    app.include_router(bridge.router)
    app.dependency_overrides[deps.require_tenant] = lambda: SimpleNamespace(subject=SUBJECT)
    app.add_middleware(sessions.ServiceBrowserSessionMiddleware)

    def request(method="GET", path=SCOPE_PATH, headers=None, content=None):
        defaults = [("Authorization", "LeafServiceSession " + BINDING),
                    ("Cookie", sessions.SESSION_COOKIE + "=" + SECRET),
                    ("X-Leaf-Integration-Origin", UPSTREAM)]
        async def run():
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=LOCAL,
                                         trust_env=False) as client:
                return await client.request(method, path, headers=defaults if headers is None else headers,
                                            content=content)
        return asyncio.run(run())

    state["request"], state["headers"], state["app"] = request, lambda: [
        ("Authorization", "LeafServiceSession " + BINDING),
        ("Cookie", sessions.SESSION_COOKIE + "=" + SECRET),
        ("X-Leaf-Integration-Origin", UPSTREAM)], app
    return state


def error(context, response, status):
    assert response.status_code == status
    assert "no-store" in response.headers["cache-control"]
    assert TOKEN not in response.text and SECRET not in response.text
    assert "set-cookie" not in response.headers


def test_joint_session_translates_only_into_fixed_upstream(context):
    response = context["request"]()
    assert response.status_code == 200 and response.json() == SCOPE
    [sent] = context["calls"]
    assert str(sent.url) == UPSTREAM + SCOPE_PATH
    assert sent.headers["authorization"] == "Bearer " + TOKEN
    assert sent.headers["accept-encoding"] == "identity"
    for name in ("cookie", "x-tenant-id", "x-leaf-integration-origin", "x-forwarded-host"):
        assert name not in sent.headers
    assert "no-store" in response.headers["cache-control"]


@pytest.mark.parametrize("headers", [[], [("Cookie", sessions.SESSION_COOKIE + "=" + SECRET)],
    [("Authorization", "LeafServiceSession " + BINDING)],
    [("Authorization", "Bearer " + TOKEN), ("Cookie", sessions.SESSION_COOKIE + "=" + SECRET)]])
def test_public_selector_cookie_or_bearer_alone_cannot_use_bridge(context, headers):
    response = context["request"](headers=headers)
    assert response.status_code in {401, 403}
    assert not context["calls"]
    assert TOKEN not in response.text and SECRET not in response.text


def test_revoked_session_is_denied_before_upstream(context):
    context["revoked"] = True
    error(context, context["request"](), 401)
    assert not context["calls"]


@pytest.mark.parametrize("name,value", [("Origin", "https://foreign.example"),
    ("Host", "foreign.example"), ("X-Leaf-Integration-Origin", "https://foreign.example")])
def test_foreign_origin_host_or_destination_assertion_cannot_route(context, name, value):
    headers = [(k, v) for k, v in context["headers"]() if k.lower() != name.lower()] + [(name, value)]
    response = context["request"](headers=headers)
    assert response.status_code in {403, 503}
    assert not context["calls"]


@pytest.mark.parametrize("name", ["Authorization", "Cookie", "Origin", "Host", "X-Leaf-Integration-Origin"])
def test_ambiguous_headers_are_rejected(context, name):
    value = {"Authorization": "LeafServiceSession " + BINDING,
             "Cookie": sessions.SESSION_COOKIE + "=" + SECRET, "Origin": LOCAL,
             "Host": "platform-staging.leafdesign.ai", "X-Leaf-Integration-Origin": UPSTREAM}[name]
    headers = context["headers"]() + [(name, value), (name, value)]
    assert context["request"](headers=headers).status_code in {401, 403}
    assert not context["calls"]


def test_write_requires_origin_and_preserves_exact_body_and_revision(context):
    context["respond"] = lambda _: httpx.Response(200, json={"revision": 3})
    headers = context["headers"]() + [("Content-Type", "application/json"), ("If-Match", "2")]
    path = ELEMENT_PATH + "/fixture-view"
    error(context, context["request"]("PUT", path, headers, b'{"title":"fixture"}'), 403)
    assert not context["calls"]
    response = context["request"]("PUT", path, headers + [("Origin", LOCAL)], b'{"title":"fixture"}')
    assert response.status_code == 200
    [sent] = context["calls"]
    assert sent.content == b'{"title":"fixture"}' and sent.headers["if-match"] == "2"


@pytest.mark.parametrize("suffix", ["connections/fixture/vercel/attempt", "connections/fixture/credential", "manual-workflow-runs",
    "manual-workflow-schedule-drafts", "webhook-registrations", "actions", "datasets/fixture/versions/1/sync"])
def test_provider_workflow_and_sync_operations_are_not_reachable(context, suffix):
    path = f"/api/v1/projects/{PROJECT}/integrations/{suffix}"
    error(context, context["request"](path=path), 404)
    assert not context["calls"]


@pytest.mark.parametrize("suffix", ["connections", "connections?limit=2&after=fixture",
    "connections/fixture", "connections/fixture/vercel/preview-permission"])
def test_supported_connection_lookups_preserve_fixed_destination_and_bearer(context, suffix):
    payload = {"fixture": "read-only-result"}
    context["respond"] = lambda _: httpx.Response(200, json=payload)
    path = f"/api/v1/projects/{PROJECT}/integrations/{suffix}"
    response = context["request"](path=path)
    assert response.status_code == 200 and response.json() == payload
    [sent] = context["calls"]
    assert str(sent.url) == UPSTREAM + path
    assert sent.method == "GET" and sent.headers["authorization"] == "Bearer " + TOKEN
    assert "cookie" not in sent.headers and "x-leaf-integration-origin" not in sent.headers
    assert "no-store" in response.headers["cache-control"]


@pytest.mark.parametrize("method", ["POST", "PUT"])
@pytest.mark.parametrize("suffix", ["connections", "connections/fixture",
    "connections/fixture/vercel/preview-permission"])
def test_readonly_connection_admission_does_not_enable_mutations(context, method, suffix):
    path = f"/api/v1/projects/{PROJECT}/integrations/{suffix}"
    error(context, context["request"](method, path), 405)
    assert not context["calls"]


def test_permission_lookup_preserves_upstream_role_denial(context):
    context["respond"] = lambda _: httpx.Response(403, json={"error": {
        "code": "WORKSPACE_SCOPE_DENIED", "correlationId": OTHER}})
    path = f"/api/v1/projects/{PROJECT}/integrations/connections/fixture/vercel/preview-permission"
    response = context["request"](path=path)
    error(context, response, 403)
    assert response.json()["error"]["code"] == "WORKSPACE_SCOPE_DENIED"
    assert response.json()["error"]["correlationId"] == OTHER
    assert len(context["calls"]) == 1


@pytest.mark.parametrize("suffix,status", [
    ("connections?limit=101", 400), ("connections?limit=1&limit=2", 400),
    ("connections?target=https://foreign.example", 400),
    ("connections/fixture?revision=1", 400),
    ("connections/fixture/vercel/preview-permission?enabled=true", 400),
    ("connections/fixture/vercel/preview-permission/enable", 404),
    ("vercel-preview-runs", 404), ("vercel-preview-runs/fixture/observe", 404)])
def test_connection_readiness_does_not_expand_query_or_provider_routes(context, suffix, status):
    path = f"/api/v1/projects/{PROJECT}/integrations/{suffix}"
    error(context, context["request"](path=path), status)
    assert not context["calls"]


@pytest.mark.parametrize("path", [SCOPE_PATH + "?target=https://foreign.example",
    ELEMENT_PATH + "?limit=1&limit=2", ELEMENT_PATH + "?after=../escape",
    ELEMENT_PATH + "?limit=101", ELEMENT_PATH + "?origin=" + UPSTREAM,
    f"/api/v1/projects/{PROJECT}/integrations/datasets/fixture/versions/2147483648",
    f"/api/v1/projects/{PROJECT}/integrations/elements/%66ixture"])
def test_invalid_parameters_never_reach_upstream(context, path):
    error(context, context["request"](path=path), 400)
    assert not context["calls"]


def test_unavailable_deployment_binding_fails_closed(context, monkeypatch):
    def missing():
        raise IntegrationConfigError("INTEGRATION_CONFIG_SOURCE")
    monkeypatch.setattr(bridge, "load_integration_config", missing)
    error(context, context["request"](), 503)
    assert not context["calls"]


@pytest.mark.parametrize("body,status", [(b'{"a":1,"a":2}', 400), (b'{"a":NaN}', 400),
    (b'{"a":1e999}', 400), (b"not-json", 400), (b" " * 262145, 413)],
    ids=["duplicate-key", "nan", "overflow", "not-json", "oversized"])
def test_invalid_or_oversized_write_body_is_rejected(context, body, status):
    headers = context["headers"]() + [("Content-Type", "application/json"), ("Origin", LOCAL)]
    error(context, context["request"]("POST", ELEMENT_PATH, headers, body), status)
    assert not context["calls"]


def test_read_cannot_carry_a_body(context):
    error(context, context["request"](content=b"{}"), 413)
    assert not context["calls"]


@pytest.mark.parametrize("change", [{"projectId": OTHER}, {"externalSubject": "foreign-subject"},
    {"tenantId": "not-a-uuid"}, {"projectRole": "admin"}, {"extra": "field"}])
def test_scope_response_is_bound_to_project_and_subject(context, change):
    context["respond"] = lambda _: httpx.Response(200, json={**SCOPE, **change})
    error(context, context["request"](), 502)


def test_redirect_is_not_followed_and_upstream_cookies_never_escape(context):
    context["respond"] = lambda _: httpx.Response(307, headers={"location": "https://foreign.example",
                                                              "set-cookie": "private=value"})
    error(context, context["request"](), 502)
    assert len(context["calls"]) == 1
    context["respond"] = lambda _: httpx.Response(200, json=SCOPE, headers={"set-cookie": "private=value"})
    response = context["request"]()
    assert response.status_code == 200 and "set-cookie" not in response.headers


@pytest.mark.parametrize("response", [httpx.Response(200, content=b"not-json", headers={"content-type": "application/json"}),
    httpx.Response(200, content=b"x" * (bridge.RESPONSE_BYTES + 1), headers={"content-type": "application/json"}),
    httpx.Response(200, content=gzip.compress(json.dumps(SCOPE).encode()),
                   headers={"content-encoding": "gzip", "content-type": "application/json"})])
def test_invalid_oversized_or_encoded_upstream_is_refused(context, response):
    context["respond"] = lambda _: response
    error(context, context["request"](), 502)


def test_revision_conflict_preserves_safe_code_but_not_upstream_message(context):
    context["respond"] = lambda _: httpx.Response(409, json={"error": {
        "code": "INTEGRATION_CONFLICT", "message": TOKEN, "sql": SECRET, "correlationId": BINDING}})
    headers = context["headers"]() + [("Origin", LOCAL), ("Content-Type", "application/json"), ("If-Match", "2")]
    response = context["request"]("PUT", ELEMENT_PATH + "/fixture", headers, b"{}")
    error(context, response, 409)
    assert response.json()["error"]["code"] == "INTEGRATION_CONFLICT"
    assert response.json()["error"]["correlationId"] == BINDING
    assert response.headers["x-correlation-id"] == BINDING
    assert len(context["calls"]) == 1


def test_ambiguous_write_is_not_retried(context):
    def failed(request):
        raise httpx.ReadError("private transport detail", request=request)
    context["respond"] = failed
    headers = context["headers"]() + [("Origin", LOCAL), ("Content-Type", "application/json")]
    error(context, context["request"]("POST", ELEMENT_PATH, headers, b"{}"), 502)
    assert len(context["calls"]) == 1


def test_absolute_deadline_cancels_upstream(context, monkeypatch):
    monkeypatch.setattr(bridge, "DEADLINE_SECONDS", 0.02)
    async def slow(_):
        await asyncio.sleep(0.2)
        return httpx.Response(200, json=SCOPE)
    context["respond"] = slow
    error(context, context["request"](), 504)
    assert len(context["calls"]) == 1


class Chunks(httpx.AsyncByteStream):
    async def __aiter__(self):
        yield b" " * bridge.RESPONSE_BYTES
        yield b" "


def test_response_cap_counts_stream_without_content_length(context):
    context["respond"] = lambda _: httpx.Response(200, stream=Chunks(),
                                                 headers={"content-type": "application/json"})
    error(context, context["request"](), 502)
    assert len(context["calls"]) == 1


def test_request_cap_counts_chunked_stream_without_content_length(context):
    async def chunks():
        yield b" " * 262144
        yield b" "
    headers = context["headers"]() + [("Origin", LOCAL), ("Content-Type", "application/json")]
    error(context, context["request"]("POST", ELEMENT_PATH, headers, chunks()), 413)
    assert not context["calls"]


def test_absolute_deadline_also_covers_body_producer(context, monkeypatch):
    monkeypatch.setattr(bridge, "DEADLINE_SECONDS", 0.02)
    async def slow():
        await asyncio.sleep(0.2)
        yield b"{}"
    headers = context["headers"]() + [("Origin", LOCAL), ("Content-Type", "application/json")]
    error(context, context["request"]("POST", ELEMENT_PATH, headers, slow()), 504)
    assert not context["calls"]
