"""Device supply contracts, with no podman, network, controller, or AWS access."""
import hashlib
import io
import json
from pathlib import Path
import re
import ssl
import subprocess
import threading
from types import SimpleNamespace
import urllib.error
import urllib.request

import pytest

from ci import native_release_device as device
from ci import native_release_producer as producer


SOURCE = "a" * 40
TREE = "b" * 40
SOLVER = "c" * 40
RUN = "dv-0123456789abcdef"
IMAGE = "sha256:" + "d" * 64
ARCHIVE = b"canonical packer archive bytes"
REAL_RESOLVE_FRESHNESS = device.resolve_freshness


def verdict(**changes):
    value = dict(schema="leaf.forge-ci.device-verdict.v1", state="success", mode="full",
                 repository="LEAF-Solar-Design/leaf-web-demo", head_sha=SOURCE, run_id=RUN)
    value.update(changes)
    return json.dumps(value).encode()


def freshness(service):
    return {name: "e" * 64 for name in producer.FRESHNESS.get(service, producer.TRIXIE)}


class FakeECR:
    def __init__(self, *, mutability="IMMUTABLE", filters=None, existing=None):
        self.mutability = mutability
        self.filters = filters
        self.existing = existing
        self.calls = []
        self.readback = [{"imageDigest": IMAGE}]

    def describe_repositories(self, **kwargs):
        self.calls.append(("repositories", kwargs))
        assert kwargs["registryId"] == "807034087062"
        assert len(kwargs["repositoryNames"]) == 1
        row = dict(repositoryName=kwargs["repositoryNames"][0])
        if self.mutability is not None:
            row["imageTagMutability"] = self.mutability
        if self.filters is not None:
            row["imageTagMutabilityExclusionFilters"] = self.filters
        return {"repositories": [row]}

    def describe_images(self, **kwargs):
        self.calls.append(("images", kwargs))
        assert kwargs["registryId"] == "807034087062"
        assert kwargs["repositoryName"] in {f"leaf-platform-{s}" for s in device.SERVICES}
        image_id, = kwargs["imageIds"]
        if "imageTag" in image_id:
            assert image_id == {"imageTag": f"native-device-{RUN}-{SOURCE}"}
            if kwargs["repositoryName"] == self.existing:
                return {"imageDetails": [{"imageDigest": IMAGE}]}
            return {"imageDetails": []}
        assert image_id == {"imageDigest": IMAGE}
        return {"imageDetails": self.readback}


class FakeRunner:
    def __init__(self, root):
        self.root = root
        self.calls = []
        self.lock = threading.Lock()
        self.health = {"ok": True, "source_sha": SOURCE}
        self.config = dict(producer.WEB_BUILD_CONFIG, vite_integrations_api_origin="")
        self.engine_bad = False
        self.container = hashlib.sha256(str(root).encode()).hexdigest()
        self.digest = IMAGE + "\n"
        self.write_digest = True

    def __call__(self, command, **kwargs):
        with self.lock:
            self.calls.append((list(command), kwargs))
        stdout = ""
        if command[0] == "git":
            if command[1:] == ["rev-parse", "HEAD"]:
                stdout = SOLVER if Path(kwargs["cwd"]) == self.root / "autofill-solver" else SOURCE
            elif command[1:] == ["rev-parse", "HEAD^{tree}"]:
                stdout = TREE
            else:
                assert command[1] == "status"
        elif command[:2] == ["podman", "build"]:
            kwargs["stdout"].write(b"build succeeded\n")
        elif command[:2] == ["podman", "push"]:
            if self.write_digest:
                Path(command[command.index("--digestfile") + 1]).write_text(self.digest)
        elif command[:2] == ["podman", "create"]:
            stdout = self.container
        elif command[:2] == ["podman", "cp"]:
            assert command[2] == f"{self.container}:/usr/share/nginx/html/."
            dist = Path(command[3])
            (dist / "health.json").write_text(json.dumps(self.health))
            (dist / "build-config.json").write_text(json.dumps(self.config))
            (dist / "engine").mkdir()
            records = {}
            for name in producer.WEB_ENGINE_FILES:
                data = name.encode()
                (dist / "engine" / name).write_bytes(data)
                records[name] = dict(bytes=len(data), sha256=hashlib.sha256(data).hexdigest())
            if self.engine_bad:
                records["engine.js"]["sha256"] = "0" * 64
            (dist / "engine/PROVENANCE.json").write_text(json.dumps({
                "contract": "leaf.cad-engine-stage.v1", "files": records}))
        elif command[:2] in (["podman", "pull"], ["podman", "rm"]):
            if command[1] == "rm":
                assert command[2] == self.container
        elif command[:2] == ["podman", "run"]:
            assert command[-1] == "--version"
            stdout = "git version 2.43.0\n"
        elif len(command) > 2 and command[1:3] == ["scripts/platform_release_manifest.py", "pack-web-dist"]:
            Path(command[command.index("--output") + 1]).write_bytes(ARCHIVE)
            stdout = json.dumps(dict(artifact_sha256="f" * 64,
                                     archive_sha256=hashlib.sha256(ARCHIVE).hexdigest()))
        else:
            pytest.fail(f"unexpected external command: {command}")
        return SimpleNamespace(stdout=stdout, returncode=0)


