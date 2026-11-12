"""Source-only device supply producer. Publication requires separately granted ECR access."""
from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
import os
from pathlib import Path
import re
import ssl
import subprocess
import tempfile
from types import FunctionType, SimpleNamespace
import urllib.parse
import urllib.request

try:
    from . import native_release_producer as producer
except ImportError:  # Direct script invocation, including Python safe-path mode.
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "native_release_producer", Path(__file__).with_name("native_release_producer.py"))
    producer = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(producer)


SERVICES = producer.SERVICES
REGISTRY_ID = "807034087062"
VERDICT_LIMIT = 65536
DEFAULT_VERDICT_BASE = "https://leaf-ci-ctl.tail95c8f1.ts.net:8443/verdicts/"
DEFAULT_VERDICT_CA = "/etc/leaf-ci/controller-ca.crt"
RUN_ID = r"dv-[0-9a-f]{16}"
DIGEST = r"sha256:[0-9a-f]{64}"


def _run_id(value):
    if not isinstance(value, str) or not re.fullmatch(RUN_ID, value):
        raise ValueError("invalid device run id")
    return value


def candidate_ref(service, run_id, source):
    _run_id(run_id)
    producer._hex(source, 40, "source")
    if service not in SERVICES:
        raise ValueError("invalid service")
    return f"{producer.REGISTRY}/leaf-platform-{service}:native-device-{run_id}-{source}"


def device_build_command(service, source, build_number, freshness, *, integration_origin,
                         solver_revision=None, solver_root=None):
    """build_number is the device run ID; the upstream numeric tag is replaced once."""
    ref = candidate_ref(service, build_number, source)
    command = producer.image_build_command(
        service, source, 1, freshness, Path("unused-device-metadata.json"),
        integration_origin=integration_origin, solver_revision=solver_revision, solver_root=solver_root)
    if command[:3] != ["docker", "buildx", "build"]:
        raise ValueError("unmapped producer argument")
    result = ["podman", "build", "--format", "oci"]
    index = 3
    tags = 0
    while index < len(command):
        arg = command[index]
        if arg == "." and index == len(command) - 1:
            result.append(arg)
        elif arg in ("--pull", "--push"):
            if arg == "--pull":
                result.append(arg)
        elif arg in ("--metadata-file", "--output", "--platform", "--file",
                     "--build-arg", "--build-context", "--tag") and index + 1 < len(command):
            value = command[index + 1]
            if arg == "--tag":
                if value != f"{producer.REGISTRY}/leaf-platform-{service}:native-1-{source}":
                    raise ValueError("unmapped producer argument")
                tags += 1
                value = ref
            if arg not in ("--metadata-file", "--output"):
                result.extend((arg, value))
            index += 1
        else:
            raise ValueError("unmapped producer argument")
        index += 1
    if tags != 1 or result[-1] != ".":
        raise ValueError("unmapped producer argument")
    return result


def _runtime_runner(runner, *, runtime):
    def run(command, **kwargs):
        if command[0] == "docker":
            command = [runtime, *command[1:]]
        return runner(command, **kwargs)
    return run


def _runtime_call(function, *args, runtime, runner, **kwargs):
    """Copy only the function's globals, never mutate shared producer/subprocess state.

    This keeps all producer validation and argv intact, including future changes.
    It is safe while the independent image builds run in other threads.
    """
    namespace = dict(function.__globals__)
    namespace["subprocess"] = SimpleNamespace(
        run=_runtime_runner(runner, runtime=runtime), TimeoutExpired=subprocess.TimeoutExpired,
        CalledProcessError=subprocess.CalledProcessError, STDOUT=subprocess.STDOUT)
    wrapped = FunctionType(function.__code__, namespace, function.__name__, function.__defaults__,
                           function.__closure__)
    wrapped.__kwdefaults__ = function.__kwdefaults__
    return wrapped(*args, **kwargs)


def git_smoke_command(service, image_digest, *, runtime="podman"):
    return [runtime, *producer.git_smoke_command(service, image_digest)[1:]]


