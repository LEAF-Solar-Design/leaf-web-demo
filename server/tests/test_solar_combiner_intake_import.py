"""The combiner intake import (sf-w2-combiners-intake-producer): server/solar_combiner_intake_import.py and its
route POST /api/drawings/{drawing_id}/imports/combiner-intake in server/routers/drawings.py. The drawing's
recorded LEAFCOMBINERAUTO input (combiner-intake-v1) and its panel-group outlines are bound to the head graph,
then stored on a new version beside the unchanged graph, so the solar-combiners tool finds them through the
source_intake trusted input. C5 end to end: import, then the tool's commit on the imported version gives the
tool's own C5 output. Every refusal is a key of drawings.COMBINER_INTAKE_IMPORT_REFUSALS and writes nothing.
Every expected value below was measured by running the module and the route on a FilesystemBackend."""
import asyncio
import copy
import json
import sys
from pathlib import Path

import pytest

SERVER = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SERVER))
sys.path.insert(0, str(SERVER / "tests"))

import write_loop  # noqa: E402  (first: puts da/ on sys.path for store)
import store  # noqa: E402
import solar_combiner_graph as cg  # noqa: E402
import solar_combiner_intake_import as cii  # noqa: E402
import solar_design_graph as sdg  # noqa: E402
from test_w1_design_graph import graph  # noqa: E402,F401
from test_w1_local_graph_adapter import held  # noqa: E402
from test_w1_solve_commit import seed_graphless  # noqa: E402
from test_solar_tool_combiners import C5_TOOL_SHA, I4_SHA, dispatch, i4, request, sha, stored  # noqa: E402
from test_solar_w2_registration import head_graph, latest  # noqa: E402

TENANT = "fixture-tenant"
DRAWING = "solar"
PROJECT = "leaf:project:00000000-0000-4000-8000-000000000001"
URL = "/api/drawings/" + DRAWING + "/imports/combiner-intake"
ENVELOPE = ("error", "degraded_mode")
ROUTE_CODES = {"COMBINER_IMPORT_DRAWING_ID_INVALID", "COMBINER_IMPORT_MEDIA_TYPE_REFUSED", "COMBINER_IMPORT_FAILED"}
RETRYABLE = {"COMBINER_IMPORT_HEAD_MOVED", "COMBINER_IMPORT_WRITES_DRAINED", "COMBINER_IMPORT_STORE_UNAVAILABLE",
             "COMBINER_IMPORT_CHECKOUT_UNAVAILABLE"}
FIRST_RESULT_SHA = "e77123bd3a8c520bf7659dd5c7043cf92ccff85c18f895a589d4a1dde035daaa"
STORED_SHA = "2b771741ec50542aef1ed113f1779005e5b041fc25612b7d945c6348f5ebd705"
INTAKE_SHA = "2f213a4625781c3fd6acbb7ae99f5455500e5d5b3802835188c153879feda7e5"
GROUPS_SHA = "9f76cbfe1725256991476b829511a47e0c009fbff815ea05af3b242fb61553e1"
NOTE = "solar-combiner-intake:62323fec9d48a362e4c66782642a8214cd8d7c90cf8ef40d8d82c3fd22b9bd87"


def encode(intake, groups, **extra):
    return json.dumps(dict({"combiner_intake": intake, "panel_groups": groups}, **extra)).encode("utf-8")


@pytest.fixture(scope="module")
def i4_inputs():
    return i4()


@pytest.fixture
def world(i4_inputs, tmp_path, monkeypatch):
    """(backend, graph, intake, groups): the i4 drawing stored as an intake-backed graph, version 1."""
    g, intake, groups = copy.deepcopy(i4_inputs)
    backend = stored(tmp_path, monkeypatch, g, {})
    monkeypatch.setattr(write_loop, "backend_for_tenant", lambda *a, **k: backend)
    return backend, g, intake, groups


