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
