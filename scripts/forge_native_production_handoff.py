#!/usr/bin/env python3
"""Build a provider-bound production handoff from native staging evidence.

This module is deliberately pure: callers must fetch and authenticate provider
readbacks before invoking it.  It writes no receipt, starts no build, and has no
deployment or Vercel capability.
"""

from __future__ import annotations

from copy import deepcopy
import hashlib
import io
import json
import re
import stat
from typing import Any
import zipfile


SERVICES = ("app", "broker", "canonical-worker", "harness", "web")
HANDOFF_SCHEMA = "leaf.production-handoff-candidate.v2"
NATIVE_RELEASE_SCHEMA = "leaf.native-release.v1"
SEMANTIC_SCHEMA = "leaf.native-staging-semantic.v1"
LIVE_BINDING_SCHEMA = "leaf.native-staging-live-binding.v1"
TRANSACTION_SCHEMA = "leaf.native-staging-transaction.v1"
AUTHORITY_SCHEMA = "leaf.native-staging-authority-binding.v1"

ACCOUNT = "807034087062"
REGION = "us-east-1"
REPOSITORY = {"id": 46, "full_name": "LEAF-Solar-Design/leaf-web-demo"}
PRODUCER_PROJECT = f"arn:aws:codebuild:{REGION}:{ACCOUNT}:project/leaf-studio-native-release"
GATE_PROJECT = f"arn:aws:codebuild:{REGION}:{ACCOUNT}:project/leaf-studio-native-gate"
DELIVERY_BUCKET = "leaf-native-staging-delivery-807034087062"
RELEASE_BUCKET = "leaf-studio-release-artifacts-807034087062-us-east-1"
CONTRACT_BUCKET = "leaf-developer-platform-artifacts-807034087062-us-east-1"
STAGING_LISTENER = (
    f"arn:aws:elasticloadbalancing:{REGION}:{ACCOUNT}:listener/app/"
    "leaf-automation-staging-api/d5cae470e8fcdbae/96cc5c0ab89ab4d3"
)
BRANCH_SCOPE = "canonical-native-five-service-staging"

_SHA = re.compile(r"^[0-9a-f]{40}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_DIGEST = re.compile(r"^sha256:[0-9a-f]{64}$")
_TX = re.compile(r"^d10-[0-9a-f]{16}$")
_UTC = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?Z$")
_ARN = re.compile(
    r"^arn:aws:codebuild:us-east-1:807034087062:"
    r"(?:project/[A-Za-z0-9_-]+|build/[A-Za-z0-9_-]+:"
    r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})$"
)


class HandoffError(ValueError):
    """The supplied native evidence cannot authorize a production handoff."""


def _exact(value: Any, fields: set[str], label: str) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != fields:
        raise HandoffError(f"{label} has unsupported or missing fields")
    return value


def _hex(value: Any, pattern: re.Pattern[str], label: str) -> str:
    if not isinstance(value, str) or not pattern.fullmatch(value):
        raise HandoffError(f"{label} is invalid")
    return value