@pytest.fixture
def checkout(tmp_path, monkeypatch):
    root = tmp_path / "source"
    (root / "deploy").mkdir(parents=True)
    (root / "deploy/web-integration-config.json").write_text(json.dumps({
        "schema": "leaf.web-integration-config.v1", "apiOrigin": ""}))
    (root / "deploy/autofill-solver-sources.json").write_text(json.dumps({SOLVER: "f" * 64}))
    (root / "autofill-solver").mkdir()
    monkeypatch.delenv("LEAF_DEVICE_SOLVER_ROOT", raising=False)
    monkeypatch.setattr(device, "resolve_freshness", lambda *a, **k: {
        s: freshness(s) for s in device.SERVICES})
    return root, FakeRunner(root)


def test_device_build_command_parity_for_every_service():
    for service in device.SERVICES:
        extras = dict(solver_revision=SOLVER, solver_root=Path("/reviewed/solver")) if service == "canonical-worker" else {}
        original = producer.image_build_command(service, SOURCE, 1, freshness(service), Path("metadata"),
                                                 integration_origin="https://api.example.com", **extras)
        expected = ["podman", "build", "--format", "oci"]
        index = 3
        while index < len(original):
            item = original[index]
            if item in ("--metadata-file", "--output"):
                index += 2
                continue
            if item == "--push":
                index += 1
                continue
            if item == "--tag":
                expected += [item, device.candidate_ref(service, RUN, SOURCE)]
                index += 2
                continue
            expected.append(item)
            index += 1
        assert device.device_build_command(service, SOURCE, RUN, freshness(service),
                                           integration_origin="https://api.example.com", **extras) == expected
        assert original[0] == "docker"


def test_unmapped_producer_argument_refuses(monkeypatch):
    original = producer.image_build_command

    def changed(*args, **kwargs):
        command = original(*args, **kwargs)
        return command[:-1] + ["--new-producer-feature", "."]

    monkeypatch.setattr(producer, "image_build_command", changed)
    with pytest.raises(ValueError, match="unmapped producer argument"):
        device.device_build_command("app", SOURCE, RUN, freshness("app"), integration_origin="")


@pytest.mark.parametrize("readback", [[], [{"imageDigest": "sha256:" + "0" * 64}],
                                     [{"imageDigest": IMAGE}, {"imageDigest": IMAGE}], [{}], None])
def test_push_requires_digestfile_and_exact_ecr_readback(tmp_path, readback):
    runner, ecr = FakeRunner(tmp_path), FakeECR()
    if readback is None:
        runner.write_digest = False
    else:
        ecr.readback = readback
    with pytest.raises((ValueError, FileNotFoundError)):
        device.push_image(device.candidate_ref("web", RUN, SOURCE), "web", runner=runner, ecr=ecr)
    assert runner.calls[0][0][:3] == ["podman", "push", "--digestfile"]


