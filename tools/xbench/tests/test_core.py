"""Synthetic contract coverage for the shared grading core."""

from dataclasses import FrozenInstanceError
import hashlib
from io import BytesIO
import json
from pathlib import Path
import re

import pytest

import xbench_core as core


SHA = "a" * 40


def fixture_row(path, data=b"synthetic"):
    return {"id": "sample", "path": str(path), "bytes": len(data),
            "sha256": hashlib.sha256(data).hexdigest()}


def manifest_value(fixtures=None):
    return {"schema": core.MANIFEST_SCHEMA, "bench": "X01",
            "fixtures": [] if fixtures is None else fixtures, "extra": "kept"}


def put_manifest(tmp_path, value):
    path = tmp_path / "manifest.json"
    path.write_text(json.dumps(value), encoding="utf-8")
    return path


def grade_args(**updates):
    args = dict(bench="X01", verdict="PASS", source_sha=SHA, manifest_sha256="b" * 64,
                started_at="2026-01-01T00:00:00Z", finished_at="2026-01-01T00:00:01Z",
                steps=[], fixtures=[])
    args.update(updates)
    return args


def test_manifest_happy_and_raw_digest(tmp_path):
    path = put_manifest(tmp_path, manifest_value([fixture_row(tmp_path / "sample.bin")]))
    value = core.load_manifest(path)
    assert value["extra"] == "kept"
    assert value["manifest_sha256"] == hashlib.sha256(path.read_bytes()).hexdigest()
    assert value["fixtures"][0]["id"] == "sample"


def test_manifest_missing(tmp_path):
    with pytest.raises(core.ManifestError):
        core.load_manifest(tmp_path / "missing.json")


def test_manifest_size_limit(tmp_path):
    path = tmp_path / "large.json"
    path.write_bytes(b" " * (core.MAX_MANIFEST_BYTES + 1))
    with pytest.raises(core.ManifestError):
        core.load_manifest(path)


@pytest.mark.parametrize("raw", [b"{broken", b"\xff", b""])
def test_manifest_invalid_json(tmp_path, raw):
    path = tmp_path / "bad.json"
    path.write_bytes(raw)
    with pytest.raises(core.ManifestError):
        core.load_manifest(path)


@pytest.mark.parametrize("value", [[], None, 1, "object"])
def test_manifest_non_object(tmp_path, value):
    with pytest.raises(core.ManifestError):
        core.load_manifest(put_manifest(tmp_path, value))


@pytest.mark.parametrize("key,value", [
    ("schema", "wrong"), ("schema", None), ("bench", "X14"), ("bench", "x01"),
    ("fixtures", {}), ("fixtures", None),
])
def test_manifest_top_level_refusals(tmp_path, key, value):
    manifest = manifest_value()
    manifest[key] = value
    with pytest.raises(core.ManifestError):
        core.load_manifest(put_manifest(tmp_path, manifest))


def test_manifest_fixture_limit(tmp_path):
    fixtures = [dict(fixture_row(tmp_path / "a"), id=str(n)) for n in range(core.MAX_FIXTURES + 1)]
    with pytest.raises(core.ManifestError):
        core.load_manifest(put_manifest(tmp_path, manifest_value(fixtures)))


@pytest.mark.parametrize("row", [None, [], "fixture", {}])
def test_manifest_fixture_shape(tmp_path, row):
    with pytest.raises(core.ManifestError):
        core.load_manifest(put_manifest(tmp_path, manifest_value([row])))


@pytest.mark.parametrize("key,value", [
    ("id", None), ("id", 7), ("path", None), ("path", 7), ("bytes", -1),
    ("bytes", True), ("bytes", 1.0), ("bytes", "9"), ("sha256", "a" * 63),
    ("sha256", "A" * 64), ("sha256", "g" * 64), ("sha256", None),
])
def test_manifest_fixture_fields(tmp_path, key, value):
    row = fixture_row(tmp_path / "sample")
    row[key] = value
    with pytest.raises(core.ManifestError):
        core.load_manifest(put_manifest(tmp_path, manifest_value([row])))


@pytest.mark.parametrize("key", ["id", "path", "bytes", "sha256"])
def test_manifest_fixture_required_fields(tmp_path, key):
    row = fixture_row(tmp_path / "sample")
    del row[key]
    with pytest.raises(core.ManifestError):
        core.load_manifest(put_manifest(tmp_path, manifest_value([row])))


def test_manifest_duplicate_ids(tmp_path):
    row = fixture_row(tmp_path / "sample")
    with pytest.raises(core.ManifestError):
        core.load_manifest(put_manifest(tmp_path, manifest_value([row, row])))


def test_manifest_boundary_count_and_zero_bytes(tmp_path):
    rows = [dict(fixture_row(tmp_path / "sample", b""), id=str(n))
            for n in range(core.MAX_FIXTURES)]
    assert len(core.load_manifest(put_manifest(tmp_path, manifest_value(rows)))["fixtures"]) == 200


