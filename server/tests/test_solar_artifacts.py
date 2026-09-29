"""Immutable Solar files, read proofs and authenticated downloads."""
import json
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

import solar_artifacts as artifacts
import solar_local_read as local
import solar_tools
import solar_xlsx
import write_loop
import store
from leaf_cloud_client import canonical_bytes
from solar_design_graph import GraphValidationError
from solar_graph_context import resolve_graph_context
from solar_sizing_client import digest
from test_w1_design_graph import graph  # noqa: F401
from test_w1_solve_commit import seed
from test_w1_local_graph_adapter import held, run as commit
from test_w1_local_graph_broker import rails  # noqa: F401
from test_w1_local_graph_jobs import isolated_jobs, no_network  # noqa: F401
from test_solar_local_read import RESULT_KEYS, SELECTED

TENANT = "fixture-tenant"
TOOL = "solar-select-by-zone"
JOB = "read-job"
PARAMS = {"drawing_id": "solar", "zone_name": "Roof"}
REQUEST_SHA = "df0bc999488ca7eaffc5192d55b11f175876fd4124f0fd64bbfe50ac91402b03"
ATTIC_SHA = "bf05c2df7567575614fb4b2b3911e64774ee596a7d16d797a6273cf0285c1227"
GRAPH_SHA = "67ab25efcda14a69da65a4184cf5d362ac19a85dcf266f722a9528f5881737c3"
CSV = b"panel,string\r\nP1,S1\r\nP2,S1\r\n"
CONTENT_SHA = "053a14f2d6a54515fb70d9e23b9d898a09d790fcdae956baad48b9ace060058a"
A1 = "e5e3bbd5333d18dc6225d58bafef88afb4720ef89958fc84d2eecc3373dcaf6d"
OUTPUT_SHA = "9af68954326573de3bf832e62f8e173798b08991607499baae0be10850464a17"
VALUE = artifacts.ArtifactOutput({"rows": 2}, "text/csv", "strings.csv", CSV)
R1 = {"schema": "leaf.solar-artifact-ref.v1", "artifact_id": A1,
      "media_type": "text/csv", "filename": "strings.csv", "byte_length": 28,
      "content_sha256": CONTENT_SHA, "source_version": 1,
      "download": "/api/drawings/solar/artifacts/" + A1}
PREFIX = "tenants/fixture-tenant/drawings/solar/artifacts/"
META_KEY = PREFIX + A1 + ".json"
BLOB_KEY = PREFIX + "blobs/" + CONTENT_SHA + ".bin"


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


def sink(backend, request_sha=REQUEST_SHA, version=1, tenant=TENANT, verify=False):
    context = resolve_graph_context(backend, TENANT, "solar", version)
    return artifacts.ArtifactSink(backend, tenant, "solar", context, TOOL, request_sha, verify)


def save(backend, value=VALUE, **kwargs):
    target = sink(backend, **kwargs)
    prepared = target.prepare(value)
    return target.finish(prepared)


def keys(backend):
    return set(backend.drawing_object_keys(TENANT, "solar"))


def read(backend, **kwargs):
    return artifacts.read_artifact(backend, TENANT, "solar", A1, **kwargs)


def builtin(monkeypatch, value=VALUE):
    monkeypatch.setattr(local, "_load_builtin", lambda tool: SimpleNamespace(run=lambda *a: value))


def run(backend):
    return local.run_local_graph_read(backend, TENANT, TOOL, PARAMS, drawing_id="solar",
                                      source_version=1, job_id=JOB)


def proof(backend, result):
    return local.graph_read_provenance(result, PARAMS, TENANT, JOB, TOOL, 1, backend=backend)


def advance(backend):
    with held(backend) as fence:
        commit(backend, fence=fence)


def workbook():
    return solar_xlsx.write_workbook([
        ("Harness", [(1, ["Harness", "Length (m)"]), (2, ["H1", 45]), (3, ["H2", 45.5])])])


