#!/usr/bin/env python3
"""Dormant, producer-owned source-impact classification.

The classifier consumes versioned producer fingerprints, never path guesses.
Production use is intentionally unconfigured. Tests may opt into the fixture
adapter to exercise the closed decision contract without granting authority.
"""

from __future__ import annotations

from collections.abc import Mapping
from copy import deepcopy
import re
from typing import Any

from platform_semantic_eligibility import ContractError, sha256_digest


SERVICES = ("app", "broker", "canonical-worker", "harness", "web")
_SHA = re.compile(r"^[0-9a-f]{40}$")
_DIGEST = re.compile(r"^sha256:[0-9a-f]{64}$")
_INPUT_CLASSES = (
    "base_images",
    "build_args",
    "dependencies",
    "dockerfile",
    "required_config",
    "source_inputs",
    "toolchain",
)


def _exact(value: Any, keys: set[str], code: str) -> dict[str, Any]:
    if not isinstance(value, Mapping) or set(value) != keys:
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


def classify_source_impact(
    document: Mapping[str, Any], *, fixture_enabled: bool = False
) -> dict[str, Any]:
    """Return a closed impact decision for one exact producer graph."""

    if not fixture_enabled:
        raise ContractError("UNCONFIGURED")
    root = _exact(
        document,
        {
            "schema",
            "selector",
            "old_tree",
            "new_tree",
            "relay_base_tree",
            "deferred",
            "producer_graph",
        },
        "SOURCE_IMPACT_INPUT_INVALID",
    )
    if (
        root["schema"] != "leaf.platform-source-impact-input.v1"
        or root["selector"] != "UNCONFIGURED"
        or not isinstance(root["deferred"], bool)
    ):
        raise ContractError("SOURCE_IMPACT_INPUT_INVALID")
    old_tree = _sha(root["old_tree"], "SOURCE_TREE_INVALID")
    new_tree = _sha(root["new_tree"], "SOURCE_TREE_INVALID")
    relay_base = _sha(root["relay_base_tree"], "SOURCE_TREE_INVALID")
    graph = _exact(
        root["producer_graph"],
        {"schema", "version", "complete", "services"},
        "PRODUCER_GRAPH_INVALID",
    )
    if (
        graph["schema"] != "leaf.platform-producer-input-graph.v1"
        or not isinstance(graph["version"], str)
        or not re.fullmatch(r"v[1-9][0-9]*", graph["version"])
        or not isinstance(graph["complete"], bool)
        or not isinstance(graph["services"], list)
    ):
        raise ContractError("PRODUCER_GRAPH_INVALID")

    seen: set[str] = set()
    canonical_services: dict[str, dict[str, Any]] = {}
    changed: list[str] = []
    incomplete: list[str] = []
    for raw in graph["services"]:
        service = _exact(
            raw,
            {
                "name",
                "complete",
                "old_fingerprint",
                "new_fingerprint",
                "input_classes",
            },
            "PRODUCER_GRAPH_INVALID",
        )
        name = service["name"]
        if name not in SERVICES or name in seen or not isinstance(service["complete"], bool):
            raise ContractError("SERVICE_SET_INVALID")
        seen.add(name)
        canonical_services[name] = service
        old_fingerprint = _digest(service["old_fingerprint"], "FINGERPRINT_INVALID")
        new_fingerprint = _digest(service["new_fingerprint"], "FINGERPRINT_INVALID")
        classes = _exact(
            service["input_classes"], set(_INPUT_CLASSES), "INPUT_CLASS_INVALID"
        )
        if any(not isinstance(classes[key], bool) for key in _INPUT_CLASSES):
            raise ContractError("INPUT_CLASS_INVALID")
        complete = service["complete"] and all(classes.values())
        if not complete:
            incomplete.append(name)
        if old_fingerprint != new_fingerprint:
            changed.append(name)
    if seen != set(SERVICES):
        raise ContractError("SERVICE_SET_INVALID")

    if root["deferred"] and relay_base != old_tree:
        classification = "product_impact"
        affected = list(SERVICES)
        reason = "deferred_reclassification_required"
    elif not graph["complete"] or incomplete:
        classification = "product_impact"
        affected = list(SERVICES) if not graph["complete"] else [
            name for name in SERVICES if name in incomplete or name in changed
        ]
        reason = "producer_graph_incomplete"
    elif changed:
        classification = "product_impact"
        affected = [name for name in SERVICES if name in changed]
        reason = "producer_input_changed"
    else:
        classification = "nil_impact"
        affected = []
        reason = "producer_inputs_equal"

    return {
        "schema": "leaf.platform-source-impact.v1",
        "state": "SHADOW",
        "classification": classification,
        "reason_code": reason,
        "affected_services": affected,
        "old_tree": old_tree,
        "new_tree": new_tree,
        "relay_base_tree": relay_base,
        "producer_graph_digest": sha256_digest(
            {
                "schema": graph["schema"],
                "version": graph["version"],
                "complete": graph["complete"],
                "services": [deepcopy(canonical_services[name]) for name in SERVICES],
            }
        ),
        "selector_activation_authorized": False,
    }


def workflow_preflight() -> dict[str, Any]:
    return {
        "schema": "leaf.platform-source-impact-preflight.v1",
        "state": "UNCONFIGURED",
        "selector_activation_authorized": False,
        "deployment_effect": False,
    }


__all__ = ["SERVICES", "classify_source_impact", "workflow_preflight"]