@pytest.mark.parametrize("mode,reason", [
    ("ok", None), ("missing", "missing"), ("directory", "missing"),
    ("size", "bytes_mismatch"), ("hash", "sha256_mismatch"),
])
def test_verify_fixture_outcomes(tmp_path, mode, reason):
    path = tmp_path / "sample.bin"
    row = fixture_row(path)
    if mode == "directory":
        path.mkdir()
    elif mode != "missing":
        path.write_bytes(b"otherdata" if mode == "hash" else b"synthetic")
    if mode == "size":
        row["bytes"] += 1
    result = core.verify_fixtures(manifest_value([row]))[0]
    assert set(result) == {"id", "path", "bytes", "sha256", "ok", "reason",
                           "observed_bytes", "observed_sha256"}
    assert result["reason"] == reason
    assert result["ok"] == (reason is None)
    if reason in ("missing", "bytes_mismatch"):
        assert result["observed_sha256"] is None
    else:
        assert result["observed_sha256"] == hashlib.sha256(path.read_bytes()).hexdigest()


def test_verify_streams_large_fixture(tmp_path, monkeypatch):
    data = b"z" * (core.HASH_CHUNK * 2 + core.HASH_CHUNK // 2)
    path = tmp_path / "large.bin"
    path.write_bytes(data)
    original_open = Path.open
    reads = []

    class Reader:
        def __init__(self, stream):
            self.stream = stream

        def __enter__(self):
            return self

        def __exit__(self, *args):
            self.stream.close()

        def read(self, size=-1):
            reads.append(size)
            assert size == core.HASH_CHUNK
            return self.stream.read(size)

    def checked_open(target, *args, **kwargs):
        stream = original_open(target, *args, **kwargs)
        return Reader(stream) if target == path else stream

    monkeypatch.setattr(Path, "open", checked_open)
    rows = core.verify_fixtures(manifest_value([fixture_row(path, data)]))
    assert rows[0]["ok"] is True
    assert len(reads) == 4


def test_verify_manifest_order(tmp_path):
    rows = [dict(fixture_row(tmp_path / name), id=name) for name in ("z", "a")]
    assert [row["id"] for row in core.verify_fixtures(manifest_value(rows))] == ["z", "a"]


@pytest.mark.parametrize("verdicts,expected", [
    ([], "UNREADABLE"), (["PASS"], "PASS"), (["FAIL"], "FAIL"),
    (["UNREADABLE"], "UNREADABLE"), (["PASS", "UNREADABLE"], "UNREADABLE"),
    (["UNREADABLE", "FAIL"], "FAIL"), (["PASS", "FAIL", "UNREADABLE"], "FAIL"),
    (["PASS", "PASS"], "PASS"),
])
def test_aggregate_precedence(verdicts, expected):
    assert core.aggregate([core.Step(n, verdict, "evidence")
                           for n, verdict in enumerate(verdicts, 1)]) == expected


def test_aggregate_duplicate_numbers():
    with pytest.raises(ValueError):
        core.aggregate([core.Step(1, "FAIL", "a"), core.Step(1, "PASS", "b")])


@pytest.mark.parametrize("n,verdict", [(0, "PASS"), (-1, "PASS"), (1, "bad"), (True, "PASS")])
def test_step_refusals(n, verdict):
    with pytest.raises(ValueError):
        core.Step(n, verdict, "evidence")


def test_step_frozen_and_truncated():
    step = core.Step(1, "PASS", "x" * 2100)
    assert len(step.evidence) == 2000
    assert step.observed is None and step.expected is None
    with pytest.raises(FrozenInstanceError):
        step.n = 2


@pytest.mark.parametrize("verdict,code", [("PASS", 0), ("FAIL", 1), ("UNREADABLE", 4)])
def test_exit_codes(verdict, code):
    assert core.exit_code(verdict) == code


def test_exit_code_invalid():
    with pytest.raises(ValueError):
        core.exit_code("READINESS_PASS")


def test_health_good():
    calls = []

    def opener(url, timeout):
        calls.append((url, timeout))
        return json.dumps({"source_sha": SHA}).encode()

    assert core.read_health("https://example.test///", timeout=2.5, opener=opener) == SHA
    assert calls == [("https://example.test/api/health", 2.5)]


@pytest.mark.parametrize("raw", [b"not json", b"{}", b"[]", b"null",
    json.dumps({"source_sha": "a" * 39}).encode(),
    json.dumps({"source_sha": "A" * 40}).encode(),
    json.dumps({"source_sha": "g" * 40}).encode()])
def test_health_invalid(raw):
    with pytest.raises(core.HealthError):
        core.read_health("https://example.test", opener=lambda url, timeout: raw)


def test_health_transport_error():
    def opener(url, timeout):
        raise OSError("offline")

    with pytest.raises(core.HealthError, match="offline"):
        core.read_health("https://example.test", opener=opener)


def test_health_default_reader_is_bounded(monkeypatch):
    calls = []

    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def read(self, size):
            assert size == 1_048_576
            return json.dumps({"source_sha": SHA}).encode()

    def fake_urlopen(url, timeout):
        calls.append((url.full_url, timeout))
        return Response()

    monkeypatch.setattr(core, "urlopen", fake_urlopen)
    assert core.read_health("https://example.test") == SHA
    assert calls == [("https://example.test/api/health", 10.0)]


def test_health_default_request_headers_and_timeout(monkeypatch):
    calls = []

    def fake_urlopen(request, timeout):
        calls.append((request, timeout))
        return BytesIO(json.dumps({"source_sha": SHA}).encode())

    monkeypatch.setattr(core, "urlopen", fake_urlopen)
    assert core.read_health("https://example.test/", timeout=2.5) == SHA
    assert len(calls) == 1
    request, timeout = calls[0]
    assert isinstance(request, core.Request)
    assert request.full_url == "https://example.test/api/health"
    assert request.get_header("User-agent") == "Mozilla/5.0 leaf-xbench-grader"
    assert request.get_header("Accept") == "application/json"
    assert timeout == 2.5


@pytest.mark.parametrize("base", ["https://example.test", "https://example.test/",
    "https://example.test:8443/staging/", "http://127.0.0.1", "http://127.0.0.1:8000/",
    "http://localhost", "http://localhost:8000/"])
def test_validate_base_accept(base):
    assert core.validate_base(base) == base.rstrip("/")


@pytest.mark.parametrize("base", ["http://example.test", "ftp://example.test", "example.test",
    "https://", "http://localhost.evil.test", "http://127.0.0.2", "http://localhost:bad",
    "http://localhost:99999", "https://user:pass@example.test", "https://example.test/#frag"])
def test_validate_base_reject(base):
    with pytest.raises(ValueError):
        core.validate_base(base)


def test_write_grade_order_and_fallback(tmp_path):
    class NotJson:
        def __repr__(self):
            return "r" * 2100

    path = core.write_grade(tmp_path, **grade_args(steps=[
        core.Step(2, "PASS", "second", NotJson(), {1, 2}),
        core.Step(1, "PASS", "first", {"value": 1}, [2]),
    ]))
    value = json.loads(path.read_text(encoding="utf-8"))
    assert list(value) == ["schema", "bench", "verdict", "source_sha", "manifest_sha256",
                           "started_at", "finished_at", "steps", "fixtures", "limits", "options"]
    assert value["options"] == {}
    assert value["schema"] == core.GRADE_SCHEMA
    assert [step["n"] for step in value["steps"]] == [1, 2]
    assert list(value["steps"][0]) == ["n", "verdict", "evidence", "observed", "expected"]
    assert value["steps"][0]["observed"] == {"value": 1}
    assert value["steps"][1]["observed"] == "r" * 2000
    assert isinstance(value["steps"][1]["expected"], str)


@pytest.mark.parametrize("updates", [dict(bench="X14"), dict(verdict="bad"),
    dict(source_sha=None), dict(verdict="FAIL", source_sha=None),
    dict(source_sha="A" * 40), dict(source_sha="a" * 39)])
def test_write_grade_rejects(tmp_path, updates):
    with pytest.raises(ValueError):
        core.write_grade(tmp_path, **grade_args(**updates))
    assert not (tmp_path / "grade.json").exists()


def test_write_grade_unreadable_without_sha(tmp_path):
    path = core.write_grade(tmp_path, **grade_args(verdict="UNREADABLE", source_sha=None))
    assert json.loads(path.read_text())["source_sha"] is None


def test_write_readiness(tmp_path):
    receipt = tmp_path / "out" / "grade.json"
    path = core.write_readiness(tmp_path / "ready", bench="X01", sha=SHA, receipt=receipt)
    value = json.loads(path.read_text())
    assert path == tmp_path / "ready" / "X01.json"
    assert list(value) == ["bench", "sha", "verdict", "receipt", "recorded_at"]
    assert value["bench"] == "X01" and value["sha"] == SHA
    assert value["verdict"] == "READINESS_PASS"
    assert value["receipt"] == str(receipt)
    assert re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z", value["recorded_at"])


@pytest.mark.parametrize("bench,sha", [("X14", SHA), ("X01", "A" * 40), ("X01", None)])
def test_write_readiness_rejects(tmp_path, bench, sha):
    with pytest.raises(ValueError):
        core.write_readiness(tmp_path, bench=bench, sha=sha, receipt="grade.json")
    assert list(tmp_path.iterdir()) == []


def test_atomic_json_format_and_replace(tmp_path):
    path = tmp_path / "nested" / "receipt.json"
    core.write_json_atomic(path, {"old": True})
    obj = {"z": "café", "a": 1}
    assert core.write_json_atomic(path, obj) == path
    assert path.read_bytes() == (json.dumps(obj, indent=2, ensure_ascii=False) + "\n").encode("utf-8")
    assert list(path.parent.iterdir()) == [path]


def test_atomic_json_cleans_up_on_replace_failure(tmp_path, monkeypatch):
    path = core.write_json_atomic(tmp_path / "receipt.json", {"old": True})

    def fail_replace(source, destination):
        raise OSError("replace refused")

    monkeypatch.setattr(core.os, "replace", fail_replace)
    with pytest.raises(OSError):
        core.write_json_atomic(path, {"new": True})
    assert json.loads(path.read_text()) == {"old": True}
    assert list(tmp_path.iterdir()) == [path]
