"""sf-w4-landxml-import-surface piece one: the LandXML terrain upload route
(POST /api/drawings/{drawing_id}/imports/landxml in server/routers/drawings.py) over the merged
intake server/solar_landxml_import.py. The route bounds the upload before anything is decoded,
answers every refusal with a closed reason code (drawings.LANDXML_IMPORT_REFUSALS) and writes
nothing on refusal; the stored terrain reopens unchanged through the intake's own loaders. Every
expected value below was measured by running the route against the FilesystemBackend the
physical-state tests seed. The positive fixture is the licensed LEAFLANDXMLDEMO capture, read with
CRLF line endings exactly as test_solar_landxml_import reads it."""
import asyncio
import json
import sys
from pathlib import Path

import pytest

SERVER = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SERVER))
sys.path.insert(0, str(SERVER / "tests"))

import solar_landxml_import as lx  # noqa: E402
import solar_physical_head as ph  # noqa: E402
import solar_physical_state as ps  # noqa: E402
import write_loop  # noqa: E402
from test_solar_landxml_import import (  # noqa: E402
    FOOT, MEASURED_FIRST_RESULT, MEASURED_SOURCE_ID, REAL, REAL_SHA, canonical, landxml, plugin_grid)
from test_solar_physical_state import DRAWING, PREFIX, PROJECT, TENANT  # noqa: E402
from test_w1_design_graph import graph  # noqa: E402,F401
from test_w1_solve_commit import seed, seed_graphless  # noqa: E402

URL = "/api/drawings/" + DRAWING + "/imports/landxml"
QUERY = "?drawing_units=m&crs=none"
ROUTE_CODES = {"LANDXML_DRAWING_ID_INVALID", "LANDXML_MEDIA_TYPE_REFUSED", "LANDXML_IMPORT_FAILED",
               "LANDXML_CHECKOUT_DENIED", "LANDXML_CHECKOUT_UNAVAILABLE"}
PASSTHROUGH = {
    "PHYSICAL_HEAD_CONFLICT", "PHYSICAL_HEAD_LOG_FULL", "PHYSICAL_HEAD_PROJECT_MISMATCH",
    "PHYSICAL_STATE_PROJECT_MISMATCH", "PHYSICAL_HEAD_WRITES_DRAINED", "PHYSICAL_STATE_WRITES_DRAINED",
    "PHYSICAL_HEAD_STORE_UNAVAILABLE", "PHYSICAL_STATE_STORE_UNAVAILABLE", "PHYSICAL_HEAD_CORRUPT",
    "PHYSICAL_HEAD_STORE_UNSAFE", "PHYSICAL_STATE_CORRUPT",
}
RETRYABLE = {"LANDXML_WRITES_DRAINED", "LANDXML_STORE_UNAVAILABLE", "PHYSICAL_HEAD_CONFLICT",
             "LANDXML_CHECKOUT_UNAVAILABLE",
             "PHYSICAL_HEAD_WRITES_DRAINED", "PHYSICAL_STATE_WRITES_DRAINED",
             "PHYSICAL_HEAD_STORE_UNAVAILABLE", "PHYSICAL_STATE_STORE_UNAVAILABLE"}
ENVELOPE = ("error", "degraded_mode")


@pytest.fixture
def backend(graph, tmp_path, monkeypatch):
    backend, _ = seed(tmp_path, monkeypatch, graph)
    monkeypatch.setattr(write_loop, "backend_for_tenant", lambda *a, **k: backend)
    return backend