def _canonical(value: dict[str, Any]) -> bytes:
    return (json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n").encode()


def _object_ref(value: Any, label: str, *, revision: bool = False) -> dict[str, Any]:
    fields = {"bucket", "key", "version_id", "sha256"}
    if revision:
        fields.add("revision")
    value = _exact(value, fields, label)
    if any(not isinstance(value[key], str) or not value[key] for key in ("bucket", "key", "version_id")):
        raise HandoffError(f"{label} is incomplete")
    if value["version_id"] == "null":
        raise HandoffError(f"{label} is mutable")
    _hex(value["sha256"], _SHA256, f"{label} digest")
    if revision and (type(value["revision"]) is not int or value["revision"] < 1):
        raise HandoffError(f"{label} revision is invalid")
    return deepcopy(value)


def _repository(value: Any, label: str) -> dict[str, Any]:
    value = _exact(value, {"id", "full_name"}, label)
    if type(value["id"]) is not int or value["id"] < 1:
        raise HandoffError(f"{label} id is invalid")
    if not isinstance(value["full_name"], str) or not re.fullmatch(
        r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", value["full_name"]
    ):
        raise HandoffError(f"{label} name is invalid")
    return deepcopy(value)


def _build_identity(value: Any, label: str, expected_project: str) -> dict[str, Any]:
    value = _exact(value, {"project_arn", "build_arn", "build_number"}, label)
    if not isinstance(value["project_arn"], str) or not _ARN.fullmatch(value["project_arn"]):
        raise HandoffError(f"{label} project is invalid")
    if value["project_arn"] != expected_project:
        raise HandoffError(f"{label} project is not the canonical native project")
    if not isinstance(value["build_arn"], str) or not _ARN.fullmatch(value["build_arn"]):
        raise HandoffError(f"{label} build is invalid")
    project_name = value["project_arn"].split("project/", 1)[-1]
    if f"build/{project_name}:" not in value["build_arn"]:
        raise HandoffError(f"{label} build belongs to another project")
    if type(value["build_number"]) is not int or value["build_number"] < 1:
        raise HandoffError(f"{label} build number is invalid")
    return deepcopy(value)


def _successful_readback(value: Any, identity: dict[str, Any], label: str) -> dict[str, Any]:
    value = _exact(
        value,
        {"project_arn", "build_arn", "build_number", "status"},
        f"{label} readback",
    )
    if value["status"] != "SUCCEEDED":
        raise HandoffError(f"{label} build did not succeed")
    if {key: value[key] for key in identity} != identity:
        raise HandoffError(f"{label} readback differs from the manifest")
    return deepcopy(value)


def _release_members(archive: Any, reference: dict[str, Any]) -> tuple[dict[str, Any], bytes]:
    if not isinstance(archive, bytes) or hashlib.sha256(archive).hexdigest() != reference["sha256"]:
        raise HandoffError("native release archive differs from its immutable object")
    try:
        with zipfile.ZipFile(io.BytesIO(archive)) as bundle:
            names = bundle.namelist()
            if len(names) != 2 or set(names) != {"staging-supply-set.json", "web-dist.zip"}:
                raise HandoffError("native release archive has unsupported members")
            manifest_bytes = bundle.read("staging-supply-set.json")
            web_archive = bundle.read("web-dist.zip")
    except (OSError, zipfile.BadZipFile, KeyError) as exc:
        raise HandoffError("native release archive is unreadable") from exc
    try:
        manifest = json.loads(manifest_bytes)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise HandoffError("native release manifest is unreadable") from exc
    if not isinstance(manifest, dict) or _canonical(manifest) != manifest_bytes:
        raise HandoffError("native release manifest bytes are not canonical")
    return manifest, web_archive


def _delivery_ref(value: Any, transaction_id: str, name: str, label: str, *, revision: bool) -> dict[str, Any]:
    reference = _object_ref(value, label, revision=revision)
    expected_key = f"delivery/v1/{transaction_id}/{reference['sha256']}/{name}"
    if reference["bucket"] != DELIVERY_BUCKET or reference["key"] != expected_key:
        raise HandoffError(f"{label} is outside the canonical delivery prefix")
    return reference


def _producer_contract(value: Any, label: str) -> dict[str, Any]:
    reference = _object_ref(value, label)
    if (
        reference["bucket"] != CONTRACT_BUCKET
        or reference["sha256"] not in reference["key"].split("/")
        or any(part in {"", ".", ".."} for part in reference["key"].split("/"))
    ):
        raise HandoffError(f"{label} is not the pinned producer contract")
    return reference


def _transaction_ref(value: Any, transaction_id: str, name: str, label: str) -> dict[str, Any]:
    value = _exact(
        value,
        {"bucket", "key", "version_id", "sha256", "revision", "transaction_id"},
        label,
    )
    if value["transaction_id"] != transaction_id:
        raise HandoffError(f"{label} belongs to another transaction")
    return _delivery_ref(
        {key: value[key] for key in ("bucket", "key", "version_id", "sha256", "revision")},
        transaction_id,
        name,
        label,
        revision=True,
    )


def _web_content_digest(archive: bytes) -> str:
    digest = hashlib.sha256(b"leaf.web-dist.v1\0")
    try:
        with zipfile.ZipFile(io.BytesIO(archive)) as bundle:
            members = bundle.infolist()
            names = [member.filename for member in members]
            if not names or names != sorted(names) or len(names) != len(set(names)):
                raise HandoffError("native web archive paths are missing, reordered, or duplicate")
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
                    raise HandoffError("native web archive contains an unsupported path")
                relative = name.removeprefix("dist/").encode()
                payload = bundle.read(member)
                digest.update(len(relative).to_bytes(8, "big"))
                digest.update(relative)
                digest.update(len(payload).to_bytes(8, "big"))
                digest.update(payload)
    except (OSError, zipfile.BadZipFile, RuntimeError) as exc:
        raise HandoffError("native web archive is malformed") from exc
    return digest.hexdigest()


def _staging_tags(value: Any, source: str, label: str) -> None:
    if (
        not isinstance(value, list)
        or any(not isinstance(row, dict) or set(row) != {"key", "value"} for row in value)
        or any(not isinstance(row["key"], str) or not isinstance(row["value"], str) for row in value)
        or len({row["key"] for row in value}) != len(value)
    ):
        raise HandoffError(f"{label} tags are invalid")
    tags = {row["key"]: row["value"] for row in value}
    if tags.get("leaf:source") != source:
        raise HandoffError(f"{label} source tag differs")


def _staging_route(value: Any, name: str) -> None:
    if name in {"app", "web"}:
        priorities = {"44", "50", "51"} if name == "app" else {"60"}
        if (
            not isinstance(value, dict)
            or set(value) != {"kind", "listener", "rules"}
            or value["kind"] != "alb"
            or value["listener"] != STAGING_LISTENER
            or not isinstance(value["rules"], dict)
            or set(value["rules"]) != priorities
        ):
            raise HandoffError(f"{name} semantic route is not canonical staging")
        rule_prefix = STAGING_LISTENER.replace(":listener/", ":listener-rule/") + "/"
        target_prefix = (
            f"arn:aws:elasticloadbalancing:{REGION}:{ACCOUNT}:targetgroup/"
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
                raise HandoffError(f"{name} semantic route rule is invalid")
            encoded = json.dumps(row, sort_keys=True)
            targets = re.findall(r'"TargetGroupArn":\s*"([^"]+)"', encoded)
            if (
                not targets
                or any(not target.startswith(target_prefix) for target in targets)
                or "platform-staging.leafdesign.ai" not in encoded
            ):
                raise HandoffError(f"{name} semantic route leaves staging")
    elif value != {"kind": "unrouted"}:
        raise HandoffError(f"{name} semantic route is not canonical staging")


def _branch_authority(
    value: Any,
    *,
    transaction_id: str,
    source: str,
    tree: str,
) -> dict[str, Any]:
    value = _exact(
        value,
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
        "branch authority binding",
    )
    _object_ref(value["grant"], "branch grant")
    _object_ref(value["proof"], "trusted CI proof")
    if (
        value["schema"] != AUTHORITY_SCHEMA
        or value["transaction_id"] != transaction_id
        or value["repository_id"] != REPOSITORY["id"]
        or value["branch"] != "main"
        or value["source_revision"] != source
        or value["source_tree"] != tree
        or value["target"] != "staging"
        or value["scope"] != BRANCH_SCOPE
        or not isinstance(value["expires_at"], str)
        or not _UTC.fullmatch(value["expires_at"])
    ):
        raise HandoffError("branch authority binding differs from canonical main staging")
    return deepcopy(value)


def _native_release(
    value: Any,
    producer_readback: dict[str, Any],
    gate_readback: dict[str, Any],
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any], dict[str, Any]]:
    value = _exact(
        value,
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
        "native release",
    )
    if value["schema"] != NATIVE_RELEASE_SCHEMA or value["provider"] != "aws.codebuild":
        raise HandoffError("native release schema or provider is invalid")
    source = _hex(value["source_revision"], _SHA, "release source")
    tree = _hex(value["source_tree"], _SHA, "release tree")
    producer = _build_identity(value["producer"], "producer", PRODUCER_PROJECT)
    producer_status = _successful_readback(producer_readback, producer, "producer")

    services = value["services"]
    if not isinstance(services, dict) or set(services) != set(SERVICES):
        raise HandoffError("native release does not contain exactly five services")
    projection: dict[str, Any] = {}
    for name in SERVICES:
        service = _exact(
            services[name],
            {"repository", "image_digest", "source_revision", "native_build_number"},
            f"{name} service",
        )
        if (
            service["repository"] != f"leaf-platform-{name}"
            or service["source_revision"] != source
            or service["native_build_number"] != producer["build_number"]
            or not isinstance(service["image_digest"], str)
            or not _DIGEST.fullmatch(service["image_digest"])
        ):
            raise HandoffError(f"{name} service differs from the native release")
        projection[name] = {
            "repository": service["repository"],
            "image_digest": service["image_digest"],
            "source_revision": source,
        }

    solver = _exact(value["solver"], {"revision", "source_sha256"}, "solver provenance")
    solver_revision = _hex(solver["revision"], _SHA, "solver revision")
    solver_hash = _hex(solver["source_sha256"], _SHA256, "solver source digest")
    projection["canonical-worker"]["provenance"] = {
        "application_source_revision": source,
        "solver_source_revision": solver_revision,
        "solver_source_sha256": solver_hash,
    }

    web = _exact(value["web"], {"member", "artifact_sha256", "archive_sha256"}, "web release")
    if web["member"] != "web-dist.zip":
        raise HandoffError("web release member is not canonical")
    web_hash = _hex(web["artifact_sha256"], _SHA256, "web artifact digest")
    _hex(web["archive_sha256"], _SHA256, "web archive digest")
    projection["web"]["artifact_sha256"] = web_hash

    gate = _exact(
        value["gate"],
        {"producer", "source_revision", "source_tree", "archive", "proof_sha256"},
        "gate evidence",
    )
    gate_identity = _build_identity(gate["producer"], "gate producer", GATE_PROJECT)
    gate_status = _successful_readback(gate_readback, gate_identity, "gate")
    if gate["source_revision"] != source or gate["source_tree"] != tree:
        raise HandoffError("gate source differs from the native release")
    gate_archive = _object_ref(gate["archive"], "gate archive")
    gate_id = gate_identity["build_arn"].rsplit(":", 1)[-1]
    if (
        gate_archive["bucket"] != RELEASE_BUCKET
        or gate_archive["key"] != f"gate/{gate_id}/evidence.zip"
    ):
        raise HandoffError("gate archive is outside the canonical native lane")
    _hex(gate["proof_sha256"], _SHA256, "gate proof digest")
    return deepcopy(value), projection, producer_status, gate_status


def build_handoff(
    *,
    release_object: dict[str, Any],
    release_archive: bytes,
    producer_readback: dict[str, Any],
    gate_readback: dict[str, Any],
    semantic_live: dict[str, Any],
    live_binding: dict[str, Any],
    transaction: dict[str, Any],
    forge_head: dict[str, Any],
    producer_contract: dict[str, Any],
    branch_authority: dict[str, Any],
) -> dict[str, Any]:
    """Validate authenticated native evidence and return one canonical v2 handoff."""
    if not isinstance(transaction, dict):
        raise HandoffError("transaction readback is invalid")
    transaction_id = _hex(transaction.get("transaction_id"), _TX, "transaction id")
    release_ref = _delivery_ref(
        release_object,
        transaction_id,
        "native-release.zip",
        "native release object",
        revision=True,
    )
    release_manifest, web_archive = _release_members(release_archive, release_ref)
    native, services, producer_status, gate_status = _native_release(
        release_manifest, producer_readback, gate_readback
    )
    source, tree = native["source_revision"], native["source_tree"]
    if hashlib.sha256(web_archive).hexdigest() != native[
        "web"
    ]["archive_sha256"]:
        raise HandoffError("web archive bytes differ from the native release")
    if _web_content_digest(web_archive) != native["web"]["artifact_sha256"]:
        raise HandoffError("web archive content differs from the native release")

    semantic = _exact(
        semantic_live,
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
    if semantic["schema"] != SEMANTIC_SCHEMA or semantic["settled"] is not True:
        raise HandoffError("semantic staging receipt is not settled LIVE evidence")
    if semantic["transaction_id"] != transaction_id:
        raise HandoffError("semantic receipt belongs to another transaction")
    if semantic["source_revision"] != source or semantic["source_tree"] != tree:
        raise HandoffError("staging source differs from the native release")
    repository = _repository(semantic["repository"], "semantic repository")
    if (
        repository != REPOSITORY
        or semantic["branch"] != "main"
        or not isinstance(semantic["activity"], (dict, type(None)))
    ):
        raise HandoffError("semantic staging branch or activity is invalid")
    observed = semantic["services"]
    if not isinstance(observed, dict) or set(observed) != set(SERVICES):
        raise HandoffError("semantic receipt does not contain exactly five services")
    images: dict[str, str] = {}
    service_evidence: dict[str, dict[str, Any]] = {}
    for name in SERVICES:
        row = _exact(observed[name], {"image", "config", "tags", "route", "evidence"}, f"{name} semantic service")
        if row["image"] != services[name]["image_digest"]:
            raise HandoffError(f"{name} staging image differs from the release")
        _hex(row["config"], _SHA256, f"{name} config digest")
        _staging_tags(row["tags"], source, name)
        _staging_route(row["route"], name)
        service_evidence[name] = _delivery_ref(
            row["evidence"],
            transaction_id,
            "readback.json",
            f"{name} semantic evidence",
            revision=True,
        )
        images[name] = row["image"]

    binding = _exact(
        live_binding,
        {
            "schema",
            "repository",
            "branch",
            "transaction_id",
            "source_revision",
            "source_tree",
            "semantic_receipt",
            "revision",
        },
        "LIVE binding",
    )
    if (
        binding["schema"] != LIVE_BINDING_SCHEMA
        or binding["transaction_id"] != transaction_id
        or binding["source_revision"] != source
        or binding["source_tree"] != tree
        or binding["repository"] != repository
        or binding["branch"] != "main"
        or type(binding["revision"]) is not int
        or binding["revision"] < 1
    ):
        raise HandoffError("LIVE binding differs from staging evidence")
    live_semantic_ref = _object_ref(binding["semantic_receipt"], "LIVE binding semantic receipt")
    if hashlib.sha256(_canonical(semantic)).hexdigest() != live_semantic_ref["sha256"]:
        raise HandoffError("semantic receipt bytes differ from the LIVE binding")

    if (
        transaction.get("schema") != TRANSACTION_SCHEMA
        or transaction.get("transaction_id") != transaction_id
        or transaction.get("state") != "LIVE"
        or transaction.get("repository") != repository
        or transaction.get("branch") != "main"
        or transaction.get("commit") != source
        or transaction.get("tree") != tree
        or type(transaction.get("revision")) is not int
        or transaction["revision"] < 1
    ):
        raise HandoffError("transaction readback is not the same LIVE release")
    semantic_ref = _delivery_ref(
        transaction.get("semantic_receipt"),
        transaction_id,
        "semantic-live.json",
        "transaction semantic receipt",
        revision=True,
    )
    if {key: semantic_ref[key] for key in ("bucket", "key", "version_id", "sha256")} != live_semantic_ref:
        raise HandoffError("transaction semantic receipt differs from the LIVE binding")
    authority_ref = _transaction_ref(
        transaction.get("authority"),
        transaction_id,
        "authority-tx.json",
        "transaction authority receipt",
    )
    trusted_contract = _producer_contract(producer_contract, "trusted producer contract")
    if _producer_contract(transaction.get("producer_contract"), "transaction producer contract") != trusted_contract:
        raise HandoffError("transaction producer contract differs from the trusted pin")
    authority = _branch_authority(
        branch_authority,
        transaction_id=transaction_id,
        source=source,
        tree=tree,
    )
    if hashlib.sha256(_canonical(authority)).hexdigest() != authority_ref["sha256"]:
        raise HandoffError("branch authority bytes differ from the transaction receipt")

    head = _exact(forge_head, {"repository", "branch", "commit", "tree"}, "Forge head readback")
    if (
        _repository(head["repository"], "Forge head repository") != REPOSITORY
        or head["branch"] != "main"
        or head["commit"] != source
        or head["tree"] != tree
    ):
        raise HandoffError("release source is not canonical main")

    native_manifest_sha = hashlib.sha256(_canonical(native)).hexdigest()
    return {
        "schema": HANDOFF_SCHEMA,
        "provider": "forge-native",
        "source_revision": source,
        "source_tree": tree,
        "release": {
            "transaction_id": transaction_id,
            "source_revision": source,
            "source_tree": tree,
            "producer": deepcopy(native["producer"]),
            "producer_readback": producer_status,
            "gate": deepcopy(native["gate"]),
            "gate_readback": gate_status,
            "artifact": release_ref,
            "manifest_sha256": native_manifest_sha,
            "producer_contract": trusted_contract,
        },
        "staging_acceptance": {
            "schema": semantic["schema"],
            "transaction_id": transaction_id,
            "environment": "staging",
            "state": "LIVE",
            "semantic_receipt": semantic_ref,
            "live_binding_revision": binding["revision"],
            "repository": repository,
            "branch": "main",
            "source_revision": source,
            "source_tree": tree,
            "images": images,
            "web": {
                "artifact_sha256": native["web"]["artifact_sha256"],
                "archive_sha256": native["web"]["archive_sha256"],
            },
            "service_evidence": service_evidence,
            "authority_receipt": authority_ref,
            "branch_authority": authority,
            "transaction_readback": {
                "schema": transaction["schema"],
                "transaction_id": transaction_id,
                "state": transaction["state"],
                "repository": repository,
                "branch": transaction["branch"],
                "commit": transaction["commit"],
                "tree": transaction["tree"],
                "semantic_receipt": semantic_ref,
                "authority_receipt": authority_ref,
                "producer_contract": trusted_contract,
                "revision": transaction["revision"],
            },
            "canonical_head": deepcopy(head),
            "settled": True,
        },
        "staging_supply_set_services": services,
        "web": {
            **deepcopy(native["web"]),
            "release_object": release_ref,
        },
        "proof": {
            "source_is_canonical_main": True,
            "producer_and_gate_succeeded": True,
            "staging_state_is_live": True,
            "staging_digests_equal_release": True,
            "web_archive_bytes_equal_release": True,
        },
    }


def canonical_handoff_bytes(value: dict[str, Any]) -> bytes:
    """Return deterministic bytes for a handoff already produced by this module."""
    if value.get("schema") != HANDOFF_SCHEMA or value.get("provider") != "forge-native":
        raise HandoffError("handoff is not Forge-native v2")
    return _canonical(value)


def build_handoff_bundle(**evidence: Any) -> dict[str, bytes]:
    """Return the closed, no-rebuild artifact set for a verified native release."""
    handoff = build_handoff(**evidence)
    reference = _delivery_ref(
        evidence.get("release_object"),
        handoff["release"]["transaction_id"],
        "native-release.zip",
        "native release object",
        revision=True,
    )
    manifest, web_archive = _release_members(evidence.get("release_archive"), reference)
    semantic = evidence.get("semantic_live")
    if not isinstance(semantic, dict):
        raise HandoffError("semantic LIVE receipt is missing")
    return {
        "staging-supply-set.json": _canonical(manifest),
        "web-dist.zip": web_archive,
        "semantic-live.json": _canonical(semantic),
        "production-handoff-candidate.json": canonical_handoff_bytes(handoff),
    }
