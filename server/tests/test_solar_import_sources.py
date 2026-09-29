"""Bounded SolarEdge source intake, immutable bindings and typed HTTP refusals."""
from pathlib import Path

import pytest

import solar_import_sources as sources
import solar_artifacts as artifacts
import write_loop
import store
from leaf_cloud_client import canonical_bytes
from solar_design_graph import GraphValidationError
from solar_graph_context import resolve_graph_context
from solar_sizing_client import digest
from test_w1_design_graph import graph  # noqa: F401
from test_w1_solve_commit import seed, seed_graphless
from test_w1_local_graph_adapter import held, run as commit

TENANT = "fixture-tenant"
PROJECT = "leaf:project:00000000-0000-4000-8000-000000000001"
GRAPH_SHA = "67ab25efcda14a69da65a4184cf5d362ac19a85dcf266f722a9528f5881737c3"
REPO_ROOT = Path(__file__).resolve().parents[2]
PDF = (REPO_ROOT / "data" / "solaredge_1to1_demo.pdf").read_bytes()
PDF_SHA = "2e8076086b8e494295e5523b3bb94325924b3196d069e62d2f517275d678a1c1"
SRC = "92756029f5a30788190c35aa652927610b707c1ada5bc3eb5ddca8188bea564c"
SOURCE_REF = {"schema": "leaf.solar-artifact-ref.v1", "artifact_id": SRC,
              "media_type": "application/pdf", "filename": "solaredge-source.pdf",
              "byte_length": 1019229, "content_sha256": PDF_SHA, "source_version": 1,
              "download": "/api/drawings/solar/artifacts/" + SRC}
PREFIX = "tenants/fixture-tenant/drawings/solar/"
META_KEY = PREFIX + "artifacts/" + SRC + ".json"
BLOB_KEY = PREFIX + "artifacts/blobs/" + PDF_SHA + ".bin"
SLOT_KEY = PREFIX + "imports/solaredge/slot-0.json"
SLOT = (b'{"content_sha256":"2e8076086b8e494295e5523b3bb94325924b3196d069e62d2f517275d678a1c1",'
        b'"kind":"solaredge-pdf","schema":"leaf.solar-import-slot.v1"}')
RESULT = {"schema": "leaf.solar-import-source.v1", "kind": "solaredge-pdf",
          "drawing_id": "solar", "project_id": PROJECT, "source_version": 1,
          "graph_sha256": GRAPH_SHA, "page_count": 1, "source": SOURCE_REF}
URL = "/api/drawings/solar/imports/solaredge-pdf"


def pdf(n):
    kids = " ".join(f"{3 + i} 0 R" for i in range(n))
    parts = [b"%PDF-1.4\n", b"1 0 obj<</Type/Catalog/Pages 2 0 R>>endobj\n",
             f"2 0 obj<</Type/Pages/Kids[{kids}]/Count {n}>>endobj\n".encode()]
    parts += [f"{3 + i} 0 obj<</Type/Page/Parent 2 0 R/MediaBox[0 0 10 10]>>endobj\n".encode()
              for i in range(n)]
    parts.append(b"trailer<</Root 1 0 R>>\n%%EOF\n")
    return b"".join(parts)


ENCRYPTED = (b"%PDF-1.4\n1 0 obj<</Type/Catalog/Pages 2 0 R>>endobj\n2 0 obj<</Type/Pages/Kids[3 0 R]/Count 1>>endobj\n"
             b"3 0 obj<</Type/Page/Parent 2 0 R/MediaBox[0 0 10 10]>>endobj\n4 0 obj<</Filter/Standard/V 1/R 2/O<"
             + b"00" * 32 + b">/U<" + b"00" * 32 + b">/P -4>>endobj\n"
             b"trailer<</Root 1 0 R/Encrypt 4 0 R/ID[<00112233445566778899aabbccddeeff><00112233445566778899aabbccddeeff>]>>\n%%EOF\n")


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


def written(backend):
    return {key for key in backend.drawing_object_keys(TENANT, "solar")
            if "/artifacts/" in key or "/imports/" in key}


