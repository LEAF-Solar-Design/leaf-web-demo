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
from typing import Any
import zipfile


SERVICES = ("app", "broker", "canonical-worker", "harness", "web")
HANDOFF_SCHEMA = "leaf.production-handoff-candidate.v2"
NATIVE_RELEASE_SCHEMA = "leaf.native-release.v1"
SEMANTIC_SCHEMA = "leaf.native-staging-semantic.v1"
LIVE_BINDING_SCHEMA = "leaf.native-staging-live-binding.v1"
TRANSACTION_SCHEMA = "leaf.native-staging-transaction.v1"

_SHA = re.compile(r"^[0-9a-f]{40}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_DIGEST = re.compile(r"^sha256:[0-9a-f]{64}$")
_TX = re.compile(r"^d10-[0-9a-f]{16}$")
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


def _build_identity(value: Any, label: str) -> dict[str, Any]:
    value = _exact(value, {"project_arn", "build_arn", "build_number"}, label)
    if not isinstance(value["project_arn"], str) or not _ARN.fullmatch(value["project_arn"]):
        raise HandoffError(f"{label} project is invalid")
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


def _native_release(
    value: Any,
    producer_readback: dict[str, Any],
    gate_readback: dict[str, Any],
) -> tuple[dict[str, Any], dict[str, Any]]:
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
    producer = _build_identity(value["producer"], "producer")
    _successful_readback(producer_readback, producer, "producer")

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
    gate_identity = _build_identity(gate["producer"], "gate producer")
    _successful_readback(gate_readback, gate_identity, "gate")
    if gate_identity["project_arn"] == producer["project_arn"]:
        raise HandoffError("gate did not use a separate project")
    if gate["source_revision"] != source or gate["source_tree"] != tree:
        raise HandoffError("gate source differs from the native release")
    _object_ref(gate["archive"], "gate archive")
    _hex(gate["proof_sha256"], _SHA256, "gate proof digest")
    return deepcopy(value), projection


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
) -> dict[str, Any]:
    """Validate authenticated native evidence and return one canonical v2 handoff."""
    release_ref = _object_ref(release_object, "native release object")
    release_manifest, web_archive = _release_members(release_archive, release_ref)
    native, services = _native_release(release_manifest, producer_readback, gate_readback)
    source, tree = native["source_revision"], native["source_tree"]
    if hashlib.sha256(web_archive).hexdigest() != native[
        "web"
    ]["archive_sha256"]:
        raise HandoffError("web archive bytes differ from the native release")

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
    transaction_id = _hex(semantic["transaction_id"], _TX, "transaction id")
    if semantic["source_revision"] != source or semantic["source_tree"] != tree:
        raise HandoffError("staging source differs from the native release")
    repository = _repository(semantic["repository"], "semantic repository")
    if semantic["branch"] != "main" or not isinstance(semantic["activity"], (dict, type(None))):
        raise HandoffError("semantic staging branch or activity is invalid")
    observed = semantic["services"]
    if not isinstance(observed, dict) or set(observed) != set(SERVICES):
        raise HandoffError("semantic receipt does not contain exactly five services")
    images: dict[str, str] = {}
    for name in SERVICES:
        row = _exact(observed[name], {"image", "config", "tags", "route", "evidence"}, f"{name} semantic service")
        if row["image"] != services[name]["image_digest"]:
            raise HandoffError(f"{name} staging image differs from the release")
        _hex(row["config"], _SHA256, f"{name} config digest")
        if not isinstance(row["tags"], dict) or not isinstance(row["route"], dict):
            raise HandoffError(f"{name} semantic metadata is invalid")
        _object_ref(row["evidence"], f"{name} semantic evidence", revision=True)
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
    semantic_ref = _object_ref(binding["semantic_receipt"], "semantic receipt")
    if hashlib.sha256(_canonical(semantic)).hexdigest() != semantic_ref["sha256"]:
        raise HandoffError("semantic receipt bytes differ from the LIVE binding")

    if (
        not isinstance(transaction, dict)
        or transaction.get("schema") != TRANSACTION_SCHEMA
        or transaction.get("transaction_id") != transaction_id
        or transaction.get("state") != "LIVE"
        or transaction.get("commit") != source
        or transaction.get("tree") != tree
        or transaction.get("semantic_receipt") != semantic_ref
    ):
        raise HandoffError("transaction readback is not the same LIVE release")

    head = _exact(forge_head, {"repository", "branch", "commit", "tree"}, "Forge head readback")
    if (
        _repository(head["repository"], "Forge head repository") != repository
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
            "producer": deepcopy(native["producer"]),
            "gate": deepcopy(native["gate"]),
            "artifact": release_ref,
            "manifest_sha256": native_manifest_sha,
        },
        "staging_acceptance": {
            "schema": semantic["schema"],
            "transaction_id": transaction_id,
            "semantic_receipt": semantic_ref,
            "live_binding_revision": binding["revision"],
            "repository": repository,
            "branch": "main",
            "source_revision": source,
            "source_tree": tree,
            "images": images,
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
    reference = _object_ref(evidence.get("release_object"), "native release object")
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
