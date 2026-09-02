#!/usr/bin/env python3
"""Generate a staging supply set and verify its production handoff candidate."""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
from pathlib import Path
from pathlib import PurePosixPath
import re
import shlex
import stat
import subprocess
from typing import Any, Sequence
import zipfile


SCHEMA = "leaf.staging-supply-set.v1"
# v2 = v1 plus tree-identity provenance (operator decision D3, 2026-08-05):
# the merge run that ADOPTS speculative PR-built images re-stamps the supply
# set with the git tree hash both builds realize and the run that built the
# digests. Deployable consumers (the staging relay, the production handoff
# verifier) accept BOTH schemas, because a rerun of a pre-v2 build run
# regenerates its artifact from the old workflow and must keep dispatching.
SCHEMA_V2 = "leaf.staging-supply-set.v2"
SCHEMA_V3 = "leaf.staging-supply-set.v3"
ACCEPTED_SCHEMAS = (SCHEMA, SCHEMA_V2, SCHEMA_V3)
SPECULATIVE_SCHEMA = "leaf.speculative-supply-set.v1"
HANDOFF_SCHEMA = "leaf.production-handoff-candidate.v1"
SURFACE_PREDICATE_TYPE = "https://leafdesign.ai/attestations/platform-surface/v1"
BUILD_WORKFLOW_PATH = ".github/workflows/build-platform-images.yml"
WEB_RESTAMP_SCHEMA = "leaf.web-source-restamp.v1"
WEB_RESTAMP_ALGORITHM = "leaf.web-source-restamp.v1"
WEB_HEALTH_PATH = "dist/health.json"
SERVICES = ("app", "broker", "canonical-worker", "harness", "web")
REPOSITORIES = {name: f"leaf-platform-{name}" for name in SERVICES}
_SHA = re.compile(r"^[0-9a-f]{40}$")
_TREE = re.compile(r"^[0-9a-f]{40}$")
_DIGEST = re.compile(r"^sha256:[0-9a-f]{64}$")
_SOURCE_HASH = re.compile(r"^[0-9a-f]{64}$")
_TAG = re.compile(r"^(?:prod-[0-9a-f]{7,40}|sha-[0-9a-f]{40})$")
# tree + the merge-preview commit that baked the images: two previews can
# realize the SAME tree while baking different LEAF_SOURCE_SHA values, so a
# tree-only tag would let a later run mint provenance for images an earlier
# run built (sol-critic round 1 on PR #450, finding 1).
_SPEC_TAG = re.compile(r"^spec-[0-9a-f]{40}-[0-9a-f]{12}$")
_RUN_ID = re.compile(r"^[1-9][0-9]{5,19}$")
_RUN_ATTEMPT = re.compile(r"^[1-9][0-9]*$")
_RECEIPT_RUN_ID = re.compile(r"^[a-z0-9][a-z0-9-]{5,49}$")
_PR_NUMBER = re.compile(r"^[1-9][0-9]{0,9}$")
_LOOKUP_TAG = re.compile(
    r"^(?:prod-[0-9a-f]{7,40}|sha-[0-9a-f]{40}|surface-v1-[0-9a-f]{64})$"
)
_ECR_SUBJECT = re.compile(
    r"^807034087062\.dkr\.ecr\.us-east-1\.amazonaws\.com/"
    r"leaf-platform-(?:app|broker|canonical-worker|harness|web)$"
)


SURFACE_INPUTS: dict[str, tuple[str, ...]] = {
    "app": (
        ".dockerignore",
        "deploy/Dockerfile.app",
        "deploy/gen_seccomp_filter.c",
        "server",
        "da",
        "engine",
        "platform",
        "contract",
        "data",
        "scripts/reconcile_customization_authority.py",
        "scripts/reconcile_sessions_authority.py",
    ),
    "broker": (
        ".dockerignore",
        "deploy/Dockerfile.broker",
        "server",
        "da",
        "engine",
        "platform",
        "contract",
        "data",
        "harness/scripts/e2b-tool-exec.mjs",
    ),
    "canonical-worker": (
        ".dockerignore",
        "deploy/Dockerfile.canonical-worker",
        "server",
        "platform",
        "scripts/canonical-container-smoke.py",
        "deploy/autofill-solver-sources.json",
    ),
    "harness": (
        ".dockerignore",
        "deploy/Dockerfile.harness",
        "harness",
        "server/glug_adoption_manifest.json",
    ),
    "web": (
        ".dockerignore",
        "deploy/Dockerfile.web",
        "deploy/nginx.conf",
        # W3 one-shell rail: the entrypoint drop-in rewrites runtime-flags.js
        # at container boot from the task-definition env - an edit changes
        # what every environment serves, so it must move the fingerprint.
        "deploy/write-runtime-flags.sh",
        "web",
        # Card F-3: the web image now compiles the CAD engine in its own
        # build stage — the vendored wrapper crate and the stage script are
        # therefore release-relevant inputs whose content must move the
        # fingerprint (a wrapper edit ships different wasm bytes).
        "vendor/acadrust-worker",
        "scripts/stage_cad_engine.mjs",
    ),
}


class ContractError(ValueError):
    """The supplied release evidence does not satisfy the frozen contract."""


def _object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            raise ContractError(f"duplicate JSON key: {key}")
        value[key] = item
    return value


def load_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"), object_pairs_hook=_object)
    except (OSError, json.JSONDecodeError) as exc:
        raise ContractError(f"cannot read JSON contract: {path}") from exc
    if not isinstance(value, dict):
        raise ContractError("contract root must be an object")
    return value


def _exact_keys(value: dict[str, Any], expected: set[str], label: str) -> None:
    if set(value) != expected:
        raise ContractError(f"{label} has unsupported or missing fields")


def _canonical_sha256(value: Any) -> str:
    encoded = json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=True
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _git(repo_root: Path, *arguments: str) -> str:
    try:
        result = subprocess.run(
            ["git", "-C", str(repo_root), *arguments],
            check=True,
            capture_output=True,
            text=True,
        )
    except (OSError, subprocess.CalledProcessError) as exc:
        raise ContractError("cannot resolve the exact git surface") from exc
    return result.stdout.strip()


def _pairs(values: Sequence[str], label: str) -> dict[str, str]:
    pairs: dict[str, str] = {}
    for value in values:
        name, separator, item = value.partition("=")
        if not separator or not name or not item or name in pairs:
            raise ContractError(f"each {label} must be one unique name=value pair")
        pairs[name] = item
    return pairs


def _surface_blobs(repo_root: Path, revision: str, service: str) -> dict[str, str]:
    if service not in SURFACE_INPUTS:
        raise ContractError("surface service is not canonical")
    if not _SHA.fullmatch(revision):
        raise ContractError("surface revision must be a full lowercase commit SHA")
    resolved = _git(repo_root, "rev-parse", revision)
    if resolved != revision:
        raise ContractError("surface revision did not resolve exactly")
    blobs: dict[str, str] = {}
    for source in SURFACE_INPUTS[service]:
        listing = _git(repo_root, "ls-tree", "-r", revision, "--", source)
        if not listing:
            raise ContractError(f"surface input is absent from git: {source}")
        for line in listing.splitlines():
            metadata, separator, path = line.partition("\t")
            parts = metadata.split()
            if not separator or len(parts) != 3 or parts[1] != "blob":
                raise ContractError("surface git listing is malformed")
            mode, _kind, blob = parts
            if mode == "160000" or not _SHA.fullmatch(blob) or path in blobs:
                raise ContractError("surface contains an unsupported or duplicate input")
            blobs[path] = blob
    return dict(sorted(blobs.items()))


