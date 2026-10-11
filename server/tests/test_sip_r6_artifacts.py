"""Project artifact schemas, actual producers, immutable storage and verified downloads."""
from copy import deepcopy
from hashlib import sha256
from io import BytesIO
import json
from uuid import UUID
from xml.etree import ElementTree
from zipfile import ZipFile

import pytest
import solar_artifacts as standalone
import solar_project_artifacts as artifacts
import solar_project_context as project
import solar_project_read as read
from leaf_cloud_client import canonical_bytes
from solar_design_graph import GraphValidationError
import test_sip_r6_read as reads
import test_w1_design_graph as design_cases
import test_w1_equipment as equipment_cases
import test_w1_sizing_groups as sizing_cases
import test_solar_tool_electrical_schedules as schedules_cases


@pytest.fixture
def lane(monkeypatch):
    s = reads.setup(monkeypatch)
    immutable(s, monkeypatch)
    return s


def immutable(s, monkeypatch):
    def put(key, content):
        s.writes.append(key)
        if key in s.blobs and s.blobs[key] != content:
            raise standalone.store.ImmutableConflict("immutable output")
        s.blobs[key] = content
    monkeypatch.setattr(s, "put_if_absent_or_verify", put)


def string_source(s):
    ctx = reads.tools_cases.assigned_parent(s,
        equipment_cases.case.__wrapped__(design_cases.graph.__wrapped__()))
    ctx = reads.tools_cases.commit(s, "solar-homeruns", {"expected_rev": 1}, ctx)[2]
    s.ctx = reads.tools_cases.commit(s, "solar-schedule",
        {"expected_rev": 2, "insertion_point": [0, 0, 0]}, ctx)[2]
    return s.ctx


def global_source(s, monkeypatch):
    recorded = sizing_cases.passing.__wrapped__()
    sizing_cases.service.__wrapped__(monkeypatch, recorded)
    original = schedules_cases.build_i7(design_cases.graph.__wrapped__(), recorded)
    s.set_parent(reads.tools_cases.embedded(original))
    ctx = reads.graph_adapter.resolve_project_graph_context(s.context())
    s.ctx = reads.tools_cases.commit(s, "solar-settings", {"expected_rev": original["rev"],
        "project_changes": {"name": original["project"]["name"]}}, ctx)[2]
    return s.ctx


def make(s, *, tool=reads.CALC, value=None, request="c" * 64):
    binding = artifacts.project_artifact_binding(s.ctx, tool, reads.tools_cases.manifest(tool), request)
    sink = artifacts.ProjectArtifactSink(s, binding)
    value = value or standalone.ArtifactOutput({"status": "ok"}, "application/json", "Output.json", b'{"ok":true}')
    prepared = sink.prepare(value)
    sink.finish(prepared)
    return sink, prepared


def save(s, **kwargs):
    sink, prepared = make(s, **kwargs)
    sink.commit()
    return prepared


def download(s, artifact_id, **kwargs):
    return artifacts.read_project_artifact(s.tenant, s.project, s.drawing, artifact_id, **kwargs)


def metadata_key(prepared):
    return artifacts._key(prepared.meta, prepared.meta["artifact_id"])


def blob_key(prepared):
    return artifacts._key(prepared.meta, prepared.meta["content_sha256"], blob=True)


def test_sip_r6_artifact_uuid_binding(lane):
    sink, prepared = make(lane)
    assert set(sink.binding) == {"schema", "organization_id", "project_id", "drawing_id",
        "source_version_id", "source_intake_sha256", "graph_sha256", "tool", "tool_manifest_sha256", "request_sha256"}
    assert set(prepared.meta) == set(sink.binding) | {
        "artifact_id", "media_type", "filename", "byte_length", "content_sha256"}
    assert set(prepared.ref) == {"schema", "organization_id", "project_id", "drawing_id",
        "source_version_id", "source_intake_sha256", "graph_sha256", "artifact_id",
        "media_type", "filename", "byte_length", "content_sha256", "download"}
    binding_bytes = json.dumps(sink.binding, sort_keys=True, separators=(",", ":"),
                              ensure_ascii=False, allow_nan=False).encode("utf-8")
    assert prepared.meta["artifact_id"] == sha256(binding_bytes).hexdigest()
    for key in artifacts._IDS:
        assert str(UUID(prepared.ref[key])) == prepared.ref[key]
    assert prepared.ref["source_intake_sha256"] == lane.ctx.intake_sha256
    assert prepared.ref["graph_sha256"] == lane.ctx.graph_sha256
    assert prepared.meta["schema"] == artifacts.ARTIFACT_SCHEMA
    assert prepared.ref["schema"] == artifacts.REF_SCHEMA
    assert prepared.ref["download"] == (
        f"/api/projects/{lane.project}/drawings/{lane.drawing}/solar-artifacts/{prepared.meta['artifact_id']}")
    assert prepared.meta_bytes == canonical_bytes(prepared.meta)
    for field, value in (("source_version_id", "1"), ("source_version_id", "00000000-0000-0000-0000-000000ABCDEF"),
                         ("graph_sha256", "A" * 64), ("tool_manifest_sha256", "a" * 64)):
        binding = {**sink.binding, field: value}
        if value != sink.binding[field]:
            reads.refused("ARTIFACT_INVALID", lambda: artifacts.ProjectArtifactSink(lane, binding))