def save(backend, data=PDF, **kwargs):
    return sources.import_solaredge_source(backend, TENANT, "solar", data, **kwargs)


def load(backend, artifact_id=SRC, project_id=PROJECT):
    return sources.load_import_source(backend, TENANT, "solar", artifact_id, project_id=project_id)


def post(client, content=PDF, url=URL, media="application/pdf"):
    headers = {"X-Tenant-Id": TENANT}
    if media is not None:
        headers["Content-Type"] = media
    return client.post(url, content=content, headers=headers)


def graphless(tmp_path, monkeypatch):
    path = tmp_path / "graphless"
    path.mkdir()
    backend, _ = seed_graphless(path, monkeypatch)
    monkeypatch.setattr(write_loop, "backend_for_tenant", lambda *a, **k: backend)
    return backend


def test_import_sources_route_huge_content_length(backend, client):
    response = client.post(URL, content=pdf(1), headers={
        "X-Tenant-Id": TENANT, "Content-Type": "application/pdf",
        "Content-Length": "9" * 4301})
    assert response.status_code == 413
    assert response.json()["error"]["reason_code"] == "IMPORT_PDF_TOO_LARGE"
    assert written(backend) == set()


def test_import_sources_route_zero_padded_content_length(backend, client):
    expected = post(client, pdf(1))
    assert expected.status_code == 200
    before = written(backend)
    response = client.post(URL, content=pdf(1), headers={
        "X-Tenant-Id": TENANT, "Content-Type": "application/pdf",
        "Content-Length": "0" * 4300 + str(len(pdf(1)))})
    assert response.status_code == 200
    assert response.json() == expected.json()
    assert written(backend) == before


@pytest.mark.parametrize("length", [b"\xb2", b"\xb3", b"\xb9"])
def test_import_sources_route_non_ascii_content_length(backend, client, length):
    import asyncio

    async def request_status(extra_headers):
        messages = []
        body = iter([pdf(1)])
        async def receive():
            return {"type": "http.request", "body": next(body), "more_body": False}
        async def send(message):
            messages.append(message)
        scope = {"type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1",
                 "method": "POST", "scheme": "http", "path": URL,
                 "raw_path": URL.encode(), "query_string": b"", "root_path": "",
                 "headers": [(b"x-tenant-id", TENANT.encode()),
                             (b"content-type", b"application/pdf")] + extra_headers,
                 "client": ("testclient", 50000), "server": ("testserver", 80)}
        await client.app(scope, receive, send)
        return next(m for m in messages if m["type"] == "http.response.start")["status"]

    expected = asyncio.run(request_status([]))
    assert expected == 200
    before = written(backend)
    assert asyncio.run(request_status([(b"content-length", length)])) == expected
    assert written(backend) == before


def test_import_sources_route_stream_cutoff(backend, client, monkeypatch):
    import asyncio
    import json

    monkeypatch.setattr(sources, "MAX_IMPORT_PDF_BYTES", 191)
    def inspected(*args):
        raise AssertionError("inspected past the cap")
    monkeypatch.setattr(sources, "inspect_solaredge_pdf", inspected)
    pulls = []
    def chunks():
        pulls.append("pdf")
        yield pdf(1)
        pulls.append("over")
        yield b"\n"
        pulls.append("past")
        yield b"unused"
    body = chunks()
    messages = []
    async def receive():
        try:
            chunk = next(body)
        except StopIteration:
            return {"type": "http.request", "body": b"", "more_body": False}
        return {"type": "http.request", "body": chunk, "more_body": True}
    async def send(message):
        messages.append(message)
    # TestClient coalesces generator bodies; feed ASGI chunks individually so
    # this pins the route's cutoff and the absence of any subsequent pull.
    scope = {"type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1",
             "method": "POST", "scheme": "http", "path": URL,
             "raw_path": URL.encode(), "query_string": b"", "root_path": "",
             "headers": [(b"x-tenant-id", TENANT.encode()),
                         (b"content-type", b"application/pdf"),
                         (b"transfer-encoding", b"chunked")],
             "client": ("testclient", 50000), "server": ("testserver", 80)}
    asyncio.run(client.app(scope, receive, send))
    assert next(m for m in messages if m["type"] == "http.response.start")["status"] == 413
    response = json.loads(b"".join(m.get("body", b"") for m in messages
                                   if m["type"] == "http.response.body"))
    assert response["error"]["reason_code"] == "IMPORT_PDF_TOO_LARGE"
    assert pulls == ["pdf", "over"]
    assert written(backend) == set()