@pytest.fixture
def client(world, monkeypatch):
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


def sub(tmp_path, name):
    path = tmp_path / name
    path.mkdir()
    return path


def keys(backend):
    return set(backend.drawing_object_keys(TENANT, DRAWING))


def post(client, content, query="", media="application/json", url=URL, tenant=TENANT, headers=None):
    sent = {"X-Tenant-Id": tenant, **(headers or {})}
    if media is not None:
        sent["Content-Type"] = media
    return client.post(url + query, content=content, headers=sent)


def body_of(response):
    body = response.json()
    assert (body["error"], body["degraded_mode"]) == (None, False)
    return {key: value for key, value in body.items() if key not in ENVELOPE}


def reason(response):
    return response.json()["error"]["reason_code"]


def version_intake(backend, version):
    _, key = store.resolve_version(backend, TENANT, DRAWING, version)
    return json.loads(backend.get(key))


def asgi(client, chunks, headers):
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
             "scheme": "http", "path": URL, "raw_path": URL.encode(), "query_string": b"",
             "root_path": "", "headers": [(b"x-tenant-id", TENANT.encode())] + headers,
             "client": ("testclient", 50000), "server": ("testserver", 80)}
    asyncio.run(client.app(scope, receive, send))
    status = next(m for m in messages if m["type"] == "http.response.start")["status"]
    body = json.loads(b"".join(m.get("body", b"") for m in messages if m["type"] == "http.response.body"))
    return status, body, pulls


# ------------------------------------------------------------------ contract --

def test_combiner_intake_import_constants():
    assert cii.RESULT_SCHEMA == "leaf.solar-combiner-intake-import.v1"
    assert cii.DUMP_SCHEMA == "leaf.combiner-placement-dump.v1"
    assert cii.TOOL == "solar-combiner-intake-import" and cii.NOTE_PREFIX == "solar-combiner-intake:"
    assert (cii.MAX_IMPORT_BYTES, cii.MAX_PROJECT_ID_CHARS, cii.MAX_GROUPS, cii.MAX_HANDLE) == (
        16_777_216, 100, 10_000, 64)
    assert cii.BODY_KEYS == frozenset({"combiner_intake", "panel_groups"})
    assert len(cii.CODES) == 18 and len(cii.PASSTHROUGH_CODES) == 8
    assert cii.PASSTHROUGH_CODES < set(cg.CODES)
    assert not cii.CODES & cii.PASSTHROUGH_CODES
    assert all(code.startswith("COMBINER_IMPORT_") and len(code) <= 64 for code in cii.CODES)
    assert issubclass(cii.CombinerIntakeImportError, ValueError)


def test_combiner_intake_import_refusal_map_is_closed():
    from envelopes import ErrorCode
    from routers import drawings
    refusals = drawings.COMBINER_INTAKE_IMPORT_REFUSALS
    assert set(refusals) == set(cii.CODES) | set(cii.PASSTHROUGH_CODES) | ROUTE_CODES
    assert len(refusals) == 29
    assert drawings.COMBINER_INTAKE_MEDIA_TYPES == frozenset({"application/json"})
    for code, (status, envelope_code, retryable) in refusals.items():
        assert status in (400, 403, 404, 409, 413, 415, 500, 503), code
        assert envelope_code == (ErrorCode.INTERNAL if status >= 500 else ErrorCode.BAD_PARAMS), code
        assert retryable is (code in RETRYABLE), code
    assert {code for code, row in refusals.items() if row[0] == 503} == RETRYABLE - {"COMBINER_IMPORT_HEAD_MOVED"}
    assert [refusals[c][0] for c in ("COMBINER_IMPORT_TOO_LARGE", "COMBINER_IMPORT_INTAKE_TOO_LARGE",
                                      "COMBINER_IMPORT_MEDIA_TYPE_REFUSED", "COMBINER_IMPORT_CHECKOUT_DENIED",
                                      "COMBINER_IMPORT_DRAWING_NOT_FOUND", "COMBINER_INTAKE_L2_MISMATCH",
                                      "COMBINER_INTAKE_INVALID", "COMBINER_IMPORT_HEAD_MOVED")] == [
        413, 413, 415, 403, 404, 409, 400, 409]