def test_sip_r6_artifact_producers(lane, monkeypatch):
    string_source(lane)
    tools = [("solar-string-data", "StringData.json", 973)]
    for tool, filename, count in tools:
        expected = reads.local._load_builtin(tool).run(deepcopy(lane.ctx.graph), {})
        result = reads.run(lane, tool, {})["result"]
        reference = result["output"]["artifact"]
        meta, content = download(lane, reference["artifact_id"])
        assert content == expected.content and meta["filename"] == filename and len(content) == count
        assert result["output"]["summary"] == expected.summary
    global_source(lane, monkeypatch)
    for tool, filename, count in (("solar-electrical-schedules", "ElectricalSchedules.json", 19514),
                                 ("solar-cable-export", "CableExport.xlsx", 34559)):
        expected = reads.local._load_builtin(tool).run(deepcopy(lane.ctx.graph), {})
        result = reads.run(lane, tool, {})["result"]
        meta, content = download(lane, result["output"]["artifact"]["artifact_id"], current=True)
        assert content == expected.content and meta["filename"] == filename and len(content) == count
        assert meta["media_type"] == expected.media_type
        assert result["output"]["summary"] == expected.summary
        if filename.endswith("xlsx"):
            with ZipFile(BytesIO(content)) as book:
                rows = [len(ElementTree.fromstring(book.read(f"xl/worksheets/sheet{i}.xml")).findall(
                    ".//{http://schemas.openxmlformats.org/spreadsheetml/2006/main}row")) for i in range(1, 5)]
            assert rows == [520, 37, 179, 1]


def test_sip_r6_artifact_immutable_storage(lane):
    before = list(lane.writes)
    sink, prepared = make(lane)
    assert lane.writes == before
    sink.commit()
    assert lane.writes[len(before):] == [blob_key(prepared), metadata_key(prepared)]
    saved = deepcopy(lane.blobs)
    sink.commit()
    assert lane.blobs == saved
    assert make(lane)[1].ref == prepared.ref
    changed = {**sink.binding, "source_version_id": str(UUID(int=9001))}
    other = artifacts.ProjectArtifactSink(lane, changed).prepare(
        standalone.ArtifactOutput({}, "application/json", "Output.json", b"{}"))
    assert other.meta["artifact_id"] != prepared.meta["artifact_id"]
    changed["source_intake_sha256"] = "d" * 64
    third = artifacts.ProjectArtifactSink(lane, changed).prepare(
        standalone.ArtifactOutput({}, "application/json", "Output.json", b"{}"))
    assert third.meta["artifact_id"] != other.meta["artifact_id"]
    conflict, _ = make(lane, value=standalone.ArtifactOutput({}, "application/json", "Output.json", b"{}"))
    reads.refused("ARTIFACT_CONFLICT", conflict.commit)
    assert lane.blobs[metadata_key(prepared)] == prepared.meta_bytes


def test_sip_r6_artifact_integrity(lane):
    prepared = save(lane)
    for field, value in (("schema", standalone.ARTIFACT_SCHEMA), ("byte_length", 99),
                         ("byte_length", True), ("content_sha256", "z" * 64),
                         ("source_intake_sha256", "d" * 64), ("source_version_id", "7"),
                         ("media_type", "text/plain"), ("filename", "../Output.json"),
                         ("project_id", str(UUID(int=999))), ("surprise", 1)):
        lane.blobs[metadata_key(prepared)] = canonical_bytes({**prepared.meta, field: value})
        reads.refused("ARTIFACT_CORRUPT", lambda: download(lane, prepared.meta["artifact_id"]))
    for raw in (prepared.meta_bytes + b" ", b'{"schema":1,"schema":2}', b"[]", b"{", b"x" * 4097):
        lane.blobs[metadata_key(prepared)] = raw
        reads.refused("ARTIFACT_CORRUPT", lambda: download(lane, prepared.meta["artifact_id"]))
    lane.blobs[metadata_key(prepared)] = prepared.meta_bytes
    for raw in (b"{}", prepared.content + b" ", b"x" * len(prepared.content)):
        lane.blobs[blob_key(prepared)] = raw
        reads.refused("ARTIFACT_CORRUPT", lambda: download(lane, prepared.meta["artifact_id"]))
    lane.blobs[blob_key(prepared)] = prepared.content
    assert download(lane, prepared.meta["artifact_id"])[1] == prepared.content