def test_import_sources_encryption_property_guard(monkeypatch):
    import pdfminer.pdfdocument

    class EncryptedDocument:
        encryption = ("encrypted", {})
    monkeypatch.setattr(pdfminer.pdfdocument, "PDFDocument", lambda parser: EncryptedDocument())
    with pytest.raises(sources.ImportSourceError) as exc:
        sources.inspect_solaredge_pdf(pdf(1))
    assert exc.value.code == "IMPORT_PDF_ENCRYPTED"


@pytest.mark.parametrize("allow_all", [False, True])
def test_import_sources_route_live_guest(backend, client, monkeypatch, allow_all):
    import time
    from routers import drawings
    import deps
    import guest_uploads

    monkeypatch.setenv("LEAF_AUTH_LIVE", "1")
    monkeypatch.setenv("LEAF_GUEST_SECRET", "test-secret-not-a-real-one")
    token = guest_uploads.mint_guest_session(
        guest_uploads.mint_guest_tenant_id(), int(time.time()) + 3600)
    assert token
    # Drain stops the negative control after admission, before backend access.
    monkeypatch.setenv("LEAF_DRAWING_MUTATIONS_ENABLED", "0")
    if allow_all:
        monkeypatch.setattr(deps, "_guest_route_allowed", lambda *args: True)
    accesses = []
    def backend_forbidden(*args, **kwargs):
        accesses.append(True)
        raise AssertionError("guest reached backend")
    monkeypatch.setattr(drawings, "_backend", backend_forbidden)
    monkeypatch.setattr(write_loop, "backend_for_tenant", backend_forbidden)
    response = client.post(URL, content=pdf(1), headers={
        "Content-Type": "application/pdf", "X-Guest-Session": token})
    if allow_all:
        assert response.status_code == 503
        assert response.json()["error"]["reason_code"] == "IMPORT_WRITES_DRAINED"
        assert "upload-only" not in response.text
    else:
        assert response.status_code == 403
        assert "upload-only" in response.text
    assert accesses == []
    assert written(backend) == set()


def test_import_sources_route_foreign_tenant(backend, client):
    assert store.load_manifest(backend, TENANT, "solar")["head"] == 1
    response = client.post(URL, content=pdf(1), headers={
        "X-Tenant-Id": "other-tenant", "Content-Type": "application/pdf"})
    assert response.status_code == 404
    assert response.json()["error"]["reason_code"] == "IMPORT_DRAWING_NOT_FOUND"
    assert written(backend) == set()
    assert not {key for key in backend.drawing_object_keys("other-tenant", "solar")
                if "/artifacts/" in key or "/imports/" in key}


def test_import_sources_failed_blob_retry_reuses_slot(backend):
    class BlobUnavailable:
        def __getattr__(self, name):
            return getattr(backend, name)

        def put_if_absent_or_verify(self, key, data):
            if "/artifacts/" in key:
                raise OSError("blob unavailable")
            return backend.put_if_absent_or_verify(key, data)

    with pytest.raises(sources.ImportSourceError) as exc:
        save(BlobUnavailable(), pdf(1))
    assert exc.value.code == "IMPORT_STORE_UNAVAILABLE"
    assert written(backend) == {SLOT_KEY}
    reservation = backend.get(SLOT_KEY)
    result = save(backend, pdf(1))
    assert {key for key in written(backend) if "/imports/" in key} == {SLOT_KEY}
    assert backend.get(SLOT_KEY) == reservation
    assert len(written(backend)) == 3
    assert load(backend, result["source"]["artifact_id"])[1] == pdf(1)