def package_web_image(root, image_digest, source, output_dir, *, runner=subprocess.run, runtime="podman"):
    return _runtime_call(producer.package_web_image, root, image_digest, source, output_dir,
                         runtime=runtime, runner=runner)


def resolve_freshness(root, *, runner=subprocess.run, runtime="podman"):
    return _runtime_call(producer.resolve_freshness, root, runtime=runtime, runner=runner)


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise ValueError("device verdict redirect refused")


def read_trusted_verdict(run_id):
    _run_id(run_id)
    base = os.environ.get("LEAF_DEVICE_VERDICT_BASE", DEFAULT_VERDICT_BASE)
    origin = urllib.parse.urlsplit(base)
    if (origin.scheme != "https" or not origin.hostname or origin.username or origin.password
            or origin.query or origin.fragment or not origin.path.endswith("/")):
        raise ValueError("invalid trusted verdict base")
    url = base + run_id + ".json"
    target = urllib.parse.urlsplit(url)
    if (target.scheme, target.netloc) != (origin.scheme, origin.netloc):
        raise ValueError("device verdict origin differs")
    # SSLContext does not load system roots. Only the configured controller CA is trusted.
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    context.load_verify_locations(cafile=os.environ.get("LEAF_DEVICE_VERDICT_CA", DEFAULT_VERDICT_CA))
    opener = urllib.request.build_opener(
        urllib.request.HTTPSHandler(context=context), _NoRedirect())
    with opener.open(url, timeout=10) as response:
        if response.status != 200 or response.geturl() != url:
            raise ValueError("device verdict HTTP response refused")
        raw = response.read(VERDICT_LIMIT + 1)
    if not raw or len(raw) > VERDICT_LIMIT:
        raise ValueError("device verdict size exceeds bound")
    return raw


def fetch_verdict(run_id, *, reader=read_trusted_verdict):
    _run_id(run_id)
    raw = reader(run_id)
    if not isinstance(raw, bytes) or not raw or len(raw) > VERDICT_LIMIT:
        raise ValueError("device verdict size exceeds bound")
    return raw


def _git(root, runner, *args):
    return runner(["git", *args], cwd=root, check=True, text=True,
                  capture_output=True, timeout=30).stdout.strip()


def bind_verdict(raw, run_id, source, tree, *, root=None, runner=subprocess.run):
    _run_id(run_id)
    producer._hex(source, 40, "source")
    producer._hex(tree, 40, "tree")
    if not isinstance(raw, bytes) or not raw or len(raw) > VERDICT_LIMIT:
        raise ValueError("device verdict size exceeds bound")
    verdict = json.loads(raw)
    expected = dict(schema="leaf.forge-ci.device-verdict.v1", state="success", mode="full",
                    repository="LEAF-Solar-Design/leaf-web-demo", head_sha=source, run_id=run_id)
    if not isinstance(verdict, dict) or any(verdict.get(k) != v for k, v in expected.items()):
        raise ValueError("device verdict binding differs")
    root = Path(root) if root is not None else Path(__file__).resolve().parents[2]
    if (_git(root, runner, "rev-parse", "HEAD") != source
            or _git(root, runner, "rev-parse", "HEAD^{tree}") != tree):
        raise ValueError("checkout differs from admitted source/tree")
    return {"run_id": run_id, "verdict_sha256": hashlib.sha256(raw).hexdigest()}