def test_artifacts_store_pinned_contract(backend):
    before = keys(backend)
    context = resolve_graph_context(backend, TENANT, "solar", 1)
    binding = artifacts.artifact_binding(TENANT, "solar", context, TOOL, REQUEST_SHA)
    assert len(binding) == 8 and len(canonical_bytes(binding)) == 373
    assert digest(binding) == A1
    assert save(backend) == R1
    assert keys(backend) - before == {META_KEY, BLOB_KEY}
    meta, content = read(backend)
    assert len(meta) == 13 and len(canonical_bytes(meta)) == 596
    assert digest(meta) == "dfd42f28de8c50c0d93b39ce05ba9380f77e70a1f8dd719555a15630dd44ac77"
    assert content == CSV
    assert store.load_manifest(backend, TENANT, "solar")["head"] == 1


def test_artifacts_duplicate(backend):
    first = save(backend)
    before = keys(backend)
    assert save(backend) == first
    assert keys(backend) == before


def test_artifacts_distinct_request(backend):
    save(backend)
    second = save(backend, request_sha=ATTIC_SHA)
    assert second["artifact_id"] == "cff4704c1c30a6b441c5b98f6c5e1654343dc9c3eaa6cbc48751937e8e34d404"
    assert artifacts.read_artifact(backend, TENANT, "solar", second["artifact_id"])[1] == CSV
    assert read(backend)[1] == CSV


@pytest.mark.parametrize("copied", [False, True])
def test_artifacts_tenant_scope(backend, copied):
    save(backend)
    if copied:
        prefix = store.drawing_prefix("other-tenant", "solar") + "/artifacts/"
        backend.put(prefix + A1 + ".json", backend.get(META_KEY))
        backend.put(prefix + "blobs/" + CONTENT_SHA + ".bin", CSV)
    with pytest.raises(GraphValidationError, match="ARTIFACT_NOT_FOUND"):
        artifacts.read_artifact(backend, "other-tenant", "solar", A1)
    prepared = sink(backend, tenant="other-tenant").prepare(VALUE)
    assert prepared.ref["artifact_id"] == "a4106870aeb342dfd4c4007dbe7731aeded911a42064c1e0752332a7f7cf5f52"


def test_artifacts_revision_binding(backend):
    save(backend)
    advance(backend)
    with pytest.raises(GraphValidationError, match="ARTIFACT_STALE"):
        read(backend, require_head=True)
    assert read(backend)[1] == CSV
    ref = save(backend, version=2)
    context = resolve_graph_context(backend, TENANT, "solar", 2)
    assert ref["artifact_id"] == digest(artifacts.artifact_binding(TENANT, "solar", context, TOOL, REQUEST_SHA))
    assert ref["artifact_id"] != A1
    meta, content = artifacts.read_artifact(backend, TENANT, "solar", ref["artifact_id"], require_head=True)
    assert meta["source_version"] == 2 and content == CSV


@pytest.mark.parametrize("size,limit,accepted", [(16777217, 16777216, False), (64, 64, True), (65, 64, False)])
def test_artifacts_size_limit(backend, monkeypatch, size, limit, accepted):
    assert artifacts.MAX_ARTIFACT_BYTES == 16777216
    monkeypatch.setattr(artifacts, "MAX_ARTIFACT_BYTES", limit)
    value = replace(VALUE, content=b"x" * size)
    if accepted:
        assert save(backend, value)["byte_length"] == size
    else:
        with pytest.raises(GraphValidationError, match="ARTIFACT_TOO_LARGE"):
            save(backend, value)
        assert not any("/artifacts/" in key for key in keys(backend))


@pytest.mark.parametrize("changes,code", [
    ({"media_type": "text/html"}, "ARTIFACT_MEDIA_TYPE_REFUSED"),
    ({"media_type": "application/pdf"}, "ARTIFACT_MEDIA_TYPE_REFUSED"),
    *[({"filename": name}, "ARTIFACT_FILENAME_INVALID") for name in
      ["strings.xlsx", "../x.csv", ".csv", "a b.csv", "strings.CSV", "a" * 121 + ".csv", "a..b.csv", 7]],
    ({"content": b""}, "ARTIFACT_INVALID"),
    ({"content": bytearray(CSV)}, "ARTIFACT_INVALID"),
    ({"summary": []}, "ARTIFACT_INVALID"),
])
def test_artifacts_shape_refusals(backend, changes, code):
    with pytest.raises(GraphValidationError, match=code):
        save(backend, replace(VALUE, **changes))
    assert not any("/artifacts/" in key for key in keys(backend))