def test_import_sources_constants():
    assert sources.MAX_IMPORT_PDF_BYTES == 16777216
    assert sources.MAX_IMPORT_PAGES == 50
    assert sources.MAX_SOURCES_PER_DRAWING == 8
    assert sources.SOURCE_TOOL == "solar-solaredge-pdf-source"
    assert artifacts.MEDIA_TYPES["application/pdf"] == "pdf"
    assert len(artifacts.MEDIA_TYPES) == 6


@pytest.mark.parametrize("data,pages", [(PDF, 1), (pdf(50), 50), (b"  \n" + pdf(1), 1)],
                         ids=["fixture", "fifty", "whitespace"])
def test_import_sources_inspect_accepted(data, pages):
    assert sources.inspect_solaredge_pdf(data) == pages


@pytest.mark.parametrize("data,code", [
    (b"", "IMPORT_PDF_EMPTY"), (bytearray(pdf(1)), "IMPORT_PDF_EMPTY"),
    ("%PDF-", "IMPORT_PDF_EMPTY"), (b"hello", "IMPORT_NOT_A_PDF"),
    (b"x" + pdf(1), "IMPORT_NOT_A_PDF"),
    (b"%PDF-1.4\n\x00\x01garbage", "IMPORT_PDF_MALFORMED"),
    (b"%PDF-1.4\ntrailer<<>>\n%%EOF\n", "IMPORT_PDF_MALFORMED"),
    (PDF[:4096], "IMPORT_PDF_MALFORMED"), (ENCRYPTED, "IMPORT_PDF_ENCRYPTED"),
    (pdf(0), "IMPORT_PDF_NO_PAGES"), (pdf(51), "IMPORT_PDF_TOO_MANY_PAGES"),
], ids=["empty", "bytearray", "str", "text", "prefix", "garbage", "root", "truncated",
        "encrypted", "zero", "fifty-one"])
def test_import_sources_inspect_refused(data, code):
    with pytest.raises(sources.ImportSourceError) as exc:
        sources.inspect_solaredge_pdf(data)
    assert exc.value.code == code


@pytest.mark.parametrize("case", ["exact", "one-over", "real-limit"])
def test_import_sources_size_bound(monkeypatch, case):
    if case == "real-limit":
        data = b"%PDF-" + b"0" * 16777212
    else:
        monkeypatch.setattr(sources, "MAX_IMPORT_PDF_BYTES", 191)
        data = pdf(1) + (b"\n" if case == "one-over" else b"")
    if case == "exact":
        assert sources.inspect_solaredge_pdf(data) == 1
    else:
        with pytest.raises(sources.ImportSourceError) as exc:
            sources.inspect_solaredge_pdf(data)
        assert exc.value.code == "IMPORT_PDF_TOO_LARGE"


def test_import_sources_pinned_store_contract(backend):
    assert save(backend) == RESULT
    assert written(backend) == {META_KEY, BLOB_KEY, SLOT_KEY}
    assert backend.get(SLOT_KEY) == SLOT and len(SLOT) == 145
    context = resolve_graph_context(backend, TENANT, "solar", 1)
    binding = artifacts.artifact_binding(TENANT, "solar", context, sources.SOURCE_TOOL, PDF_SHA)
    assert len(binding) == 8 and len(canonical_bytes(binding)) == 379
    assert digest(binding) == SRC
    assert digest(artifacts.artifact_binding("other-tenant", "solar", context,
                                           sources.SOURCE_TOOL, PDF_SHA)) == (
        "dd4e22a8dc7204dcbfb5904d01ee7d542b378d638395f52be57f7dd246afa344")
    meta, content = artifacts.read_artifact(backend, TENANT, "solar", SRC)
    assert len(meta) == 13 and len(canonical_bytes(meta)) == 623
    assert digest(meta) == "0d709d0405d41beba463a4b9d23962fc796fab5d9ba91bb9bc99b63465b27f23"
    assert digest(SOURCE_REF) == "7353045ee558187c44631c3e5b787d01a997e5f8d4a0b7bd9fb92c1e3f09d1fe"
    assert content == PDF
    assert store.load_manifest(backend, TENANT, "solar")["head"] == 1