# ---------------------------------------------------------------- the import --

def test_combiner_intake_import_route_stores_and_the_tool_places(world, client):
    backend, g, intake, groups = world
    assert intake["schema"] == cii.DUMP_SCHEMA
    response = post(client, encode(intake, groups))
    assert response.status_code == 200
    result = body_of(response)
    assert sha(result) == FIRST_RESULT_SHA
    assert result == {
        "schema_version": "leaf.solar-combiner-intake-import.v1", "drawing_id": DRAWING, "project_id": PROJECT,
        "created": True, "version": 2, "parent_version": 1, "graph_sha256": I4_SHA, "graph_rev": 0,
        "intake_sha256": STORED_SHA, "combiner_intake_sha256": INTAKE_SHA, "panel_groups_sha256": GROUPS_SHA,
        "bound": {"l2_inverters": 8, "strings": 173}, "panel_groups": 11, "outline_vertices": 245}
    assert latest(backend) == 2 and head_graph(backend) == g
    assert version_intake(backend, 2) == dict(version_intake(backend, 1), combiner_intake=intake,
                                              panel_groups=groups)
    entry = store.load_manifest(backend, TENANT, DRAWING)["versions"][-1]
    assert (entry["v"], entry["parent"], entry["tool"], entry["note"], entry["sha256"]) == (
        2, 1, "solar-combiner-intake-import", NOTE, STORED_SHA)
    receipt = dispatch(backend, request(), source_version=2)
    assert receipt["new_version"] == {"drawing_id": DRAWING, "version": 3, "parent": 2}
    assert (receipt["before_rev"], receipt["after_rev"]) == (0, 1)
    assert sha(head_graph(backend)) == C5_TOOL_SHA
    kept = version_intake(backend, 3)
    assert kept["combiner_intake"] == intake and kept["panel_groups"] == groups


def test_combiner_intake_import_route_equals_the_module(world, client, i4_inputs, tmp_path, monkeypatch):
    backend, g, intake, groups = world
    routed = body_of(post(client, encode(intake, groups)))
    other = stored(sub(tmp_path, "other"), monkeypatch, copy.deepcopy(i4_inputs[0]), {})
    assert cii.import_combiner_intake(other, TENANT, DRAWING, encode(intake, groups)) == routed


def test_combiner_intake_import_same_body_twice_writes_nothing(world, client):
    backend, g, intake, groups = world
    first = body_of(post(client, encode(intake, groups)))
    before = keys(backend)
    second = body_of(post(client, encode(intake, groups), query="?project_id=" + PROJECT))
    assert second == dict(first, created=False)
    assert keys(backend) == before and latest(backend) == 2


def test_combiner_intake_import_new_outlines_replace_the_old(world, client):
    backend, g, intake, groups = world
    body_of(post(client, encode(intake, groups)))
    changed = groups[::-1]
    result = body_of(post(client, encode(intake, changed)))
    assert (result["created"], result["version"], result["parent_version"]) == (True, 3, 2)
    assert result["combiner_intake_sha256"] == INTAKE_SHA and result["panel_groups_sha256"] != GROUPS_SHA
    kept = version_intake(backend, 3)
    assert kept["panel_groups"] == changed and kept["combiner_intake"] == intake
    assert head_graph(backend) == g


def test_combiner_intake_import_after_placement_is_refused(world, client):
    backend, g, intake, groups = world
    body_of(post(client, encode(intake, groups)))
    dispatch(backend, request(), source_version=2)
    response = post(client, encode(intake, groups))
    assert (response.status_code, reason(response)) == (409, "COMBINER_EXISTING_L1")
    assert latest(backend) == 3


