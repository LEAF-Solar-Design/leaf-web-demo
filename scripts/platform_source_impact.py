#!/usr/bin/env python3
"""Dormant source-impact classification from one verified typed token."""

from __future__ import annotations

from collections.abc import Mapping
from copy import deepcopy
from dataclasses import dataclass
import re
from typing import Any

from platform_semantic_eligibility import ContractError, sha256_digest


SERVICES = ("app", "broker", "canonical-worker", "harness", "web")
PRODUCER_REPOSITORY = "LEAF-Solar-Design/leaf-web-demo"
PRODUCER_WORKFLOW = ".github/workflows/build-platform-images.yml"
FIXTURE_NOW = 1786638600
_SHA = re.compile(r"^[0-9a-f]{40}$")
_DIGEST = re.compile(r"^sha256:[0-9a-f]{64}$")
_TASK_DEFINITION = re.compile(r"^leaf-platform-[a-z-]+:[1-9][0-9]*$")
_ARTIFACT_NAME = re.compile(r"^staging-supply-set-[0-9a-f]{40}-attempt-[1-9][0-9]*$")
_INPUT_CLASSES = (
    "base_images",
    "build_args",
    "dependencies",
    "dockerfile",
    "required_config",
    "source_inputs",
    "toolchain",
)
_MAX_EVIDENCE_LIFETIME_SECONDS = 31 * 24 * 60 * 60


def _exact(value: Any, keys: set[str], code: str) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise ContractError(code)
    observed = tuple(value)
    if len(observed) != len(keys) or set(observed) != keys:
        raise ContractError(code)
    return dict(value)


def _sha(value: Any, code: str) -> str:
    if not isinstance(value, str) or _SHA.fullmatch(value) is None:
        raise ContractError(code)
    return value


def _digest(value: Any, code: str) -> str:
    if not isinstance(value, str) or _DIGEST.fullmatch(value) is None:
        raise ContractError(code)
    return value


