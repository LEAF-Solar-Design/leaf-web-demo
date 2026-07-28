#!/usr/bin/env python3
"""Generate a staging supply set and verify its production handoff candidate."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import re
import subprocess
from typing import Any, Sequence


SCHEMA = "leaf.staging-supply-set.v1"
HANDOFF_SCHEMA = "leaf.production-handoff-candidate.v1"
SERVICES = ("app", "broker", "canonical-worker", "harness", "web")
REPOSITORIES = {name: f"leaf-platform-{name}" for name in SERVICES}
_SHA = re.compile(r"^[0-9a-f]{40}$")
_DIGEST = re.compile(r"^sha256:[0-9a-f]{64}$")
_SOURCE_HASH = re.compile(r"^[0-9a-f]{64}$")
_TAG = re.compile(r"^(?:prod-[0-9a-f]{7,40}|sha-[0-9a-f]{40})$")
_RUN_ID = re.compile(r"^[1-9][0-9]{5,19}$")
_RUN_ATTEMPT = re.compile(r"^[1-9][0-9]*$")
_RECEIPT_RUN_ID = re.compile(r"^[a-z0-9][a-z0-9-]{5,49}$")


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


def build_manifest(
    source_revision: str,
    build_tag: str,
    image_digests: dict[str, str],
    solver_revision: str,
    solver_source_sha256: str,
    web_artifact_sha256: str,
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
    return {
        "schema": SCHEMA,
        "source_revision": source_revision,
        "build_tag": build_tag,
        "services": services,
    }


def validate_manifest(manifest: dict[str, Any]) -> None:
    _exact_keys(
        manifest, {"schema", "source_revision", "build_tag", "services"}, "manifest"
    )
    if manifest["schema"] != SCHEMA:
        raise ContractError("unsupported release manifest schema")
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
    source = manifest["source_revision"]
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
        "staging_supply_set_services": manifest["services"],
        "proof": {
            "source_is_ancestor_of_main": True,
            "staging_digests_equal_release": True,
        },
    }


def web_dist_digest(root: Path) -> str:
    if not root.is_dir():
        raise ContractError("web dist root is missing")
    files = sorted(path for path in root.rglob("*") if path.is_file())
    if not files:
        raise ContractError("web dist root contains no files")
    digest = hashlib.sha256(b"leaf.web-dist.v1\0")
    for path in files:
        if path.is_symlink():
            raise ContractError("web dist must not contain symbolic links")
        relative = path.relative_to(root).as_posix().encode("utf-8")
        content = path.read_bytes()
        digest.update(len(relative).to_bytes(8, "big"))
        digest.update(relative)
        digest.update(len(content).to_bytes(8, "big"))
        digest.update(content)
    return digest.hexdigest()


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
    args = parser.parse_args(argv)
    try:
        if args.command == "digest-web-dist":
            print(web_dist_digest(args.root))
            return 0
        if args.command == "verify-workflow-run":
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
            value = build_manifest(
                args.source_revision,
                args.build_tag,
                _images(args.image),
                args.solver_revision,
                args.solver_source_sha256,
                args.web_artifact_sha256,
            )
            validate_manifest(value)
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