def test_sip_r6_artifact_historical_and_current(lane, monkeypatch):
    prepared = save(lane)
    assert download(lane, prepared.meta["artifact_id"], current=True)[1] == prepared.content
    lane.head = next(v.version_id for v in lane.versions.values() if v.seq == 2)
    assert download(lane, prepared.meta["artifact_id"])[1] == prepared.content
    reads.refused("ARTIFACT_STALE", lambda: download(lane, prepared.meta["artifact_id"], current=True))
    global_source(lane, monkeypatch)
    exported = reads.run(lane, "solar-cable-export", {})["result"]["output"]["artifact"]
    assert download(lane, exported["artifact_id"], current=True)[1][:4] == b"PK\x03\x04"
    with monkeypatch.context() as patch:
        def stale(graph):
            raise GraphValidationError("SOLAR_OUTPUT_NOT_CURRENT")
        patch.setattr(artifacts, "require_current_export", stale)
        reads.refused("SOLAR_OUTPUT_NOT_CURRENT", lambda: download(lane, exported["artifact_id"], current=True))
        assert download(lane, exported["artifact_id"])[1][:4] == b"PK\x03\x04"


def test_sip_r6_artifact_limits_and_failures(lane, monkeypatch):
    assert (artifacts.MAX_ARTIFACT_BYTES, artifacts.MAX_META_BYTES) == (16777216, 4096)
    for value, code in ((standalone.ArtifactOutput({}, "application/json", "../Bad.json", b"{}"),
                         "ARTIFACT_FILENAME_INVALID"),
                        (standalone.ArtifactOutput({}, "application/json", "Bad.json", b"bad"),
                         "ARTIFACT_CONTENT_MISMATCH"),
                        (standalone.ArtifactOutput({}, "text/csv", "Large.csv", b"a" * 16777217),
                         "ARTIFACT_TOO_LARGE")):
        reads.refused(code, lambda: make(lane, value=value))
    sink, prepared = make(lane, value=standalone.ArtifactOutput({}, "text/csv", "Limit.csv", b"a" * 16777216))
    assert len(prepared.content) == 16777216
    before = list(lane.writes)
    assert sink.finish(prepared) == prepared.ref and lane.writes == before
    with monkeypatch.context() as patch:
        patch.setattr(artifacts.write_loop, "drawing_mutations_refusal", lambda: {})
        reads.refused("ARTIFACT_WRITES_DRAINED", sink.commit)
    assert lane.writes == before
    reads.refused("ARTIFACT_NOT_FOUND", lambda: download(lane, "0" * 64))
    with monkeypatch.context() as patch:
        def unavailable(*args):
            raise OSError("private backend detail")
        patch.setattr(artifacts.write_loop, "upload_backend_for_tenant", unavailable)
        reads.refused("ARTIFACT_STORE_UNAVAILABLE", lambda: download(lane, "0" * 64))
    sink, prepared = make(lane, request="e" * 64)
    original = lane.put_if_absent_or_verify
    with monkeypatch.context() as patch:
        def metadata_failure(key, raw):
            if key.endswith(".json"):
                raise RuntimeError("private metadata detail")
            return original(key, raw)
        patch.setattr(lane, "put_if_absent_or_verify", metadata_failure)
        reads.refused("ARTIFACT_STORE_UNAVAILABLE", sink.commit)
    assert blob_key(prepared) in lane.blobs and metadata_key(prepared) not in lane.blobs
    reads.refused("ARTIFACT_NOT_FOUND", lambda: download(lane, prepared.meta["artifact_id"]))
    with monkeypatch.context() as patch:
        encode = artifacts.canonical_bytes
        patch.setattr(artifacts, "canonical_bytes", lambda value:
            b"x" * 4097 if value.get("schema") == artifacts.ARTIFACT_SCHEMA else encode(value))
        reads.refused("ARTIFACT_INVALID", lambda: make(lane))
    saved = save(lane)
    with monkeypatch.context() as patch:
        get = lane.get
        def unavailable_blob(key):
            if key == blob_key(saved):
                raise OSError("private blob detail")
            return get(key)
        patch.setattr(lane, "get", unavailable_blob)
        reads.refused("ARTIFACT_STORE_UNAVAILABLE", lambda: download(lane, saved.meta["artifact_id"]))
    monkeypatch.delenv("LEAF_SOLAR_PROJECT_RUN_ENABLED", raising=False)
    before = list(lane.reads)
    reads.refused("project_execution_disabled", lambda: download(lane, saved.meta["artifact_id"]))
    assert lane.reads == before
