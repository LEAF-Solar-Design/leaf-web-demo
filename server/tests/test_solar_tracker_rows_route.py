"""Tracker row HTTP parsing, real publication parity and checkout boundaries."""
import asyncio
from copy import deepcopy
import json
from pathlib import Path
import sys

import pytest

SERVER = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SERVER))
sys.path.insert(0, str(SERVER / "tests"))

import checkout_capability
import deps
import entitlements
import solar_artifacts
import solar_physical_head as ph
import solar_physical_state as ps
import solar_tracker_rows as domain
import write_loop
from envelopes import ErrorCode, err_envelope
from routers import drawings, solar_tracker_rows as route
from test_solar_tracker_rows import factory, RAW, TENANT, DRAWING, PROJECT, head
from test_w1_design_graph import graph  # noqa: F401

URL = "/api/drawings/" + DRAWING + "/tracker-rows"
CAP = domain.MAX_REQUEST_BYTES
JSON_HEADERS = [(b"content-type", b"application/json")]


def request_body(**changes):
    body = {"operation": "manual-create", "rows": deepcopy(RAW),
            "module_power_watts": 450, "expected_head": None}
    body.update(changes)
    return body


@pytest.fixture
def backend(factory):
    return factory()[0]


@pytest.fixture
def client(backend, monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from envelopes import install_error_handlers
    monkeypatch.delenv("LEAF_AUTH_LIVE", raising=False)
    monkeypatch.delenv("APS_LIVE", raising=False)
    monkeypatch.setattr(route, "_backend", lambda tenant: backend)
    app = FastAPI()
    install_error_handlers(app)
    app.include_router(route.router)
    app.include_router(drawings.router)
    return TestClient(app, raise_server_exceptions=False)


def post(client, body=None, *, content=None, media="application/json", query="", url=URL,
         tenant=TENANT, capability=None):
    headers = {"X-Tenant-Id": tenant}
    if media is not None:
        headers["Content-Type"] = media
    if capability is not None:
        headers["X-Checkout-Capability"] = capability
    return client.post(url + query, content=content if content is not None else json.dumps(
        request_body() if body is None else body), headers=headers)


def body_of(response):
    body = response.json()
    assert body["error"] is None and body["degraded_mode"] is False
    return {k: v for k, v in body.items() if k not in ("error", "degraded_mode")}


def refused(response, status, suffix, retryable=False):
    code = "TRACKER_ROWS_" + suffix
    expected = err_envelope(ErrorCode.INTERNAL if status >= 500 else ErrorCode.BAD_PARAMS,
                            code, retryable=retryable)
    expected["error"]["reason_code"] = code
    assert response.status_code == status
    assert response.json() == expected


def forbidden(*args, **kwargs):
    pytest.fail("refusal reached publication or a writer")


def snapshot(backend, tenant=TENANT, drawing=DRAWING):
    return {k: backend.get(k) for k in backend.drawing_object_keys(tenant, drawing)}


def asgi(client, chunks, headers=JSON_HEADERS, *, query="", url=URL):
    pending, pulls, messages = list(chunks), [], []

    async def receive():
        if not pending:
            return {"type": "http.request", "body": b"", "more_body": False}
        name, data = pending.pop(0)
        pulls.append(name)
        return {"type": "http.request", "body": data, "more_body": bool(pending)}

    async def send(message):
        messages.append(message)

    scope = {"type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1",
             "method": "POST", "scheme": "http", "path": url, "raw_path": url.encode(),
             "query_string": query.lstrip("?").encode(), "root_path": "",
             "headers": [(b"x-tenant-id", TENANT.encode())] + list(headers),
             "client": ("testclient", 50000), "server": ("testserver", 80)}
    asyncio.run(client.app(scope, receive, send))
    status = next(m["status"] for m in messages if m["type"] == "http.response.start")
    body = json.loads(b"".join(m.get("body", b"") for m in messages
                               if m["type"] == "http.response.body"))
    return status, body, pulls


def stream_refused(client, chunks, status, suffix, headers=JSON_HEADERS, **kwargs):
    actual, body, pulls = asgi(client, chunks, headers, **kwargs)
    assert (actual, body["error"]["reason_code"]) == (status, "TRACKER_ROWS_" + suffix)
    return pulls


def acquire(client):
    response = client.post("/api/drawings/" + DRAWING + "/checkout",
                           json={"holder": "sess-a"}, headers={"X-Tenant-Id": TENANT})
    assert response.status_code == 200
    return response.json()["checkout_capability"]


def gate_guard(backend, monkeypatch):
    before = snapshot(backend)
    monkeypatch.setattr(domain, "publish_manual_create", forbidden)
    monkeypatch.setattr(ph, "publish_physical_state", forbidden)
    monkeypatch.setattr(ps, "store_physical_state", forbidden)
    monkeypatch.setattr(solar_artifacts, "store_artifact", forbidden)
    return before


def test_publication_201_equals_domain(backend, factory, client):
    twin, _ = factory()
    expected = domain.publish_manual_create(twin, TENANT, DRAWING, rows=RAW,
                                            module_power_watts=450, expected_head=None)
    response = post(client)
    assert response.status_code == 201 and body_of(response) == expected


def test_retry_200_equals_domain(backend, client):
    first = body_of(post(client))
    expected = domain.publish_manual_create(backend, TENANT, DRAWING, rows=RAW,
                                            module_power_watts=450, expected_head=None)
    response = post(client)
    assert response.status_code == 200 and body_of(response) == expected
    assert expected["head"] == first["head"]


def test_canonical_retry_same_head(backend, client):
    first = body_of(post(client))
    body = request_body(module_power_watts=450.0)
    body["rows"][0]["axis_start"] = [-0.0, 0]
    response = post(client, content=" \n" + json.dumps(body, indent=2) + " \n")
    assert response.status_code == 200
    assert body_of(response)["head"] == first["head"]


def test_exact_received_bytes(client, monkeypatch):
    seen = []
    original = domain.publish_manual_create
    def record(*args, **kwargs):
        seen.append(kwargs["request_bytes"])
        return original(*args, **kwargs)
    monkeypatch.setattr(domain, "publish_manual_create", record)
    data = (" \n" + json.dumps(request_body()) + "   ").encode()
    assert post(client, content=data).status_code == 201
    assert seen == [len(data)]


def test_json_media_types(client):
    for media in ("Application/JSON; charset=utf-8", " application/json ; profile=x"):
        assert post(client, media=media).status_code in (200, 201)


def test_non_json_media_types(client, monkeypatch):
    monkeypatch.setattr(route, "_backend", forbidden)
    for media in (None, "text/plain", "application/problem+json"):
        headers = [] if media is None else [(b"content-type", media.encode())]
        assert stream_refused(client, [("unread", b"x")], 415,
                              "CONTENT_TYPE_UNSUPPORTED", headers) == []


def test_body_at_limit(client, monkeypatch):
    data = json.dumps(request_body()).encode()
    seen = []
    original = domain.publish_manual_create
    def record(*args, **kwargs):
        seen.append(kwargs["request_bytes"])
        return original(*args, **kwargs)
    monkeypatch.setattr(domain, "publish_manual_create", record)
    status, body, pulls = asgi(client, [("body", data + b" " * (CAP - len(data)))])
    assert status == 201 and body["created"] is True and body["error"] is None
    assert seen == [CAP] and pulls == ["body"]


def test_stream_over_limit_before_decode(client, monkeypatch):
    monkeypatch.setattr(domain, "publish_manual_create", forbidden)
    original = json.loads
    seen = []
    def decode(*args, **kwargs):
        if "object_pairs_hook" in kwargs:
            seen.append(True)
            forbidden()
        return original(*args, **kwargs)
    monkeypatch.setattr(route.json, "loads", decode)
    assert stream_refused(client, [("first", b"x" * CAP), ("overflow", b"x"),
                                   ("sentinel", b"x")], 413, "LIMIT_EXCEEDED") == ["first", "overflow"]
    assert seen == []


def test_declared_over_limit_reads_no_body(client, monkeypatch):
    monkeypatch.setattr(route, "_backend", forbidden)
    assert stream_refused(client, [("unread", b"x")], 413, "LIMIT_EXCEEDED",
                          JSON_HEADERS + [(b"content-length", b"262145")]) == []


def test_huge_and_zero_padded_lengths(client):
    assert stream_refused(client, [("unread", b"x")], 413, "LIMIT_EXCEEDED",
                          JSON_HEADERS + [(b"content-length", b"9" * 10000)]) == []
    data = json.dumps(request_body()).encode()
    status, body, pulls = asgi(client, [("body", data)], JSON_HEADERS + [
        (b"content-length", b"0" * 10000 + str(len(data)).encode())])
    assert status == 201 and body["error"] is None and pulls == ["body"]


def test_missing_malformed_and_short_lengths_bounded(client, monkeypatch):
    monkeypatch.setattr(domain, "publish_manual_create", forbidden)
    for length in (None, b"garbage", b"1"):
        headers = JSON_HEADERS + ([] if length is None else [(b"content-length", length)])
        assert stream_refused(client, [("overflow", b"x" * (CAP + 1))], 413,
                              "LIMIT_EXCEEDED", headers) == ["overflow"]


def test_invalid_json_and_encoding(backend, client, monkeypatch):
    monkeypatch.setattr(route, "_backend", forbidden)
    for data in (b"", b"{", b"\xff", b"[" * 2000 + b"]" * 2000,
                 b'{"operation":NaN}', b'{"operation":Infinity}', b'{"operation":-Infinity}'):
        refused(post(client, content=data), 400, "REQUEST_INVALID")
    before = gate_guard(backend, monkeypatch)
    before_head = deepcopy(head(backend))
    monkeypatch.setattr(route, "_lock_authorization", forbidden)
    data = json.dumps(request_body())
    probes = [data.replace('"module_power_watts": 450', '"module_power_watts": ' + token)
              for token in ("NaN", "Infinity", "-Infinity")]
    body = request_body()
    body["rows"][0]["axis_end"] = [0, float("inf")]
    probes.append(json.dumps(body))
    # Only strict UTF-8 is read: a lone surrogate (ED A0 80) in an otherwise valid body, the
    # same body in UTF-16 or UTF-32 (json.loads on bytes would detect and accept them), and a
    # UTF-8 byte order mark are all refused while decoding, before authorization.
    encoded = data.encode()
    surrogate = encoded.replace(b'"expected_head": null',
                                b'"expected_head": "' + bytes.fromhex("eda080") + b'"')
    assert bytes.fromhex("eda080") in surrogate
    probes.append(surrogate)
    probes += [data.encode(codec) for codec in ("utf-16", "utf-16-le", "utf-16-be", "utf-32",
                                                "utf-32-le", "utf-32-be")]
    probes.append(b"\xef\xbb\xbf" + encoded)
    for raw in probes:
        refused(post(client, content=raw), 400, "REQUEST_INVALID")
        assert snapshot(backend) == before
        assert head(backend) == before_head


def test_duplicate_keys_at_all_depths(client, monkeypatch):
    monkeypatch.setattr(route, "_backend", forbidden)
    data = json.dumps(request_body())
    for raw in (data.replace('"operation":', '"operation":"manual-create","operation":'),
                data.replace('"slots": 3', '"slots":3,"slots":3')):
        refused(post(client, content=raw), 400, "REQUEST_INVALID")


def test_nonobject_body(client, monkeypatch):
    monkeypatch.setattr(route, "_backend", forbidden)
    for value in ([], 1, "text", None, True):
        refused(post(client, content=json.dumps(value)), 400, "REQUEST_INVALID")


def test_missing_and_extra_keys(client, monkeypatch):
    monkeypatch.setattr(route, "_backend", forbidden)
    for key in request_body():
        body = request_body()
        del body[key]
        refused(post(client, body), 400, "REQUEST_INVALID")
    for key in ("project_id", "units", "capability", "summary"):
        refused(post(client, request_body(**{key: "secret"})), 400, "REQUEST_INVALID")


def test_unsupported_operation(client, monkeypatch):
    monkeypatch.setattr(route, "_backend", forbidden)
    for value in ("mesh", None, 7, [], {}, True):
        refused(post(client, request_body(operation=value)), 400, "OPERATION_UNSUPPORTED")


def test_invalid_drawing_id_no_body(client):
    for drawing in ("Bad Id", "a" * 64):
        assert stream_refused(client, [("unread", b"x")], 400, "REQUEST_INVALID",
                              url="/api/drawings/" + drawing + "/tracker-rows") == []


def test_invalid_project_id_no_body(client):
    for project in ("", "p" * 101):
        assert stream_refused(client, [("unread", b"x")], 400, "PROJECT_ID_INVALID",
                              query="?project_id=" + project) == []


def test_resolved_and_matching_project(client):
    assert body_of(post(client))["project_id"] == PROJECT
    response = post(client, query="?project_id=" + PROJECT)
    assert response.status_code == 200 and body_of(response)["project_id"] == PROJECT


def test_project_mismatch(backend, client):
    before = snapshot(backend)
    refused(post(client, query="?project_id=other"), 409, "PROJECT_MISMATCH")
    assert snapshot(backend) == before


def test_active_checkout_missing_capability(backend, client, monkeypatch):
    acquire(client)
    before = gate_guard(backend, monkeypatch)
    for proof in (None, "", "   ", "garbage"):
        refused(post(client, capability=proof), 403, "CHECKOUT_REQUIRED")
        assert snapshot(backend) == before


def test_active_checkout_wrong_capability(backend, client, monkeypatch):
    import store
    acquire(client)
    co = store.load_manifest(backend, TENANT, DRAWING)["checkout"]
    proofs = [checkout_capability.mint(deps.TenantContext(TENANT, subject="other"), DRAWING, co["fence"]),
              checkout_capability.mint(TENANT, "other", co["fence"]),
              checkout_capability.mint(TENANT, DRAWING, co["fence"] + 1)]
    before = gate_guard(backend, monkeypatch)
    for proof in proofs:
        refused(post(client, capability=proof), 403, "CHECKOUT_REQUIRED")
        assert snapshot(backend) == before


def test_active_checkout_valid_capability(client, monkeypatch):
    proof = acquire(client)
    original = route._lock_authorization
    seen = []
    def record(*args):
        result = original(*args)
        seen.append(result)
        return result
    monkeypatch.setattr(route, "_lock_authorization", record)
    assert post(client, capability=proof).status_code == 201
    assert seen == [("sess-a", 1)]


def test_no_checkout_allowed(client, monkeypatch):
    original = route._lock_authorization
    seen = []
    def record(*args):
        result = original(*args)
        seen.append(result)
        return result
    monkeypatch.setattr(route, "_lock_authorization", record)
    for proof in (None, "", "   ", "garbage"):
        assert post(client, capability=proof).status_code in (200, 201)
    assert seen == [(None, None)] * 4


def test_expired_checkout_allowed(backend, client):
    import store
    acquire(client)
    manifest = store.load_manifest(backend, TENANT, DRAWING)
    manifest["checkout"]["expires"] = "2000-01-01T00:00:00+00:00"
    store.save_manifest(backend, TENANT, DRAWING, manifest)
    assert post(client).status_code == 201


def test_checkout_unavailable(backend, client, monkeypatch):
    acquire(client)
    before = gate_guard(backend, monkeypatch)
    def fail(*args, **kwargs):
        raise checkout_capability.CapabilityUnavailable("sensitive")
    monkeypatch.setattr(checkout_capability, "verify", fail)
    refused(post(client), 503, "CHECKOUT_UNAVAILABLE", True)
    assert snapshot(backend) == before


def test_missing_drawing_preserves_404(backend, client, monkeypatch):
    before = gate_guard(backend, monkeypatch)
    for proof in (None, "garbage"):
        refused(post(client, url="/api/drawings/nosuch/tracker-rows", capability=proof),
                404, "DRAWING_NOT_FOUND")
        assert snapshot(backend) == before
        assert snapshot(backend, drawing="nosuch") == {}


def test_gate_before_any_write(backend, client, monkeypatch):
    proof = acquire(client)
    assert post(client, capability=proof).status_code == 201
    before = gate_guard(backend, monkeypatch)
    # Even an exact retry must present proof while the checkout is active.
    refused(post(client), 403, "CHECKOUT_REQUIRED")
    assert snapshot(backend) == before


def test_gate_receives_tenant_context(client, monkeypatch):
    tenant = deps.TenantContext(TENANT, tier="demo", subject="auth0|holder")
    client.app.dependency_overrides[deps.require_active_tenant] = lambda: tenant
    seen = []
    def record(drawing, context, backend, capability):
        seen.append(context)
    monkeypatch.setattr(route, "_lock_authorization", record)
    assert post(client).status_code == 201
    assert seen == [tenant] and seen[0] is tenant and seen[0].subject == "auth0|holder"


def test_foreign_tenant_not_found(backend, client, monkeypatch):
    before = gate_guard(backend, monkeypatch)
    refused(post(client, tenant="other-tenant"), 404, "DRAWING_NOT_FOUND")
    assert snapshot(backend) == before
    assert snapshot(backend, tenant="other-tenant") == {}


def test_stale_head(backend, client):
    assert post(client).status_code == 201
    before = snapshot(backend)
    refused(post(client, request_body(module_power_watts=451)), 409, "STALE_HEAD", True)
    assert snapshot(backend) == before


def test_already_exists(backend, client, factory, monkeypatch):
    assert post(client).status_code == 201
    digest = head(backend)["state"]["artifact_id"]
    refused(post(client, request_body(expected_head=digest, module_power_watts=451)),
            409, "ALREADY_EXISTS")
    empty, _ = factory(state={"tracker_rows": []})
    monkeypatch.setattr(route, "_backend", lambda tenant: empty)
    refused(post(client, request_body(expected_head=head(empty)["state"]["artifact_id"])),
            409, "ALREADY_EXISTS")


def test_write_drain_reads_no_body(client, monkeypatch):
    monkeypatch.setattr(write_loop, "drawing_mutations_refusal", lambda: "drained")
    monkeypatch.setattr(route, "_backend", forbidden)
    assert stream_refused(client, [("unread", b"x")], 503, "WRITES_DRAINED", []) == []


def test_drain_after_route_preflight(backend, client, monkeypatch):
    calls = []
    def draining():
        calls.append(True)
        return None if len(calls) == 1 else "drained"
    monkeypatch.setattr(write_loop, "drawing_mutations_refusal", draining)
    before = snapshot(backend)
    refused(post(client), 503, "WRITES_DRAINED", True)
    assert len(calls) == 2 and snapshot(backend) == before


def test_backend_and_lookup_unavailable(client, monkeypatch):
    monkeypatch.setattr(domain, "publish_manual_create", forbidden)
    original = route._backend
    for location, errors in (("_backend", (RuntimeError, OSError)),
                             ("_lock_authorization", (ValueError, OSError))):
        monkeypatch.setattr(route, "_backend", original)
        for error in errors:
            def fail(*args, **kwargs):
                raise error("sensitive")
            monkeypatch.setattr(route, location, fail)
            refused(post(client), 503, "STORE_UNAVAILABLE", True)


def test_entitlement_denied_and_unavailable(client, monkeypatch):
    monkeypatch.setattr(route, "_backend", forbidden)
    monkeypatch.setattr(entitlements, "resolve_tier", lambda tenant: "restricted")
    response = post(client)
    assert response.status_code == 403 and response.json()["required"] == "run_write"
    def fail(*args):
        raise entitlements.EntitlementsError("sensitive")
    monkeypatch.setattr(entitlements, "entitlements_for", fail)
    status, body, pulls = asgi(client, [("unread", b"x")])
    assert status == 503 and pulls == []


def test_all_domain_errors_status_and_envelope(client, monkeypatch):
    # Exercise domain validation through HTTP before injecting the complete vocabulary.
    cases = [({"rows": []}, 400, "ROWS_INVALID"),
             ({"module_power_watts": 0}, 400, "POWER_INVALID"),
             ({"expected_head": "A" * 64}, 400, "HEAD_INVALID"),
             ({"rows": [dict(RAW[0], axis_end=[0, 0])]}, 400, "AXIS_INVALID"),
             ({"rows": deepcopy(RAW) * 129}, 413, "LIMIT_EXCEEDED"),
             ({"rows": [dict(RAW[0], slots=10000)] * 11}, 413, "LIMIT_EXCEEDED")]
    cases += [({"rows": [dict(RAW[0], cross_axis_width_du=value)]}, 400, "WIDTH_INVALID")
              for value in (0, -1)]
    cases += [({"rows": [dict(RAW[0], slots=value)]}, 400, "SLOTS_INVALID")
              for value in (0, 1.5, True, 10001)]
    for changes, status, suffix in cases:
        refused(post(client, request_body(**changes)), status, suffix)
    for code in domain.CODES:
        def fail(*args, **kwargs):
            raise domain.TrackerRowsError(code)
        monkeypatch.setattr(domain, "publish_manual_create", fail)
        status, _, retryable = route.TRACKER_ROWS_ROUTE_REFUSALS[code]
        refused(post(client), status, code.removeprefix("TRACKER_ROWS_"), retryable)


def test_unknown_and_unexpected_errors_sanitized(client, monkeypatch):
    for error in (domain.TrackerRowsError("secret-capability"), RuntimeError("secret-capability")):
        def fail(*args, **kwargs):
            raise error
        monkeypatch.setattr(domain, "publish_manual_create", fail)
        response = post(client)
        refused(response, 500, "STATE_INVALID")
        assert "secret-capability" not in response.text


def test_refusal_map_closed_and_frozen():
    table = route.TRACKER_ROWS_ROUTE_REFUSALS
    assert set(table) == domain.CODES and len(table) == 30
    groups = {
        400: "OPERATION_UNSUPPORTED REQUEST_INVALID ROWS_INVALID ROW_INVALID POWER_INVALID HEAD_INVALID SLOTS_INVALID AXIS_INVALID WIDTH_INVALID PROJECT_ID_INVALID",
        409: "UNITS_UNSUPPORTED UNITS_MISMATCH FRAME_UNSUPPORTED PROJECT_MISMATCH GRAPH_REQUIRED GROUND_REQUIRED GRAPH_CONVERTED DEPENDENT_STATE ALREADY_EXISTS STALE_HEAD LOG_FULL",
        413: "LIMIT_EXCEEDED", 404: "DRAWING_NOT_FOUND", 403: "CHECKOUT_REQUIRED",
        415: "CONTENT_TYPE_UNSUPPORTED", 500: "STATE_INVALID STORE_UNSAFE",
        503: "WRITES_DRAINED STORE_UNAVAILABLE CHECKOUT_UNAVAILABLE"}
    retryable = {"STALE_HEAD", "WRITES_DRAINED", "STORE_UNAVAILABLE", "CHECKOUT_UNAVAILABLE"}
    expected = {"TRACKER_ROWS_" + suffix: (status, ErrorCode.INTERNAL if status >= 500
                                         else ErrorCode.BAD_PARAMS, suffix in retryable)
                for status, suffixes in groups.items() for suffix in suffixes.split()}
    assert dict(table) == expected
    with pytest.raises(TypeError):
        table["TRACKER_ROWS_STATE_INVALID"] = (400, ErrorCode.BAD_PARAMS, False)


def test_app_mount_and_openapi(monkeypatch):
    monkeypatch.delenv("LEAF_AUTH_LIVE", raising=False)
    from app import app
    from route_flatten import iter_leaf_routes
    path = "/api/drawings/{drawing_id}/tracker-rows"
    matches = [r for mounted_path, r in iter_leaf_routes(app.routes) if mounted_path == path
               and "POST" in getattr(r, "methods", set())]
    assert len(matches) == 1
    assert "post" in app.openapi()["paths"][path]