def test_import_sources_duplicate(backend):
    first = save(backend)
    before = written(backend)
    assert save(backend) == first
    assert written(backend) == before


def test_import_sources_new_revision(backend):
    save(backend)
    before = written(backend)
    with held(backend) as fence:
        commit(backend, fence=fence)
    second = save(backend)
    context = resolve_graph_context(backend, TENANT, "solar", 2)
    expected = digest(artifacts.artifact_binding(TENANT, "solar", context, sources.SOURCE_TOOL, PDF_SHA))
    assert second["source_version"] == 2
    assert second["source"]["artifact_id"] == expected and expected != SRC
    assert written(backend) - before == {PREFIX + "artifacts/" + expected + ".json"}
    assert load(backend)[1] == PDF


def test_import_sources_quota(backend):
    results = [save(backend, pdf(n)) for n in range(1, 9)]
    assert {key for key in written(backend) if "/imports/" in key} == {
        sources._slot_key(TENANT, "solar", n) for n in range(8)}
    before = written(backend)
    with pytest.raises(sources.ImportSourceError) as exc:
        save(backend, pdf(9))
    assert exc.value.code == "IMPORT_QUOTA_EXCEEDED"
    assert written(backend) == before
    assert save(backend, pdf(3)) == results[2]
    assert written(backend) == before


@pytest.mark.parametrize("occupied", [canonical_bytes({"schema": sources.SLOT_SCHEMA,
    "kind": sources.SOURCE_KIND, "content_sha256": "0" * 64}), b"junk"])
def test_import_sources_occupied_slot(backend, occupied):
    backend.put(SLOT_KEY, occupied)
    assert save(backend) == RESULT
    assert backend.get(SLOT_KEY) == occupied
    assert backend.get(sources._slot_key(TENANT, "solar", 1)) == SLOT


def test_import_sources_other_tenant(backend):
    save(backend)
    with pytest.raises(GraphValidationError) as exc:
        artifacts.read_artifact(backend, "other-tenant", "solar", SRC)
    assert exc.value.code == "ARTIFACT_NOT_FOUND"
    with pytest.raises(sources.ImportSourceError) as exc:
        sources.load_import_source(backend, "other-tenant", "solar", SRC, project_id=PROJECT)
    assert exc.value.code == "IMPORT_SOURCE_NOT_FOUND"


@pytest.mark.parametrize("case,code", [("graphless", "IMPORT_GRAPH_REQUIRED"),
    ("missing", "IMPORT_DRAWING_NOT_FOUND"), ("project", "IMPORT_PROJECT_MISMATCH"),
    ("long-project", "IMPORT_PROJECT_ID_INVALID")])
def test_import_sources_context_refusals(backend, tmp_path, monkeypatch, case, code):
    if case == "graphless":
        backend = graphless(tmp_path, monkeypatch)
    project = {"project": "leaf:project:other", "long-project": "p" * 101}.get(case)
    with pytest.raises(sources.ImportSourceError) as exc:
        sources.import_solaredge_source(backend, TENANT, "nosuch" if case == "missing" else "solar",
                                        PDF, project_id=project)
    assert exc.value.code == code
    assert written(backend) == set()


def test_import_sources_matching_project(backend):
    assert save(backend, project_id=PROJECT)["source"] == SOURCE_REF


def test_import_sources_drained(backend, monkeypatch):
    monkeypatch.setenv("LEAF_DRAWING_MUTATIONS_ENABLED", "0")
    with pytest.raises(sources.ImportSourceError) as exc:
        save(backend)
    assert exc.value.code == "IMPORT_WRITES_DRAINED"
    assert written(backend) == set()


