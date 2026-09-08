"""Focused transport contract; no live AWS or GitHub calls."""

import io
import json
from pathlib import Path
import stat
import subprocess
import sys
import zipfile

import pytest
import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent))
import release_result_transport as transport

SOURCE = "a" * 40


class FakeAWS:
    def __init__(self):
        self.objects = {}
        self.calls = []
        self.truncated = False

    def __call__(self, operation, *args):
        self.calls.append((operation, args))
        def option(name):
            return args[args.index(name) + 1]
        assert option("--bucket") == transport.BUCKET
        if operation == "list-objects-v2":
            assert option("--max-keys") == "1000"
            assert "--no-paginate" in args
            return {"Contents": [{"Key": key} for key in self.objects
                                  if key.startswith(option("--prefix"))],
                    "IsTruncated": self.truncated}
        key = option("--key")
        if operation == "put-object":
            assert option("--if-none-match") == "*"
            data = Path(option("--body")).read_bytes()
            assert option("--checksum-sha256") == transport.checksum(data)
            if key in self.objects:
                raise subprocess.CalledProcessError(1, "aws", stderr="PreconditionFailed")
            self.objects[key] = (data, {"Metadata": json.loads(option("--metadata")),
                                      "ChecksumSHA256": transport.checksum(data),
                                      "ContentLength": len(data), "ETag": '"etag"',
                                      "VersionId": "version-1"})
            return {}
        data, response = self.objects[key]
        assert option("--checksum-mode") == "ENABLED"
        if operation == "get-object":
            if "VersionId" in response:
                assert option("--version-id") == response["VersionId"]
            else:
                assert option("--if-match") == response["ETag"]
            Path(args[-1]).write_bytes(data)
        return dict(response)


def publish(tmp_path, fake, kind="service-app", attempt=1):
    path = tmp_path / (kind + "-input")
    if transport.is_archive(kind):
        path.mkdir(exist_ok=True)
        (path / "index.html").write_text("exact dist")
        (path / "assets").mkdir(exist_ok=True)
        (path / "assets" / "app.js").write_bytes(b"js")
    else:
        path.write_bytes(b'{"exact":"entry"}\n')
    descriptor = transport.put(path, SOURCE, 123, attempt, kind, fake)
    return path, descriptor


@pytest.mark.parametrize("kind", sorted(transport.KINDS))
def test_roundtrip(tmp_path, kind):
    fake = FakeAWS()
    path, descriptor = publish(tmp_path, fake, kind)
    output = tmp_path / "retrieved"
    result = transport.get(output, SOURCE, 123, 1, kind, fake)
    assert result == descriptor
    assert "artifact_id" not in result
    assert result["version_id"] == "version-1"
    if transport.is_archive(kind):
        assert (output / "index.html").read_bytes() == (path / "index.html").read_bytes()
        assert transport.package(path, kind) == transport.package(output, kind)
    else:
        assert output.read_bytes() == path.read_bytes()


def test_failed_only_retry_and_newest_corrupt(tmp_path):
    fake = FakeAWS()
    _, first = publish(tmp_path, fake)
    assert transport.get(tmp_path / "old", SOURCE, 123, 3, "service-app", fake) == first
    _, newest = publish(tmp_path, fake, attempt=2)
    data, response = fake.objects[newest["key"]]
    fake.objects[newest["key"]] = (b"x" * len(data), response)
    with pytest.raises(transport.TransportError, match="checksum"):
        transport.get(tmp_path / "absent", SOURCE, 123, 3, "service-app", fake)
    assert not (tmp_path / "absent").exists()


@pytest.mark.parametrize("field,value", [("source", "b" * 40), ("run", "124"),
                                          ("kind", "service-web"), ("attempt", "2")])
def test_wrong_metadata(tmp_path, field, value):
    fake = FakeAWS()
    _, descriptor = publish(tmp_path, fake)
    fake.objects[descriptor["key"]][1]["Metadata"][field] = value
    with pytest.raises(transport.TransportError):
        transport.get(tmp_path / "out", SOURCE, 123, 1, "service-app", fake)


def test_immutable_same_and_different(tmp_path):
    fake = FakeAWS()
    path, descriptor = publish(tmp_path, fake)
    assert transport.put(path, SOURCE, 123, 1, "service-app", fake) == descriptor
    path.write_text('{"different":true}')
    with pytest.raises(transport.TransportError, match="differs"):
        transport.put(path, SOURCE, 123, 1, "service-app", fake)


@pytest.mark.parametrize("source,run,attempt,kind", [("A" * 40, 1, 1, "service-app"),
    (SOURCE, 0, 1, "service-app"), (SOURCE, 1, -1, "service-app"),
    (SOURCE, 1, 1, "unknown")])
def test_invalid_identity_never_calls_aws(tmp_path, source, run, attempt, kind):
    fake = FakeAWS()
    with pytest.raises(transport.TransportError):
        transport.put(tmp_path / "missing", source, run, attempt, kind, fake)
    assert not fake.calls