@pytest.mark.parametrize("media,filename,content", [
    ("text/csv", "x.csv", b"\xff\xfe"),
    ("application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", "x.xlsx", CSV),
    ("application/json", "x.json", b"{"),
    ("application/json", "x.json", b"NaN"),
    ("application/json", "x.json", b"1e309"),
    ("application/json", "x.json", b"-1e309"),
    ("application/vnd.google-earth.kml+xml", "x.kml", b"hello"),
])
def test_artifacts_content_refused(backend, media, filename, content):
    with pytest.raises(GraphValidationError, match="ARTIFACT_CONTENT_MISMATCH"):
        save(backend, artifacts.ArtifactOutput({}, media, filename, content))
    assert not any("/artifacts/" in key for key in keys(backend))


@pytest.mark.parametrize("extension", ["xlsx", "json", "kml", "xml"])
def test_artifacts_content_accepted(backend, extension):
    data = {"json": b'{"a":1}', "kml": b'<?xml version="1.0"?><kml/>',
            "xml": b"\xef\xbb\xbf  <LandXML/>"}
    content = workbook() if extension == "xlsx" else data[extension]
    media = next(media for media, ext in artifacts.MEDIA_TYPES.items() if ext == extension)
    ref = save(backend, artifacts.ArtifactOutput({}, media, "file." + extension, content))
    assert ref["byte_length"] == len(content)
    assert read(backend)[1] == content


def test_artifacts_conflict_keeps_first(backend):
    save(backend)
    with pytest.raises(GraphValidationError, match="ARTIFACT_CONFLICT"):
        save(backend, replace(VALUE, content=CSV + b"P3,S2\r\n"))
    assert read(backend)[1] == CSV


@pytest.mark.parametrize("mutation", ["content", "metadata", "missing"])
def test_artifacts_corruption(backend, mutation):
    save(backend)
    if mutation == "content":
        backend.put(BLOB_KEY, b"other")
    elif mutation == "metadata":
        backend.put(META_KEY, json.dumps(json.loads(backend.get(META_KEY)), indent=1).encode())
    else:
        Path(backend.root, BLOB_KEY).unlink()
    with pytest.raises(GraphValidationError, match="ARTIFACT_CORRUPT"):
        read(backend)


@pytest.mark.parametrize("artifact_id,code", [(A1.upper(), "ARTIFACT_ID_INVALID"),
    ("a" * 63, "ARTIFACT_ID_INVALID"), ("0" * 64, "ARTIFACT_NOT_FOUND")])
def test_artifacts_id_refusals(backend, artifact_id, code):
    with pytest.raises(GraphValidationError, match=code):
        artifacts.read_artifact(backend, TENANT, "solar", artifact_id)


def test_artifacts_drain(backend, monkeypatch):
    monkeypatch.setenv("LEAF_DRAWING_MUTATIONS_ENABLED", "0")
    with pytest.raises(GraphValidationError, match="ARTIFACT_WRITES_DRAINED"):
        save(backend)
    assert not any("/artifacts/" in key for key in keys(backend))


def test_artifacts_retention(backend):
    before = keys(backend)
    save(backend)
    assert all(key.startswith(store.drawing_prefix(TENANT, "solar") + "/artifacts/")
               for key in keys(backend) - before)


def test_artifacts_read_result_and_duplicate(backend, monkeypatch):
    builtin(monkeypatch)
    result = run(backend)
    assert set(result) == RESULT_KEYS
    assert result["output"] == {"summary": {"rows": 2}, "artifact": R1}
    assert result["output_bytes"] == 431 and result["output_sha256"] == OUTPUT_SHA
    assert result["request_sha256"] == REQUEST_SHA and result["graph_sha256"] == GRAPH_SHA
    assert result["drawing_changed"] is False
    assert store.load_manifest(backend, TENANT, "solar")["head"] == 1
    before = keys(backend)
    assert run(backend) == result and keys(backend) == before


