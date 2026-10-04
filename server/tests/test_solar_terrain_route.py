"""Terrain HTTP contract over real imported physical heads and twin stores."""
import asyncio
import json
import sys
import time
from pathlib import Path

import pytest

SERVER = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SERVER))
sys.path.insert(0, str(SERVER / "tests"))

import entitlements
import guest_uploads
import solar_ground_terrain_adapter as adapter
import solar_physical_head as ph
import solar_physical_state as ps
import write_loop
from routers import drawings, solar_terrain as route
from test_solar_landxml_route import backend, body_of, keys, reason  # noqa: F401
from test_solar_ground_terrain_adapter import (
    g1_state, head_document, imp, publish, representable_grid, steep_head, TINYTEST)  # noqa: F401
from test_solar_physical_state import DRAWING, PROJECT, TENANT
from test_w1_design_graph import graph  # noqa: F401
from test_w1_solve_commit import seed

URL = "/api/drawings/" + DRAWING + "/terrain"
ROUTE_CODES = {"TERRAIN_DRAWING_ID_INVALID", "TERRAIN_OPERATION_INVALID", "TERRAIN_BODY_INVALID",
               "TERRAIN_BODY_TOO_LARGE", "TERRAIN_MEDIA_TYPE_REFUSED", "TERRAIN_OPERATION_FAILED",
               "TERRAIN_CHECKOUT_DENIED", "TERRAIN_CHECKOUT_UNAVAILABLE"}


