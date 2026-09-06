"""Native Studio release producer building blocks.

No provider is provisioned and nothing runs on import. The release controller
must establish source admission and gate success before calling build_image.
This is not yet the complete release entry point or a deployment command.
"""
from __future__ import annotations

import json
import hashlib
from pathlib import Path
import re
import subprocess
import sys
import time


SERVICES = ("app", "broker", "canonical-worker", "harness", "web")
REGISTRY = "807034087062.dkr.ecr.us-east-1.amazonaws.com"
BASE_IMAGES = (
    "nginx:alpine", "rust:1-slim", "node:20-slim", "node:22-bookworm",
    "node:22-slim", "python:3.12-slim",
)
FRESHNESS = {
    "harness": ("HARNESS_DEBIAN_SECURITY_INRELEASE_SHA256", "HARNESS_DEBIAN_UPDATES_INRELEASE_SHA256"),
    "web": ("WEB_ALPINE_MAIN_APKINDEX_SHA256",),
}
TRIXIE = ("TRIXIE_DEBIAN_SECURITY_INRELEASE_SHA256", "TRIXIE_DEBIAN_UPDATES_INRELEASE_SHA256")


def _hex(value, length, label):
    if not isinstance(value, str) or not re.fullmatch(rf"[0-9a-f]{{{length}}}", value):
        raise ValueError(f"invalid {label}")
    return value


def _build_identity(value: dict) -> dict:
    """Validate a native identity claim, not its provider authenticity."""
    if not isinstance(value, dict) or set(value) != {"project_arn", "build_arn", "build_number"}:
        raise ValueError("invalid native build identity fields")
    project = value["project_arn"]
    prefix = "arn:aws:codebuild:us-east-1:807034087062:"
    if not isinstance(project, str) or not re.fullmatch(re.escape(prefix) + r"project/[A-Za-z0-9_-]+", project):
        raise ValueError("invalid native project")
    build = value["build_arn"]
    build_prefix = prefix + "build/" + project.split("/", 1)[1] + ":"
    if not isinstance(build, str) or not re.fullmatch(
        re.escape(build_prefix) + r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", build
    ):
        raise ValueError("native build does not belong to project")
    if type(value["build_number"]) is not int or value["build_number"] <= 0:
        raise ValueError("invalid native build number")
    return dict(value)