@pytest.mark.parametrize("mutation", ["none", "content", "metadata"])
def test_artifacts_read_proof(backend, monkeypatch, mutation):
    builtin(monkeypatch)
    result = run(backend)
    if mutation == "content":
        backend.put(BLOB_KEY, b"tampered")
    elif mutation == "metadata":
        Path(backend.root, META_KEY).unlink()
    if mutation == "none":
        value = proof(backend, result)
        assert value == {"execution_mode": "local_graph_read", "adapter": "local-graph-read",
                         "request_sha256": REQUEST_SHA, "graph_sha256": GRAPH_SHA,
                         "source_version": 1, "output_sha256": OUTPUT_SHA}
    else:
        with pytest.raises(ValueError, match="^graph read terminal proof rejected$"):
            proof(backend, result)


@pytest.mark.parametrize("mutation,code", [("oversize", "ARTIFACT_TOO_LARGE"),
    ("media", "ARTIFACT_MEDIA_TYPE_REFUSED"), ("summary", "READ_OUTPUT_LIMIT_EXCEEDED")])
def test_artifacts_read_refusals_write_nothing(backend, monkeypatch, mutation, code):
    changes = {"oversize": {"content": b"x" * 16777217}, "media": {"media_type": "text/html"},
               "summary": {"summary": {"blob": "x" * 1048576}}}
    builtin(monkeypatch, replace(VALUE, **changes[mutation]))
    with pytest.raises(GraphValidationError, match=code):
        run(backend)
    assert not any("/artifacts/" in key for key in keys(backend))


def test_artifacts_dict_unchanged(backend):
    assert run(backend)["output"] == SELECTED
    assert not any("/artifacts/" in key for key in keys(backend))


@pytest.mark.parametrize("stored", [False, True])
@pytest.mark.parametrize("output", [
    {"summary": {"rows": 2}, "artifact": R1},
    {"summary": {"rows": 2}, "report": R1},
    {"artifact": None},
    R1,
])
def test_artifacts_plain_dict_reference_reserved(backend, monkeypatch, stored, output):
    if stored:
        save(backend)
    before = {key: backend.get(key) for key in keys(backend)}
    builtin(monkeypatch, output)
    with pytest.raises(GraphValidationError) as error:
        local.run_local_graph_read(backend, TENANT, TOOL,
                                  dict(PARAMS, zone_name="Attic"), drawing_id="solar",
                                  source_version=1, job_id=JOB)
    assert error.value.code == "ARTIFACT_REFERENCE_RESERVED"
    assert {key: backend.get(key) for key in keys(backend)} == before


@pytest.mark.parametrize("reference_key", ["artifact", "report"])
@pytest.mark.parametrize("mutation", ["request", "metadata", "content"])
def test_artifacts_proof_checks_reference_independently(backend, monkeypatch, reference_key, mutation):
    builtin(monkeypatch)
    result = run(backend)
    params = dict(PARAMS)
    if reference_key != "artifact":
        result["output"][reference_key] = result["output"].pop("artifact")
    if mutation == "request":
        params["zone_name"] = "Attic"
        result["request_sha256"] = ATTIC_SHA
    else:
        Path(backend.root, META_KEY if mutation == "metadata" else BLOB_KEY).unlink()
    data = canonical_bytes(result["output"])
    result["output_bytes"] = len(data)
    result["output_sha256"] = digest(result["output"])
    # Bypass execution checks so this row exercises the terminal reference guard alone.
    monkeypatch.setattr(local, "_read_output", lambda *args: (result["output"], data))
    with pytest.raises(ValueError, match="^graph read terminal proof rejected$"):
        local.graph_read_provenance(result, params, TENANT, JOB, TOOL, 1, backend=backend)


