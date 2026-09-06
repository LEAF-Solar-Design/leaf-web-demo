"""Native Studio release producer building blocks.

No provider is provisioned and nothing runs on import. The release controller
must establish source admission and gate success before calling build_image.
This is not yet the complete release entry point or a deployment command.
"""
from __future__ import annotations

import json
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
