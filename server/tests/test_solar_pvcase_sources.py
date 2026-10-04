"""Frozen immutable G33 admission, route order and scoped source readback."""
import asyncio
import copy
import hashlib
import json
import time
from pathlib import Path

import pytest

import solar_artifacts as artifacts
import solar_pvcase_conversion as conversion
import solar_pvcase_sources as sources
import store
import write_loop
from envelopes import ErrorCode, install_error_handlers
from leaf_cloud_client import canonical_bytes
from solar_design_graph import GraphValidationError
from solar_graph_context import resolve_graph_context
from solar_sizing_client import digest
from test_w1_design_graph import graph  # noqa: F401
from test_w1_solve_commit import seed, seed_graphless
from test_w1_local_graph_adapter import held, run as commit

ROOT = Path(__file__).resolve().parents[2]
F = (ROOT / "docs/parity/evidence/rooftop/pvcase/intake.json").read_bytes()
B = b'{"schema":"leaf.pvcase-g33.v1","intake":' + F + b'}'
H = "a40b8c102a862cc7e49b9dce7c74e5419f370e7f85fd7ba3d4bd420232f07874"
A = "238cb1123f00177a6684136a2d5e58cb18e16f44c2c3f60f7857ae1dbfba6ed4"
TENANT = "fixture-tenant"
PROJECT = "leaf:project:00000000-0000-4000-8000-000000000001"
GRAPH_SHA = "67ab25efcda14a69da65a4184cf5d362ac19a85dcf266f722a9528f5881737c3"
URL = "/api/drawings/solar/imports/pvcase-g33"
PREFIX = "tenants/fixture-tenant/drawings/solar/"
META_KEY = PREFIX + "artifacts/" + A + ".json"
BLOB_KEY = PREFIX + "artifacts/blobs/" + H + ".bin"
REF = {"schema": "leaf.solar-artifact-ref.v1", "artifact_id": A,
       "media_type": "application/json", "filename": "pvcase-g33-source.json",
       "byte_length": 274972, "content_sha256": H, "source_version": 1,
       "download": "/api/drawings/solar/artifacts/" + A}
RESULT = {"schema": "leaf.pvcase-g33-source.v1", "kind": "pvcase-g33",
          "drawing_id": "solar", "project_id": PROJECT, "source_version": 1,
          "graph_rev": 0, "graph_sha256": GRAPH_SHA, "source": REF}
EXPECTED_STATUSES = {
    "PVS_DRAWING_ID_INVALID": 400, "PVS_PROJECT_ID_INVALID": 400,
    "PVS_MEDIA_TYPE_REFUSED": 415, "PVG_INVALID_JSON": 400,
    "PVG_INPUT_BYTES_EXCEEDED": 413, "PVG_ENVELOPE_FIELDS": 400,
    "PVG_ENVELOPE_SCHEMA": 400, "PVG_INVALID_INTAKE": 400,
    "PVG_LIST_LIMIT": 400, "PVG_DEPTH_LIMIT": 400, "PVG_NODE_LIMIT": 400,
    "PVS_DRAWING_NOT_FOUND": 404, "PVS_GRAPH_REQUIRED": 409,
    "PVS_PROJECT_MISMATCH": 409, "PVS_SOURCE_ID_INVALID": 400,
    "PVS_SOURCE_NOT_FOUND": 404, "PVS_SOURCE_KIND_MISMATCH": 409,
    "PVS_WRITES_DRAINED": 503, "PVS_STORE_UNAVAILABLE": 503,
    "PVS_SOURCE_CONFLICT": 500, "PVS_SOURCE_CORRUPT": 500,
    "PVS_SOURCE_INVALID": 500,
}


@pytest.fixture
def backend(graph, tmp_path, monkeypatch):
    value, _ = seed(tmp_path, monkeypatch, graph)
    monkeypatch.setattr(write_loop, "backend_for_tenant", lambda *a, **k: value)
    return value


