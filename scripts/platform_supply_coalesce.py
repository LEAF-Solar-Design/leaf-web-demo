#!/usr/bin/env python3
"""Dormant planning for one complete five-service supply lineage."""

from __future__ import annotations

from collections.abc import Mapping
from copy import deepcopy
import re
from typing import Any

from platform_semantic_eligibility import ContractError, sha256_digest


SERVICES = ("app", "broker", "canonical-worker", "harness", "web")
_SHA = re.compile(r"^[0-9a-f]{40}$")
_DIGEST = re.compile(r"^sha256:[0-9a-f]{64}$")
_SERVICE_KEYS = {
    "name",
    "image_digest",
    "provenance_digest",
    "producer_source_revision",
    "producer_source_tree",
    "surface_fingerprint",
    "recipe_fingerprint",
    "toolchain_digest",
    "dependencies_digest",
    "build_arguments_digest",
    "required_config_digest",
}


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


def _service_manifest(raw: Any) -> dict[str, dict[str, Any]]:
    if not isinstance(raw, list):
        raise ContractError("SUPPLY_SERVICE_SET_INVALID")
    result: dict[str, dict[str, Any]] = {}
    for item in raw:
        service = _exact(item, _SERVICE_KEYS, "SUPPLY_SERVICE_INVALID")
        name = service["name"]
        if name not in SERVICES or name in result:
            raise ContractError("SUPPLY_SERVICE_SET_INVALID")
        _digest(service["image_digest"], "SUPPLY_PROVENANCE_INVALID")
        _digest(service["provenance_digest"], "SUPPLY_PROVENANCE_INVALID")
        _sha(service["producer_source_revision"], "SUPPLY_PROVENANCE_INVALID")
        _sha(service["producer_source_tree"], "SUPPLY_PROVENANCE_INVALID")
        for key in (
            "surface_fingerprint",
            "recipe_fingerprint",
            "toolchain_digest",
            "dependencies_digest",
            "build_arguments_digest",
            "required_config_digest",
        ):
            _digest(service[key], "SUPPLY_PROVENANCE_INVALID")
        result[name] = service
    if set(result) != set(SERVICES):
        raise ContractError("SUPPLY_SERVICE_SET_INVALID")
    return result


def _build_identity(service: Mapping[str, Any]) -> tuple[str, ...]:
    return tuple(
        service[key]
        for key in (
            "image_digest",
            "provenance_digest",
            "producer_source_revision",
            "producer_source_tree",
            "surface_fingerprint",
            "recipe_fingerprint",
            "toolchain_digest",
            "dependencies_digest",
            "build_arguments_digest",
            "required_config_digest",
        )
    )


def evaluate_supply_coalescing(
    document: Mapping[str, Any], *, fixture_enabled: bool = False
) -> dict[str, Any]:
    """Plan at most one lineage and refuse partial or changed supply evidence."""

    if not fixture_enabled:
        raise ContractError("UNCONFIGURED")
    root = _exact(
        document,
        {"schema", "selector", "admission_window_digest", "movements"},
        "COALESCE_INPUT_INVALID",
    )
    if root["schema"] != "leaf.platform-supply-coalesce-input.v1" or root["selector"] != "UNCONFIGURED":
        raise ContractError("COALESCE_INPUT_INVALID")
    window = _digest(root["admission_window_digest"], "COALESCE_INPUT_INVALID")
    if not isinstance(root["movements"], list) or not 1 <= len(root["movements"]) <= 100:
        raise ContractError("COALESCE_INPUT_INVALID")

    movements: list[dict[str, Any]] = []
    trees: set[str] = set()
    for raw in root["movements"]:
        movement = _exact(
            raw,
            {"source_tree", "impact_classification", "impact_digest", "services"},
            "COALESCE_MOVEMENT_INVALID",
        )
        tree = _sha(movement["source_tree"], "COALESCE_MOVEMENT_INVALID")
        if tree in trees:
            raise ContractError("COALESCE_MOVEMENT_DUPLICATE")
        trees.add(tree)
        if movement["impact_classification"] not in {"nil_impact", "product_impact"}:
            raise ContractError("COALESCE_MOVEMENT_INVALID")
        _digest(movement["impact_digest"], "COALESCE_MOVEMENT_INVALID")
        movement["services"] = _service_manifest(movement["services"])
        movements.append(movement)

    baseline = movements[0]["services"]
    changed_services: list[str] = []
    for name in SERVICES:
        expected = _build_identity(baseline[name])
        if any(_build_identity(movement["services"][name]) != expected for movement in movements[1:]):
            changed_services.append(name)
    product_impact = any(
        movement["impact_classification"] != "nil_impact" for movement in movements
    )
    if product_impact:
        decision = "refuse"
        reason = "product_impact_present"
        lineage = None
    elif changed_services:
        decision = "refuse"
        reason = "producer_inputs_changed"
        lineage = None
    else:
        decision = "plan"
        reason = "complete_supply_equivalent"
        lineage = sha256_digest(
            {
                "admission_window_digest": window,
                "source_tree": movements[-1]["source_tree"],
                "services": [deepcopy(baseline[name]) for name in SERVICES],
            }
        )

    return {
        "schema": "leaf.platform-supply-coalesce.v1",
        "state": "SHADOW",
        "decision": decision,
        "reason_code": reason,
        "movement_count": len(movements),
        "affected_services": changed_services,
        "admission_window_digest": window,
        "planned_lineage_digest": lineage,
        "selector_activation_authorized": False,
        "supply_mint_authorized": False,
    }


def workflow_preflight() -> dict[str, Any]:
    return {
        "schema": "leaf.platform-supply-coalesce-preflight.v1",
        "state": "UNCONFIGURED",
        "selector_activation_authorized": False,
        "supply_mint_authorized": False,
    }


__all__ = ["SERVICES", "evaluate_supply_coalescing", "workflow_preflight"]
