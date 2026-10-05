"""Project drawing HTTP contract through injected canonical services."""
from dataclasses import replace

from fastapi import FastAPI
from envelopes import install_error_handlers
from fastapi.testclient import TestClient
import pytest

import checkout_capability as caps
import deps
import platform_link
import solar_project_context as service
from routers import project_drawings
from test_sip_r1_context import memory, O1, O2, P1, P2, D1, D2, V2, A, DIGEST

CONTEXT = f"/api/projects/{P1}/drawing-versions/{V2}/context"
CHECKOUT = f"/api/projects/{P1}/drawings/{D1}/checkout"


@pytest.fixture
def http(memory):
    app = FastAPI()
    install_error_handlers(app)
    app.middleware("http")(project_drawings.no_store_responses)
    app.include_router(project_drawings.router)
    memory.tenant_calls = 0

    def tenant():
        memory.tenant_calls += 1
        return memory.tenant

    app.dependency_overrides[deps.require_active_tenant] = tenant
    with TestClient(app) as client:
        yield client


def reason(response, status, code):
    assert response.status_code == status, response.text
    assert response.json()["error"]["reason_code"] == "SIP_R1_" + code
    assert response.headers["cache-control"] == "no-store"


def token_headers(response):
    return {"X-Checkout-Capability": response.json()["checkout_capability"]}


def test_sip_r1_routes_mounted_once():
    from app import app
    from route_flatten import iter_leaf_routes
    expected = {
        ("GET", "/api/projects/{project_id}/drawing-versions/{input_version_id}/context"),
        ("GET", "/api/projects/{project_id}/drawings/{drawing_id}/checkout"),
        ("POST", "/api/projects/{project_id}/drawings/{drawing_id}/checkout"),
        ("DELETE", "/api/projects/{project_id}/drawings/{drawing_id}/checkout"),
    }
    counts = dict.fromkeys(expected, 0)
    for path, route in iter_leaf_routes(app.routes):
        for method in getattr(route, "methods", ()) or ():
            pair = method, path
            if pair in expected:
                counts[pair] += 1
                assert deps.require_active_tenant in [d.call for d in route.dependant.dependencies]
    assert set(counts.values()) == {1}


def test_sip_r1_context_http_shape(http, memory):
    response = http.get(CONTEXT)
    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    body = response.json()
    assert body["context"] == {"organization_id": str(O1), "project_id": str(P1),
        "drawing_id": str(D1), "input_version_id": str(V2), "head_version_id": str(V2),
        "is_head": True, "intake_sha256": DIGEST}
    assert body["intake"] == {} and body["error"] is None
    assert "intake_ref" not in response.text and "provenance" not in response.text
    assert "holder_binding_id" not in response.text


def test_sip_r1_status_without_checkout(http, memory):
    response = http.get(CHECKOUT)
    assert response.status_code == 200 and response.json()["checkout"] is None
    assert memory.last_fence == 0 and memory.mutations == []


def test_sip_r1_status_hides_capability(http, memory):
    acquired = http.post(CHECKOUT)
    response = http.get(CHECKOUT)
    assert response.status_code == 200
    checkout = response.json()["checkout"]
    assert set(checkout) == {"holder", "acquired", "expires", "fence"}
    assert checkout["fence"] == "1" and checkout["expires"].endswith("Z")
    assert "checkout_capability" not in response.json() and "binding" not in response.text
    assert acquired.json()["checkout_capability"] not in response.text
    memory.checkout = replace(memory.checkout, fence=2**60 + 1)
    assert http.get(CHECKOUT).json()["checkout"]["fence"] == str(2**60 + 1)


def test_sip_r1_acquire_defaults(http, memory):
    response = http.post(CHECKOUT)
    assert response.status_code == 200 and response.json()["acquired"] is True
    assert memory.mutations == [("Project editor", 3600)]
    assert response.json()["checkout"]["holder"] == "Project editor"
    assert response.headers["cache-control"] == "no-store"