def test_artifacts_no_sink(graph, monkeypatch):
    builtin(monkeypatch)
    with pytest.raises(GraphValidationError, match="READ_OUTPUT_INVALID"):
        local._read_output(TOOL, graph, PARAMS)


def test_artifacts_broker(rails, monkeypatch):
    builtin(monkeypatch)
    broker = rails[0]
    response = broker._broker_run(broker.BrokerRunRequest(
        tenant_id=TENANT, tool=solar_tools.trusted_record(TOOL), params=PARAMS,
        dwg="solar", dwg_version=1, job_id=JOB, aps_live=False,
        checkout_holder=None, checkout_fence=None))
    assert response.status_code == 200
    assert json.loads(response.body)["result"]["output"]["artifact"]["artifact_id"] == A1
    assert read(rails[1])[1] == CSV


def test_artifacts_route_download_and_etag(backend, client, monkeypatch):
    save(backend)
    monkeypatch.setenv("LEAF_DRAWING_MUTATIONS_ENABLED", "0")
    response = client.get(R1["download"], headers={"X-Tenant-Id": TENANT})
    assert response.status_code == 200 and response.content == CSV
    assert response.headers["content-type"].startswith("text/csv")
    expected = {"content-disposition": 'attachment; filename="strings.csv"',
                "etag": '"' + CONTENT_SHA + '"', "x-leaf-artifact-id": A1,
                "x-leaf-source-version": "1", "cache-control": "private, no-cache",
                "x-content-type-options": "nosniff"}
    for key, value in expected.items():
        assert response.headers[key] == value
    cached = client.get(R1["download"], headers={"X-Tenant-Id": TENANT, "If-None-Match": expected["etag"]})
    assert cached.status_code == 304 and cached.content == b""
    for key, value in expected.items():
        assert cached.headers[key] == value


@pytest.mark.parametrize("mutation,status,code", [
    ("tenant", 404, "ARTIFACT_NOT_FOUND"), ("id", 400, "ARTIFACT_ID_INVALID"),
    ("drawing", 400, "ARTIFACT_ID_INVALID"), ("stale", 409, "ARTIFACT_STALE"),
    ("content", 500, "ARTIFACT_CORRUPT"),
])
def test_artifacts_route_refusals(backend, client, mutation, status, code):
    save(backend)
    tenant, url = TENANT, R1["download"]
    if mutation == "tenant":
        tenant = "other-tenant"
    elif mutation == "id":
        url = url.replace(A1, "E" * 64)
    elif mutation == "drawing":
        url = url.replace("/solar/", "/Bad!/")
    elif mutation == "stale":
        advance(backend)
        url += "?current=1"
    else:
        backend.put(BLOB_KEY, b"tampered")
    response = client.get(url, headers={"X-Tenant-Id": tenant})
    assert response.status_code == status
    assert response.json()["error"]["reason_code"] == code
    assert response.json()["error"]["retryable"] is False


def test_artifacts_route_xlsx(backend, client):
    media = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
    content = workbook()
    ref = save(backend, artifacts.ArtifactOutput({}, media, "harness.xlsx", content))
    response = client.get(ref["download"], headers={"X-Tenant-Id": TENANT})
    assert response.status_code == 200 and response.headers["content-type"] == media
    assert response.content == content and len(response.content) == ref["byte_length"]


@pytest.mark.parametrize("field,value", [
    ("tenant_id", "Bad!"), ("drawing_id", "../solar"), ("project_id", ""),
    ("project_id", "x" * 101), ("resolved_version", True), ("resolved_version", 0),
    ("resolved_version", 100000000), ("graph_sha256", "A" * 64),
    ("request_sha256", "a" * 63), ("tool", "BadTool"),
])
def test_artifacts_binding_refusals(backend, field, value):
    context = resolve_graph_context(backend, TENANT, "solar", 1)
    args = dict(tenant_id=TENANT, drawing_id="solar", context=context, tool=TOOL,
                request_sha256=REQUEST_SHA)
    if field in context:
        context[field] = value
    else:
        args[field] = value
    with pytest.raises(GraphValidationError, match="ARTIFACT_BINDING_INVALID"):
        artifacts.artifact_binding(**args)
    assert not any("/artifacts/" in key for key in keys(backend))