@pytest.fixture
def client(backend, monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from envelopes import install_error_handlers
    from routers import drawings
    monkeypatch.delenv("LEAF_AUTH_LIVE", raising=False)
    monkeypatch.delenv("APS_LIVE", raising=False)
    app = FastAPI()
    install_error_handlers(app)
    app.include_router(drawings.router)
    return TestClient(app, raise_server_exceptions=False)


def keys(backend):
    return set(backend.drawing_object_keys(TENANT, DRAWING))


def post(client, content=REAL, query=QUERY, media="application/xml", url=URL, tenant=TENANT):
    headers = {"X-Tenant-Id": tenant}
    if media is not None:
        headers["Content-Type"] = media
    return client.post(url + query, content=content, headers=headers)


def body_of(response):
    body = response.json()
    assert (body["error"], body["degraded_mode"]) == (None, False)
    return {key: value for key, value in body.items() if key not in ENVELOPE}


def reason(response):
    return response.json()["error"]["reason_code"]


def asgi(client, chunks, headers, query=QUERY):
    """Drive the app with ASGI chunks one at a time (TestClient coalesces a generator body), and
    return (status, body, pulls) where pulls names every chunk the route asked for."""
    pulls = []
    pending = list(chunks)
    messages = []

    async def receive():
        if not pending:
            return {"type": "http.request", "body": b"", "more_body": False}
        name, chunk = pending.pop(0)
        pulls.append(name)
        return {"type": "http.request", "body": chunk, "more_body": bool(pending)}

    async def send(message):
        messages.append(message)

    scope = {"type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1", "method": "POST",
             "scheme": "http", "path": URL, "raw_path": URL.encode(), "query_string": query[1:].encode(),
             "root_path": "", "headers": [(b"x-tenant-id", TENANT.encode())] + headers,
             "client": ("testclient", 50000), "server": ("testserver", 80)}
    asyncio.run(client.app(scope, receive, send))
    status = next(m for m in messages if m["type"] == "http.response.start")["status"]
    body = json.loads(b"".join(m.get("body", b"") for m in messages if m["type"] == "http.response.body"))
    return status, body, pulls


# ------------------------------------------------------------------ contract --

def test_landxml_route_refusal_map_is_closed():
    from envelopes import ErrorCode
    from routers import drawings
    refusals = drawings.LANDXML_IMPORT_REFUSALS
    assert set(refusals) == set(lx.CODES) | ROUTE_CODES | PASSTHROUGH
    assert len(refusals) == 44
    assert PASSTHROUGH <= ps.CODES | ph.CODES
    assert drawings.LANDXML_MEDIA_TYPES == frozenset({"application/xml", "text/xml"})
    for code, (status, envelope_code, retryable) in refusals.items():
        assert status in (400, 403, 404, 409, 413, 415, 422, 500, 503), code
        assert envelope_code == (ErrorCode.INTERNAL if status >= 500 else ErrorCode.BAD_PARAMS), code
        assert retryable is (code in RETRYABLE), code
    assert {code for code, row in refusals.items() if row[0] == 503} == RETRYABLE - {"PHYSICAL_HEAD_CONFLICT"}
    assert refusals["LANDXML_TOO_LARGE"][0] == 413 and refusals["LANDXML_MEDIA_TYPE_REFUSED"][0] == 415
    assert refusals["LANDXML_RESAMPLE_TOO_LARGE"][0] == 422


def test_landxml_route_refusal_statuses_pinned():
    from routers import drawings
    expected = {
        "LANDXML_CHECKOUT_DENIED": 403,
        "LANDXML_CHECKOUT_UNAVAILABLE": 503,
        "LANDXML_DRAWING_ID_INVALID": 400,
        "LANDXML_PROJECT_ID_INVALID": 400,
        "LANDXML_DRAWING_UNITS_INVALID": 400,
        "LANDXML_CRS_INVALID": 400,
        "LANDXML_TARGET_CELLS_INVALID": 400,
        "LANDXML_MEDIA_TYPE_REFUSED": 415,
        "LANDXML_EMPTY": 400,
        "LANDXML_TOO_LARGE": 413,
        "LANDXML_ENCODING_INVALID": 400,
        "LANDXML_UNSAFE": 400,
        "LANDXML_MALFORMED": 400,
        "LANDXML_NOT_LANDXML": 400,
        "LANDXML_UNITS_MISSING": 400,
        "LANDXML_UNITS_UNSUPPORTED": 400,
        "LANDXML_CRS_UNSUPPORTED": 400,
        "LANDXML_TOO_MANY_POINTS": 400,
        "LANDXML_TOO_FEW_POINTS": 400,
        "LANDXML_COORDINATE_OUT_OF_RANGE": 400,
        "LANDXML_SOURCE_ID_INVALID": 400,
        "LANDXML_DRAWING_NOT_FOUND": 404,
        "LANDXML_SOURCE_NOT_FOUND": 404,
        "LANDXML_GRAPH_REQUIRED": 409,
        "LANDXML_PROJECT_MISMATCH": 409,
        "LANDXML_CRS_MISMATCH": 409,
        "LANDXML_UNITS_MISMATCH": 409,
        "LANDXML_SOURCE_KIND_MISMATCH": 409,
        "LANDXML_RESAMPLE_TOO_LARGE": 422,
        "LANDXML_WRITES_DRAINED": 503,
        "LANDXML_STORE_UNAVAILABLE": 503,
        "LANDXML_SOURCE_CORRUPT": 500,
        "PHYSICAL_HEAD_CONFLICT": 409,
        "PHYSICAL_HEAD_LOG_FULL": 409,
        "PHYSICAL_HEAD_PROJECT_MISMATCH": 409,
        "PHYSICAL_STATE_PROJECT_MISMATCH": 409,
        "PHYSICAL_HEAD_WRITES_DRAINED": 503,
        "PHYSICAL_STATE_WRITES_DRAINED": 503,
        "PHYSICAL_HEAD_STORE_UNAVAILABLE": 503,
        "PHYSICAL_STATE_STORE_UNAVAILABLE": 503,
        "PHYSICAL_HEAD_CORRUPT": 500,
        "PHYSICAL_HEAD_STORE_UNSAFE": 500,
        "PHYSICAL_STATE_CORRUPT": 500,
        "LANDXML_IMPORT_FAILED": 500,
    }
    assert expected == {code: row[0] for code, row in drawings.LANDXML_IMPORT_REFUSALS.items()}


# ------------------------------------------------------------- the positive --

def test_landxml_route_import_reopens_unchanged(backend, client):
    before = keys(backend)
    response = post(client)
    assert response.status_code == 200
    result = body_of(response)
    assert canonical(result) == MEASURED_FIRST_RESULT
    assert result["source"]["artifact_id"] == MEASURED_SOURCE_ID
    assert (result["created"], result["head"]["index"], result["points"]) == (
        True, 0, {"declared": 441, "accepted": 441, "skipped": 0})
    state = result["head"]["state"]
    assert keys(backend) - before == {
        PREFIX + "artifacts/" + MEASURED_SOURCE_ID + ".json",
        PREFIX + "artifacts/blobs/" + REAL_SHA + ".bin",
        PREFIX + "artifacts/" + state["artifact_id"] + ".json",
        PREFIX + "artifacts/blobs/" + state["content_sha256"] + ".bin",
        PREFIX + "physical/head-0000.json"}
    head, document, grid = lx.load_terrain(backend, TENANT, DRAWING, project_id=PROJECT)
    assert head == result["head"] == ph.physical_head(backend, TENANT, DRAWING, project_id=PROJECT)
    assert grid == plugin_grid(REAL) and repr(grid) == repr(plugin_grid(REAL))
    assert canonical(grid) == "1c78f602025556918336710801265c08a50a9e9098b48d2bda4a669bc9927452"
    assert (document["units"], document["frame"]["crs"], document["source"]) == (
        {"drawing_units": "m", "meters_per_unit": 1.0}, "none", {"kind": "ground-intake", "sha256": REAL_SHA})
    meta, content = lx.load_landxml_source(backend, TENANT, DRAWING, MEASURED_SOURCE_ID, project_id=PROJECT)
    assert content == REAL and meta["tool"] == "solar-landxml-source"


def test_landxml_route_source_downloads(backend, client):
    result = body_of(post(client))
    downloaded = client.get(result["source"]["download"], headers={"X-Tenant-Id": TENANT})
    assert result["source"]["download"] == "/api/drawings/solar/artifacts/" + MEASURED_SOURCE_ID
    assert downloaded.status_code == 200 and downloaded.content == REAL
    assert downloaded.headers["content-type"] == "application/xml"
    assert downloaded.headers["content-disposition"] == 'attachment; filename="landxml-source.xml"'
    assert downloaded.headers["etag"] == '"' + REAL_SHA + '"'


def test_landxml_route_equals_the_intake(backend, client, tmp_path, monkeypatch, graph):
    routed = body_of(post(client, query="?drawing_units=m&crs=none&target_cells=12"))
    other = tmp_path / "direct"
    other.mkdir()
    direct_backend, _ = seed(other, monkeypatch, graph)
    direct = lx.import_landxml_terrain(direct_backend, TENANT, DRAWING, REAL, drawing_units="m", crs="none",
                                       target_cells=12)
    assert routed == direct
    assert (routed["grid"]["rows"], routed["grid"]["cols"], routed["grid"]["target_cells"]) == (12, 12, 12)


@pytest.mark.parametrize("media", ["text/xml", "Application/XML; charset=utf-8"])
def test_landxml_route_media_types(backend, client, media):
    response = post(client, media=media)
    assert response.status_code == 200
    assert canonical(body_of(response)) == MEASURED_FIRST_RESULT


def test_landxml_route_same_file_twice_writes_nothing(backend, client):
    first = body_of(post(client))
    before = keys(backend)
    again = post(client)
    assert again.status_code == 200
    assert body_of(again) == dict(first, created=False)
    assert keys(backend) == before


def test_landxml_route_new_cells_is_a_child(backend, client):
    first = body_of(post(client))
    second = body_of(post(client, query=QUERY + "&target_cells=010"))
    assert (second["created"], second["head"]["index"], second["head"]["parent"]) == (
        True, 1, first["head"]["state"]["artifact_id"])
    assert lx.load_terrain(backend, TENANT, DRAWING, project_id=PROJECT)[2] == plugin_grid(REAL, 10)


def test_landxml_route_feet_file_into_feet(backend, client):
    data = landxml(units=FOOT)
    result = body_of(post(client, data, "?drawing_units=ft&crs=none"))
    assert (result["interpretation"]["linear_unit"], result["interpretation"]["horizontal_scale"],
            result["interpretation"]["elevation_scale"]) == ("foot", 1.0, 0.3048)
    assert lx.load_terrain(backend, TENANT, DRAWING, project_id=PROJECT)[2] == plugin_grid(
        data, 30, elevation_scale=0.3048)


def test_landxml_route_file_crs(backend, client):
    data = landxml(crs='<CoordinateSystem name="CA V" epsgCode="2229"/>', units=FOOT)
    result = body_of(post(client, data, "?drawing_units=ft&crs=EPSG:2229"))
    assert (result["interpretation"]["crs"], result["interpretation"]["crs_source"]) == ("EPSG:2229", "file")
    assert lx.load_terrain(backend, TENANT, DRAWING, project_id=PROJECT)[1]["frame"]["crs"] == "EPSG:2229"


def test_landxml_route_matching_project(backend, client):
    response = post(client, query=QUERY + "&project_id=" + PROJECT)
    assert response.status_code == 200 and body_of(response)["project_id"] == PROJECT


# ----------------------------------------------------------------- refusals --

CASES = [
    ("media", 415, "LANDXML_MEDIA_TYPE_REFUSED"),
    ("no-media", 415, "LANDXML_MEDIA_TYPE_REFUSED"),
    ("pdf-media", 415, "LANDXML_MEDIA_TYPE_REFUSED"),
    ("empty", 400, "LANDXML_EMPTY"),
    ("large", 413, "LANDXML_TOO_LARGE"),
    ("chunked", 413, "LANDXML_TOO_LARGE"),
    ("drawing-id", 400, "LANDXML_DRAWING_ID_INVALID"),
    ("project-long", 400, "LANDXML_PROJECT_ID_INVALID"),
    ("project-empty", 400, "LANDXML_PROJECT_ID_INVALID"),
    ("project", 409, "LANDXML_PROJECT_MISMATCH"),
    ("units-missing", 400, "LANDXML_DRAWING_UNITS_INVALID"),
    ("units-in", 400, "LANDXML_DRAWING_UNITS_INVALID"),
    ("crs-missing", 400, "LANDXML_CRS_INVALID"),
    ("crs-lower", 400, "LANDXML_CRS_INVALID"),
    ("cells-text", 400, "LANDXML_TARGET_CELLS_INVALID"),
    ("cells-long", 400, "LANDXML_TARGET_CELLS_INVALID"),
    ("cells-negative", 400, "LANDXML_TARGET_CELLS_INVALID"),
    ("cells-empty", 400, "LANDXML_TARGET_CELLS_INVALID"),
    ("cells-low", 400, "LANDXML_TARGET_CELLS_INVALID"),
    ("cells-high", 400, "LANDXML_TARGET_CELLS_INVALID"),
    ("encoding", 400, "LANDXML_ENCODING_INVALID"),
    ("doctype", 400, "LANDXML_UNSAFE"),
    ("malformed", 400, "LANDXML_MALFORMED"),
    ("kml", 400, "LANDXML_NOT_LANDXML"),
    ("no-units", 400, "LANDXML_UNITS_MISSING"),
    ("millimeter", 400, "LANDXML_UNITS_UNSUPPORTED"),
    ("crs-file", 400, "LANDXML_CRS_UNSUPPORTED"),
    ("two-points", 400, "LANDXML_TOO_FEW_POINTS"),
    ("far", 400, "LANDXML_COORDINATE_OUT_OF_RANGE"),
    ("crs-mismatch", 409, "LANDXML_CRS_MISMATCH"),
    ("resample", 422, "LANDXML_RESAMPLE_TOO_LARGE"),
    ("drained", 503, "LANDXML_WRITES_DRAINED"),
    ("graphless", 409, "LANDXML_GRAPH_REQUIRED"),
    ("missing", 404, "LANDXML_DRAWING_NOT_FOUND"),
    ("backend", 503, "LANDXML_STORE_UNAVAILABLE"),
]


@pytest.mark.parametrize("case,status,code", CASES, ids=[case for case, _, _ in CASES])
def test_landxml_route_refusals(backend, client, tmp_path, monkeypatch, case, status, code):
    content, query, media, url = REAL, QUERY, "application/xml", URL
    if case == "media":
        media = "text/plain"
    elif case == "no-media":
        media = None
    elif case == "pdf-media":
        media = "application/pdf"
    elif case == "empty":
        content = b""
    elif case in ("large", "chunked"):
        monkeypatch.setattr(lx, "MAX_LANDXML_BYTES", len(REAL) - 1)
        content = iter([REAL[:100], REAL[100:]]) if case == "chunked" else REAL
    elif case == "drawing-id":
        url = URL.replace("/solar/", "/Bad!/")
    elif case == "project-long":
        query += "&project_id=" + "p" * 101
    elif case == "project-empty":
        query += "&project_id="
    elif case == "project":
        query += "&project_id=leaf:project:other"
    elif case == "units-missing":
        query = "?crs=none"
    elif case == "units-in":
        query = "?drawing_units=in&crs=none"
    elif case == "crs-missing":
        query = "?drawing_units=m"
    elif case == "crs-lower":
        query = "?drawing_units=m&crs=epsg:4326"
    elif case.startswith("cells-"):
        query += "&target_cells=" + {"cells-text": "abc", "cells-long": "1000", "cells-negative": "-5",
                                     "cells-empty": "", "cells-low": "1", "cells-high": "201"}[case]
    elif case == "encoding":
        content = b"\xff"
    elif case == "doctype":
        content = b'<?xml version="1.0"?><!DOCTYPE LandXML [<!ENTITY a "b">]><LandXML/>'
    elif case == "malformed":
        content = b"<LandXML><Units>"
    elif case == "kml":
        content = b"<kml><Units/></kml>"
    elif case == "no-units":
        content = landxml(units=None)
    elif case == "millimeter":
        content = landxml(units='<Metric linearUnit="millimeter"/>')
    elif case == "crs-file":
        content = landxml(crs='<CoordinateSystem name="no code"/>')
    elif case == "two-points":
        content = landxml(["0 0 1", "1 1 1"])
    elif case == "far":
        content = landxml(["0 0 1", "1 1 1", "0 1000000001 1"])
    elif case == "crs-mismatch":
        content = landxml(crs='<CoordinateSystem name="CA V" epsgCode="2229"/>')
    elif case == "resample":
        content = landxml(["%d %d 1" % (i // 25, i % 25) for i in range(600)])
        query += "&target_cells=187"
    elif case == "drained":
        monkeypatch.setenv("LEAF_DRAWING_MUTATIONS_ENABLED", "0")
    elif case == "graphless":
        path = tmp_path / "graphless"
        path.mkdir()
        backend, _ = seed_graphless(path, monkeypatch)
        monkeypatch.setattr(write_loop, "backend_for_tenant", lambda *a, **k: backend)
    elif case == "missing":
        url = URL.replace("/solar/", "/nosuch/")
    elif case == "backend":
        from routers import drawings

        def unavailable(*args):
            raise RuntimeError("unavailable")
        monkeypatch.setattr(drawings, "_backend", unavailable)
    before = keys(backend)
    response = post(client, content, query, media, url)
    assert response.status_code == status
    error = response.json()["error"]
    assert error["reason_code"] == code
    assert error["retryable"] is (code in RETRYABLE)
    assert keys(backend) == before


def test_landxml_route_head_units_mismatch(backend, client):
    assert post(client).status_code == 200
    before = keys(backend)
    response = post(client, query="?drawing_units=ft&crs=none")
    assert (response.status_code, reason(response)) == (409, "LANDXML_UNITS_MISMATCH")
    assert keys(backend) == before


def test_landxml_route_head_conflict_passes_through(backend, client, monkeypatch):
    def conflict(*args, **kwargs):
        raise ph.PhysicalHeadError("PHYSICAL_HEAD_CONFLICT")
    monkeypatch.setattr(ph, "publish_physical_state", conflict)
    response = post(client)
    assert (response.status_code, reason(response), response.json()["error"]["retryable"]) == (
        409, "PHYSICAL_HEAD_CONFLICT", True)
    assert not {key for key in keys(backend) if "/physical/" in key}


def test_landxml_route_unlisted_code_is_import_failed(backend, client, monkeypatch):
    def unlisted(*args, **kwargs):
        raise ps.PhysicalStateError("PHYSICAL_STATE_KEY_UNKNOWN")
    monkeypatch.setattr(lx, "import_landxml_terrain", unlisted)
    response = post(client)
    assert (response.status_code, reason(response), response.json()["error"]["retryable"]) == (
        500, "LANDXML_IMPORT_FAILED", False)


# ------------------------------------------------------- bounds before work --

def test_landxml_route_huge_content_length(backend, client, monkeypatch):
    def inspected(*args):
        raise AssertionError("decoded past the declared length")
    monkeypatch.setattr(lx, "inspect_landxml", inspected)
    for length in ("9" * 4301, str(lx.MAX_LANDXML_BYTES + 1)):
        status, body, pulls = asgi(client, [("xml", REAL)], [
            (b"content-type", b"application/xml"), (b"content-length", length.encode())])
        assert (status, body["error"]["reason_code"], pulls) == (413, "LANDXML_TOO_LARGE", [])
    assert not {key for key in keys(backend) if "/artifacts/" in key or "/physical/" in key}


def test_landxml_route_zero_padded_content_length(backend, client):
    status, body, _ = asgi(client, [("xml", REAL)], [
        (b"content-type", b"application/xml"), (b"content-length", ("0" * 4300 + str(len(REAL))).encode())])
    assert status == 200
    assert canonical({key: value for key, value in body.items() if key not in ENVELOPE}) == MEASURED_FIRST_RESULT


def test_landxml_route_stream_cutoff(backend, client, monkeypatch):
    def inspected(*args):
        raise AssertionError("decoded past the cap")
    monkeypatch.setattr(lx, "MAX_LANDXML_BYTES", 150)
    monkeypatch.setattr(lx, "inspect_landxml", inspected)
    status, body, pulls = asgi(client, [("xml", REAL[:100]), ("over", REAL[100:200]), ("past", REAL[200:])],
                               [(b"content-type", b"application/xml"), (b"transfer-encoding", b"chunked")])
    assert (status, body["error"]["reason_code"]) == (413, "LANDXML_TOO_LARGE")
    assert pulls == ["xml", "over"]
    assert not {key for key in keys(backend) if "/artifacts/" in key or "/physical/" in key}


@pytest.mark.parametrize("query,drained,status,code", [
    (QUERY + "&target_cells=x1", False, 400, "LANDXML_TARGET_CELLS_INVALID"),
    (QUERY + "&project_id=" + "p" * 101, False, 400, "LANDXML_PROJECT_ID_INVALID"),
    (QUERY, True, 503, "LANDXML_WRITES_DRAINED"),
], ids=["cells", "project", "drained"])
def test_landxml_route_cheap_refusals_read_no_body(backend, client, monkeypatch, query, drained, status, code):
    if drained:
        monkeypatch.setenv("LEAF_DRAWING_MUTATIONS_ENABLED", "0")
    result, body, pulls = asgi(client, [("xml", REAL)], [(b"content-type", b"application/xml")], query=query)
    assert (result, body["error"]["reason_code"], pulls) == (status, code, [])


@pytest.mark.parametrize("unavailable", [False, True])
def test_landxml_route_entitlement(backend, client, monkeypatch, unavailable):
    import entitlements

    def policy(*args):
        if unavailable:
            raise entitlements.EntitlementsError("x")
        return {"upload": False}
    monkeypatch.setattr(entitlements, "entitlements_for", policy)
    response = post(client)
    assert response.status_code == (503 if unavailable else 403)
    if not unavailable:
        assert response.json()["entitlement_required"] is True
        assert response.json()["required"] == "upload"
    assert not {key for key in keys(backend) if "/artifacts/" in key or "/physical/" in key}


def test_landxml_route_guest_is_refused(backend, client, monkeypatch):
    import time
    import guest_uploads
    from routers import drawings

    monkeypatch.setenv("LEAF_AUTH_LIVE", "1")
    monkeypatch.setenv("LEAF_GUEST_SECRET", "test-secret-not-a-real-one")
    token = guest_uploads.mint_guest_session(guest_uploads.mint_guest_tenant_id(), int(time.time()) + 3600)
    assert token
    accesses = []

    def backend_forbidden(*args, **kwargs):
        accesses.append(True)
        raise AssertionError("guest reached backend")
    monkeypatch.setattr(drawings, "_backend", backend_forbidden)
    monkeypatch.setattr(write_loop, "backend_for_tenant", backend_forbidden)
    response = client.post(URL + QUERY, content=REAL, headers={
        "Content-Type": "application/xml", "X-Guest-Session": token})
    assert response.status_code == 403 and "upload-only" in response.text
    assert accesses == []


def test_landxml_route_foreign_tenant(backend, client):
    response = post(client, tenant="other-tenant")
    assert (response.status_code, reason(response)) == (404, "LANDXML_DRAWING_NOT_FOUND")
    assert not {key for key in keys(backend) if "/artifacts/" in key or "/physical/" in key}
    assert not {key for key in backend.drawing_object_keys("other-tenant", DRAWING)
                if "/artifacts/" in key or "/physical/" in key}


@pytest.mark.parametrize("lease,proof,status", [
    ("active", None, 403), ("active", "valid", 200), ("none", None, 200),
    ("expired", None, 200), ("active", "", 403), ("active", "   ", 403),
    ("active", "garbage", 403), ("none", "", 200), ("none", "   ", 200),
    ("none", "garbage", 200), ("active", "unavailable", 503),
])
def test_landxml_route_checkout_matrix(backend, client, monkeypatch, lease, proof, status):
    import checkout_capability
    import store
    # An existing physical head makes unchanged-head assertions substantive.
    lx.import_landxml_terrain(backend, TENANT, DRAWING, REAL, drawing_units="m", crs="none")
    if lease != "none":
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
    headers = {"X-Tenant-Id": TENANT, "Content-Type": "application/xml"}
    if proof is not None:
        headers["X-Checkout-Capability"] = proof
    response = client.post(URL + QUERY, content=landxml(), headers=headers)
    assert response.status_code == status
    assert store.load_manifest(backend, TENANT, DRAWING) == manifest
    if status == 200:
        assert response.json()["error"] is None
        assert body_of(response)["created"] is True
        assert body_of(response)["head"]["state"]["artifact_id"] != head["state"]["artifact_id"]
    else:
        code = "LANDXML_CHECKOUT_UNAVAILABLE" if status == 503 else "LANDXML_CHECKOUT_DENIED"
        error = response.json()["error"]
        assert (error["error_code"], error["reason_code"], error["retryable"]) == (
            "INTERNAL" if status == 503 else "BAD_PARAMS", code, status == 503)
        assert ph.physical_head(backend, TENANT, DRAWING, project_id=PROJECT) == head
        # Compare all bytes, including source metadata/blobs and the version manifest.
        assert {key: backend.get(key) for key in keys(backend)} == before


@pytest.mark.parametrize("proof", [None, "garbage"])
def test_landxml_route_missing_drawing_unchanged_by_gate(backend, client, monkeypatch, proof):
    from routers import drawings
    headers = {"X-Tenant-Id": TENANT, "Content-Type": "application/xml"}
    if proof is not None:
        headers["X-Checkout-Capability"] = proof
    url = URL.replace("/solar/", "/nosuch/") + QUERY
    before = {key: backend.get(key) for key in keys(backend)}
    response = client.post(url, content=REAL, headers=headers)
    assert response.status_code == 404
    assert (response.json()["error"]["error_code"], reason(response)) == (
        "BAD_PARAMS", "LANDXML_DRAWING_NOT_FOUND")
    monkeypatch.setattr(drawings, "_lock_authorization", lambda *args: None)
    assert client.post(url, content=REAL, headers=headers).content == response.content
    assert {key: backend.get(key) for key in keys(backend)} == before
    assert not set(backend.drawing_object_keys(TENANT, "nosuch"))


@pytest.mark.parametrize("proof", [None, "", "   ", "garbage", "unavailable"])
def test_landxml_route_refused_import_writes_no_artifact(backend, client, monkeypatch, proof):
    import checkout_capability
    import store
    acquired = client.post("/api/drawings/" + DRAWING + "/checkout",
                           json={"holder": "another session"}, headers={"X-Tenant-Id": TENANT})
    assert acquired.status_code == 200
    if proof == "unavailable":
        def unavailable(*args, **kwargs):
            raise checkout_capability.CapabilityUnavailable("unavailable")
        monkeypatch.setattr(checkout_capability, "verify", unavailable)
    def importer_forbidden(*args, **kwargs):
        pytest.fail("checkout refusal reached the source artifact writer")
    monkeypatch.setattr(lx, "import_landxml_terrain", importer_forbidden)
    head = ph.physical_head(backend, TENANT, DRAWING, project_id=PROJECT)
    manifest = store.load_manifest(backend, TENANT, DRAWING)
    before = {key: backend.get(key) for key in keys(backend)}
    headers = {"X-Tenant-Id": TENANT, "Content-Type": "application/xml"}
    if proof is not None:
        headers["X-Checkout-Capability"] = proof
    response = client.post(URL + QUERY, content=REAL, headers=headers)
    status = 503 if proof == "unavailable" else 403
    assert response.status_code == status
    error = response.json()["error"]
    assert (error["error_code"], error["reason_code"], error["retryable"]) == (
        "INTERNAL" if status == 503 else "BAD_PARAMS",
        "LANDXML_CHECKOUT_UNAVAILABLE" if status == 503 else "LANDXML_CHECKOUT_DENIED", status == 503)
    assert ph.physical_head(backend, TENANT, DRAWING, project_id=PROJECT) == head
    assert store.load_manifest(backend, TENANT, DRAWING) == manifest
    assert {key: backend.get(key) for key in keys(backend)} == before
    assert not {key for key in keys(backend) if "/artifacts/" in key}


def test_landxml_route_gate_receives_tenant_context(backend, client, monkeypatch):
    import deps
    from routers import drawings
    tenant = deps.TenantContext(TENANT, tier="demo", subject="auth0|landxml-holder")
    client.app.dependency_overrides[deps.require_active_tenant] = lambda: tenant
    seen = []
    def record(drawing_id, context, selected_backend, capability):
        seen.append(context)
    monkeypatch.setattr(drawings, "_lock_authorization", record)
    assert post(client).status_code == 200
    assert seen == [tenant] and seen[0] is tenant
    assert isinstance(seen[0], deps.TenantContext) and seen[0].subject == "auth0|landxml-holder"
