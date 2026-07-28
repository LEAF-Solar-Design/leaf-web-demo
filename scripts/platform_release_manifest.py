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
    _exact_keys(manifest, {"schema", "source_revision", "build_tag", "services"}, "manifest")
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
    if not isinstance(provenance["solver_source_sha256"], str) or not _SOURCE_HASH.fullmatch(
        provenance["solver_source_sha256"]
    ):
        raise ContractError("canonical-worker solver source hash is invalid")


def verify_staging_receipt(
    manifest: dict[str, Any],
    receipt: dict[str, Any],
    repo_root: Path,
    main_ref: str,
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
    expected = {
        name: manifest["services"][name]["image_digest"] for name in SERVICES
    }
    if images != expected:
        raise ContractError("staging and release image digests differ")
    run_id = receipt.get("run_id")
    if not isinstance(run_id, str) or not re.fullmatch(r"[a-z0-9][a-z0-9-]{5,49}", run_id):
        raise ContractError("staging receipt run ID is invalid")

    commit = subprocess.run(
        ["git", "-C", str(repo_root), "cat-file", "-e", f"{source}^{{commit}}"],
        capture_output=True,
    )
    ancestry = subprocess.run(
        ["git", "-C", str(repo_root), "merge-base", "--is-ancestor", source, main_ref],
        capture_output=True,
    )
    if commit.returncode != 0 or ancestry.returncode != 0:
        raise ContractError("release source is not an ancestor of the protected main ref")

    canonical = json.dumps(manifest, sort_keys=True, separators=(",", ":")).encode()
    return {
        "schema": HANDOFF_SCHEMA,
        "source_revision": source,
        "staging_supply_set_manifest_sha256": hashlib.sha256(canonical).hexdigest(),
        "staging_acceptance": {
            "run_id": run_id,
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
    verify.add_argument("--output", type=Path, required=True)
    web_digest = commands.add_parser("digest-web-dist")
    web_digest.add_argument("--root", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        if args.command == "digest-web-dist":
            print(web_dist_digest(args.root))
            return 0
        if args.command == "generate":
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
            )
        _write_new(args.output, value)
    except ContractError as exc:
        parser.error(str(exc))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