@pytest.mark.parametrize("mutation", ["schema", "extra", "version", "length", "digest", "large", "at_bound", "constant"])
def test_artifacts_metadata_bounds(backend, monkeypatch, mutation):
    save(backend)
    meta, _ = read(backend)
    if mutation == "schema":
        meta["schema"] = "unknown"
    elif mutation == "extra":
        meta["extra"] = 1
    elif mutation == "version":
        meta["source_version"] = True
    elif mutation == "length":
        meta["byte_length"] = True
    elif mutation == "digest":
        meta["content_sha256"] = "A" * 64
    raw = canonical_bytes(meta)
    constants = []
    if mutation in ("large", "at_bound"):
        assert artifacts.MAX_META_BYTES == 4096
        size = 4097 if mutation == "large" else 4096
        raw = raw[:1] + b" " * (size - len(raw)) + raw[1:]
        assert len(raw) == size and json.loads(raw) == meta
        # Isolate the byte bound from the independent canonical encoding guard.
        # All fields and the binding remain valid; only JSON whitespace is padded.
        monkeypatch.setattr(artifacts, "canonical_bytes", lambda value: raw)
    elif mutation == "constant":
        raw = raw.replace(b'"byte_length":28', b'"byte_length":NaN')
        assert set(json.loads(raw)) == set(meta)
        rejected = []
        reject_constant = artifacts._constant
        def record_constant(value):
            constants.append(value)
            try:
                return reject_constant(value)
            except ValueError:
                rejected.append(value)
                raise
        monkeypatch.setattr(artifacts, "_constant", record_constant)
    backend.put(META_KEY, raw)
    if mutation == "at_bound":
        assert read(backend) == (meta, CSV)
        return
    with pytest.raises(GraphValidationError, match="ARTIFACT_CORRUPT") as caught:
        read(backend)
    if mutation == "constant":
        assert constants == ["NaN"]
        assert rejected == ["NaN"]
        assert isinstance(caught.value.__context__, ValueError)
        assert str(caught.value.__context__) == "nonfinite JSON constant"


@pytest.mark.parametrize("error", [OSError, RuntimeError, ValueError])
def test_artifacts_store_unavailable(backend, monkeypatch, error):
    def fail(*args):
        raise error("unavailable")
    monkeypatch.setattr(backend, "put_if_absent_or_verify", fail)
    with pytest.raises(GraphValidationError, match="ARTIFACT_STORE_UNAVAILABLE"):
        save(backend)


@pytest.mark.parametrize("stage", ["backend", "metadata", "content"])
def test_artifacts_route_unavailable(backend, client, monkeypatch, stage):
    from routers import drawings
    save(backend)
    original = backend.get
    def fail(*args, **kwargs):
        raise OSError("unavailable")
    if stage == "backend":
        monkeypatch.setattr(drawings, "_backend", fail)
    else:
        target = META_KEY if stage == "metadata" else BLOB_KEY
        def get(key):
            return fail() if key == target else original(key)
        monkeypatch.setattr(backend, "get", get)
    response = client.get(R1["download"], headers={"X-Tenant-Id": TENANT})
    assert response.status_code == 503
    assert response.json()["error"]["reason_code"] == "ARTIFACT_STORE_UNAVAILABLE"
    assert response.json()["error"]["retryable"] is True


def test_artifacts_verify_mismatch(backend):
    save(backend)
    target = sink(backend, verify=True)
    prepared = target.prepare(replace(VALUE, content=CSV + b"P3,S2\r\n"))
    with pytest.raises(GraphValidationError, match="ARTIFACT_VERIFY_MISMATCH"):
        target.finish(prepared)
    assert read(backend)[1] == CSV


def test_artifacts_sink_constructor_is_lazy():
    target = artifacts.ArtifactSink({}, None, None, {}, None, None, False)
    assert target.backend == {} and target.context == {}