def test_sip_r1_acquire_conflict(http, memory):
    http.post(CHECKOUT, json={"holder": "Editor A"})
    prior = memory.checkout
    response = http.post(CHECKOUT, json={"holder": "Editor A"})
    reason(response, 409, "CHECKOUT_CONFLICT")
    assert response.json()["acquired"] is False and response.json()["locked_by"] == "Editor A"
    assert response.json()["checkout"]["fence"] == "1"
    assert "checkout_capability" not in response.json() and memory.checkout == prior


def test_sip_r1_renew_rotates_capability(http, memory):
    first = http.post(CHECKOUT)
    renewed = http.post(CHECKOUT, headers=token_headers(first), json={"ttl_s": 120})
    assert renewed.status_code == 200 and renewed.json()["checkout"]["fence"] == "2"
    assert renewed.json()["checkout_capability"] != first.json()["checkout_capability"]
    reason(http.post(CHECKOUT, headers=token_headers(first)), 409, "CHECKOUT_CONFLICT")
    assert memory.last_fence == 2


def test_sip_r1_release_active(http, memory):
    grant = http.post(CHECKOUT)
    before = memory.checkout
    reason(http.delete(CHECKOUT, headers={"X-Checkout-Capability": "invalid"}), 403, "CHECKOUT_DENIED")
    assert memory.checkout == before
    response = http.delete(CHECKOUT, headers=token_headers(grant))
    assert response.status_code == 200 and response.json()["released"] is True
    assert response.json()["checkout"] is None and memory.last_fence == 1


def test_sip_r1_release_empty_or_expired(http, memory):
    empty = http.delete(CHECKOUT)
    assert empty.status_code == 200 and empty.json()["released"] is False
    http.post(CHECKOUT)
    memory.now = memory.checkout.expires_at
    expired = http.delete(CHECKOUT)
    assert expired.status_code == 200 and expired.json()["released"] is True
    assert empty.json()["checkout"] is expired.json()["checkout"] is None
    assert memory.last_fence == 1


def test_sip_r1_http_auth_matrix(http, memory, monkeypatch):
    for method, path, write in (("GET", CONTEXT, False), ("GET", CHECKOUT, False),
                                ("POST", CHECKOUT, True), ("DELETE", CHECKOUT, True)):
        memory.accesses.clear()
        before = memory.tenant_calls
        memory.role = "read_only"
        response = http.request(method, path)
        assert memory.tenant_calls == before + 1 and memory.accesses == [write]
        if write:
            reason(response, 403, "PROJECT_FORBIDDEN")
        else:
            assert response.status_code == 200
    def unbound(tenant):
        raise platform_link.ProjectSessionForbidden()
    monkeypatch.setattr(platform_link, "resolve_caller_binding", unbound)
    for method, path in (("GET", CONTEXT), ("GET", CHECKOUT), ("POST", CHECKOUT), ("DELETE", CHECKOUT)):
        reason(http.request(method, path, headers={"X-Org-Id": str(O1)}), 403, "PROJECT_FORBIDDEN")
    assert memory.mutations == []


def test_sip_r1_rejects_scope_overrides(http, memory):
    for method, path in (("GET", CONTEXT), ("GET", CHECKOUT), ("POST", CHECKOUT), ("DELETE", CHECKOUT)):
        for headers in ({"X-Org-Id": str(O2)}, {"X-Project-Id": str(P2)}):
            reason(http.request(method, path, headers=headers), 404, "CONTEXT_NOT_FOUND")
        reason(http.request(method, path.replace(str(P1), str(P2))), 404, "CONTEXT_NOT_FOUND")
    reason(http.get(CONTEXT, params={"drawing_id": str(D2)}), 404, "CONTEXT_NOT_FOUND")
    reason(http.post(CHECKOUT.replace(str(D1), str(D2))), 404, "CONTEXT_NOT_FOUND")
    assert memory.mutations == [] and memory.reads == []