def test_combiner_intake_import_under_a_checkout(world, client):
    backend, g, intake, groups = world
    with held(backend):
        response = post(client, encode(intake, groups))
        assert (response.status_code, reason(response)) == (403, "COMBINER_IMPORT_CHECKOUT_DENIED")
        assert latest(backend) == 1


def test_combiner_intake_import_gate_receives_tenant_context(world, client, monkeypatch):
    import deps
    from routers import drawings
    backend, g, intake, groups = world
    tenant = deps.TenantContext(TENANT, tier="demo", subject="auth0|combiner-holder")
    client.app.dependency_overrides[deps.require_active_tenant] = lambda: tenant
    seen = []
    def record(drawing_id, context, selected_backend, capability):
        seen.append(context)
        return None, None
    monkeypatch.setattr(drawings, "_lock_authorization", record)
    assert post(client, encode(intake, groups)).status_code == 200
    assert seen == [tenant] and seen[0] is tenant
    assert isinstance(seen[0], deps.TenantContext) and seen[0].subject == "auth0|combiner-holder"


# ----------------------------------------------------------------- refusals --

def _intake_patch(change):
    def apply(intake, groups):
        intake = copy.deepcopy(intake)
        change(intake)
        return encode(intake, groups)
    return apply


def _set(path, value):
    def change(intake):
        node = intake
        for key in path[:-1]:
            node = node[key]
        node[path[-1]] = value
    return change


BODIES = [
    ("empty", lambda i, g: b"", 400, "COMBINER_IMPORT_EMPTY"),
    ("not-utf8", lambda i, g: b"\xff{}", 400, "COMBINER_IMPORT_ENCODING_INVALID"),
    ("bom", lambda i, g: b"\xef\xbb\xbf" + encode(i, g), 400, "COMBINER_IMPORT_ENCODING_INVALID"),
    ("not-json", lambda i, g: b"{", 400, "COMBINER_IMPORT_JSON_INVALID"),
    ("nan", lambda i, g: b'{"combiner_intake": {"x": NaN}, "panel_groups": []}', 400,
     "COMBINER_IMPORT_JSON_INVALID"),
    ("deep", lambda i, g: b"[" * 100000 + b"]" * 100000, 400, "COMBINER_IMPORT_JSON_INVALID"),
    ("list", lambda i, g: b"[]", 400, "COMBINER_IMPORT_BODY_INVALID"),
    ("extra-key", lambda i, g: encode(i, g, hardware={}), 400, "COMBINER_IMPORT_BODY_INVALID"),
    ("no-groups", lambda i, g: json.dumps({"combiner_intake": i}).encode(), 400, "COMBINER_IMPORT_BODY_INVALID"),
    ("intake-list", lambda i, g: encode([], g), 400, "COMBINER_IMPORT_BODY_INVALID"),
    ("groups-object", lambda i, g: encode(i, {"A": []}), 400, "COMBINER_IMPORT_BODY_INVALID"),
    ("group-extra", lambda i, g: encode(i, [{"handle": "A", "outlines": [], "name": "x"}]), 400,
     "COMBINER_IMPORT_BODY_INVALID"),
    ("handle-empty", lambda i, g: encode(i, [{"handle": "", "outlines": []}]), 400, "COMBINER_IMPORT_BODY_INVALID"),
    ("handle-65", lambda i, g: encode(i, [{"handle": "h" * 65, "outlines": []}]), 400,
     "COMBINER_IMPORT_BODY_INVALID"),
    ("outlines-null", lambda i, g: encode(i, [{"handle": "A", "outlines": None}]), 400,
     "COMBINER_IMPORT_BODY_INVALID"),
    ("lone-surrogate", lambda i, g: encode(i, [{"handle": "\ud800", "outlines": []}]), 400,
     "COMBINER_IMPORT_BODY_INVALID"),
    ("outline-point", lambda i, g: encode(i, [{"handle": "A", "outlines": [[[0, "x"]]]}]), 400,
     "COMBINER_OUTLINES_INVALID"),
    ("groups-empty", lambda i, g: encode(i, []), 400, "COMBINER_OUTLINES_INVALID"),
    ("schema-wrong", _intake_patch(_set(["schema"], "wrong.v99")), 400, "COMBINER_INTAKE_INVALID"),
    ("schema-missing", _intake_patch(lambda i: i.pop("schema")), 400, "COMBINER_INTAKE_INVALID"),
    ("format", _intake_patch(_set(["format"], "combiner-intake-v2")), 400, "COMBINER_INTAKE_INVALID"),
    ("existing-l1", _intake_patch(_set(["inputs", "existingL1s"], [{"number": 1}])), 409, "COMBINER_EXISTING_L1"),
    ("units", _intake_patch(_set(["drawing", "metersPerUnit"], 0.3048)), 409, "COMBINER_INTAKE_UNITS_MISMATCH"),
    ("context", _intake_patch(_set(["commandContext", "l2NumMppt"], 5)), 409, "COMBINER_INTAKE_CONTEXT_MISMATCH"),
    ("l2-moved", _intake_patch(lambda i: i["inputs"]["l2Inverters"][0]["InsertPt"].__setitem__(
        "X", i["inputs"]["l2Inverters"][0]["InsertPt"]["X"] + 100)), 409, "COMBINER_INTAKE_L2_MISMATCH"),
    ("string-dropped", _intake_patch(lambda i: i["inputs"]["preBuiltStrings"].pop()), 409,
     "COMBINER_INTAKE_STRING_MISMATCH"),
]