@pytest.mark.parametrize("digest", ["", "sha256:" + "A" * 64, "sha256:" + "a" * 63, IMAGE + "x"])
def test_push_refuses_invalid_digest(tmp_path, digest):
    runner, ecr = FakeRunner(tmp_path), FakeECR()
    runner.digest = digest
    with pytest.raises(ValueError, match="digest"):
        device.push_image(device.candidate_ref("app", RUN, SOURCE), "app", runner=runner, ecr=ecr)
    assert not ecr.calls


def test_app_push_keeps_zstd_compression(tmp_path):
    runner, ecr = FakeRunner(tmp_path), FakeECR()
    for service in device.SERVICES:
        ref = device.candidate_ref(service, RUN, SOURCE)
        result = device.push_image(ref, service, runner=runner, ecr=ecr)
        command = runner.calls[-1][0]
        assert command[4:] == (["--compression-format", "zstd", "--force-compression", ref]
                               if service == "app" else [ref])
        assert result == dict(repository=f"leaf-platform-{service}", image_digest=IMAGE,
                              source_revision=SOURCE)


@pytest.mark.parametrize("bad", [None, "health", "config", "engine"])
def test_web_package_uses_same_packer_and_checks(checkout, tmp_path, bad):
    root, runner = checkout
    if bad == "health":
        runner.health["source_sha"] = "0" * 40
    elif bad == "config":
        runner.config["vite_cad_edit"] = "0"
    elif bad == "engine":
        runner.engine_bad = True
    if bad:
        with pytest.raises(ValueError):
            device.package_web_image(root, IMAGE, SOURCE, tmp_path / "web", runner=runner)
    else:
        receipt = device.package_web_image(root, IMAGE, SOURCE, tmp_path / "web", runner=runner)
        assert Path(receipt["path"]).read_bytes() == ARCHIVE
        assert receipt["source_revision"] == SOURCE and receipt["image_digest"] == IMAGE
        command = runner.calls[-1][0]
        assert command[1:] == ["scripts/platform_release_manifest.py", "pack-web-dist", "--root",
                               str(tmp_path / "web/dist"), "--output", str(tmp_path / "web/web-dist.zip")]
    commands = [c for c, _ in runner.calls]
    assert all(c[0] != "docker" for c in commands)
    assert [c[1] for c in commands if c[0] == "podman"] == ["pull", "create", "cp", "rm"]
    assert commands[0][2:] == ["--platform", "linux/amd64",
                              f"{producer.REGISTRY}/leaf-platform-web@{IMAGE}"]
    if bad:
        assert not any("pack-web-dist" in c for c in commands)


@pytest.mark.parametrize("field,value", [("state", "failed"), ("mode", "partial"),
    ("repository", "untrusted/repo"), ("head_sha", "0" * 40), ("schema", "other"),
    ("run_id", "dv-0000000000000000"), ("run_id", "../file")])
def test_verdict_binding_refusals(checkout, field, value):
    root, runner = checkout
    with pytest.raises(ValueError, match="binding"):
        device.bind_verdict(verdict(**{field: value}), RUN, SOURCE, TREE, root=root, runner=runner)
    assert not runner.calls
    with pytest.raises(ValueError, match="size"):
        device.fetch_verdict(RUN, reader=lambda _: b" " * 65537)
    with pytest.raises(ValueError, match="run id"):
        device.fetch_verdict("dv-123", reader=lambda _: pytest.fail("invalid run must not be fetched"))


def test_verdict_binds_exact_bytes_and_checkout(checkout):
    root, runner = checkout
    raw = verdict() + b"\n"
    assert device.bind_verdict(raw, RUN, SOURCE, TREE, root=root, runner=runner) == {
        "run_id": RUN, "verdict_sha256": hashlib.sha256(raw).hexdigest()}
    for source, tree in [("0" * 40, TREE), (SOURCE, "0" * 40)]:
        with pytest.raises(ValueError, match="checkout"):
            device.bind_verdict(verdict(head_sha=source), RUN, source, tree, root=root, runner=runner)


class Response(io.BytesIO):
    def __init__(self, raw, url, status=200):
        super().__init__(raw)
        self.url, self.status = url, status

    def geturl(self):
        return self.url


