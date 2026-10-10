#!/usr/bin/env python3
"""Prepare and attest one provenance-bound Vercel production deployment."""

from __future__ import annotations

import argparse
import hashlib
import io
import json
from pathlib import Path
import re
import shutil
import stat
from typing import Any, Sequence
import uuid
import zipfile

from platform_release_manifest import SERVICES, load_json, web_dist_digest


PROJECT_ID = "prj_tBxvYtXa47THZ8aF59gvRx8W0bBc"
PROJECT_NAME = "leaf-platform-web"
STABLE_URL = "https://leaf-platform-web.vercel.app"
PRODUCTION_HOSTS = ("app.leafdesign.ai", "platform.leafdesign.ai")
HANDOFF_V1_SCHEMA = "leaf.production-handoff-candidate.v1"
HANDOFF_V2_SCHEMA = "leaf.production-handoff-candidate.v2"
HANDOFF_SCHEMA = HANDOFF_V1_SCHEMA
PREPARED_V1_SCHEMA = "leaf.production-web-prepared.v1"
PREPARED_V2_SCHEMA = "leaf.production-web-prepared.v2"
PREPARED_SCHEMA = PREPARED_V1_SCHEMA
RECEIPT_SCHEMA = "leaf.production-web-deployment.v1"
APPROVAL_SCHEMA = "leaf.production-web-approval.v2"
NATIVE_REPOSITORY = {"id": 46, "full_name": "LEAF-Solar-Design/leaf-web-demo"}
NATIVE_PRODUCER_PROJECT = (
    "arn:aws:codebuild:us-east-1:807034087062:project/leaf-studio-native-release"
)
NATIVE_GATE_PROJECT = (
    "arn:aws:codebuild:us-east-1:807034087062:project/leaf-studio-native-gate"
)
NATIVE_DELIVERY_BUCKET = "leaf-native-staging-delivery-807034087062"
NATIVE_RELEASE_BUCKET = "leaf-studio-release-artifacts-807034087062-us-east-1"
NATIVE_CONTRACT_BUCKET = "leaf-developer-platform-artifacts-807034087062-us-east-1"
NATIVE_STAGING_LISTENER = (
    "arn:aws:elasticloadbalancing:us-east-1:807034087062:listener/app/"
    "leaf-automation-staging-api/d5cae470e8fcdbae/96cc5c0ab89ab4d3"
)
NATIVE_BRANCH_SCOPE = "canonical-native-five-service-staging"
# The two approval modes the production deploy workflow can record. Closed set:
# an unrecognized mode is refused rather than treated as independent.
_APPROVAL_MODES = frozenset({"independent", "administrator-self-authorization"})

_SHA = re.compile(r"^[0-9a-f]{40}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_DIGEST = re.compile(r"^sha256:[0-9a-f]{64}$")
_TRANSACTION = re.compile(r"^d10-[0-9a-f]{16}$")
_CODEBUILD_ARN = re.compile(
    r"^arn:aws:codebuild:us-east-1:807034087062:"
    r"(?:project/[A-Za-z0-9_-]+|build/[A-Za-z0-9_-]+:"
    r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})$"
)
_RUN_ID = re.compile(r"^[1-9][0-9]{5,19}$")
_ATTEMPT = re.compile(r"^[1-9][0-9]*$")
_DEPLOYMENT_ID = re.compile(r"^dpl_[A-Za-z0-9]{20,64}$")
_DEPLOYMENT_URL = re.compile(r"^[a-z0-9][a-z0-9-]*\.vercel\.app$")
_ENTRY_ASSET = re.compile(rb"assets/index-[A-Za-z0-9_-]+\.js")
_LOGIN = re.compile(r"^[A-Za-z0-9-]{1,39}$")
_UTC = re.compile(r"^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z$")
_NATIVE_UTC = re.compile(
    r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?Z$"
)


class ReleaseError(ValueError):
    """The supplied deployment evidence is not safe to publish."""


def _exact(value: dict[str, Any], keys: set[str], label: str) -> None:
    if set(value) != keys:
        raise ReleaseError(f"{label} has unsupported or missing fields")


def _positive(value: str, pattern: re.Pattern[str], label: str) -> int:
    if not pattern.fullmatch(value):
        raise ReleaseError(f"{label} is invalid")
    return int(value)