@pytest.mark.parametrize("case,make,status,code", BODIES, ids=[case for case, _, _, _ in BODIES])
def test_combiner_intake_import_bodies_fail_closed(world, client, case, make, status, code):
    backend, g, intake, groups = world
    before = keys(backend)
    response = post(client, make(intake, groups))
    assert (response.status_code, reason(response)) == (status, code)
    assert response.json()["error"]["retryable"] is False
    assert keys(backend) == before and latest(backend) == 1
    assert store.load_manifest(backend, TENANT, DRAWING)["head"] == 1


def _l2_mode_off(backend, g, intake, groups, tmp_path, monkeypatch):
    w1 = graph.__wrapped__()
    assert w1["settings"].get("use_l2_collectors") is not True
    return stored(sub(tmp_path, "off"), monkeypatch, w1, {})


def _graphless(backend, g, intake, groups, tmp_path, monkeypatch):
    return seed_graphless(sub(tmp_path, "graphless"), monkeypatch)[0]


def _not_local(backend, g, intake, groups, tmp_path, monkeypatch):
    real = cii.resolve_graph_context
    monkeypatch.setattr(cii, "resolve_graph_context",
                        lambda *a, **k: dict(real(*a, **k), representation="dwg-bundle"))
    return backend


def _corrupt(backend, g, intake, groups, tmp_path, monkeypatch):
    real = store.resolve_version_entry

    def entry(*args, **kwargs):
        resolved, key, value = real(*args, **kwargs)
        return resolved, key, dict(value, sha256="0" * 64)
    monkeypatch.setattr(store, "resolve_version_entry", entry)
    return backend


def _too_large(backend, g, intake, groups, tmp_path, monkeypatch):
    monkeypatch.setattr(sdg, "MAX_NODES", 170_000)
    return backend


def _drained(backend, g, intake, groups, tmp_path, monkeypatch):
    monkeypatch.setenv("LEAF_DRAWING_MUTATIONS_ENABLED", "0")
    return backend


def _store_down(backend, g, intake, groups, tmp_path, monkeypatch):
    from routers import drawings

    def down(*args, **kwargs):
        raise RuntimeError("store down")
    monkeypatch.setattr(drawings, "_backend", down)
    return backend