def trusted_transport(monkeypatch, handler):
    contexts = []
    real_context = ssl.SSLContext

    class Context:
        def __init__(self, protocol):
            self.actual = real_context(protocol)
            self.ca = None
            contexts.append(self)

        def load_verify_locations(self, *, cafile):
            self.ca = cafile

    monkeypatch.setattr(device, "ssl", SimpleNamespace(
        SSLContext=Context, PROTOCOL_TLS_CLIENT=ssl.PROTOCOL_TLS_CLIENT))
    monkeypatch.setenv("LEAF_DEVICE_VERDICT_BASE", device.DEFAULT_VERDICT_BASE)
    monkeypatch.setenv("LEAF_DEVICE_VERDICT_CA", "/controller/only-ca.crt")

    def build(*handlers):
        https, redirects = handlers
        assert isinstance(https, urllib.request.HTTPSHandler)
        assert isinstance(redirects, device._NoRedirect)
        return SimpleNamespace(open=lambda url, timeout: handler(url, timeout, redirects))

    monkeypatch.setattr(device.urllib.request, "build_opener", build)
    return contexts


def test_untrusted_wellformed_receipt_refused(tmp_path, monkeypatch):
    local = tmp_path / "perfect-success.json"
    local.write_bytes(verdict())
    parser = device.argument_parser()
    for argument in ("--verdict", "--verdict-file", "--verdict-bytes", "--verdict-path"):
        with pytest.raises(SystemExit):
            parser.parse_args(["produce", "--run-id", RUN, "--output", str(tmp_path), argument, str(local)])
    assert set(vars(parser.parse_args(["produce", "--run-id", RUN, "--output", str(tmp_path)]))) == {
        "command", "run_id", "output"}
    monkeypatch.setenv("LEAF_DEVICE_VERDICT_BASE", local.parent.as_uri() + "/")
    with pytest.raises(ValueError, match="trusted verdict base"):
        device.read_trusted_verdict(RUN)

    def unsigned(url, timeout, redirects):
        # Model an unsigned TLS peer at the opener boundary: its otherwise perfect body is unreadable.
        assert timeout == 10
        raise urllib.error.URLError(ssl.SSLCertVerificationError("untrusted controller certificate"))

    contexts = trusted_transport(monkeypatch, unsigned)
    with pytest.raises(urllib.error.URLError):
        device.read_trusted_verdict(RUN)
    assert contexts[0].ca == "/controller/only-ca.crt"
    assert contexts[0].actual.get_ca_certs() == []
    assert contexts[0].actual.verify_mode == ssl.CERT_REQUIRED
    assert contexts[0].actual.check_hostname


@pytest.mark.parametrize("code", [301, 302])
def test_verdict_redirect_refused(monkeypatch, code):
    requests = []

    def redirect(url, timeout, handler):
        requests.append(url)
        return handler.redirect_request(urllib.request.Request(url), None, code, "redirect",
                                        {}, "https://attacker.example/success.json")

    trusted_transport(monkeypatch, redirect)
    with pytest.raises(ValueError, match="redirect refused"):
        device.read_trusted_verdict(RUN)
    assert requests == [device.DEFAULT_VERDICT_BASE + RUN + ".json"]


@pytest.mark.parametrize("status,size", [(200, 65536), (200, 65537), (403, 30), (200, 0)])
def test_trusted_verdict_http_bounds(monkeypatch, status, size):
    def respond(url, timeout, redirects):
        assert timeout == 10
        return Response(b"x" * size, url, status)

    trusted_transport(monkeypatch, respond)
    if status != 200 or size > 65536 or size == 0:
        with pytest.raises(ValueError):
            device.read_trusted_verdict(RUN)
    else:
        assert len(device.read_trusted_verdict(RUN)) == size


@pytest.mark.parametrize("service", device.SERVICES)
def test_existing_candidate_tag_refuses_before_push(checkout, tmp_path, service):
    root, runner = checkout
    with pytest.raises(ValueError, match="already exists"):
        device.produce(root, tmp_path / "out", RUN, runner=runner,
                       ecr=FakeECR(existing=f"leaf-platform-{service}"), reader=lambda _: verdict())
    assert not any(c[:2] == ["podman", "push"] for c, _ in runner.calls)