def _canonical(value: dict[str, Any]) -> bytes:
    return (json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n").encode()


def _canonical_json(value: bytes, label: str) -> dict[str, Any]:
    try:
        parsed = json.loads(value)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ReleaseError(f"{label} is unreadable") from exc
    if not isinstance(parsed, dict) or _canonical(parsed) != value:
        raise ReleaseError(f"{label} bytes are not canonical")
    return parsed


def _sha256(value: Any, label: str) -> str:
    if not isinstance(value, str) or not _SHA256.fullmatch(value):
        raise ReleaseError(f"{label} is invalid")
    return value


def _object_ref(value: Any, label: str, *, revision: bool = False) -> dict[str, Any]:
    fields = {"bucket", "key", "version_id", "sha256"}
    if revision:
        fields.add("revision")
    if not isinstance(value, dict):
        raise ReleaseError(f"{label} is invalid")
    _exact(value, fields, label)
    if any(
        not isinstance(value[field], str) or not value[field]
        for field in ("bucket", "key", "version_id")
    ):
        raise ReleaseError(f"{label} is incomplete")
    if value["version_id"] == "null":
        raise ReleaseError(f"{label} is mutable")
    _sha256(value["sha256"], f"{label} digest")
    if revision and (type(value["revision"]) is not int or value["revision"] < 1):
        raise ReleaseError(f"{label} revision is invalid")
    return value


def _build_identity(
    value: Any, label: str, expected_project: str
) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ReleaseError(f"{label} is invalid")
    _exact(value, {"project_arn", "build_arn", "build_number"}, label)
    if not isinstance(value["project_arn"], str) or not _CODEBUILD_ARN.fullmatch(
        value["project_arn"]
    ):
        raise ReleaseError(f"{label} project is invalid")
    if value["project_arn"] != expected_project:
        raise ReleaseError(f"{label} project is not the canonical native project")
    if not isinstance(value["build_arn"], str) or not _CODEBUILD_ARN.fullmatch(
        value["build_arn"]
    ):
        raise ReleaseError(f"{label} build is invalid")
    project = value["project_arn"].split("project/", 1)[-1]
    if f"build/{project}:" not in value["build_arn"]:
        raise ReleaseError(f"{label} build belongs to another project")
    if type(value["build_number"]) is not int or value["build_number"] < 1:
        raise ReleaseError(f"{label} build number is invalid")
    return value


def _native_delivery_ref(
    value: Any,
    transaction_id: str,
    name: str,
    label: str,
    *,
    revision: bool,
) -> dict[str, Any]:
    reference = _object_ref(value, label, revision=revision)
    expected_key = f"delivery/v1/{transaction_id}/{reference['sha256']}/{name}"
    if reference["bucket"] != NATIVE_DELIVERY_BUCKET or reference["key"] != expected_key:
        raise ReleaseError(f"{label} is outside the canonical delivery prefix")
    return reference


def _native_release_ref(
    value: Any, producer: dict[str, Any], label: str
) -> dict[str, Any]:
    """Bind a release archive to the canonical CodeBuild artifact object."""
    reference = _object_ref(value, label)
    build_id = producer["build_arn"].rsplit(":", 1)[-1]
    if (
        reference["bucket"] != NATIVE_RELEASE_BUCKET
        or reference["key"] != f"release/{build_id}/evidence.zip"
    ):
        raise ReleaseError(f"{label} is outside the canonical native release lane")
    return reference


def _native_contract_ref(value: Any, label: str) -> dict[str, Any]:
    reference = _object_ref(value, label)
    if (
        reference["bucket"] != NATIVE_CONTRACT_BUCKET
        or reference["sha256"] not in reference["key"].split("/")
        or any(part in {"", ".", ".."} for part in reference["key"].split("/"))
    ):
        raise ReleaseError(f"{label} is not the pinned producer contract")
    return reference


def _native_success_readback(
    value: Any, identity: dict[str, Any], label: str
) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ReleaseError(f"{label} readback is invalid")
    _exact(
        value,
        {"project_arn", "build_arn", "build_number", "status"},
        f"{label} readback",
    )
    if value["status"] != "SUCCEEDED" or {
        key: value[key] for key in identity
    } != identity:
        raise ReleaseError(f"{label} readback does not prove success")
    return value


def _native_tags(value: Any, source: str, label: str) -> None:
    if (
        not isinstance(value, list)
        or any(not isinstance(row, dict) or set(row) != {"key", "value"} for row in value)
        or any(not isinstance(row["key"], str) or not isinstance(row["value"], str) for row in value)
        or len({row["key"] for row in value}) != len(value)
    ):
        raise ReleaseError(f"semantic {label} tags are invalid")
    if {row["key"]: row["value"] for row in value}.get("leaf:source") != source:
        raise ReleaseError(f"semantic {label} source tag differs")


def _native_route(value: Any, name: str) -> None:
    if name in {"app", "web"}:
        priorities = {"44", "50", "51"} if name == "app" else {"60"}
        if (
            not isinstance(value, dict)
            or set(value) != {"kind", "listener", "rules"}
            or value["kind"] != "alb"
            or value["listener"] != NATIVE_STAGING_LISTENER
            or not isinstance(value["rules"], dict)
            or set(value["rules"]) != priorities
        ):
            raise ReleaseError(f"semantic {name} route is not canonical staging")
        rule_prefix = (
            NATIVE_STAGING_LISTENER.replace(":listener/", ":listener-rule/") + "/"
        )
        target_prefix = (
            "arn:aws:elasticloadbalancing:us-east-1:807034087062:targetgroup/"
            "leaf-stg-platform-"
        )
        for row in value["rules"].values():
            if (
                not isinstance(row, dict)
                or set(row) != {"arn", "actions", "conditions"}
                or not isinstance(row["arn"], str)
                or not row["arn"].startswith(rule_prefix)
                or not isinstance(row["actions"], list)
                or not row["actions"]
                or not isinstance(row["conditions"], list)
            ):
                raise ReleaseError(f"semantic {name} route rule is invalid")
            encoded = json.dumps(row, sort_keys=True)
            targets = re.findall(r'"TargetGroupArn":\s*"([^"]+)"', encoded)
            if (
                not targets
                or any(not target.startswith(target_prefix) for target in targets)
                or "platform-staging.leafdesign.ai" not in encoded
            ):
                raise ReleaseError(f"semantic {name} route leaves staging")
    elif value != {"kind": "unrouted"}:
        raise ReleaseError(f"semantic {name} route is not canonical staging")


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


def _validate_native_manifest(
    manifest: dict[str, Any], handoff: dict[str, Any], source: str, tree: str
) -> None:
    _exact(
        manifest,
        {
            "schema",
            "provider",
            "source_revision",
            "source_tree",
            "producer",
            "services",
            "solver",
            "web",
            "gate",
        },
        "native release manifest",
    )
    if (
        manifest["schema"] != "leaf.native-release.v1"
        or manifest["provider"] != "aws.codebuild"
        or manifest["source_revision"] != source
        or manifest["source_tree"] != tree
    ):
        raise ReleaseError("native release manifest identity differs")
    release = handoff["release"]
    if manifest["producer"] != release["producer"] or manifest["gate"] != release["gate"]:
        raise ReleaseError("native release build identity differs from the handoff")
    services = handoff["staging_supply_set_services"]
    native_services = manifest["services"]
    if not isinstance(native_services, dict) or set(native_services) != set(SERVICES):
        raise ReleaseError("native release manifest service set differs")
    producer = release["producer"]
    for name in SERVICES:
        row = native_services[name]
        if not isinstance(row, dict):
            raise ReleaseError(f"native {name} service evidence is invalid")
        _exact(
            row,
            {"repository", "image_digest", "source_revision", "native_build_number"},
            f"native {name} service evidence",
        )
        if row != {
            "repository": services[name]["repository"],
            "image_digest": services[name]["image_digest"],
            "source_revision": source,
            "native_build_number": producer["build_number"],
        }:
            raise ReleaseError(f"native {name} service differs from the handoff")
    solver = manifest["solver"]
    if not isinstance(solver, dict):
        raise ReleaseError("native solver provenance is invalid")
    _exact(solver, {"revision", "source_sha256"}, "native solver provenance")
    expected_solver = services["canonical-worker"]["provenance"]
    if solver != {
        "revision": expected_solver["solver_source_revision"],
        "source_sha256": expected_solver["solver_source_sha256"],
    }:
        raise ReleaseError("native solver provenance differs from the handoff")
    native_web = manifest["web"]
    if not isinstance(native_web, dict):
        raise ReleaseError("native web evidence is invalid")
    _exact(
        native_web,
        {"member", "artifact_sha256", "archive_sha256"},
        "native web evidence",
    )
    if native_web != {
        key: handoff["web"][key]
        for key in ("member", "artifact_sha256", "archive_sha256")
    }:
        raise ReleaseError("native web evidence differs from the handoff")


def _validate_native_semantic(
    semantic: dict[str, Any], handoff: dict[str, Any], source: str, tree: str
) -> None:
    acceptance = handoff["staging_acceptance"]
    _exact(
        semantic,
        {
            "schema",
            "transaction_id",
            "settled",
            "services",
            "activity",
            "source_revision",
            "source_tree",
            "repository",
            "branch",
        },
        "semantic LIVE receipt",
    )
    if (
        semantic["schema"] != "leaf.native-staging-semantic.v1"
        or semantic["transaction_id"] != acceptance["transaction_id"]
        or semantic["settled"] is not True
        or semantic["source_revision"] != source
        or semantic["source_tree"] != tree
        or semantic["repository"] != acceptance["repository"]
        or semantic["branch"] != "main"
        or not isinstance(semantic["activity"], (dict, type(None)))
    ):
        raise ReleaseError("semantic LIVE receipt differs from the handoff")
    observed = semantic["services"]
    if not isinstance(observed, dict) or set(observed) != set(SERVICES):
        raise ReleaseError("semantic LIVE receipt service set differs")
    for name in SERVICES:
        row = observed[name]
        if not isinstance(row, dict):
            raise ReleaseError(f"semantic {name} service evidence is invalid")
        _exact(
            row,
            {"image", "config", "tags", "route", "evidence"},
            f"semantic {name} service evidence",
        )
        if row["image"] != acceptance["images"][name]:
            raise ReleaseError(f"semantic {name} image differs from the handoff")
        _sha256(row["config"], f"semantic {name} config digest")
        _native_tags(row["tags"], source, name)
        _native_route(row["route"], name)
        evidence = _native_delivery_ref(
            row["evidence"],
            acceptance["transaction_id"],
            "readback.json",
            f"semantic {name} evidence",
            revision=True,
        )
        if evidence != acceptance["service_evidence"][name]:
            raise ReleaseError(f"semantic {name} evidence differs from the handoff")


def validate_native_bundle(
    bundle: dict[str, bytes], *, source: str
) -> dict[str, Any]:
    """Validate the closed Forge-native handoff bundle without writing files."""
    if not isinstance(bundle, dict) or set(bundle) != {
        "staging-supply-set.json",
        "web-dist.zip",
        "semantic-live.json",
        "production-handoff-candidate.json",
    } or any(not isinstance(value, bytes) for value in bundle.values()):
        raise ReleaseError("Forge-native handoff bundle is incomplete or open-ended")
    if not _SHA.fullmatch(source):
        raise ReleaseError("source revision is invalid")
    handoff_bytes = bundle["production-handoff-candidate.json"]
    handoff = _canonical_json(handoff_bytes, "Forge-native production handoff")
    _exact(
        handoff,
        {
            "schema",
            "provider",
            "source_revision",
            "source_tree",
            "release",
            "staging_acceptance",
            "staging_supply_set_services",
            "web",
            "proof",
        },
        "Forge-native production handoff",
    )
    if (
        handoff["schema"] != HANDOFF_V2_SCHEMA
        or handoff["provider"] != "forge-native"
        or handoff["source_revision"] != source
    ):
        raise ReleaseError("Forge-native handoff schema, provider, or source differs")
    tree = handoff["source_tree"]
    if not isinstance(tree, str) or not _SHA.fullmatch(tree):
        raise ReleaseError("Forge-native handoff tree is invalid")

    release = handoff["release"]
    if not isinstance(release, dict):
        raise ReleaseError("Forge-native release evidence is invalid")
    _exact(
        release,
        {
            "transaction_id",
            "source_revision",
            "source_tree",
            "producer",
            "producer_readback",
            "gate",
            "gate_readback",
            "artifact",
            "manifest_sha256",
            "producer_contract",
        },
        "Forge-native release evidence",
    )
    transaction_id = release["transaction_id"]
    if not isinstance(transaction_id, str) or not _TRANSACTION.fullmatch(transaction_id):
        raise ReleaseError("Forge-native transaction ID is invalid")
    if release["source_revision"] != source or release["source_tree"] != tree:
        raise ReleaseError("Forge-native release source differs")
    producer = _build_identity(
        release["producer"], "Forge-native producer", NATIVE_PRODUCER_PROJECT
    )
    _native_success_readback(
        release["producer_readback"], producer, "Forge-native producer"
    )
    gate = release["gate"]
    if not isinstance(gate, dict):
        raise ReleaseError("Forge-native gate evidence is invalid")
    _exact(
        gate,
        {"producer", "source_revision", "source_tree", "archive", "proof_sha256"},
        "Forge-native gate evidence",
    )
    gate_producer = _build_identity(
        gate["producer"], "Forge-native gate producer", NATIVE_GATE_PROJECT
    )
    _native_success_readback(
        release["gate_readback"], gate_producer, "Forge-native gate"
    )
    if gate["source_revision"] != source or gate["source_tree"] != tree:
        raise ReleaseError("Forge-native gate source differs")
    gate_archive = _object_ref(gate["archive"], "Forge-native gate archive")
    gate_build_id = gate_producer["build_arn"].rsplit(":", 1)[-1]
    if (
        gate_archive["bucket"] != NATIVE_RELEASE_BUCKET
        or gate_archive["key"] != f"gate/{gate_build_id}/evidence.zip"
    ):
        raise ReleaseError("Forge-native gate archive is outside the canonical lane")
    _sha256(gate["proof_sha256"], "Forge-native gate proof digest")
    release_object = _native_release_ref(
        release["artifact"],
        producer,
        "Forge-native release object",
    )
    producer_contract = _native_contract_ref(
        release["producer_contract"], "Forge-native producer contract"
    )
    _sha256(release["manifest_sha256"], "native release manifest digest")

    services = _validate_service_set(handoff, source)
    acceptance = handoff["staging_acceptance"]
    if not isinstance(acceptance, dict):
        raise ReleaseError("Forge-native staging acceptance is invalid")
    _exact(
        acceptance,
        {
            "schema",
            "transaction_id",
            "environment",
            "state",
            "semantic_receipt",
            "live_binding_revision",
            "repository",
            "branch",
            "source_revision",
            "source_tree",
            "images",
            "web",
            "service_evidence",
            "authority_receipt",
            "branch_authority",
            "transaction_readback",
            "canonical_head",
            "settled",
        },
        "Forge-native staging acceptance",
    )
    repository = acceptance["repository"]
    if not isinstance(repository, dict):
        raise ReleaseError("Forge-native staging repository is invalid")
    _exact(repository, {"id", "full_name"}, "Forge-native staging repository")
    if (
        repository != NATIVE_REPOSITORY
    ):
        raise ReleaseError("Forge-native staging repository is invalid")
    expected_images = {name: services[name]["image_digest"] for name in SERVICES}
    if (
        acceptance["schema"] != "leaf.native-staging-semantic.v1"
        or acceptance["transaction_id"] != transaction_id
        or acceptance["environment"] != "staging"
        or acceptance["state"] != "LIVE"
        or acceptance["branch"] != "main"
        or acceptance["source_revision"] != source
        or acceptance["source_tree"] != tree
        or acceptance["images"] != expected_images
        or acceptance["settled"] is not True
        or type(acceptance["live_binding_revision"]) is not int
        or acceptance["live_binding_revision"] < 1
    ):
        raise ReleaseError("Forge-native staging acceptance differs")
    semantic_ref = _native_delivery_ref(
        acceptance["semantic_receipt"],
        transaction_id,
        "semantic-live.json",
        "Forge-native semantic receipt",
        revision=True,
    )
    service_evidence = acceptance["service_evidence"]
    if not isinstance(service_evidence, dict) or set(service_evidence) != set(SERVICES):
        raise ReleaseError("Forge-native service evidence set differs")
    for name in SERVICES:
        _native_delivery_ref(
            service_evidence[name],
            transaction_id,
            "readback.json",
            f"Forge-native {name} service evidence",
            revision=True,
        )

    authority_ref = _native_delivery_ref(
        acceptance["authority_receipt"],
        transaction_id,
        "authority-tx.json",
        "Forge-native authority receipt",
        revision=True,
    )
    authority = acceptance["branch_authority"]
    if not isinstance(authority, dict):
        raise ReleaseError("Forge-native branch authority is invalid")
    _exact(
        authority,
        {
            "schema",
            "transaction_id",
            "grant",
            "proof",
            "repository_id",
            "branch",
            "source_revision",
            "source_tree",
            "target",
            "scope",
            "expires_at",
        },
        "Forge-native branch authority",
    )
    _object_ref(authority["grant"], "Forge-native branch grant")
    _object_ref(authority["proof"], "Forge-native trusted CI proof")
    if (
        authority["schema"] != "leaf.native-staging-authority-binding.v1"
        or authority["transaction_id"] != transaction_id
        or authority["repository_id"] != NATIVE_REPOSITORY["id"]
        or authority["branch"] != "main"
        or authority["source_revision"] != source
        or authority["source_tree"] != tree
        or authority["target"] != "staging"
        or authority["scope"] != NATIVE_BRANCH_SCOPE
        or not isinstance(authority["expires_at"], str)
        or not _NATIVE_UTC.fullmatch(authority["expires_at"])
        or hashlib.sha256(_canonical(authority)).hexdigest() != authority_ref["sha256"]
    ):
        raise ReleaseError("Forge-native branch authority differs")

    transaction = acceptance["transaction_readback"]
    if not isinstance(transaction, dict):
        raise ReleaseError("Forge-native transaction readback is invalid")
    _exact(
        transaction,
        {
            "schema",
            "transaction_id",
            "state",
            "repository",
            "branch",
            "commit",
            "tree",
            "semantic_receipt",
            "authority_receipt",
            "producer_contract",
            "revision",
        },
        "Forge-native transaction readback",
    )
    if transaction != {
        "schema": "leaf.native-staging-transaction.v1",
        "transaction_id": transaction_id,
        "state": "LIVE",
        "repository": NATIVE_REPOSITORY,
        "branch": "main",
        "commit": source,
        "tree": tree,
        "semantic_receipt": semantic_ref,
        "authority_receipt": authority_ref,
        "producer_contract": producer_contract,
        "revision": transaction["revision"],
    } or type(transaction["revision"]) is not int or transaction["revision"] < 1:
        raise ReleaseError("Forge-native transaction readback differs")
    canonical_head = acceptance["canonical_head"]
    if not isinstance(canonical_head, dict):
        raise ReleaseError("Forge-native canonical head is invalid")
    _exact(
        canonical_head,
        {"repository", "branch", "commit", "tree"},
        "Forge-native canonical head",
    )
    if canonical_head != {
        "repository": NATIVE_REPOSITORY,
        "branch": "main",
        "commit": source,
        "tree": tree,
    }:
        raise ReleaseError("Forge-native source is not canonical main")

    web = handoff["web"]
    if not isinstance(web, dict):
        raise ReleaseError("Forge-native web evidence is invalid")
    _exact(
        web,
        {"member", "artifact_sha256", "archive_sha256", "release_object"},
        "Forge-native web evidence",
    )
    if web["member"] != "web-dist.zip" or web["release_object"] != release_object:
        raise ReleaseError("Forge-native web object differs from the release")
    web_artifact_sha = _sha256(web["artifact_sha256"], "web artifact digest")
    if services["web"]["artifact_sha256"] != web_artifact_sha:
        raise ReleaseError("Forge-native web content identity differs")
    web_archive_sha = _sha256(web["archive_sha256"], "web archive digest")
    if acceptance["web"] != {
        "artifact_sha256": web_artifact_sha,
        "archive_sha256": web_archive_sha,
    }:
        raise ReleaseError("Forge-native accepted web digests differ")
    manifest_bytes = bundle["staging-supply-set.json"]
    if hashlib.sha256(manifest_bytes).hexdigest() != release["manifest_sha256"]:
        raise ReleaseError("native release manifest digest differs from the handoff")
    manifest = _canonical_json(manifest_bytes, "native release manifest")
    _validate_native_manifest(manifest, handoff, source, tree)
    semantic_bytes = bundle["semantic-live.json"]
    if hashlib.sha256(semantic_bytes).hexdigest() != semantic_ref["sha256"]:
        raise ReleaseError("semantic LIVE receipt digest differs from the handoff")
    semantic = _canonical_json(semantic_bytes, "semantic LIVE receipt")
    _validate_native_semantic(semantic, handoff, source, tree)
    if hashlib.sha256(bundle["web-dist.zip"]).hexdigest() != web_archive_sha:
        raise ReleaseError("web archive bytes differ from the Forge-native handoff")
    if _native_web_members(bundle["web-dist.zip"]) != web_artifact_sha:
        raise ReleaseError("web content digest differs from the Forge-native handoff")
    derived_proof = {
        "source_is_canonical_main": canonical_head["commit"] == source,
        "producer_and_gate_succeeded": (
            release["producer_readback"]["status"] == "SUCCEEDED"
            and release["gate_readback"]["status"] == "SUCCEEDED"
        ),
        "staging_state_is_live": (
            acceptance["environment"] == "staging"
            and acceptance["state"] == "LIVE"
            and transaction["state"] == "LIVE"
            and semantic["settled"] is True
        ),
        "staging_digests_equal_release": (
            acceptance["images"]
            == {name: services[name]["image_digest"] for name in SERVICES}
            and acceptance["web"]
            == {
                "artifact_sha256": web_artifact_sha,
                "archive_sha256": web_archive_sha,
            }
        ),
        "web_archive_bytes_equal_release": (
            hashlib.sha256(bundle["web-dist.zip"]).hexdigest()
            == web_archive_sha
        ),
    }
    if handoff["proof"] != derived_proof or not all(derived_proof.values()):
        raise ReleaseError("Forge-native handoff proof is incomplete")
    return handoff


def _prepare_web_output(
    web_dist: Path,
    output_root: Path,
    *,
    source: str,
    expected_web_sha256: str,
    require_native_engine: bool,
) -> tuple[str, str]:
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
    if require_native_engine:
        build_config = load_json(web_dist / "build-config.json")
        if build_config != {
            "schema": "leaf.web-build-config.v1",
            "vite_cad_edit": "1",
        }:
            raise ReleaseError("web artifact does not enable the reviewed CAD engine")
        provenance = load_json(web_dist / "engine" / "PROVENANCE.json")
        if not isinstance(provenance, dict):
            raise ReleaseError("web engine provenance is invalid")
        _exact(provenance, {"contract", "files"}, "web engine provenance")
        if (
            provenance["contract"] != "leaf.cad-engine-stage.v1"
            or not isinstance(provenance["files"], dict)
            or set(provenance["files"]) != {"engine.js", "engine_bg.wasm"}
        ):
            raise ReleaseError("web engine provenance differs")
        for name in ("engine.js", "engine_bg.wasm"):
            record = provenance["files"][name]
            if not isinstance(record, dict):
                raise ReleaseError(f"web engine provenance differs: {name}")
            _exact(record, {"sha256", "bytes"}, f"{name} engine provenance")
            path = web_dist / "engine" / name
            content = path.read_bytes()
            if (
                not content
                or type(record["bytes"]) is not int
                or record["bytes"] != len(content)
                or record["sha256"] != hashlib.sha256(content).hexdigest()
            ):
                raise ReleaseError(f"web engine provenance differs: {name}")

    if output_root.exists():
        raise ReleaseError("Vercel output root already exists")
    static = output_root / "static"
    static.mkdir(parents=True)
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
    return actual_hash, entries[0]


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

    actual_hash, entry = _prepare_web_output(
        web_dist,
        output_root,
        source=source,
        expected_web_sha256=expected_web_sha256,
        require_native_engine=False,
    )
    return {
        "schema": PREPARED_SCHEMA,
        "source_revision": source,
        "web_artifact_sha256": actual_hash,
        "entry_asset": entry,
        "release_workflow_run_id": release_id,
        "release_workflow_run_attempt": attempt,
        "api_boundary": "terminal-404",
        "build_performed": False,
    }


def _native_web_members(archive: bytes) -> str:
    digest = hashlib.sha256(b"leaf.web-dist.v1\0")
    try:
        with zipfile.ZipFile(io.BytesIO(archive)) as bundle:
            members = bundle.infolist()
            names = [member.filename for member in members]
            if not names or names != sorted(names) or len(names) != len(set(names)):
                raise ReleaseError("native web archive paths are missing, reordered, or duplicate")
            for member in members:
                name = member.filename
                mode = (member.external_attr >> 16) & 0o170000
                parts = name.split("/")
                if (
                    member.is_dir()
                    or not name.startswith("dist/")
                    or name.startswith("/")
                    or "\\" in name
                    or any(part in {"", ".", ".."} for part in parts)
                    or mode not in {0, stat.S_IFREG}
                ):
                    raise ReleaseError("native web archive contains an unsupported path")
                relative = name.removeprefix("dist/").encode()
                payload = bundle.read(member)
                digest.update(len(relative).to_bytes(8, "big"))
                digest.update(relative)
                digest.update(len(payload).to_bytes(8, "big"))
                digest.update(payload)
    except (OSError, zipfile.BadZipFile, RuntimeError) as exc:
        raise ReleaseError("native web archive is malformed") from exc
    return digest.hexdigest()


def _read_native_bundle(bundle_root: Path) -> dict[str, bytes]:
    expected = {
        "staging-supply-set.json",
        "web-dist.zip",
        "semantic-live.json",
        "production-handoff-candidate.json",
    }
    try:
        members = list(bundle_root.iterdir())
    except OSError as exc:
        raise ReleaseError("Forge-native handoff bundle is unreadable") from exc
    if set(path.name for path in members) != expected or any(
        not path.is_file() or path.is_symlink() for path in members
    ):
        raise ReleaseError("Forge-native handoff bundle is incomplete or open-ended")
    try:
        return {path.name: path.read_bytes() for path in members}
    except OSError as exc:
        raise ReleaseError("Forge-native handoff bundle is unreadable") from exc


def prepare_native(
    bundle_root: Path,
    output_root: Path,
    *,
    source: str,
) -> dict[str, Any]:
    """Prepare a verified Forge-native bundle without inventing workflow IDs."""
    bundle = _read_native_bundle(bundle_root)
    handoff = validate_native_bundle(bundle, source=source)
    archive = bundle["web-dist.zip"]
    _native_web_members(archive)
    if output_root.exists():
        raise ReleaseError("Vercel output root already exists")
    output_root.parent.mkdir(parents=True, exist_ok=True)
    temporary_root = output_root.parent / f".forge-native-web-{uuid.uuid4().hex}"
    temporary_root.mkdir()
    try:
        extracted = temporary_root / "extracted"
        extracted.mkdir()
        with zipfile.ZipFile(io.BytesIO(archive)) as web_bundle:
            web_bundle.extractall(extracted)
        actual_hash, entry = _prepare_web_output(
            extracted / "dist",
            output_root,
            source=source,
            expected_web_sha256=handoff["web"]["artifact_sha256"],
            require_native_engine=True,
        )
    finally:
        shutil.rmtree(temporary_root)
    release = handoff["release"]
    acceptance = handoff["staging_acceptance"]
    return {
        "schema": PREPARED_V2_SCHEMA,
        "provider": "forge-native",
        "source_revision": source,
        "source_tree": handoff["source_tree"],
        "web_artifact_sha256": actual_hash,
        "web_archive_sha256": handoff["web"]["archive_sha256"],
        "entry_asset": entry,
        "transaction_id": release["transaction_id"],
        "release_object": release["artifact"],
        "producer": release["producer"],
        "gate": release["gate"],
        "semantic_receipt": acceptance["semantic_receipt"],
        "live_binding_revision": acceptance["live_binding_revision"],
        "handoff_sha256": hashlib.sha256(
            bundle["production-handoff-candidate.json"]
        ).hexdigest(),
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
        "schema",
        "project_id",
        "deployment_id",
        "source_revision",
        "release_workflow_run_id",
        "release_workflow_run_attempt",
        "handoff_workflow_run_id",
        "handoff_workflow_run_attempt",
        "web_artifact_sha256",
        "workflow_head_sha",
        "deployment_workflow_run_id",
        "deployment_workflow_run_attempt",
        "issue_number",
        "comment_id",
        "approver_login",
        "approver_id",
        "permission",
        "created_at",
        "validated_at",
        "approval_payload_sha256",
        "exact_body_verified",
        "approval_mode",
        "author_separated",
        "timely_at_promotion",
    }
    _exact(approval, approval_keys, "production approval proof")
    if (
        approval["schema"] != APPROVAL_SCHEMA
        or approval["project_id"] != PROJECT_ID
        or approval["deployment_id"] != deployment.get("id")
        or approval["source_revision"] != prepared["source_revision"]
        or approval["release_workflow_run_id"] != prepared["release_workflow_run_id"]
        or approval["release_workflow_run_attempt"]
        != prepared["release_workflow_run_attempt"]
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
        or approval["approval_mode"] not in _APPROVAL_MODES
        # author_separated is no longer an invariant, it is a FACT ABOUT the mode,
        # so it must agree with the mode rather than being asserted true. A proof
        # claiming independence while naming the self-authorization mode (or the
        # reverse) is contradictory evidence and fails closed here.
        or approval["author_separated"] is not (approval["approval_mode"] == "independent")
        # Defence in depth: the workflow already requires live admin to
        # self-authorize, and the verifier re-asserts it against the recorded
        # permission so a forged or replayed proof cannot self-authorize on write.
        or (
            approval["approval_mode"] == "administrator-self-authorization"
            and approval["permission"] != "admin"
        )
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
        "production_hosts": list(PRODUCTION_HOSTS),
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
    native = commands.add_parser("prepare-native")
    native.add_argument("--bundle-root", type=Path, required=True)
    native.add_argument("--output-root", type=Path, required=True)
    native.add_argument("--source", required=True)
    native.add_argument("--proof-output", type=Path, required=True)
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
        if args.command == "prepare-native":
            value = prepare_native(
                args.bundle_root,
                args.output_root,
                source=args.source,
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