def test_sip_r1_rejects_extra_fields_and_bad_ttl(http, memory):
    bodies = [{field: "forbidden"} for field in ("intake_ref", "object_key", "actor_binding_id", "org_id", "fence", "version")]
    bodies += [{"ttl_s": value} for value in (True, False, 0, -1, 86401, "60", None)]
    for body in bodies:
        response = http.post(CHECKOUT, json=body)
        assert response.status_code == 422
        assert response.headers["cache-control"] == "no-store"
    for text in ('{"ttl_s":NaN}', '{"ttl_s":Infinity}', '{"ttl_s":1e999}', '[]', '"text"'):
        assert http.post(CHECKOUT, content=text, headers={"Content-Type": "application/json"}).status_code == 422
    assert http.post(CHECKOUT.replace(str(D1), "invalid"), json={}).status_code == 422
    assert http.request("DELETE", CHECKOUT, json={"fence": 1}).status_code == 422
    assert memory.mutations == [] and memory.last_fence == 0


def test_sip_r1_drain_and_unavailable_mapping(http, memory, monkeypatch):
    memory.drain = "drained"
    reason(http.post(CHECKOUT), 503, "WRITES_DRAINED")
    reason(http.delete(CHECKOUT), 503, "WRITES_DRAINED")
    memory.drain = None
    def unavailable(*args, **kwargs):
        raise RuntimeError("private backend detail")
    with monkeypatch.context() as patch:
        patch.setattr(memory, "get_checkout", unavailable)
        response = http.get(CHECKOUT)
        reason(response, 503, "STORE_UNAVAILABLE")
        assert "private backend detail" not in response.text
    monkeypatch.setattr(caps, "ensure_mintable", unavailable)
    reason(http.post(CHECKOUT), 503, "CHECKOUT_UNAVAILABLE")
    assert memory.mutations == []


def test_sip_r1_mint_failure_rolls_back(http, memory, monkeypatch):
    grant = http.post(CHECKOUT)
    prior = memory.checkout, memory.last_fence, list(memory.mutations)
    original = caps.mint
    def fail_granted(tenant, scope, fence):
        if fence == 2:
            raise RuntimeError("mint failed")
        return original(tenant, scope, fence)
    monkeypatch.setattr(caps, "mint", fail_granted)
    reason(http.post(CHECKOUT, headers=token_headers(grant)), 503, "CHECKOUT_UNAVAILABLE")
    assert (memory.checkout, memory.last_fence, memory.mutations) == prior
    assert http.get(CHECKOUT).json()["checkout"]["fence"] == "1"


def _reject_checkout_proof(http, memory, *, method, nonascii):
    grant = http.post(CHECKOUT)
    assert grant.status_code == 200 and grant.json()["checkout"]["fence"] == "1"
    before = memory.checkout, memory.last_fence, list(memory.mutations)
    token = grant.json()["checkout_capability"]
    if nonascii:
        headers = [(b"X-Checkout-Capability", b"lco1." + b"a" * 63 + b"\xe9")]
    else:
        wrong = token[:-1] + ("0" if token[-1] != "0" else "1")
        assert service._well_formed_capability(wrong)
        headers = {"X-Checkout-Capability": wrong}
    response = http.request(method, CHECKOUT, headers=headers)
    if method == "POST":
        reason(response, 409, "CHECKOUT_CONFLICT")
        assert response.json()["acquired"] is False
    else:
        reason(response, 403, "CHECKOUT_DENIED")
    assert "checkout_capability" not in response.json()
    assert (memory.checkout, memory.last_fence, memory.mutations) == before


def test_sip_r1_renew_nonascii_proof(http, memory):
    _reject_checkout_proof(http, memory, method="POST", nonascii=True)


def test_sip_r1_release_nonascii_proof(http, memory):
    _reject_checkout_proof(http, memory, method="DELETE", nonascii=True)


def test_sip_r1_renew_wrong_ascii_proof(http, memory):
    _reject_checkout_proof(http, memory, method="POST", nonascii=False)


def test_sip_r1_release_wrong_ascii_proof(http, memory):
    _reject_checkout_proof(http, memory, method="DELETE", nonascii=False)