def assemble_release(*, source: str, tree: str, producer: dict,
                     images: dict, web: dict, solver: dict, gate: dict) -> dict:
    """Bind release bytes without pretending an in-progress build is successful.

    This manifest is a claim inside the managed ZIP. Consumers must authenticate
    the completed publisher and gate builds through independently fetched AWS
    records. The enclosing ZIP's version/hash belong in the external receipt,
    not this document, which would create a circular digest dependency.
    """
    _hex(source, 40, "source")
    _hex(tree, 40, "tree")
    identity = _build_identity(producer)
    if set(images) != set(SERVICES):
        raise ValueError("release requires exactly five service images")
    services = {}
    for service in SERVICES:
        item = images[service]
        if (set(item) != {"repository", "image_digest", "source_revision", "native_build_number"}
                or item["repository"] != f"leaf-platform-{service}"
                or item["source_revision"] != source
                or type(item["native_build_number"]) is not int
                or item["native_build_number"] != identity["build_number"]):
            raise ValueError(f"{service} differs from admitted producer/source")
        digest = item["image_digest"]
        if not isinstance(digest, str) or not re.fullmatch(r"sha256:[0-9a-f]{64}", digest):
            raise ValueError(f"invalid {service} digest")
        services[service] = dict(item)
    if set(solver) != {"revision", "source_sha256"}:
        raise ValueError("invalid solver provenance fields")
    _hex(solver["revision"], 40, "solver revision")
    _hex(solver["source_sha256"], 64, "solver content digest")
    if (set(web) != {"path", "artifact_sha256", "archive_sha256", "image_digest", "source_revision"}
            or web["image_digest"] != services["web"]["image_digest"]
            or web["source_revision"] != source):
        raise ValueError("web archive differs from release image/source")
    _hex(web["artifact_sha256"], 64, "web content digest")
    _hex(web["archive_sha256"], 64, "web archive digest")
    archive = Path(web["path"])
    with archive.open("rb") as stream:
        archive_digest = hashlib.file_digest(stream, "sha256").hexdigest()
    if archive_digest != web["archive_sha256"]:
        raise ValueError("web archive bytes changed")
    if set(gate) != {"producer", "source_revision", "source_tree", "archive", "proof_sha256"}:
        raise ValueError("invalid gate evidence fields")
    gate_identity = _build_identity(gate["producer"])
    if gate_identity["project_arn"] == identity["project_arn"]:
        raise ValueError("gate requires a separate low-privilege project")
    if gate["source_revision"] != source or gate["source_tree"] != tree:
        raise ValueError("gate source/tree differs from release")
    _hex(gate["proof_sha256"], 64, "gate proof digest")
    obj = gate["archive"]
    if not isinstance(obj, dict) or set(obj) != {"bucket", "key", "version_id", "sha256"}:
        raise ValueError("invalid gate archive identity")
    if any(not isinstance(obj[k], str) or not obj[k] for k in ("bucket", "key", "version_id")) or obj["version_id"] == "null":
        raise ValueError("gate requires immutable archive version")
    _hex(obj["sha256"], 64, "gate archive digest")
    return {
        "schema": "leaf.native-release.v1", "provider": "aws.codebuild",
        "source_revision": source, "source_tree": tree, "producer": identity,
        "services": services, "solver": dict(solver),
        "web": {"member": "web-dist.zip", "artifact_sha256": web["artifact_sha256"],
                "archive_sha256": web["archive_sha256"]},
        "gate": json.loads(json.dumps(gate)),
    }