@pytest.mark.parametrize("operation", ["put", "get"])
def test_import_sources_store_unavailable(backend, operation):
    class Unavailable:
        def __getattr__(self, name):
            return getattr(backend, name)

        def get(self, key):
            if operation == "get" and "/imports/" in key:
                raise OSError("unavailable")
            return backend.get(key)

        def put_if_absent_or_verify(self, key, data):
            if operation == "put":
                raise OSError("unavailable")
            return backend.put_if_absent_or_verify(key, data)

    with pytest.raises(sources.ImportSourceError) as exc:
        save(Unavailable())
    assert exc.value.code == "IMPORT_STORE_UNAVAILABLE"
    assert written(backend) == set()


@pytest.mark.parametrize("case,code", [("success", None), ("csv", "IMPORT_SOURCE_KIND_MISMATCH"),
    ("project", "IMPORT_PROJECT_MISMATCH"), ("missing", "IMPORT_SOURCE_NOT_FOUND"),
    ("id", "IMPORT_SOURCE_ID_INVALID"), ("corrupt", "IMPORT_SOURCE_CORRUPT")])
def test_import_sources_load(backend, case, code):
    save(backend)
    artifact_id, project = SRC, PROJECT
    if case == "csv":
        context = resolve_graph_context(backend, TENANT, "solar", 1)
        sink = artifacts.ArtifactSink(backend, TENANT, "solar", context, "solar-select-by-zone",
            "df0bc999488ca7eaffc5192d55b11f175876fd4124f0fd64bbfe50ac91402b03", False)
        value = artifacts.ArtifactOutput({"rows": 2}, "text/csv", "strings.csv",
                                         b"panel,string\r\nP1,S1\r\nP2,S1\r\n")
        artifact_id = sink.finish(sink.prepare(value))["artifact_id"]
    elif case == "project":
        project = "leaf:project:other"
    elif case == "missing":
        artifact_id = "0" * 64
    elif case == "id":
        artifact_id = "E" * 64
    elif case == "corrupt":
        backend.put(BLOB_KEY, pdf(1))
    if code is None:
        meta, content = load(backend)
        assert meta["tool"] == sources.SOURCE_TOOL and meta["request_sha256"] == PDF_SHA
        assert content == PDF
    else:
        with pytest.raises(sources.ImportSourceError) as exc:
            load(backend, artifact_id, project)
        assert exc.value.code == code


def test_import_sources_c14_source_retained(backend):
    from solar_solaredge_pdf import extract_primitives
    from solar_solaredge_parse import parse_primitives
    save(backend)
    _, content = load(backend)
    parsed, matrices = parse_primitives(extract_primitives(content), "solaredge_1to1_demo")
    assert len(matrices) == 14
    assert len(parsed.panels) == 3526


def test_import_sources_nothing_synthesized(backend):
    before = resolve_graph_context(backend, TENANT, "solar", "head")
    save(backend)
    after = resolve_graph_context(backend, TENANT, "solar", "head")
    assert after == before
    assert after["resolved_version"] == 1
    assert after["graph_sha256"] == "67ab25efcda14a69da65a4184cf5d362ac19a85dcf266f722a9528f5881737c3"


@pytest.mark.parametrize("media,filename,content,code", [
    ("application/pdf", "x.pdf", pdf(1), None),
    ("application/pdf", "x.pdf", b"hello", "ARTIFACT_CONTENT_MISMATCH"),
    ("application/pdf", "x.csv", pdf(1), "ARTIFACT_FILENAME_INVALID"),
    ("text/csv", "x.pdf", pdf(1), "ARTIFACT_FILENAME_INVALID"),
])
def test_import_sources_artifact_pdf_rules(backend, media, filename, content, code):
    context = resolve_graph_context(backend, TENANT, "solar", 1)
    sink = artifacts.ArtifactSink(backend, TENANT, "solar", context, sources.SOURCE_TOOL, PDF_SHA, False)
    value = artifacts.ArtifactOutput({}, media, filename, content)
    if code:
        with pytest.raises(GraphValidationError) as exc:
            sink.prepare(value)
        assert exc.value.code == code
        assert not written(backend)
    else:
        ref = sink.finish(sink.prepare(value))
        assert artifacts.read_artifact(backend, TENANT, "solar", ref["artifact_id"])[1] == content