def _write_raises(error):
    def setup(backend, g, intake, groups, tmp_path, monkeypatch):
        def put(*args, **kwargs):
            raise error
        monkeypatch.setattr(write_loop, "_put_bytes_version", put)
        return backend
    return setup


DRAWINGS = [
    ("drawing-id", "", "/api/drawings/Bad!/imports/combiner-intake", None, 400, "COMBINER_IMPORT_DRAWING_ID_INVALID"),
    ("project-long", "?project_id=" + "p" * 101, URL, None, 400, "COMBINER_IMPORT_PROJECT_ID_INVALID"),
    ("project-empty", "?project_id=", URL, None, 400, "COMBINER_IMPORT_PROJECT_ID_INVALID"),
    ("project-other", "?project_id=leaf:project:other", URL, None, 409, "COMBINER_IMPORT_PROJECT_MISMATCH"),
    ("no-drawing", "", "/api/drawings/nosuch/imports/combiner-intake", None, 404,
     "COMBINER_IMPORT_DRAWING_NOT_FOUND"),
    ("l2-mode-off", "", URL, _l2_mode_off, 409, "COMBINER_L2_MODE_REQUIRED"),
    ("graphless", "", URL, _graphless, 409, "COMBINER_IMPORT_GRAPH_REQUIRED"),
    ("not-local", "", URL, _not_local, 409, "COMBINER_IMPORT_GRAPH_NOT_LOCAL"),
    ("corrupt", "", URL, _corrupt, 500, "COMBINER_IMPORT_SOURCE_CORRUPT"),
    ("intake-too-large", "", URL, _too_large, 413, "COMBINER_IMPORT_INTAKE_TOO_LARGE"),
    ("drained", "", URL, _drained, 503, "COMBINER_IMPORT_WRITES_DRAINED"),
    ("store-down", "", URL, _store_down, 503, "COMBINER_IMPORT_STORE_UNAVAILABLE"),
    ("head-moved", "", URL, _write_raises(ValueError("stale parent: expected 1")), 409, "COMBINER_IMPORT_HEAD_MOVED"),
    ("pg-head-moved", "", URL, _write_raises(ValueError("stale drawing head: expected 1")), 409,
     "COMBINER_IMPORT_HEAD_MOVED"),
    ("write-refused", "", URL, _write_raises(ValueError("bad version")), 500, "COMBINER_IMPORT_WRITE_REFUSED"),
    ("checkout-denied", "", URL, _write_raises(store.CheckoutDenied("held")), 403, "COMBINER_IMPORT_CHECKOUT_DENIED"),
    ("store-oserror", "", URL, _write_raises(OSError("disk")), 503, "COMBINER_IMPORT_STORE_UNAVAILABLE"),
]


@pytest.mark.parametrize("case,query,url,setup,status,code", DRAWINGS, ids=[row[0] for row in DRAWINGS])
def test_combiner_intake_import_drawings_fail_closed(world, client, tmp_path, monkeypatch, case, query, url, setup,
                                                     status, code):
    backend, g, intake, groups = world
    if setup is not None:
        target = setup(backend, g, intake, groups, tmp_path, monkeypatch)
        monkeypatch.setattr(write_loop, "backend_for_tenant", lambda *a, **k: target)
        backend = target
    before = keys(backend)
    response = post(client, encode(intake, groups), query=query, url=url)
    assert (response.status_code, reason(response)) == (status, code)
    assert response.json()["error"]["retryable"] is (code in RETRYABLE)
    assert keys(backend) == before


@pytest.mark.parametrize("media,status", [
    ("text/plain", 415), (None, 415), ("application/xml", 415), ("Application/JSON; charset=utf-8", 200),
], ids=["text", "none", "xml", "json-charset"])
def test_combiner_intake_import_media_types(world, client, media, status):
    backend, g, intake, groups = world
    response = post(client, encode(intake, groups), media=media)
    assert response.status_code == status
    if status == 415:
        assert reason(response) == "COMBINER_IMPORT_MEDIA_TYPE_REFUSED" and latest(backend) == 1
    else:
        assert sha(body_of(response)) == FIRST_RESULT_SHA


