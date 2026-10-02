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
               "TERRAIN_BODY_TOO_LARGE", "TERRAIN_MEDIA_TYPE_REFUSED", "TERRAIN_OPERATION_FAILED"}


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
        assert (status, body["error"]["reason_code"], pulls) == (413, "TERRAIN_BODY_TOO_LARGE", [])
    status, body, pulls = asgi(client, [("first", b"x" * 8192), ("overflow", b"x"), ("unread", b"x")],
                               [(b"content-type", b"application/json")])
    assert (status, body["error"]["reason_code"], pulls) == (
        413, "TERRAIN_BODY_TOO_LARGE", ["first", "overflow"])


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
    passthrough = {key for key in drawings.LANDXML_IMPORT_REFUSALS if key.startswith("PHYSICAL_")}
    assert set(route.TERRAIN_ROUTE_REFUSALS) == adapter.CODES | passthrough | ROUTE_CODES
    for key in passthrough:
        assert route.TERRAIN_ROUTE_REFUSALS[key] == drawings.LANDXML_IMPORT_REFUSALS[key]
    assert route.MAX_TERRAIN_BODY_BYTES == 8192


def test_terrain_route_refusal_statuses_pinned():
    from envelopes import ErrorCode
    groups = {
        400: {"TERRAIN_PROJECT_ID_INVALID", "TERRAIN_EXPECTED_HEAD_INVALID", "TERRAIN_LIMITS_INVALID",
              "TERRAIN_DRAWING_ID_INVALID", "TERRAIN_OPERATION_INVALID", "TERRAIN_BODY_INVALID"},
        404: {"TERRAIN_DRAWING_NOT_FOUND", "TERRAIN_STATE_NOT_FOUND"},
        409: {"TERRAIN_GRAPH_REQUIRED", "TERRAIN_PROJECT_MISMATCH", "TERRAIN_FRAME_UNSUPPORTED",
              "TERRAIN_GRID_MISSING", "TERRAIN_NO_TRACKER_ROWS", "TERRAIN_HEAD_MOVED"},
        413: {"TERRAIN_BODY_TOO_LARGE"}, 415: {"TERRAIN_MEDIA_TYPE_REFUSED"},
        422: {"TERRAIN_GRID_INVALID", "TERRAIN_GRID_TOO_LARGE", "TERRAIN_FRAMES_INVALID", "TERRAIN_TOO_MANY_ROWS"},
        500: {"TERRAIN_OPERATION_FAILED"},
        503: {"TERRAIN_WRITES_DRAINED", "TERRAIN_STORE_UNAVAILABLE"},
    }
    retryable = {"TERRAIN_HEAD_MOVED", "TERRAIN_WRITES_DRAINED", "TERRAIN_STORE_UNAVAILABLE"}
    assert set().union(*groups.values()) == adapter.CODES | ROUTE_CODES
    for status, codes in groups.items():
        for code in codes:
            expected = (status, ErrorCode.INTERNAL if status >= 500 else ErrorCode.BAD_PARAMS,
                        code in retryable)
            assert route.TERRAIN_ROUTE_REFUSALS[code] == expected
            response = route._terrain_refused(code)
            body = json.loads(response.body)
            assert (response.status_code, body["error"]["reason_code"], body["error"]["retryable"]) == (
                status, code, code in retryable)


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