def stage_release(output_dir: Path, **inputs) -> dict:
    """Write the exact two members for a CodeBuild-managed ZIP artifact.

    The buildspec must publish only this directory with packaging ZIP. AWS
    owns the enclosing archive digest and version after build completion.
    Independent readiness remains a consumer control, not a manifest boolean.
    """
    manifest = assemble_release(**inputs)
    payload = Path(inputs["web"]["path"]).read_bytes()
    if hashlib.sha256(payload).hexdigest() != manifest["web"]["archive_sha256"]:
        raise ValueError("web archive changed before staging")
    encoded = (json.dumps(manifest, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")
    output_dir.mkdir(parents=True, exist_ok=False)
    # No provider publication happens here. An interrupted directory is never
    # reused on a retry, and a failed build cannot be admitted by the consumer.
    (output_dir / "web-dist.zip").write_bytes(payload)
    (output_dir / "staging-supply-set.json").write_bytes(encoded)
    return {name: {"size": len(data), "sha256": hashlib.sha256(data).hexdigest()}
            for name, data in (("web-dist.zip", payload), ("staging-supply-set.json", encoded))}


def run_gate(root: Path, results_dir: Path, *, env: dict[str, str],
             timeout_seconds: int = 2700) -> Path:
    """Run all eight canonical shards and require an emitted tree-bound proof.

    Run this in the dedicated test project, not the image-publishing project.
    Its IAM role may write test artifacts but must not write trusted releases
    or ECR. The caller supplies the test environment and runtime dependencies.
    This function makes no assertion that process env is an isolation boundary.
    """
    if type(timeout_seconds) is not int or timeout_seconds <= 0:
        raise ValueError("gate timeout must be positive")
    root = root.resolve()
    results_dir = results_dir.resolve()
    results_dir.mkdir(parents=True, exist_ok=False)
    proof = results_dir / "gate-proof.json"
    deadline = time.monotonic() + timeout_seconds

    def run(command, *, check=True, capture_output=False):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("native gate exceeded total runtime bound")
        return subprocess.run(command, cwd=root, env=env, check=check,
                              capture_output=capture_output, text=True,
                              timeout=remaining)

    tree = run(["git", "rev-parse", "HEAD^{tree}"], capture_output=True).stdout.strip()
    if not re.fullmatch(r"[0-9a-f]{40}", tree):
        raise ValueError("gate checkout has no exact tree")
    runner = [sys.executable, "scripts/run-all-gates.py"]
    failures = []
    # Serial execution preserves suite filesystem isolation on one checkout.
    # The single deadline caps the complete gate, not each shard independently.
    for shard in range(8):
        result = run(runner + ["--retry", "1", "--shard-count", "8",
                              "--shard-index", str(shard), "--result-json",
                              str(results_dir / f"shard-{shard}.json"),
                              "--log-dir", str(results_dir / f"logs-{shard}")], check=False)
        if result.returncode != 0:
            failures.append(shard)
    # The canonical verifier owns partition/catalog completeness and the proof
    # format. Do not replace it with a summary of subprocess return codes.
    run(runner + ["--verify-shard-results", str(results_dir), "--emit-proof", str(proof)])
    if failures:
        raise ValueError(f"native gate shards failed: {failures}")
    if not proof.is_file():
        raise ValueError("canonical gate did not emit a proof")
    run(runner + ["--verify-gate-proof", str(proof), "--expect-tree", tree])
    return proof


def image_build_command(service: str, source: str, build_number: int,
                        freshness: dict[str, str], metadata: Path, *,
                        solver_revision: str | None = None) -> list[str]:
    """Construct the current canonical image recipe for one native build.

    Native tags do not move production aliases or reuse GitHub run IDs. Every
    required freshness hash must be resolved by the caller before this step.
    """
    if service not in SERVICES or not re.fullmatch(r"[0-9a-f]{40}", source):
        raise ValueError("invalid service or exact source")
    if type(build_number) is not int or build_number <= 0:
        raise ValueError("native build number must be positive")
    required = FRESHNESS.get(service, TRIXIE)
    if set(freshness) != set(required) or any(
        not isinstance(value, str) or not re.fullmatch(r"[0-9a-f]{64}", value)
        for value in freshness.values()
    ):
        raise ValueError("package freshness inputs do not match this service")
    command = ["docker", "buildx", "build", "--pull", "--platform", "linux/amd64",
               "--file", f"deploy/Dockerfile.{service}",
               "--metadata-file", str(metadata), "--build-arg", f"LEAF_SOURCE_SHA={source}"]
    for name in required:
        command += ["--build-arg", f"{name}={freshness[name]}"]
    for base in BASE_IMAGES:
        command += ["--build-context", f"{base}=docker-image://{REGISTRY}/public-ecr/docker/library/{base}"]
    if service == "canonical-worker":
        if not isinstance(solver_revision, str) or not re.fullmatch(r"[0-9a-f]{40}", solver_revision):
            raise ValueError("canonical worker requires exact solver source")
        command += ["--build-arg", f"AUTOFILL_SOLVER_REVISION={solver_revision}",
                    "--build-context", "autofill_solver=./autofill-solver"]
    elif solver_revision is not None:
        raise ValueError("solver source is only valid for canonical worker")
    if service == "app":
        command += ["--output", "type=image,push=true,compression=zstd,force-compression=true,oci-mediatypes=true"]
    else:
        command += ["--push"]
    command += ["--tag", f"{REGISTRY}/leaf-platform-{service}:native-{build_number}-{source}", "."]
    return command


def build_image(root: Path, service: str, source: str, build_number: int,
                freshness: dict[str, str], metadata: Path, *,
                solver_revision: str | None = None) -> dict:
    """Execute one admitted image build and return its exact pushed digest.

    The caller owns authentication, approval, complete source/gate admission,
    solver content hash verification, and the overall bounded release lifetime.
    This function never discovers credentials or launches another AWS build.
    """
    command = image_build_command(service, source, build_number, freshness,
                                  metadata, solver_revision=solver_revision)
    actual = subprocess.run(["git", "rev-parse", "HEAD"], cwd=root, check=True,
                            text=True, capture_output=True, timeout=10).stdout.strip()
    if actual != source:
        raise ValueError("checkout differs from admitted source")
    if service == "canonical-worker":
        actual_solver = subprocess.run(
            ["git", "-C", "autofill-solver", "rev-parse", "HEAD"], cwd=root,
            check=True, text=True, capture_output=True, timeout=10,
        ).stdout.strip()
        if actual_solver != solver_revision:
            raise ValueError("solver checkout differs from admitted source")
    # An exclusive destination prevents a failed build from consuming stale
    # successful metadata left by another attempt. Buildx owns the subsequent write.
    metadata = metadata if metadata.is_absolute() else root / metadata
    with metadata.open("x", encoding="utf-8") as stream:
        stream.write("{}")
    subprocess.run(command, cwd=root, check=True, timeout=45 * 60)
    result = json.loads(metadata.read_text(encoding="utf-8"))
    digest = result.get("containerimage.digest")
    if not isinstance(digest, str) or not re.fullmatch(r"sha256:[0-9a-f]{64}", digest):
        raise ValueError("buildx did not return one immutable image digest")
    return {"repository": f"leaf-platform-{service}", "image_digest": digest,
            "source_revision": source, "native_build_number": build_number}


def package_web_image(root: Path, image_digest: str, source: str,
                      output_dir: Path) -> dict:
    """Package the exact pushed web image's files, including its compiled engine.

    The temporary container is never started. Only its static web files are
    copied. A second npm build cannot silently create a different release.
    The canonical archive writer preserves the existing web ZIP wire format.
    """
    if (not re.fullmatch(r"sha256:[0-9a-f]{64}", image_digest)
            or not re.fullmatch(r"[0-9a-f]{40}", source)):
        raise ValueError("web package requires exact image and source")
    root, output_dir = root.resolve(), output_dir.resolve()
    output_dir.mkdir(parents=True, exist_ok=False)
    dist = output_dir / "dist"
    dist.mkdir()
    image = f"{REGISTRY}/leaf-platform-web@{image_digest}"
    subprocess.run(["docker", "pull", "--platform", "linux/amd64", image],
                   cwd=root, check=True, timeout=600)
    container = subprocess.run(
        ["docker", "create", "--platform", "linux/amd64", "--entrypoint", "/bin/true", image],
        cwd=root, check=True, text=True, capture_output=True, timeout=60,
    ).stdout.strip()
    if not re.fullmatch(r"[0-9a-f]{64}", container):
        raise ValueError("docker did not return an exact temporary container ID")
    try:
        subprocess.run(["docker", "cp", f"{container}:/usr/share/nginx/html/.", str(dist)],
                       cwd=root, check=True, timeout=120)
    finally:
        subprocess.run(["docker", "rm", container], cwd=root, check=True, timeout=60)
    health = json.loads((dist / "health.json").read_text(encoding="utf-8"))
    if health.get("source_sha") != source or health.get("ok") is not True:
        raise ValueError("web image files do not carry the admitted source")
    archive = output_dir / "web-dist.zip"
    result = subprocess.run(
        [sys.executable, "scripts/platform_release_manifest.py", "pack-web-dist",
         "--root", str(dist), "--output", str(archive)],
        cwd=root, check=True, text=True, capture_output=True, timeout=120,
    )
    receipt = json.loads(result.stdout)
    if set(receipt) != {"artifact_sha256", "archive_sha256"} or any(
        not isinstance(value, str) or not re.fullmatch(r"[0-9a-f]{64}", value)
        for value in receipt.values()
    ):
        raise ValueError("canonical web packer returned invalid identity")
    return dict(receipt, path=str(archive), image_digest=image_digest, source_revision=source)