def test_combiner_intake_import_unlisted_code_is_import_failed(world, client, monkeypatch):
    backend, g, intake, groups = world

    def unlisted(*args, **kwargs):
        raise cii.CombinerIntakeImportError("COMBINER_IMPORT_SOMETHING_NEW")
    monkeypatch.setattr(cii, "import_combiner_intake", unlisted)
    response = post(client, encode(intake, groups))
    assert (response.status_code, reason(response), response.json()["error"]["retryable"]) == (
        500, "COMBINER_IMPORT_FAILED", False)


def test_combiner_intake_import_module_bounds(world, monkeypatch):
    backend, g, intake, groups = world
    for data, code in ((None, "COMBINER_IMPORT_EMPTY"), ("{}", "COMBINER_IMPORT_EMPTY")):
        with pytest.raises(cii.CombinerIntakeImportError) as error:
            cii.import_combiner_intake(backend, TENANT, DRAWING, data)
        assert error.value.code == code
    monkeypatch.setattr(cii, "MAX_IMPORT_BYTES", 10)
    with pytest.raises(cii.CombinerIntakeImportError) as error:
        cii.import_combiner_intake(backend, TENANT, DRAWING, b"{" * 11)
    assert error.value.code == "COMBINER_IMPORT_TOO_LARGE"
    monkeypatch.setattr(cii, "MAX_IMPORT_BYTES", 16_777_216)
    monkeypatch.setattr(cii, "MAX_GROUPS", 10)
    with pytest.raises(cii.CombinerIntakeImportError) as error:
        cii.import_combiner_intake(backend, TENANT, DRAWING, encode(intake, groups))
    assert error.value.code == "COMBINER_IMPORT_BODY_INVALID"
    for project in (1, ""):
        with pytest.raises(cii.CombinerIntakeImportError) as error:
            cii.import_combiner_intake(backend, TENANT, DRAWING, encode(intake, groups), project_id=project)
        assert error.value.code == "COMBINER_IMPORT_PROJECT_ID_INVALID"
    assert latest(backend) == 1


def test_combiner_intake_import_never_mutates_its_inputs(world):
    backend, g, intake, groups = world
    snapshot = copy.deepcopy((intake, groups))
    data = encode(intake, groups)
    calls = []
    result = cii.import_combiner_intake(backend, TENANT, DRAWING, data,
                                        authorize=lambda: calls.append(1) or (None, None))
    assert (intake, groups) == snapshot and calls == [1] and result["created"] is True


# ------------------------------------------------------- bounds before work --

def test_combiner_intake_import_huge_content_length(world, client, monkeypatch):
    backend, g, intake, groups = world

    def parsed(*args):
        raise AssertionError("parsed past the declared length")
    monkeypatch.setattr(cii, "_parse", parsed)
    for length in ("9" * 4301, str(cii.MAX_IMPORT_BYTES + 1)):
        status, body, pulls = asgi(client, [("json", b"{}")], [
            (b"content-type", b"application/json"), (b"content-length", length.encode())])
        assert (status, body["error"]["reason_code"], pulls) == (413, "COMBINER_IMPORT_TOO_LARGE", [])
    assert latest(backend) == 1


def test_combiner_intake_import_zero_padded_content_length(world, client):
    backend, g, intake, groups = world
    data = encode(intake, groups)
    status, body, _ = asgi(client, [("json", data)], [
        (b"content-type", b"application/json"), (b"content-length", ("0" * 4300 + str(len(data))).encode())])
    assert status == 200
    assert sha({key: value for key, value in body.items() if key not in ENVELOPE}) == FIRST_RESULT_SHA


