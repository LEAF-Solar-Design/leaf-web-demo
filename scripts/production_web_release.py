#!/usr/bin/env python3
"""Prepare and attest one provenance-bound Vercel production deployment."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import re
import shutil
from typing import Any, Sequence
import zipfile

from platform_release_manifest import SERVICES, load_json, web_dist_digest


PROJECT_ID = "prj_tBxvYtXa47THZ8aF59gvRx8W0bBc"
PROJECT_NAME = "leaf-platform-web"
STABLE_URL = "https://leaf-platform-web.vercel.app"
HANDOFF_SCHEMA = "leaf.production-handoff-candidate.v1"
PREPARED_SCHEMA = "leaf.production-web-prepared.v1"
RECEIPT_SCHEMA = "leaf.production-web-deployment.v1"
APPROVAL_SCHEMA = "leaf.production-web-approval.v1"

_SHA = re.compile(r"^[0-9a-f]{40}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_DIGEST = re.compile(r"^sha256:[0-9a-f]{64}$")
_RUN_ID = re.compile(r"^[1-9][0-9]{5,19}$")
_ATTEMPT = re.compile(r"^[1-9][0-9]*$")
_DEPLOYMENT_ID = re.compile(r"^dpl_[A-Za-z0-9]{20,64}$")
_DEPLOYMENT_URL = re.compile(r"^[a-z0-9][a-z0-9-]*\.vercel\.app$")
_ENTRY_ASSET = re.compile(rb"assets/index-[A-Za-z0-9_-]+\.js")
_LOGIN = re.compile(r"^[A-Za-z0-9-]{1,39}$")
_UTC = re.compile(r"^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z$")


class ReleaseError(ValueError):
    """The supplied deployment evidence is not safe to publish."""


def _exact(value: dict[str, Any], keys: set[str], label: str) -> None:
    if set(value) != keys:
        raise ReleaseError(f"{label} has unsupported or missing fields")


def _positive(value: str, pattern: re.Pattern[str], label: str) -> int:
    if not pattern.fullmatch(value):
        raise ReleaseError(f"{label} is invalid")
    return int(value)


def extract_artifact(archive: Path, destination: Path) -> None:
    if destination.exists():
        raise ReleaseError("artifact destination already exists")
    destination.mkdir(mode=0o700, parents=True)
    root = destination.resolve()
    try:
        with zipfile.ZipFile(archive) as bundle:
            if not bundle.infolist():
                raise ReleaseError("artifact archive is empty")
            for member in bundle.infolist():
                name = member.filename.replace("\\", "/")
                if (
                    not name
                    or name.startswith("/")
                    or re.match(r"^[A-Za-z]:", name)
                    or any(
                        part in {"", ".", ".."} for part in name.rstrip("/").split("/")
                    )
                    or ((member.external_attr >> 16) & 0o170000) == 0o120000
                ):
                    raise ReleaseError("artifact archive contains an unsafe path")
                target = (destination / name).resolve()
                if target != root and root not in target.parents:
                    raise ReleaseError("artifact member escapes its destination")
            bundle.extractall(destination)
    except (OSError, zipfile.BadZipFile) as exc:
        raise ReleaseError("artifact archive cannot be extracted") from exc


def _validate_service_set(handoff: dict[str, Any], source: str) -> dict[str, Any]:
    services = handoff.get("staging_supply_set_services")
    if not isinstance(services, dict) or set(services) != set(SERVICES):
        raise ReleaseError("handoff does not contain exactly five staging services")
    for name, service in services.items():
        if not isinstance(service, dict):
            raise ReleaseError(f"{name} service evidence is invalid")
        expected_fields = {"repository", "image_digest", "source_revision"}
        if name == "canonical-worker":
            expected_fields.add("provenance")
        if name == "web":
            expected_fields.add("artifact_sha256")
        _exact(service, expected_fields, f"{name} service evidence")
        if service.get("source_revision") != source:
            raise ReleaseError("handoff contains mixed source revisions")
        if service.get("repository") != f"leaf-platform-{name}":
            raise ReleaseError("handoff contains a noncanonical repository")
        digest = service.get("image_digest")
        if not isinstance(digest, str) or not _DIGEST.fullmatch(digest):
            raise ReleaseError("handoff contains a mutable image identity")
    web_hash = services["web"].get("artifact_sha256")
    if not isinstance(web_hash, str) or not _SHA256.fullmatch(web_hash):
        raise ReleaseError("handoff web artifact hash is invalid")
    provenance = services["canonical-worker"]["provenance"]
    if not isinstance(provenance, dict):
        raise ReleaseError("canonical worker provenance is invalid")
    _exact(
        provenance,
        {
            "application_source_revision",
            "solver_source_revision",
            "solver_source_sha256",
        },
        "canonical worker provenance",
    )
    if (
        provenance["application_source_revision"] != source
        or not isinstance(provenance["solver_source_revision"], str)
        or not _SHA.fullmatch(provenance["solver_source_revision"])
        or not isinstance(provenance["solver_source_sha256"], str)
        or not _SHA256.fullmatch(provenance["solver_source_sha256"])
    ):
        raise ReleaseError("canonical worker source provenance differs")
    return services


def prepare(
    handoff: dict[str, Any],
    web_dist: Path,
    output_root: Path,
    *,
    source: str,
    release_run_id: str,
    release_attempt: str,
    expected_web_sha256: str,
) -> dict[str, Any]:
    if not _SHA.fullmatch(source) or not _SHA256.fullmatch(expected_web_sha256):
        raise ReleaseError("source or expected web artifact hash is invalid")
    release_id = _positive(release_run_id, _RUN_ID, "release run ID")
    attempt = _positive(release_attempt, _ATTEMPT, "release run attempt")
    _exact(
        handoff,
        {
            "schema",
            "source_revision",
            "staging_supply_set_manifest_sha256",
            "release",
            "staging_acceptance",
            "staging_supply_set_services",
            "proof",
        },
        "production handoff",
    )
    if handoff["schema"] != HANDOFF_SCHEMA or handoff["source_revision"] != source:
        raise ReleaseError("production handoff schema or source differs")
    supply_hash = handoff["staging_supply_set_manifest_sha256"]
    if not isinstance(supply_hash, str) or not _SHA256.fullmatch(supply_hash):
        raise ReleaseError("production handoff supply-set hash is invalid")
    proof = handoff.get("proof")
    if proof != {
        "source_is_ancestor_of_main": True,
        "staging_digests_equal_release": True,
    }:
        raise ReleaseError("production handoff proof is incomplete")
    release = handoff.get("release")
    if not isinstance(release, dict):
        raise ReleaseError("handoff release provenance differs")
    _exact(
        release,
        {
            "workflow_run_id",
            "workflow_run_attempt",
            "workflow_id",
            "workflow_path",
            "event",
            "head_branch",
            "head_sha",
        },
        "handoff release provenance",
    )
    if (
        release.get("workflow_run_id") != release_id
        or release.get("workflow_run_attempt") != attempt
        or release.get("workflow_path") != ".github/workflows/build-platform-images.yml"
        or release.get("event") != "push"
        or release.get("head_branch") != "main"
        or release.get("head_sha") != source
    ):
        raise ReleaseError("handoff release provenance differs")
    services = _validate_service_set(handoff, source)
    acceptance = handoff.get("staging_acceptance")
    expected_images = {name: value["image_digest"] for name, value in services.items()}
    if not isinstance(acceptance, dict):
        raise ReleaseError("handoff staging acceptance provenance differs")
    _exact(
        acceptance,
        {
            "run_id",
            "workflow_run_id",
            "workflow_run_attempt",
            "workflow_id",
            "workflow_path",
            "event",
            "head_branch",
            "head_sha",
            "source_revision",
            "images",
        },
        "handoff staging acceptance provenance",
    )
    if (
        acceptance.get("source_revision") != source
        or acceptance.get("event") != "workflow_dispatch"
        or acceptance.get("head_branch") != "main"
        or acceptance.get("workflow_path")
        != ".github/workflows/accept-leaf-platform-staging-authored-cad.yml"
        or not isinstance(acceptance.get("workflow_run_id"), int)
        or acceptance["workflow_run_id"] < 1
        or not isinstance(acceptance.get("workflow_run_attempt"), int)
        or acceptance["workflow_run_attempt"] < 1
        or acceptance.get("images") != expected_images
    ):
        raise ReleaseError("handoff staging acceptance provenance differs")
    if services["web"]["artifact_sha256"] != expected_web_sha256:
        raise ReleaseError("handoff web artifact hash differs from the approved hash")

    actual_hash = web_dist_digest(web_dist)
    if actual_hash != expected_web_sha256:
        raise ReleaseError("downloaded web artifact bytes differ from the handoff")
    health = load_json(web_dist / "health.json")
    if health != {
        "ok": True,
        "service": "leaf-platform-web",
        "component": "frontend",
        "source_sha": source,
    }:
        raise ReleaseError("web health identity differs from the release source")
    index = (web_dist / "index.html").read_bytes()
    entries = sorted(set(item.decode("ascii") for item in _ENTRY_ASSET.findall(index)))
    if len(entries) != 1 or not (web_dist / entries[0]).is_file():
        raise ReleaseError("web artifact does not contain one referenced entry asset")
    config = load_json(web_dist / "vercel.json")
    if (
        set(config) != {"rewrites"}
        or not isinstance(config["rewrites"], list)
        or len(config["rewrites"]) != 1
        or config["rewrites"][0].get("destination") != "/index.html"
    ):
        raise ReleaseError(
            "web artifact does not contain the reviewed SPA route contract"
        )

    if output_root.exists():
        raise ReleaseError("Vercel output root already exists")
    static = output_root / "static"
    static.mkdir(mode=0o700, parents=True)
    for path in sorted(web_dist.rglob("*")):
        if path.is_symlink():
            raise ReleaseError("web artifact contains a symbolic link")
        if not path.is_file() or path == web_dist / "vercel.json":
            continue
        relative = path.relative_to(web_dist)
        target = static / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(path, target)
    output_config = {
        "version": 3,
        "routes": [
            {
                "src": "/health\\.json",
                "headers": {
                    "Content-Type": "application/json; charset=utf-8",
                    "Cache-Control": "no-store",
                },
                "continue": True,
            },
            {
                "src": "/api(?:/.*)?",
                "status": 404,
                "headers": {
                    "Content-Type": "application/json; charset=utf-8",
                    "Cache-Control": "no-store",
                },
            },
            {"handle": "filesystem"},
            {"src": "/.*", "dest": "/index.html"},
        ],
    }
    (output_root / "config.json").write_text(
        json.dumps(output_config, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    return {
        "schema": PREPARED_SCHEMA,
        "source_revision": source,
        "web_artifact_sha256": actual_hash,
        "entry_asset": entries[0],
        "release_workflow_run_id": release_id,
        "release_workflow_run_attempt": attempt,
        "api_boundary": "terminal-404",
        "build_performed": False,
    }


def deployment_receipt(
    prepared: dict[str, Any],
    baseline: dict[str, Any],
    deployment: dict[str, Any],
    stable: dict[str, Any],
    approval: dict[str, Any],
    *,
    handoff_run_id: str,
    handoff_attempt: str,
    workflow_run_id: str,
    workflow_attempt: str,
    workflow_head_sha: str,
) -> dict[str, Any]:
    _exact(
        prepared,
        {
            "schema",
            "source_revision",
            "web_artifact_sha256",
            "entry_asset",
            "release_workflow_run_id",
            "release_workflow_run_attempt",
            "api_boundary",
            "build_performed",
        },
        "prepared proof",
    )
    if (
        prepared["schema"] != PREPARED_SCHEMA
        or prepared["build_performed"] is not False
    ):
        raise ReleaseError("prepared proof is invalid")
    if not _SHA.fullmatch(workflow_head_sha):
        raise ReleaseError("deployment workflow head SHA is invalid")
    handoff_id = _positive(handoff_run_id, _RUN_ID, "handoff run ID")
    handoff_try = _positive(handoff_attempt, _ATTEMPT, "handoff run attempt")
    current_id = _positive(workflow_run_id, _RUN_ID, "deployment workflow run ID")
    current_try = _positive(workflow_attempt, _ATTEMPT, "deployment run attempt")
    approval_keys = {
        "schema", "project_id", "source_revision", "release_workflow_run_id",
        "release_workflow_run_attempt", "handoff_workflow_run_id",
        "handoff_workflow_run_attempt", "web_artifact_sha256", "workflow_head_sha",
        "deployment_workflow_run_id", "deployment_workflow_run_attempt",
        "issue_number", "comment_id", "approver_login", "approver_id", "permission",
        "created_at", "validated_at", "approval_payload_sha256",
        "exact_body_verified", "author_separated", "timely_at_promotion",
    }
    _exact(approval, approval_keys, "production approval proof")
    if (
        approval["schema"] != APPROVAL_SCHEMA
        or approval["project_id"] != PROJECT_ID
        or approval["source_revision"] != prepared["source_revision"]
        or approval["release_workflow_run_id"] != prepared["release_workflow_run_id"]
        or approval["release_workflow_run_attempt"] != prepared["release_workflow_run_attempt"]
        or approval["handoff_workflow_run_id"] != handoff_id
        or approval["handoff_workflow_run_attempt"] != handoff_try
        or approval["web_artifact_sha256"] != prepared["web_artifact_sha256"]
        or approval["workflow_head_sha"] != workflow_head_sha
        or approval["deployment_workflow_run_id"] != current_id
        or approval["deployment_workflow_run_attempt"] != current_try
        or not isinstance(approval["issue_number"], int)
        or approval["issue_number"] < 1
        or not isinstance(approval["comment_id"], int)
        or approval["comment_id"] < 1
        or not isinstance(approval["approver_id"], int)
        or approval["approver_id"] < 1
        or not isinstance(approval["approver_login"], str)
        or not _LOGIN.fullmatch(approval["approver_login"])
        or approval["permission"] not in {"write", "maintain", "admin"}
        or not isinstance(approval["created_at"], str)
        or not _UTC.fullmatch(approval["created_at"])
        or not isinstance(approval["validated_at"], str)
        or not _UTC.fullmatch(approval["validated_at"])
        or not isinstance(approval["approval_payload_sha256"], str)
        or not _SHA256.fullmatch(approval["approval_payload_sha256"])
        or approval["exact_body_verified"] is not True
        or approval["author_separated"] is not True
        or approval["timely_at_promotion"] is not True
    ):
        raise ReleaseError("production approval proof is invalid or unbound")
    for label, value in (
        ("baseline", baseline),
        ("deployment", deployment),
        ("stable", stable),
    ):
        if value.get("projectId") != PROJECT_ID:
            raise ReleaseError(f"{label} deployment belongs to another project")
        if value.get("name") != PROJECT_NAME or value.get("readyState") != "READY":
            raise ReleaseError(f"{label} deployment is not ready")
        if value.get("target") != "production":
            raise ReleaseError(f"{label} deployment is not a production target")
        if not isinstance(value.get("id"), str) or not _DEPLOYMENT_ID.fullmatch(
            value["id"]
        ):
            raise ReleaseError(f"{label} deployment ID is invalid")
        if not isinstance(value.get("url"), str) or not _DEPLOYMENT_URL.fullmatch(
            value["url"]
        ):
            raise ReleaseError(f"{label} deployment URL is invalid")
    if deployment["id"] != stable["id"]:
        raise ReleaseError(
            "stable production URL does not resolve to the new deployment"
        )
    if baseline["id"] == deployment["id"]:
        raise ReleaseError(
            "production deployment did not create a new immutable deployment"
        )
    return {
        "schema": RECEIPT_SCHEMA,
        "project_id": PROJECT_ID,
        "project_name": PROJECT_NAME,
        "source_revision": prepared["source_revision"],
        "web_artifact_sha256": prepared["web_artifact_sha256"],
        "entry_asset": prepared["entry_asset"],
        "deployment_id": deployment["id"],
        "deployment_url": f"https://{deployment['url']}",
        "stable_url": STABLE_URL,
        "baseline_deployment_id": baseline["id"],
        "release_workflow_run_id": prepared["release_workflow_run_id"],
        "release_workflow_run_attempt": prepared["release_workflow_run_attempt"],
        "handoff_workflow_run_id": handoff_id,
        "handoff_workflow_run_attempt": handoff_try,
        "deployment_workflow_run_id": current_id,
        "deployment_workflow_run_attempt": current_try,
        "deployment_workflow_head_sha": workflow_head_sha,
        "approval": approval,
        "build_performed": False,
        "pre_promotion_verified": True,
        "stable_routes_verified": True,
        "api_boundary_verified": True,
        "secret_values_observed": False,
    }


def _write_new(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("x", encoding="utf-8", newline="\n") as handle:
        json.dump(value, handle, indent=2, sort_keys=True)
        handle.write("\n")


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    commands = parser.add_subparsers(dest="command", required=True)
    extract = commands.add_parser("extract")
    extract.add_argument("--archive", type=Path, required=True)
    extract.add_argument("--destination", type=Path, required=True)
    prepared = commands.add_parser("prepare")
    prepared.add_argument("--handoff", type=Path, required=True)
    prepared.add_argument("--web-dist", type=Path, required=True)
    prepared.add_argument("--output-root", type=Path, required=True)
    prepared.add_argument("--source", required=True)
    prepared.add_argument("--release-run-id", required=True)
    prepared.add_argument("--release-attempt", required=True)
    prepared.add_argument("--expected-web-sha256", required=True)
    prepared.add_argument("--proof-output", type=Path, required=True)
    receipt = commands.add_parser("receipt")
    receipt.add_argument("--prepared", type=Path, required=True)
    receipt.add_argument("--baseline", type=Path, required=True)
    receipt.add_argument("--deployment", type=Path, required=True)
    receipt.add_argument("--stable", type=Path, required=True)
    receipt.add_argument("--approval", type=Path, required=True)
    receipt.add_argument("--handoff-run-id", required=True)
    receipt.add_argument("--handoff-attempt", required=True)
    receipt.add_argument("--workflow-run-id", required=True)
    receipt.add_argument("--workflow-attempt", required=True)
    receipt.add_argument("--workflow-head-sha", required=True)
    receipt.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        if args.command == "extract":
            extract_artifact(args.archive, args.destination)
            return 0
        if args.command == "prepare":
            value = prepare(
                load_json(args.handoff),
                args.web_dist,
                args.output_root,
                source=args.source,
                release_run_id=args.release_run_id,
                release_attempt=args.release_attempt,
                expected_web_sha256=args.expected_web_sha256,
            )
            _write_new(args.proof_output, value)
            return 0
        value = deployment_receipt(
            load_json(args.prepared),
            load_json(args.baseline),
            load_json(args.deployment),
            load_json(args.stable),
            load_json(args.approval),
            handoff_run_id=args.handoff_run_id,
            handoff_attempt=args.handoff_attempt,
            workflow_run_id=args.workflow_run_id,
            workflow_attempt=args.workflow_attempt,
            workflow_head_sha=args.workflow_head_sha,
        )
        _write_new(args.output, value)
    except ReleaseError as exc:
        parser.error(str(exc))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