def check_candidate_repositories(run_id, source, *, ecr):
    """Check all five before any push; only ECR policy enforces concurrent exclusion."""
    for service in SERVICES:
        ref = candidate_ref(service, run_id, source)
        repository, tag = f"leaf-platform-{service}", ref.rsplit(":", 1)[1]
        rows = ecr.describe_repositories(registryId=REGISTRY_ID,
                                        repositoryNames=[repository]).get("repositories", [])
        if len(rows) != 1 or rows[0].get("repositoryName") != repository:
            raise ValueError("ECR repository readback differs")
        row = rows[0]
        mutability = row.get("imageTagMutability")
        if mutability == "IMMUTABLE_WITH_EXCLUSION":
            filters = row.get("imageTagMutabilityExclusionFilters")
            if not isinstance(filters, list) or any(
                not isinstance(item, dict) or item.get("filterType") != "WILDCARD"
                or not isinstance(item.get("filter"), str) or not item["filter"]
                # ECR wildcard filters use '*' only, not shell character classes or '?'.
                or re.fullmatch(re.escape(item["filter"]).replace(r"\*", ".*"), tag)
                for item in filters
            ):
                raise ValueError("candidate tag is not immutable")
        elif mutability != "IMMUTABLE":
            raise ValueError("candidate tag is not immutable")
        try:
            found = ecr.describe_images(registryId=REGISTRY_ID, repositoryName=repository,
                                        imageIds=[{"imageTag": tag}])
        except Exception as exc:
            if getattr(exc, "response", {}).get("Error", {}).get("Code") != "ImageNotFoundException":
                raise
        else:
            if found.get("imageDetails"):
                raise ValueError("candidate tag already exists")


def push_image(ref, service, *, runner=subprocess.run, ecr):
    if service not in SERVICES:
        raise ValueError("invalid service")
    match = re.fullmatch(re.escape(f"{producer.REGISTRY}/leaf-platform-{service}:native-device-")
                         + rf"({RUN_ID})-([0-9a-f]{{40}})", ref)
    if not match:
        raise ValueError("invalid candidate reference")
    with tempfile.TemporaryDirectory(prefix="leaf-device-push-") as scratch:
        digestfile = Path(scratch) / "digest"
        command = ["podman", "push", "--digestfile", str(digestfile)]
        if service == "app":
            command += ["--compression-format", "zstd", "--force-compression"]
        runner([*command, ref], check=True, timeout=45 * 60)
        try:
            digest = digestfile.read_text(encoding="ascii").strip()
        except (FileNotFoundError, UnicodeError) as exc:
            raise ValueError("podman did not return an exact push digestfile") from exc
    if not re.fullmatch(DIGEST, digest):
        raise ValueError("podman did not return an exact push digest")
    repository = f"leaf-platform-{service}"
    rows = ecr.describe_images(registryId=REGISTRY_ID, repositoryName=repository,
                               imageIds=[{"imageDigest": digest}]).get("imageDetails", [])
    if len(rows) != 1 or rows[0].get("imageDigest") != digest:
        raise ValueError("ECR digest readback differs")
    return {"repository": repository, "image_digest": digest, "source_revision": match[2]}