def test_combiner_intake_import_stream_cutoff(world, client, monkeypatch):
    backend, g, intake, groups = world
    data = encode(intake, groups)

    def parsed(*args):
        raise AssertionError("parsed past the cap")
    monkeypatch.setattr(cii, "MAX_IMPORT_BYTES", 150)
    monkeypatch.setattr(cii, "_parse", parsed)
    status, body, pulls = asgi(client, [("a", data[:100]), ("b", data[100:200]), ("c", data[200:])],
                               [(b"content-type", b"application/json"), (b"transfer-encoding", b"chunked")])
    assert (status, body["error"]["reason_code"], pulls) == (413, "COMBINER_IMPORT_TOO_LARGE", ["a", "b"])
    assert latest(backend) == 1


@pytest.mark.parametrize("url,drained,media,status,code", [
    ("/api/drawings/Bad!/imports/combiner-intake", False, b"application/json", 400,
     "COMBINER_IMPORT_DRAWING_ID_INVALID"),
    (URL, True, b"application/json", 503, "COMBINER_IMPORT_WRITES_DRAINED"),
    (URL, False, b"text/plain", 415, "COMBINER_IMPORT_MEDIA_TYPE_REFUSED"),
], ids=["drawing-id", "drained", "media"])
def test_combiner_intake_import_cheap_refusals_read_no_body(world, client, monkeypatch, url, drained, media,
                                                            status, code):
    if drained:
        monkeypatch.setenv("LEAF_DRAWING_MUTATIONS_ENABLED", "0")
    pulls = []
    messages = []
    pending = [("json", b"{}")]

    async def receive():
        name, chunk = pending.pop(0) if pending else ("end", b"")
        pulls.append(name)
        return {"type": "http.request", "body": chunk, "more_body": False}

    async def send(message):
        messages.append(message)

    scope = {"type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1", "method": "POST",
             "scheme": "http", "path": url, "raw_path": url.encode(), "query_string": b"", "root_path": "",
             "headers": [(b"x-tenant-id", TENANT.encode()), (b"content-type", media)],
             "client": ("testclient", 50000), "server": ("testserver", 80)}
    asyncio.run(client.app(scope, receive, send))
    result = next(m for m in messages if m["type"] == "http.response.start")["status"]
    body = json.loads(b"".join(m.get("body", b"") for m in messages if m["type"] == "http.response.body"))
    assert (result, body["error"]["reason_code"], pulls) == (status, code, [])


# ------------------------------------------------------------ who may import --

@pytest.mark.parametrize("unavailable", [False, True])
def test_combiner_intake_import_entitlement(world, client, monkeypatch, unavailable):
    import entitlements
    backend, g, intake, groups = world

    def policy(*args):
        if unavailable:
            raise entitlements.EntitlementsError("x")
        return {"upload": False}
    monkeypatch.setattr(entitlements, "entitlements_for", policy)
    response = post(client, encode(intake, groups))
    assert response.status_code == (503 if unavailable else 403)
    if not unavailable:
        assert response.json()["entitlement_required"] is True
        assert response.json()["required"] == "upload"
    assert latest(backend) == 1


def test_combiner_intake_import_guest_is_refused(world, client, monkeypatch):
    import time
    import guest_uploads
    from routers import drawings
    backend, g, intake, groups = world
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
    response = client.post(URL, content=encode(intake, groups), headers={
        "Content-Type": "application/json", "X-Guest-Session": token})
    assert response.status_code == 403 and "upload-only" in response.text
    assert accesses == []


def test_combiner_intake_import_foreign_tenant(world, client):
    backend, g, intake, groups = world
    response = post(client, encode(intake, groups), tenant="other-tenant")
    assert (response.status_code, reason(response)) == (404, "COMBINER_IMPORT_DRAWING_NOT_FOUND")
    assert latest(backend) == 1
    assert not backend.drawing_object_keys("other-tenant", DRAWING)