@pytest.mark.parametrize("mutability,filters,allowed", [
    ("MUTABLE", None, False), (None, None, False),
    ("IMMUTABLE_WITH_EXCLUSION", [{"filterType": "WILDCARD", "filter": "native-device-*"}], False),
    ("IMMUTABLE_WITH_EXCLUSION", [{"filterType": "WILDCARD", "filter": "*-" + SOURCE}], False),
    ("IMMUTABLE_WITH_EXCLUSION", [{"filterType": "WILDCARD", "filter": "production-*"}], True),
    ("IMMUTABLE_WITH_EXCLUSION", None, False),
    ("IMMUTABLE", None, True), ("MUTABLE_WITH_EXCLUSION", [], False)])
def test_push_refuses_unless_ecr_enforces_immutability(checkout, tmp_path, mutability, filters, allowed):
    root, runner = checkout
    ecr = FakeECR(mutability=mutability, filters=filters)
    if allowed:
        device.produce(root, tmp_path / "out", RUN, runner=runner, ecr=ecr, reader=lambda _: verdict())
        assert len([c for c, _ in runner.calls if c[:2] == ["podman", "push"]]) == 5
    else:
        with pytest.raises(ValueError, match="immutable"):
            device.produce(root, tmp_path / "out", RUN, runner=runner, ecr=ecr, reader=lambda _: verdict())
        assert not any(c[:2] == ["podman", "push"] for c, _ in runner.calls)


def test_device_request_matches_adapter_shape(checkout, tmp_path):
    # Admission rules from leaf-automation-aws-terraform
    # .codebuild/deploy/native-five-service.py (device manifest, #3305).
    root, runner = checkout
    ecr = FakeECR()
    reads = []

    def read(run_id):
        reads.append(run_id)
        return verdict()

    output = tmp_path / "out"
    result = device.produce(root, output, RUN, runner=runner, ecr=ecr, reader=read)
    request = json.loads((output / "device-request.json").read_text())
    assert request == result
    assert set(request) == {"release", "source_tree", "device_manifest"}
    release = request["release"]
    assert set(release) == {"schema", "source_revision", "source_tree", "run_id", "verdict_sha256"}
    assert release["schema"] == "leaf.native-release.device.v1"
    assert re.fullmatch(r"[0-9a-f]{40}", release["source_revision"])
    assert re.fullmatch(r"[0-9a-f]{40}", release["source_tree"])
    assert release["source_tree"] == request["source_tree"] == TREE
    assert re.fullmatch(r"dv-[0-9a-f]{16}", release["run_id"])
    assert re.fullmatch(r"[0-9a-f]{64}", release["verdict_sha256"])
    assert release["verdict_sha256"] == hashlib.sha256(verdict()).hexdigest()
    manifest = request["device_manifest"]
    assert set(manifest) == {"schema", "services"}
    assert manifest["schema"] == "leaf.native-release.device-supply.v1"
    assert set(manifest["services"]) == set(device.SERVICES)
    for service, row in manifest["services"].items():
        assert set(row) == {"repository", "image_digest", "source_revision"}
        assert row["repository"] == f"leaf-platform-{service}"
        assert re.fullmatch(r"sha256:[0-9a-f]{64}", row["image_digest"])
        assert row["source_revision"] == release["source_revision"] == SOURCE
    receipt = json.loads((output / "device-supply-receipt.json").read_text())
    assert set(receipt["build_logs_sha256"]) == {f"{s}.log" for s in device.SERVICES}
    assert all(d == hashlib.sha256(b"build succeeded\n").hexdigest()
               for d in receipt["build_logs_sha256"].values())
    assert receipt["push_digests"] == {s: IMAGE for s in device.SERVICES}
    assert receipt["ecr_readback"] == manifest["services"]
    assert (output / "web-dist.zip").read_bytes() == ARCHIVE
    assert receipt["web"]["archive_sha256"] == hashlib.sha256(ARCHIVE).hexdigest()
    assert set(p.name for p in output.iterdir()) == {
        "device-request.json", "device-supply-receipt.json", "web-dist.zip"}
    assert reads == [RUN]
    smoke = [c for c, _ in runner.calls if c[:2] == ["podman", "run"]]
    assert smoke == [device.git_smoke_command(s, IMAGE) for s in producer.GIT_IMAGES]
    assert all(c[0] != "docker" for c, _ in runner.calls)
    readbacks = [k for method, k in ecr.calls if method == "images" and "imageDigest" in k["imageIds"][0]]
    assert len(readbacks) == 5