def _dockerfile_contract(
    repo_root: Path, revision: str, service: str
) -> tuple[dict[str, str], set[str]]:
    dockerfile = _git(repo_root, "show", f"{revision}:deploy/Dockerfile.{service}")
    logical: list[str] = []
    pending = ""
    for raw in dockerfile.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        pending = f"{pending} {line}".strip()
        if pending.endswith("\\"):
            pending = pending[:-1].rstrip()
            continue
        logical.append(pending)
        pending = ""
    if pending:
        raise ContractError("Dockerfile ends with an incomplete instruction")

    declared = SURFACE_INPUTS[service]
    aliases: set[str] = set()
    external_images: set[str] = set()
    args: dict[str, str] = {}
    for instruction in logical:
        upper = instruction.upper()
        if upper.startswith("FROM "):
            tokens = shlex.split(instruction)
            if len(tokens) < 2:
                raise ContractError("Dockerfile FROM instruction is malformed")
            external_images.add(tokens[1])
            if len(tokens) >= 4 and tokens[-2].upper() == "AS":
                aliases.add(tokens[-1])
        elif upper.startswith("ARG "):
            token = instruction[4:].strip()
            name, separator, default = token.partition("=")
            if not name or any(character.isspace() for character in name):
                raise ContractError("Dockerfile ARG instruction is malformed")
            if name != "LEAF_SOURCE_SHA":
                args[name] = default if separator else ""
        elif upper.startswith(("COPY ", "ADD ")):
            tokens = shlex.split(instruction)
            flags = [token for token in tokens[1:] if token.startswith("--")]
            from_flags = [token for token in flags if token.startswith("--from=")]
            if from_flags:
                if len(from_flags) != 1:
                    raise ContractError("Dockerfile copy source is ambiguous")
                source = from_flags[0].split("=", 1)[1]
                if source not in aliases and source != "autofill_solver":
                    external_images.add(source)
                continue
            values = [token for token in tokens[1:] if not token.startswith("--")]
            if len(values) < 2:
                raise ContractError("Dockerfile local copy is malformed")
            for source in values[:-1]:
                normalized = source.rstrip("/")
                covered = any(
                    normalized == root.rstrip("/")
                    or normalized.startswith(root.rstrip("/") + "/")
                    for root in declared
                )
                if not covered:
                    raise ContractError(
                        f"Dockerfile local input is not fingerprinted: {source}"
                    )
    return args, external_images


def build_surface_fingerprint(
    repo_root: Path,
    revision: str,
    service: str,
    base_images: dict[str, str],
    build_args: dict[str, str],
    *,
    buildkit_version: str,
    platform: str,
    solver_revision: str | None = None,
    solver_source_sha256: str | None = None,
) -> dict[str, Any]:
    if not buildkit_version or not platform:
        raise ContractError("builder version and platform are required")
    docker_args, external_images = _dockerfile_contract(
        repo_root, revision, service
    )
    if set(base_images) != external_images or any(
        not _DIGEST.fullmatch(value) for value in base_images.values()
    ):
        raise ContractError("every resolved base image must be an immutable digest")
    if "LEAF_SOURCE_SHA" in build_args:
        raise ContractError("release coordinator SHA is not a surface build argument")
    effective_args = dict(docker_args)
    for name, value in build_args.items():
        if name not in docker_args:
            raise ContractError(f"undeclared surface build argument: {name}")
        effective_args[name] = value
    if service == "canonical-worker":
        if not solver_revision or not _SHA.fullmatch(solver_revision):
            raise ContractError("canonical-worker solver revision is invalid")
        if not solver_source_sha256 or not _SOURCE_HASH.fullmatch(
            solver_source_sha256
        ):
            raise ContractError("canonical-worker solver source hash is invalid")
    elif solver_revision is not None or solver_source_sha256 is not None:
        raise ContractError("solver provenance applies only to canonical-worker")
    blobs = _surface_blobs(repo_root, revision, service)
    recipe: dict[str, Any] = {
        "build_args": dict(sorted(effective_args.items())),
        "buildkit_version": buildkit_version,
        "dockerfile_blob": blobs[f"deploy/Dockerfile.{service}"],
        "platform": platform,
        "resolved_base_images": dict(sorted(base_images.items())),
    }
    if service == "canonical-worker":
        recipe["solver"] = {
            "source_revision": solver_revision,
            "source_sha256": solver_source_sha256,
        }
    recipe_fingerprint = _canonical_sha256(recipe)
    surface = {
        "blobs": blobs,
        "recipe_fingerprint": recipe_fingerprint,
        "service": service,
    }
    migration_paths = {
        path: blob
        for path, blob in blobs.items()
        if path == "platform/db.py"
        or path.startswith("platform/migrations/")
        or path.startswith("platform/migration")
    }
    return {
        "schema": "leaf.platform-surface-fingerprint.v1",
        "service": service,
        "source_revision": revision,
        "surface_fingerprint": _canonical_sha256(surface),
        "recipe_fingerprint": recipe_fingerprint,
        "migration_fingerprint": (
            _canonical_sha256(migration_paths) if service == "app" else "not_app"
        ),
        "inputs": blobs,
        "recipe": recipe,
    }


def build_manifest(
    source_revision: str,
    build_tag: str,
    image_digests: dict[str, str],
    solver_revision: str,
    solver_source_sha256: str,
    web_artifact_sha256: str,
    speculative: dict[str, Any] | None = None,
) -> dict[str, Any]:
    if not _SHA.fullmatch(source_revision):
        raise ContractError("source revision must be a full lowercase commit SHA")
    if not _TAG.fullmatch(build_tag):
        raise ContractError("build tag is not an approved immutable lookup tag")
    if set(image_digests) != set(SERVICES):
        raise ContractError("release must contain exactly five logical services")
    if not _SHA.fullmatch(solver_revision):
        raise ContractError("solver revision must be a full lowercase commit SHA")
    if not _SOURCE_HASH.fullmatch(solver_source_sha256):
        raise ContractError("solver source hash must be lowercase SHA-256")
    if not _SOURCE_HASH.fullmatch(web_artifact_sha256):
        raise ContractError("web artifact hash must be lowercase SHA-256")

    services: dict[str, Any] = {}
    for name in SERVICES:
        digest = image_digests[name]
        if not _DIGEST.fullmatch(digest):
            raise ContractError(f"{name} image digest is not immutable")
        service: dict[str, Any] = {
            "repository": REPOSITORIES[name],
            "image_digest": digest,
            "source_revision": source_revision,
        }
        if name == "canonical-worker":
            service["provenance"] = {
                "application_source_revision": source_revision,
                "solver_source_revision": solver_revision,
                "solver_source_sha256": solver_source_sha256,
            }
        if name == "web":
            service["artifact_sha256"] = web_artifact_sha256
        services[name] = service
    manifest: dict[str, Any] = {
        "schema": SCHEMA,
        "source_revision": source_revision,
        "build_tag": build_tag,
        "services": services,
    }
    if speculative is not None:
        manifest["schema"] = SCHEMA_V2
        manifest["source_tree"] = speculative["source_tree"]
        manifest["speculative"] = {
            "built_from_revision": speculative["built_from_revision"],
            "pr_number": speculative["pr_number"],
            "workflow_run_id": speculative["workflow_run_id"],
        }
    return manifest


_V3_SERVICE_FIELDS = {
    "repository",
    "image_digest",
    "immutable_lookup_tag",
    "producer_source_revision",
    "producer_source_tree",
    "surface_fingerprint",
    "recipe_fingerprint",
    "producer_workflow_path",
    "producer_workflow_blob",
    "producer_run_id",
    "producer_run_attempt",
    "provenance_subject",
    "provenance_digest",
    "build_disposition",
}