def archive(entries):
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as output:
        for name, mode in entries:
            entry = zipfile.ZipInfo(name)
            # ZipInfo normalizes backslashes on Windows. Preserve the raw test
            # name so the archive actually contains the unsafe member path.
            entry.filename = name
            entry.external_attr = mode << 16
            output.writestr(entry, b"data")
    return buffer.getvalue()


@pytest.mark.parametrize("entries", [
    [("good", stat.S_IFREG), ("../escape", stat.S_IFREG)],
    [("/absolute", stat.S_IFREG)], [("a\\b", stat.S_IFREG)],
    [("a", stat.S_IFREG), ("a", stat.S_IFREG)],
    [("a", stat.S_IFREG), ("A", stat.S_IFREG)],
    [("a", stat.S_IFREG), ("a/b", stat.S_IFREG)],
    [("link", stat.S_IFLNK)], [("dir/", stat.S_IFDIR)]])
def test_invalid_archive_before_extraction(tmp_path, entries):
    fake = FakeAWS()
    _, descriptor = publish(tmp_path, fake, "web-dist")
    data = archive(entries)
    response = fake.objects[descriptor["key"]][1]
    response.update(ContentLength=len(data), ChecksumSHA256=transport.checksum(data))
    fake.objects[descriptor["key"]] = (data, response)
    with pytest.raises(transport.TransportError):
        transport.get(tmp_path / "out", SOURCE, 123, 1, "web-dist", fake)
    assert not (tmp_path / "out").exists()


@pytest.mark.parametrize("bound,value", [("FILES", 1), ("ARCHIVE_BYTES", 1),
                                       ("EXPANDED_BYTES", 1), ("JSON_BYTES", 1)])
def test_bounds(tmp_path, monkeypatch, bound, value):
    fake = FakeAWS()
    kind = "service-app" if bound == "JSON_BYTES" else "web-dist"
    path, _ = publish(tmp_path, fake, kind)
    monkeypatch.setattr(transport, bound, value)
    with pytest.raises(transport.TransportError):
        transport.package(path, kind)


def test_links_and_listing_bound(tmp_path):
    fake = FakeAWS()
    path, _ = publish(tmp_path, fake, "web-dist")
    fake.truncated = True
    with pytest.raises(transport.TransportError, match="listing"):
        transport.get(tmp_path / "out", SOURCE, 123, 1, "web-dist", fake)
    fake.truncated = False
    link = tmp_path / "link"
    try:
        link.symlink_to(path, target_is_directory=True)
    except OSError:
        pytest.skip("host does not permit symlink creation")
    with pytest.raises(transport.TransportError, match="link"):
        transport.get(link, SOURCE, 123, 1, "web-dist", fake)
    with pytest.raises(transport.TransportError, match="link"):
        transport.package(link, "web-dist")


def test_etag_binding_and_bad_checksum(tmp_path):
    fake = FakeAWS()
    _, descriptor = publish(tmp_path, fake)
    response = fake.objects[descriptor["key"]][1]
    del response["VersionId"]
    transport.get(tmp_path / "out", SOURCE, 123, 1, "service-app", fake)
    response["ChecksumSHA256"] = "wrong"
    with pytest.raises(transport.TransportError):
        transport.get(tmp_path / "bad", SOURCE, 123, 1, "service-app", fake)


def test_workflow_required_transport_and_strict_v3():
    path = Path(__file__).resolve().parents[1] / ".github/workflows/build-platform-images.yml"
    workflow = yaml.safe_load(path.read_text(encoding="utf-8"))
    for job, copies in {
        "build": [("Publish exact v3 web deployment artifact to S3", "Preserve the exact v3 web deployment artifact"),
                  ("Publish exact v3 service entry to S3", "Upload exact v3 service entry")],
        "verify": [("Publish immutable staging supply set and web dist to S3", "Upload immutable staging supply-set manifest"),
                   ("Publish immutable staging supply set and web dist to S3", "Upload deterministic web deployment artifact")],
    }.items():
        steps = workflow["jobs"][job]["steps"]
        names = [step.get("name") for step in steps]
        for required, optional in copies:
            assert names.index(required) < names.index(optional)
            assert not steps[names.index(required)].get("continue-on-error", False)
            assert steps[names.index(optional)]["continue-on-error"] is True
            assert "release_result_transport.py put" in steps[names.index(required)]["run"]
    steps = workflow["jobs"]["verify"]["steps"]
    names = [step.get("name") for step in steps]
    for fetch in ("Download exact v3 service entries", "Download exact v3 web deployment artifact"):
        assert names.index("Configure AWS credentials (OIDC)") < names.index(fetch)
        assert "release_result_transport.py get" in steps[names.index(fetch)]["run"]
    strict = steps[names.index("Write the immutable five-service staging supply set")]["run"]
    assert "for image in app broker canonical-worker harness web; do" in strict
    assert "docker buildx imagetools inspect --raw" in strict
    assert "platform_release_manifest.py generate-v3" in strict
    assert '--service-entry "web=${entries[web]}"' in strict
    assert "--build-run-attempt" in strict