@pytest.mark.parametrize("existing", ["directory", "file", "symlink"])
def test_outputs_are_create_only(checkout, tmp_path, existing):
    root, runner = checkout
    output = tmp_path / "out"
    if existing == "directory":
        output.mkdir()
        (output / "device-request.json").write_bytes(b"previous")
    elif existing == "file":
        output.write_bytes(b"previous")
    else:
        target = tmp_path / "target"
        target.mkdir()
        output.symlink_to(target, target_is_directory=True)
    ecr = FakeECR()
    with pytest.raises(FileExistsError):
        device.produce(root, output, RUN, runner=runner, ecr=ecr,
                       reader=lambda _: pytest.fail("existing output must refuse before fetch"))
    assert not runner.calls and not ecr.calls
    if existing == "directory":
        assert (output / "device-request.json").read_bytes() == b"previous"


def test_freshness_substitutes_only_runtime(checkout, monkeypatch):
    root, _ = checkout
    calls = []
    monkeypatch.setattr(producer, "_channel_digest", lambda url: hashlib.sha256(url.encode()).hexdigest())

    def run(command, **kwargs):
        calls.append((command, kwargs))
        return SimpleNamespace(stdout="https://dl-cdn.alpinelinux.org/alpine/v3.23/main\n")

    expected = producer.resolve_freshness
    # Compare the entire producer result with an isolated docker runner, then the podman wrapper.
    docker = device._runtime_call(expected, root, runtime="docker", runner=run)
    podman = REAL_RESOLVE_FRESHNESS(root, runner=run)
    assert docker == podman
    assert calls[1][0] == ["podman", *calls[0][0][1:]]
    assert calls[1][1] == calls[0][1]


def test_builds_are_concurrent_and_pin_race_retries_once(checkout, tmp_path, monkeypatch):
    root, runner = checkout
    barrier = threading.Barrier(5, timeout=10)
    builds = []
    lock = threading.Lock()
    newer = {k: "1" * 64 for k in freshness("app")}
    monkeypatch.setattr(producer, "refresh_debian_freshness", lambda service: newer)

    def run(command, **kwargs):
        if command[:2] == ["podman", "build"]:
            service = command[command.index("--file") + 1].split("Dockerfile.")[1]
            with lock:
                builds.append(service)
                attempt = builds.count(service)
            if attempt == 1:
                barrier.wait()
            if service == "app" and attempt == 1:
                kwargs["stdout"].write(b"computed checksum did NOT match: InRelease\n")
                raise subprocess.CalledProcessError(1, command)
            if service == "app":
                assert all(f"{key}={value}" in command for key, value in newer.items())
        return runner(command, **kwargs)

    device.produce(root, tmp_path / "out", RUN, runner=run, ecr=FakeECR(), reader=lambda _: verdict())
    assert builds.count("app") == 2
    assert all(builds.count(s) == 1 for s in device.SERVICES if s != "app")
    receipt = json.loads((tmp_path / "out/device-supply-receipt.json").read_text())
    assert "app.retry1.log" in receipt["build_logs_sha256"]


def test_build_failure_prevents_all_pushes(checkout, tmp_path):
    root, runner = checkout

    def run(command, **kwargs):
        if command[:2] == ["podman", "build"]:
            kwargs["stdout"].write(b"unrelated build failure\n")
            raise subprocess.CalledProcessError(1, command)
        return runner(command, **kwargs)

    with pytest.raises(subprocess.CalledProcessError):
        device.produce(root, tmp_path / "out", RUN, runner=run, ecr=FakeECR(), reader=lambda _: verdict())
    assert not any(c[:2] == ["podman", "push"] for c, _ in runner.calls)