def build_v3_manifest(
    release_source_revision: str,
    release_source_tree: str,
    build_run_id: str,
    build_run_attempt: str,
    services: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    manifest = {
        "schema": SCHEMA_V3,
        "release_source_revision": release_source_revision,
        "release_source_tree": release_source_tree,
        "build_run_id": int(build_run_id) if _RUN_ID.fullmatch(build_run_id) else 0,
        "build_run_attempt": (
            int(build_run_attempt) if _RUN_ATTEMPT.fullmatch(build_run_attempt) else 0
        ),
        "services": services,
    }
    validate_v3_manifest(manifest)
    return manifest


def re_envelope_speculative_v3_manifest(
    manifest: dict[str, Any],
    *,
    web_restamp_receipt: dict[str, Any],
    speculative_run_id: str,
    speculative_run_attempt: str,
    expected_candidate_tree: str,
    expected_workflow_blob: str,
    release_source_revision: str,
    release_source_tree: str,
    build_run_id: str,
    build_run_attempt: str,
) -> dict[str, Any]:
    """Bind one speculative v3 producer artifact to its merged main release.

    The immutable image and producer fields stay producer-owned. The adopter
    records reuse and replaces only the web artifact hash proved by the
    source-restamp receipt, so it cannot claim the main run built the images.
    """

    validate_v3_manifest(manifest)
    if not _RUN_ID.fullmatch(speculative_run_id) or not _RUN_ATTEMPT.fullmatch(
        speculative_run_attempt
    ):
        raise ContractError("speculative v3 provider run identity is invalid")
    if not _TREE.fullmatch(expected_candidate_tree):
        raise ContractError("speculative v3 candidate tree is invalid")
    if not _SHA.fullmatch(expected_workflow_blob):
        raise ContractError("speculative v3 workflow blob is invalid")
    if release_source_tree != expected_candidate_tree:
        raise ContractError("speculative v3 candidate tree does not equal main tree")
    if manifest["build_run_id"] != int(speculative_run_id) or manifest[
        "build_run_attempt"
    ] != int(speculative_run_attempt):
        raise ContractError("speculative v3 manifest is not bound to its provider run")
    candidate_source = manifest["release_source_revision"]
    candidate_tree = manifest["release_source_tree"]
    if candidate_tree != expected_candidate_tree:
        raise ContractError("speculative v3 candidate tree does not match main")
    for name in SERVICES:
        service = manifest["services"][name]
        if (
            service["producer_source_revision"] != candidate_source
            or service["producer_source_tree"] != candidate_tree
            or service["producer_run_id"] != int(speculative_run_id)
            or service["producer_run_attempt"] != int(speculative_run_attempt)
            or service["producer_workflow_blob"] != expected_workflow_blob
            or service["build_disposition"] != "built"
        ):
            raise ContractError(
                f"{name} speculative v3 evidence is rebound or not producer-owned"
            )
    validate_web_restamp_receipt(web_restamp_receipt)
    speculative_web_hash = manifest["services"]["web"]["artifact_sha256"]
    if (
        web_restamp_receipt["old_source_revision"] != candidate_source
        or web_restamp_receipt["new_source_revision"] != release_source_revision
        or web_restamp_receipt["common_tree"] != release_source_tree
        or web_restamp_receipt["old_artifact_sha256"] != speculative_web_hash
    ):
        raise ContractError("web source-restamp receipt does not bind this release")
    services = copy.deepcopy(manifest["services"])
    for service in services.values():
        service["build_disposition"] = "reused"
    services["web"]["artifact_sha256"] = web_restamp_receipt[
        "new_artifact_sha256"
    ]
    return build_v3_manifest(
        release_source_revision,
        release_source_tree,
        build_run_id,
        build_run_attempt,
        services,
    )


def validate_v3_manifest(manifest: dict[str, Any]) -> None:
    _exact_keys(
        manifest,
        {
            "schema",
            "release_source_revision",
            "release_source_tree",
            "build_run_id",
            "build_run_attempt",
            "services",
        },
        "v3 manifest",
    )
    if manifest["schema"] != SCHEMA_V3:
        raise ContractError("unsupported v3 release manifest schema")
    release_source = manifest["release_source_revision"]
    release_tree = manifest["release_source_tree"]
    if not isinstance(release_source, str) or not _SHA.fullmatch(release_source):
        raise ContractError("v3 release source revision is invalid")
    if not isinstance(release_tree, str) or not _TREE.fullmatch(release_tree):
        raise ContractError("v3 release source tree is invalid")
    if (
        not isinstance(manifest["build_run_id"], int)
        or not _RUN_ID.fullmatch(str(manifest["build_run_id"]))
        or not isinstance(manifest["build_run_attempt"], int)
        or not _RUN_ATTEMPT.fullmatch(str(manifest["build_run_attempt"]))
    ):
        raise ContractError("v3 build run identity is invalid")
    services = manifest["services"]
    if not isinstance(services, dict) or set(services) != set(SERVICES):
        raise ContractError("v3 manifest must contain exactly five services")
    for name in SERVICES:
        service = services[name]
        if not isinstance(service, dict):
            raise ContractError(f"{name} v3 entry must be an object")
        fields = set(_V3_SERVICE_FIELDS)
        if name == "canonical-worker":
            fields.add("solver_provenance")
        if name == "web":
            fields.add("artifact_sha256")
        _exact_keys(service, fields, f"{name} v3 entry")
        if service["repository"] != REPOSITORIES[name]:
            raise ContractError(f"{name} v3 repository is not canonical")
        if not isinstance(service["image_digest"], str) or not _DIGEST.fullmatch(
            service["image_digest"]
        ):
            raise ContractError(f"{name} v3 digest is not immutable")
        if not isinstance(service["immutable_lookup_tag"], str) or not (
            _LOOKUP_TAG.fullmatch(service["immutable_lookup_tag"])
        ):
            raise ContractError(f"{name} v3 lookup tag is invalid")
        for field in ("producer_source_revision", "producer_source_tree", "producer_workflow_blob"):
            value = service[field]
            if not isinstance(value, str) or not _SHA.fullmatch(value):
                raise ContractError(f"{name} v3 {field} is invalid")
        if service["producer_workflow_path"] != BUILD_WORKFLOW_PATH:
            raise ContractError(f"{name} v3 producer workflow is not canonical")
        for field in ("surface_fingerprint", "recipe_fingerprint"):
            value = service[field]
            if not isinstance(value, str) or not _SOURCE_HASH.fullmatch(value):
                raise ContractError(f"{name} v3 {field} is invalid")
        expected_lookup_tag = f"surface-v1-{service['surface_fingerprint']}"
        if service["immutable_lookup_tag"] != expected_lookup_tag:
            raise ContractError(
                f"{name} v3 lookup tag does not bind its surface fingerprint"
            )
        if (
            not isinstance(service["producer_run_id"], int)
            or not _RUN_ID.fullmatch(str(service["producer_run_id"]))
            or not isinstance(service["producer_run_attempt"], int)
            or not _RUN_ATTEMPT.fullmatch(str(service["producer_run_attempt"]))
        ):
            raise ContractError(f"{name} v3 producer run is invalid")
        expected_subject = (
            "807034087062.dkr.ecr.us-east-1.amazonaws.com/"
            f"{REPOSITORIES[name]}"
        )
        if service["provenance_subject"] != expected_subject or not (
            _ECR_SUBJECT.fullmatch(service["provenance_subject"])
        ):
            raise ContractError(f"{name} v3 provenance subject is invalid")
        if not isinstance(service["provenance_digest"], str) or not (
            _DIGEST.fullmatch(service["provenance_digest"])
        ):
            raise ContractError(f"{name} v3 provenance digest is invalid")
        if service["build_disposition"] not in {"built", "reused"}:
            raise ContractError(f"{name} v3 build disposition is invalid")
        if name == "web" and (
            not isinstance(service["artifact_sha256"], str)
            or not _SOURCE_HASH.fullmatch(service["artifact_sha256"])
        ):
            raise ContractError("web v3 artifact hash is invalid")
    worker = services["canonical-worker"]["solver_provenance"]
    if not isinstance(worker, dict):
        raise ContractError("canonical-worker v3 solver provenance is invalid")
    _exact_keys(
        worker,
        {"solver_source_revision", "solver_source_sha256"},
        "canonical-worker v3 solver provenance",
    )
    if not isinstance(worker["solver_source_revision"], str) or not _SHA.fullmatch(
        worker["solver_source_revision"]
    ):
        raise ContractError("canonical-worker v3 solver revision is invalid")
    if not isinstance(worker["solver_source_sha256"], str) or not (
        _SOURCE_HASH.fullmatch(worker["solver_source_sha256"])
    ):
        raise ContractError("canonical-worker v3 solver source hash is invalid")


def build_surface_predicate(
    fingerprint: dict[str, Any],
    *,
    repository: str,
    image_digest: str,
    source_tree: str,
    workflow_blob: str,
    run_id: str,
    run_attempt: str,
    artifact_sha256: str | None = None,
) -> dict[str, Any]:
    service = fingerprint.get("service")
    if service not in SERVICES or repository != REPOSITORIES[service]:
        raise ContractError("surface predicate repository is not canonical")
    for key in ("surface_fingerprint", "recipe_fingerprint"):
        if not isinstance(fingerprint.get(key), str) or not _SOURCE_HASH.fullmatch(
            fingerprint[key]
        ):
            raise ContractError("surface predicate fingerprint is invalid")
    source = fingerprint.get("source_revision")
    if not isinstance(source, str) or not _SHA.fullmatch(source):
        raise ContractError("surface predicate source revision is invalid")
    if not _DIGEST.fullmatch(image_digest):
        raise ContractError("surface predicate image digest is invalid")
    if not _TREE.fullmatch(source_tree) or not _SHA.fullmatch(workflow_blob):
        raise ContractError("surface predicate tree or workflow blob is invalid")
    if not _RUN_ID.fullmatch(run_id) or not _RUN_ATTEMPT.fullmatch(run_attempt):
        raise ContractError("surface predicate run identity is invalid")
    predicate: dict[str, Any] = {
        "schema": "leaf.platform-surface-provenance.v1",
        "service": service,
        "repository": repository,
        "image_digest": image_digest,
        "producer_source_revision": source,
        "producer_source_tree": source_tree,
        "surface_fingerprint": fingerprint["surface_fingerprint"],
        "recipe_fingerprint": fingerprint["recipe_fingerprint"],
        "migration_fingerprint": fingerprint["migration_fingerprint"],
        "producer_workflow_path": BUILD_WORKFLOW_PATH,
        "producer_workflow_blob": workflow_blob,
        "producer_run_id": int(run_id),
        "producer_run_attempt": int(run_attempt),
    }
    if service == "canonical-worker":
        predicate["solver_provenance"] = {
            "solver_source_revision": fingerprint["recipe"]["solver"][
                "source_revision"
            ],
            "solver_source_sha256": fingerprint["recipe"]["solver"][
                "source_sha256"
            ],
        }
    if service == "web":
        if not artifact_sha256 or not _SOURCE_HASH.fullmatch(artifact_sha256):
            raise ContractError("web surface predicate artifact hash is invalid")
        predicate["artifact_sha256"] = artifact_sha256
    elif artifact_sha256 is not None:
        raise ContractError("artifact hash applies only to the web surface")
    validate_surface_predicate(predicate)
    return predicate


_SURFACE_PREDICATE_FIELDS = {
    "schema",
    "service",
    "repository",
    "image_digest",
    "producer_source_revision",
    "producer_source_tree",
    "surface_fingerprint",
    "recipe_fingerprint",
    "migration_fingerprint",
    "producer_workflow_path",
    "producer_workflow_blob",
    "producer_run_id",
    "producer_run_attempt",
}


def validate_surface_predicate(predicate: dict[str, Any]) -> None:
    service = predicate.get("service")
    if service not in SERVICES:
        raise ContractError("surface predicate service is not canonical")
    fields = set(_SURFACE_PREDICATE_FIELDS)
    if service == "canonical-worker":
        fields.add("solver_provenance")
    if service == "web":
        fields.add("artifact_sha256")
    _exact_keys(predicate, fields, "surface predicate")
    if predicate["schema"] != "leaf.platform-surface-provenance.v1":
        raise ContractError("surface predicate schema is invalid")
    if predicate["repository"] != REPOSITORIES[service]:
        raise ContractError("surface predicate repository is not canonical")
    if not isinstance(predicate["image_digest"], str) or not _DIGEST.fullmatch(
        predicate["image_digest"]
    ):
        raise ContractError("surface predicate digest is invalid")
    for field in (
        "producer_source_revision",
        "producer_source_tree",
        "producer_workflow_blob",
    ):
        value = predicate[field]
        if not isinstance(value, str) or not _SHA.fullmatch(value):
            raise ContractError(f"surface predicate {field} is invalid")
    for field in ("surface_fingerprint", "recipe_fingerprint"):
        value = predicate[field]
        if not isinstance(value, str) or not _SOURCE_HASH.fullmatch(value):
            raise ContractError(f"surface predicate {field} is invalid")
    migration = predicate["migration_fingerprint"]
    if service == "app":
        if not isinstance(migration, str) or not _SOURCE_HASH.fullmatch(migration):
            raise ContractError("app surface migration fingerprint is invalid")
    elif migration != "not_app":
        raise ContractError("non-app surface migration fingerprint is invalid")
    if predicate["producer_workflow_path"] != BUILD_WORKFLOW_PATH:
        raise ContractError("surface predicate workflow is not canonical")
    if (
        not isinstance(predicate["producer_run_id"], int)
        or not _RUN_ID.fullmatch(str(predicate["producer_run_id"]))
        or not isinstance(predicate["producer_run_attempt"], int)
        or not _RUN_ATTEMPT.fullmatch(str(predicate["producer_run_attempt"]))
    ):
        raise ContractError("surface predicate run identity is invalid")
    if service == "canonical-worker":
        solver = predicate["solver_provenance"]
        if not isinstance(solver, dict):
            raise ContractError("surface predicate solver provenance is invalid")
        _exact_keys(
            solver,
            {"solver_source_revision", "solver_source_sha256"},
            "surface predicate solver provenance",
        )
        if not isinstance(solver["solver_source_revision"], str) or not (
            _SHA.fullmatch(solver["solver_source_revision"])
        ):
            raise ContractError("surface predicate solver revision is invalid")
        if not isinstance(solver["solver_source_sha256"], str) or not (
            _SOURCE_HASH.fullmatch(solver["solver_source_sha256"])
        ):
            raise ContractError("surface predicate solver source hash is invalid")
    if service == "web" and (
        not isinstance(predicate["artifact_sha256"], str)
        or not _SOURCE_HASH.fullmatch(predicate["artifact_sha256"])
    ):
        raise ContractError("surface predicate web artifact hash is invalid")


def validate_manifest(manifest: dict[str, Any]) -> None:
    schema = manifest.get("schema")
    if schema not in ACCEPTED_SCHEMAS:
        raise ContractError("unsupported release manifest schema")
    if schema == SCHEMA_V3:
        validate_v3_manifest(manifest)
        return
    top_level = {"schema", "source_revision", "build_tag", "services"}
    if schema == SCHEMA_V2:
        top_level |= {"source_tree", "speculative"}
    _exact_keys(manifest, top_level, "manifest")
    source = manifest["source_revision"]
    if not isinstance(source, str) or not _SHA.fullmatch(source):
        raise ContractError("release source revision is not a full commit SHA")
    tag = manifest["build_tag"]
    if not isinstance(tag, str) or not _TAG.fullmatch(tag):
        raise ContractError("release build tag is invalid")
    services = manifest["services"]
    if not isinstance(services, dict) or set(services) != set(SERVICES):
        raise ContractError("release manifest does not contain exactly five services")
    for name in SERVICES:
        service = services[name]
        if not isinstance(service, dict):
            raise ContractError(f"{name} release entry must be an object")
        fields = {"repository", "image_digest", "source_revision"}
        if name == "canonical-worker":
            fields.add("provenance")
        if name == "web":
            fields.add("artifact_sha256")
        _exact_keys(service, fields, f"{name} release entry")
        if service["repository"] != REPOSITORIES[name]:
            raise ContractError(f"{name} repository is not canonical")
        if not isinstance(service["image_digest"], str) or not _DIGEST.fullmatch(
            service["image_digest"]
        ):
            raise ContractError(f"{name} image digest is not immutable")
        if service["source_revision"] != source:
            raise ContractError(f"{name} source revision is mixed")
        if name == "web" and (
            not isinstance(service["artifact_sha256"], str)
            or not _SOURCE_HASH.fullmatch(service["artifact_sha256"])
        ):
            raise ContractError("web artifact hash is invalid")
    provenance = services["canonical-worker"]["provenance"]
    if not isinstance(provenance, dict):
        raise ContractError("canonical-worker provenance must be an object")
    _exact_keys(
        provenance,
        {
            "application_source_revision",
            "solver_source_revision",
            "solver_source_sha256",
        },
        "canonical-worker provenance",
    )
    if provenance["application_source_revision"] != source:
        raise ContractError("canonical-worker application provenance is mixed")
    if not isinstance(provenance["solver_source_revision"], str) or not _SHA.fullmatch(
        provenance["solver_source_revision"]
    ):
        raise ContractError("canonical-worker solver revision is invalid")
    if not isinstance(
        provenance["solver_source_sha256"], str
    ) or not _SOURCE_HASH.fullmatch(provenance["solver_source_sha256"]):
        raise ContractError("canonical-worker solver source hash is invalid")
    if schema == SCHEMA_V2:
        tree = manifest["source_tree"]
        if not isinstance(tree, str) or not _TREE.fullmatch(tree):
            raise ContractError("release source tree is not a full git tree hash")
        speculative = manifest["speculative"]
        if not isinstance(speculative, dict):
            raise ContractError("speculative provenance must be an object")
        _exact_keys(
            speculative,
            {"built_from_revision", "pr_number", "workflow_run_id"},
            "speculative provenance",
        )
        built_from = speculative["built_from_revision"]
        if not isinstance(built_from, str) or not _SHA.fullmatch(built_from):
            raise ContractError("speculative built-from revision is invalid")
        pr_number = speculative["pr_number"]
        if not isinstance(pr_number, int) or not _PR_NUMBER.fullmatch(str(pr_number)):
            raise ContractError("speculative PR number is invalid")
        run_id = speculative["workflow_run_id"]
        if not isinstance(run_id, int) or not _RUN_ID.fullmatch(str(run_id)):
            raise ContractError("speculative workflow run ID is invalid")


def build_speculative_manifest(
    source_tree: str,
    built_from_revision: str,
    pr_number: str,
    workflow_run_id: str,
    image_digests: dict[str, str],
    solver_revision: str,
    solver_source_sha256: str,
) -> dict[str, Any]:
    if not _TREE.fullmatch(source_tree):
        raise ContractError("speculative source tree must be a full git tree hash")
    if not _SHA.fullmatch(built_from_revision):
        raise ContractError("speculative built-from revision must be a full commit SHA")
    if not _PR_NUMBER.fullmatch(pr_number):
        raise ContractError("speculative PR number is invalid")
    if not _RUN_ID.fullmatch(workflow_run_id):
        raise ContractError("speculative workflow run ID is invalid")
    if set(image_digests) != set(SERVICES):
        raise ContractError("speculative set must contain exactly five services")
    if not _SHA.fullmatch(solver_revision):
        raise ContractError("solver revision must be a full lowercase commit SHA")
    if not _SOURCE_HASH.fullmatch(solver_source_sha256):
        raise ContractError("solver source hash must be lowercase SHA-256")
    services: dict[str, Any] = {}
    for name in SERVICES:
        digest = image_digests[name]
        if not _DIGEST.fullmatch(digest):
            raise ContractError(f"{name} speculative image digest is not immutable")
        services[name] = {
            "repository": REPOSITORIES[name],
            "image_digest": digest,
        }
    return {
        "schema": SPECULATIVE_SCHEMA,
        "source_tree": source_tree,
        "spec_tag": f"spec-{source_tree}-{built_from_revision[:12]}",
        "built_from_revision": built_from_revision,
        "pr_number": int(pr_number),
        "workflow_run_id": int(workflow_run_id),
        "solver": {
            "solver_source_revision": solver_revision,
            "solver_source_sha256": solver_source_sha256,
        },
        "services": services,
    }


def validate_speculative_manifest(
    manifest: dict[str, Any], expect_tree: str | None = None
) -> None:
    _exact_keys(
        manifest,
        {
            "schema",
            "source_tree",
            "spec_tag",
            "built_from_revision",
            "pr_number",
            "workflow_run_id",
            "solver",
            "services",
        },
        "speculative manifest",
    )
    if manifest["schema"] != SPECULATIVE_SCHEMA:
        raise ContractError("unsupported speculative manifest schema")
    tree = manifest["source_tree"]
    if not isinstance(tree, str) or not _TREE.fullmatch(tree):
        raise ContractError("speculative source tree is not a full git tree hash")
    if expect_tree is not None and tree != expect_tree:
        raise ContractError("speculative manifest binds a different source tree")
    built_from = manifest["built_from_revision"]
    if not isinstance(built_from, str) or not _SHA.fullmatch(built_from):
        raise ContractError("speculative built-from revision is invalid")
    if manifest["spec_tag"] != f"spec-{tree}-{built_from[:12]}" or not (
        _SPEC_TAG.fullmatch(manifest["spec_tag"])
    ):
        raise ContractError(
            "speculative tag does not derive from the source tree and the"
            " built-from revision"
        )
    pr_number = manifest["pr_number"]
    if not isinstance(pr_number, int) or not _PR_NUMBER.fullmatch(str(pr_number)):
        raise ContractError("speculative PR number is invalid")
    run_id = manifest["workflow_run_id"]
    if not isinstance(run_id, int) or not _RUN_ID.fullmatch(str(run_id)):
        raise ContractError("speculative workflow run ID is invalid")
    solver = manifest["solver"]
    if not isinstance(solver, dict):
        raise ContractError("speculative solver provenance must be an object")
    _exact_keys(
        solver,
        {"solver_source_revision", "solver_source_sha256"},
        "speculative solver provenance",
    )
    if not isinstance(solver["solver_source_revision"], str) or not _SHA.fullmatch(
        solver["solver_source_revision"]
    ):
        raise ContractError("speculative solver revision is invalid")
    if not isinstance(
        solver["solver_source_sha256"], str
    ) or not _SOURCE_HASH.fullmatch(solver["solver_source_sha256"]):
        raise ContractError("speculative solver source hash is invalid")
    services = manifest["services"]
    if not isinstance(services, dict) or set(services) != set(SERVICES):
        raise ContractError(
            "speculative manifest does not contain exactly five services"
        )
    for name in SERVICES:
        service = services[name]
        if not isinstance(service, dict):
            raise ContractError(f"{name} speculative entry must be an object")
        _exact_keys(service, {"repository", "image_digest"}, f"{name} speculative entry")
        if service["repository"] != REPOSITORIES[name]:
            raise ContractError(f"{name} speculative repository is not canonical")
        if not isinstance(service["image_digest"], str) or not _DIGEST.fullmatch(
            service["image_digest"]
        ):
            raise ContractError(f"{name} speculative image digest is not immutable")


def verify_workflow_run(
    run: dict[str, Any],
    workflow: dict[str, Any],
    *,
    run_id: str,
    run_attempt: str,
    workflow_path: str,
    event: str,
    branch: str,
    head_sha: str | None = None,
) -> dict[str, Any]:
    if not _RUN_ID.fullmatch(run_id) or not _RUN_ATTEMPT.fullmatch(run_attempt):
        raise ContractError("workflow run ID or attempt is invalid")
    if not workflow_path.startswith(".github/workflows/") or not workflow_path.endswith(
        (".yml", ".yaml")
    ):
        raise ContractError("canonical workflow path is invalid")
    workflow_id = workflow.get("id")
    if not isinstance(workflow_id, int) or workflow_id < 1:
        raise ContractError("canonical workflow ID is invalid")
    if workflow.get("path") != workflow_path:
        raise ContractError("canonical workflow metadata has the wrong path")
    if run.get("id") != int(run_id) or run.get("run_attempt") != int(run_attempt):
        raise ContractError("workflow run identity or attempt differs")
    if run.get("workflow_id") != workflow_id or run.get("path") != workflow_path:
        raise ContractError("workflow run did not execute the canonical workflow")
    if run.get("event") != event or run.get("head_branch") != branch:
        raise ContractError("workflow run event or protected branch differs")
    actual_head = run.get("head_sha")
    if not isinstance(actual_head, str) or not _SHA.fullmatch(actual_head):
        raise ContractError("workflow run head SHA is invalid")
    if head_sha is not None and actual_head != head_sha:
        raise ContractError("workflow run head SHA differs from the trusted source")
    if run.get("status") != "completed" or run.get("conclusion") != "success":
        raise ContractError("workflow run did not complete successfully")
    return {
        "workflow_id": workflow_id,
        "workflow_path": workflow_path,
        "run_id": int(run_id),
        "run_attempt": int(run_attempt),
        "event": event,
        "head_branch": branch,
        "head_sha": actual_head,
    }


def verify_artifact(
    listing: dict[str, Any],
    *,
    artifact_name: str,
    run_id: str,
    head_sha: str,
) -> dict[str, Any]:
    artifacts = listing.get("artifacts")
    if not isinstance(artifacts, list):
        raise ContractError("workflow artifact listing is invalid")
    matches = [item for item in artifacts if item.get("name") == artifact_name]
    if len(matches) != 1:
        raise ContractError("expected exactly one attempt-specific workflow artifact")
    artifact = matches[0]
    workflow_run = artifact.get("workflow_run")
    if (
        artifact.get("expired") is not False
        or not isinstance(artifact.get("id"), int)
        or not isinstance(workflow_run, dict)
        or workflow_run.get("id") != int(run_id)
        or workflow_run.get("head_sha") != head_sha
    ):
        raise ContractError("workflow artifact is not bound to the accepted run")
    return artifact


def _validate_run_proof(proof: dict[str, Any], label: str) -> None:
    required = {
        "workflow_id",
        "workflow_path",
        "run_id",
        "run_attempt",
        "event",
        "head_branch",
        "head_sha",
    }
    if set(proof) != required:
        raise ContractError(f"{label} workflow run proof is incomplete")
    if (
        not isinstance(proof["workflow_id"], int)
        or proof["workflow_id"] < 1
        or not isinstance(proof["run_id"], int)
        or proof["run_id"] < 1
        or not isinstance(proof["run_attempt"], int)
        or proof["run_attempt"] < 1
        or not isinstance(proof["workflow_path"], str)
        or not proof["workflow_path"].startswith(".github/workflows/")
        or proof["event"] not in {"push", "workflow_dispatch"}
        or proof["head_branch"] != "main"
        or not isinstance(proof["head_sha"], str)
        or not _SHA.fullmatch(proof["head_sha"])
    ):
        raise ContractError(f"{label} workflow run proof is invalid")


def _production_handoff_projection(
    manifest: dict[str, Any],
) -> tuple[str, dict[str, dict[str, Any]]]:
    """Project every accepted supply schema into the stable handoff contract.

    V3 keeps producer identity and release identity separate. Production
    consumers still read the compact handoff v1 service shape, so bind each
    projected row to the release source while the full producer evidence stays
    authenticated by the supply-set manifest hash.
    """

    if manifest["schema"] != SCHEMA_V3:
        return manifest["source_revision"], copy.deepcopy(manifest["services"])

    source = manifest["release_source_revision"]
    services: dict[str, dict[str, Any]] = {}
    for name in SERVICES:
        entry = manifest["services"][name]
        projected = {
            "repository": entry["repository"],
            "image_digest": entry["image_digest"],
            "source_revision": source,
        }
        if name == "web":
            projected["artifact_sha256"] = entry["artifact_sha256"]
        if name == "canonical-worker":
            projected["provenance"] = {
                "application_source_revision": source,
                **entry["solver_provenance"],
            }
        services[name] = projected
    return source, services


def verify_staging_receipt(
    manifest: dict[str, Any],
    receipt: dict[str, Any],
    repo_root: Path,
    main_ref: str,
    release_run: dict[str, Any],
    acceptance_run: dict[str, Any],
    expected_receipt_run_id: str,
) -> dict[str, Any]:
    validate_manifest(manifest)
    if receipt.get("schema") != "leaf.deployed-authored-cad-acceptance.v1":
        raise ContractError("staging receipt schema is not accepted")
    if receipt.get("environment") != "staging" or receipt.get("mode") != "execute":
        raise ContractError("production handoff requires executed staging acceptance")
    if receipt.get("ok") is not True or receipt.get("secrets_recorded") is not False:
        raise ContractError("staging acceptance did not finish safely")
    source, handoff_services = _production_handoff_projection(manifest)
    if receipt.get("source_revision") != source:
        raise ContractError("staging and release source revisions differ")
    images = receipt.get("images")
    if not isinstance(images, dict) or set(images) != set(SERVICES):
        raise ContractError("staging receipt does not contain exactly five images")
    expected = {name: manifest["services"][name]["image_digest"] for name in SERVICES}
    if images != expected:
        raise ContractError("staging and release image digests differ")
    run_id = receipt.get("run_id")
    if (
        not isinstance(run_id, str)
        or not _RECEIPT_RUN_ID.fullmatch(run_id)
        or run_id != expected_receipt_run_id
    ):
        raise ContractError("staging receipt run ID is invalid")
    _validate_run_proof(release_run, "release")
    _validate_run_proof(acceptance_run, "staging")
    if release_run["head_sha"] != source or release_run["event"] != "push":
        raise ContractError(
            "release workflow run proof differs from the release source"
        )
    if acceptance_run["event"] != "workflow_dispatch":
        raise ContractError("staging workflow run proof has the wrong event")

    commit = subprocess.run(
        ["git", "-C", str(repo_root), "cat-file", "-e", f"{source}^{{commit}}"],
        capture_output=True,
    )
    ancestry = subprocess.run(
        ["git", "-C", str(repo_root), "merge-base", "--is-ancestor", source, main_ref],
        capture_output=True,
    )
    if commit.returncode != 0 or ancestry.returncode != 0:
        raise ContractError(
            "release source is not an ancestor of the protected main ref"
        )

    canonical = json.dumps(manifest, sort_keys=True, separators=(",", ":")).encode()
    return {
        "schema": HANDOFF_SCHEMA,
        "source_revision": source,
        "staging_supply_set_manifest_sha256": hashlib.sha256(canonical).hexdigest(),
        "release": {
            "workflow_run_id": release_run["run_id"],
            "workflow_run_attempt": release_run["run_attempt"],
            "workflow_id": release_run["workflow_id"],
            "workflow_path": release_run["workflow_path"],
            "event": release_run["event"],
            "head_branch": release_run["head_branch"],
            "head_sha": release_run["head_sha"],
        },
        "staging_acceptance": {
            "run_id": run_id,
            "workflow_run_id": acceptance_run["run_id"],
            "workflow_run_attempt": acceptance_run["run_attempt"],
            "workflow_id": acceptance_run["workflow_id"],
            "workflow_path": acceptance_run["workflow_path"],
            "event": acceptance_run["event"],
            "head_branch": acceptance_run["head_branch"],
            "head_sha": acceptance_run["head_sha"],
            "source_revision": source,
            "images": images,
        },
        "staging_supply_set_services": handoff_services,
        "proof": {
            "source_is_ancestor_of_main": True,
            "staging_digests_equal_release": True,
        },
    }


def _web_entries_digest(entries: dict[str, bytes]) -> str:
    if not entries:
        raise ContractError("web dist contains no files")
    digest = hashlib.sha256(b"leaf.web-dist.v1\0")
    for name in sorted(entries):
        if not name.startswith("dist/"):
            raise ContractError("web artifact paths must be rooted at dist")
        relative = name.removeprefix("dist/").encode("utf-8")
        content = entries[name]
        digest.update(len(relative).to_bytes(8, "big"))
        digest.update(relative)
        digest.update(len(content).to_bytes(8, "big"))
        digest.update(content)
    return digest.hexdigest()


def _web_dist_entries(root: Path) -> dict[str, bytes]:
    if not root.is_dir():
        raise ContractError("web dist root is missing")
    files = sorted(path for path in root.rglob("*") if path.is_file())
    if not files:
        raise ContractError("web dist root contains no files")
    entries: dict[str, bytes] = {}
    for path in files:
        if path.is_symlink():
            raise ContractError("web dist must not contain symbolic links")
        relative = path.relative_to(root).as_posix()
        if not relative or relative.startswith("/") or "\\" in relative:
            raise ContractError("web dist path is invalid")
        entries[f"dist/{relative}"] = path.read_bytes()
    return entries


def web_dist_digest(root: Path) -> str:
    return _web_entries_digest(_web_dist_entries(root))


def _zip_info(name: str) -> zipfile.ZipInfo:
    info = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
    info.compress_type = zipfile.ZIP_STORED
    info.create_system = 3
    info.external_attr = (stat.S_IFREG | 0o644) << 16
    return info


def _write_web_archive(path: Path, entries: dict[str, bytes]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        raise ContractError("web artifact output already exists")
    with zipfile.ZipFile(path, "x", compression=zipfile.ZIP_STORED) as archive:
        for name in sorted(entries):
            archive.writestr(_zip_info(name), entries[name])


def build_web_archive(root: Path, output: Path) -> dict[str, str]:
    entries = _web_dist_entries(root)
    _write_web_archive(output, entries)
    return {
        "artifact_sha256": _web_entries_digest(entries),
        "archive_sha256": hashlib.sha256(output.read_bytes()).hexdigest(),
    }


def _read_web_archive(path: Path) -> dict[str, bytes]:
    try:
        with zipfile.ZipFile(path) as archive:
            infos = archive.infolist()
            names = [info.filename for info in infos]
            if not names or names != sorted(names) or len(names) != len(set(names)):
                raise ContractError("web artifact paths are missing, reordered, or duplicate")
            entries: dict[str, bytes] = {}
            for info in infos:
                name = info.filename
                pure = PurePosixPath(name)
                mode = (info.external_attr >> 16) & 0o170000
                if (
                    info.is_dir()
                    or pure.is_absolute()
                    or ".." in pure.parts
                    or "\\" in name
                    or not name.startswith("dist/")
                    or mode not in {0, stat.S_IFREG}
                ):
                    raise ContractError("web artifact contains an unsupported path")
                entries[name] = archive.read(info)
    except (OSError, zipfile.BadZipFile, RuntimeError) as exc:
        raise ContractError("web artifact archive is malformed") from exc
    return entries


def _health_bytes(source_revision: str) -> bytes:
    return (
        json.dumps(
            {
                "ok": True,
                "service": "leaf-platform-web",
                "component": "frontend",
                "source_sha": source_revision,
            },
            separators=(",", ":"),
            ensure_ascii=True,
        )
        + "\n"
    ).encode("utf-8")


def validate_web_restamp_receipt(receipt: dict[str, Any]) -> None:
    _exact_keys(
        receipt,
        {
            "schema",
            "algorithm",
            "old_artifact_sha256",
            "new_artifact_sha256",
            "old_archive_sha256",
            "new_archive_sha256",
            "old_source_revision",
            "new_source_revision",
            "common_tree",
            "changed_path",
            "changed_field",
            "unchanged_path_count",
            "receipt_sha256",
        },
        "web source-restamp receipt",
    )
    if receipt["schema"] != WEB_RESTAMP_SCHEMA or receipt["algorithm"] != WEB_RESTAMP_ALGORITHM:
        raise ContractError("web source-restamp version is unsupported")
    for field in ("old_artifact_sha256", "new_artifact_sha256", "old_archive_sha256", "new_archive_sha256", "receipt_sha256"):
        value = receipt[field]
        if not isinstance(value, str) or not _SOURCE_HASH.fullmatch(value):
            raise ContractError(f"web source-restamp {field} is invalid")
    for field in ("old_source_revision", "new_source_revision", "common_tree"):
        value = receipt[field]
        if not isinstance(value, str) or not _SHA.fullmatch(value):
            raise ContractError(f"web source-restamp {field} is invalid")
    if receipt["old_source_revision"] == receipt["new_source_revision"]:
        raise ContractError("web source-restamp must change the source revision")
    if receipt["changed_path"] != WEB_HEALTH_PATH or receipt["changed_field"] != "source_sha":
        raise ContractError("web source-restamp changed an unsupported field")
    if not isinstance(receipt["unchanged_path_count"], int) or receipt["unchanged_path_count"] < 0:
        raise ContractError("web source-restamp path count is invalid")
    body = {key: value for key, value in receipt.items() if key != "receipt_sha256"}
    if receipt["receipt_sha256"] != _canonical_sha256(body):
        raise ContractError("web source-restamp receipt digest is invalid")


def restamp_web_archive(
    input_archive: Path,
    output_archive: Path,
    output_root: Path,
    *,
    old_source_revision: str,
    new_source_revision: str,
    common_tree: str,
    expected_old_artifact_sha256: str,
) -> dict[str, Any]:
    if not _SHA.fullmatch(old_source_revision) or not _SHA.fullmatch(new_source_revision):
        raise ContractError("web source-restamp revisions must be full commit SHAs")
    if not _TREE.fullmatch(common_tree):
        raise ContractError("web source-restamp common tree is invalid")
    if not _SOURCE_HASH.fullmatch(expected_old_artifact_sha256):
        raise ContractError("web source-restamp expected artifact hash is invalid")
    if old_source_revision == new_source_revision:
        raise ContractError("web source-restamp revisions must differ")
    entries = _read_web_archive(input_archive)
    old_artifact_sha256 = _web_entries_digest(entries)
    if old_artifact_sha256 != expected_old_artifact_sha256:
        raise ContractError("web source-restamp input does not match producer evidence")
    health = entries.get(WEB_HEALTH_PATH)
    if health is None:
        raise ContractError("web source-restamp health artifact is missing")
    try:
        parsed = json.loads(health.decode("utf-8"), object_pairs_hook=_object)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ContractError("web source-restamp health artifact is malformed") from exc
    _exact_keys(parsed, {"ok", "service", "component", "source_sha"}, "web health artifact")
    if health != _health_bytes(old_source_revision) or parsed != {
        "ok": True,
        "service": "leaf-platform-web",
        "component": "frontend",
        "source_sha": old_source_revision,
    }:
        raise ContractError("web source-restamp input identity is not canonical")
    transformed = dict(entries)
    transformed[WEB_HEALTH_PATH] = _health_bytes(new_source_revision)
    if set(transformed) != set(entries) or any(
        entries[name] != transformed[name] for name in entries if name != WEB_HEALTH_PATH
    ):
        raise ContractError("web source-restamp changed an unrelated path")
    _write_web_archive(output_archive, transformed)
    if _read_web_archive(output_archive) != transformed:
        raise ContractError("web source-restamp output is not deterministic")
    if output_root.exists() and any(output_root.iterdir()):
        raise ContractError("web source-restamp output root is not empty")
    for name, content in transformed.items():
        destination = output_root.joinpath(*PurePosixPath(name).parts)
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(content)
    receipt: dict[str, Any] = {
        "schema": WEB_RESTAMP_SCHEMA,
        "algorithm": WEB_RESTAMP_ALGORITHM,
        "old_artifact_sha256": old_artifact_sha256,
        "new_artifact_sha256": _web_entries_digest(transformed),
        "old_archive_sha256": hashlib.sha256(input_archive.read_bytes()).hexdigest(),
        "new_archive_sha256": hashlib.sha256(output_archive.read_bytes()).hexdigest(),
        "old_source_revision": old_source_revision,
        "new_source_revision": new_source_revision,
        "common_tree": common_tree,
        "changed_path": WEB_HEALTH_PATH,
        "changed_field": "source_sha",
        "unchanged_path_count": len(entries) - 1,
    }
    receipt["receipt_sha256"] = _canonical_sha256(receipt)
    validate_web_restamp_receipt(receipt)
    return receipt


def _write_new(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("x", encoding="utf-8", newline="\n") as handle:
        json.dump(value, handle, indent=2, sort_keys=True)
        handle.write("\n")


def _images(values: Sequence[str]) -> dict[str, str]:
    images: dict[str, str] = {}
    for value in values:
        name, separator, digest = value.partition("=")
        if not separator or name in images:
            raise ContractError("each --image must be one unique service=digest pair")
        images[name] = digest
    return images


def _service_entries(values: Sequence[str]) -> dict[str, dict[str, Any]]:
    entries: dict[str, dict[str, Any]] = {}
    for value in values:
        name, separator, path = value.partition("=")
        if not separator or name in entries or name not in SERVICES:
            raise ContractError(
                "each --service-entry must be one unique canonical service=path pair"
            )
        entries[name] = load_json(Path(path))
    return entries


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    commands = parser.add_subparsers(dest="command", required=True)
    generate = commands.add_parser("generate")
    generate.add_argument("--source-revision", required=True)
    generate.add_argument("--build-tag", required=True)
    generate.add_argument("--solver-revision", required=True)
    generate.add_argument("--solver-source-sha256", required=True)
    generate.add_argument("--web-artifact-sha256", required=True)
    generate.add_argument("--image", action="append", default=[])
    generate.add_argument("--output", type=Path, required=True)
    # All three or none: a v2 supply set records where its adopted digests
    # were speculatively built. Passing a subset is a caller bug.
    generate.add_argument("--source-tree")
    generate.add_argument("--speculative-built-from")
    generate.add_argument("--speculative-pr-number")
    generate.add_argument("--speculative-run-id")
    generate_speculative = commands.add_parser("generate-speculative")
    generate_speculative.add_argument("--source-tree", required=True)
    generate_speculative.add_argument("--built-from-revision", required=True)
    generate_speculative.add_argument("--pr-number", required=True)
    generate_speculative.add_argument("--workflow-run-id", required=True)
    generate_speculative.add_argument("--solver-revision", required=True)
    generate_speculative.add_argument("--solver-source-sha256", required=True)
    generate_speculative.add_argument("--image", action="append", default=[])
    generate_speculative.add_argument("--output", type=Path, required=True)
    verify_speculative = commands.add_parser("verify-speculative")
    verify_speculative.add_argument("--manifest", type=Path, required=True)
    verify_speculative.add_argument("--expect-tree", required=True)
    verify_speculative.add_argument("--output", type=Path, required=True)
    verify = commands.add_parser("verify-staging")
    verify.add_argument("--manifest", type=Path, required=True)
    verify.add_argument("--receipt", type=Path, required=True)
    verify.add_argument("--repo-root", type=Path, required=True)
    verify.add_argument("--main-ref", required=True)
    verify.add_argument("--release-run-proof", type=Path, required=True)
    verify.add_argument("--acceptance-run-proof", type=Path, required=True)
    verify.add_argument("--expected-receipt-run-id", required=True)
    verify.add_argument("--output", type=Path, required=True)
    verify_run = commands.add_parser("verify-workflow-run")
    verify_run.add_argument("--run", type=Path, required=True)
    verify_run.add_argument("--workflow", type=Path, required=True)
    verify_run.add_argument("--run-id", required=True)
    verify_run.add_argument("--run-attempt", required=True)
    verify_run.add_argument("--workflow-path", required=True)
    verify_run.add_argument("--event", required=True)
    verify_run.add_argument("--branch", required=True)
    verify_run.add_argument("--head-sha")
    verify_run.add_argument("--output", type=Path, required=True)
    verify_artifact_command = commands.add_parser("verify-artifact")
    verify_artifact_command.add_argument("--listing", type=Path, required=True)
    verify_artifact_command.add_argument("--artifact-name", required=True)
    verify_artifact_command.add_argument("--run-id", required=True)
    verify_artifact_command.add_argument("--head-sha", required=True)
    verify_artifact_command.add_argument("--output", type=Path, required=True)
    web_digest = commands.add_parser("digest-web-dist")
    web_digest.add_argument("--root", type=Path, required=True)
    pack_web = commands.add_parser("pack-web-dist")
    pack_web.add_argument("--root", type=Path, required=True)
    pack_web.add_argument("--output", type=Path, required=True)
    restamp_web = commands.add_parser("restamp-web-artifact")
    restamp_web.add_argument("--input-archive", type=Path, required=True)
    restamp_web.add_argument("--output-archive", type=Path, required=True)
    restamp_web.add_argument("--output-root", type=Path, required=True)
    restamp_web.add_argument("--old-source-revision", required=True)
    restamp_web.add_argument("--new-source-revision", required=True)
    restamp_web.add_argument("--common-tree", required=True)
    restamp_web.add_argument("--expect-old-artifact-sha256", required=True)
    restamp_web.add_argument("--receipt", type=Path, required=True)
    fingerprint = commands.add_parser("surface-fingerprint")
    fingerprint.add_argument("--repo-root", type=Path, required=True)
    fingerprint.add_argument("--source-revision", required=True)
    fingerprint.add_argument("--service", choices=SERVICES, required=True)
    fingerprint.add_argument("--base-image", action="append", default=[])
    fingerprint.add_argument("--build-arg", action="append", default=[])
    fingerprint.add_argument("--buildkit-version", required=True)
    fingerprint.add_argument("--platform", required=True)
    fingerprint.add_argument("--solver-revision")
    fingerprint.add_argument("--solver-source-sha256")
    fingerprint.add_argument("--output", type=Path, required=True)
    predicate = commands.add_parser("surface-predicate")
    predicate.add_argument("--fingerprint", type=Path, required=True)
    predicate.add_argument("--repository", required=True)
    predicate.add_argument("--image-digest", required=True)
    predicate.add_argument("--source-tree", required=True)
    predicate.add_argument("--workflow-blob", required=True)
    predicate.add_argument("--run-id", required=True)
    predicate.add_argument("--run-attempt", required=True)
    predicate.add_argument("--artifact-sha256")
    predicate.add_argument("--output", type=Path, required=True)
    verify_predicate = commands.add_parser("verify-surface-predicate")
    verify_predicate.add_argument("--predicate", type=Path, required=True)
    verify_predicate.add_argument("--output", type=Path, required=True)
    generate_v3 = commands.add_parser("generate-v3")
    generate_v3.add_argument("--release-source-revision", required=True)
    generate_v3.add_argument("--release-source-tree", required=True)
    generate_v3.add_argument("--build-run-id", required=True)
    generate_v3.add_argument("--build-run-attempt", required=True)
    generate_v3.add_argument("--service-entry", action="append", default=[])
    generate_v3.add_argument("--output", type=Path, required=True)
    re_envelope_v3 = commands.add_parser("re-envelope-speculative-v3")
    re_envelope_v3.add_argument("--manifest", type=Path, required=True)
    re_envelope_v3.add_argument("--web-restamp-receipt", type=Path, required=True)
    re_envelope_v3.add_argument("--speculative-run-id", required=True)
    re_envelope_v3.add_argument("--speculative-run-attempt", required=True)
    re_envelope_v3.add_argument("--expect-candidate-tree", required=True)
    re_envelope_v3.add_argument("--expect-workflow-blob", required=True)
    re_envelope_v3.add_argument("--release-source-revision", required=True)
    re_envelope_v3.add_argument("--release-source-tree", required=True)
    re_envelope_v3.add_argument("--build-run-id", required=True)
    re_envelope_v3.add_argument("--build-run-attempt", required=True)
    re_envelope_v3.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        if args.command == "digest-web-dist":
            print(web_dist_digest(args.root))
            return 0
        if args.command == "pack-web-dist":
            value = build_web_archive(args.root, args.output)
            print(json.dumps(value, sort_keys=True, separators=(",", ":")))
            return 0
        if args.command == "restamp-web-artifact":
            value = restamp_web_archive(
                args.input_archive,
                args.output_archive,
                args.output_root,
                old_source_revision=args.old_source_revision,
                new_source_revision=args.new_source_revision,
                common_tree=args.common_tree,
                expected_old_artifact_sha256=args.expect_old_artifact_sha256,
            )
            _write_new(args.receipt, value)
            return 0
        if args.command == "surface-fingerprint":
            value = build_surface_fingerprint(
                args.repo_root,
                args.source_revision,
                args.service,
                _pairs(args.base_image, "--base-image"),
                _pairs(args.build_arg, "--build-arg"),
                buildkit_version=args.buildkit_version,
                platform=args.platform,
                solver_revision=args.solver_revision,
                solver_source_sha256=args.solver_source_sha256,
            )
        elif args.command == "surface-predicate":
            value = build_surface_predicate(
                load_json(args.fingerprint),
                repository=args.repository,
                image_digest=args.image_digest,
                source_tree=args.source_tree,
                workflow_blob=args.workflow_blob,
                run_id=args.run_id,
                run_attempt=args.run_attempt,
                artifact_sha256=args.artifact_sha256,
            )
        elif args.command == "verify-surface-predicate":
            value = load_json(args.predicate)
            validate_surface_predicate(value)
        elif args.command == "generate-v3":
            value = build_v3_manifest(
                args.release_source_revision,
                args.release_source_tree,
                args.build_run_id,
                args.build_run_attempt,
                _service_entries(args.service_entry),
            )
        elif args.command == "re-envelope-speculative-v3":
            value = re_envelope_speculative_v3_manifest(
                load_json(args.manifest),
                web_restamp_receipt=load_json(args.web_restamp_receipt),
                speculative_run_id=args.speculative_run_id,
                speculative_run_attempt=args.speculative_run_attempt,
                expected_candidate_tree=args.expect_candidate_tree,
                expected_workflow_blob=args.expect_workflow_blob,
                release_source_revision=args.release_source_revision,
                release_source_tree=args.release_source_tree,
                build_run_id=args.build_run_id,
                build_run_attempt=args.build_run_attempt,
            )
        elif args.command == "verify-workflow-run":
            value = verify_workflow_run(
                load_json(args.run),
                load_json(args.workflow),
                run_id=args.run_id,
                run_attempt=args.run_attempt,
                workflow_path=args.workflow_path,
                event=args.event,
                branch=args.branch,
                head_sha=args.head_sha,
            )
        elif args.command == "verify-artifact":
            value = verify_artifact(
                load_json(args.listing),
                artifact_name=args.artifact_name,
                run_id=args.run_id,
                head_sha=args.head_sha,
            )
        elif args.command == "generate":
            speculative_args = (
                args.source_tree,
                args.speculative_built_from,
                args.speculative_pr_number,
                args.speculative_run_id,
            )
            speculative = None
            if any(value is not None for value in speculative_args):
                if any(value is None for value in speculative_args):
                    raise ContractError(
                        "speculative provenance requires --source-tree, "
                        "--speculative-built-from, --speculative-pr-number, "
                        "and --speculative-run-id together"
                    )
                if not _TREE.fullmatch(args.source_tree):
                    raise ContractError(
                        "speculative source tree must be a full git tree hash"
                    )
                if not _SHA.fullmatch(args.speculative_built_from):
                    raise ContractError(
                        "speculative built-from revision must be a full commit SHA"
                    )
                if not _PR_NUMBER.fullmatch(args.speculative_pr_number):
                    raise ContractError("speculative PR number is invalid")
                if not _RUN_ID.fullmatch(args.speculative_run_id):
                    raise ContractError("speculative workflow run ID is invalid")
                speculative = {
                    "source_tree": args.source_tree,
                    "built_from_revision": args.speculative_built_from,
                    "pr_number": int(args.speculative_pr_number),
                    "workflow_run_id": int(args.speculative_run_id),
                }
            value = build_manifest(
                args.source_revision,
                args.build_tag,
                _images(args.image),
                args.solver_revision,
                args.solver_source_sha256,
                args.web_artifact_sha256,
                speculative=speculative,
            )
            validate_manifest(value)
        elif args.command == "generate-speculative":
            value = build_speculative_manifest(
                args.source_tree,
                args.built_from_revision,
                args.pr_number,
                args.workflow_run_id,
                _images(args.image),
                args.solver_revision,
                args.solver_source_sha256,
            )
            validate_speculative_manifest(value)
        elif args.command == "verify-speculative":
            value = load_json(args.manifest)
            validate_speculative_manifest(value, expect_tree=args.expect_tree)
        else:
            value = verify_staging_receipt(
                load_json(args.manifest),
                load_json(args.receipt),
                args.repo_root,
                args.main_ref,
                load_json(args.release_run_proof),
                load_json(args.acceptance_run_proof),
                args.expected_receipt_run_id,
            )
        _write_new(args.output, value)
    except ContractError as exc:
        parser.error(str(exc))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