def test_import_sources_route_import_and_download(backend, client):
    response = post(client)
    assert response.status_code == 200
    assert response.json() == dict(RESULT, error=None, degraded_mode=False)
    downloaded = client.get(SOURCE_REF["download"], headers={"X-Tenant-Id": TENANT})
    assert downloaded.status_code == 200 and downloaded.content == PDF
    assert downloaded.headers["content-type"] == "application/pdf"
    assert downloaded.headers["content-disposition"] == 'attachment; filename="solaredge-source.pdf"'
    assert downloaded.headers["etag"] == '"' + PDF_SHA + '"'
    assert downloaded.headers["x-content-type-options"] == "nosniff"


def test_import_sources_route_duplicate(backend, client):
    first = post(client)
    assert first.status_code == 200
    before = written(backend)
    second = post(client)
    assert second.status_code == 200 and second.json() == first.json()
    assert written(backend) == before


@pytest.mark.parametrize("case,status,code,retryable", [
    ("media", 415, "IMPORT_MEDIA_TYPE_REFUSED", False),
    ("no-media", 415, "IMPORT_MEDIA_TYPE_REFUSED", False),
    ("empty", 400, "IMPORT_PDF_EMPTY", False),
    ("text", 400, "IMPORT_NOT_A_PDF", False),
    ("large", 413, "IMPORT_PDF_TOO_LARGE", False),
    ("chunked", 413, "IMPORT_PDF_TOO_LARGE", False),
    ("drawing-id", 400, "IMPORT_DRAWING_ID_INVALID", False),
    ("drained", 503, "IMPORT_WRITES_DRAINED", True),
    ("quota", 429, "IMPORT_QUOTA_EXCEEDED", False),
    ("project", 409, "IMPORT_PROJECT_MISMATCH", False),
    ("graphless", 409, "IMPORT_GRAPH_REQUIRED", False),
    ("missing", 404, "IMPORT_DRAWING_NOT_FOUND", False),
    ("backend", 503, "IMPORT_STORE_UNAVAILABLE", True),
])
def test_import_sources_route_refusals(backend, client, tmp_path, monkeypatch, case, status, code, retryable):
    content, url, media = PDF, URL, "application/pdf"
    if case == "media":
        media = "text/plain"
    elif case == "no-media":
        media = None
    elif case == "empty":
        content = b""
    elif case == "text":
        content = b"hello"
    elif case in ("large", "chunked"):
        monkeypatch.setattr(sources, "MAX_IMPORT_PDF_BYTES", 191)
        content = iter([pdf(1), b"\n"]) if case == "chunked" else pdf(1) + b"\n"
    elif case == "drawing-id":
        url = URL.replace("/solar/", "/Bad!/")
    elif case == "drained":
        monkeypatch.setenv("LEAF_DRAWING_MUTATIONS_ENABLED", "0")
    elif case == "quota":
        for n in range(1, 9):
            save(backend, pdf(n))
    elif case == "project":
        url += "?project_id=leaf:project:other"
    elif case == "graphless":
        backend = graphless(tmp_path, monkeypatch)
    elif case == "missing":
        url = URL.replace("/solar/", "/nosuch/")
    elif case == "backend":
        from routers import drawings
        def unavailable(*args):
            raise RuntimeError("unavailable")
        monkeypatch.setattr(drawings, "_backend", unavailable)
    before = written(backend)
    response = post(client, content, url, media)
    assert response.status_code == status
    body = response.json()
    assert body["error"]["reason_code"] == code
    assert body["error"]["retryable"] is retryable
    if case == "quota":
        assert body["error"]["error_code"] == "quota_exceeded"
    assert written(backend) == before


@pytest.mark.parametrize("unavailable", [False, True])
def test_import_sources_route_entitlement(backend, client, monkeypatch, unavailable):
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
    assert written(backend) == set()