def _integer(value: Any, minimum: int, code: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise ContractError(code)
    return value


@dataclass(frozen=True, slots=True, init=False)
class TrustedProducerRoots:
    """Opaque roots capability. Only the producer-owned private loader may mint it."""

    token_content_digest: str
    producer_identity_digest: str
    artifact_identity_digest: str
    base_source_revision: str
    base_source_tree: str
    candidate_source_revision: str
    candidate_source_tree: str
    supply_digest: str
    graph_digest: str
    deployment_identity_digest: str
    deployment_identity_body_digest: str
    terminal_receipt_digest: str
    release_lineage_digest: str
    tenant_binding_digest: str
    approval_scope_digest: str
    rollback_digest: str
    verifier_digest: str
    topology_digest: str
    release_scope_digest: str
    environment: str
    expires_at_epoch: int

    def __new__(cls, *_args: Any, **_kwargs: Any) -> TrustedProducerRoots:
        raise TypeError("trusted roots have no public constructor")


@dataclass(frozen=True, slots=True, init=False)
class ValidatedProducerEvidenceToken:
    """Immutable result returned only by ``verify_producer_evidence_token``."""

    content_digest: str
    producer_identity_digest: str
    artifact_identity_digest: str
    base_source_revision: str
    base_source_tree: str
    candidate_source_revision: str
    candidate_source_tree: str
    supply_digest: str
    graph_digest: str
    deployment_identity_digest: str
    deployment_identity_body_digest: str
    terminal_receipt_digest: str
    release_lineage_digest: str
    tenant_binding_digest: str
    approval_scope_digest: str
    rollback_digest: str
    verifier_digest: str
    topology_digest: str
    release_scope_digest: str
    environment: str
    services: tuple[tuple[str, str], ...]
    changed_services: tuple[str, ...]
    incomplete_services: tuple[str, ...]
    graph_complete: bool
    expires_at_epoch: int

    def __new__(cls, *_args: Any, **_kwargs: Any) -> ValidatedProducerEvidenceToken:
        raise TypeError("validated tokens have no public constructor")


def _new_opaque(cls: type[Any], values: Mapping[str, Any]) -> Any:
    instance = object.__new__(cls)
    for field, value in values.items():
        object.__setattr__(instance, field, value)
    return instance


def _canonical_named_digests(raw: Any, code: str) -> tuple[tuple[str, str], ...]:
    if not isinstance(raw, list) or len(raw) != len(SERVICES):
        raise ContractError(code)
    result: list[tuple[str, str]] = []
    for expected, item in zip(SERVICES, raw, strict=True):
        row = _exact(item, {"name", "image_digest"}, code)
        if row["name"] != expected:
            raise ContractError(code)
        result.append((expected, _digest(row["image_digest"], code)))
    return tuple(result)


def _canonical_graph(
    raw: Any, supply: tuple[tuple[str, str], ...]
) -> tuple[dict[str, Any], tuple[str, ...], tuple[str, ...]]:
    graph = _exact(
        raw,
        {"schema", "version", "complete", "services"},
        "PRODUCER_GRAPH_INVALID",
    )
    if (
        graph["schema"] != "leaf.platform-producer-input-graph.v1"
        or graph["version"] != 1
        or not isinstance(graph["complete"], bool)
        or not isinstance(graph["services"], list)
        or len(graph["services"]) != len(SERVICES)
    ):
        raise ContractError("PRODUCER_GRAPH_INVALID")
    supply_map = dict(supply)
    canonical_services: list[dict[str, Any]] = []
    changed: list[str] = []
    incomplete: list[str] = []
    for expected, raw_service in zip(SERVICES, graph["services"], strict=True):
        service = _exact(
            raw_service,
            {
                "name",
                "complete",
                "image_digest",
                "old_fingerprint",
                "new_fingerprint",
                "input_classes",
            },
            "PRODUCER_GRAPH_INVALID",
        )
        if service["name"] != expected or not isinstance(service["complete"], bool):
            raise ContractError("SERVICE_SET_INVALID")
        if _digest(service["image_digest"], "FINGERPRINT_INVALID") != supply_map[expected]:
            raise ContractError("PRODUCER_GRAPH_SUPPLY_MISMATCH")
        old_fingerprint = _digest(service["old_fingerprint"], "FINGERPRINT_INVALID")
        new_fingerprint = _digest(service["new_fingerprint"], "FINGERPRINT_INVALID")
        classes = _exact(
            service["input_classes"], set(_INPUT_CLASSES), "INPUT_CLASS_INVALID"
        )
        if any(not isinstance(classes[key], bool) for key in _INPUT_CLASSES):
            raise ContractError("INPUT_CLASS_INVALID")
        if not service["complete"] or not all(classes.values()):
            incomplete.append(expected)
        if old_fingerprint != new_fingerprint:
            changed.append(expected)
        canonical_services.append(deepcopy(service))
    return (
        {
            "schema": graph["schema"],
            "version": graph["version"],
            "complete": graph["complete"],
            "services": canonical_services,
        },
        tuple(changed),
        tuple(incomplete),
    )


def _token_payload_without_digests(token: Mapping[str, Any]) -> dict[str, Any]:
    return {
        key: deepcopy(value)
        for key, value in token.items()
        if key != "content_digest"
    }


def _seal_token(token: dict[str, Any]) -> dict[str, Any]:
    """Private fixture producer. Consumers never call this function."""

    token = deepcopy(token)
    token["producer_graph_digest"] = sha256_digest(token["producer_graph"])
    token["supply_digest"] = sha256_digest(token["supply"])
    token["deployment_identity_digest"] = sha256_digest(token["deployment_identity"])
    terminal = token["terminal"]
    for name in (
        "release_lineage",
        "tenant_binding",
        "approval_scope",
        "rollback",
        "verifier",
        "topology",
    ):
        terminal[f"{name}_digest"] = sha256_digest(terminal[name])
    token["release_scope_digest"] = sha256_digest(token["release_scope"])
    token["content_digest"] = sha256_digest(
        _token_payload_without_digests(token)
    )
    return token


def _fixture_token_payload() -> dict[str, Any]:
    """Exact dormant fixture shaped from the terminal Level 2 topology."""

    source_revision = "6ebf16d2b032d3e460669b7aed253d16d06f7fb4"
    supply = [
        {"name": "app", "image_digest": "sha256:f8af2d90bb86b088473570cfab1a6f4f7217fb7ce8ee07b332ffdb0b92a1bfba"},
        {"name": "broker", "image_digest": "sha256:de29217e7d9b0bea56fbf405971ccd5eeff3de2bdc905820bff04da5a45039cd"},
        {"name": "canonical-worker", "image_digest": "sha256:184a799fc4c577d8987af3926a3b9b66c88df23e81d78ca54c4b9e571b04bf18"},
        {"name": "harness", "image_digest": "sha256:8ecc697af13a0a1e61dc0b47c51c40a744ff0f0e86cf143deaad4708a72d326b"},
        {"name": "web", "image_digest": "sha256:21fa2e82576cb7808d35173000c0e0cd9131fed59d6cb3af678c9abc348c4faa"},
    ]
    supply_map = {item["name"]: item["image_digest"] for item in supply}
    graph_services = []
    for name in SERVICES:
        fingerprint = sha256_digest(
            {
                "service": name,
                "image_digest": supply_map[name],
                "manifest": "sha256:7dc138f6bc217a4f0f68a43faec242e0782d70b53781f587bf0ac41d7eba5f0d",
            }
        )
        graph_services.append(
            {
                "name": name,
                "complete": True,
                "image_digest": supply_map[name],
                "old_fingerprint": fingerprint,
                "new_fingerprint": fingerprint,
                "input_classes": {key: True for key in _INPUT_CLASSES},
            }
        )
    token = {
        "schema": "leaf.platform-producer-evidence-token.v1",
        "version": 1,
        "producer": {
            "repository": PRODUCER_REPOSITORY,
            "workflow": PRODUCER_WORKFLOW,
            "workflow_blob": "babae5cbbb819896e662499175c5c691d14e3573",
            "repository_id": 1304548236,
            "run_id": 31705183737,
        },
        "artifact": {
            "artifact_id": 9183077573,
            "artifact_name": f"staging-supply-set-{source_revision}-attempt-1",
            "archive_content_sha256": "sha256:bf17a6601f35cacba1c3c9de87146e4aad8db494b529cd41edabce2cc7be4f5e",
            "manifest_digest": "sha256:7dc138f6bc217a4f0f68a43faec242e0782d70b53781f587bf0ac41d7eba5f0d",
        },
        "source": {
            "base_revision": "41c24487ce3c25c923d28ffd4778818af6fb49fb",
            "base_tree": "698efba6b35b2a08eece8c548ba77f71d8859c21",
            "candidate_revision": source_revision,
            "candidate_tree": "0a2eaab98582526b8f9579f443b6965a945270ec",
        },
        "supply": {
            "manifest_digest": "sha256:7dc138f6bc217a4f0f68a43faec242e0782d70b53781f587bf0ac41d7eba5f0d",
            "services": supply,
        },
        "producer_graph": {
            "schema": "leaf.platform-producer-input-graph.v1",
            "version": 1,
            "complete": True,
            "services": graph_services,
        },
        "deployment_identity": {
            "schema": "leaf.deployment-identity.v1",
            "environment": "staging",
            "source_revision": source_revision,
            "services": [
                {"name": "app", "image_digest": supply_map["app"], "task_definition": "leaf-platform-app:607"},
                {"name": "broker", "image_digest": supply_map["broker"], "task_definition": "leaf-platform-broker:403"},
                {"name": "canonical-worker", "image_digest": supply_map["canonical-worker"], "task_definition": "leaf-platform-canonical-worker:173"},
                {"name": "harness", "image_digest": supply_map["harness"], "task_definition": "leaf-platform-harness:290"},
                {"name": "web", "image_digest": supply_map["web"], "task_definition": "leaf-platform-web:188"},
            ],
            "body_digest": "sha256:24c68a7622a5d2cd4d7fe1505de9556b65a7f26a00e83178f2e437a2d242ede3",
        },
        "terminal": {
            "status": "terminal_green",
            "receipt_digest": "sha256:151a522f52cbf5c492278037c864853950d78fa8de8fad2305d325a274089127",
            "release_lineage": {
                "predecessor_revision": "41c24487ce3c25c923d28ffd4778818af6fb49fb",
                "candidate_revision": source_revision,
                "candidate_tree": "0a2eaab98582526b8f9579f443b6965a945270ec",
                "manifest_digest": "sha256:7dc138f6bc217a4f0f68a43faec242e0782d70b53781f587bf0ac41d7eba5f0d",
            },
            "release_lineage_digest": "",
            "tenant_binding": {
                "environment": "staging",
                "tenant_scope": "level2-shared-staging",
                "tenant_set": ["leaf-platform-staging"],
            },
            "tenant_binding_digest": "",
            "approval_scope": {
                "class": "level2-terminal-convergence",
                "provider_mutation": False,
                "selectors": "UNCONFIGURED",
            },
            "approval_scope_digest": "",
            "rollback": {
                "source_revision": source_revision,
                "task_definitions": [
                    "leaf-platform-app:607",
                    "leaf-platform-broker:403",
                    "leaf-platform-canonical-worker:173",
                    "leaf-platform-harness:290",
                    "leaf-platform-web:188",
                ],
            },
            "rollback_digest": "",
            "verifier": {
                "contract": "leaf.optimization-l2-terminal.v1",
                "result": "pass",
                "receipt_digest": "sha256:151a522f52cbf5c492278037c864853950d78fa8de8fad2305d325a274089127",
            },
            "verifier_digest": "",
            "topology": {
                "drawing_fence": "open",
                "marker_count": 0,
                "p4a": "postgres/postgres/1",
                "route_priorities": [50, 51, 60],
                "writer_count": 0,
            },
            "topology_digest": "",
        },
        "release_scope": {
            "artifact_id": 9183077573,
            "candidate_revision": source_revision,
            "candidate_tree": "0a2eaab98582526b8f9579f443b6965a945270ec",
            "environment": "staging",
            "producer_repository": PRODUCER_REPOSITORY,
            "selector": "UNCONFIGURED",
            "tenant_scope": "level2-shared-staging",
        },
        "issued_at_epoch": 1786628168,
        "expires_at_epoch": 1789220167,
        "producer_graph_digest": "",
        "supply_digest": "",
        "deployment_identity_digest": "",
        "release_scope_digest": "",
        "content_digest": "",
    }
    return _seal_token(token)


def _fixture_trusted_roots() -> TrustedProducerRoots:
    """Private exact roots capability for dormant executable fixtures only."""

    token = _fixture_token_payload()
    producer_identity = sha256_digest(token["producer"])
    artifact_identity = sha256_digest(token["artifact"])
    source = token["source"]
    terminal = token["terminal"]
    values = {
        "token_content_digest": token["content_digest"],
        "producer_identity_digest": producer_identity,
        "artifact_identity_digest": artifact_identity,
        "base_source_revision": source["base_revision"],
        "base_source_tree": source["base_tree"],
        "candidate_source_revision": source["candidate_revision"],
        "candidate_source_tree": source["candidate_tree"],
        "supply_digest": token["supply_digest"],
        "graph_digest": token["producer_graph_digest"],
        "deployment_identity_digest": token["deployment_identity_digest"],
        "deployment_identity_body_digest": token["deployment_identity"]["body_digest"],
        "terminal_receipt_digest": terminal["receipt_digest"],
        "release_lineage_digest": terminal["release_lineage_digest"],
        "tenant_binding_digest": terminal["tenant_binding_digest"],
        "approval_scope_digest": terminal["approval_scope_digest"],
        "rollback_digest": terminal["rollback_digest"],
        "verifier_digest": terminal["verifier_digest"],
        "topology_digest": terminal["topology_digest"],
        "release_scope_digest": token["release_scope_digest"],
        "environment": token["deployment_identity"]["environment"],
        "expires_at_epoch": token["expires_at_epoch"],
    }
    return _new_opaque(TrustedProducerRoots, values)


def verify_producer_evidence_token(
    token_value: Any,
    trusted_roots: TrustedProducerRoots,
    *,
    now_epoch: int,
) -> ValidatedProducerEvidenceToken:
    """Strictly validate a full token against an opaque producer root set."""

    if type(trusted_roots) is not TrustedProducerRoots:
        raise ContractError("TRUSTED_ROOTS_UNCONFIGURED")
    now = _integer(now_epoch, 0, "PRODUCER_TOKEN_TIME_INVALID")
    token = _exact(
        token_value,
        {
            "schema",
            "version",
            "producer",
            "artifact",
            "source",
            "supply",
            "producer_graph",
            "producer_graph_digest",
            "supply_digest",
            "deployment_identity",
            "deployment_identity_digest",
            "terminal",
            "release_scope",
            "release_scope_digest",
            "issued_at_epoch",
            "expires_at_epoch",
            "content_digest",
        },
        "PRODUCER_TOKEN_INVALID",
    )
    if token["schema"] != "leaf.platform-producer-evidence-token.v1" or token["version"] != 1:
        raise ContractError("PRODUCER_TOKEN_VERSION_INVALID")

    producer = _exact(
        token["producer"],
        {"repository", "workflow", "workflow_blob", "repository_id", "run_id"},
        "PRODUCER_IDENTITY_INVALID",
    )
    if producer["repository"] != PRODUCER_REPOSITORY or producer["workflow"] != PRODUCER_WORKFLOW:
        raise ContractError("PRODUCER_IDENTITY_INVALID")
    _sha(producer["workflow_blob"], "PRODUCER_IDENTITY_INVALID")
    _integer(producer["repository_id"], 1, "PRODUCER_IDENTITY_INVALID")
    _integer(producer["run_id"], 1, "PRODUCER_IDENTITY_INVALID")
    producer_identity_digest = sha256_digest(producer)

    artifact = _exact(
        token["artifact"],
        {"artifact_id", "artifact_name", "archive_content_sha256", "manifest_digest"},
        "PRODUCER_ARTIFACT_INVALID",
    )
    _integer(artifact["artifact_id"], 1, "PRODUCER_ARTIFACT_INVALID")
    if not isinstance(artifact["artifact_name"], str) or _ARTIFACT_NAME.fullmatch(artifact["artifact_name"]) is None:
        raise ContractError("PRODUCER_ARTIFACT_INVALID")
    _digest(artifact["archive_content_sha256"], "PRODUCER_ARTIFACT_INVALID")
    _digest(artifact["manifest_digest"], "PRODUCER_ARTIFACT_INVALID")
    artifact_identity_digest = sha256_digest(artifact)

    source = _exact(
        token["source"],
        {"base_revision", "base_tree", "candidate_revision", "candidate_tree"},
        "PRODUCER_SOURCE_INVALID",
    )
    for key in source:
        _sha(source[key], "PRODUCER_SOURCE_INVALID")
    if artifact["artifact_name"] != f"staging-supply-set-{source['candidate_revision']}-attempt-1":
        raise ContractError("PRODUCER_ARTIFACT_SOURCE_MISMATCH")

    supply_value = _exact(token["supply"], {"manifest_digest", "services"}, "PRODUCER_SUPPLY_INVALID")
    if _digest(supply_value["manifest_digest"], "PRODUCER_SUPPLY_INVALID") != artifact["manifest_digest"]:
        raise ContractError("PRODUCER_MANIFEST_MISMATCH")
    services = _canonical_named_digests(supply_value["services"], "PRODUCER_SUPPLY_INVALID")
    canonical_supply = {
        "manifest_digest": supply_value["manifest_digest"],
        "services": [{"name": name, "image_digest": digest} for name, digest in services],
    }
    supply_digest = _digest(token["supply_digest"], "PRODUCER_SUPPLY_INVALID")
    if supply_digest != sha256_digest(canonical_supply):
        raise ContractError("PRODUCER_SUPPLY_DIGEST_MISMATCH")

    graph, changed, incomplete = _canonical_graph(token["producer_graph"], services)
    graph_digest = _digest(token["producer_graph_digest"], "PRODUCER_GRAPH_INVALID")
    if graph_digest != sha256_digest(graph):
        raise ContractError("PRODUCER_GRAPH_DIGEST_MISMATCH")

    identity = _exact(
        token["deployment_identity"],
        {"schema", "environment", "source_revision", "services", "body_digest"},
        "DEPLOYMENT_IDENTITY_INVALID",
    )
    if identity["schema"] != "leaf.deployment-identity.v1" or identity["environment"] != "staging":
        raise ContractError("DEPLOYMENT_IDENTITY_INVALID")
    if _sha(identity["source_revision"], "DEPLOYMENT_IDENTITY_INVALID") != source["candidate_revision"]:
        raise ContractError("DEPLOYMENT_IDENTITY_SOURCE_MISMATCH")
    if not isinstance(identity["services"], list) or len(identity["services"]) != len(SERVICES):
        raise ContractError("DEPLOYMENT_IDENTITY_INVALID")
    identity_services: list[dict[str, Any]] = []
    supply_map = dict(services)
    for expected, raw_service in zip(SERVICES, identity["services"], strict=True):
        row = _exact(raw_service, {"name", "image_digest", "task_definition"}, "DEPLOYMENT_IDENTITY_INVALID")
        if row["name"] != expected or row["image_digest"] != supply_map[expected]:
            raise ContractError("DEPLOYMENT_IDENTITY_SUPPLY_MISMATCH")
        _digest(row["image_digest"], "DEPLOYMENT_IDENTITY_INVALID")
        if not isinstance(row["task_definition"], str) or _TASK_DEFINITION.fullmatch(row["task_definition"]) is None:
            raise ContractError("DEPLOYMENT_IDENTITY_INVALID")
        identity_services.append(deepcopy(row))
    body_digest = _digest(identity["body_digest"], "DEPLOYMENT_IDENTITY_INVALID")
    canonical_identity = {
        "schema": identity["schema"],
        "environment": identity["environment"],
        "source_revision": identity["source_revision"],
        "services": identity_services,
        "body_digest": body_digest,
    }
    deployment_identity_digest = _digest(
        token["deployment_identity_digest"], "DEPLOYMENT_IDENTITY_INVALID"
    )
    if deployment_identity_digest != sha256_digest(canonical_identity):
        raise ContractError("DEPLOYMENT_IDENTITY_DIGEST_MISMATCH")

    terminal = _exact(
        token["terminal"],
        {
            "status",
            "receipt_digest",
            "release_lineage",
            "release_lineage_digest",
            "tenant_binding",
            "tenant_binding_digest",
            "approval_scope",
            "approval_scope_digest",
            "rollback",
            "rollback_digest",
            "verifier",
            "verifier_digest",
            "topology",
            "topology_digest",
        },
        "TERMINAL_RECEIPT_INVALID",
    )
    if terminal["status"] != "terminal_green":
        raise ContractError("TERMINAL_RECEIPT_INVALID")
    _digest(terminal["receipt_digest"], "TERMINAL_RECEIPT_INVALID")
    canonical_terminal_payloads: dict[str, dict[str, Any]] = {}
    for name, keys in (
        (
            "release_lineage",
            {
                "predecessor_revision",
                "candidate_revision",
                "candidate_tree",
                "manifest_digest",
            },
        ),
        (
            "tenant_binding",
            {"environment", "tenant_scope", "tenant_set"},
        ),
        (
            "approval_scope",
            {"class", "provider_mutation", "selectors"},
        ),
        ("rollback", {"source_revision", "task_definitions"}),
        ("verifier", {"contract", "result", "receipt_digest"}),
        (
            "topology",
            {
                "drawing_fence",
                "marker_count",
                "p4a",
                "route_priorities",
                "writer_count",
            },
        ),
    ):
        payload = _exact(
            terminal[name], keys, "TERMINAL_RECEIPT_INVALID"
        )
        digest_value = _digest(
            terminal[f"{name}_digest"], "TERMINAL_RECEIPT_INVALID"
        )
        if digest_value != sha256_digest(payload):
            raise ContractError("TERMINAL_PAYLOAD_DIGEST_MISMATCH")
        canonical_terminal_payloads[name] = payload
    release_lineage = canonical_terminal_payloads["release_lineage"]
    if (
        _sha(
            release_lineage["predecessor_revision"],
            "TERMINAL_RECEIPT_INVALID",
        )
        != source["base_revision"]
        or _sha(
            release_lineage["candidate_revision"],
            "TERMINAL_RECEIPT_INVALID",
        )
        != source["candidate_revision"]
        or _sha(
            release_lineage["candidate_tree"],
            "TERMINAL_RECEIPT_INVALID",
        )
        != source["candidate_tree"]
        or _digest(
            release_lineage["manifest_digest"],
            "TERMINAL_RECEIPT_INVALID",
        )
        != artifact["manifest_digest"]
    ):
        raise ContractError("RELEASE_LINEAGE_MISMATCH")
    tenant_binding = canonical_terminal_payloads["tenant_binding"]
    if (
        tenant_binding["environment"] != identity["environment"]
        or tenant_binding["tenant_scope"] != "level2-shared-staging"
        or tenant_binding["tenant_set"] != ["leaf-platform-staging"]
    ):
        raise ContractError("TENANT_BINDING_INVALID")
    approval_scope = canonical_terminal_payloads["approval_scope"]
    if approval_scope != {
        "class": "level2-terminal-convergence",
        "provider_mutation": False,
        "selectors": "UNCONFIGURED",
    }:
        raise ContractError("APPROVAL_SCOPE_INVALID")
    rollback = canonical_terminal_payloads["rollback"]
    if (
        rollback["source_revision"] != source["candidate_revision"]
        or rollback["task_definitions"]
        != [row["task_definition"] for row in identity_services]
    ):
        raise ContractError("ROLLBACK_BINDING_INVALID")
    verifier = canonical_terminal_payloads["verifier"]
    if (
        verifier["contract"] != "leaf.optimization-l2-terminal.v1"
        or verifier["result"] != "pass"
        or verifier["receipt_digest"] != terminal["receipt_digest"]
    ):
        raise ContractError("VERIFIER_BINDING_INVALID")
    topology = canonical_terminal_payloads["topology"]
    if topology != {
        "drawing_fence": "open",
        "marker_count": 0,
        "p4a": "postgres/postgres/1",
        "route_priorities": [50, 51, 60],
        "writer_count": 0,
    }:
        raise ContractError("TOPOLOGY_BINDING_INVALID")

    release_scope = _exact(
        token["release_scope"],
        {
            "artifact_id",
            "candidate_revision",
            "candidate_tree",
            "environment",
            "producer_repository",
            "selector",
            "tenant_scope",
        },
        "RELEASE_SCOPE_INVALID",
    )
    if release_scope != {
        "artifact_id": artifact["artifact_id"],
        "candidate_revision": source["candidate_revision"],
        "candidate_tree": source["candidate_tree"],
        "environment": identity["environment"],
        "producer_repository": producer["repository"],
        "selector": "UNCONFIGURED",
        "tenant_scope": tenant_binding["tenant_scope"],
    }:
        raise ContractError("RELEASE_SCOPE_INVALID")
    release_scope_digest = _digest(token["release_scope_digest"], "RELEASE_SCOPE_INVALID")
    expected_release_scope = sha256_digest(release_scope)
    if release_scope_digest != expected_release_scope:
        raise ContractError("RELEASE_SCOPE_DIGEST_MISMATCH")

    issued_at = _integer(token["issued_at_epoch"], 0, "PRODUCER_TOKEN_TIME_INVALID")
    expires_at = _integer(token["expires_at_epoch"], 1, "PRODUCER_TOKEN_TIME_INVALID")
    if issued_at > now or now >= expires_at or expires_at <= issued_at or expires_at - issued_at > _MAX_EVIDENCE_LIFETIME_SECONDS:
        raise ContractError("PRODUCER_TOKEN_EXPIRED")
    content_digest = _digest(token["content_digest"], "PRODUCER_TOKEN_INVALID")
    expected_content = sha256_digest(
        _token_payload_without_digests(token)
        | {
            "producer_graph_digest": graph_digest,
            "supply_digest": supply_digest,
            "deployment_identity_digest": deployment_identity_digest,
            "release_scope_digest": release_scope_digest,
        }
    )
    if content_digest != expected_content:
        raise ContractError("PRODUCER_TOKEN_CONTENT_MISMATCH")

    derived = {
        "token_content_digest": content_digest,
        "producer_identity_digest": producer_identity_digest,
        "artifact_identity_digest": artifact_identity_digest,
        "base_source_revision": source["base_revision"],
        "base_source_tree": source["base_tree"],
        "candidate_source_revision": source["candidate_revision"],
        "candidate_source_tree": source["candidate_tree"],
        "supply_digest": supply_digest,
        "graph_digest": graph_digest,
        "deployment_identity_digest": deployment_identity_digest,
        "deployment_identity_body_digest": body_digest,
        "terminal_receipt_digest": terminal["receipt_digest"],
        "release_lineage_digest": terminal["release_lineage_digest"],
        "tenant_binding_digest": terminal["tenant_binding_digest"],
        "approval_scope_digest": terminal["approval_scope_digest"],
        "rollback_digest": terminal["rollback_digest"],
        "verifier_digest": terminal["verifier_digest"],
        "topology_digest": terminal["topology_digest"],
        "release_scope_digest": release_scope_digest,
        "environment": identity["environment"],
        "expires_at_epoch": expires_at,
    }
    for field, value in derived.items():
        if getattr(trusted_roots, field) != value:
            raise ContractError("PRODUCER_TOKEN_UNTRUSTED")

    return _new_opaque(
        ValidatedProducerEvidenceToken,
        {
            "content_digest": content_digest,
            "producer_identity_digest": producer_identity_digest,
            "artifact_identity_digest": artifact_identity_digest,
            "base_source_revision": source["base_revision"],
            "base_source_tree": source["base_tree"],
            "candidate_source_revision": source["candidate_revision"],
            "candidate_source_tree": source["candidate_tree"],
            "supply_digest": supply_digest,
            "graph_digest": graph_digest,
            "deployment_identity_digest": deployment_identity_digest,
            "deployment_identity_body_digest": body_digest,
            "terminal_receipt_digest": terminal["receipt_digest"],
            "release_lineage_digest": terminal["release_lineage_digest"],
            "tenant_binding_digest": terminal["tenant_binding_digest"],
            "approval_scope_digest": terminal["approval_scope_digest"],
            "rollback_digest": terminal["rollback_digest"],
            "verifier_digest": terminal["verifier_digest"],
            "topology_digest": terminal["topology_digest"],
            "release_scope_digest": release_scope_digest,
            "environment": identity["environment"],
            "services": services,
            "changed_services": changed,
            "incomplete_services": incomplete,
            "graph_complete": graph["complete"],
            "expires_at_epoch": expires_at,
        },
    )


def _impact_from_validated(
    validated: ValidatedProducerEvidenceToken,
    *,
    relay_base_tree: str,
    deferred: bool,
) -> dict[str, Any]:
    relay_base = _sha(relay_base_tree, "SOURCE_TREE_INVALID")
    if not isinstance(deferred, bool):
        raise ContractError("SOURCE_IMPACT_INPUT_INVALID")
    if deferred and relay_base != validated.base_source_tree:
        classification = "product_impact"
        affected = list(SERVICES)
        reason = "deferred_reclassification_required"
    elif not validated.graph_complete or validated.incomplete_services:
        classification = "product_impact"
        affected = list(SERVICES) if not validated.graph_complete else [
            name
            for name in SERVICES
            if name in validated.incomplete_services or name in validated.changed_services
        ]
        reason = "producer_graph_incomplete"
    elif validated.changed_services:
        classification = "product_impact"
        affected = [name for name in SERVICES if name in validated.changed_services]
        reason = "producer_input_changed"
    else:
        classification = "nil_impact"
        affected = []
        reason = "producer_inputs_equal"
    impact_digest = sha256_digest(
        {
            "classification": classification,
            "reason_code": reason,
            "affected_services": affected,
            "base_source_revision": validated.base_source_revision,
            "base_source_tree": validated.base_source_tree,
            "candidate_source_revision": validated.candidate_source_revision,
            "candidate_source_tree": validated.candidate_source_tree,
            "relay_base_tree": relay_base,
            "producer_token_digest": validated.content_digest,
        }
    )
    return {
        "classification": classification,
        "reason_code": reason,
        "affected_services": affected,
        "relay_base_tree": relay_base,
        "impact_digest": impact_digest,
    }


def classify_source_impact(
    document: Mapping[str, Any],
    *,
    trusted_roots: TrustedProducerRoots | Any,
    now_epoch: int,
    fixture_enabled: bool = False,
) -> dict[str, Any]:
    """Verify the full token, then derive one closed source-impact result."""

    if not fixture_enabled:
        raise ContractError("UNCONFIGURED")
    root = _exact(
        document,
        {"schema", "selector", "relay_base_tree", "deferred", "producer_token"},
        "SOURCE_IMPACT_INPUT_INVALID",
    )
    if root["schema"] != "leaf.platform-source-impact-input.v3" or root["selector"] != "UNCONFIGURED":
        raise ContractError("SOURCE_IMPACT_INPUT_INVALID")
    validated = verify_producer_evidence_token(
        root["producer_token"], trusted_roots, now_epoch=now_epoch
    )
    impact = _impact_from_validated(
        validated,
        relay_base_tree=root["relay_base_tree"],
        deferred=root["deferred"],
    )
    return {
        "schema": "leaf.platform-source-impact.v3",
        "state": "SHADOW",
        **impact,
        "base_source_revision": validated.base_source_revision,
        "base_source_tree": validated.base_source_tree,
        "candidate_source_revision": validated.candidate_source_revision,
        "candidate_source_tree": validated.candidate_source_tree,
        "producer_token_digest": validated.content_digest,
        "release_scope_digest": validated.release_scope_digest,
        "selector_activation_authorized": False,
    }


def workflow_preflight() -> dict[str, Any]:
    return {
        "schema": "leaf.platform-source-impact-preflight.v1",
        "state": "UNCONFIGURED",
        "selector_activation_authorized": False,
        "deployment_effect": False,
    }


__all__ = [
    "PRODUCER_REPOSITORY",
    "PRODUCER_WORKFLOW",
    "SERVICES",
    "TrustedProducerRoots",
    "ValidatedProducerEvidenceToken",
    "classify_source_impact",
    "verify_producer_evidence_token",
    "workflow_preflight",
]