@pytest.fixture
def client(backend, monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from envelopes import install_error_handlers
    monkeypatch.delenv("LEAF_AUTH_LIVE", raising=False)
    monkeypatch.delenv("APS_LIVE", raising=False)
    app = FastAPI()
    install_error_handlers(app)
    app.include_router(route.router)
    return TestClient(app, raise_server_exceptions=False)


@pytest.fixture
def twin(graph, tmp_path, monkeypatch):
    path = tmp_path / "twin"
    path.mkdir()
    return seed(path, monkeypatch, graph)[0]


def current(backend):
    return ph.physical_head(backend, TENANT, DRAWING, project_id=PROJECT)["state"]["artifact_id"]


def post(client, body=None, *, content=None, media="application/json", tenant=TENANT, url=URL,
         query=""):
    headers = {"X-Tenant-Id": tenant}
    if media is not None:
        headers["Content-Type"] = media
    if content is None:
        content = json.dumps(body if body is not None else {
            "operation": "mesh", "expected_head": "0" * 64})
    return client.post(url + "/operations" + query, content=content, headers=headers)


def get(client, url=URL, query="", tenant=TENANT):
    return client.get(url + query, headers={"X-Tenant-Id": tenant})


def assert_refused(response, status, code, retryable=False):
    assert (response.status_code, reason(response), response.json()["error"]["retryable"]) == (
        status, code, retryable)


def forbidden(*args, **kwargs):
    pytest.fail("cheap refusal reached the adapter or backend")


def asgi(client, chunks, headers=(), query="", url=URL):
    pulls, messages = [], []
    pending = list(chunks)

    async def receive():
        if not pending:
            return {"type": "http.request", "body": b"", "more_body": False}
        name, chunk = pending.pop(0)
        pulls.append(name)
        return {"type": "http.request", "body": chunk, "more_body": bool(pending)}

    async def send(message):
        messages.append(message)

    path = url + "/operations"
    scope = {"type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1", "method": "POST",
             "scheme": "http", "path": path, "raw_path": path.encode(),
             "query_string": query.lstrip("?").encode(), "root_path": "",
             "headers": [(b"x-tenant-id", TENANT.encode())] + list(headers),
             "client": ("testclient", 50000), "server": ("testserver", 80)}
    asyncio.run(client.app(scope, receive, send))
    status = next(m for m in messages if m["type"] == "http.response.start")["status"]
    body = json.loads(b"".join(m.get("body", b"") for m in messages
                               if m["type"] == "http.response.body"))
    return status, body, pulls


def test_terrain_route_mesh_equals_the_adapter(backend, twin, client):
    imported = imp(backend)
    assert imp(twin) == imported
    expected_head = current(backend)
    expected = adapter.render_mesh(twin, TENANT, DRAWING, project_id=PROJECT,
                                   expected_head=expected_head)
    response = post(client, {"operation": "mesh", "expected_head": expected_head},
                    query="?project_id=" + PROJECT, media="Application/JSON; charset=utf-8")
    assert response.status_code == 200
    assert body_of(response) == expected


def test_terrain_route_slope_and_clear_equal_the_adapter(backend, twin, client, g1_state):
    for store in (backend, twin):
        steep_head(store, g1_state)
    for operation, call in (("slope", adapter.check_tracker_slope),
                            ("slope-clear", adapter.clear_tracker_slope)):
        expected_head = current(backend)
        body = {"operation": operation, "expected_head": expected_head}
        kwargs = {}
        if operation == "slope":
            body["limits"] = TINYTEST
            kwargs["limits"] = TINYTEST
        expected = call(twin, TENANT, DRAWING, expected_head=expected_head, **kwargs)
        response = post(client, body)
        assert response.status_code == 200
        assert body_of(response) == expected
        assert current(backend) == expected["head"]["state"]["artifact_id"]


def test_terrain_route_expected_head_invalid(backend, client, monkeypatch):
    imp(backend)
    before = keys(backend)
    monkeypatch.setattr(adapter, "render_mesh", forbidden)
    bodies = [{"operation": "mesh"}] + [
        {"operation": "mesh", "expected_head": value}
        for value in (None, "a" * 63, "A" * 64, 7)]
    for body in bodies:
        assert_refused(post(client, body), 400, "TERRAIN_EXPECTED_HEAD_INVALID")
        assert keys(backend) == before


def test_terrain_route_head_moved_is_retryable(backend, client):
    imp(backend)
    body = {"operation": "mesh", "expected_head": current(backend)}
    assert post(client, body).status_code == 200
    before = keys(backend)
    assert_refused(post(client, body), 409, "TERRAIN_HEAD_MOVED", True)
    assert keys(backend) == before


def test_terrain_route_repeat_writes_nothing(backend, client):
    imp(backend)
    first = post(client, {"operation": "mesh", "expected_head": current(backend)})
    assert first.status_code == 200
    before = keys(backend)
    response = post(client, {"operation": "mesh", "expected_head": body_of(first)["head"]["state"]["artifact_id"]})
    assert response.status_code == 200 and body_of(response)["created"] is False
    assert keys(backend) == before


def test_terrain_route_operation_invalid(client, monkeypatch):
    monkeypatch.setattr(route, "_backend", forbidden)
    bodies = [{"expected_head": "a" * 64}] + [
        {"operation": value, "expected_head": "a" * 64} for value in ("piles", 7, "MESH")]
    for body in bodies:
        assert_refused(post(client, body), 400, "TERRAIN_OPERATION_INVALID")


def test_terrain_route_body_invalid(client, monkeypatch):
    monkeypatch.setattr(route, "_backend", forbidden)
    for content in (json.dumps({"operation": "mesh", "expected_head": "a" * 64, "limits": {}}),
                    json.dumps({"operation": "mesh", "expected_head": "a" * 64, "extra": 1}),
                    "[]", '{"operation":"mesh","operation":"slope"}', "not JSON",
                    '{"operation":"slope","limits":{"Columns":1,"Columns":2}}'):
        assert_refused(post(client, content=content), 400, "TERRAIN_BODY_INVALID")


def test_terrain_route_body_too_large(client, monkeypatch):
    monkeypatch.setattr(adapter, "render_mesh", forbidden)
    monkeypatch.setattr(route, "_backend", forbidden)
    assert_refused(post(client, content="x" * 8193), 413, "TERRAIN_BODY_TOO_LARGE")
    for length in (b"8193", b"00000000000008193", b"0001000000000000"):
        status, body, pulls = asgi(client, [("unread", b"x" * 8193)],
                                   [(b"content-type", b"application/json"), (b"content-length", length)])
        assert (status, body["error"]["reason_code"], pulls) == (
            413, "TERRAIN_BODY_TOO_LARGE", [] if length == b"0001000000000000" else ["unread"])
    status, body, pulls = asgi(client, [("first", b"x" * 8192), ("overflow", b"x"), ("unread", b"x")],
                               [(b"content-type", b"application/json")])
    assert (status, body["error"]["reason_code"], pulls) == (
        413, "TERRAIN_BODY_TOO_LARGE", ["first", "overflow", "unread"])


def test_terrain_route_media_type_refused(client, monkeypatch):
    monkeypatch.setattr(route, "_backend", forbidden)
    for media in ("text/plain", None):
        assert_refused(post(client, media=media), 415, "TERRAIN_MEDIA_TYPE_REFUSED")
    status, body, pulls = asgi(client, [("unread", b"x" * 8193)])
    assert (status, body["error"]["reason_code"], pulls) == (415, "TERRAIN_MEDIA_TYPE_REFUSED", [])


def test_terrain_route_tenancy(backend, client, monkeypatch):
    imp(backend)
    before = keys(backend)
    # Context maps an invisible drawing's GRAPH_CONTEXT_UNAVAILABLE to this code.
    with pytest.raises(adapter.TerrainAdapterError) as exc:
        adapter._context(backend, "other-tenant", DRAWING, None)
    assert exc.value.code == "TERRAIN_DRAWING_NOT_FOUND"
    assert_refused(post(client, tenant="other-tenant"), 404, "TERRAIN_DRAWING_NOT_FOUND")
    assert_refused(get(client, tenant="other-tenant"), 404, "TERRAIN_DRAWING_NOT_FOUND")
    assert keys(backend) == before
    assert not set(backend.drawing_object_keys("other-tenant", DRAWING))
    monkeypatch.setenv("LEAF_AUTH_LIVE", "1")
    monkeypatch.setenv("LEAF_GUEST_SECRET", "test-secret-not-a-real-one")
    token = guest_uploads.mint_guest_session(guest_uploads.mint_guest_tenant_id(), int(time.time()) + 3600)
    assert token
    monkeypatch.setattr(route, "_backend", forbidden)
    monkeypatch.setattr(write_loop, "backend_for_tenant", forbidden)
    for method, url in ((client.post, URL + "/operations"), (client.get, URL)):
        response = method(url, headers={"X-Guest-Session": token})
        assert response.status_code == 403 and "upload-only" in response.text


def test_terrain_route_drained_reads_no_body(client, monkeypatch):
    monkeypatch.setattr(write_loop, "drawing_mutations_refusal", lambda: "drained")
    monkeypatch.setattr(route, "_backend", forbidden)
    status, body, pulls = asgi(client, [("unread", b"x" * 8193)])
    assert (status, body["error"]["reason_code"], body["error"]["retryable"], pulls) == (
        503, "TERRAIN_WRITES_DRAINED", True, [])


def test_terrain_route_collapsed_grid_refused(backend, client):
    imp(backend)
    publish(backend, dict(head_document(backend)["state"], grid=representable_grid()))
    before = keys(backend)
    assert_refused(post(client, {"operation": "mesh", "expected_head": current(backend)}),
                   422, "TERRAIN_GRID_INVALID")
    assert_refused(get(client), 422, "TERRAIN_GRID_INVALID")
    assert keys(backend) == before


def test_terrain_route_unlisted_code_is_operation_failed(client, monkeypatch):
    def fail(*args, **kwargs):
        raise adapter.TerrainAdapterError("SOMETHING_NEW")
    monkeypatch.setattr(adapter, "render_mesh", fail)
    assert_refused(post(client), 500, "TERRAIN_OPERATION_FAILED")


def test_terrain_route_refusal_map_is_closed():
    import solar_frames_piles as fp
    import solar_civil_operations as civil
    passthrough = {key for key in drawings.LANDXML_IMPORT_REFUSALS if key.startswith("PHYSICAL_")}
    assert set(route.TERRAIN_ROUTE_REFUSALS) == adapter.CODES | passthrough | ROUTE_CODES
    assert set(route.CIVIL_ROUTE_REFUSALS) == fp.CODES | civil.CODES
    assert set(route.TERRAIN_ROUTE_REFUSALS).isdisjoint(route.CIVIL_ROUTE_REFUSALS)
    for key in passthrough:
        assert route.TERRAIN_ROUTE_REFUSALS[key] == drawings.LANDXML_IMPORT_REFUSALS[key]
    assert route.MAX_TERRAIN_BODY_BYTES == 8192


def test_terrain_route_refusal_statuses_pinned():
    from envelopes import ErrorCode
    import solar_frames_piles as fp
    import solar_civil_operations as civil
    groups = {
        400: {"TERRAIN_PROJECT_ID_INVALID", "TERRAIN_EXPECTED_HEAD_INVALID", "TERRAIN_LIMITS_INVALID",
              "TERRAIN_DRAWING_ID_INVALID", "TERRAIN_OPERATION_INVALID", "TERRAIN_BODY_INVALID"},
        404: {"TERRAIN_DRAWING_NOT_FOUND", "TERRAIN_STATE_NOT_FOUND"},
        403: {"TERRAIN_CHECKOUT_DENIED"},
        409: {"TERRAIN_GRAPH_REQUIRED", "TERRAIN_PROJECT_MISMATCH", "TERRAIN_FRAME_UNSUPPORTED",
              "TERRAIN_GRID_MISSING", "TERRAIN_NO_TRACKER_ROWS", "TERRAIN_HEAD_MOVED"},
        413: {"TERRAIN_BODY_TOO_LARGE"}, 415: {"TERRAIN_MEDIA_TYPE_REFUSED"},
        422: {"TERRAIN_GRID_INVALID", "TERRAIN_GRID_TOO_LARGE", "TERRAIN_FRAMES_INVALID", "TERRAIN_TOO_MANY_ROWS"},
        500: {"TERRAIN_OPERATION_FAILED"},
        503: {"TERRAIN_WRITES_DRAINED", "TERRAIN_STORE_UNAVAILABLE", "TERRAIN_CHECKOUT_UNAVAILABLE"},
    }
    retryable = {"TERRAIN_HEAD_MOVED", "TERRAIN_WRITES_DRAINED", "TERRAIN_STORE_UNAVAILABLE",
                 "TERRAIN_CHECKOUT_UNAVAILABLE"}
    additions = {
        400: {"FRAMES_PILES_PROJECT_ID_INVALID", "FRAMES_PILES_BASE_INVALID",
              "FRAMES_PILES_DRAWING_UNITS_INVALID", "FRAMES_PILES_BOUNDARY_INVALID",
              "FRAMES_PILES_PRESET_INVALID", "FRAMES_PILES_PILE_TEMPLATE_INVALID", "CIVIL_GRADE_INPUT_INVALID"},
        404: {"FRAMES_PILES_DRAWING_NOT_FOUND"},
        409: {"FRAMES_PILES_GRAPH_REQUIRED", "FRAMES_PILES_PROJECT_MISMATCH", "FRAMES_PILES_STATE_REQUIRED",
              "FRAMES_PILES_UNITS_MISMATCH", "FRAMES_PILES_TERRAIN_REQUIRED",
              "FRAMES_PILES_SITE_CONSTRAINTS_UNSUPPORTED", "FRAMES_PILES_PVCASE_UNSUPPORTED",
              "FRAMES_PILES_NO_FRAMES_FIT", "FRAMES_PILES_NO_FRAMES", "FRAMES_PILES_NO_PILES",
              "FRAMES_PILES_STALE_BASE"},
        422: {"FRAMES_PILES_TERRAIN_INVALID", "FRAMES_PILES_OFF_TERRAIN", "FRAMES_PILES_RANGE_INVALID",
              "FRAMES_PILES_STATE_INVALID", "FRAMES_PILES_LIMIT_EXCEEDED",
              "CIVIL_GRADE_LIMIT_EXCEEDED", "CIVIL_GRADE_NO_PAD"},
        500: {"CIVIL_GRADE_FAILED"},
        503: {"FRAMES_PILES_WRITES_DRAINED", "FRAMES_PILES_STORE_UNAVAILABLE"},
    }
    for status, codes in additions.items():
        groups[status].update(codes)
    retryable.update({"FRAMES_PILES_STALE_BASE", "FRAMES_PILES_WRITES_DRAINED", "FRAMES_PILES_STORE_UNAVAILABLE"})
    assert set().union(*groups.values()) == adapter.CODES | ROUTE_CODES | fp.CODES | civil.CODES
    refusals = {**route.TERRAIN_ROUTE_REFUSALS, **route.CIVIL_ROUTE_REFUSALS}
    for status, codes in groups.items():
        for code in codes:
            expected = (status, ErrorCode.INTERNAL if status >= 500 else ErrorCode.BAD_PARAMS,
                        code in retryable)
            assert refusals[code] == expected
            response = route._terrain_refused(code)
            body = json.loads(response.body)
            assert (response.status_code, body["error"]["reason_code"], body["error"]["retryable"]) == (
                status, code, code in retryable)


def test_terrain_route_civil_codes_answer_from_the_civil_table():
    for code, status, retryable in (("FRAMES_PILES_STALE_BASE", 409, True),
                                   ("CIVIL_GRADE_NO_PAD", 422, False)):
        response = route._terrain_refused(code)
        body = json.loads(response.body)
        assert (response.status_code, body["error"]["reason_code"], body["error"]["retryable"]) == (
            status, code, retryable)


def test_terrain_route_get_no_head(backend, client):
    before = keys(backend)
    response = get(client)
    assert response.status_code == 200
    assert body_of(response) == {"schema": "leaf.solar-terrain-view-response.v1",
                                 "stored": False, "head": None, "terrain": None}
    assert keys(backend) == before


def test_terrain_route_get_equals_the_view(backend, client, monkeypatch):
    imp(backend)
    head, document = ph.load_physical_head(backend, TENANT, DRAWING, project_id=PROJECT)
    before = keys(backend)
    monkeypatch.setattr(write_loop, "drawing_mutations_refusal", lambda: "drained")
    response = get(client, query="?project_id=" + PROJECT)
    assert response.status_code == 200
    assert body_of(response) == {"schema": "leaf.solar-terrain-view-response.v1", "stored": True,
                                 "head": head, "terrain": dict(adapter.terrain_view(document),
                                                               drawing_id=DRAWING, project_id=PROJECT)}
    assert keys(backend) == before


def test_terrain_route_entitlement(client, monkeypatch):
    monkeypatch.setattr(route, "_backend", forbidden)
    monkeypatch.setattr(adapter, "render_mesh", forbidden)
    monkeypatch.setattr(entitlements, "resolve_tier", lambda tenant: "restricted")
    response = post(client)
    assert response.status_code == 403 and response.json()["required"] == "run_write"
    monkeypatch.setattr(entitlements, "resolve_tier", lambda tenant: "guest")
    response = get(client)
    assert response.status_code == 403 and response.json()["required"] == "run_read"

    def unavailable(*args):
        raise entitlements.EntitlementsError("unavailable")
    monkeypatch.setattr(entitlements, "entitlements_for", unavailable)
    assert post(client).status_code == 503
    assert get(client).status_code == 503
    status, body, pulls = asgi(client, [("unread", b"x" * 8193)])
    assert status == 503 and pulls == []


def test_terrain_route_drawing_id_invalid(client, monkeypatch):
    monkeypatch.setattr(route, "_backend", forbidden)
    for drawing_id in ("Bad Id", "a" * 64):
        url = "/api/drawings/" + drawing_id + "/terrain"
        assert_refused(post(client, url=url), 400, "TERRAIN_DRAWING_ID_INVALID")
        assert_refused(get(client, url=url), 400, "TERRAIN_DRAWING_ID_INVALID")
        status, body, pulls = asgi(client, [("unread", b"x" * 8193)], url=url)
        assert (status, body["error"]["reason_code"], pulls) == (400, "TERRAIN_DRAWING_ID_INVALID", [])


def test_terrain_route_physical_passthrough(client, monkeypatch):
    def fail(*args, **kwargs):
        raise ps.PhysicalStateError("PHYSICAL_HEAD_CONFLICT")
    monkeypatch.setattr(adapter, "render_mesh", fail)
    assert_refused(post(client), 409, "PHYSICAL_HEAD_CONFLICT", True)
    assert route.TERRAIN_ROUTE_REFUSALS["PHYSICAL_HEAD_CONFLICT"] == drawings.LANDXML_IMPORT_REFUSALS[
        "PHYSICAL_HEAD_CONFLICT"]


def test_terrain_route_project_id_invalid_reads_no_body(client, monkeypatch):
    monkeypatch.setattr(route, "_backend", forbidden)
    for project_id in ("", "p" * 101):
        query = "?project_id=" + project_id
        assert_refused(post(client, query=query), 400, "TERRAIN_PROJECT_ID_INVALID")
        assert_refused(get(client, query=query), 400, "TERRAIN_PROJECT_ID_INVALID")
        status, body, pulls = asgi(client, [("unread", b"x" * 8193)], query=query)
        assert (status, body["error"]["reason_code"], pulls) == (400, "TERRAIN_PROJECT_ID_INVALID", [])


def test_terrain_route_backend_store_unavailable(client, monkeypatch):
    for error in (RuntimeError, OSError):
        def fail(*args, **kwargs):
            raise error("unavailable")
        monkeypatch.setattr(route, "_backend", fail)
        assert_refused(post(client), 503, "TERRAIN_STORE_UNAVAILABLE", True)
        assert_refused(get(client), 503, "TERRAIN_STORE_UNAVAILABLE", True)


def test_terrain_route_limits_validation_belongs_to_adapter(backend, client):
    imp(backend)
    before = keys(backend)
    for limits in (7, {"Bogus": 1}, {"Columns": True}):
        assert_refused(post(client, {"operation": "slope", "expected_head": current(backend),
                                    "limits": limits}), 400, "TERRAIN_LIMITS_INVALID")
        assert keys(backend) == before


@pytest.mark.parametrize("lease,proof,status", [
    ("active", None, 403), ("active", "valid", 200), ("none", None, 200),
    ("expired", None, 200), ("active", "", 403), ("active", "   ", 403),
    ("active", "garbage", 403), ("none", "", 200), ("none", "   ", 200),
    ("none", "garbage", 200), ("active", "unavailable", 503),
])
def test_terrain_route_checkout_matrix(backend, client, monkeypatch, lease, proof, status):
    import checkout_capability
    import store
    imp(backend)
    if lease != "none":
        # Take a real checkout through the route, including its minted proof.
        client.app.include_router(drawings.router)
        acquired = client.post("/api/drawings/" + DRAWING + "/checkout",
                               json={"holder": "another session"}, headers={"X-Tenant-Id": TENANT})
        assert acquired.status_code == 200
        if proof == "valid":
            proof = acquired.json()["checkout_capability"]
        if lease == "expired":
            manifest = store.load_manifest(backend, TENANT, DRAWING)
            manifest["checkout"]["expires"] = "2000-01-01T00:00:00+00:00"
            store.save_manifest(backend, TENANT, DRAWING, manifest)
    if proof == "unavailable":
        def unavailable(*args, **kwargs):
            raise checkout_capability.CapabilityUnavailable("unavailable")
        monkeypatch.setattr(checkout_capability, "verify", unavailable)
    head = ph.physical_head(backend, TENANT, DRAWING, project_id=PROJECT)
    manifest = store.load_manifest(backend, TENANT, DRAWING)
    before = {key: backend.get(key) for key in keys(backend)}
    headers = {"X-Tenant-Id": TENANT}
    if proof is not None:
        headers["X-Checkout-Capability"] = proof
    response = client.post(URL + "/operations", json={"operation": "mesh", "expected_head": current(backend)},
                           headers=headers)
    assert response.status_code == status
    assert store.load_manifest(backend, TENANT, DRAWING) == manifest
    if status == 200:
        assert response.json()["error"] is None
        assert body_of(response)["created"] is True
        assert current(backend) != head["state"]["artifact_id"]
    else:
        code = "TERRAIN_CHECKOUT_UNAVAILABLE" if status == 503 else "TERRAIN_CHECKOUT_DENIED"
        assert response.json()["error"]["error_code"] == ("INTERNAL" if status == 503 else "BAD_PARAMS")
        assert_refused(response, status, code, status == 503)
        assert ph.physical_head(backend, TENANT, DRAWING, project_id=PROJECT) == head
        assert {key: backend.get(key) for key in keys(backend)} == before


@pytest.mark.parametrize("proof", [None, "garbage"])
def test_terrain_route_missing_drawing_unchanged_by_gate(backend, client, monkeypatch, proof):
    headers = {"X-Tenant-Id": TENANT}
    if proof is not None:
        headers["X-Checkout-Capability"] = proof
    url = "/api/drawings/nosuch/terrain/operations"
    body = {"operation": "mesh", "expected_head": "0" * 64}
    before = {key: backend.get(key) for key in keys(backend)}
    response = client.post(url, json=body, headers=headers)
    assert_refused(response, 404, "TERRAIN_DRAWING_NOT_FOUND")
    assert response.json()["error"]["error_code"] == "BAD_PARAMS"
    monkeypatch.setattr(route, "_lock_authorization", lambda *args: None)
    assert client.post(url, json=body, headers=headers).content == response.content
    assert {key: backend.get(key) for key in keys(backend)} == before
    assert not set(backend.drawing_object_keys(TENANT, "nosuch"))


def test_terrain_route_gate_receives_tenant_context(backend, client, monkeypatch):
    import deps
    imp(backend)
    tenant = deps.TenantContext(TENANT, tier="demo", subject="auth0|terrain-holder")
    client.app.dependency_overrides[deps.require_active_tenant] = lambda: tenant
    seen = []
    def record(drawing_id, context, selected_backend, capability):
        seen.append(context)
    monkeypatch.setattr(route, "_lock_authorization", record)
    response = post(client, {"operation": "mesh", "expected_head": current(backend)})
    assert response.status_code == 200
    assert seen == [tenant] and seen[0] is tenant
    assert isinstance(seen[0], deps.TenantContext) and seen[0].subject == "auth0|terrain-holder"