@pytest.fixture
def client(backend, monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from routers import drawings
    monkeypatch.delenv("LEAF_AUTH_LIVE", raising=False)
    monkeypatch.delenv("APS_LIVE", raising=False)
    app = FastAPI()
    install_error_handlers(app)
    app.include_router(drawings.router)
    return TestClient(app, raise_server_exceptions=False)


def minimal():
    return {"schema": "leaf.pvcase-g33.v1",
            "intake": {"units": "m", "panels_per_string": 0, "panel_groups": []}}


def encode(value):
    return json.dumps(value, separators=(",", ":"), allow_nan=False).encode()


def save(backend, data=B, **kwargs):
    return sources.import_pvcase_source(backend, TENANT, "solar", data, **kwargs)


def load(backend, artifact_id=A, project_id=PROJECT):
    return sources.load_pvcase_source(backend, TENANT, "solar", artifact_id, project_id=project_id)


def written(backend):
    return {key for key in backend.drawing_object_keys(TENANT, "solar")
            if "/artifacts/" in key or "/imports/" in key}


def snapshot(backend):
    return {key: backend.get(key) for key in backend.drawing_object_keys(TENANT, "solar")
            if "/artifacts/" not in key}


def post(client, data=B, url=URL, media="application/json", **headers):
    headers = {"X-Tenant-Id": TENANT, **headers}
    if media is not None:
        headers["Content-Type"] = media
    return client.post(url, content=data, headers=headers)


def refusal(response, code):
    status, error, retryable = sources.REFUSALS[code]
    assert response.status_code == status
    body = response.json()
    assert body["error"]["reason_code"] == code
    assert body["error"]["message"] == code
    assert body["error"]["error_code"] == error
    assert body["error"]["retryable"] is retryable


def raises(code, operation):
    with pytest.raises(sources.PvcaseSourceError) as caught:
        operation()
    assert caught.value.code == code
    assert str(caught.value) == code
    assert caught.value.__cause__ is None


def trap(*args, **kwargs):
    raise AssertionError("unexpected body, validator or backend access")


def asgi(client, chunks, *, path=URL, query=b"", extra_headers=(), media=b"application/json"):
    pulls, messages = [], []
    iterator = iter(chunks)

    async def receive():
        try:
            chunk = next(iterator)
        except StopIteration:
            return {"type": "http.request", "body": b"", "more_body": False}
        pulls.append(chunk)
        return {"type": "http.request", "body": chunk, "more_body": True}

    async def send(message):
        messages.append(message)

    scope = {"type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1",
             "method": "POST", "scheme": "http", "path": path, "raw_path": path.encode(),
             "query_string": query, "root_path": "",
             "headers": [(b"x-tenant-id", TENANT.encode()), (b"content-type", media),
                         *extra_headers],
             "client": ("testclient", 50000), "server": ("testserver", 80)}
    asyncio.run(client.app(scope, receive, send))
    status = next(m for m in messages if m["type"] == "http.response.start")["status"]
    body = json.loads(b"".join(m.get("body", b"") for m in messages
                              if m["type"] == "http.response.body"))
    return status, body, pulls


def artifact(backend, content=B, *, tool=sources.SOURCE_TOOL, request_hash=None,
             media=sources.SOURCE_MEDIA_TYPE, filename=sources.SOURCE_FILENAME):
    context = resolve_graph_context(backend, TENANT, "solar", "head")
    sink = artifacts.ArtifactSink(backend, TENANT, "solar", context, tool,
                                 request_hash or hashlib.sha256(content).hexdigest(), False)
    return sink.finish(sink.prepare(artifacts.ArtifactOutput({}, media, filename, content)))["artifact_id"]


def test_pvs_constants_and_refusals(backend, client, monkeypatch):
    from routers import drawings
    assert (sources.SOURCE_TOOL, sources.SOURCE_KIND, sources.SOURCE_MEDIA_TYPE,
            sources.SOURCE_FILENAME, sources.RESULT_SCHEMA) == (
        "solar-pvcase-g33-source", "pvcase-g33", "application/json",
        "pvcase-g33-source.json", "leaf.pvcase-g33-source.v1")
    assert sources.MAX_IMPORT_G33_BYTES == artifacts.MAX_ARTIFACT_BYTES == 16777216
    assert sources.MAX_PROJECT_ID_CHARS == 100
    expected = {code: (status, ErrorCode.BAD_PARAMS if status < 500 else ErrorCode.INTERNAL,
                       status == 503) for code, status in EXPECTED_STATUSES.items()}
    assert sources.REFUSALS == expected
    assert sources.SOURCE_CODES == frozenset(expected)
    assert sources.SHARED_CODES == frozenset({
        "UNAUTHENTICATED", "FORBIDDEN", "ENTITLEMENT_REQUIRED", "INTERNAL", "BAD_PARAMS"})
    assert sources.CODES == sources.SOURCE_CODES | sources.SHARED_CODES
    translations = {
        "ARTIFACT_WRITES_DRAINED": "PVS_WRITES_DRAINED",
        "ARTIFACT_STORE_UNAVAILABLE": "PVS_STORE_UNAVAILABLE",
        "ARTIFACT_CONFLICT": "PVS_SOURCE_CONFLICT",
        "ARTIFACT_NOT_FOUND": "PVS_SOURCE_NOT_FOUND",
        "ARTIFACT_ID_INVALID": "PVS_SOURCE_ID_INVALID",
        "ARTIFACT_CORRUPT": "PVS_SOURCE_CORRUPT",
        "ARTIFACT_VERIFY_MISMATCH": "PVS_SOURCE_CORRUPT",
        "UNKNOWN": "PVS_SOURCE_INVALID",
    }
    for artifact_code, source_code in translations.items():
        assert sources._artifact_refusal(artifact_code) == source_code
    for code in [*expected, "UNLISTED_PRIVATE_DETAIL"]:
        def fail(*args, **kwargs):
            error = sources.PvcaseSourceError(code)
            # Exercise the route's independent unknown-code collapse as well.
            error.code = code
            raise error
        monkeypatch.setattr(drawings, "_import_pvcase", fail)
        refusal(post(client), code if code in expected else "PVS_SOURCE_INVALID")
    assert not written(backend)


def test_pvs_capture_upload_and_download(backend, client):
    before = snapshot(backend)
    context = resolve_graph_context(backend, TENANT, "solar", "head")
    assert len(F) == 274931 and len(B) == 274972
    assert hashlib.sha256(B).hexdigest() == H
    assert digest(artifacts.artifact_binding(TENANT, "solar", context, sources.SOURCE_TOOL, H)) == A
    response = post(client)
    assert response.status_code == 200
    assert response.json() == dict(RESULT, error=None, degraded_mode=False)
    downloaded = client.get(REF["download"], headers={"X-Tenant-Id": TENANT})
    assert downloaded.status_code == 200 and downloaded.content == B
    assert snapshot(backend) == before
    assert resolve_graph_context(backend, TENANT, "solar", "head") == context
    assert written(backend) == {META_KEY, BLOB_KEY}


def test_pvs_duplicate_same_head(backend, client):
    first = post(client)
    assert first.status_code == 200
    before = written(backend)
    assert before == {META_KEY, BLOB_KEY}
    assert load(backend, first.json()["source"]["artifact_id"])[1] == B
    assert post(client).json() == first.json()
    assert written(backend) == before


def test_pvs_changed_bytes_keep_old_source(backend):
    first = save(backend)
    second = save(backend, B + b"\n")
    assert first["source"]["artifact_id"] != second["source"]["artifact_id"]
    assert second["source"]["content_sha256"] == hashlib.sha256(B + b"\n").hexdigest() != H
    assert load(backend)[1] == B
    assert load(backend, second["source"]["artifact_id"])[1] == B + b"\n"
    assert store.load_manifest(backend, TENANT, "solar")["head"] == 1


def test_pvs_same_bytes_later_head(backend):
    save(backend)
    old = load(backend)
    before = written(backend)
    with held(backend) as fence:
        commit(backend, fence=fence)
    second = save(backend)
    new_id = second["source"]["artifact_id"]
    assert new_id != A and second["source_version"] == 2
    assert second["source"]["content_sha256"] == H
    assert written(backend) - before == {PREFIX + "artifacts/" + new_id + ".json"}
    assert load(backend) == old


def test_pvs_graph_required(backend, client, tmp_path, monkeypatch):
    path = tmp_path / "graphless"
    path.mkdir()
    other, _ = seed_graphless(path, monkeypatch)
    with monkeypatch.context() as patch:
        patch.setattr(write_loop, "backend_for_tenant", lambda *a, **k: other)
        refusal(post(client), "PVS_GRAPH_REQUIRED")
        assert not written(other)
    assert post(client, encode(minimal())).status_code == 200


def test_pvs_foreign_scope(backend, client):
    before = snapshot(backend)
    refusal(post(client, **{"X-Tenant-Id": "other-tenant"}), "PVS_DRAWING_NOT_FOUND")
    refusal(post(client, url=URL.replace("/solar/", "/missing/")), "PVS_DRAWING_NOT_FOUND")
    assert not written(backend)
    save(backend)
    for tenant, drawing in (("other-tenant", "solar"), (TENANT, "other")):
        raises("PVS_SOURCE_NOT_FOUND", lambda: sources.load_pvcase_source(
            backend, tenant, drawing, A, project_id=PROJECT))
    assert snapshot(backend) == before


def test_pvs_project_scope(backend, client):
    for project in ("", "x" * 101):
        refusal(post(client, url=URL + "?project_id=" + project), "PVS_PROJECT_ID_INVALID")
        raises("PVS_PROJECT_ID_INVALID", lambda: load(backend, project_id=project))
    refusal(post(client, url=URL + "?project_id=other"), "PVS_PROJECT_MISMATCH")
    assert not written(backend)
    assert post(client, url=URL + "?project_id=" + PROJECT).status_code == 200
    raises("PVS_PROJECT_MISMATCH", lambda: load(backend, project_id="other"))
    assert load(backend)[1] == B


def test_pvs_strict_utf8(backend, client):
    text = encode(minimal()).decode()
    for data in (text.encode("utf-16"), text.encode("utf-32"), b"\xff",
                 b'{"schema":"\xed\xa0\x80","intake":{}}',
                 b'{"schema":"\\ud800","intake":{}}'):
        refusal(post(client, data), "PVG_INVALID_JSON")
    for value in (bytearray(B), B.decode(), None):
        raises("PVG_INVALID_JSON", lambda: sources.inspect_pvcase_source(value))
    assert not written(backend)


def test_pvs_json_refusals(backend, client):
    for data in (b"", b"{", b'{"schema":1,"schema":2}', b"NaN", b"Infinity",
                 b"-Infinity", b"1e999", b'{"schema":"leaf.pvcase-g33.v1","intake":NaN}'):
        refusal(post(client, data), "PVG_INVALID_JSON")
    assert not written(backend)


def test_pvs_envelope_refusals(backend, client):
    for value in ({}, {"schema": "leaf.pvcase-g33.v1"}, {**minimal(), "extra": 1}):
        refusal(post(client, encode(value)), "PVG_ENVELOPE_FIELDS")
    value = minimal()
    value["schema"] = "leaf.pvcase-g33.v2"
    refusal(post(client, encode(value)), "PVG_ENVELOPE_SCHEMA")
    from test_solar_pvcase_conversion import ground_envelope
    for mutate in (lambda e: e["intake"].update(extra=1),
                   lambda e: e["intake"].update(panels_per_string=True),
                   lambda e: e["intake"]["panel_groups"][0]["rows"][0][2].update(id="1")):
        value = ground_envelope()
        mutate(value)
        refusal(post(client, encode(value)), "PVG_INVALID_INTAKE")
    refusal(post(client, b'{"schema":"leaf.pvcase-g33.v1","intake":{}}'), "PVG_INVALID_INTAKE")
    assert not written(backend)


def test_pvs_validator_bounds_and_delegation(backend, client, monkeypatch):
    original = conversion.validate_envelope
    received = []
    def spy(data):
        received.append(data)
        return original(data)
    monkeypatch.setattr(conversion, "validate_envelope", spy)
    envelope = sources.inspect_pvcase_source(B)
    assert received == [B] and received[0] is B
    assert envelope == original(B)
    assert conversion.MAX_LIST == 100000 and conversion.MAX_DEPTH == 32
    assert conversion.MAX_NODES == 500000
    list_value = minimal()
    list_value["intake"]["panel_groups"] = [None] * 100001
    deep = None
    for _ in range(34):
        deep = [deep]
    node_value = minimal()
    node_value["intake"]["panel_groups"] = [[None] * 100000 for _ in range(6)]
    for value, code in ((list_value, "PVG_LIST_LIMIT"), (deep, "PVG_DEPTH_LIMIT"),
                        (node_value, "PVG_NODE_LIMIT")):
        refusal(post(client, encode(value)), code)
    for code in ("PVG_TARGET_NOT_EMPTY", "PRIVATE_CODE"):
        def unexpected(data):
            raise GraphValidationError(code)
        monkeypatch.setattr(conversion, "validate_envelope", unexpected)
        raises("PVS_SOURCE_INVALID", lambda: sources.inspect_pvcase_source(B))
    monkeypatch.setattr(conversion, "validate_envelope", trap)
    raises("PVS_SOURCE_INVALID", lambda: sources.inspect_pvcase_source(B))
    for envelope in ({"schema": "\ud800"}, {"\ud800": "value"},
                     {"intake": {"nested": ["\ud800"]}}):
        monkeypatch.setattr(conversion, "validate_envelope", lambda data: envelope)
        raises("PVG_INVALID_JSON", lambda: sources.inspect_pvcase_source(B))
    assert not written(backend)


def test_pvs_byte_ceiling(backend, client):
    data = encode(minimal())
    data += b" " * (16777216 - len(data))
    response = post(client, data)
    assert response.status_code == 200
    assert load(backend, response.json()["source"]["artifact_id"])[1] == data
    before = written(backend)
    refusal(post(client, data + b" "), "PVG_INPUT_BYTES_EXCEEDED")
    raises("PVG_INPUT_BYTES_EXCEEDED", lambda: sources.inspect_pvcase_source(data + b" "))
    assert written(backend) == before


def test_pvs_content_length(backend, client, monkeypatch):
    from routers import drawings
    with monkeypatch.context() as patch:
        patch.setattr(drawings, "_backend", trap)
        status, body, pulls = asgi(client, [B], extra_headers=[(b"content-length", b"9" * 4301)])
        assert status == 413 and body["error"]["reason_code"] == "PVG_INPUT_BYTES_EXCEEDED"
        assert pulls == []
    assert post(client, **{"Content-Length": "000000" + str(len(B))}).status_code == 200
    assert post(client, **{"Content-Length": "0" * 20 + str(len(B))}).status_code == 200
    before = written(backend)
    monkeypatch.setattr(sources, "MAX_IMPORT_G33_BYTES", 4)
    for length in (None, b"\xb2", b"-1", b"1.0", b" 1 "):
        headers = [] if length is None else [(b"content-length", length)]
        status, body, pulls = asgi(client, [b"1234", b"5", b"unused"], extra_headers=headers)
        assert status == 413 and body["error"]["reason_code"] == "PVG_INPUT_BYTES_EXCEEDED"
        assert pulls == [b"1234", b"5"]
    assert written(backend) == before


def test_pvs_stream_cutoff(backend, client, monkeypatch):
    from routers import drawings
    appended = []

    class Recording(bytearray):
        def extend(self, chunk):
            appended.append(bytes(chunk))
            super().extend(chunk)

    monkeypatch.setattr(drawings, "bytearray", Recording, raising=False)
    monkeypatch.setattr(sources, "MAX_IMPORT_G33_BYTES", 4)
    monkeypatch.setattr(sources, "inspect_pvcase_source", trap)
    monkeypatch.setattr(drawings, "_backend", trap)
    status, body, pulls = asgi(client, [b"1234", b"5", b"unused"])
    assert status == 413 and body["error"]["reason_code"] == "PVG_INPUT_BYTES_EXCEEDED"
    assert pulls == [b"1234", b"5"]
    assert appended == [b"1234"]
    assert not written(backend)


def test_pvs_early_check_order(backend, client, monkeypatch):
    import entitlements
    from routers import drawings
    with monkeypatch.context() as patch:
        patch.setattr(drawings, "_backend", trap)
        patch.setenv("LEAF_DRAWING_MUTATIONS_ENABLED", "0")
        patch.setattr(entitlements, "entitlements_for", lambda *a: {"upload": False})
        status, body, pulls = asgi(client, [B], path=URL.replace("/solar/", "/Bad!/"),
                                  query=b"project_id=", media=b"text/plain")
        assert status == 400 and body["error"]["reason_code"] == "PVS_DRAWING_ID_INVALID"
        assert pulls == []
        status, body, pulls = asgi(client, [B], query=b"project_id=", media=b"text/plain")
        assert status == 400 and body["error"]["reason_code"] == "PVS_PROJECT_ID_INVALID"
        assert pulls == []
        status, body, pulls = asgi(client, [B], media=b"text/plain")
        assert status == 403 and body["required"] == "upload" and pulls == []
        patch.setattr(entitlements, "entitlements_for", lambda *a: {"upload": True})
        status, body, pulls = asgi(client, [B], media=b"text/plain")
        assert status == 503 and body["error"]["reason_code"] == "PVS_WRITES_DRAINED"
        assert pulls == []
        patch.setenv("LEAF_DRAWING_MUTATIONS_ENABLED", "1")
        for media in (b"", b"text/plain"):
            status, body, pulls = asgi(client, [B], media=media)
            assert status == 415 and body["error"]["reason_code"] == "PVS_MEDIA_TYPE_REFUSED"
            assert pulls == []
    assert post(client, media=" Application/JSON ; charset=UTF-8").status_code == 200


def test_pvs_auth_and_entitlement(backend, client, monkeypatch):
    import entitlements
    import guest_uploads
    from routers import drawings
    monkeypatch.setattr(drawings, "_backend", trap)
    with monkeypatch.context() as patch:
        patch.setenv("LEAF_AUTH_LIVE", "1")
        response = client.post(URL, content=B, headers={"Content-Type": "application/json"})
        assert response.status_code == 401
        assert response.json()["error"]["error_code"] == "UNAUTHENTICATED"
        patch.setenv("LEAF_GUEST_SECRET", "test-secret-not-a-real-one")
        token = guest_uploads.mint_guest_session(
            guest_uploads.mint_guest_tenant_id(), int(time.time()) + 3600)
        assert token
        response = client.post(URL, content=B, headers={
            "Content-Type": "application/json", "X-Guest-Session": token})
        assert response.status_code == 403
        assert response.json()["error"]["error_code"] == "FORBIDDEN"
    monkeypatch.setattr(entitlements, "entitlements_for", lambda *a: {"upload": False})
    status, body, pulls = asgi(client, [B])
    assert status == 403 and body["entitlement_required"] is True
    assert body["required"] == "upload" and "reason_code" not in body["error"]
    assert pulls == []
    def unavailable(*args):
        raise entitlements.EntitlementsError("private")
    monkeypatch.setattr(entitlements, "entitlements_for", unavailable)
    status, body, pulls = asgi(client, [B])
    assert status == 503 and body["error"]["retryable"] is True
    assert "reason_code" not in body["error"] and pulls == []
    assert not written(backend)


def test_pvs_route_mount_and_checkout_parity(backend, client, monkeypatch):
    from routers import drawings
    from app import app
    from route_flatten import iter_leaf_routes
    path = "/api/drawings/{drawing_id}/imports/pvcase-g33"
    mounted = [r for mounted_path, r in iter_leaf_routes(app.routes)
               if mounted_path == path and "POST" in getattr(r, "methods", set())]
    assert len(mounted) == 1
    assert "post" in app.openapi()["paths"][path]
    assert len([r for p, r in iter_leaf_routes(client.app.routes)
                if p == path and "POST" in getattr(r, "methods", set())]) == 1
    assert "post" in client.app.openapi()["paths"][path]
    assert set(client.app.openapi()["paths"][path]) == {"post"}
    monkeypatch.setattr(drawings, "_lock_authorization", trap)
    assert post(client).status_code == 200


def test_pvs_storage_failures(backend, client, monkeypatch):
    from routers import drawings
    with monkeypatch.context() as patch:
        patch.setattr(drawings, "_backend", lambda *a: (_ for _ in ()).throw(RuntimeError("private")))
        refusal(post(client), "PVS_STORE_UNAVAILABLE")
    class Failed:
        def __init__(self, mode):
            self.mode = mode
        def __getattr__(self, name):
            return getattr(backend, name)
        def put_if_absent_or_verify(self, key, data):
            if self.mode == "conflict":
                raise store.ImmutableConflict("private")
            if self.mode == "all" or key.endswith(".json"):
                raise OSError("private")
            return backend.put_if_absent_or_verify(key, data)
    for mode, code in (("all", "PVS_STORE_UNAVAILABLE"), ("conflict", "PVS_SOURCE_CONFLICT")):
        raises(code, lambda: save(Failed(mode)))
        assert not written(backend)
    calls = []
    with monkeypatch.context() as patch:
        def fence():
            calls.append(True)
            return None if len(calls) == 1 else "closed"
        patch.setattr(write_loop, "drawing_mutations_refusal", fence)
        raises("PVS_WRITES_DRAINED", lambda: save(backend))
        assert not written(backend)
    raises("PVS_STORE_UNAVAILABLE", lambda: save(Failed("metadata")))
    assert written(backend) == {BLOB_KEY}
    assert save(backend) == RESULT
    assert written(backend) == {META_KEY, BLOB_KEY}


def test_pvs_loader_id_and_kind(backend, monkeypatch):
    raises("PVS_SOURCE_ID_INVALID", lambda: load(backend, "E" * 64))
    raises("PVS_SOURCE_NOT_FOUND", lambda: load(backend, "0" * 64))
    save(backend)
    meta, content = artifacts.read_artifact(backend, TENANT, "solar", A)
    for key, value in (("tool", "other-tool"), ("media_type", "text/csv"),
                       ("filename", "other.json")):
        changed = dict(meta, **{key: value})
        with monkeypatch.context() as patch:
            patch.setattr(artifacts, "read_artifact", lambda *a, **k: (changed, content))
            raises("PVS_SOURCE_KIND_MISMATCH", lambda: load(backend))
    foreign_id = artifact(backend, tool="other-tool")
    raises("PVS_SOURCE_KIND_MISMATCH", lambda: load(backend, foreign_id))
    other_filename = artifact(backend, filename="other.json", request_hash="b" * 64)
    raises("PVS_SOURCE_KIND_MISMATCH", lambda: load(backend, other_filename))


def test_pvs_loader_integrity(backend, monkeypatch):
    save(backend)
    original_meta = backend.get(META_KEY)
    backend.put(BLOB_KEY, B + b"\n")
    raises("PVS_SOURCE_CORRUPT", lambda: load(backend))
    backend.put(BLOB_KEY, B)
    for key, value, code in (("byte_length", len(B) + 1, "PVS_SOURCE_CORRUPT"),
                             ("graph_sha256", "a" * 64, "PVS_SOURCE_CORRUPT"),
                             ("tenant_id", "foreign", "PVS_SOURCE_NOT_FOUND"),
                             ("drawing_id", "other", "PVS_SOURCE_NOT_FOUND")):
        meta = json.loads(original_meta)
        meta[key] = value
        backend.put(META_KEY, canonical_bytes(meta))
        raises(code, lambda: load(backend))
        backend.put(META_KEY, original_meta)
    other = artifact(backend, request_hash="a" * 64)
    raises("PVS_SOURCE_CORRUPT", lambda: load(backend, other))
    meta, content = artifacts.read_artifact(backend, TENANT, "solar", A)
    with monkeypatch.context() as patch:
        patch.setattr(artifacts, "read_artifact", lambda *a, **k: (dict(meta, tenant_id="foreign"), content))
        raises("PVS_SOURCE_NOT_FOUND", lambda: load(backend))


def test_pvs_loader_revalidates(backend, monkeypatch):
    invalid_id = artifact(backend, b'{"schema":"leaf.pvcase-g33.v2","intake":{}}')
    raises("PVS_SOURCE_CORRUPT", lambda: load(backend, invalid_id))
    save(backend)
    monkeypatch.setattr(sources, "resolve_graph_context", trap)
    meta, content, envelope = load(backend)
    assert meta["request_sha256"] == H and content == B
    assert envelope == conversion.validate_envelope(B)
    first = copy.deepcopy(envelope)
    envelope["intake"]["panel_groups"].clear()
    assert load(backend)[2] == first