def _write_json(path, value):
    with path.open("x", encoding="utf-8") as stream:
        stream.write(json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n")


def produce(root, output, run_id, *, runner=subprocess.run, ecr, reader=read_trusted_verdict):
    root, output = Path(root).resolve(), Path(output).absolute()
    _run_id(run_id)
    # Reserve the whole destination before any external side effect. Never reuse partial output.
    output.mkdir(parents=True, exist_ok=False)
    source = _git(root, runner, "rev-parse", "HEAD")
    tree = _git(root, runner, "rev-parse", "HEAD^{tree}")
    fetch_raw = fetch_verdict(run_id, reader=reader)
    binding = bind_verdict(fetch_raw, run_id, source, tree,
                           root=root, runner=runner)
    if _git(root, runner, "status", "--porcelain", "--untracked-files=normal"):
        raise ValueError("device producer requires a clean checkout")
    check_candidate_repositories(run_id, source, ecr=ecr)
    pins = json.loads((root / "deploy/autofill-solver-sources.json").read_text())
    if not isinstance(pins, dict) or len(pins) != 1:
        raise ValueError("device requires one reviewed solver pin")
    revision, content_hash = next(iter(pins.items()))
    producer._hex(revision, 40, "solver revision")
    producer._hex(content_hash, 64, "solver content hash")
    solver_root = Path(os.environ.get("LEAF_DEVICE_SOLVER_ROOT", str(root / "autofill-solver"))).resolve()
    if (_git(solver_root, runner, "rev-parse", "HEAD") != revision
            or _git(solver_root, runner, "status", "--porcelain", "--untracked-files=normal")):
        raise ValueError("solver checkout differs from reviewed pin")
    origin = producer.read_web_integration_origin(root)
    freshness = resolve_freshness(root, runner=runner)
    with tempfile.TemporaryDirectory(prefix="leaf-device-build-") as scratch:
        work = Path(scratch)

        def build(service, fresh, retry=False):
            bind_verdict(fetch_raw, run_id, source, tree, root=root, runner=runner)
            extras = dict(solver_revision=revision, solver_root=solver_root) if service == "canonical-worker" else {}
            command = device_build_command(service, source, run_id, fresh,
                                           integration_origin=origin, **extras)
            log = work / (f"{service}.retry1.log" if retry else f"{service}.log")
            with log.open("xb") as stream:
                runner(command, cwd=root, check=True, timeout=45 * 60,
                       stdout=stream, stderr=subprocess.STDOUT)

        # Retain exactly the authenticated bytes rather than refetching a moving response.
        with ThreadPoolExecutor(max_workers=len(SERVICES)) as pool:
            futures = {s: pool.submit(build, s, freshness[s]) for s in SERVICES}
        for service in SERVICES:
            error = futures[service].exception()
            if error is None:
                continue
            tail = producer._build_log_tail(work / f"{service}.log")
            if (service == "web" or not isinstance(error, subprocess.CalledProcessError)
                    or tail is None or not all(m in tail for m in producer.PIN_RACE_MARKERS)):
                raise error
            fresh = producer.refresh_debian_freshness(service)
            if fresh == freshness[service]:
                raise error
            build(service, fresh, retry=True)
        # Policy and source are rechecked after builds, before the first push.
        bind_verdict(fetch_raw, run_id, source, tree, root=root, runner=runner)
        check_candidate_repositories(run_id, source, ecr=ecr)
        images = {s: push_image(candidate_ref(s, run_id, source), s, runner=runner, ecr=ecr)
                  for s in SERVICES}
        versions = producer.verify_git_images(images, runner=_runtime_runner(runner, runtime="podman"))
        web = package_web_image(root, images["web"]["image_digest"], source, work / "web", runner=runner)
        payload = Path(web["path"]).read_bytes()
        if hashlib.sha256(payload).hexdigest() != web["archive_sha256"]:
            raise ValueError("web archive bytes changed")
        request = {"release": {"schema": "leaf.native-release.device.v1",
                                "source_revision": source, "source_tree": tree, **binding},
                   "source_tree": tree,
                   "device_manifest": {"schema": "leaf.native-release.device-supply.v1", "services": images}}
        logs = {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in sorted(work.glob("*.log"))}
        with (output / "web-dist.zip").open("xb") as stream:
            stream.write(payload)
        _write_json(output / "device-supply-receipt.json", {
            "schema": "leaf.native-release.device-supply-receipt.v1", **binding,
            "source_revision": source, "source_tree": tree, "build_logs_sha256": logs,
            "push_digests": {s: images[s]["image_digest"] for s in SERVICES},
            "ecr_readback": images, "git_versions": versions,
            "web": {k: v for k, v in web.items() if k != "path"}})
        _write_json(output / "device-request.json", request)
    return request


def argument_parser():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    command = commands.add_parser("produce")
    command.add_argument("--run-id", required=True, type=_run_id)
    command.add_argument("--output", required=True, type=Path)
    return parser


def main(argv=None):
    args = argument_parser().parse_args(argv)
    import boto3
    from botocore.config import Config
    ecr = boto3.client("ecr", region_name="us-east-1", config=Config(
        connect_timeout=10, read_timeout=60, retries={"max_attempts": 1}))
    produce(Path(__file__).resolve().parents[2], args.output, args.run_id, ecr=ecr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
